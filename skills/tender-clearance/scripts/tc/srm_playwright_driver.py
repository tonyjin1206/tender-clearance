"""Playwright 实现的 SRM 浏览器驱动（SrmBrowserDriver 宿主桥接）。

边界（与《SRM浏览器会话集成.md》一致）：
- 只做真实浏览器页面交互（打开页面、输入运行时凭据、按可见文字定位导航、
  读取可见 DOM）；不调用 requests/curl/fetch，不复现 XHR；
- Cookie 存活于浏览器进程内存（非持久化 context），close 即销毁；
- 登录页出现图形验证码 / 二次验证 → 返回 manual（需要人工接管），不绕过、
  不重试规避；密码错误 → blocked；
- 密码摘要由登录页自身 JS 完成（本驱动只做常规页面输入与提交）。

登录前 DOM（#username/#password/#submit_btn_login、验证码 #inputCode）已于
2026-09-09 在真实登录页核实；登录后的导航（主页 → 查企业 → 搜索框 → 唯一命中 →
企业详情/企业画像 → 基本信息/司法风险/经营风险）按真实页面可见文字实现，
**待一次真实凭据联调验证**；元素定位失败时如实返回 unconfirmed/needs_manual_review。
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal

from .srm_browser import (
    BrowserCredentials,
    BrowserLoginResult,
    BrowserSectionResult,
    BrowserSubjectResult,
    SrmBrowserClient,
    SrmBrowserDriver,
    register_browser_driver,
)
from .sources import AdapterResult, QuerySubject

Portal = "https://yonbip.fawer.com.cn/"

_LOGIN_ERROR_MARKERS = ("密码错误", "用户名或密码", "账号被锁", "已被锁定", "不存在", "登录失败")

# 集成文档 §6 的风险分类白名单（用于从页面文本中识别分类计数）
_JUDICIAL_CATEGORIES = (
    "法律诉讼", "历史被执行人", "法院公告", "开庭公告", "失信被执行人",
    "被执行人", "股权冻结", "限制高消费", "终本案件",
)
_OPERATING_CATEGORIES = (
    "经营异常", "行政处罚", "行政处罚(信用中国)", "严重违法", "欠税公告",
    "动产抵押", "股权出质", "司法拍卖", "清算信息", "知识产权出质",
    "税收违法", "环保处罚",
)
_BASIC_COUNT_KEYS = ("股东", "工商股东", "对外投资", "主要人员", "变更记录", "分支机构")


def _parse_categories(text: str, whitelist: tuple[str, ...]) -> tuple[dict[str, int], list[str]]:
    """从画像页可见文本解析分类计数：『分类名 (数字)』→ counts；白名单中无数字者 → disabled。"""
    counts: dict[str, int] = {}
    disabled: list[str] = []
    for name in whitelist:
        m = re.search(re.escape(name) + r"\s*[（(]\s*(\d+)\s*[）)]", text)
        if m:
            counts[name] = int(m.group(1))
        else:
            disabled.append(name)
    return counts, disabled


def _norm(v: str | None) -> str:
    if not v:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(v)))


def _clean_company_name(value: str | None) -> str:
    """清理文书版面噪声，保留完整法定主体名称。

    只移除末尾的“(公章)”标记；“分公司”“支公司”等均是主体组成，禁止
    用通用后缀清理把它们抹掉。
    """
    if not value:
        return ""
    name = unicodedata.normalize("NFKC", str(value)).strip()
    name = re.sub(r"\s*[（(]\s*公章\s*[）)]\s*$", "", name)
    return re.sub(r"\s+", "", name).strip()


def _company_lookup_query(subject: QuerySubject) -> str:
    """生成 SRM 查企业输入值：准确全称优先，信用代码只作二次核验。"""
    return _clean_company_name(subject.name) or str(subject.uscc or "").strip().upper()


_COMPANY_NAME_KEYS = (
    "企业名称", "公司名称", "单位名称", "主体名称", "企业", "公司", "名称",
)
_COMPANY_USCC_KEYS = (
    "统一社会信用代码", "社会信用代码", "信用代码", "统一信用代码", "注册号",
)
_USCC_RE = re.compile(r"[0-9A-HJ-NPQRTUWXY]{18}", re.I)


def _first_record_value(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按表头优先取值；兼容 SRM 表头带换行、空格或括号的情况。"""
    normalized = {_norm(str(k)): str(v or "").strip() for k, v in record.items()}
    for key in keys:
        value = normalized.get(_norm(key), "")
        if value and value not in {"-", "--", "暂无", "无"}:
            return value
    for raw_key, raw_value in record.items():
        key = _norm(str(raw_key))
        if any(_norm(alias) in key for alias in keys):
            value = str(raw_value or "").strip()
            if value and value not in {"-", "--", "暂无", "无"}:
                return value
    return ""


def _normalize_company_candidates(raw_rows: list[dict[str, Any]], query: str = "") -> list[dict[str, str]]:
    """把“查企业”结果表转换为可核对的候选主体，不从空行或操作列猜主体。"""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    query_norm = _norm(query)
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        uscc = _first_record_value(raw, _COMPANY_USCC_KEYS)
        uscc_match = _USCC_RE.search(uscc)
        uscc = uscc_match.group(0).upper() if uscc_match else ""
        name = _clean_company_name(_first_record_value(raw, _COMPANY_NAME_KEYS))
        # 没有明确企业名称列时不从法人、地址、操作列猜公司名；有信用代码的
        # 行仍可用于按 USCC 精确选择，但不能伪造一个完整名称。
        if _USCC_RE.fullmatch(name):
            name = ""
        if not name and not uscc:
            continue
        if query_norm and len(query_norm) >= 3:
            candidate_text = _norm(name) + uscc
            if query_norm not in candidate_text and query_norm[:4] not in candidate_text:
                continue
        key = (_norm(name), uscc)
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "uscc": uscc})
    return result


def _select_company_candidate(
    rows: list[dict[str, str]], query: str, subject_uscc: str | None = None
) -> dict[str, str] | None:
    """在“查企业”结果中选择可证明属于查询主体的唯一卡片。

    SRM 会把同名企业的分支机构一起返回。只要存在一个企业名称完全等于
    查询名称的卡片，就不应因为其它“名称+分公司”的卡片而整体判为歧义；
    但多个完全同名主体或多个相同信用代码仍必须人工复核。
    """
    query_norm = _norm(_clean_company_name(query))
    exact_name = [r for r in rows if _norm(_clean_company_name(r.get("name"))) == query_norm]
    if len(exact_name) == 1:
        selected = exact_name[0]
        # 准确名称是第一主体键；只有当 SRM 行明确带出代码时，才用代码
        # 做冲突校验。代码缺失不把一个唯一准确名称误判成“无结果”。
        if subject_uscc and selected.get("uscc") and str(selected["uscc"]).upper() != subject_uscc.upper():
            return None
        return selected
    if subject_uscc:
        exact_uscc = [r for r in rows if str(r.get("uscc") or "").upper() == subject_uscc.upper()]
        if len(exact_uscc) == 1 and not exact_name:
            return exact_uscc[0]
    return None


def _is_branch_name(name: str | None) -> bool:
    return bool(re.search(r"(?:分公司|支公司)$", _clean_company_name(name)))


def _branch_candidates(
    rows: list[dict[str, str]], query: str, selected: dict[str, str] | None,
) -> list[dict[str, str]]:
    """识别与精确主体同名族的分公司，仅作为 branch_candidate 线索。"""
    query_norm = _norm(_clean_company_name(query))
    selected_key = (
        _norm(_clean_company_name(selected.get("name"))) if selected else "",
        str(selected.get("uscc") or "").upper() if selected else "",
    )
    out: list[dict[str, str]] = []
    for row in rows:
        name = _clean_company_name(row.get("name"))
        code = str(row.get("uscc") or "").upper()
        if not _is_branch_name(name):
            continue
        if selected and (_norm(name), code) == selected_key:
            continue
        # 只有明确包含查询主体全名的分公司才建立关系，不把相似名称自动归属。
        if query_norm and query_norm in _norm(name):
            out.append({"name": name, "uscc": code, "relation": "branch_candidate"})
    return out


class PlaywrightSrmDriver:
    """基于 Playwright 的真实浏览器驱动（同步 API）。"""

    source_id = "srm"
    version = "srm-browser-playwright/0.1.0"

    def __init__(self, headless: bool = False, timeout_ms: int = 45000,
                 manual_takeover_wait_s: int = 0, stable_wait_ms: int = 1200) -> None:
        self._headless = headless
        self._timeout = timeout_ms
        self._takeover_wait = manual_takeover_wait_s
        self._stable_wait_ms = max(0, int(stable_wait_ms))
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._active_profile_frame = None
        # 同一批次复用会话时，查企业页面可能已保持在当前标签。
        # 只复用查企业搜索面，不复用旧企业画像结果。
        self._company_search_open = False

    # ------------------------------------------------------------ 驱动生命周期

    def open(self, portal_url: str = Portal) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(channel="chrome", headless=self._headless)
        except Exception:  # noqa: BLE001 - 无本机 Chrome 时用随包 Chromium
            self._browser = self._pw.chromium.launch(headless=self._headless)
        # 非持久化 context：Cookie 仅存活于进程内存，close 即销毁
        self._context = self._browser.new_context(locale="zh-CN")
        self._page = self._context.new_page()
        self._page.goto(portal_url, timeout=self._timeout, wait_until="domcontentloaded")

    def _cas_frame(self):
        """等待并返回 CAS 登录 iframe（登录表单所在）。"""
        deadline = time.monotonic() + self._timeout / 1000
        while time.monotonic() < deadline:
            for frame in self._page.frames:
                if "/cas/login" in frame.url:
                    try:
                        if frame.locator("#username").count():
                            return frame
                    except Exception:  # noqa: BLE001
                        pass
            self._page.wait_for_timeout(500)
        return None

    def _logged_in_hint(self) -> bool:
        try:
            text = self._page.evaluate(
                "document.body.innerText.replace(/\\s+/g,' ').slice(0,500)")
            return any(k in text for k in ("工作台", "首页", "查企业", "应用中心"))
        except Exception:  # noqa: BLE001
            return False

    def login(self, username: str, password: str) -> BrowserLoginResult:
        cas = self._cas_frame()
        if cas is None:
            if self._logged_in_hint():
                return BrowserLoginResult(status="authenticated",
                                          detail="未出现登录页，使用浏览器已有会话")
            return BrowserLoginResult(status="failed", detail="未找到 SRM 登录页（CAS iframe 未出现）")

        # 图形验证码可见 → 需人工接管（不绕过、不重试）
        try:
            code_loc = cas.locator("#inputCode")
            if code_loc.count() and code_loc.is_visible():
                return BrowserLoginResult(
                    status="manual",
                    detail="登录页需要图形验证码：请在可见浏览器窗口人工完成登录后重试",
                )
        except Exception:  # noqa: BLE001
            pass

        cas.locator("#username").fill(username)
        cas.locator("#password").fill(password)
        cas.locator("#submit_btn_login").click()

        # 轮询判定：跳转成功 / 出验证码 / 报错。SRM 认证成功后的工作台
        # 偶发需要 20~30 秒才把登录 iframe 移除；固定 15 秒会把慢加载误报为
        # manual。上限 45 秒，成功即返回，不影响正常速度。
        deadline = time.monotonic() + min(45, max(15, self._timeout / 1000))
        while time.monotonic() < deadline:
            if not any("/cas/login" in f.url for f in self._page.frames) or self._logged_in_hint():
                self._page.wait_for_timeout(4000)  # 工作台渲染时间
                return BrowserLoginResult(status="authenticated")
            try:
                if cas.locator("#inputCode").count() and cas.locator("#inputCode").is_visible():
                    # 等待人工在有头浏览器窗口中完成验证码登录（不绕过、不自动重试）
                    if not self._headless and self._takeover_wait > 0:
                        takeover_deadline = time.monotonic() + self._takeover_wait
                        while time.monotonic() < takeover_deadline:
                            if not any("/cas/login" in f.url for f in self._page.frames) \
                                    or self._logged_in_hint():
                                self._page.wait_for_timeout(4000)
                                return BrowserLoginResult(status="authenticated",
                                                          detail="验证码由人工在浏览器窗口完成")
                            cas.page.wait_for_timeout(2000)
                    return BrowserLoginResult(
                        status="manual",
                        detail="登录触发图形验证码：请使用有头模式运行并在浏览器窗口人工完成登录")
            except Exception:  # noqa: BLE001
                pass
            try:
                body_text = cas.evaluate(
                    "document.body.innerText.replace(/\\s+/g,' ').slice(0,200)")
                hit = next((m for m in _LOGIN_ERROR_MARKERS if m in body_text), None)
                if hit:
                    return BrowserLoginResult(status="blocked", detail=f"SRM 登录被拒绝（{hit}）")
            except Exception:  # noqa: BLE001
                pass
            cas.page.wait_for_timeout(1000)
        return BrowserLoginResult(status="manual", detail="登录未完成（可能触发验证码或网络缓慢），需人工接管")

    def wait_for_manual_login(self, timeout_s: int) -> BrowserLoginResult:
        """等待用户在可见浏览器中完成登录；绝不读取或填充账号密码。"""
        if self._page is None:
            return BrowserLoginResult(status="failed", detail="浏览器页面未创建")
        deadline = time.monotonic() + max(1, int(timeout_s))
        while time.monotonic() < deadline:
            try:
                if self._logged_in_hint():
                    return BrowserLoginResult(status="authenticated", detail="检测到人工登录成功")
                # 页面/上下文被用户误关时，交给客户端执行一次有限重开。
                if self._page.is_closed():
                    return BrowserLoginResult(status="failed", detail="浏览器页面已关闭")
                self._page.wait_for_timeout(500)
            except Exception as exc:  # noqa: BLE001
                return BrowserLoginResult(status="failed", detail=f"浏览器已关闭或断开：{type(exc).__name__}")
        return BrowserLoginResult(
            status="manual",
            detail=f"人工登录等待超时（{max(1, int(timeout_s))} 秒）；未继续执行主体查询",
        )

    # ------------------------------------------------------------ 主体检索与核对
    # 以下导航顺序基于已观察到的 SRM 主页：
    # - 主页最近应用中有“查企业”，直接进入企业搜索，而不是供应商档案；
    # - 搜索结果必须形成唯一企业候选，随后才允许进入企业详情/画像；
    # - 企业画像在独立 iframe（URL 含 /intellid/portrait/）；
    # - 表格为表头/表体分离渲染；分类计数格式为“分类名 (数字)”。

    def search_subject(self, subject: QuerySubject) -> BrowserSubjectResult:
        """主页 → 查企业 → 搜索框 → 唯一命中 → 企业详情/画像主体核对。"""
        query = _company_lookup_query(subject)
        # 查询框优先使用准确公司全称；统一社会信用代码只用于结果核验。
        name_query = _clean_company_name(subject.name) or query

        # 批量查询中，上一家供应商的画像 iframe/页签可能仍覆盖在查企业
        # 搜索页上。轻量刷新当前已认证页面，保留 Cookie 但清掉旧画像，
        # 避免下一家被旧 iframe 或旧分类页吞掉；首次查询不触发刷新。
        if self._active_profile_frame is not None:
            try:
                self._page.reload(timeout=self._timeout, wait_until="domcontentloaded")
                self._page.wait_for_timeout(2500)
            except Exception:  # noqa: BLE001
                pass
            self._active_profile_frame = None
            self._company_search_open = False

        if not self._ensure_company_search_open():
            self._dump_debug_page("company-entry")
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail="未能从 SRM 主页打开『查企业』搜索（页面结构已存 /tmp/srm-nav-debug-*.json 供诊断）")

        # 直接使用“查企业”搜索框，不打开旧的档案高级查询面板。
        filled = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._fill_company_search(query):
                filled = True
                break
            self._page.wait_for_timeout(800)
        if not filled:
            self._dump_debug_page("company-search")
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail="未能在『查企业』页面找到搜索框或提交查询（页面结构已存 /tmp/srm-nav-debug-*.json 供诊断）")
        # 结果行异步渲染：轮询等待（最多 12 秒），只采集有企业名称/信用代码的行。
        rows: list[dict[str, str]] = []
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self._page.wait_for_timeout(800)
            rows = self._company_result_rows(query)
            if rows:
                break
        if len(rows) == 0:
            self._dump_debug_page("company-result")
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail="『查企业』查询结果为 0 条（页面结构已存 /tmp/srm-nav-debug-company-result.json 供诊断）")
        selected = _select_company_candidate(rows, name_query, subject.uscc)
        if selected is None:
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail=f"『查企业』命中 {len(rows)} 条企业，未找到唯一精确主体；需人工确认企业")
        branch_candidates = _branch_candidates(rows, name_query, selected)

        # 精确命中：点击企业结果 → 企业详情/画像 → 等待画像 iframe。
        self._active_profile_frame = None
        if not self._click_company_result(selected):
            return BrowserSubjectResult(None, None, "unconfirmed",
                                        detail="唯一企业结果无法点击进入详情，需人工核对")
        self._page.wait_for_timeout(2500)
        self._click_anywhere(["企业详情", "企业画像", "更全面企业信息"], exact=True)
        # 会话批量查询会保留旧的 portrait iframe；必须等待当前查询主体的
        # 名称/信用代码出现在同一个 iframe，不能只看到一个旧的“企业名称”就返回。
        pf = self._wait_profile_ready(query)
        if pf is None:
            return BrowserSubjectResult(None, None, "unconfirmed",
                                        detail="未进入企业画像页（portrait iframe 未出现或未加载），需人工核对")
        self._active_profile_frame = pf

        name, uscc = self._read_identity(pf)
        expected_name = _clean_company_name(subject.name)
        expected_uscc = str(subject.uscc or "").upper()
        actual_name = _clean_company_name(name)
        # 有两个主体键时必须同时一致；只一致一个键也属于冲突，不能自动
        # match。名称查询没有 USCC 时只能是 candidate，不能升级为 confirmed。
        if expected_uscc and uscc and uscc.upper() != expected_uscc:
            return BrowserSubjectResult(
                name, uscc, "unconfirmed", branch_candidates=branch_candidates,
                profile_ref=f"企业画像（名称={name}，信用代码={uscc}）",
                detail="企业画像统一社会信用代码与查询主体冲突，需人工复核")
        if expected_name and actual_name and actual_name != _clean_company_name(expected_name):
            return BrowserSubjectResult(
                name, uscc, "unconfirmed", branch_candidates=branch_candidates,
                profile_ref=f"企业画像（名称={name}，信用代码={uscc}）",
                detail="企业画像企业名称与查询主体冲突，需人工复核")
        if expected_uscc and uscc and uscc.upper() == expected_uscc and (
            not expected_name or actual_name == _norm(expected_name)
        ):
            confirmation: Literal["confirmed", "candidate"] = "confirmed"
        elif expected_name and actual_name == _clean_company_name(expected_name) and not expected_uscc:
            confirmation = "candidate"
        else:
            return BrowserSubjectResult(
                name, uscc, "unconfirmed", branch_candidates=branch_candidates,
                profile_ref=f"企业画像（名称={name}，信用代码={uscc}）",
                detail="企业画像主体与查询主体不一致，停止读取风险结果")
        return BrowserSubjectResult(
            name, uscc, confirmation,
            profile_ref=f"企业画像（名称={name}，信用代码={uscc}）",
            detail=f"入口=主页>查企业；精确命中=名称:{selected.get('name') or '-'}，信用代码:{selected.get('uscc') or '-'}；候选总数={len(rows)}；分公司候选={len(branch_candidates)}",
            branch_candidates=branch_candidates,
        )

    def _wait_profile_frame(self, timeout_ms: int | None = None):
        if self._active_profile_frame is not None:
            try:
                if "/intellid/portrait/" in self._active_profile_frame.url:
                    return self._active_profile_frame
            except Exception:  # noqa: BLE001
                self._active_profile_frame = None
        deadline = time.monotonic() + (timeout_ms or self._timeout) / 1000
        while time.monotonic() < deadline:
            all_frames = [f for pg in self._context.pages for f in pg.frames]
            for frame in all_frames:
                if "/intellid/portrait/" in frame.url:
                    try:
                        frame.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                    return frame
            self._page.wait_for_timeout(500)
        return None

    _FULL_TEXT_JS = """
        () => {
          const parts = [];
          try { parts.push(document.body ? document.body.innerText : ''); } catch (e) {}
          const walk = (root) => {
            let els = [];
            try { els = root.querySelectorAll('*'); } catch (e) { return; }
            for (const e of els) {
              if (e.shadowRoot) {
                try { parts.push(e.shadowRoot.textContent || ''); } catch (e2) {}
                walk(e.shadowRoot);
              }
            }
          };
          try { walk(document); } catch (e) {}
          return parts.join('\n');
        }
    """

    def _full_text(self, frame) -> str:
        try:
            text = frame.evaluate(self._FULL_TEXT_JS)
            if text:
                return text
        except Exception:  # noqa: BLE001
            pass
        try:
            return frame.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _wait_profile_ready(self, expected_query: str | None = None, timeout_ms: int | None = None):
        """等待当前主体的画像 iframe 出现且内容加载。"""
        deadline = time.monotonic() + (timeout_ms or 20000) / 1000
        while time.monotonic() < deadline:
            all_frames = [f for pg in self._context.pages for f in pg.frames]
            for frame in all_frames:
                if "/intellid/portrait/" not in frame.url:
                    continue
                try:
                    text = self._full_text(frame)
                except Exception:  # noqa: BLE001
                    continue
                if not (("企业名称" in text) or ("统一社会信用代码" in text)):
                    continue
                if expected_query:
                    compact_text = _norm(text)
                    if re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", expected_query, re.I):
                        if expected_query.upper() not in compact_text.upper():
                            continue
                    elif _norm(expected_query) not in compact_text:
                        # 名称查询时先等待当前结果真正替换旧 iframe。
                        continue
                    # 画像 iframe 会先渲染旧主体/空骨架，再替换为当前企业。
                    # 只在主体身份连续稳定一段时间后继续，避免读到上一家结果。
                    stable = self._wait_for_stable_identity(
                        frame, expected_query, self._stable_wait_ms
                    )
                    if stable:
                        return frame
            self._page.wait_for_timeout(1000)
        return None

    def _wait_for_stable_identity(
        self, frame, expected_query: str | None, stable_wait_ms: int,
    ) -> bool:
        """确认画像主体身份在短窗口内保持不变。

        稳定键只包含名称/统一社会信用代码，不保存页面全文；名称查询仍要求
        完整名称精确相等，模糊命中不会因等待结束而升级为自动匹配。
        """
        deadline = time.monotonic() + max(0, stable_wait_ms) / 1000
        previous: tuple[str, str] | None = None
        while True:
            name, uscc = self._read_identity(frame)
            key = (_norm(_clean_company_name(name)), str(uscc or "").upper())
            if expected_query:
                if _USCC_RE.fullmatch(expected_query):
                    if key[1] != expected_query.upper():
                        return False
                elif key[0] != _norm(_clean_company_name(expected_query)):
                    return False
            if key != ("", "") and key == previous:
                if time.monotonic() >= deadline:
                    return True
            else:
                previous = key
            if time.monotonic() >= deadline:
                return stable_wait_ms == 0 and key != ("", "")
            frame.page.wait_for_timeout(min(250, max(1, stable_wait_ms)))

    def _read_identity(self, pf) -> tuple[str | None, str | None]:
        """兼容标签/值同一行或分行的画像文本，读取主体身份。"""
        text = self._full_text(pf)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        uscc = None
        uscc_idx = None
        for i, ln in enumerate(lines):
            inline = re.search(r"统一社会信用代码\s*[:：]?\s*([0-9A-HJ-NPQRTUWXY]{18})", ln, re.I)
            if inline:
                uscc = inline.group(1).upper()
                uscc_idx = i
                break
            if ln.rstrip(":：") == "统一社会信用代码" and i + 1 < len(lines):
                hit = re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", lines[i + 1], re.I)
                if hit:
                    uscc = hit.group(0).upper()
                    uscc_idx = i + 1
                    break
        name = None
        search_lines = lines[:uscc_idx + 1] if uscc_idx is not None else lines[:80]
        for i, ln in enumerate(search_lines):
            inline = re.search(r"企业名称\s*[:：]\s*(.{2,60})", ln)
            if inline:
                name = inline.group(1).strip()
            elif ln.rstrip(":：") == "企业名称" and i + 1 < len(search_lines):
                cand = search_lines[i + 1]
                if 2 <= len(cand) <= 60 and cand != "统一社会信用代码":
                    name = cand
        # 页面可能只把企业名称放在顶部且信用代码字段隐藏，取第一个明确的
        # 企业名称值；不从股东/对外投资表格的任意公司名猜主体。
        return name, uscc

    # ------------------------------------------------------------ 页面读取

    def read_section(self, section: Literal["basic", "shareholders", "branches", "personnel", "judicial", "operating"]) -> BrowserSectionResult:
        pf = self._wait_profile_frame(10000)
        if pf is None:
            return BrowserSectionResult(section=section, structured=False,
                                        detail="企业画像 iframe 未出现，需人工核对")
        if section == "basic":
            return self._read_basic(pf)
        if section in {"shareholders", "branches", "personnel"}:
            labels = {
                "shareholders": ["股东信息", "股东", "工商股东"],
                "branches": ["分支机构"],
                "personnel": ["主要人员"],
            }[section]
            header_groups = {
                # SRM 的股东区可能同时有“发起人/股东”和“工商股东”两张表，
                # 两者都属于股东记录；不能用全页第一张表兜底。
                "shareholders": (("公司名称或股东名称",), ("发起人名称", "发起人类型")),
                "branches": (("分支机构",),),
                "personnel": (("姓名", "职位"),),
            }[section]
            clicked = self._click_in_frame(pf, labels)
            if clicked:
                pf.page.wait_for_timeout(1200)
            records = self._frame_table_rows(pf, header_groups)
            if not clicked and not records:
                return BrowserSectionResult(section=section, structured=False,
                                            detail=f"画像页未找到『{labels[0]}』入口，需人工核对")
            return BrowserSectionResult(
                section=section,
                records=records,
                evidence_ref=f"企业画像 > {labels[0]}",
                structured=bool(records),
                detail=None if records else f"{labels[0]}页面无可机读记录",
            )
        tab = "司法风险" if section == "judicial" else "经营风险"
        categories = _JUDICIAL_CATEGORIES if section == "judicial" else _OPERATING_CATEGORIES
        if not self._click_in_frame(pf, [tab]):
            return BrowserSectionResult(section=section, structured=False,
                                        detail=f"画像页未找到『{tab}』页签，需人工核对")
        pf.page.wait_for_timeout(3000)
        text = self._full_text(pf)
        counts, disabled = _parse_categories(text, categories)
        records = self._frame_table_rows(pf)
        structured = bool(counts or disabled or records)
        return BrowserSectionResult(
            section=section,
            counts=counts,
            records=records,
            disabled_categories=disabled,
            evidence_ref=f"企业画像 > {tab}",
            structured=structured,
            detail=("页面分类全部禁用或无可见记录，不解释为无风险" if not counts and not records else None),
        )

    def _read_basic(self, pf) -> BrowserSectionResult:
        fields: dict[str, Any] = {}
        text = self._full_text(pf)
        # 按真实画像字段提取（label 与 value 为相邻可见文本行）
        label_aliases = {
            "企业名称": "企业名称",
            "统一社会信用代码": "统一社会信用代码",
            "法定代表人身份证号": "法定代表人身份证号",
            "法人代表身份证号": "法人代表身份证号",
            "法人身份证号": "法人身份证号",
            "法定代表人证件号": "法定代表人证件号",
            "法定代表人": "法定代表人",
            "法人代表": "法人代表",
            "注册资本": "注册资本",
            "实缴资本": "实缴资本",
            "成立日期": "成立日期",
            "企业状态": "企业状态",
            "企业类型": "企业类型",
            "注册地址": "注册地址",
            "所属行业": "所属行业",
            "人员规模": "人员规模",
            "参保人数": "参保人数",
            "曾用名": "曾用名",
            "注册号": "注册号",
        }
        # 长标签优先，避免“法定代表人”截断“法定代表人身份证号”。
        labels = sorted(label_aliases, key=len, reverse=True)
        keys = tuple(label_aliases)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            matched_label = next((label for label in labels if ln.startswith(label)), None)
            if matched_label is None:
                continue
            canonical = label_aliases[matched_label]
            inline = ln[len(matched_label):].lstrip(" ：:")
            if inline and canonical not in fields:
                fields[canonical] = inline[:80]
                continue
            if canonical in fields or i + 1 >= len(lines):
                continue
            v = lines[i + 1].strip()
            if v and v not in keys and not any(v.startswith(label) for label in labels):
                fields[canonical] = v[:80]
        for key in _BASIC_COUNT_KEYS:
            m = re.search(re.escape(key) + r"\s*[（(](\d+)[）)]", text)
            if m:
                fields[f"{key}入口计数"] = int(m.group(1))
        return BrowserSectionResult(
            section="basic", fields=fields,
            evidence_ref="企业画像 > 基本信息",
            structured=bool(fields),
            detail=None if fields else "基本信息页未见可机读字段，需人工核对",
        )

    _TABLE_PAIR_JS = """
        (expectedGroups) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none'
              && s.visibility !== 'hidden' && s.opacity !== '0';
          };
          const tables = [...document.querySelectorAll('table')].filter(visible);
          const headSets = [];
          tables.forEach(t => {
            const ths = [...t.querySelectorAll('thead th')].map(th => (th.innerText||'').trim());
            if (ths.filter(Boolean).length >= 2) headSets.push(ths);
          });
          const norm = (value) => String(value || '').replace(/\\s+/g, '');
          const matchesGroup = (heads, group) => group.every(expected =>
            heads.some(head => norm(head).includes(norm(expected)))
          );
          const scopedHeads = expectedGroups && expectedGroups.length
            ? headSets.filter(heads => expectedGroups.some(group => matchesGroup(heads, group)))
            : headSets;
          const out = [];
          tables.forEach(t => {
            if (!visible(t)) return;
            [...t.querySelectorAll('tbody tr')].forEach(tr => {
              if (!visible(tr)) return;
              const cells = [...tr.querySelectorAll('td')].map(td => (td.innerText||'').trim());
              if (cells.length < 2 || !cells.some(c => c)) return;
              // 列数一致且属于目标栏目的一组表头才允许配对；没有目标表头
              // 时直接跳过，禁止把别的栏目或变更记录当成当前栏目。
              const heads = scopedHeads.find(hs => hs.length === cells.length);
              if (expectedGroups && expectedGroups.length && !heads) return;
              const rec = {};
              cells.forEach((c, i) => {
                const key = heads[i] && heads[i] !== '操作' ? heads[i] : (heads[i] || `列${i+1}`);
                rec[key] = c;
              });
              if (Object.values(rec).some(v => v)) out.push(rec);
            });
          });
          return out;
        }
    """

    def _frame_table_rows(self, pf, expected_groups: tuple[tuple[str, ...], ...] = ()) -> list[dict[str, str]]:
        """读取画像 iframe 内的数据行：逐表配对表头（处理表头/表体分离渲染）。"""
        try:
            return pf.evaluate(self._TABLE_PAIR_JS, [list(group) for group in expected_groups]) or []
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------ 通用定位辅助

    def _all_frames(self) -> list:
        pages = []
        if self._page is not None:
            pages.append(self._page)
        if self._context is not None:
            pages.extend(p for p in self._context.pages if p not in pages)
        frames = []
        for page in pages:
            try:
                frames.extend(page.frames)
            except Exception:  # noqa: BLE001
                continue
        return frames

    def _company_search_input(self):
        """定位“查企业”搜索框，排除旧供应商档案和工作台服务搜索框。"""
        selectors = (
            # “查企业”实际使用的搜索框：不同版本分别暴露为 ykj-search
            # 或 keyword/placeholder=请输入企业名称、请输入关键字。
            # 必须优先于工作台的服务搜索框。
            'input#ykj-search',
            'input#keyword',
            'input[placeholder="请输入企业名称"]',
            'input[type="search"][placeholder="请输入关键字"]',
            'input[placeholder="请输入关键字"]',
            'input[placeholder*="企业"]',
            'input[placeholder*="公司"]',
            'input[placeholder*="统一社会信用代码"]',
            'input[placeholder*="关键字"]',
            'input[placeholder*="关键词"]',
            'input[placeholder*="搜索"]',
            'input[aria-label*="企业"]',
            'input[type="text"]',
        )
        for frame in self._all_frames():
            for selector in selectors:
                try:
                    for loc in frame.locator(selector).all():
                        if not loc.is_visible():
                            continue
                        attrs = loc.evaluate("""el => ({
                            id: el.id || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            aria: el.getAttribute('aria-label') || '',
                            fieldid: el.getAttribute('fieldid') || ''
                        })""") or {}
                        marker = " ".join(str(attrs.get(k, "")) for k in ("id", "placeholder", "aria", "fieldid"))
                        # aa_vendorlist|children|search 是“查企业”页面的输入框，
                        # 不能再按旧供应商档案规则排除；仅排除工作台服务搜索。
                        if "搜索服务名称" in marker or attrs.get("placeholder") == "搜索" \
                                or attrs.get("fieldid") == "workbench-search":
                            continue
                        return frame, loc
                except Exception:  # noqa: BLE001
                    continue
        return None, None

    def _ensure_company_search_open(self) -> bool:
        """从 SRM 主页直接打开“查企业”，不复用供应商档案入口。"""
        if self._company_search_open and self._company_search_input()[1] is not None:
            self._company_search_open = True
            return True
        self._company_search_open = False

        # 上一家公司可能把当前页留在画像详情；先回到门户主页，保留登录 Cookie。
        try:
            home_text = self._full_text(self._page.main_frame)
        except Exception:  # noqa: BLE001
            home_text = ""
        if "查企业" not in home_text:
            try:
                self._page.goto(Portal, timeout=self._timeout, wait_until="domcontentloaded")
                self._page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass

        opened = self._click_anywhere(["查企业"], exact=True)
        if not opened:
            # 主页最近应用偶发尚未完成渲染；只允许从主页应用搜索“查企业”兜底。
            opened = self._open_via_app_search("查企业")
        if not opened:
            return False

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._company_search_input()[1] is not None:
                self._company_search_open = True
                return True
            self._page.wait_for_timeout(500)
        return False

    def _fill_company_search(self, value: str) -> bool:
        """填写“查企业”搜索框并提交，输入为空时不触发查询。"""
        if not value.strip():
            return False
        frame, inp = self._company_search_input()
        if frame is None or inp is None:
            return False
        try:
            inp.click()
            inp.fill(value)
            # 当前真实页面按钮文案是“查一下”，不是“搜索/查询”。
            for label in ("查一下", "查询", "搜索"):
                for loc in (frame.get_by_role("button", name=label, exact=True),
                            frame.get_by_text(label, exact=True)):
                    for button in loc.all():
                        if button.is_visible():
                            button.click(timeout=5000)
                            return True
            # 兼容按钮仅有 class/type、无稳定可访问名称的版本。
            for button in frame.locator('button.ep-search-btn, button[type="submit"]').all():
                try:
                    if button.is_visible():
                        button.click(timeout=5000)
                        return True
                except Exception:  # noqa: BLE001
                    continue
            # 当前真实页面没有“搜索/查询”文字按钮，输入框右侧只有放大镜图标。
            # 逐级在输入框容器内寻找可见搜索控件并点击，覆盖 button、role=button
            # 以及图标元素，避免只 press Enter 而停留在“暂无相关内容”。
            clicked_icon = inp.evaluate(
                """
                (input) => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none'
                      && s.visibility !== 'hidden' && s.opacity !== '0';
                  };
                  let root = input.parentElement;
                  for (let depth = 0; root && depth < 5; depth++, root = root.parentElement) {
                    const candidates = [...root.querySelectorAll(
                      'button, [role="button"], [aria-label*="搜索"], [title*="搜索"], '
                      '[class*="search"], [class*="Search"], [class*="icon"], [class*="Icon"]'
                    )].filter(el => el !== input && visible(el));
                    const preferred = candidates.find(el => {
                      const marker = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`;
                      return /搜索|search|icon/i.test(marker);
                    }) || candidates[0];
                    if (preferred) {
                      preferred.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
            if clicked_icon:
                self._page.wait_for_timeout(400)
                return True
            inp.press("Enter")
            return True
        except Exception:  # noqa: BLE001
            return False

    _COMPANY_ROWS_JS = """
        () => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none'
              && s.visibility !== 'hidden' && s.opacity !== '0';
          };
          const text = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          const out = [];
          for (const table of [...document.querySelectorAll('table')].filter(visible)) {
            let heads = [...table.querySelectorAll('thead th')].map(text);
            const rows = [...table.querySelectorAll('tbody tr')].filter(visible);
            if (!heads.length) {
              const first = table.querySelector('tr');
              if (first) heads = [...first.querySelectorAll('th')].map(text);
            }
            for (const row of rows) {
              const cells = [...row.querySelectorAll('td')].map(text);
              if (!cells.some(Boolean)) continue;
              const rec = {};
              cells.forEach((cell, i) => { rec[heads[i] || `列${i + 1}`] = cell; });
              out.push(rec);
            }
          }
          if (out.length) return out;
          for (const row of [...document.querySelectorAll('[role="row"]')].filter(visible)) {
            const cells = [...row.querySelectorAll('[role="cell"], [role="gridcell"]')].map(text);
            if (cells.some(Boolean)) {
              const rec = {};
              cells.forEach((cell, i) => { rec[`列${i + 1}`] = cell; });
              out.push(rec);
            }
          }
          // 当前“查企业”不是 table，而是企业卡片：企业名在 .col-name，
          // 法人/注册资本/成立日期等信息位于同一 .info-block。只把卡片
          // 作为候选，不从推荐词或页面其它公司名猜主体。
          for (const nameEl of [...document.querySelectorAll('.col-name')].filter(visible)) {
            const card = nameEl.closest('.info-block') || nameEl.parentElement;
            if (!card || !visible(card)) continue;
            const name = text(nameEl);
            if (!name) continue;
            const cardText = text(card);
            const legal = (cardText.match(/法人[：:]\\s*([^\\s]+)/) || [])[1] || '';
            out.push({企业名称: name, 法定代表人: legal});
          }
          return out;
        }
    """

    def _company_result_rows(self, query: str) -> list[dict[str, str]]:
        raw: list[dict[str, Any]] = []
        for frame in self._all_frames():
            try:
                rows = frame.evaluate(self._COMPANY_ROWS_JS) or []
                if isinstance(rows, list):
                    raw.extend(row for row in rows if isinstance(row, dict))
            except Exception:  # noqa: BLE001
                continue
        return _normalize_company_candidates(raw, query)

    _CLICK_COMPANY_ROW_JS = """
        (keyword) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none'
              && s.visibility !== 'hidden' && s.opacity !== '0';
          };
          const elements = [...document.querySelectorAll('tr, [role="row"], li')]
            .filter(visible)
            .filter(el => (el.innerText || el.textContent || '').includes(keyword));
          if (elements.length) {
            const row = elements.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0];
            row.scrollIntoView({block: 'center'});
            row.click();
            return true;
          }
          const exact = [...document.querySelectorAll('a, button, span, div')]
            .find(el => visible(el) && (el.innerText || el.textContent || '').trim() === keyword);
          if (exact) { exact.click(); return true; }
          return false;
        }
    """

    def _click_company_result(self, candidate: dict[str, str]) -> bool:
        keywords = [candidate.get("name", ""), candidate.get("uscc", "")]
        for keyword in keywords:
            if not keyword:
                continue
            for frame in self._all_frames():
                try:
                    if frame.evaluate(self._CLICK_COMPANY_ROW_JS, keyword):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _wait_click_anywhere(self, texts: list[str], timeout_ms: int = 15000) -> bool:
        """等待元素渲染后点击（工作台宫格/菜单异步加载）。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._click_anywhere(texts, exact=True):
                return True
            self._page.wait_for_timeout(1000)
        return False

    def _open_via_app_search(self, app_name: str) -> bool:
        """工作台『搜索服务名称』输入应用名 → 打开下拉结果（JS 直点叶子节点）。

        下拉浮层常被其他层遮挡，Playwright 常规 click 的可交互性检查容易失败，
        故优先对“文本恰为应用名”的叶子元素派发 JS click；再以键盘选择兜底。
        """
        for frame in self._page.frames:
            for loc in (frame.locator('input[placeholder="搜索服务名称"]'),
                        frame.locator('input[placeholder="搜索"]')):
                try:
                    if not loc.count():
                        continue
                    el = None
                    for cand in loc.all():
                        try:
                            if cand.is_visible():
                                el = cand
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if el is None:
                        continue
                    el.fill(app_name)
                    frame.page.wait_for_timeout(1500)
                    # 1) 真实点击“文本恰为应用名”的可见元素，避免点中拼接容器。
                    try:
                        items = frame.get_by_text(app_name, exact=True).all()
                    except Exception:  # noqa: BLE001
                        items = []
                    for item in items:
                        try:
                            if item.is_visible():
                                item.click(timeout=5000)
                                frame.page.wait_for_timeout(2500)
                                if self._app_opened(app_name):
                                    return True
                        except Exception:  # noqa: BLE001
                            continue
                    # 2) 键盘选择兜底
                    el.press("ArrowDown")
                    el.press("Enter")
                    frame.page.wait_for_timeout(2500)
                    if self._app_opened(app_name):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def _app_opened(self, app_name: str) -> bool:
        """判断主页应用是否已打开：查企业以搜索框可见为准。"""
        if app_name == "查企业":
            return self._company_search_input()[1] is not None
        return False

    def _dump_debug_page(self, tag: str) -> None:
        """定位失败时保存各 frame 可见文本摘要（不含业务数据内容），便于远程诊断。"""
        try:
            import json as _json

            frames = []
            for pg in self._context.pages:
                main = pg is self._page
                for f in pg.frames:
                    try:
                        txt = f.evaluate("document.body.innerText.replace(/\\s+/g,' ').slice(0,3000)")
                    except Exception:  # noqa: BLE001
                        txt = ""
                    try:
                        tr_count = f.evaluate("document.querySelectorAll('tbody tr').length")
                    except Exception:  # noqa: BLE001
                        tr_count = -1
                    frames.append({"url": f.url[:120], "text": txt, "tbody_tr": tr_count,
                                   "page_is_active": main})
            path = f"/tmp/srm-nav-debug-{tag}.json"
            with open(path, "w", encoding="utf-8") as fh:
                _json.dump({"at": datetime.now().isoformat(timespec="seconds"), "frames": frames},
                           fh, ensure_ascii=False, indent=1)
            try:
                self._page.screenshot(path=f"/tmp/srm-nav-debug-{tag}.png", full_page=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _click_anywhere(self, texts: list[str], strict_button: bool = False,
                        exact: bool = False) -> bool:
        """在所有 frame 中按可见文字点击：遍历全部匹配直到找到可见可点击项。"""
        for frame in self._page.frames:
            for text in texts:
                if strict_button:
                    locs = [frame.get_by_role("button", name=text, exact=False)]
                else:
                    locs = [frame.get_by_text(text, exact=exact),
                            frame.get_by_role("link", name=text, exact=exact)]
                for loc in locs:
                    try:
                        for el in loc.all():
                            try:
                                if el.is_visible():
                                    el.click(timeout=5000)
                                    return True
                            except Exception:  # noqa: BLE001
                                continue
                    except Exception:  # noqa: BLE001
                        continue
        return False

    def _click_in_frame(self, frame, texts: list[str]) -> bool:
        for text in texts:
            for loc in (frame.get_by_text(text, exact=False),
                        frame.get_by_role("button", name=text, exact=False)):
                try:
                    for el in loc.all():
                        try:
                            if el.is_visible():
                                el.click(timeout=5000)
                                return True
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    continue
        return False

    def close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._page = self._context = self._browser = self._pw = None
        self._active_profile_frame = None
        self._company_search_open = False


def ensure_registered(headless: bool = True) -> None:
    """把 Playwright 驱动注册进宿主注册表（供 SrmAdapter / 流水线使用）。"""
    timeout_ms = int(os.environ.get("SRM_BROWSER_TIMEOUT_MS", "45000"))
    manual_wait_s = int(os.environ.get("SRM_MANUAL_TAKEOVER_WAIT_SECONDS", "0"))
    stable_wait_ms = int(os.environ.get("SRM_BROWSER_STABLE_WAIT_MS", "1200"))
    register_browser_driver(
        factory=lambda: PlaywrightSrmDriver(
            headless=headless,
            timeout_ms=max(1000, timeout_ms),
            manual_takeover_wait_s=max(0, manual_wait_s),
            stable_wait_ms=max(0, stable_wait_ms),
        ),
        credential_provider=lambda: BrowserCredentials(
            username=os.environ.get("SRM_USER", ""),
            password=os.environ.get("SRM_PASSWORD", ""),
        ),
    )


def query_once(
    keyword: str,
    username: str = "",
    password: str = "",
    headless: bool = False,
    manual_login: bool = False,
) -> AdapterResult:
    """单次查询；manual_login 模式由用户在可见浏览器内完成登录。"""
    creds = None if manual_login else BrowserCredentials(username=username, password=password)
    client = SrmBrowserClient(
        driver_factory=lambda: PlaywrightSrmDriver(headless=False if manual_login else headless),
        credentials=creds,
        manual_login=manual_login,
    )
    uscc_m = re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", keyword, re.I)
    subject = QuerySubject(
        supplier_id="runtime-query",
        name=None if uscc_m else keyword,
        uscc=uscc_m.group(0).upper() if uscc_m else None,
    )
    return client.query(subject, datetime.now(timezone.utc))
