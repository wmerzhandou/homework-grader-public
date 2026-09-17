"""监控富化测试：UA 解析、数据访问审计、可疑度打分、离线 IP 归属。"""

from __future__ import annotations

import pytest

from app.config import PROJECT_ROOT
from app.geoip import NETWORK_IDC, NETWORK_MOBILE, NETWORK_PRIVATE, lookup
from app.insights import audit_logs, parse_ua, risk_level, score_source
from app.models import AccessLog

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)


# --------------------------------------------------------------------------
# UA 解析
# --------------------------------------------------------------------------
def test_parse_ua_iphone():
    info = parse_ua(IPHONE_UA)
    assert info.device == "iPhone"
    assert info.os == "iOS 18.5"
    assert info.browser == "Safari 18"
    assert info.is_bot is False
    assert "iPhone" in info.label


def test_parse_ua_android_with_model():
    info = parse_ua(ANDROID_UA)
    assert info.device == "Android (SM-S918B)"
    assert info.os == "Android 14"
    assert info.browser == "Chrome 126"


def test_parse_ua_bot_and_empty():
    assert parse_ua("curl/8.5.0").is_bot is True
    empty = parse_ua("")
    assert empty.is_bot is True and empty.device == "无 UA"


# --------------------------------------------------------------------------
# 数据访问审计
# --------------------------------------------------------------------------
def _log(method: str, path: str, status: int = 200) -> AccessLog:
    return AccessLog(ip="203.0.113.1", method=method, path=path, status=status)


def test_audit_logs_collects_what_was_seen_and_taken():
    rows = [
        _log("GET", "/api/auth/login", 200),
        _log("GET", "/api/auth/login", 401),
        _log("POST", "/api/auth/login", 401),
        _log("GET", "/api/threads/" + "a" * 32),
        _log("GET", "/api/threads/" + "a" * 32),
        _log("GET", "/api/materials/" + "b" * 32 + "/file"),
        _log("GET", "/api/materials/" + "c" * 32 + "/file", 401),
        _log("POST", "/api/threads/" + "a" * 32 + "/materials", 200),
        _log("POST", "/api/threads/" + "a" * 32 + "/turns", 200),
        _log("GET", "/.env", 404),
    ]
    summary = audit_logs(
        rows,
        thread_titles={"a" * 32: "数学作业"},
        material_names={"b" * 32: ("作业.jpg", "数学作业")},
    )
    assert summary.logins_ok == 1
    assert summary.logins_failed == 2
    assert summary.threads_viewed == ["数学作业"]
    assert summary.materials_downloaded == ["作业.jpg（数学作业）"]  # 401 的那次不算
    assert summary.uploads == 1 and summary.turns == 1
    assert summary.sensitive_probes and summary.sensitive_probes[0].startswith("GET /.env")
    assert summary.not_found == 1


# --------------------------------------------------------------------------
# 可疑度打分
# --------------------------------------------------------------------------
def _score(**overrides):
    base = dict(
        requests=10,
        authenticated=True,
        usernames=["admin"],
        logins_failed=0,
        sensitive_probes=[],
        not_found=0,
        materials_downloaded=0,
        network_type="固网宽带",
        ua_is_bot=False,
        has_ua=True,
        device_known=True,
        device_trusted=False,
        source_trusted=False,
        is_new_source=False,
    )
    base.update(overrides)
    return score_source(**base)


def test_score_scanning_bot_from_datacenter():
    score, flags = _score(
        authenticated=False,
        usernames=[],
        network_type=NETWORK_IDC,
        ua_is_bot=True,
        has_ua=False,
        requests=300,
        sensitive_probes=["GET /.env → 404", "GET /.git/config → 404", "GET /wp-login → 404"],
        not_found=60,
        device_known=False,
    )
    assert score >= 60 and risk_level(score) == "高"
    assert any("云主机" in f for f in flags)
    assert any("敏感路径" in f for f in flags)


def test_score_trusted_family_device_is_low():
    score, flags = _score(device_trusted=True, requests=50)
    assert score < 30 and risk_level(score) == "低"
    assert any("可信设备" in f for f in flags)


def test_score_trusted_source_is_low_and_missing_ua_is_mild():
    score, flags = _score(
        requests=200,
        authenticated=False,
        usernames=[],
        has_ua=False,
        ua_is_bot=True,
        source_trusted=True,
    )
    assert score < 30
    assert any("可信来源" in f for f in flags)
    # 空 UA 只轻微扣分，不再等同于"爬虫"重罚
    assert any("无 User-Agent" in f for f in flags)
    assert not any("不是正常浏览器" in f for f in flags)


def test_score_bruteforce_attempts():
    score, flags = _score(
        authenticated=False, usernames=[], logins_failed=12, device_known=False
    )
    assert score >= 30
    assert any("爆破" in f for f in flags)


# --------------------------------------------------------------------------
# 离线 IP 归属（依赖真实 mmdb，缺失时跳过）
# --------------------------------------------------------------------------
# 仓库自带的离线库（data/geoip 已 gitignore，没有就跳过相关用例）
_CITY_DB = PROJECT_ROOT / "data" / "geoip" / "GeoLite2-City.mmdb"
_ASN_DB = PROJECT_ROOT / "data" / "geoip" / "GeoLite2-ASN.mmdb"


def test_lookup_private_ip(settings):
    # 用通用的 RFC1918 地址，别把具体内网地址写进代码
    info = lookup("10.0.0.7")
    assert info.network_type == NETWORK_PRIVATE


@pytest.mark.skipif(
    not (_CITY_DB.is_file() and _ASN_DB.is_file()),
    reason="本机没有 GeoLite2 库",
)
def test_lookup_returns_provider_and_network_type(settings, monkeypatch):
    monkeypatch.setenv("GEOIP_CITY_DB", str(_CITY_DB))
    monkeypatch.setenv("GEOIP_ASN_DB", str(_ASN_DB))
    from app.config import get_settings
    from app.geoip import _readers

    get_settings.cache_clear()
    _readers.cache_clear()

    mobile = lookup("223.104.196.203")
    assert mobile.country == "中国"
    assert mobile.network_type == NETWORK_MOBILE
    assert "Mobile" in mobile.org

    google = lookup("8.8.8.8")
    assert google.network_type == NETWORK_IDC

    get_settings.cache_clear()
    _readers.cache_clear()
