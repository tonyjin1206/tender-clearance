"""Playwright 实现的 SRM 浏览器驱动（SrmBrowserDriver 宿主桥接）。

边界（与《SRM浏览器会话集成.md》一致）：
- 只做真实浏览器页面交互（打开页面、输入运行时凭据、按可见文字定位导航、
  读取可见 DOM）；不调用 requests/curl/fetch，不复现 XHR；
- Cookie 存活于浏览器进程内存（非持久化 context），close 即销毁；
- 登录页出现图形验证码 / 二次验证 → 返回 manual（需要人工接管），不绕过、
  不重试规避；密码错误 → blocked；
- 密码摘要由登录页自身 JS 完成（本驱动只做常规页面输入与提交）。

登录前 DOM（#username/#password/#submit_btn_login、验证码 #inputCode）已于
2026-09-09 在真实登录页核实；登录后的导航（供应商档案 → 高级查询 → 唯一命中 →
更全面企业信息 → 企业画像 → 基本信息/司法风险/经营风险）按集成文档 §5–§6 实现，
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


class PlaywrightSrmDriver:
    """基于 Playwright 的真实浏览器驱动（同步 API）。"""

    source_id = "srm"
    version = "srm-browser-playwright/0.1.0"

    def __init__(self, headless: bool = False, timeout_ms: int = 45000,
                 manual_takeover_wait_s: int = 180) -> None:
        self._headless = headless
        self._timeout = timeout_ms
        self._takeover_wait = manual_takeover_wait_s
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

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
            return any(k in text for k in ("工作台", "首页", "供应商档案", "应用中心"))
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

        # 轮询判定（最多 15 秒）：跳转成功 / 出验证码 / 报错，避免把跳转中误判为失败
        deadline = time.monotonic() + 15
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

    # ------------------------------------------------------------ 主体检索与核对
    # 以下导航顺序基于 2026-09-09 真实页面结构探测（srm-nav-dump）：
    # - 工作台/供应商档案应用在主文档；档案搜索框 id 含 aa_vendorlist；
    # - 企业画像在独立 iframe（URL 含 /intellid/portrait/）；
    # - 表格为表头/表体分离渲染；分类计数格式为“分类名 (数字)”。

    def search_subject(self, subject: QuerySubject) -> BrowserSubjectResult:
        """供应商档案 → 关键字搜索 → 唯一命中 → 更全面企业信息 → 画像主体核对。"""
        query = subject.name or subject.uscc or ""

        # 工作台导航为复杂树状结构，稳妥路径是“搜索服务名称”应用搜索；
        # 顶部导航/宫格异步渲染，整体给 12 秒重试窗口。
        opened = False
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not opened:
            opened = (self._open_via_app_search("供应商档案")
                      or self._click_anywhere(["供应商档案"], exact=True))
            if not opened:
                self._page.wait_for_timeout(1500)
        if not opened:
            self._dump_debug_page("supplier-entry")
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail="未能通过应用搜索或宫格打开『供应商档案』（页面结构已存 /tmp/srm-nav-debug-*.json 供诊断）")
        # 微应用可能在新标签页打开：切换到承载供应商档案列表的页面
        self._focus_vendor_page()
        self._page.wait_for_timeout(3000)

        # 高级查询面板：展开面板 → 填编码/名称 → 点查询，作为整体重试
        # （面板开合存在时序抖动，单步各自尝试会相互干扰）
        filled = False
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self._open_advanced_query_panel(6000) and self._fill_code_name_input(query):
                filled = True
                break
            self._page.wait_for_timeout(1500)
        if not filled:
            self._dump_debug_page("vendor-search")
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail="未能打开高级查询面板或未完成关键字填写（页面结构已存 /tmp/srm-nav-debug-*.json 供诊断）")
        # 结果行异步渲染：轮询等待（最多 12 秒）
        rows: list = []
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self._page.wait_for_timeout(1500)
            rows = self._result_rows(query[:4])
            if rows:
                break
        if len(rows) == 0:
            self._dump_debug_page("search-result")
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail="查询结果为 0 条（页面结构已存 /tmp/srm-nav-debug-search-result.json 供诊断）")
        if len(rows) > 1:
            return BrowserSubjectResult(
                None, None, "unconfirmed",
                detail=f"查询命中 {len(rows)} 条供应商，主体歧义；不自动选择，需人工确认档案编码")

        # 唯一命中：点开行 → 更全面企业信息 → 等待企业画像 iframe
        self._click_result_row(query[:4])
        self._page.wait_for_timeout(2500)
        self._click_anywhere(["更全面企业信息"])
        pf = self._wait_profile_ready()
        if pf is None:
            return BrowserSubjectResult(None, None, "unconfirmed",
                                        detail="未进入企业画像页（portrait iframe 未出现或未加载），需人工核对")

        name, uscc = self._read_identity(pf)
        if uscc and subject.uscc and uscc == subject.uscc:
            confirmation: Literal["confirmed", "candidate"] = "confirmed"
        elif name and subject.name and _norm(name) == _norm(subject.name) and not subject.uscc:
            confirmation = "candidate"
        else:
            return BrowserSubjectResult(
                name, uscc, "unconfirmed",
                profile_ref=f"企业画像（名称={name}，信用代码={uscc}）",
                detail="企业画像主体与查询主体不一致，停止读取风险结果")
        return BrowserSubjectResult(
            name, uscc, confirmation,
            profile_ref=f"企业画像（名称={name}，信用代码={uscc}）",
        )

    def _wait_profile_frame(self, timeout_ms: int | None = None):
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

    def _wait_profile_ready(self, timeout_ms: int | None = None):
        """等待画像 iframe 出现且内容加载（可见文本包含主体字段）。"""
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
                if ("企业名称" in text) or ("统一社会信用代码" in text):
                    return frame
            self._page.wait_for_timeout(1000)
        return None

    def _read_identity(self, pf) -> tuple[str | None, str | None]:
        """从画像页文本按行结构取主体：『统一社会信用代码』行的下一行为代码，
        向上最近『企业名称』行的下一行为名称（画像文本中存在多个企业名称
        ——对外投资等区块 —— 不能用全文正则取第一个）。"""
        text = self._full_text(pf)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        uscc = None
        uscc_idx = None
        for i, ln in enumerate(lines):
            if ln == "统一社会信用代码" and i + 1 < len(lines) \
                    and re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", lines[i + 1], re.I):
                uscc = lines[i + 1].upper()
                uscc_idx = i + 1
                break
        name = None
        if uscc_idx:
            for i in range(uscc_idx - 1, -1, -1):
                if lines[i] == "企业名称" and i + 1 < len(lines):
                    cand = lines[i + 1]
                    if 2 <= len(cand) <= 60 and cand != "统一社会信用代码":
                        name = cand
                    break
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
            if not self._click_in_frame(pf, labels):
                return BrowserSectionResult(section=section, structured=False,
                                            detail=f"画像页未找到『{labels[0]}』入口，需人工核对")
            pf.page.wait_for_timeout(2500)
            records = self._frame_table_rows(pf)
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
        keys = ("企业名称", "统一社会信用代码", "法定代表人", "注册资本", "实缴资本",
                "成立日期", "企业状态", "企业类型", "注册地址", "所属行业",
                "人员规模", "参保人数", "曾用名", "注册号")
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            k = ln.rstrip(":：")
            if k in keys and k not in fields and i + 1 < len(lines):
                v = lines[i + 1].strip()
                if v and v not in keys and "：" not in v:
                    fields[k] = v[:80]
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
        () => {
          const tables = [...document.querySelectorAll('table')];
          const headSets = [];
          tables.forEach(t => {
            const ths = [...t.querySelectorAll('thead th')].map(th => (th.innerText||'').trim());
            if (ths.filter(Boolean).length >= 2) headSets.push(ths);
          });
          const out = [];
          tables.forEach(t => {
            [...t.querySelectorAll('tbody tr')].forEach(tr => {
              const cells = [...tr.querySelectorAll('td')].map(td => (td.innerText||'').trim());
              if (cells.length < 2 || !cells.some(c => c)) return;
              // 列数一致的表头组优先；否则退回第一组
              const heads = headSets.find(hs => hs.length === cells.length)
                || (headSets[0] || []);
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

    def _frame_table_rows(self, pf) -> list[dict[str, str]]:
        """读取画像 iframe 内的数据行：逐表配对表头（处理表头/表体分离渲染）。"""
        try:
            return pf.evaluate(self._TABLE_PAIR_JS) or []
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------ 通用定位辅助

    def _vendor_pages(self) -> list:
        """返回 context 中承载供应商档案（aa_vendorlist）的页面，可见者优先。"""
        all_pages = [self._page] + [p for p in self._context.pages if p is not self._page]
        visible, any_hit = [], []
        for pg in all_pages:
            try:
                if pg.locator('input[id*="aa_vendorlist"]').count():
                    any_hit.append(pg)
                    if pg.locator('input[id*="aa_vendorlist"]').first.is_visible():
                        visible.append(pg)
            except Exception:  # noqa: BLE001
                continue
        return visible + [p for p in any_hit if p not in visible]

    def _focus_vendor_page(self) -> None:
        pages = self._vendor_pages()
        if pages and pages[0] is not self._page:
            self._page = pages[0]

    def _open_advanced_query_panel(self, timeout_ms: int = 10000) -> bool:
        """确保高级查询面板展开：先查输入框是否可见；未展开则点一次开关并等待。

        每次调用至多点一次（按钮是切换式的，连点会开-关往返）；外层组合循环
        负责必要时整轮重试。
        """
        deadline = time.monotonic() + timeout_ms / 1000
        clicked = False
        while time.monotonic() < deadline:
            try:
                inp = self._page.locator('input#yssupplierInputcode, input[placeholder="编码/名称"]').first
                if inp.count() and inp.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                pass
            if not clicked:
                try:
                    loc = self._page.locator(
                        'button:has(i.yonicon-gaojichaxun), i.yonicon-gaojichaxun')
                    for cand in loc.all():
                        try:
                            if cand.is_visible():
                                cand.click(timeout=5000)
                                clicked = True
                                self._page.wait_for_timeout(4000)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    pass
            else:
                self._page.wait_for_timeout(1000)
        return False

    def _fill_code_name_input(self, value: str) -> bool:
        """向编码/名称输入框写入关键字，并触发查询。

        高级查询为模态弹窗，提交按钮有两种形态：
        - 弹窗底部蓝色「查询」文字按钮（get_by_role 精确匹配）；
        - 列表工具栏的 btnDirectSearch 图标按钮（非弹窗布局）。
        """
        try:
            inp = self._page.locator('input#yssupplierInputcode, input[placeholder="编码/名称"]').first
            if not (inp.count() and inp.is_visible()):
                return False
            inp.click()
            inp.fill(value)
            # 1) 弹窗底部「查询」文字按钮
            try:
                for btn in self._page.get_by_role("button", name="查询", exact=True).all():
                    if btn.is_visible():
                        btn.click(timeout=5000)
                        return True
            except Exception:  # noqa: BLE001
                pass
            # 2) 工具栏 btnDirectSearch 图标按钮
            btn = self._page.locator('button.btnDirectSearch').first
            if btn.count() and btn.is_visible():
                btn.click(timeout=5000)
                return True
            # 3) 回车兜底
            inp.press("Enter")
            return True
        except Exception:  # noqa: BLE001
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
                    # 1) 真实点击“文本恰为应用名”的可见元素（子串匹配会命中
                    #    “供应商档案采购项目”这类拼接容器，点了无效）
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
        """判断供应商档案应用是否已打开：其搜索框必须可见（非 0 尺寸）。"""
        try:
            if self._page.locator('input[id*="aa_vendorlist"]').first.is_visible():
                return True
            return any(
                f.locator('input[id*="aa_vendorlist"]').first.is_visible()
                for f in self._page.frames if f is not self._page.main_frame
            )
        except Exception:  # noqa: BLE001
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

    _VENDOR_SEARCH_JS = """
        (val) => {
          const sel = 'input[id*="aa_vendorlist"]';
          let el = document.querySelector(sel)
            || [...document.querySelectorAll('input')].find(i => i.placeholder === '请输入关键字');
          if (!el) return false;
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
          setter.call(el, val);
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
          // 同时尝试点击“查询”按钮（叶子/按钮元素）
          const btns = [...document.querySelectorAll('button, a, span, div')]
            .filter(e => (e.innerText||'').trim() === '查询' && e.children.length <= 1);
          if (btns.length) btns[btns.length - 1].click();
          return true;
        }
    """

    def _fill_vendor_search(self, value: str) -> bool:
        """档案搜索框（id 含 aa_vendorlist）：React 受控输入用原生 setter 写值。

        面板未展开（输入框不可见）时先点「高级查询」展开再试。
        """
        for attempt in range(2):
            for frame in self._page.frames:
                try:
                    if frame.evaluate(self._VENDOR_SEARCH_JS, value):
                        return True
                except Exception:  # noqa: BLE001
                    continue
            if attempt == 0:
                self._click_anywhere(["高级查询"])
                self._page.wait_for_timeout(2000)
        return False

    # 结果网格可能是 div 网格（无 <table>）且名称列会截断（如“腾讯云计…”）：
    # 按“包含关键字前缀的最内层可见元素”识别行，穿透 shadow DOM，按 y 聚合（±4px）。
    _ROW_WALK_JS = """
        (kw) => {
          const all = [];
          const walk = (root) => {
            for (const e of root.querySelectorAll('*')) {
              all.push(e);
              if (e.shadowRoot) walk(e.shadowRoot);
            }
          };
          walk(document);
          const hits = [];
          for (const e of all) {
            let t = '';
            try { t = (e.innerText || e.textContent || '').trim(); } catch (err) { continue; }
            if (!t.includes(kw) || t.length > 120) continue;
            const r = e.getBoundingClientRect();
            if (r.width < 30 || r.height < 8) continue;
            const childHit = [...e.children].some(c => (c.textContent||'').includes(kw))
              || (e.shadowRoot && e.shadowRoot.textContent.includes(kw));
            if (childHit) continue;
            hits.push({node: e, startsWith: t.startsWith(kw),
                       text: t.replace(/\\s+/g,' ').slice(0,120), y: Math.round(r.y)});
          }
          return hits;
        }
    """

    def _result_rows(self, keyword: str) -> list:
        try:
            hits = self._page.evaluate(self._ROW_WALK_JS, keyword) or []
        except Exception:  # noqa: BLE001
            return []
        # 顶部“已选条件: 供应商:名称”标签也含关键字但非结果行：
        # 优先取以关键字开头的命中（网格单元格），无则退回全部命中
        pref = [h for h in hits if h.get("startsWith")]
        if pref:
            hits = pref
        rows: dict[int, list] = {}
        for h in sorted(hits, key=lambda x: x["y"]):
            anchor = next((k for k in rows if abs(k - h["y"]) <= 4), h["y"])
            rows.setdefault(anchor, []).append(h)
        return [v for _, v in sorted(rows.items())]

    def _click_result_row(self, keyword: str) -> None:
        try:
            self._page.evaluate("""
                (kw) => {
                  const all = [];
                  const walk = (root) => {
                    for (const e of root.querySelectorAll('*')) {
                      all.push(e);
                      if (e.shadowRoot) walk(e.shadowRoot);
                    }
                  };
                  walk(document);
                  const hits = [];
                  for (const e of all) {
                    let t = '';
                    try { t = (e.innerText || e.textContent || '').trim(); } catch (err) { continue; }
                    if (!t.includes(kw) || t.length > 120) continue;
                    const r = e.getBoundingClientRect();
                    if (r.width < 30 || r.height < 8) continue;
                    const childHit = [...e.children].some(c => (c.textContent||'').includes(kw))
                      || (e.shadowRoot && e.shadowRoot.textContent.includes(kw));
                    if (childHit) continue;
                    hits.push(e);
                  }
                  if (hits.length) {
                    const el = hits[hits.length - 1];
                    el.scrollIntoView({block: 'center'});
                    el.click();
                  }
                }
            """, keyword)
        except Exception:  # noqa: BLE001
            pass

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


def ensure_registered(headless: bool = True) -> None:
    """把 Playwright 驱动注册进宿主注册表（供 SrmAdapter / 流水线使用）。"""
    register_browser_driver(
        factory=lambda: PlaywrightSrmDriver(headless=headless),
        credential_provider=lambda: BrowserCredentials(
            username=os.environ.get("SRM_USER", ""),
            password=os.environ.get("SRM_PASSWORD", ""),
        ),
    )


def query_once(keyword: str, username: str, password: str, headless: bool = False) -> AdapterResult:
    """单次查询：登录 + 主体核对 + 三类页面读取；结束后清空凭据并关闭浏览器。"""
    creds = BrowserCredentials(username=username, password=password)
    client = SrmBrowserClient(driver_factory=lambda: PlaywrightSrmDriver(headless=headless),
                              credentials=creds)
    uscc_m = re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", keyword, re.I)
    subject = QuerySubject(
        supplier_id="runtime-query",
        name=None if uscc_m else keyword,
        uscc=uscc_m.group(0).upper() if uscc_m else None,
    )
    return client.query(subject, datetime.now(timezone.utc))
