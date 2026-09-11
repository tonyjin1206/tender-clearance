#!/usr/bin/env python3
"""一次性文档资料解析入口。

先生成文本/字段候选与 OCR 任务，再生成文件属性；不执行 OCR、不联网。
"""

from __future__ import annotations

from pathlib import Path

import typer

from extract_content import build_content
from extract_metadata import build_metadata

app = typer.Typer(help="一次性解析文档内容、属性并生成宿主 OCR 任务")


@app.command()
def run(project_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    # 同一 Python 进程内完成两个阶段；底层解析快照写入 document cache，
    # 属性阶段直接复用，避免子进程启动和再次打开/解码原始文档。
    build_content(project_dir)
    build_metadata(project_dir)


if __name__ == "__main__":
    app()
