"""带模型心跳的外部命令执行器（用于首次安装等长任务）。"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from .progress import Heartbeat, emit_progress


def run_command_with_progress(
    command: Sequence[str],
    *,
    phase: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    interval_seconds: float = 10.0,
) -> int:
    """执行命令并每隔固定时间发出心跳，不回显命令行和原始输出。"""
    if not command:
        raise ValueError("command 不能为空")
    started = time.monotonic()
    heartbeat = Heartbeat(phase=phase, interval_seconds=interval_seconds)
    emit_progress(
        phase,
        "started",
        event="started",
        message="已开始执行长任务",
        command_name=Path(str(command[0])).name,
    )
    output_lines = 0
    lines: queue.Queue[str | None] = queue.Queue()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError:
        emit_progress(
            phase,
            "failed",
            event="failed",
            message="长任务无法启动",
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
        raise

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_output, name="tc-progress-reader", daemon=True)
    reader.start()
    while True:
        try:
            line = lines.get(timeout=0.5)
            if line is not None:
                output_lines += 1
        except queue.Empty:
            pass
        if heartbeat.due():
            heartbeat.send(message="安装任务仍在执行", output_lines=output_lines)
        if process.poll() is not None:
            break
    reader.join(timeout=1.0)
    return_code = int(process.returncode or 0)
    elapsed = round(time.monotonic() - started, 1)
    emit_progress(
        phase,
        "completed" if return_code == 0 else "failed",
        event="completed" if return_code == 0 else "failed",
        message="长任务已完成" if return_code == 0 else "长任务失败",
        elapsed_seconds=elapsed,
        output_lines=output_lines,
        exit_code=return_code,
    )
    return return_code
