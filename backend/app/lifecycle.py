"""生命周期公共设施：时间比较、字段组解析与跨模块状态锁。

去重引擎停用和快照生成/确认会互相读取对方的状态，必须放在同一把锁里，
否则并发请求会读到半更新的批次或统计。这里只提供基础设施，业务规则仍然
留在各自的 service 里。
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime, timezone
from typing import Any

# 引擎停用与快照生成/确认共用一把可重入锁：线程内可以嵌套调用，
# 跨请求之间串行执行状态准入和计数回写。
LIFECYCLE_LOCK = threading.RLock()

_GROUP_SEPARATOR = re.compile(r"[,，、;；/|]+")


def now_iso() -> str:
    """统一使用带时区的 ISO 时间；精确到微秒，停用/启用先后比较不会被同秒截断。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: Any) -> datetime | None:
    """把日期或 ISO 时间解析成带时区的 datetime；解析不了就返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def request_in_disabled_window(request_time: Any, disabled_at: Any, enabled_at: Any) -> bool:
    """请求是否在引擎停用之后才到达：停用后、再次启用前发出的请求都算晚到。

    引擎重新启用后，启用时间之后的新请求属于新的批次窗口，不受上一次停用影响。
    """
    request_at = parse_time(request_time)
    disabled = parse_time(disabled_at)
    if request_at is None or disabled is None:
        return False
    if request_at < disabled:
        return False
    enabled = parse_time(enabled_at)
    # 停用时间晚于启用时间说明引擎目前仍是停用状态，停用之后的请求都算晚到。
    if enabled is not None and disabled <= enabled:
        return request_at < enabled
    return True


def split_groups(value: Any) -> list[str]:
    """把去重字段组统一成列表，兼容逗号、顿号、分号分隔的旧数据。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in _GROUP_SEPARATOR.split(str(value)) if item.strip()]


def same_groups(left: Any, right: Any) -> bool:
    return sorted(split_groups(left)) == sorted(split_groups(right))
