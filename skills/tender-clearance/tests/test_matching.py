"""主体识别与交叉匹配单元测试（规范化、摘要稳定性）+ T05。"""

from __future__ import annotations

import json

import pytest

from tc.normalize import (
    address_region_key,
    company_search_key,
    id_digest,
    mask_id_number,
    name_similarity,
    normalize_phone,
    uscc_check_digit,
    validate_uscc,
)


class TestNormalize:
    def test_uscc_standard_example(self):
        # GB 32100-2015 校验示例
        ok = validate_uscc("91350100M000100Y43")
        assert ok[1] is True

    def test_uscc_rejects_bad_check_digit(self):
        code = "91350100M000100Y44"
        ok = validate_uscc(code)
        assert ok[1] is False

    def test_uscc_check_digit_matches_weights(self):
        code17 = "91110108MA01CWY70"
        assert uscc_check_digit(code17) == "M"

    def test_id_digest_stable_and_salted(self):
        a = id_digest("110101199003077258", salt="s1")
        b = id_digest("110101199003077258", salt="s1")
        c = id_digest("110101199003077258", salt="s2")
        assert a == b and a != c and len(a) == 16

    def test_mask_keeps_head_tail(self):
        assert mask_id_number("110101199003077258") == "1101**********7258"

    def test_phone_extension_split(self):
        main, ext = normalize_phone("010-88886666转801")
        assert main == "01088886666" and ext == "801"

    def test_region_key_hierarchical(self):
        assert address_region_key("北京市海淀区北清路68号") == "北京市海淀区"
        assert address_region_key("北京市朝阳区建国路88号") == "北京市朝阳区"
        assert address_region_key("深圳市南山区高新南一道") == "深圳市南山区"

    def test_name_similarity_candidates_only(self):
        assert name_similarity("北京华信创远科技有限公司", "北京华信创远信息技术有限公司") > 0.75
        assert name_similarity("中恒泰达（北京）工程管理有限公司", "云图智联信息技术有限公司") < 0.5
        # 检索键剥离组织形式后缀
        assert company_search_key("北京华信创远科技有限公司") == "北京华信创远科技"


class TestCrossMatching:
    def test_shared_agent_id_produces_exact_digest_match(self, alpha_project):
        """T02 前置：两家的授权代表证件摘要 exact 匹配。"""
        matches = json.loads((alpha_project / "output/interim/matches.json").read_text(encoding="utf-8"))
        hit = [m for m in matches["matches"] if m["field"] == "bid_agent_id_digest" and m["match_type"] == "exact"]
        assert len(hit) == 1
        m = hit[0]
        assert sorted([m["supplier_a"], m["supplier_b"]]) == ["huaxin-chuangyuan", "zhongheng-taida"]

    def test_similar_names_not_merged(self, alpha_project):
        """T05：名称相近但信用代码不同 —— 仅候选线索，主体保持独立。"""
        entities = json.loads((alpha_project / "output/interim/entities.json").read_text(encoding="utf-8"))
        sups = {s["directory_name"]: s for s in entities["suppliers"]}
        a, d = sups["huaxin-chuangyuan"], sups["huaxin-info"]
        assert a["uscc"] != d["uscc"]

        matches = json.loads((alpha_project / "output/interim/matches.json").read_text(encoding="utf-8"))
        cand = [m for m in matches["matches"]
                if m["field"] == "company_name" and m["match_type"] == "candidate"]
        pairs = [{m["supplier_a"], m["supplier_b"]} for m in cand]
        assert any(p == {"huaxin-chuangyuan", "huaxin-info"} for p in pairs)

        # 股权/失信记录不得因名称相似而归属错主体
        ext = json.loads((alpha_project / "output/interim/external.json").read_text(encoding="utf-8"))
        sup_id_of = {s["supplier_id"]: s["directory_name"] for s in entities["suppliers"]}
        for r in ext["records"]:
            if r["subject_uscc"] == d["uscc"]:
                assert r["supplier_id"] in (None, d["supplier_id"])

    def test_mobile_numbers_compared_by_digest_not_plaintext(self, alpha_project):
        matches = json.loads((alpha_project / "output/interim/matches.json").read_text(encoding="utf-8"))
        for m in matches["matches"]:
            if m["field"] == "phone":
                assert "1" not in m["value_a"] or "****" in m["value_a"]
