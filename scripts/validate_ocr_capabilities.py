#!/usr/bin/env python3
"""校验宿主 OCR Skill 的 ``ocr.capabilities.v1`` 声明。

该命令只记录能力和安装建议，不安装 Provider、不下载模型、不修改系统环境。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import write_json
from tc.models import OCRCapabilities, OCRCapabilitiesFile
from tc.ocr_contract import capability_file

app = typer.Typer(help="校验 ocr.capabilities.v1；不安装或运行 OCR")


@app.command()
def run(
    input_path: Path = typer.Argument(..., exists=True, dir_okay=False, help="能力声明 JSON"),
    output_path: Path = typer.Option(None, "--output", "-o", help="可选的校验结果 JSON"),
) -> None:
    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
        if "capabilities" in raw:
            raw = raw["capabilities"]
        result = capability_file(OCRCapabilities(**raw))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        typer.secho(f"[拒绝] 能力声明无效：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if output_path:
        write_json(output_path, result.model_dump(mode="json"))
    if result.accepted:
        typer.secho(f"[OK] OCR Provider 可被 Core 接受：{result.capabilities.provider_id}", fg=typer.colors.GREEN)
    else:
        typer.secho("[不可用] " + "；".join(result.rejection_reasons), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

