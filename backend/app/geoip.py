"""离线 IP 归属查询：GeoLite2-City（城市/省份/国家）+ GeoLite2-ASN（运营商/机构）。

设计取舍：
- 完全离线查询，不把访客 IP 发给任何第三方接口；两个 mmdb 放在 DATA_DIR/geoip/。
- 结果写入 ipgeo 表做缓存，重复查询零成本；查询失败/库缺失时降级为"未知"，不影响主流程。
- network_type 是**推断值**：ASN 机构名 + 是否缺少城市信息。
  云厂商/VPN 出口的名单是按机构名关键字匹配的，宁可漏判也不误判。
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlmodel import Session

from .config import get_settings
from .models import IpGeo

logger = logging.getLogger(__name__)

# 云主机 / IDC / VPN 出口的机构关键字（命中就认为不是自然人家庭网络）
_HOSTING_KEYWORDS = (
    "amazon", "aws", "google", "microsoft", "azure", "oracle", "digitalocean", "vultr",
    "linode", "akamai", "hetzner", "ovh", "choopa", "cloudflare", "leaseweb", "contabo",
    "scaleway", "alibaba", "aliyun", "tencent", "huawei cloud", "ucloud", "m247",
    "datacamp", "colocrossing", "quadranet", "hostwinds", "hosting", "datacenter",
    "data communications", "idc", "cloud", "server", "vps", "dedicated",
)

NETWORK_IDC = "云主机/VPN"
NETWORK_MOBILE = "移动网络"
NETWORK_BROADBAND = "固网宽带"
NETWORK_FOREIGN = "国外网络"
NETWORK_PRIVATE = "内网"
NETWORK_UNKNOWN = "未知"


@dataclass(frozen=True)
class GeoInfo:
    country: str = ""
    province: str = ""
    city: str = ""
    asn: int | None = None
    org: str = ""
    network_type: str = NETWORK_UNKNOWN

    @property
    def location(self) -> str:
        """人类可读的位置：中国·山东 or 美国·加州 or 内网。"""
        if self.network_type == NETWORK_PRIVATE:
            return NETWORK_PRIVATE
        parts = [p for p in (self.country, self.province, self.city) if p]
        return " · ".join(parts) if parts else NETWORK_UNKNOWN

    @property
    def summary(self) -> str:
        """一行摘要：中国移动（山东）· 移动网络。"""
        org = self.org or "未知运营商"
        return f"{org}（{self.location}）· {self.network_type}" if self.location != NETWORK_PRIVATE else NETWORK_PRIVATE


def _load(path: Path):  # noqa: ANN202 - 返回 maxminddb.Reader 或 None
    if not path.is_file():
        return None
    try:
        import maxminddb

        return maxminddb.open_database(str(path))
    except Exception:  # noqa: BLE001 - 库损坏/未安装都不该影响业务
        logger.warning("GeoIP 库无法加载：%s", path, exc_info=True)
        return None


@lru_cache(maxsize=4)
def _readers(city_path: str, asn_path: str):
    return _load(Path(city_path)), _load(Path(asn_path))


def _is_private(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _classify(country: str, province: str, city: str, org: str) -> str:
    lowered = org.lower()
    if any(keyword in lowered for keyword in _HOSTING_KEYWORDS):
        return NETWORK_IDC
    if country == "中国":
        if "mobile" in lowered or "移动" in org:
            return NETWORK_MOBILE
        # 国内手机流量常见"只有省、没有市"，据此粗判；宁可标成固网也别误判成自然人
        if province and not city:
            return NETWORK_MOBILE
        return NETWORK_BROADBAND
    if country:
        return NETWORK_FOREIGN
    return NETWORK_UNKNOWN


def lookup(ip: str) -> GeoInfo:
    """离线查询一个 IP 的归属（不落库，纯查询）。"""
    if _is_private(ip):
        return GeoInfo(network_type=NETWORK_PRIVATE)
    settings = get_settings()
    city_reader, asn_reader = _readers(
        str(settings.geoip_city_db), str(settings.geoip_asn_db)
    )
    if city_reader is None and asn_reader is None:
        return GeoInfo()

    country = province = city = org = ""
    asn: int | None = None
    try:
        if city_reader is not None:
            record = city_reader.get(ip) or {}
            names = lambda node: (node or {}).get("names", {})  # noqa: E731
            country = names(record.get("country")).get("zh-CN") or names(record.get("country")).get("en") or ""
            subs = record.get("subdivisions") or []
            if subs:
                province = names(subs[0]).get("zh-CN") or names(subs[0]).get("en") or ""
            city = names(record.get("city")).get("zh-CN") or names(record.get("city")).get("en") or ""
        if asn_reader is not None:
            record = asn_reader.get(ip) or {}
            asn = record.get("autonomous_system_number")
            org = record.get("autonomous_system_organization") or ""
    except Exception:  # noqa: BLE001 - 查询失败不影响业务
        logger.warning("GeoIP 查询失败：%s", ip, exc_info=True)

    return GeoInfo(
        country=country,
        province=province,
        city=city,
        asn=asn,
        org=org,
        network_type=_classify(country, province, city, org),
    )


def cached_lookup(session: Session, ip: str) -> GeoInfo:
    """带数据库缓存的查询：命中 ipgeo 表就直接用，否则查库并写回。"""
    row = session.get(IpGeo, ip)
    if row is not None:
        return GeoInfo(
            country=row.country,
            province=row.province,
            city=row.city,
            asn=row.asn,
            org=row.org,
            network_type=row.network_type,
        )
    info = lookup(ip)
    session.add(
        IpGeo(
            ip=ip,
            country=info.country,
            province=info.province,
            city=info.city,
            asn=info.asn,
            org=info.org,
            network_type=info.network_type,
            resolved_at=datetime.now(timezone.utc),
        )
    )
    try:
        session.commit()
    except Exception:  # noqa: BLE001 - 缓存写入失败无所谓
        session.rollback()
    return info
