"""工具函数（日期/时间/配置解析）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def load_dotenv(path: Path) -> dict[str, str]:
    """解析 dotenv 文件（支持 KEY=VALUE / 注释 / 空行），返回键值字典。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def beijing_naive_now() -> datetime:
    """北京时间的裸 datetime（与库中无时区时间戳对齐用）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def format_short_time(value) -> str:
    """datetime 或 ISO 字符串 → 「MM-DD HH:MM」。"""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return (value or "")[:16].replace("T", " ")
    return f"{value:%m-%d %H:%M}"
