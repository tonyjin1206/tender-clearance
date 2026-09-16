from __future__ import annotations

import json
from types import SimpleNamespace

from resolve_supplier_groups import _document_role, _load_manual_confirmations
from query_sources import _query_subject_for_supplier
from tc.pipeline import _requires_supplier_grouping


def test_flat_upload_requires_supplier_grouping(tmp_path):
    bids = tmp_path / "bids" / "inbox"
    bids.mkdir(parents=True)
    (bids / "商务.pdf").write_bytes(b"pdf")
    assert _requires_supplier_grouping(tmp_path)


def test_legacy_supplier_directories_keep_compatibility(tmp_path):
    supplier_dir = tmp_path / "bids" / "supplier-a"
    supplier_dir.mkdir(parents=True)
    (supplier_dir / "商务.pdf").write_bytes(b"pdf")
    assert not _requires_supplier_grouping(tmp_path)


def test_manual_group_confirmation_is_explicit_and_auditable(tmp_path):
    payload = {
        "schema_version": "tender-clearance.supplier-group-confirmation.v1",
        "assignments": [{
            "document_id": "DOC-1",
            "supplier_name": "示例供应商有限公司",
            "reason": "人工核对首页",
        }],
        "conflicts": [{"document_id": "DOC-2", "reason": "内容冲突"}],
    }
    (tmp_path / "supplier-group-confirmations.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    assignments, conflicts, errors = _load_manual_confirmations(tmp_path)
    assert not errors
    assert assignments["DOC-1"]["normalized_name"] == "示例供应商有限公司"
    assert assignments["DOC-1"]["reason"] == "人工核对首页"
    assert conflicts[0]["document_id"] == "DOC-2"


def test_manual_group_confirmation_preserves_document_role():
    inventory = SimpleNamespace(documents=[
        SimpleNamespace(document_id="DOC-1", category="bid", bid_subtype="business"),
    ])

    assert _document_role(inventory, "DOC-1", {}) == "business"
    assert _document_role(inventory, "DOC-1", {"role": "technical"}) == "technical"


def test_external_queries_use_confirmed_display_name_not_tenderer_declared_name():
    supplier = SimpleNamespace(
        supplier_id="SUP-2",
        display_name="吉林省鑫誉环境检测有限公司",
        declared_name="富奥汽车零部件股份有限公司",
        uscc=None,
    )
    subject = _query_subject_for_supplier(supplier)
    assert subject.name == "吉林省鑫誉环境检测有限公司"
