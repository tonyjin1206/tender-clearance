#!/usr/bin/env python3
"""生成 OCR 字段级过程底稿和报告安全输入。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json
from tc.models import ContentFile, EntitiesFile, FindingsFile, InventoryFile
from tc.process_artifacts import build_process_artifacts

app = typer.Typer(help="生成不依赖对话上下文的过程底稿与报告输入")


@app.command()
def run(project_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    interim = project_dir / "output" / "interim"
    inventory = InventoryFile(**load_json(interim / "inventory.json"))
    content = ContentFile(**load_json(interim / "content.json"))
    entities_path = interim / "entities.json"
    findings_path = interim / "findings.json"
    entities = EntitiesFile(**load_json(entities_path)) if entities_path.exists() else None
    findings = FindingsFile(**load_json(findings_path)) if findings_path.exists() else None
    build_process_artifacts(
        project_dir,
        inventory=inventory,
        content=content,
        entities=entities,
        findings=findings,
    )
    typer.echo("[OK] 已生成 process-workpaper.json 与 report-input.json")


if __name__ == "__main__":
    app()
