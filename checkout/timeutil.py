"""时间工具：统一使用带时区的 datetime，边界以 ISO-8601 字符串持久化。"""

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(value: str | datetime) -> datetime:
    """解析 ISO-8601；无时区输入按 UTC 处理（fixture 一律显式带 +08:00）。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def between(start: datetime, at: datetime, end: datetime | None) -> bool:
    """左闭右开 [start, end)。"""
    if at < start:
        return False
    return end is None or at < end
