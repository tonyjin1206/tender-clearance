"""按阶段顺序运行完整流水线（供测试与 SKILL.md 的命令序列复用）。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .projio import ProjectError, load_project_config

SCRIPTS = Path(__file__).resolve().parent.parent  # scripts/ 目录（tc 的上一级）

STAGES = [
    ("inventory.py", []),
    ("extract_documents.py", []),
    ("normalize_and_match.py", []),
    ("import_external_evidence.py", []),
    ("assess_risk.py", []),
    ("render_report.py", ["--profile", "report"]),
]


def run_stage(
    script: str,
    args: list[str],
    project_dir: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    started = time.perf_counter()
    cmd = [sys.executable, str(SCRIPTS / script), *args, str(project_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"{script} 失败（exit {proc.returncode}）：\n{output[-2000:]}")
    if output:
        elapsed = time.perf_counter() - started
        print(f"[{script} {elapsed:.2f}s] {output.splitlines()[-1]}")


def run_all(
    project_dir: Path,
    skip_report: bool = False,
    require_srm: bool = True,
    srm_credentials: tuple[str, str] | None = None,
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
    stage_env: dict[str, str] | None = None
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
        stage_env = os.environ.copy()
        stage_env.update({"SRM_USER": username, "SRM_PASSWORD": password})

    stages = STAGES if not skip_report else [s for s in STAGES if s[0] != "render_report.py"]
    if require_srm:
        insert_at = next(i for i, (name, _args) in enumerate(stages) if name == "assess_risk.py")
        stages.insert(insert_at, ("query_sources.py", []))
    for script, args in stages:
        if script == "render_report.py" and not require_srm:
            args = [*args, "--skip-srm-gate"]
        run_stage(script, args, project_dir, env=stage_env)
