"""测试公共夹具：project-alpha 一次性构建并跑完流水线。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from tc.pipeline import run_all  # noqa: E402

FIXTURES = SKILL_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def alpha_project(tmp_path_factory) -> Path:
    """复制模板夹具到临时目录并运行完整流水线（offline 模式）。"""
    src = FIXTURES / "project-alpha"
    if not src.exists():
        from fixtures_meta.make_fixtures import build  # type: ignore[import-not-found]

        build()
    dst = tmp_path_factory.mktemp("alpha") / "project-alpha"
    shutil.copytree(src, dst)
    # 模板中的历史输出不带入
    shutil.rmtree(dst / "output", ignore_errors=True)
    (dst / "output").mkdir()
    # 离线固定夹具用于 Core 规则回归；真实报告默认由 SRM 门禁强制控制。
    run_all(dst, require_srm=False)
    return dst


@pytest.fixture(scope="session")
def alpha_findings(alpha_project: Path) -> dict:
    import json

    return json.loads((alpha_project / "output/interim/findings.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def alpha_bundle(alpha_project: Path) -> dict:
    import json

    return json.loads((alpha_project / "output/清标结果.json").read_text(encoding="utf-8"))
