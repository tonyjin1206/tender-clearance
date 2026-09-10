"""富奥 SRM 浏览器会话适配器。

本模块刻意不实现 HTTP 客户端，也不导入 requests/httpx/urllib。站点登录、
页面导航和可见页面读取由宿主（Claude/Codex 的浏览器能力）通过
``SrmBrowserDriver`` 提供；本模块只负责：

* 运行时凭据的内存生命周期；
* 登录墙、验证码、权限阻断和主体歧义的状态机；
* 从浏览器桥接返回的可见页面结构化结果生成 AdapterResult；
* 保留页面证据引用，不把空 XHR 解释为“无风险”。

禁止把 Cookie、loginToken、密码或完整页面 HTML 放进本模块的结果对象、日志
或异常文本。浏览器宿主也必须遵守相同边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Protocol

from .sources import AdapterRecord, AdapterResult, QuerySubject


ADAPTER_VERSION = "srm-browser/0.1.0"
PORTAL_URL = "https://yonbip.fawer.com.cn/"


@dataclass
class BrowserCredentials:
    """运行时凭据；不提供可暴露密码的 repr，任务结束后主动清空。"""

    username: str = ""
    password: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 仅防止误打印
        return "BrowserCredentials(username=<redacted>, password=<redacted>)"

    def clear(self) -> None:
        self.username = ""
        self.password = ""


CredentialProvider = Callable[[], BrowserCredentials]


BrowserLoginStatus = Literal["authenticated", "blocked", "failed", "manual"]
SubjectConfirmation = Literal["confirmed", "candidate", "unconfirmed"]
SectionName = Literal["basic", "judicial", "operating"]


@dataclass
class BrowserLoginResult:
    status: BrowserLoginStatus
    detail: str | None = None


@dataclass
class BrowserSubjectResult:
    name: str | None
    uscc: str | None
    confirmation: SubjectConfirmation
    profile_ref: str | None = None
    detail: str | None = None


@dataclass
class BrowserSectionResult:
    """浏览器从可见页面提取的脱敏结构化结果。

    ``evidence_ref`` 应是页面 URL、页面标题+分类或快照引用，不应包含 Cookie、
    loginToken、密码和未经脱敏的完整 HTML。
    """

    section: SectionName
    fields: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    disabled_categories: list[str] = field(default_factory=list)  # 页面禁用/空分类（≠无风险）
    evidence_ref: str | None = None
    structured: bool = True
    detail: str | None = None


class SrmBrowserDriver(Protocol):
    """由 Claude/Codex 浏览器能力实现的最小桥接接口。

    实现必须使用真实浏览器页面交互读取可见 DOM/无敏感的页面状态；不得在桥接
    内部改用 requests、curl、fetch 或直接复现 XHR。
    """

    def open(self, portal_url: str) -> None: ...

    def login(self, username: str, password: str) -> BrowserLoginResult: ...

    def search_subject(self, subject: QuerySubject) -> BrowserSubjectResult: ...

    def read_section(self, section: SectionName) -> BrowserSectionResult: ...

    def close(self) -> None: ...


# ----------------------------------------------------------------- 宿主注册表
# 宿主（Playwright 驱动、桌面浏览器桥接等）在运行时注册驱动工厂；
# SrmAdapter 据此选择浏览器模式。未注册时 SRM 不走浏览器模式。
# 注册表只保存工厂回调，不保存凭据。

_DRIVER_FACTORY: Callable[[], SrmBrowserDriver] | None = None
_CREDENTIAL_PROVIDER: CredentialProvider | None = None


def register_browser_driver(
    factory: Callable[[], SrmBrowserDriver],
    credential_provider: CredentialProvider | None = None,
) -> None:
    """注册宿主浏览器驱动工厂（及可选的运行时凭据回调）。仅存回调，不存凭据。"""
    global _DRIVER_FACTORY, _CREDENTIAL_PROVIDER
    _DRIVER_FACTORY = factory
    _CREDENTIAL_PROVIDER = credential_provider


def clear_browser_driver() -> None:
    global _DRIVER_FACTORY, _CREDENTIAL_PROVIDER
    _DRIVER_FACTORY = None
    _CREDENTIAL_PROVIDER = None


def get_browser_driver_factory() -> Callable[[], SrmBrowserDriver] | None:
    return _DRIVER_FACTORY


def get_browser_credential_provider() -> CredentialProvider | None:
    return _CREDENTIAL_PROVIDER


class SrmBrowserClient:
    """不调用 HTTP 的 SRM 浏览器会话查询器。"""

    source_id = "srm"
    version = ADAPTER_VERSION

    def __init__(
        self,
        driver_factory: Callable[[], SrmBrowserDriver],
        credentials: BrowserCredentials | None = None,
        credential_provider: CredentialProvider | None = None,
        portal_url: str = PORTAL_URL,
        keep_session: bool = False,
    ) -> None:
        if credentials is not None and credential_provider is not None:
            raise ValueError("credentials 与 credential_provider 只能提供一个")
        self._driver_factory = driver_factory
        self._credentials = credentials
        self._credential_provider = credential_provider
        self.portal_url = portal_url
        # keep_session=True：多次 query 间复用已登录的浏览器会话（流水线批量查询，
        # 避免逐供应商反复登录触发风控）；凭据仍在首次 query 后清空，会话靠 Cookie。
        self._keep_session = keep_session
        self._driver: SrmBrowserDriver | None = None
        self._session_authenticated = False

    def query(self, subject: QuerySubject, as_of: datetime) -> AdapterResult:
        if not subject.name and not subject.uscc:
            return AdapterResult(
                status="needs_manual_review",
                detail="缺少企业名称和统一社会信用代码，无法在浏览器中查询",
                request_mode="browser_session",
            )

        # keep_session 模式：复用已登录会话（流水线批量查询只登录一次，
        # 降低触发风控验证码的概率）。会话失效时结果会如实标注，重跑即重新登录。
        if self._keep_session and self._session_authenticated and self._driver is not None:
            return self._query_with_driver(self._driver, subject, as_of)

        creds = self._take_credentials()
        if not creds.username or not creds.password:
            creds.clear()
            return AdapterResult(
                status="needs_manual_review",
                detail="缺少 SRM 运行时凭据；未打开浏览器或发起登录",
                request_mode="browser_session",
            )

        try:
            driver = self._driver_factory()
            driver.open(self.portal_url)
            login = driver.login(creds.username, creds.password)
            if login.status != "authenticated":
                return AdapterResult(
                    status={
                        "blocked": "blocked",
                        "failed": "failed",
                        "manual": "needs_manual_review",
                    }[login.status],
                    detail=self._safe_detail(login.detail or "SRM 浏览器登录未完成"),
                    request_mode="browser_session",
                )
            if self._keep_session:
                # 会话由浏览器 Cookie 维持；凭据用后即清，后续查询不再需要
                self._driver = driver
                self._session_authenticated = True
                return self._query_with_driver(driver, subject, as_of)
            return self._query_with_driver(driver, subject, as_of)
        except Exception as exc:  # noqa: BLE001
            return AdapterResult(
                status="failed",
                detail=f"SRM 浏览器适配器执行失败：{type(exc).__name__}",
                request_mode="browser_session",
            )
        finally:
            creds.clear()
            if not self._keep_session:
                if driver is not None:
                    try:
                        driver.close()
                    except Exception:
                        pass
                self._driver = None

    def close(self) -> None:
        """keep_session 模式下显式关闭会话（流水线结束后调用）。"""
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
        self._driver = None
        self._session_authenticated = False

    def _query_with_driver(
        self, driver: SrmBrowserDriver, subject: QuerySubject, as_of: datetime
    ) -> AdapterResult:
        matched = driver.search_subject(subject)
        if matched.confirmation == "unconfirmed":
            return AdapterResult(
                status="needs_manual_review",
                detail=self._safe_detail(matched.detail or "企业主体未确认，停止读取风险页面"),
                request_mode="browser_session",
                response_ref=matched.profile_ref,
            )

        sections: list[BrowserSectionResult] = []
        for section in ("basic", "judicial", "operating"):
            try:
                item = driver.read_section(section)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                return AdapterResult(
                    status="failed",
                    detail=f"SRM 浏览器读取 {section} 页面失败：{type(exc).__name__}",
                    request_mode="browser_session",
                )
            sections.append(item)

        return self._to_result(subject, matched, sections, as_of)

    def _take_credentials(self) -> BrowserCredentials:
        if self._credential_provider is not None:
            return self._credential_provider()
        return self._credentials or BrowserCredentials()

    @staticmethod
    def _safe_detail(detail: str) -> str:
        # 驱动不应返回凭据；这里仍限制长度并过滤常见敏感键，避免状态文本外泄。
        text = str(detail)[:240]
        for marker in ("password=", "passwd=", "token=", "cookie="):
            if marker in text.lower():
                return "浏览器返回了敏感字段，已隐藏；请人工复核登录状态"
        return text

    @staticmethod
    def _to_result(
        subject: QuerySubject,
        matched: BrowserSubjectResult,
        sections: list[BrowserSectionResult],
        as_of: datetime,
    ) -> AdapterResult:
        records: list[AdapterRecord] = []
        refs: list[str] = []
        structured_sections = 0
        for item in sections:
            if item.evidence_ref:
                refs.append(item.evidence_ref)
            if item.structured:
                structured_sections += 1
            if item.section == "basic" and item.fields:
                fields = dict(item.fields)
                fields.setdefault("企业名称", matched.name or subject.name or "")
                if matched.uscc or subject.uscc:
                    fields.setdefault("统一社会信用代码", matched.uscc or subject.uscc or "")
                records.append(AdapterRecord(
                    record_kind="registration",
                    fields=fields,
                    subject_confirmation=matched.confirmation,
                ))
            elif item.section == "judicial":
                for record in item.records:
                    records.append(AdapterRecord(
                        record_kind="judicial_case",
                        fields=dict(record),
                        subject_confirmation=matched.confirmation,
                    ))
                if item.counts and not item.records:
                    summary: dict[str, Any] = {"分类数量": dict(item.counts)}
                    if item.disabled_categories:
                        summary["禁用分类"] = list(item.disabled_categories)
                    records.append(AdapterRecord(
                        record_kind="judicial_summary",
                        fields=summary,
                        subject_confirmation=matched.confirmation,
                    ))
            elif item.section == "operating":
                for record in item.records:
                    records.append(AdapterRecord(
                        record_kind="operating_risk",
                        fields=dict(record),
                        subject_confirmation=matched.confirmation,
                    ))
                if item.counts and not item.records:
                    op_summary: dict[str, Any] = {"分类数量": dict(item.counts)}
                    if item.disabled_categories:
                        op_summary["禁用分类"] = list(item.disabled_categories)
                    records.append(AdapterRecord(
                        record_kind="operating_summary",
                        fields=op_summary,
                        subject_confirmation=matched.confirmation,
                    ))

        if not structured_sections:
            return AdapterResult(
                status="no_result",
                detail="已完成主体确认，但页面没有可机读的结构化结果；不能据此结论为无风险，需人工复核页面",
                request_mode="browser_session",
                response_ref="; ".join(refs) or None,
            )
        if records:
            return AdapterResult(
                status="match",
                records=records,
                parsed_count=len(records),
                detail=(
                    f"浏览器会话查询完成（主体确认={matched.confirmation}，"
                    f"查询时间={as_of.isoformat()}）；页面空分类不解释为无风险"
                ),
                request_mode="browser_session",
                response_ref="; ".join(refs) or matched.profile_ref,
            )
        return AdapterResult(
            status="no_result",
            detail="页面已打开但没有可确认的记录；不能据此结论为无风险",
            request_mode="browser_session",
            response_ref="; ".join(refs) or matched.profile_ref,
        )
