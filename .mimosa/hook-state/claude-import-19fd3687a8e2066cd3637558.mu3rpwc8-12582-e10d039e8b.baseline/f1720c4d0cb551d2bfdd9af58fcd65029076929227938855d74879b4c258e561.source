"""按阶段顺序运行完整流水线（供测试与 SKILL.md 的命令序列复用）。"""

from __future__ import annotations

import os
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
) -> None:
    """即使失败也落盘已完成阶段的耗时，便于定位长耗时而不记录阶段输出。"""
    _out, interim = ensure_output_dirs(project_dir)
    write_json(interim / "performance.json", {
        "schema_version": "tender-clearance.performance.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "total_seconds": round(time.perf_counter() - started, 3),
        "status": status,
        "failure_type": failure_type,
        "stages": [asdict(item) for item in timings],
    })


def run_stage(
    script: str,
    args: list[str],
    project_dir: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> StageTiming:
    started = time.perf_counter()
    cmd = [sys.executable, str(SCRIPTS / script), *args, str(project_dir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        print(f"[耗时] {script} {elapsed:.2f}s（超时，预算 {timeout_seconds}s）")
        raise StageExecutionError(
            f"{script} 超过 {timeout_seconds}s 执行预算，已停止；"
            "请查看 output/interim/performance.json 与 query-timings.json 后决定是否重试",
            StageTiming(Path(script).stem, round(elapsed, 3), "timed_out", None),
        ) from exc
    output = (proc.stdout + proc.stderr).strip()
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
    srm_env: dict[str, str] | None = None
    if require_srm:
        # 凭据必须在任何阶段启动前准备完成；此处只把它短暂传给子进程，
        # 不写入项目文件、不在阶段中途调用 input/getpass。
        username, password = srm_credentials or (
            os.environ.get("SRM_USER", "").strip(), os.environ.get("SRM_PASSWORD", "")
        )
        if not username or not password:
            raise ProjectError(
                "SRM 凭据必须在流水线启动前提供；请先完成交互确认，再通过参数或"
                " SRM_USER/SRM_PASSWORD 注入，流水线不会中途询问"
            )
        # 仅把凭据传给需要它的查询子进程；本地解析、规则和渲染阶段不继承。
        srm_env = os.environ.copy()
        # 正式 CLI 默认走真实浏览器会话；仍允许调用方显式选择其它已配置驱动。
        # 该变量只传给查询子进程，不进入项目配置或任何输出文件。
        srm_env.update({
            "SRM_USER": username,
            "SRM_PASSWORD": password,
            "SRM_BROWSER_DRIVER": os.environ.get("SRM_BROWSER_DRIVER", "playwright"),
        })

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
        )
