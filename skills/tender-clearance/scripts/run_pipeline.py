#!/usr/bin/env python3
"""正式流水线入口：输出阶段耗时并限制实时外部查询的最长等待。"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tc.pipeline import StageExecutionError, run_all
from tc.projio import ProjectError

app = typer.Typer(help="运行完整清标流水线并写入 performance.json")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="项目目录"),
    profile: str = typer.Option("report", help="输出档位：report / review / workpaper"),
    live_query_timeout_seconds: int = typer.Option(
        300,
        min=1,
        help="实时外部查询总执行上限（秒）；超时停止，不生成误导性的正式报告。",
    ),
    offline_draft: bool = typer.Option(
        False,
        "--offline-draft",
        help="仅生成离线草稿，跳过 SRM 门禁；不得作为正式评标报告。",
    ),
    from_stage: str | None = typer.Option(
        None,
        "--from-stage",
        help="从指定阶段继续（可写脚本名或阶段名），例如 normalize_and_match。",
    ),
    to_stage: str | None = typer.Option(
        None,
        "--to-stage",
        help="执行到指定阶段后停止；用于先生成中间结果或排查单段流程。",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="按 performance.json 从上次未成功阶段继续，不重复已成功阶段。",
    ),
    refresh_queries: bool = typer.Option(
        False,
        "--refresh-queries",
        help="强制刷新本次启用的全部 live 查询；默认复用同一主体已成功结果。",
    ),
) -> None:
    """凭据必须已在当前进程环境中提供；本入口绝不交互索取密码。"""
    try:
        run_all(
            project_dir,
            require_srm=not offline_draft,
            profile=profile,
            live_query_timeout_seconds=live_query_timeout_seconds,
            from_stage=from_stage,
            to_stage=to_stage,
            resume=resume,
            refresh_queries=refresh_queries,
        )
    except (ProjectError, StageExecutionError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
