"""T11 + 清单完整性：盘点、哈希、异常显性化。"""

from __future__ import annotations

import json

SUP_DIRS = ["huaxin-chuangyuan", "huaxin-info", "yuntu-zhilian", "zhongheng-taida"]


def _inventory(alpha_project):
    return json.loads((alpha_project / "output/interim/inventory.json").read_text(encoding="utf-8"))


def test_all_input_files_inventoried_with_hashes(alpha_project):
    inv = _inventory(alpha_project)
    assert inv["supplier_dirs"] == sorted(SUP_DIRS)
    paths = {d["relative_path"] for d in inv["documents"]}
    assert "procurement/采购文件.pdf" in paths
    assert "bids/huaxin-chuangyuan/商务标-授权书.pdf" in paths
    for d in inv["documents"]:
        assert len(d["sha256"]) == 64
        assert d["document_id"].startswith("DOC-")


def test_broken_files_recorded_not_crashing(alpha_project):
    """T11：密码保护 / 损坏 / 零字节文件 —— 记录原因，不崩溃。"""
    inv = _inventory(alpha_project)
    by_name = {d["relative_path"]: d for d in inv["documents"]}
    pwd = by_name["bids/yuntu-zhilian/附件-授权书加密版.pdf"]
    corrupt = by_name["bids/yuntu-zhilian/历史业绩损坏文件.pdf"]
    empty = by_name["bids/yuntu-zhilian/营业执照空文件.pdf"]
    assert pwd["extraction_status"] == "password_protected"
    assert corrupt["extraction_status"] == "corrupt"
    assert empty["extraction_status"] == "empty"
    for d in (pwd, corrupt, empty):
        assert d["status_detail"]


def test_original_inputs_not_modified(alpha_project):
    """原始标书只读：output/ 与输入分离。"""
    inv = _inventory(alpha_project)
    assert all(not d["relative_path"].startswith("output/") for d in inv["documents"])


def test_id_digest_salt_not_written(alpha_project):
    text = (alpha_project / "output/清标结果.json").read_text(encoding="utf-8")
    assert "tender-clearance-default-salt-v1" not in text
    assert "id_digest_salt_fingerprint" in text  # 只写指纹


def test_bid_subtypes_are_classified_and_non_business_fields_are_excluded(alpha_project):
    """新口径：技术标/一览表仍盘点，但不参与主体字段识别。"""
    inv = _inventory(alpha_project)
    by_name = {d["relative_path"]: d for d in inv["documents"]}
    assert by_name["bids/huaxin-chuangyuan/技术标.docx"]["bid_subtype"] == "technical"
    assert by_name["bids/huaxin-chuangyuan/开标一览表.xlsx"]["bid_subtype"] == "bid_schedule"
    content = json.loads((alpha_project / "output/interim/content.json").read_text(encoding="utf-8"))
    excluded = [f for f in content["fields"] if "技术标.docx" in f["relative_path"] or "开标一览表.xlsx" in f["relative_path"]]
    assert excluded == []
