from __future__ import annotations

from datetime import datetime, timezone

from tc.srm_browser import (
    BrowserCredentials,
    BrowserQueryCheckpoint,
    BrowserLoginResult,
    BrowserSectionResult,
    BrowserSubjectResult,
    SrmBrowserClient,
)
from tc.sources import QuerySubject


class FakeBrowser:
    def __init__(self, login="authenticated", structured=True):
        self.login_status = login
        self.structured = structured
        self.login_args = None
        self.closed = False

    def open(self, portal_url):
        assert portal_url.startswith("https://")

    def login(self, username, password):
        self.login_args = (username, password)
        return BrowserLoginResult(self.login_status, "登录结果")

    def search_subject(self, subject):
        return BrowserSubjectResult(
            name="腾讯云计算（北京）有限责任公司",
            uscc="911101085636549482",
            confirmation="confirmed",
            profile_ref="portrait://tencent",
        )

    def read_section(self, section):
        if section in ("shareholders", "branches", "personnel"):
            return BrowserSectionResult(
                section=section,
                records=([{"股东名称": "示例股东", "投资金额": "100"}] if section == "shareholders"
                         else [{"企业名称": "示例分支", "成立日期": "2020-01-01", "企业状态": "存续", "法人": "示例法人"}] if section == "branches"
                         else [{"姓名": "示例人员", "职位": "经理"}]),
                evidence_ref=f"portrait://tencent/{section}",
                structured=self.structured,
            )
        return BrowserSectionResult(
            section=section,
            fields={"企业名称": "腾讯云计算（北京）有限责任公司", "企业状态": "存续"}
            if section == "basic" and self.structured else {},
            records=([
                {"案由": "服务合同纠纷", "案号": "（2025）示例号"},
            ] if section == "judicial" and self.structured else [
                {"登记日期": "2022-07-12", "状态": "无效"},
            ] if section == "operating" and self.structured else []),
            counts={"法律诉讼": 72} if section == "judicial" and self.structured else {},
            evidence_ref=f"portrait://tencent/{section}",
            structured=self.structured,
        )

    def close(self):
        self.closed = True


def test_browser_query_uses_no_http_and_returns_three_sections():
    driver = FakeBrowser()
    client = SrmBrowserClient(lambda: driver, credentials=BrowserCredentials("u", "p"))
    result = client.query(
        QuerySubject("S1", "腾讯云计算（北京）有限责任公司", "911101085636549482"),
        datetime.now(timezone.utc),
    )
    assert result.status == "match"
    assert result.request_mode == "browser_session"
    assert result.parsed_count == 6
    assert driver.login_args == ("u", "p")
    assert driver.closed is True


def test_browser_query_clears_runtime_credentials():
    creds = BrowserCredentials("u", "secret")
    driver = FakeBrowser()
    client = SrmBrowserClient(lambda: driver, credentials=creds)
    result = client.query(QuerySubject("S1", "腾讯云", None), datetime.now(timezone.utc))
    assert result.status == "match"
    assert creds.username == ""
    assert creds.password == ""
    assert "secret" not in repr(creds)
    assert "secret" not in (result.detail or "")


def test_missing_credentials_does_not_open_browser():
    called = False

    def make_driver():
        nonlocal called
        called = True
        raise AssertionError("不得打开浏览器")

    result = SrmBrowserClient(make_driver).query(
        QuerySubject("S1", "腾讯云", None), datetime.now(timezone.utc)
    )
    assert result.status == "needs_manual_review"
    assert called is False


def test_captcha_or_login_wall_is_blocked_without_retry():
    driver = FakeBrowser(login="blocked")
    result = SrmBrowserClient(
        lambda: driver, credentials=BrowserCredentials("u", "p")
    ).query(QuerySubject("S1", "腾讯云", None), datetime.now(timezone.utc))
    assert result.status == "blocked"
    assert driver.closed is True


def test_empty_page_is_no_result_not_no_risk():
    driver = FakeBrowser(structured=False)
    result = SrmBrowserClient(
        lambda: driver, credentials=BrowserCredentials("u", "p")
    ).query(QuerySubject("S1", "腾讯云", None), datetime.now(timezone.utc))
    assert result.status == "no_result"
    assert "无风险" in (result.detail or "")


def test_keep_session_logs_in_once_and_closes_explicitly():
    """批量查询复用一个浏览器会话；close 后句柄和认证状态均清除。"""
    drivers = []

    def factory():
        d = FakeBrowser()
        drivers.append(d)
        return d

    client = SrmBrowserClient(
        factory,
        credential_provider=lambda: BrowserCredentials("u", "p"),
        keep_session=True,
    )
    now = datetime.now(timezone.utc)
    assert client.query(QuerySubject("S1", "甲公司", "911101085636549482"), now).status == "match"
    assert client.query(QuerySubject("S2", "乙公司", "911101085636549482"), now).status == "match"
    assert len(drivers) == 1
    assert drivers[0].closed is False
    client.close()
    assert drivers[0].closed is True


def test_query_batch_resumes_from_checkpoint_and_calls_checkpoint_hook():
    driver = FakeBrowser()
    checkpoint = BrowserQueryCheckpoint(run_id="run-1", completed={"S1": "match"})
    snapshots = []
    client = SrmBrowserClient(
        lambda: driver,
        credential_provider=lambda: BrowserCredentials("u", "p"),
        keep_session=True,
    )

    results = client.query_batch(
        [QuerySubject("S1", "甲公司", None), QuerySubject("S2", "乙公司", None)],
        datetime.now(timezone.utc), checkpoint,
        on_checkpoint=lambda current: snapshots.append(current.to_dict()),
    )

    assert [subject.supplier_id for subject, _ in results] == ["S2"]
    assert checkpoint.completed == {"S1": "match", "S2": "match"}
    assert snapshots[-1]["schema_version"].endswith("checkpoint.v1")
    assert "password" not in str(snapshots[-1]).lower()
    assert driver.closed is True


def test_browser_reopens_once_after_accidental_close():
    class ClosedOnce(FakeBrowser):
        def search_subject(self, subject):
            raise RuntimeError("Target page, context or browser has been closed")

    first = ClosedOnce()
    second = FakeBrowser()
    drivers = iter([first, second])
    client = SrmBrowserClient(
        lambda: next(drivers),
        credentials=BrowserCredentials("u", "p"),
        reopen_attempts=1,
    )

    result = client.query(QuerySubject("S1", "甲公司", None), datetime.now(timezone.utc))

    assert result.status == "match"
    assert first.closed is True and second.closed is True


def test_browser_session_state_exposes_manual_login_state():
    driver = FakeBrowser(login="manual")
    client = SrmBrowserClient(
        lambda: driver, credentials=BrowserCredentials("u", "p")
    )

    result = client.query(QuerySubject("S1", "甲公司", None), datetime.now(timezone.utc))

    assert result.status == "needs_manual_review"
    assert client.state == "manual_review"


def test_branch_candidates_survive_adapter_result_without_becoming_records():
    class BranchBrowser(FakeBrowser):
        def search_subject(self, subject):
            return BrowserSubjectResult(
                name="甲公司", uscc="91310000MA00000001", confirmation="confirmed",
                branch_candidates=[{
                    "name": "甲公司上海分公司",
                    "uscc": "91310000MA00000002",
                    "relation": "branch_candidate",
                }],
            )

    result = SrmBrowserClient(
        lambda: BranchBrowser(), credentials=BrowserCredentials("u", "p")
    ).query(QuerySubject("S1", "甲公司", None), datetime.now(timezone.utc))

    assert result.branch_candidates[0]["relation"] == "branch_candidate"
    assert all(record.record_kind != "branch_candidate" for record in result.records)



# ---- 集成：宿主注册表 → SrmAdapter 浏览器模式优先（与 HTTP 客户端分离） ----


class _FakeSrmDriver:
    """最小可用驱动替身：登录成功 + 主体确认 + basic 有字段。"""

    version = "fake-browser/0.1"

    def __init__(self):
        self.closed = False

    def open(self, portal_url):
        pass

    def login(self, username, password):
        from tc.srm_browser import BrowserLoginResult

        return BrowserLoginResult(status="authenticated")

    def search_subject(self, subject):
        from tc.srm_browser import BrowserSubjectResult

        return BrowserSubjectResult(subject.name, subject.uscc, "confirmed")

    def read_section(self, section):
        from tc.srm_browser import BrowserSectionResult

        if section == "basic":
            return BrowserSectionResult(section="basic", fields={"注册资本": "1000万元"},
                                        evidence_ref="企业画像 > 基本信息")
        if section == "judicial":
            return BrowserSectionResult(section="judicial", counts={"法律诉讼": 3},
                                        evidence_ref="企业画像 > 司法风险")
        return BrowserSectionResult(section="operating", counts={},
                                    disabled_categories=["经营异常", "行政处罚"],
                                    evidence_ref="企业画像 > 经营风险")

    def close(self):
        self.closed = True


def test_srm_adapter_uses_registered_browser_driver():
    """注册驱动工厂后 SrmAdapter 走浏览器模式；query 后注册表可清理。"""
    import os
    from datetime import datetime, timezone

    from tc import srm_browser as sb
    from tc.sources import QuerySubject, SrmAdapter

    os.environ.pop("SRM_BROWSER_DRIVER", None)
    sb.clear_browser_driver()
    created = []
    sb.register_browser_driver(
        factory=lambda: (created.append(1) or _FakeSrmDriver()),
        credential_provider=lambda: sb.BrowserCredentials("u", "p"),
    )
    try:
        adapter = SrmAdapter()
        r = adapter.query(QuerySubject("S1", "虚构供应商甲有限公司", "91350100M000100Y43"),
                          datetime.now(timezone.utc))
        assert r.status == "match"
        assert r.request_mode == "browser_session"
        assert adapter.version.startswith("srm-browser/")
        kinds = [rec.record_kind for rec in r.records]
        assert "registration" in kinds and "judicial_summary" in kinds
        assert created == [1]
    finally:
        sb.clear_browser_driver()

    # 清理后回到 HTTP 客户端路径（auth.mode=none → needs_manual_review）
    r2 = SrmAdapter().query(QuerySubject("S1", "某公司", "91350100M000100Y43"),
                            datetime.now(timezone.utc))
    assert r2.status == "needs_manual_review"


def test_external_record_accepts_browser_record_kinds():
    """新 record_kind（judicial_summary/operating_risk/operating_summary）可入契约。"""
    from tc.models import ExternalRecord

    for kind in ("judicial_summary", "operating_risk", "operating_summary"):
        rec = ExternalRecord(
            record_id="R-test", source_id="srm", record_kind=kind,
            fields={"分类数量": {"法律诉讼": 3}},
        )
        assert rec.record_kind == kind


def test_srm_adapter_env_switch_registers_playwright_driver(monkeypatch):
    """SRM_BROWSER_DRIVER=playwright 时 SrmAdapter 启用浏览器模式（不真正启动浏览器）。"""
    import os
    from datetime import datetime, timezone

    from tc import srm_browser as sb
    from tc.sources import SrmAdapter

    sb.clear_browser_driver()
    monkeypatch.setenv("SRM_BROWSER_DRIVER", "playwright")
    try:
        adapter = SrmAdapter()
        client = adapter._get_client()
        assert isinstance(client, sb.SrmBrowserClient)
        assert sb.get_browser_driver_factory() is not None
        # 凭据回调读环境变量，缺失时 SrmBrowserClient 如实返回 needs_manual_review
        r = client.query(
            __import__("tc.sources", fromlist=["QuerySubject"]).QuerySubject(
                "S1", "虚构供应商甲有限公司", "91350100M000100Y43"),
            datetime.now(timezone.utc))
        assert r.status == "needs_manual_review"
    finally:
        sb.clear_browser_driver()
        monkeypatch.delenv("SRM_BROWSER_DRIVER", raising=False)
