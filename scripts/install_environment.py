#!/usr/bin/env python3
"""首次安装命令包装器：输出模型可见的 10 秒进度事件。

用法：
    python scripts/install_environment.py -- uv pip install -p .venv/bin/python -r requirements-core.txt

安装前的授权和依赖清单确认仍由宿主 Agent 负责；本脚本只负责执行已确认的命令和
报告进度，不把命令行参数、凭据或包安装日志写入过程事件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tc.command_progress import run_command_with_progress


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="以 -- 开始的已确认安装命令")
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("必须提供安装命令，例如：-- uv pip install ...")
    return run_command_with_progress(
        command,
        phase="environment_install",
        interval_seconds=args.interval_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
