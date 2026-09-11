"""富奥 SRM（用友 YonBIP）实时查询客户端（P5）。

设计约束（方案 6.4 / 8.2，安全与留存）：
- 系统账户的用户名/密码由用户在运行时提供，仅保存在本进程内存；
  绝不写入 Skill、配置文件、external.json、证据、日志或异常堆栈；
- 接口形状完全由 rules/srm-api.yaml 配置驱动（对接 API 访问规范），
  适配器代码不硬编码任何业务接口路径；
- 未配置 auth.mode（或为 none）时不发起任何访问，返回 needs_manual_review；
- 鉴权失败/人机校验 → blocked；网络错误 → failed；接口成功但无记录 →
  no_match_verified（仅此含义）。

配置文件模板见 rules/srm-api.yaml，字段说明：
  auth.mode: none | token_post
  auth.path / auth.payload（{username}/{password} 占位）
  auth.token_json_path: 从登录响应 JSON 取 token 的键路径（a.b.c）
  auth.token_header: {头名: "{token}"}，后续业务请求附带
  auth.extra_headers: 静态请求头
  queries.<name>: {path, method, payload_template, records_json_path, field_map}
    payload_template 支持 {uscc} / {name} / {token} 占位
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from .sources import AdapterRecord, AdapterResult, HttpRequest, QuerySubject


def _norm_name(v: str | None) -> str:
    import unicodedata

    if not v:
        return ""
    t = unicodedata.normalize("NFKC", str(v))
    return re.sub(r"\s+", "", t).replace("　", "")

ADAPTER_VERSION = "srm/0.5.0"

DEFAULT_SRM_CONFIG_PATH = Path(__file__).resolve().parents[2] / "rules" / "srm-api.yaml"

JsonTransport = Callable[[HttpRequest], tuple[int, str]]


@dataclass
class SrmCredentials:
    """运行时凭据（仅内存）。"""
    username: str = ""
    password: str = ""

    def clear(self) -> None:
        self.username = ""
        self.password = ""


def load_srm_config(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_SRM_CONFIG_PATH
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("srm") or {}


class SrmClient:
    """配置驱动的 SRM 查询客户端。"""

    def __init__(
        self,
        config: dict[str, Any],
        credentials: SrmCredentials | None = None,
        transport: JsonTransport | None = None,
    ) -> None:
        self.config = config or {}
        self.credentials = credentials or SrmCredentials()
        self._transport = transport
        self._token: str | None = None
        self._last_query: float = 0.0

    # ------------------------------------------------------------- 基础

    def _get_transport(self) -> JsonTransport:
        if self._transport is not None:
            return self._transport
        import requests

        session = requests.Session()

        def transport(req: HttpRequest) -> tuple[int, str]:
            headers = {
                "User-Agent": "tender-clearance/0.2 (compliance review; contact=procurement office)",
                "Content-Type": "application/json;charset=utf-8",
                **req.headers,
            }
            if req.method == "POST":
                resp = session.post(
                    req.url, json=req.form, params=req.params or None,
                    timeout=(5, 25), headers=headers,
                )
            else:
                resp = session.get(
                    req.url, params=req.params or None,
                    timeout=(5, 25), headers=headers,
                )
            return resp.status_code, resp.text

        return transport

    def _render(self, template: Any, subject: QuerySubject | None = None) -> Any:
        """递归渲染模板中的 {username}/{password}/{token}/{uscc}/{name} 占位。"""
        if isinstance(template, str):
            subs = {
                "username": self.credentials.username,
                "password": self.credentials.password,
                "token": self._token or "",
            }
            if subject:
                subs.update({"uscc": subject.uscc or "", "name": subject.name or ""})
            out = template
            for k, v in subs.items():
                out = out.replace("{" + k + "}", v)
            return out
        if isinstance(template, dict):
            return {k: self._render(v, subject) for k, v in template.items()}
        if isinstance(template, list):
            return [self._render(v, subject) for v in template]
        return template

    def _request(self, path: str, method: str, payload: Any, extra_headers: dict | None = None,
                 extra_params: dict[str, str] | None = None, subject: QuerySubject | None = None) -> tuple[int, Any]:
        import time as _time

        cfg = self.config
        base = str(cfg.get("base_url", "")).rstrip("/")
        headers: dict[str, str] = {}
        auth = cfg.get("auth") or {}
        for k, v in (auth.get("extra_headers") or {}).items():
            headers[str(k)] = str(v)
        if self._token:
            for k, v in (auth.get("token_header") or {}).items():
                headers[str(k)] = str(v).replace("{token}", self._token)
        if extra_headers:
            headers.update(extra_headers)
        req = HttpRequest(
            method=method.upper(),
            url=base + path,
            form=self._render(payload, subject) if payload is not None else {},
            params=extra_params or {},
            headers=headers,
        )
        wait = float((cfg.get("auth") or {}).get("rate_limit_seconds", 3.0)) - (_time.monotonic() - self._last_query)
        if wait > 0:
            _time.sleep(min(wait, 30.0))
        try:
            code, body = self._get_transport()(req)
        finally:
            self._last_query = _time.monotonic()
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = body
        return code, parsed

    @staticmethod
    def _json_path(data: Any, dotted: str) -> Any:
        cur = data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.lstrip("-").isdigit():
                idx = int(part)
                if -len(cur) <= idx < len(cur):
                    cur = cur[idx]
                else:
                    return None
            else:
                return None
        return cur

    # ------------------------------------------------------------- 鉴权

    def authenticate(self) -> tuple[bool, str]:
        """按配置执行登录/取 token。返回 (成功, 说明)。"""
        auth = self.config.get("auth") or {}
        mode = str(auth.get("mode", "none"))
        if mode == "none":
            return False, "SRM 鉴权未配置（auth.mode=none）；需 API 访问规范后启用实时查询"
        if mode == "token_post":
            if not self.credentials.username or not self.credentials.password:
                return False, "缺少 SRM 运行时凭据（用户名/密码为空）"
            if str(auth.get("method", "post")).lower() == "get":
                # YonBIP 开放平台形态：GET + query 参数（appKey/appSecret 等）
                params = {k: str(v).replace("{username}", self.credentials.username)
                           .replace("{password}", self.credentials.password)
                          for k, v in (auth.get("query_template") or {}).items()}
                code, body = self._request(str(auth.get("path", "")), "GET", None, extra_params=params)
            else:
                code, body = self._request(
                    str(auth.get("path", "")),
                    "POST",
                    auth.get("payload") or {"userName": "{username}", "password": "{password}"},
                )
            if code in (401, 403):
                return False, f"SRM 鉴权被拒绝（HTTP {code}）"
            token_path = str(auth.get("token_json_path", "data.token"))
            token = self._json_path(body, token_path)
            if not token or not isinstance(token, str):
                # 登录失败时响应体常含错误码/消息 —— 只保留状态与消息码，不回显凭据
                msg = ""
                if isinstance(body, dict):
                    msg = str(body.get("message") or body.get("msg") or "")[:80]
                return False, f"SRM 登录未取得令牌（HTTP {code}，{msg or '响应无 ' + token_path + ' 键'}）"
            self._token = token
            return True, "SRM 登录成功（令牌仅存内存）"
        return False, f"未知 auth.mode：{mode}"

    def close(self) -> None:
        """清除运行时凭据和令牌。"""
        self.credentials.clear()
        self._token = None

    # ------------------------------------------------------------- 查询

    def query(self, subject: QuerySubject, as_of: datetime, query_name: str = "supplier") -> AdapterResult:
        cfg = self.config
        auth = cfg.get("auth") or {}
        if str(auth.get("mode", "none")) == "none" or not cfg.get("queries"):
            return AdapterResult(
                status="needs_manual_review",
                detail="SRM 实时查询未配置（rules/srm-api.yaml 需 API 访问规范）；请使用授权导出文件导入",
                request_mode="none",
            )
        if not subject.uscc and not subject.name:
            return AdapterResult(
                status="needs_manual_review",
                detail="缺少主体键（无统一社会信用代码与企业名称），无法查询 SRM",
                request_mode="none",
            )
        ok, detail = self.authenticate()
        if not ok:
            # 凭据未提供/未配置 → 转人工；已提供但被拒（鉴权未通过）→ blocked
            status = "needs_manual_review" if (detail.startswith("缺少") or "未配置" in detail) else "blocked"
            return AdapterResult(status=status, detail=detail, request_mode="official_api")

        qcfg = (cfg.get("queries") or {}).get(query_name)
        if not qcfg:
            return AdapterResult(
                status="needs_manual_review",
                detail=f"SRM 查询配置缺少 queries.{query_name}",
                request_mode="official_api",
            )
        extra_params: dict[str, str] = {}
        if self._token:
            for k, v in (auth.get("token_query") or {}).items():
                extra_params[str(k)] = str(v).replace("{token}", self._token)
        try:
            code, body = self._request(
                str(qcfg.get("path", "")),
                str(qcfg.get("method", "POST")),
                qcfg["payload_template"] if "payload_template" in qcfg else {"uscc": "{uscc}", "name": "{name}"},
                extra_params=extra_params,
                subject=subject,
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterResult(
                status="failed",
                detail=f"SRM 查询请求失败：{type(exc).__name__}: {exc}",
                request_mode="official_api",
            )
        # 业务状态码（YonBIP 常见：HTTP 200 + body.code）
        body_code_path = qcfg.get("body_code_path") or auth.get("body_code_path")
        if body_code_path:
            bcode = str(self._json_path(body, str(body_code_path)) or "")
            ok_codes = [str(x) for x in (qcfg.get("body_ok_codes") or auth.get("body_ok_codes") or ["200"])]
            blocked_codes = [str(x) for x in (qcfg.get("body_blocked_codes") or auth.get("body_blocked_codes") or [])]
            if bcode and bcode not in ok_codes:
                msg = str(body.get("message") or body.get("msg") or "")[:80] if isinstance(body, dict) else ""
                status = "blocked" if bcode in blocked_codes else "failed"
                return AdapterResult(
                    status=status,
                    detail=f"SRM 业务返回码 {bcode}（{msg or '无消息'}）",
                    request_mode="official_api",
                )
        lowered = json.dumps(body, ensure_ascii=False)[:20000].lower() if not isinstance(body, str) else body[:20000].lower()
        blocked_indicators = [str(x) for x in (auth.get("blocked_indicators") or [])]
        if code in (401, 403) or any(ind.lower() in lowered for ind in blocked_indicators):
            return AdapterResult(
                status="blocked",
                detail=f"SRM 返回 {code} 或人机校验/权限特征，已停止自动化查询",
                request_mode="official_api",
            )
        if code != 200:
            return AdapterResult(
                status="failed",
                detail=f"SRM 返回非预期状态码 {code}",
                request_mode="official_api",
            )
        records_raw = self._json_path(body, str(qcfg.get("records_json_path", "data.records")))
        if records_raw is None:
            return AdapterResult(
                status="no_result",
                detail="SRM 查询成功返回，但未在 records_json_path 处取得记录列表，需人工复核",
                request_mode="official_api",
            )
        if not isinstance(records_raw, list):
            records_raw = [records_raw]
        field_map = qcfg.get("field_map") or {}
        match_fields = [str(x) for x in (qcfg.get("match_fields") or [])]
        out: list[AdapterRecord] = []
        for item in records_raw:
            if not isinstance(item, dict):
                continue
            fields = {}
            for src, dst in field_map.items():
                v = item.get(src)
                if v is None:
                    continue
                if isinstance(v, list):
                    v = "、".join(str(x) for x in v if str(x).strip())
                if str(v).strip():
                    fields[dst] = str(v).strip()
            if not fields:
                continue
            confirmed = bool(subject.uscc) and any(v == subject.uscc for v in fields.values())
            if match_fields:
                hit_vals = [fields.get(f, "") for f in match_fields]
                code_hit = bool(subject.uscc) and subject.uscc in hit_vals
                name_hit = bool(subject.name) and any(
                    _norm_name(v) == _norm_name(subject.name) for v in hit_vals if v)
                if code_hit:
                    confirmed = True
                elif not name_hit:
                    continue  # 非本主体的记录不得计入
            out.append(AdapterRecord(
                record_kind=str(qcfg.get("record_kind", "other")),
                fields=fields,
                effective_from=(str(item.get(str(qcfg.get("effective_from_field")))) if qcfg.get("effective_from_field") and item.get(str(qcfg.get("effective_from_field"))) else None),
                effective_to=(str(item.get(str(qcfg.get("effective_to_field")))) if qcfg.get("effective_to_field") and item.get(str(qcfg.get("effective_to_field"))) else None),
                subject_confirmation="confirmed" if confirmed else "candidate",
            ))
        if out:
            return AdapterResult(
                status="match",
                records=out,
                parsed_count=len(out),
                request_mode="official_api",
                response_ref=f"SRM 接口 {qcfg.get('path')} 返回 {len(out)} 条记录",
            )
        # 有列表但按主体键过滤后为空：SRM 是内部系统，“查无此主体”即已核验证词
        return AdapterResult(
            status="no_match_verified",
            detail=f"已成功查询 SRM（接口 {qcfg.get('path')}），无该主体的匹配记录",
            request_mode="official_api",
        )
