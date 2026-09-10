#!/usr/bin/env python3
"""项目验证器（P0）。

- 校验 project.yaml 必填字段与目录结构；
- 校验各阶段中间产物是否符合 schemas/*.schema.json；
- 发现缺失输入或损坏产物时，返回非 0 并列出具体路径/字段。

用法：
  python validate_project.py <project_dir> [--stage pre|interim|final]
  pre     仅校验输入结构（inventory 之前）
  interim 校验全部中间产物（默认：存在多少校验多少）
  final   要求全部产物存在且通过校验
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import jsonschema
import typer
import yaml

from tc.projio import REQUIRED_PROJECT_FIELDS, ProjectError

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
INTERIM_FILES = {
    "inventory.json": "inventory.schema.json",
    "content.json": "content.schema.json",
    "entities.json": "supplier.schema.json",
    "matches.json": "matches.schema.json",
    "metadata.json": "metadata.schema.json",
    "external.json": "external-query.schema.json",
    "findings.json": "finding.schema.json",
    "evidence-content.json": "evidence.schema.json",
    "evidence-metadata.json": "evidence.schema.json",
    "evidence-external.json": "evidence.schema.json",
}

app = typer.Typer(help="校验项目输入与产物结构")


class Problem:
    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.path}: {self.detail}"


def load_json_file(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema_file(data, schema_path: Path, problems: list[Problem], label: str) -> None:
    try:
        schema = load_json_file(schema_path)
    except FileNotFoundError:
        problems.append(Problem(label, f"缺少 Schema 文件 {schema_path.name}"))
        return
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(x) for x in err.absolute_path) or "<root>"
        problems.append(Problem(label, f"[{loc}] {err.message[:300]}"))


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    stage: str = typer.Option("interim", help="pre / interim / final"),
) -> None:
    problems: list[Problem] = []

    # project.yaml
    cfg_path = project_dir / "project.yaml"
    if not cfg_path.exists():
        problems.append(Problem("project.yaml", "文件不存在"))
    else:
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                problems.append(Problem("project.yaml", "内容必须是键值映射"))
            else:
                for key in REQUIRED_PROJECT_FIELDS:
                    if data.get(key) in (None, ""):
                        problems.append(Problem("project.yaml", f"缺少必填字段 {key}"))
        except yaml.YAMLError as exc:
            problems.append(Problem("project.yaml", f"YAML 解析失败：{exc}"))

    # 目录结构
    if not (project_dir / "bids").is_dir():
        problems.append(Problem("bids/", "缺少标书目录"))
    else:
        subs = [p for p in (project_dir / "bids").iterdir() if p.is_dir() and not p.name.startswith(".")]
        if not subs:
            problems.append(Problem("bids/", "未发现供应商子目录"))
    if not (project_dir / "procurement").is_dir():
        problems.append(Problem("procurement/", "缺少采购文件目录（如确实没有，请人工确认）"))

    if stage != "pre":
        for name, schema_name in INTERIM_FILES.items():
            path = project_dir / "output/interim" / name
            if not path.exists():
                if stage == "final":
                    problems.append(Problem(f"output/interim/{name}", "缺少产物（final 模式要求全部存在）"))
                continue
            validate_schema_file(load_json_file(path), SCHEMA_DIR / schema_name, problems, f"output/interim/{name}")

    if problems:
        typer.secho(f"[FAIL] 项目校验未通过（{len(problems)} 个问题）：", fg=typer.colors.RED, err=True)
        for p in problems:
            typer.secho(f"  - {p}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"[OK] 项目校验通过（stage={stage}）：{project_dir}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
