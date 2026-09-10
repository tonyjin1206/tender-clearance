#!/usr/bin/env python3
"""SRM 浏览器会话自测（P5 联调用，Playwright 真实浏览器）。

用法（凭据只进本进程内存，绝不写入任何文件）：

    export SRM_USER='<SRM账号>'
    export SRM_PASSWORD='<SRM密码>'
    python scripts/srm_browser_selftest.py '<企业名称或统一社会信用代码>'

可选：
    --headed     有头模式（默认无头后台；仅当需要人工接管验证码时使用）
    --keep       查询后不立即关闭浏览器（便于人工核对页面状态）

行为：打开 SRM → 登录 → 供应商档案检索主体 → 企业画像读取
基本信息/司法风险/经营风险 → 打印脱敏结果。
登录页出现图形验证码时需人工在浏览器窗口完成登录后重试（驱动不自动重试）。
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer

from tc.srm_browser import BrowserCredentials, SrmBrowserClient
from tc.srm_playwright_driver import PlaywrightSrmDriver, _norm  # noqa: F401
from tc.srm_playwright_driver import query_once

app = typer.Typer(add_completion=False)


@app.command()
def main(
    keyword: str = typer.Argument(..., help="企业名称或统一社会信用代码"),
    headed: bool = typer.Option(False, "--headed", help="有头模式（验证码人工接管用；默认无头后台）"),
    user: str = typer.Option("", help="SRM 账号（默认读环境变量 SRM_USER 或交互输入）"),
) -> int:
    password = os.environ.get("SRM_PASSWORD", "")
    username = user or os.environ.get("SRM_USER", "")
    try:
        if not username:
            username = input("SRM 用户名: ")
        if not password:
            password = getpass.getpass("SRM 密码: ")
    except EOFError:
        typer.secho("[提示] 未提供凭据（设置 SRM_USER / SRM_PASSWORD 后重试）", fg="red", err=True)
        raise typer.Exit(code=2)
    if not username or not password:
        typer.secho("[提示] 凭据为空，未打开浏览器", fg="red", err=True)
        raise typer.Exit(code=2)

    result = query_once(keyword, username, password, headless=not headed)

    typer.secho(f"状态：{result.status}", fg="green" if result.status == "match" else "yellow")
    if result.detail:
        print(f"说明：{result.detail}")
    if result.response_ref:
        print(f"证据引用：{result.response_ref}")
    if result.records:
        print(f"记录 {result.parsed_count} 条：")
        for rec in result.records:
            print(f"  - [{rec.record_kind}] 主体确认={rec.subject_confirmation}")
            for k, v in rec.fields.items():
                print(f"      {k}: {v}")
    # 自检：结果文本不得包含凭据
    blob = f"{result.detail or ''}{result.response_ref or ''}" + "".join(
        str(f) for r in result.records for f in r.fields.values())
    if password and password in blob:
        typer.secho("[安全自检失败] 结果包含凭据，已隐藏全部输出", fg="red", err=True)
        raise typer.Exit(code=3)
    return 0 if result.status == "match" else 1


if __name__ == "__main__":
    raise SystemExit(app())
