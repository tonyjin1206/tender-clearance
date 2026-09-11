#!/usr/bin/env python3
"""生成清标运行预检计划。

预检只读项目目录和当前 Python 环境，不安装依赖、不联网、不索取凭据。
宿主 Agent 应先展示本计划并一次性取得用户确认，再执行安装或正式流水线。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

import typer

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tc.projio import ProjectError, load_project_config

app = typer.Typer(help="生成清标安装/运行前预检计划（只读）")

CORE_MODULES = {
    "typer": "typer",
    "jinja2": "jinja2",
    "pydantic": "pydantic",
    "yaml": "PyYAML",
    "jsonschema": "jsonschema",
    "fitz": "pymupdf",
    "PIL": "Pillow",
    "reportlab": "reportlab",
}
OPTIONAL_MODULES = {
    "review": {"docx": "python-docx"},
    "workpaper": {"docx": "python-docx", "openpyxl": "openpyxl"},
    "live": {"requests": "requests"},
    "srm": {"playwright": "playwright"},
}


def _classify_filename(name: str) -> str:
    value = name.lower()
    if value.endswith(('.xlsx', '.xls')) or '一览' in name or '报价' in name:
        return 'bid_schedule'
    if '技术' in name or 'technical' in value or 'tech' in value:
        return 'technical'
    if '封面' in name or 'cover' in value:
        return 'cover'
    if any(token in name for token in ('商务', '授权', '营业执照', '声明', '扫描')) or any(
        token in value for token in ('business',)
    ):
        return 'business'
    return 'unknown'


def build_plan(project_dir: Path, profile: str) -> dict:
    if profile not in {"report", "review", "workpaper"}:
        raise ProjectError(f"不支持的输出档位：{profile}")
    cfg = load_project_config(project_dir)
    files = [
        p for root in ("bids", "procurement", "external-evidence")
        for p in (project_dir / root).rglob("*")
        if p.is_file() and not p.name.startswith('.') and '__pycache__' not in p.parts
    ]
    bid_files = [p for p in files if "bids" in p.relative_to(project_dir).parts]
    classifications = Counter(_classify_filename(p.name) for p in bid_files)

    required = dict(CORE_MODULES)
    if profile in OPTIONAL_MODULES:
        required.update(OPTIONAL_MODULES[profile])
    if cfg.external_query_mode == "live":
        required.update(OPTIONAL_MODULES["live"])
        if "srm" in cfg.external_query_sources:
            required.update(OPTIONAL_MODULES["srm"])
    missing = [package for module, package in required.items()
               if importlib.util.find_spec(module) is None]
    live_srm = cfg.external_query_mode == "live" and "srm" in cfg.external_query_sources
    credentials_ready = bool(os.environ.get("SRM_USER", "").strip() and os.environ.get("SRM_PASSWORD", ""))

    questions = [
        {
            "id": "execution_scope",
            "prompt": "确认项目目录、输出档位以及是否按文件名跳过技术标正文扫描",
            "default": {"profile": profile, "skip_technical_body": True},
        },
        {
            "id": "environment_install",
            "prompt": "如有缺失，是否允许一次性安装下列依赖",
            "required": bool(missing),
            "packages": missing,
        },
        {
            "id": "ocr_provider",
            "prompt": "扫描页是否已有宿主 OCR Provider；没有则保留 ocr_unavailable 并进入人工复核",
            "default": "use_existing_provider_or_manual_review",
        },
    ]
    if live_srm:
        questions.append({
            "id": "srm_credentials",
            "prompt": "是否授权本次 SRM 登录查询，并在流水线启动前提供凭据",
            "required": True,
            "ready_from_environment": credentials_ready,
        })

    blocking = []
    if missing:
        blocking.append("environment_install_confirmation")
    if live_srm and not credentials_ready:
        blocking.append("srm_credentials_before_run")
    return {
        "status": "needs_confirmation" if questions else "ready",
        "project": {
            "path": str(project_dir.resolve()),
            "project_id": cfg.project_id,
            "project_name": cfg.project_name,
            "profile": profile,
            "external_query_mode": cfg.external_query_mode,
            "external_query_sources": list(cfg.external_query_sources),
        },
        "input_summary": {
            "file_count": len(files),
            "bid_file_count": len(bid_files),
            "bid_filename_classification": dict(sorted(classifications.items())),
            "technical_body_scan": "skip_by_filename",
        },
        "environment": {
            "python": sys.version.split()[0],
            "required_packages": required,
            "missing_packages": missing,
            "srm_credentials_present": credentials_ready,
        },
        "questions": questions,
        "blocking": blocking,
        "after_confirmation": [
            "install_all_confirmed_packages_once",
            "run_pipeline_without_mid_run_questions",
            "never_install_or_network_during_report_render",
        ],
    }


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    profile: str = typer.Option("report", help="输出档位：report / review / workpaper"),
) -> None:
    try:
        plan = build_plan(project_dir, profile)
    except ProjectError as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
