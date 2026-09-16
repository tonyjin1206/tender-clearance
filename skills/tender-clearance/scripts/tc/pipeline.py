"""按阶段顺序运行完整流水线（供测试与 SKILL.md 的命令序列复用）。"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .canon import write_json
from .projio import ProjectError, ensure_output_dirs, load_project_config

SCRIPTS = Path(__file__).resolve().parent.parent  # scripts/ 目录（tc 的上一级）

STAGES = [
    ("inventory.py", []),
    ("extract_documents.py", []),
    ("resolve_supplier_groups.py", []),
    ("normalize_and_match.py", []),
    ("analyze_bid_data.py", []),
    ("import_external_evidence.py", []),
    ("assess_risk.py", []),
    ("render_report.py", ["--profile", "report"]),
]

def _stage_name(value: str) -> str:
    """Accept either a script name or its stable stage name in CLI/API calls."""
    value = value.strip()
    if value.endswith(".py"):
        return value
    return f"{value}.py"


def _read_prior_timings(project_dir: Path) -> dict[str, str]:
    """Return the last known status per stage; malformed telemetry never blocks a run."""
    try:
        data = __import__("json").loads(
            (project_dir / "output/interim/performance.json").read_text(encoding="utf-8")
        )
        return {str(item["stage"]): str(item["status"]) for item in data.get("stages", [])}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _select_stage_range(
    stages: list[tuple[str, list[str]]],
    *,
    from_stage: str | None = None,
    to_stage: str | None = None,
    resume: bool = False,
    prior_status: dict[str, str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Select a contiguous stage range without making the agent reconstruct dependencies."""
    if from_stage and resume:
        raise ProjectError("--resume 与 --from-stage 不能同时使用")
    selected = list(stages)
    if from_stage:
        start = _stage_name(from_stage)
        names = [name for name, _ in selected]
        if start not in names:
            raise ProjectError(f"未知起始阶段：{from_stage}；可选：{', '.join(names)}")
        selected = selected[names.index(start):]
    if to_stage:
        end = _stage_name(to_stage)
        names = [name for name, _ in selected]
        if end not in names:
            raise ProjectError(f"未知结束阶段：{to_stage}；可选：{', '.join(names)}")
        selected = selected[:names.index(end) + 1]
    if resume:
        prior_status = prior_status or {}
        first_pending = next(
            (i for i, (name, _) in enumerate(selected)
             if prior_status.get(Path(name).stem) != "succeeded"),
            len(selected),
        )
        selected = selected[first_pending:]
    return selected


def _requires_supplier_grouping(project_dir: Path) -> bool:
    """只有平铺上传区才需要新归组阶段；保留旧版人工整理项目兼容性。"""
    bids = project_dir / "bids"
    if not bids.exists():
        return False
    if any((bids / marker).is_dir() for marker in ("inbox", "incoming", "uploads")):
        return True
    # bids/ 根下的平铺文件也必须先归组；已有 bids/<supplier>/ 目录的旧项目
    # 继续走兼容路径，但不会影响新上传流程。
    return any(p.is_file() for p in bids.iterdir())


@dataclass(frozen=True)
class StageTiming:
    """一次阶段执行的可持久化耗时记录，不含任何命令行参数或敏感值。"""

    stage: str
    elapsed_seconds: float
    status: str
    exit_code: int | None


class StageExecutionError(RuntimeError):
    """阶段失败时连同耗时回传给编排器，保证失败也有可观测记录。"""

    def __init__(self, message: str, timing: StageTiming) -> None:
        super().__init__(message)
        self.timing = timing


def _write_performance_report(
    project_dir: Path,
    started: float,
    timings: list[StageTiming],
    status: str,
    failure_type: str | None = None,
    selected_stages: list[str] | None = None,
) -> None:
    """追加/更新阶段耗时，避免失败续跑覆盖此前已完成阶段。"""
    _out, interim = ensure_output_dirs(project_dir)
    stage_records: dict[str, dict] = {}
    stage_order: list[str] = []
    try:
        prior = json.loads((interim / "performance.json").read_text(encoding="utf-8"))
        for item in prior.get("stages", []):
            if not isinstance(item, dict) or not item.get("stage"):
                continue
            stage = str(item["stage"])
            if stage not in stage_records:
                stage_order.append(stage)
            stage_records[stage] = item
    except (OSError, ValueError, TypeError):
        pass
    for item in timings:
        if item.stage not in stage_records:
            stage_order.append(item.stage)
        stage_records[item.stage] = asdict(item)
    write_json(interim / "performance.json", {
        "schema_version": "tender-clearance.performance.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "total_seconds": round(time.perf_counter() - started, 3),
        "status": status,
        "failure_type": failure_type,
        "selected_stages": selected_stages or [],
        "stages": [stage_records[stage] for stage in stage_order],
    })


def run_stage(
    script: str,
    args: list[str],
    project_dir: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    stream_output: bool = False,
) -> StageTiming:
    started = time.perf_counter()
    cmd = [sys.executable, str(SCRIPTS / script), *args, str(project_dir)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=not stream_output,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        print(f"[耗时] {script} {elapsed:.2f}s（超时，预算 {timeout_seconds}s）")
        raise StageExecutionError(
            f"{script} 超过 {timeout_seconds}s 执行预算，已停止；"
            "请查看 output/interim/performance.json 与 query-timings.json 后决定是否重试",
            StageTiming(Path(script).stem, round(elapsed, 3), "timed_out", None),
        ) from exc
    output = "" if stream_output else (proc.stdout + proc.stderr).strip()
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        print(f"[耗时] {script} {elapsed:.2f}s（失败，exit {proc.returncode}）")
        raise StageExecutionError(
            f"{script} 失败（exit {proc.returncode}）：\n{output[-2000:]}",
            StageTiming(Path(script).stem, round(elapsed, 3), "failed", proc.returncode),
        )
    print(f"[耗时] {script} {elapsed:.2f}s")
    if output:
        print(f"[{script}] {output.splitlines()[-1]}")
    return StageTiming(
        stage=Path(script).stem,
        elapsed_seconds=round(elapsed, 3),
        status="succeeded",
        exit_code=proc.returncode,
    )


def run_all(
    project_dir: Path,
    skip_report: bool = False,
    require_srm: bool = True,
    srm_credentials: tuple[str, str] | None = None,
    profile: str = "report",
    live_query_timeout_seconds: int | None = 300,
    from_stage: str | None = None,
    to_stage: str | None = None,
    resume: bool = False,
    refresh_queries: bool = False,
) -> None:
    """运行完整流程；正式报告默认要求本次运行完成 SRM 登录查询。

    ``require_srm=False`` 仅供离线单元测试夹具使用，不是生产运行模式。
    """
    cfg = load_project_config(project_dir)
    if require_srm and (
        cfg.external_query_mode != "live" or "srm" not in cfg.external_query_sources
    ):
        raise ProjectError(
            "正式报告必须先登录并查询富奥 SRM：请将 project.yaml 的 "
            "external_query_mode 设为 live，并在 external_query_sources 中启用 srm"
        )
    if profile not in {"report", "review", "workpaper"}:
        raise ProjectError(f"不支持的输出档位：{profile}")
    stage_defs = STAGES if _requires_supplier_grouping(project_dir) else [
        stage for stage in STAGES if stage[0] != "resolve_supplier_groups.py"
    ]
    stages = [
        (name, ["--profile", profile] if name == "render_report.py" else list(args))
        for name, args in (stage_defs if not skip_report else [s for s in stage_defs if s[0] != "render_report.py"])
    ]
    if require_srm:
        insert_at = next(i for i, (name, _args) in enumerate(stages) if name == "assess_risk.py")
        query_args = []
        if live_query_timeout_seconds is not None:
            query_args = ["--max-total-seconds", str(live_query_timeout_seconds)]
        stages.insert(insert_at, ("query_sources.py", query_args))

    stages = _select_stage_range(
        stages,
        from_stage=from_stage,
        to_stage=to_stage,
        resume=resume,
        prior_status=_read_prior_timings(project_dir),
    )
    if refresh_queries:
        stages = [
            (name, [*args, "--refresh"] if name == "query_sources.py" else args)
            for name, args in stages
        ]
    if not stages:
        print("[跳过] 没有待执行阶段；如需重新查询请使用 --refresh 或指定 --from-stage")
        return

    srm_env: dict[str, str] | None = None
    if require_srm and any(script == "query_sources.py" for script, _ in stages):
        # 只有本次选中的阶段实际访问 SRM 时才要求凭据；局部规则/渲染续跑不应被无关门禁阻断。
        username, password = srm_credentials or (
            os.environ.get("SRM_USER", "").strip(), os.environ.get("SRM_PASSWORD", "")
        )
        if not username or not password:
            raise ProjectError(
                "SRM 凭据必须在查询阶段启动前提供；请先完成交互确认，再通过参数或"
                " SRM_USER/SRM_PASSWORD 注入，流水线不会中途询问"
            )
        # 仅把凭据传给需要它的查询子进程；本地解析、规则和渲染阶段不继承。
        srm_env = os.environ.copy()
        srm_env.update({
            "SRM_USER": username,
            "SRM_PASSWORD": password,
            "SRM_BROWSER_DRIVER": os.environ.get("SRM_BROWSER_DRIVER", "playwright"),
        })

    started = time.perf_counter()
    timings: list[StageTiming] = []
    failed: Exception | None = None
    try:
        for script, args in stages:
            if script == "render_report.py" and not require_srm:
                args = [*args, "--skip-srm-gate"]
            timeout = live_query_timeout_seconds if script == "query_sources.py" else None
            timings.append(run_stage(
                script,
                args,
                project_dir,
                env=srm_env if script == "query_sources.py" else None,
                timeout_seconds=timeout,
                stream_output=True,
            ))
    except StageExecutionError as exc:
        timings.append(exc.timing)
        failed = exc
        raise
    except Exception as exc:  # noqa: BLE001 - 原异常在 finally 后继续抛出
        failed = exc
        raise
    finally:
        _write_performance_report(
            project_dir,
            started,
            timings,
            status="failed" if failed else "succeeded",
            failure_type=type(failed).__name__ if failed else None,
            selected_stages=[Path(name).stem for name, _ in stages],
        )
