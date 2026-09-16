#!/usr/bin/env python3
"""SRM 导航结构探测（联调校准用，只读、不输入任何业务数据）。

流程：脚本打开真实浏览器 → 【你手动登录】→ 按终端提示逐步点击页面
（供应商档案 → 高级查询 → 查询结果 → 企业画像 → 各页签），每步回车后脚本
把当前页面结构（iframe 列表、可点击文字、表格表头）dump 到 /tmp/srm-nav-dump/。

不记录凭据、Cookie、业务数据内容，只记录导航结构（菜单名/按钮名/表头名）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/tmp/srm-nav-dump")
OUT.mkdir(exist_ok=True)

DUMP_JS = """
() => {
  const pick = el => (el.innerText || '').trim().replace(/\\s+/g, ' ');
  const clickables = [];
  document.querySelectorAll('button,a,li,span,div,[role=button],[role=menuitem],[role=tab]').forEach(el => {
    const t = pick(el);
    if (!t || t.length > 20 || t.length < 2) return;
    if (el.children.length > 3) return;
    const clickable = el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'LI'
      || el.getAttribute('role') || /cursor-pointer|clickable|menu|tab/i.test(el.className || '');
    if (clickable) clickables.push({tag: el.tagName, text: t.slice(0, 20), cls: String(el.className||'').slice(0, 60)});
  });
  // 去重
  const seen = new Set();
  const uniq = clickables.filter(c => {
    const k = c.tag + '|' + c.text;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  const tables = [...document.querySelectorAll('table')].slice(0, 3).map(t => ({
    heads: [...t.querySelectorAll('th')].map(th => (th.innerText || '').trim()).filter(Boolean),
    row_count: t.querySelectorAll('tbody tr').length,
  }));
  const inputs = [...document.querySelectorAll('input,textarea')].map(e => ({
    type: e.type, placeholder: e.placeholder || '', id: e.id || '',
  })).filter(i => i.type !== 'hidden');
  return {
    url: location.href.slice(0, 120),
    title: document.title.slice(0, 60),
    text_sample: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 800),
    clickables: uniq.slice(0, 60),
    tables, inputs,
  };
}
"""


def dump(page, step: str) -> None:
    record = {"step": step, "captured_at": datetime.now().isoformat(timespec="seconds"), "frames": []}
    for frame in page.frames:
        try:
            data = frame.evaluate(DUMP_JS)
        except Exception as exc:  # noqa: BLE001
            data = {"url": getattr(frame, "url", "?"), "error": type(exc).__name__}
        data["is_main"] = frame is page.main_frame
        record["frames"].append(data)
    path = OUT / f"step-{step}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    n_frames = len(record["frames"])
    n_click = sum(len(f.get("clickables") or []) for f in record["frames"])
    print(f"  [已保存] {path.name}（{n_frames} 个 frame，{n_click} 个可点击候选）")


def main() -> None:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=False)
        except Exception:  # noqa: BLE001
            browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="zh-CN")
        page = context.new_page()
        page.goto("https://yonbip.fawer.com.cn/", timeout=45000, wait_until="domcontentloaded")

        input("① 请在浏览器窗口完成登录（含验证码）。登录进入工作台后回到这里按回车 > ")
        dump(page, "1-after-login")

        input("② 请点击进入『供应商档案』（找不到就直接回车） > ")
        page.wait_for_timeout(2000)
        dump(page, "2-supplier-entry")

        input("③ 请进入『高级查询』并点『查询』得到结果列表（找不到就直接回车） > ")
        page.wait_for_timeout(2000)
        dump(page, "3-search-result")

        input("④ 请打开任一供应商档案，点『更全面企业信息』进入企业画像 > ")
        page.wait_for_timeout(2000)
        dump(page, "4-profile-basic")

        input("⑤ 请点击『司法风险』页签 > ")
        page.wait_for_timeout(2000)
        dump(page, "5-judicial")

        input("⑥ 请点击『经营风险』页签 > ")
        page.wait_for_timeout(2000)
        dump(page, "6-operating")

        print(f"\n完成。结构文件在 {OUT} ，请让 Claude 读取该目录。")
        time.sleep(2)
        browser.close()


if __name__ == "__main__":
    main()
