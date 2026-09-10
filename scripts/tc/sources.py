"""外部数据源适配器（阶段四，P4/P5）。

设计约束（方案 1.1 / 6.4 / 8.2）：
- 只使用正式 API 或允许的公开查询入口；不规避验证码、登录、限频或技术保护措施；
- 遇到验证码 / 登录墙 / 访问控制 → status="blocked"，停止自动化查询；
- 未配置正式查询入口或缺主体键 → status="needs_manual_review"，生成待人工查询任务；
- 适配器可注入传输层（HttpTransport），在测试替身上验证状态流转；
  真实站点必须在已授权环境中人工验收后才能启用。

富奥 SRM（source_id="srm"）为内网系统：HTTP/token 客户端与浏览器会话客户端分离。
浏览器模式由宿主注入浏览器桥接，不在本模块内发起网页请求。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

ADAPTER_VERSION = "0.3.0"

# 渠道 → external-evidence/ 导入子目录
IMPORT_DIRS = {
    "government_procurement": "government-procurement",
    "srm": "srm-authorized-export",
}

SOURCE_LABELS = {
    "government_procurement": "中国政府采购网",
    "srm": "富奥SRM",
}


@dataclass
class QuerySubject:
    supplier_id: str | None
    name: str | None
    uscc: str | None


@dataclass
class AdapterRecord:
    record_kind: str  # dishonesty / penalty / judicial_case / ownership / registration / other
    fields: dict[str, Any]
    effective_from: str | None = None
    effective_to: str | None = None
    subject_confirmation: str = "confirmed"


@dataclass
class AdapterResult:
    status: str  # ExternalQueryStatus
    records: list[AdapterRecord] = field(default_factory=list)
    detail: str | None = None
    request_mode: str = "none"  # official_api / public_web / none
    response_ref: str | None = None  # 原始响应/快照引用说明
    parsed_count: int = 0


@dataclass
class HttpRequest:
    method: str = "GET"                 # GET / POST（表单编码）
    url: str = ""
    params: dict[str, str] = field(default_factory=dict)   # GET 查询参数
    form: dict[str, str] = field(default_factory=dict)     # POST 表单字段
    headers: dict[str, str] = field(default_factory=dict)


HttpTransport = Callable[[HttpRequest], tuple[int, str]]


class SourceAdapter(Protocol):
    source_id: str
    version: str

    def query(self, subject: QuerySubject, as_of: datetime) -> AdapterResult: ...


# --------------------------------------------------------------------- 配置

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "rules" / "external-sources.yaml"


@dataclass
class SourceConfig:
    source_id: str
    label: str
    query_mode: str = "none"  # official_api / public_web / none
    endpoint: str | None = None
    portal_url: str | None = None          # 人工查询入口（公开网站首页/查询页）
    requires_credentials: bool = False     # true 表示需用户运行时提供账号（仅内存使用）
    params_name: str = "name"
    params_uscc: str | None = None
    method: str = "GET"                          # GET / POST
    form_fields: dict[str, str] = field(default_factory=dict)  # POST 表单（值支持 {name}/{uscc} 占位）
    response_type: str = "pattern"               # pattern（正则命名组） / html_table（行 x 列）
    row_pattern: str | None = None               # html_table 行正则
    field_map: list[str] = field(default_factory=list)  # html_table 列名（按位置）
    pages: int = 1                               # 抓取页数（从 1 开始）
    page_param: str | None = None                # 页码参数名（GET 追加 / POST 合并）
    rate_limit_seconds: float = 3.0
    blocked_indicators: list[str] = field(default_factory=list)
    match_marker: str | None = None
    no_match_marker: str | None = None
    record_pattern: str | None = None
    note: str | None = None


def load_source_config(path: Path | None = None) -> dict[str, SourceConfig]:
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, SourceConfig] = {}
    for sid, item in (data.get("sources") or {}).items():
        out[sid] = SourceConfig(source_id=sid, label=str(item.get("label", sid)), **{
            k: v for k, v in item.items() if k in {
                "query_mode", "endpoint", "portal_url", "requires_credentials",
                "params_name", "params_uscc", "method", "form_fields", "response_type",
                "row_pattern", "field_map", "pages", "page_param",
                "rate_limit_seconds", "blocked_indicators",
                "match_marker", "no_match_marker", "record_pattern", "note",
            }
        })
    return out


# --------------------------------------------------------------------- HTTP 适配器


class HttpSourceAdapter:
    """通用只读 HTTP 查询适配器。

    仅执行一次带超时的 GET 请求；任何需要 POST 模拟、会话保持、验证码交互的
    站点都不适合本适配器，将进入 blocked/needs_manual_review。
    """

    def __init__(
        self,
        config: SourceConfig,
        transport: HttpTransport | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.source_id = config.source_id
        self.version = ADAPTER_VERSION
        self._transport = transport
        self._last_query: float = 0.0
        self._config_path = config_path

    def _get_transport(self) -> HttpTransport:
        if self._transport is not None:
            return self._transport
        import requests

        session = requests.Session()

        def transport(req: HttpRequest) -> tuple[int, str]:
            headers = {"User-Agent": "tender-clearance/0.2 (compliance review; contact=procurement office)",
                       **req.headers}
            if req.method == "POST":
                resp = session.post(
                    req.url, data=req.form, params=req.params or None,
                    timeout=(5, 25), headers=headers,
                )
            else:
                resp = session.get(
                    req.url, params=req.params or None,
                    timeout=(5, 25), headers=headers,
                )
            return resp.status_code, resp.text

        return transport

    def _build_request(self, subject: QuerySubject, page: int) -> HttpRequest:
        cfg = self.config
        url = cfg.endpoint or ""
        get_params: dict[str, str] = {}
        form: dict[str, str] = {}

        def render(v: str) -> str:
            return v.replace("{name}", subject.name or "").replace("{uscc}", subject.uscc or "")

        if cfg.method == "POST":
            for k, v in (cfg.form_fields or {}).items():
                form[k] = render(str(v))
            # 查询参数：有专门 form 字段时用 form，否则挂到 query string
            if cfg.params_uscc and cfg.params_uscc not in form and subject.uscc:
                form[cfg.params_uscc] = subject.uscc
            if cfg.params_name and cfg.params_name not in form and subject.name and not subject.uscc:
                form[cfg.params_name] = subject.name
            if cfg.page_param:
                form[cfg.page_param] = str(page)
        else:
            if subject.uscc and cfg.params_uscc:
                get_params[cfg.params_uscc] = subject.uscc
            elif subject.name and cfg.params_name:
                get_params[cfg.params_name] = subject.name
            if cfg.page_param:
                get_params[cfg.page_param] = str(page)
        return HttpRequest(method=cfg.method, url=url, params=get_params, form=form)

    def query(self, subject: QuerySubject, as_of: datetime) -> AdapterResult:
        cfg = self.config
        if not cfg.endpoint:
            return AdapterResult(
                status="needs_manual_review",
                detail="未配置正式查询入口（endpoint）；请导入授权证据或人工查询后导入",
                request_mode="none",
            )
        if not subject.uscc and not subject.name:
            return AdapterResult(
                status="needs_manual_review",
                detail="缺少主体键（无统一社会信用代码与企业名称），无法查询",
                request_mode="none",
            )
        if not subject.uscc and cfg.params_uscc and not cfg.params_name:
            return AdapterResult(
                status="needs_manual_review",
                detail="该渠道仅支持按统一社会信用代码查询；同名结果不能自动归属，需人工确认",
                request_mode="none",
            )
        transport = self._get_transport()
        records: list[AdapterRecord] = []
        pages_fetched = 0
        pages_exhausted = False
        try:
            for page in range(1, max(1, cfg.pages) + 1):
                # 限速（首页立即，翻页间等待）
                wait = cfg.rate_limit_seconds - (time.monotonic() - self._last_query)
                if wait > 0:
                    time.sleep(min(wait, 30.0))
                req = self._build_request(subject, page)
                try:
                    code, body = transport(req)
                except Exception as exc:  # noqa: BLE001
                    if page == 1:
                        return AdapterResult(
                            status="failed",
                            detail=f"网络请求失败：{type(exc).__name__}: {exc}",
                            request_mode=cfg.query_mode,
                        )
                    pages_exhausted = True  # 后续页失败：保留已取得的记录
                    break
                finally:
                    self._last_query = time.monotonic()
                lowered = body[:20000].lower()
                if code in (401, 403, 302) or any(ind.lower() in lowered for ind in cfg.blocked_indicators):
                    return AdapterResult(
                        status="blocked",
                        detail=f"渠道返回 {code} 或出现人机校验/登录/访问控制特征，已停止自动化查询",
                        request_mode=cfg.query_mode,
                        response_ref=f"HTTP {code}，响应前 200 字节已留存于运行日志",
                    )
                if code != 200:
                    if page == 1:
                        return AdapterResult(
                            status="failed",
                            detail=f"渠道返回非预期状态码 {code}",
                            request_mode=cfg.query_mode,
                        )
                    pages_exhausted = True
                    break
                pages_fetched = page
                page_records = self._parse_body(body, subject)
                if page_records:
                    records.extend(page_records)
                if not page_records:
                    break  # 空页：不再翻页
        except Exception as exc:  # noqa: BLE001 - 分页循环外的意外错误必须显性化
            return AdapterResult(
                status="failed",
                detail=f"适配器执行失败：{type(exc).__name__}: {exc}",
                request_mode=cfg.query_mode,
            )

        if records:
            return AdapterResult(
                status="match",
                records=records,
                parsed_count=len(records),
                request_mode=cfg.query_mode,
                response_ref=f"已抓取 {pages_fetched} 页公开查询结果并逐行解析",
                detail=None if not pages_exhausted else "部分结果页未取得（渠道返回异常），如需完整结果请人工核对",
            )
        if pages_fetched:
            # 首页成功但无记录：区分“明示无匹配”与“不可判定”
            first_page_marker = self._page_has_no_match_marker
            if cfg.no_match_marker and first_page_marker:
                return AdapterResult(
                    status="no_match_verified",
                    detail=f"已成功查询（{pages_fetched} 页），页面明示无匹配记录",
                    request_mode=cfg.query_mode,
                )
            return AdapterResult(
                status="no_result",
                detail="查询成功返回，但页面未提供可机读/可定位的匹配记录，需人工复核页面",
                request_mode=cfg.query_mode,
            )
        return AdapterResult(
            status="needs_manual_review",
            detail="渠道未配置结果解析规则；请人工在浏览器核对并导入证据",
            request_mode=cfg.query_mode,
        )

    # 最近一次解析页面是否含 no_match 标记（供状态判定）
    _page_has_no_match_marker: bool = False

    def _parse_body(self, body: str, subject: QuerySubject) -> list[AdapterRecord]:
        cfg = self.config
        self._page_has_no_match_marker = bool(
            cfg.no_match_marker and re.search(cfg.no_match_marker, body[:20000], re.IGNORECASE)
        )
        out: list[AdapterRecord] = []
        if cfg.response_type == "html_table" and cfg.row_pattern:
            for row_m in re.finditer(cfg.row_pattern, body, re.DOTALL):
                row = row_m.group(1)
                cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
                fields = {name: (cells[pos] if pos < len(cells) else "")
                          for pos, name in enumerate(cfg.field_map or [])}
                fields = {k: v for k, v in fields.items() if v}
                if not fields:
                    continue
                confirmed = bool(subject.uscc) and any(v == subject.uscc for v in fields.values())
                out.append(AdapterRecord(
                    record_kind="dishonesty",
                    fields=fields,
                    subject_confirmation="confirmed" if confirmed else "candidate",
                ))
        elif cfg.record_pattern:
            for m in re.finditer(cfg.record_pattern, body, re.DOTALL):
                fields = {k: (v or "").strip() for k, v in m.groupdict().items()}
                if fields:
                    confirmed = bool(subject.uscc) and any(v == subject.uscc for v in fields.values())
                    out.append(AdapterRecord(
                        record_kind="dishonesty",
                        fields=fields,
                        subject_confirmation="confirmed" if confirmed else "candidate",
                    ))
        return out


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(fragment: str) -> str:
    import html as _html

    text = _TAG_RE.sub("", fragment)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).replace(" ;", ";").strip()


class SrmAdapter:
    """富奥 SRM（用友 YonBIP，https://yonbip.fawer.com.cn/）适配器（P5）。

    两种实时模式互斥（浏览器客户端与 HTTP/token 客户端保持分离）：
    - 浏览器会话（优先）：宿主已通过 tc.srm_browser.register_browser_driver 注册
      驱动工厂 → 委托 SrmBrowserClient（真实浏览器页面交互，request_mode=browser_session）；
    - HTTP/token：未注册浏览器驱动时，按 rules/srm-api.yaml 配置走 SrmClient
      （auth.mode=none 即不发起任何访问）；
    - 凭据一律运行时提供（环境变量/凭据回调），仅存内存；
    - 授权导出文件仍走 external-evidence/srm-authorized-export/ 导入通道。
    """

    source_id = "srm"
    version = ADAPTER_VERSION

    def __init__(self, config: SourceConfig | None = None, client: Any = None) -> None:
        self.config = config
        self._client = client

    def _get_client(self):
        if self._client is None:
            from . import srm_browser as _srm_browser
            from .srm import SrmClient, SrmCredentials, load_srm_config
            import os

            # SRM_BROWSER_DRIVER=playwright 时启用真实浏览器会话模式：
            # 默认无头后台运行（用户目标：只对话、不弹窗）；SRM_BROWSER_HEADED=1
            # 为可选有头模式（人工接管验证码场景）
            if os.environ.get("SRM_BROWSER_DRIVER", "") == "playwright":
                from .srm_playwright_driver import ensure_registered

                ensure_registered(headless=os.environ.get("SRM_BROWSER_HEADED", "") != "1")

            factory = _srm_browser.get_browser_driver_factory()
            if factory is not None:
                # keep_session：一次登录批量查询本项目全部供应商，降低触发风控概率
                self._client = _srm_browser.SrmBrowserClient(
                    driver_factory=factory,
                    credential_provider=(
                        _srm_browser.get_browser_credential_provider()
                        or (lambda: _srm_browser.BrowserCredentials(
                            username=os.environ.get("SRM_USER", ""),
                            password=os.environ.get("SRM_PASSWORD", ""),
                        ))
                    ),
                    keep_session=True,
                )
            else:
                cfg = load_srm_config()
                creds = SrmCredentials(
                    username=os.environ.get("SRM_USER", ""),
                    password=os.environ.get("SRM_PASSWORD", ""),
                )
                self._client = SrmClient(cfg, creds)
        return self._client

    def query(self, subject: QuerySubject, as_of: datetime) -> AdapterResult:
        client = self._get_client()
        try:
            result = client.query(subject, as_of)
        except Exception as exc:  # noqa: BLE001 - 适配器异常必须显性化且不带凭据
            return AdapterResult(
                status="failed",
                detail=f"SRM 适配器执行失败：{type(exc).__name__}: {exc}",
                request_mode="official_api",
            )
        # 结果里的适配器版本随模式区分（browser_session vs HTTP/token）
        self.version = getattr(client, "version", ADAPTER_VERSION)
        return result

    def close(self) -> None:
        """关闭浏览器会话；HTTP/token 客户端无状态时为空操作。"""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class BrowserSrmAdapter:
    """SRM 浏览器会话适配器的 SourceAdapter 外壳。

    ``client`` 必须是 ``tc.srm_browser.SrmBrowserClient`` 或具有同等 query 接口
    的宿主实现。浏览器句柄由 Claude/Codex 注入；本类不创建 HTTP transport。
    """

    source_id = "srm"
    version = "srm-browser/0.1.0"

    def __init__(self, client: Any) -> None:
        self._client = client

    def query(self, subject: QuerySubject, as_of: datetime) -> AdapterResult:
        try:
            return self._client.query(subject, as_of)
        except Exception as exc:  # noqa: BLE001 - 不泄漏宿主凭据
            return AdapterResult(
                status="failed",
                detail=f"SRM 浏览器适配器执行失败：{type(exc).__name__}",
                request_mode="browser_session",
            )

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def build_adapters(
    configs: dict[str, SourceConfig],
    transport_factory: Callable[[SourceConfig], HttpTransport | None] | None = None,
) -> dict[str, SourceAdapter]:
    out: dict[str, SourceAdapter] = {}
    for sid, cfg in configs.items():
        if sid == "srm":
            out[sid] = SrmAdapter(cfg)
        else:
            transport = transport_factory(cfg) if transport_factory else None
            out[sid] = HttpSourceAdapter(cfg, transport=transport)
    return out
