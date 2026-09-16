from datetime import datetime, timezone

import pytest

from tc.models import (
    EntitiesFile,
    ExternalEvidenceFile,
    ExternalQuery,
    RunInfo,
    Supplier,
)
from tc.srm_gate import SrmReportGateError, validate_srm_report_gate


def _run() -> RunInfo:
    return RunInfo(
        run_id="run-test",
        started_at=datetime.now(timezone.utc),
        tool_version="test",
        rules_version="test",
        python_version="3.12",
        project_id="P-test",
        redaction_mode="none",
        id_digest_salt_fingerprint="00000000",
    )


def _entities(run: RunInfo) -> EntitiesFile:
    return EntitiesFile(
        run=run,
        suppliers=[Supplier(
            supplier_id="SUP-1", directory_name="a", display_name="甲公司",
            declared_name="甲公司", uscc="91350100M000100Y43",
            uscc_status="present_valid", confirmation="confirmed",
        )],
        parties=[], contacts=[], notes=[],
    )


def _external(run: RunInfo, *, status: str, mode: str = "browser_session") -> ExternalEvidenceFile:
    return ExternalEvidenceFile(
        run=run,
        queries=[ExternalQuery(
            query_id="Q-1", source_id="srm", subject_supplier_id="SUP-1",
            subject_key={"name": "甲公司", "uscc": "91350100M000100Y43"},
            query_mode=mode, queried_at=datetime.now(timezone.utc), status=status,
            record_count=0, detail="test",
        )],
        records=[], ownership=[],
    )


def test_authenticated_srm_no_result_allows_report():
    run = _run()
    validate_srm_report_gate(_entities(run), _external(run, status="no_result"))


def test_manual_import_cannot_replace_authenticated_srm_query():
    run = _run()
    with pytest.raises(SrmReportGateError, match="未完成本次 SRM 登录查询"):
        validate_srm_report_gate(_entities(run), _external(run, status="no_result", mode="manual_import"))


def test_blocked_srm_query_stops_report():
    run = _run()
    with pytest.raises(SrmReportGateError, match="blocked"):
        validate_srm_report_gate(_entities(run), _external(run, status="blocked"))
