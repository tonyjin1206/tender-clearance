#!/usr/bin/env python3
"""SRM 联调本地反向代理（127.0.0.1:8899 → https://yonbip.fawer.com.cn）。

用途：把真实 SRM 门户映射到 localhost，供用户在预览浏览器中正常登录，
代理侧只记录「请求端点/方法/状态码/请求体字段名」用于逆向接口结构。

安全约定：
- 仅绑定 127.0.0.1；
- 不记录任何请求体/响应体的值，只记录 JSON/表单的键名；
- 日志写入 /tmp/srm-proxy.log。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests

UPSTREAM = "https://yonbip.fawer.com.cn"
LOG_PATH = "/tmp/srm-proxy.log"
SENSITIVE_KEYS = {"password", "passwordEncrypt", "loginPassword", "pwd", "appSecret", "secret", "verifyCode"}

_log_lock = threading.Lock()


def log(line: str) -> None:
    with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def key_names(body: bytes, content_type: str) -> list[str]:
    """只提取字段名，丢弃值。"""
    try:
        if "json" in (content_type or ""):
            data = json.loads(body or b"{}")
            if isinstance(data, dict):
                return sorted(data.keys())
            if isinstance(data, list):
                return [f"[list:{len(data)}]"]
            return [type(data).__name__]
        if "form" in (content_type or ""):
            from urllib.parse import parse_qs

            return sorted(parse_qs(body.decode("utf-8", "replace")).keys())
    except Exception:  # noqa: BLE001
        return ["<unparseable>"]
    return []


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = UPSTREAM + self.path
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "origin", "referer"):
                continue
            headers[k] = v
        headers["Origin"] = UPSTREAM
        headers["Referer"] = UPSTREAM + "/"
        try:
            session = requests.Session()
            session.trust_env = False  # 内网直连：忽略系统代理环境变量
            resp = session.request(
                method, url, data=body, headers=headers,
                allow_redirects=False, timeout=(5, 60), verify=True,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ERR {method} {self.path} :: {type(exc).__name__}: {exc}")
            self.send_error(502, str(exc))
            return

        log(f"{method} {self.path} -> {resp.status_code} | body_keys={key_names(body, self.headers.get('Content-Type'))} | set_cookie_names={sorted(resp.cookies.keys())}")

        # 回写响应（去除 Cookie 的 Domain/Secure 属性，映射到 localhost）
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            lk = k.lower()
            if lk in ("content-length", "transfer-encoding", "connection", "content-encoding", "set-cookie"):
                continue
            self.send_header(k, v)
        for c in resp.cookies:
            c["domain"] = None
            c["secure"] = False
            self.send_header("Set-Cookie", c.output(header="", attrs=["Path", "Expires", "HttpOnly", "SameSite"]).strip())
        for k, v in resp.raw.headers.getlist("Set-Cookie") if hasattr(resp.raw, "headers") else []:
            pass
        body_out = resp.content
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body_out)

    def do_GET(self):  # noqa: N802
        self._proxy("GET")

    def do_POST(self):  # noqa: N802
        self._proxy("POST")

    def do_HEAD(self):  # noqa: N802
        self._proxy("HEAD")

    def do_PUT(self):  # noqa: N802
        self._proxy("PUT")

    def do_OPTIONS(self):  # noqa: N802
        self._proxy("OPTIONS")

    def log_message(self, *args):  # 静默默认日志
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    server = ThreadingHTTPServer(("127.0.0.1", port), Proxy)
    log(f"=== proxy started on 127.0.0.1:{port} -> {UPSTREAM} ===")
    print(f"proxy ready: http://127.0.0.1:{port}/  (log: {LOG_PATH})")
    server.serve_forever()


if __name__ == "__main__":
    main()
