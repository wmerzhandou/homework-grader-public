"""下载离线 IP 归属库（GeoLite2-City + GeoLite2-ASN）到 DATA_DIR/geoip/。

用法：
    python -m scripts.fetch_geoip

数据源是社区镜像（P3TERX/GeoLite.mmdb）。库建议季度更新一次：重新跑这个脚本即可，
不会影响已有记录（历史 IP 的归属已缓存在 ipgeo 表里，保持当时的样子）。
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

MIRROR = "https://github.com/P3TERX/GeoLite.mmdb/raw/download"
FILES = ("GeoLite2-City.mmdb", "GeoLite2-ASN.mmdb")


def fetch(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  下载 {url}")
    with urllib.request.urlopen(url, timeout=600) as response, tmp.open("wb") as fh:
        while chunk := response.read(1024 * 1024):
            fh.write(chunk)
    tmp.replace(dest)
    print(f"  → {dest} ({dest.stat().st_size // 1024} KB)")


def main() -> None:
    settings = get_settings()
    target_dir = settings.data_dir / "geoip"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        fetch(f"{MIRROR}/{name}", target_dir / name)
    print(f"完成。如需更新库，重新执行本脚本；查询失败时会自动降级为『未知』。")


if __name__ == "__main__":
    main()
