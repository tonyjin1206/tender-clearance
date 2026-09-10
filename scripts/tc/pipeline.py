"""按阶段顺序运行完整流水线（供测试与 SKILL.md 的命令序列复用）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent  # scripts/ 目录（tc 的上一级）

STAGES = [
    ("inventory.py", []),
    ("extract_content.py", []),
    ("extract_metadata.py", []),
    ("normalize_and_match.py", []),
    ("import_external_evidence.py", []),
    ("query_sources.py", []),
    ("assess_risk.py", []),
    ("render_report.py", ["--no-docx"]),
]


def run_stage(script: str, args: list[str], project_dir: Path) -> None:
    cmd = [sys.executable, str(SCRIPTS / script), *args, str(project_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"{script} 失败（exit {proc.returncode}）：\n{output[-2000:]}")
    if output:
        print(f"[{script}] {output.splitlines()[-1]}")


def run_all(project_dir: Path, skip_report: bool = False) -> None:
    stages = STAGES if not skip_report else [s for s in STAGES if s[0] != "render_report.py"]
    for script, args in stages:
        run_stage(script, args, project_dir)
