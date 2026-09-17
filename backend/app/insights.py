"""把访问记录翻译成"人话"：UA 解析、数据访问审计、可疑度打分。

这个模块只做纯计算（输入原始事实、输出结论），方便单测；不碰数据库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# User-Agent 解析（纯正则，不引第三方依赖）
# --------------------------------------------------------------------------
_BOT_RE = re.compile(
    r"bot|crawler|spider|slurp|curl|wget|python-|httpx|aiohttp|okhttp|axios|go-http|"
    r"headless|phantom|scrapy|nmap|masscan|zgrab",
    re.IGNORECASE,
)
_ANDROID_MODEL_RE = re.compile(r"Android [\d.]+;\s*([^;)]+?)(?:\s+Build[/)]|\))")


@dataclass(frozen=True)
class UaInfo:
    device: str = "未知设备"
    os: str = ""
    browser: str = ""
    is_bot: bool = False

    @property
    def label(self) -> str:
        parts = [p for p in (self.device, self.os, self.browser) if p]
        return " · ".join(parts) if parts else "未知客户端"


def parse_ua(user_agent: str) -> UaInfo:
    ua = (user_agent or "").strip()
    if not ua:
        return UaInfo(device="无 UA", is_bot=True)
    if _BOT_RE.search(ua):
        tool = "curl" if "curl" in ua.lower() else ("python" if "python" in ua.lower() or "httpx" in ua.lower() else "脚本/爬虫")
        return UaInfo(device=tool, is_bot=True)

    if "iPhone" in ua:
        device = "iPhone"
    elif "iPad" in ua:
        device = "iPad"
    elif "Android" in ua:
        match = _ANDROID_MODEL_RE.search(ua)
        device = f"Android ({match.group(1).strip()})" if match else "Android 设备"
    elif "Windows" in ua:
        device = "Windows 电脑"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        device = "Mac"
    elif "Linux" in ua:
        device = "Linux 设备"
    else:
        device = "未知设备"

    os_name = ""
    if match := re.search(r"(CPU iPhone OS|CPU OS) ([\d_]+)", ua):
        os_name = f"iOS {match.group(2).replace('_', '.')}"
    elif match := re.search(r"Android ([\d.]+)", ua):
        os_name = f"Android {match.group(1)}"
    elif match := re.search(r"Windows NT ([\d.]+)", ua):
        win = {"10.0": "10/11", "6.3": "8.1", "6.1": "7"}.get(match.group(1), match.group(1))
        os_name = f"Windows {win}"
    elif match := re.search(r"Mac OS X ([\d_]+)", ua):
        os_name = f"macOS {match.group(1).replace('_', '.')}"
    elif "Linux" in ua:
        os_name = "Linux"

    browser = ""
    if match := re.search(r"Edg/([\d.]+)", ua):
        browser = f"Edge {match.group(1).split('.')[0]}"
    elif match := re.search(r"OPR/([\d.]+)", ua):
        browser = f"Opera {match.group(1).split('.')[0]}"
    elif match := re.search(r"MicroMessenger/([\d.]+)", ua):
        browser = f"微信 {match.group(1)}"
    elif match := re.search(r"Chrome/([\d.]+)", ua):
        browser = f"Chrome {match.group(1).split('.')[0]}"
    elif match := re.search(r"Firefox/([\d.]+)", ua):
        browser = f"Firefox {match.group(1).split('.')[0]}"
    elif match := re.search(r"Version/([\d.]+).*Safari", ua):
        browser = f"Safari {match.group(1).split('.')[0]}"
    elif "Safari" in ua:
        browser = "Safari"

    return UaInfo(device=device, os=os_name, browser=browser)


# --------------------------------------------------------------------------
# 数据访问审计
# --------------------------------------------------------------------------
@dataclass
class AuditSummary:
    threads_viewed: list[str] = field(default_factory=list)
    materials_downloaded: list[str] = field(default_factory=list)
    uploads: int = 0
    turns: int = 0
    logins_ok: int = 0
    logins_failed: int = 0
    sensitive_probes: list[str] = field(default_factory=list)
    not_found: int = 0
    total: int = 0


_SENSITIVE_PATTERNS = (
    "/.env", "/.git", "/wp-login", "/wp-admin", "/phpmyadmin", "/admin.php", "/phpinfo",
    "/cgi-bin", "/actuator", "/.aws", "/config.json", "/server-status", "/shell",
)


def audit_logs(rows, *, thread_titles: dict[str, str], material_names: dict[str, tuple[str, str]]) -> AuditSummary:
    """把一串 AccessLog 汇总成"这个来源看过/拿过什么"。

    material_names: material_id -> (filename, thread_title)
    """
    summary = AuditSummary()
    seen_threads: set[str] = set()
    seen_materials: set[str] = set()
    for row in rows:
        summary.total += 1
        path = row.path
        if row.status >= 400:
            summary.not_found += 1 if row.status == 404 else 0
        lowered = path.lower()
        if any(p in lowered for p in _SENSITIVE_PATTERNS):
            label = f"{row.method} {path} → {row.status}"
            if label not in summary.sensitive_probes:
                summary.sensitive_probes.append(label)
        if path.startswith("/api/auth/login"):
            if row.status == 200:
                summary.logins_ok += 1
            else:
                summary.logins_failed += 1
        elif (match := re.match(r"^/api/threads/([0-9a-f]{32})$", path)):
            thread_id = match.group(1)
            if thread_id not in seen_threads:
                seen_threads.add(thread_id)
                summary.threads_viewed.append(thread_titles.get(thread_id, f"已删除会话({thread_id[:8]})"))
        elif (match := re.match(r"^/api/materials/([0-9a-f]{32})/file", path)):
            material_id = match.group(1)
            if row.status == 200 and material_id not in seen_materials:
                seen_materials.add(material_id)
                filename, thread_title = material_names.get(material_id, (f"已删除材料({material_id[:8]})", ""))
                summary.materials_downloaded.append(
                    f"{filename}（{thread_title}）" if thread_title else filename
                )
        elif re.match(r"^/api/threads/[0-9a-f]{32}/materials$", path) and row.method == "POST" and row.status < 400:
            summary.uploads += 1
        elif re.match(r"^/api/threads/[0-9a-f]{32}/turns$", path) and row.method == "POST" and row.status < 400:
            summary.turns += 1
    return summary


# --------------------------------------------------------------------------
# 可疑度打分
# --------------------------------------------------------------------------
RISK_HIGH = 60
RISK_MEDIUM = 30


def risk_level(score: int) -> str:
    if score >= RISK_HIGH:
        return "高"
    if score >= RISK_MEDIUM:
        return "中"
    return "低"


def score_source(
    *,
    requests: int,
    authenticated: bool,
    usernames: list[str],
    logins_failed: int,
    sensitive_probes: list[str],
    not_found: int,
    materials_downloaded: int,
    network_type: str,
    ua_is_bot: bool,
    has_ua: bool,
    device_known: bool,
    device_trusted: bool,
    source_trusted: bool = False,
    is_new_source: bool = False,
) -> tuple[int, list[str]]:
    """给一个访问来源打分（0-100）并返回解释性标签。"""
    score = 0
    flags: list[str] = []

    if network_type == "云主机/VPN":
        score += 35
        flags.append("来自云主机/VPN 出口（家庭网络不会是这类 IP）")
    if len(sensitive_probes) >= 3:
        score += 35
        flags.append(f"探测敏感路径 {len(sensitive_probes)} 次")
    elif sensitive_probes:
        score += 15
        flags.append("探测敏感路径")
    if logins_failed >= 5:
        score += 30
        flags.append(f"登录失败 {logins_failed} 次（疑似爆破）")
    elif logins_failed:
        score += 10
        flags.append(f"登录失败 {logins_failed} 次")
    if not has_ua:
        # 空 UA 既可能是脚本，也可能是加固前的历史记录，所以只轻量扣分
        score += 10
        flags.append("无 User-Agent（脚本特征或加固前的旧记录）")
    elif ua_is_bot:
        score += 20
        flags.append("客户端不是正常浏览器（脚本/爬虫特征）")
    if requests >= 100 and not authenticated:
        score += 20
        flags.append(f"未登录却请求 {requests} 次")
    elif requests >= 500:
        score += 10
        flags.append(f"请求量异常（{requests} 次）")
    if not_found >= 20:
        score += 15
        flags.append(f"404 比例高（{not_found} 次）")
    if authenticated and not device_trusted and not device_known:
        score += 15
        flags.append("未知设备上的登录会话")
    if is_new_source:
        score += 10
        flags.append("新来源（最近 24 小时内首次出现）")
    if materials_downloaded >= 10:
        score += 10
        flags.append(f"下载材料 {materials_downloaded} 个")
    if device_trusted:
        score -= 30
        flags.append("已标记为可信设备")
    if source_trusted:
        score -= 40
        flags.append("已标记为可信来源（自家网络）")
    if authenticated and usernames:
        score -= 5

    return max(0, min(100, score)), flags
