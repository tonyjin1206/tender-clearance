#!/usr/bin/env python3
"""Replay the confirmed read-only SRM portrait XHR with an existing browser session.

The browser username/password login request was not captured in the reverse-engineering
run.  This script therefore deliberately does not send SRM_USER or SRM_PASSWORD to a
guessed endpoint.  Provide a short-lived SRM_COOKIE at runtime after logging in through
the browser.  No cookie, token, or raw contact field is written to disk or printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

import requests


BASE_URL = "https://c2.yonyoucloud.com"
PATH = "/iuap-data-ep/intellid/outside/custom/getDimensBycfId"
DATA_ID = "f1dd4bd1785ecf875d26df335e08b897"
CF_IDS = {
    "basic": "xf808081675a42dd01675d50979b0002",
    # This is the risk request actually captured; its datas was empty.
    "risk": "8a865144726507bc0172650ed3160282",
}


SENSITIVE_KEY = re.compile(
    r"(?:cookie|token|password|secret|authorization|mobile|phone|tel|email|"
    r"id.?card|identity|document.?number|traceid)$",
    re.I,
)


def redact(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "<removed>"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str) and ("loginToken" in value or "access_token" in value):
        return "<removed>"
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay confirmed read-only SRM portrait XHR")
    parser.add_argument("--kind", choices=sorted(CF_IDS), required=True)
    parser.add_argument("--name", default="丹东富田精工机械有限公司")
    parser.add_argument("--base-url", default=os.environ.get("SRM_PORTRAIT_BASE", BASE_URL))
    parser.add_argument("--data-id", default=os.environ.get("SRM_PORTRAIT_DATA_ID", DATA_ID))
    parser.add_argument("--cf-id", help="Override a captured cfId only when separately verified")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cookie = os.environ.get("SRM_COOKIE", "")
    if not cookie:
        print(
            "缺少 SRM_COOKIE：请先在已登录浏览器会话中运行。"
            "网页登录提交端点尚未实测，脚本不会猜测性发送 SRM_USER/SRM_PASSWORD。",
            file=sys.stderr,
        )
        return 2

    params = {
        "isAjax": "1",
        "tabId": "0" if args.kind == "basic" else "2",
        "dataId": args.data_id,
        "cfId": args.cf_id or CF_IDS[args.kind],
        "companyName": args.name,
        "dataCode": "10",
        "source": "public",
        "enterpriseType": "0",
        "funcCode": "ent_info_view",
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://c2.yonyoucloud.com/",
        "Cookie": cookie,
        "User-Agent": "srm-readonly-replay/1.0",
    }
    try:
        response = requests.get(
            args.base_url.rstrip("/") + PATH,
            params=params,
            headers=headers,
            timeout=(5, 30),
        )
        response.raise_for_status()
        body: Any = response.json()
    except requests.RequestException as exc:
        print(f"请求失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    except ValueError:
        print(f"响应不是 JSON（HTTP {response.status_code}）", file=sys.stderr)
        return 1

    safe = {
        "kind": args.kind,
        "http_status": response.status_code,
        "request": {"method": "GET", "path": PATH, "query": params},
        "response": redact(body),
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
