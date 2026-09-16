"""面向编排模型的可折叠过程事件。

事件写到 stderr，不进入报告、证据、日志正文或用户业务答复。宿主 Agent
可以识别 ``TC_PROGRESS_V1`` 前缀，将事件作为模型可见的折叠过程信息展示。
事件只包含阶段、页号、耗时、计数和状态，不包含凭据、OCR 原文或命令行参数。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any


PREFIX = "TC_PROGRESS_V1 "


def emit_progress(
    phase: str,
    status: str,
    *,
    event: str,
    message: str,
    **details: Any,
) -> None:
    """发出一条模型过程事件；失败时不影响业务主流程。"""
    payload: dict[str, Any] = {
        "schema_version": "tender-clearance.progress.v1",
        "visibility": "model_only",
        "collapsible": True,
        "event": event,
        "phase": phase,
        "status": status,
        "message": message,
        "at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    try:
        sys.stderr.write(PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stderr.flush()
    except (OSError, TypeError, ValueError):
        # 过程展示不可用时不应阻断 OCR 或安装本身。
        return


class Heartbeat:
    """按固定间隔发送安装/长任务心跳。"""

    def __init__(self, *, phase: str, interval_seconds: float = 10.0) -> None:
        self.phase = phase
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.started = time.monotonic()
        self.next_at = self.started + self.interval_seconds

    def due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now >= self.next_at

    def send(self, *, message: str, **details: Any) -> None:
        now = time.monotonic()
        elapsed = round(now - self.started, 1)
        emit_progress(
            self.phase,
            "running",
            event="heartbeat",
            message=message,
            elapsed_seconds=elapsed,
            interval_seconds=self.interval_seconds,
            **details,
        )
        while self.next_at <= now:
            self.next_at += self.interval_seconds
