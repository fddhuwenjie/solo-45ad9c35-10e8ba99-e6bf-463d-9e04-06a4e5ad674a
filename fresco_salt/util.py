"""通用工具：确定性哈希、JSON 规范化、数值舍入、时间解析。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def canonical_json(obj: Any) -> str:
    """sort_keys + 无空白的确定性 JSON（保证哈希与封版复算可复现）。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_default
    )


def content_hash(obj: Any, prefix: str = "") -> str:
    """对内容生成短指纹，形如 sha256:8f3a...（可加前缀分类）。"""
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest}" if prefix else digest


def r4(x: Any) -> Any:
    """输出用：保留 4 位有效数字（内部计算不提前舍入）。"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return x
    if x == 0:
        return 0.0
    return round(x, 4)


def parse_dt(value: str) -> datetime:
    """解析 ISO8601；末尾 Z 视为 UTC，统一转为 naive（内部仅做间隔运算）。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def hours_between(a: datetime, b: datetime) -> float:
    return round((a - b).total_seconds() / 3600.0, 3)
