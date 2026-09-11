#!/usr/bin/env python3
"""导入并校验宿主 Agent 的 ``ocr-result.v1`` 结果。

导入是事务性的：任一结果与任务不匹配时，整批拒绝，不生成字段；错误写入
``output/interim/ocr-import-errors.json``，且不暴露 Provider 返回的原始文本。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.canon import load_json, write_json
from tc.models import InventoryFile, OCRJobFile, OCRResult, OCRResultFile
from tc.ocr_contract import cache_path, validate_result_against_job
from tc.projio import ProjectError, ensure_output_dirs, load_project_config

app = typer.Typer(help="导入并严格校验宿主 OCR 结果（ocr-result.v1）")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    input_path: Path = typer.Option(None, "--input", "-i", help="Provider 结果 JSON；默认读取项目根 ocr-results.json"),
) -> None:
    try:
        cfg = load_project_config(project_dir)
        interim = project_dir / "output/interim"
        inventory = InventoryFile(**load_json(interim / "inventory.json"))
        jobs_file = OCRJobFile(**load_json(interim / "ocr-jobs.json"))
    except (ProjectError, FileNotFoundError, ValueError) as exc:
        typer.secho(f"[错误] 缺少或无法读取 OCR 任务：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    source = input_path or (project_dir / "ocr-results.json")
    if not source.exists():
        typer.secho(f"[错误] 未找到 OCR 结果：{source}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        results_raw = raw.get("results", []) if isinstance(raw, dict) else raw
        if isinstance(results_raw, dict):
            results_raw = [results_raw]
        results = [OCRResult(**item) for item in results_raw]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _write_errors(project_dir, inventory, ["OCR 结果 JSON 无法解析或不符合 ocr-result.v1"])
        typer.secho(f"[错误] OCR 结果无效：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    jobs = {job.job_id: job for job in jobs_file.jobs}
    errors: list[str] = []
    for result in results:
        job = jobs.get(result.job_id)
        if job is None:
            errors.append(f"{result.job_id}: 无对应 OCR 任务")
            continue
        errors.extend(f"{result.job_id}: {error}" for error in validate_result_against_job(result, job))
        if job.privacy_requirement == "local_only" and result.provider.processing_location != "local":
            errors.append(f"{result.job_id}: 任务要求 local_only，但结果声明为 cloud")

    if errors:
        _write_errors(project_dir, inventory, errors)
        typer.secho(f"[拒绝] OCR 结果未导入：{len(errors)} 个契约错误；已写入 ocr-import-errors.json", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    _, interim = ensure_output_dirs(project_dir)
    existing: dict[str, OCRResult] = {}
    current = interim / "ocr-results.json"
    if current.exists():
        try:
            existing = {r.job_id: r for r in OCRResultFile(**load_json(current)).results}
        except (ValueError, TypeError):
            existing = {}
    existing.update({result.job_id: result for result in results})
    bundle = OCRResultFile(run=inventory.run, results=sorted(existing.values(), key=lambda r: r.job_id))
    write_json(current, bundle.model_dump(mode="json"))

    cache_root = project_dir / "output/cache"
    for result in results:
        job = jobs[result.job_id]
        write_json(cache_path(cache_root, job, result), result.model_dump(mode="json"))

    # 导入成功后重建字段候选；该函数只读本地输入和已验证结果。
    from extract_content import build_content

    build_content(project_dir)
    typer.secho(f"[OK] OCR 结果已导入：{len(results)} 条；字段候选已重建", fg=typer.colors.GREEN)


def _write_errors(project_dir: Path, inventory: InventoryFile, errors: list[str]) -> None:
    _, interim = ensure_output_dirs(project_dir)
    # 只写契约错误，不回显 OCR 文本或 Provider detail。
    write_json(interim / "ocr-import-errors.json", {
        "run": inventory.run.model_dump(mode="json"),
        "status": "ocr_result_invalid",
        "errors": errors[:100],
    })


if __name__ == "__main__":
    app()

