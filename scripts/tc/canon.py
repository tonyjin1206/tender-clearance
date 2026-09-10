"""规范化 JSON、稳定 ID 与哈希。

可重复运行（方案 2.1 / T12）：所有证据、匹配、发现的 ID 都由内容派生，
不包含运行时间与运行标识；同一输入与规则版本连续运行两次，ID 逐字段一致。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    """键排序、去空白、确保中文不被转义，保证跨运行字节级一致。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def stable_id(prefix: str, obj: Any, length: int = 12) -> str:
    return f"{prefix}-{content_hash(obj)[:length]}"


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n"
    path.write_text(text, encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
