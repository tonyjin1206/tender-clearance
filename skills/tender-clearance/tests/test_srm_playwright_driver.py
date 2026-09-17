from __future__ import annotations

import inspect

from tc.srm_playwright_driver import (
    PlaywrightSrmDriver,
    _company_lookup_query,
    _normalize_company_candidates,
    _branch_candidates,
    _clean_company_name,
    _select_company_candidate,
)
from tc.sources import QuerySubject


def test_company_candidates_ignore_empty_and_operation_rows():
    rows = _normalize_company_candidates(
        [
            {"企业名称": "甲公司", "统一社会信用代码": "91310000MA00000001"},
            {"企业名称": "甲公司", "统一社会信用代码": "91310000MA00000001"},
            {"企业名称": "-", "统一社会信用代码": "-", "操作": "查看"},
            {"公司名称": "乙公司", "信用代码": "91310000MA00000002", "操作": "详情"},
        ],
        "公司",
    )

    assert rows == [
        {"name": "甲公司", "uscc": "91310000MA00000001"},
        {"name": "乙公司", "uscc": "91310000MA00000002"},
    ]


def test_company_lookup_query_removes_bid_document_stamp_suffix_only():
    assert _company_lookup_query(QuerySubject("S1", "长春市赢天环保科技有限公司(公章)", None)) == "长春市赢天环保科技有限公司"
    assert _company_lookup_query(QuerySubject("S1", "甲 公司", None)) == "甲公司"
    assert _company_lookup_query(QuerySubject("S1", "长春市赢天环保科技有限公司(公章)", "91310000MA00000002")) == "91310000MA00000002"


def test_company_name_cleanup_preserves_branch_suffix():
    assert _clean_company_name("甲 公司（公章）") == "甲公司"
    assert _clean_company_name("甲公司分公司（公章）") == "甲公司分公司"


def test_company_candidates_do_not_guess_name_from_unlabelled_fields():
    rows = _normalize_company_candidates(
        [{"法定代表人": "张三", "注册地址": "北京市", "统一社会信用代码": "91310000MA00000001"}],
        "",
    )
    assert rows == [{"name": "", "uscc": "91310000MA00000001"}]


def test_company_candidates_do_not_count_unrelated_visible_tables():
    rows = _normalize_company_candidates(
        [
            {"企业名称": "甲公司", "统一社会信用代码": "91310000MA00000001"},
            {"企业名称": "页面其他企业", "统一社会信用代码": "91310000MA00000009"},
        ],
        "甲公司",
    )

    assert rows == [{"name": "甲公司", "uscc": "91310000MA00000001"}]


def test_company_candidate_prefers_exact_company_over_branch_cards():
    rows = [
        {"name": "吉林省鑫誉环境检测有限公司", "uscc": "91310100MA00000001"},
        {"name": "吉林省鑫誉环境检测有限公司兴安盟分公司", "uscc": "91150100MA00000002"},
        {"name": "吉林省鑫誉环境检测有限公司吉林市分公司", "uscc": "91220200MA00000003"},
    ]

    assert _select_company_candidate(rows, "吉林省鑫誉环境检测有限公司") == rows[0]


def test_company_candidate_keeps_multiple_exact_subjects_manual():
    rows = [
        {"name": "甲公司", "uscc": "91310000MA00000001"},
        {"name": "甲公司", "uscc": "91310000MA00000002"},
    ]

    assert _select_company_candidate(rows, "甲公司") is None


def test_company_candidate_keeps_name_code_conflict_manual_even_with_matching_code():
    rows = [
        {"name": "甲公司", "uscc": "91310000MA00000001"},
        {"name": "甲公司", "uscc": "91310000MA00000002"},
    ]

    assert _select_company_candidate(rows, "甲公司", "91310000MA00000001") is None


def test_branch_cards_are_candidates_and_not_the_selected_duplicate():
    rows = [
        {"name": "甲公司", "uscc": "91310000MA00000001"},
        {"name": "甲公司上海分公司", "uscc": "91310000MA00000002"},
    ]
    selected = _select_company_candidate(rows, "甲公司")
    branches = _branch_candidates(rows, "甲公司", selected)

    assert selected == rows[0]
    assert branches == [{
        "name": "甲公司上海分公司",
        "uscc": "91310000MA00000002",
        "relation": "branch_candidate",
    }]


def test_subject_search_default_route_is_home_company_lookup():
    source = inspect.getsource(PlaywrightSrmDriver.search_subject)
    assert "_ensure_company_search_open" in source
    assert "_fill_company_search" in source
    assert "_ensure_vendor_archive_open" not in source
    assert "供应商档案" not in source


def test_company_search_prioritizes_real_homepage_search_input_and_icon_submit():
    input_source = inspect.getsource(PlaywrightSrmDriver._company_search_input)
    fill_source = inspect.getsource(PlaywrightSrmDriver._fill_company_search)

    assert "ykj-search" in input_source
    assert "请输入企业名称" in input_source
    assert "请输入关键字" in input_source
    assert '"搜索服务名称"' in input_source
    assert "查一下" in fill_source
    assert "preferred.click()" in fill_source
    assert "class*='search'" in fill_source or 'class*="search"' in fill_source

    rows_source = inspect.getsource(PlaywrightSrmDriver._company_result_rows)
    assert "_COMPANY_ROWS_JS" in rows_source
    assert ".col-name" in inspect.getsource(PlaywrightSrmDriver)


def test_section_tables_are_scoped_by_expected_headers():
    source = PlaywrightSrmDriver._TABLE_PAIR_JS
    assert "expectedGroups" in source
    assert "scopedHeads" in source
    assert "别的栏目" in source


def test_subject_search_records_home_company_lookup_evidence():
    class Page:
        def wait_for_timeout(self, _milliseconds):
            pass

    driver = PlaywrightSrmDriver.__new__(PlaywrightSrmDriver)
    driver._page = Page()
    driver._active_profile_frame = None
    driver._company_search_open = False
    calls: list[str] = []

    driver._ensure_company_search_open = lambda: calls.append("open") or True
    driver._fill_company_search = lambda value: calls.append(f"fill:{value}") or True
    driver._company_result_rows = lambda query: [{
        "name": "甲公司",
        "uscc": "91310000MA00000001",
    }]
    driver._click_company_result = lambda row: calls.append("click") or True
    driver._click_anywhere = lambda *args, **kwargs: calls.append("details") or True
    driver._wait_profile_ready = lambda query: object()
    driver._read_identity = lambda _frame: ("甲公司", "91310000MA00000001")

    result = driver.search_subject(
        QuerySubject("S1", "甲公司", "91310000MA00000001")
    )

    assert result.confirmation == "confirmed"
    assert calls == ["open", "fill:91310000MA00000001", "click", "details"]
    assert "入口=主页>查企业" in (result.detail or "")


def test_subject_search_clicks_exact_company_when_branches_are_also_returned():
    class Page:
        def wait_for_timeout(self, _milliseconds):
            pass

    driver = PlaywrightSrmDriver.__new__(PlaywrightSrmDriver)
    driver._page = Page()
    driver._active_profile_frame = None
    driver._company_search_open = False
    calls: list[object] = []

    rows = [
        {"name": "吉林省鑫誉环境检测有限公司", "uscc": "91310100MA00000001"},
        {"name": "吉林省鑫誉环境检测有限公司兴安盟分公司", "uscc": "91150100MA00000002"},
        {"name": "吉林省鑫誉环境检测有限公司吉林市分公司", "uscc": "91220200MA00000003"},
    ]
    driver._ensure_company_search_open = lambda: True
    driver._fill_company_search = lambda _value: True
    driver._company_result_rows = lambda _query: rows
    driver._click_company_result = lambda row: calls.append(row) or True
    driver._click_anywhere = lambda *args, **kwargs: True
    driver._wait_profile_ready = lambda _query: object()
    driver._read_identity = lambda _frame: ("吉林省鑫誉环境检测有限公司", "91310100MA00000001")

    result = driver.search_subject(
        QuerySubject("S3", "吉林省鑫誉环境检测有限公司", None)
    )

    assert result.confirmation == "candidate"
    assert calls == [rows[0]]
    assert "精确命中" in (result.detail or "")


def test_subject_search_name_conflict_requires_manual_review():
    class Page:
        def wait_for_timeout(self, _milliseconds):
            pass

    driver = PlaywrightSrmDriver.__new__(PlaywrightSrmDriver)
    driver._page = Page()
    driver._active_profile_frame = None
    driver._company_search_open = False
    driver._ensure_company_search_open = lambda: True
    driver._fill_company_search = lambda _value: True
    driver._company_result_rows = lambda _query: [{
        "name": "甲公司", "uscc": "91310000MA00000001",
    }]
    driver._click_company_result = lambda _row: True
    driver._click_anywhere = lambda *args, **kwargs: True
    driver._wait_profile_ready = lambda _query: object()
    driver._read_identity = lambda _frame: ("乙公司", "91310000MA00000001")

    result = driver.search_subject(QuerySubject("S1", "甲公司", "91310000MA00000001"))

    assert result.confirmation == "unconfirmed"
    assert "名称与查询主体冲突" in (result.detail or "")


def test_profile_identity_must_be_stable_before_reading():
    class Page:
        def wait_for_timeout(self, _milliseconds):
            pass

    driver = PlaywrightSrmDriver.__new__(PlaywrightSrmDriver)
    driver._read_identity = lambda _frame: ("甲公司", "91310000MA00000001")
    frame = type("Frame", (), {"page": Page()})()

    assert driver._wait_for_stable_identity(frame, "91310000MA00000001", 0) is True


def test_read_basic_extracts_legal_identity_fields_from_profile():
    class Frame:
        def evaluate(self, _script):
            return """企业名称
甲公司
统一社会信用代码
91310000MA00000001
法定代表人
张三
法定代表人身份证号
110101199001011234
注册资本
100万人民币"""

    driver = PlaywrightSrmDriver.__new__(PlaywrightSrmDriver)
    result = driver._read_basic(Frame())

    assert result.structured is True
    assert result.fields["企业名称"] == "甲公司"
    assert result.fields["统一社会信用代码"] == "91310000MA00000001"
    assert result.fields["法定代表人"] == "张三"
    assert result.fields["法定代表人身份证号"] == "110101199001011234"


def test_read_basic_extracts_inline_profile_fields():
    class Frame:
        def evaluate(self, _script):
            return "企业名称：甲公司\n法定代表人：张三\n法人代表身份证号：110101199001011234"

    driver = PlaywrightSrmDriver.__new__(PlaywrightSrmDriver)
    result = driver._read_basic(Frame())

    assert result.fields["企业名称"] == "甲公司"
    assert result.fields["法定代表人"] == "张三"
    assert result.fields["法人代表身份证号"] == "110101199001011234"
