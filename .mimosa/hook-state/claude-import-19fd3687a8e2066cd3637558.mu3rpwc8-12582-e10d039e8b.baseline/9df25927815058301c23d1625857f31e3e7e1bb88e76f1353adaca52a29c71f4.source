"""SRM 实时查询客户端测试（P5，配置驱动 + 测试替身）。

凭据仅内存；断言不含任何凭据泄漏。
"""

from __future__ import annotations

import json

from tc.srm import SrmClient, SrmCredentials
from tc.sources import QuerySubject

import pytest

CONFIG = {
    "base_url": "https://srm.example.invalid",
    "auth": {
        "mode": "token_post",
        "path": "/auth/login.do",
        "payload": {"userName": "{username}", "password": "{password}"},
        "token_json_path": "data.token",
        "token_header": {"X-Auth": "{token}"},
        "blocked_indicators": ["权限不足"],
        "rate_limit_seconds": 0.0,
    },
    "queries": {
        "supplier": {
            "path": "/supplier/query.do",
            "method": "POST",
            "payload_template": {"uscc": "{uscc}", "name": "{name}"},
            "records_json_path": "data.records",
            "field_map": {"supplierName": "企业名称", "socialCreditCode": "统一社会信用代码",
                          "registeredCapital": "注册资本"},
            "record_kind": "registration",
        },
    },
}


class ScriptedTransport:
    """按 URL 脚本化响应，记录全部请求。"""

    def __init__(self, responses: dict[str, tuple[int, dict]]):
        self.responses = responses
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        for key, resp in self.responses.items():
            if req.url.endswith(key):
                return resp[0], json.dumps(resp[1], ensure_ascii=False)
        return 404, "{}"


@pytest.fixture()
def ok_transport():
    return ScriptedTransport({
        "/auth/login.do": (200, {"code": 200, "data": {"token": "T0KEN123"}}),
        "/supplier/query.do": (200, {"code": 200, "data": {"records": [
            {"supplierName": "虚构供应商甲有限公司", "socialCreditCode": "91350100M000100Y43",
             "registeredCapital": "5000万元"},
        ]}}),
    })


def test_login_and_match_confirmed(ok_transport):
    from datetime import datetime, timezone

    client = SrmClient(CONFIG, SrmCredentials("u", "p"), transport=ok_transport)
    r = client.query(QuerySubject("S1", "虚构供应商甲有限公司", "91350100M000100Y43"),
                     datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.parsed_count == 1
    rec = r.records[0]
    assert rec.fields["企业名称"] == "虚构供应商甲有限公司"
    assert rec.subject_confirmation == "confirmed"
    # 登录请求携带渲染后的凭据；业务请求携带令牌头
    login_req = ok_transport.requests[0]
    assert login_req.form["userName"] == "u" and login_req.form["password"] == "p"
    biz_req = ok_transport.requests[1]
    assert biz_req.headers.get("X-Auth") == "T0KEN123"
    assert biz_req.form["uscc"] == "91350100M000100Y43"


def test_name_only_match_is_candidate(ok_transport):
    from datetime import datetime, timezone

    client = SrmClient(CONFIG, SrmCredentials("u", "p"), transport=ok_transport)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.records[0].subject_confirmation == "candidate"


def test_missing_credentials_needs_manual_review_not_network():
    from datetime import datetime, timezone

    def boom(req):
        raise AssertionError("缺少凭据时不得发起任何网络访问")

    client = SrmClient(CONFIG, SrmCredentials("", ""), transport=boom)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "needs_manual_review"


def test_wrong_password_blocked():
    from datetime import datetime, timezone

    t = ScriptedTransport({"/auth/login.do": (200, {"code": 500, "message": "用户名或密码错误"})})
    client = SrmClient(CONFIG, SrmCredentials("u", "bad"), transport=t)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "blocked"
    # 错误信息不得回显凭据
    assert "bad" not in (r.detail or "")


def test_http_401_blocked():
    from datetime import datetime, timezone

    t = ScriptedTransport({"/auth/login.do": (401, {"message": "unauthorized"})})
    client = SrmClient(CONFIG, SrmCredentials("u", "p"), transport=t)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "blocked"


def test_no_records_no_match_verified():
    from datetime import datetime, timezone

    t = ScriptedTransport({
        "/auth/login.do": (200, {"code": 200, "data": {"token": "T"}}),
        "/supplier/query.do": (200, {"code": 200, "data": {"records": []}}),
    })
    client = SrmClient(CONFIG, SrmCredentials("u", "p"), transport=t)
    r = client.query(QuerySubject("S1", "查无此供应商", "91350100M000100Y43"), datetime.now(timezone.utc))
    assert r.status == "no_match_verified"


def test_permission_marker_blocked():
    from datetime import datetime, timezone

    t = ScriptedTransport({
        "/auth/login.do": (200, {"code": 200, "data": {"token": "T"}}),
        "/supplier/query.do": (200, {"code": 403, "message": "权限不足"}),
    })
    client = SrmClient(CONFIG, SrmCredentials("u", "p"), transport=t)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "blocked"


def test_default_config_none_mode_needs_manual_review():
    """auth.mode=none（未配置 API 规范）：零网络访问，转人工。"""
    from datetime import datetime, timezone

    def boom(req):
        raise AssertionError("auth.mode=none 时不得发起任何网络访问")

    client = SrmClient({"auth": {"mode": "none"}}, SrmCredentials("u", "p"), transport=boom)
    r = client.query(QuerySubject("S1", "某公司", "91350100M000100Y43"), datetime.now(timezone.utc))
    assert r.status == "needs_manual_review"


# ---- 与 srm-api.yaml（获取企业信息规范）同形的配置 ----

YONBIP_CONFIG = {
    "base_url": "https://srm.example.invalid",
    "auth": {
        "mode": "token_post",
        "path": "/auth/token.do",
        "payload": {"appKey": "{username}", "appSecret": "{password}"},
        "token_json_path": "data.access_token",
        "token_query": {"access_token": "{token}"},
        "body_code_path": "code",
        "body_ok_codes": ["200"],
        "body_blocked_codes": ["204"],
        "rate_limit_seconds": 0.0,
    },
    "queries": {
        "supplier": {
            "path": "/yonbip/cpu/tenant/query",
            "method": "POST",
            "payload_template": {},
            "records_json_path": "data",
            "body_code_path": "code",
            "body_ok_codes": ["200"],
            "body_blocked_codes": ["204"],
            "match_fields": ["统一社会信用代码", "企业名称"],
            "field_map": {"enterpriseName": "企业名称", "bsCode": "统一社会信用代码",
                          "supplierLevel": "供应商等级", "legalRepName": "法定代表人"},
            "record_kind": "registration",
        },
    },
}


def _yonbip_transport(records, code="200"):
    return ScriptedTransport({
        "/auth/token.do": (200, {"code": "200", "data": {"access_token": "AT-1"}}),
        "/yonbip/cpu/tenant/query": (200, {"code": code, "message": "获取成功", "data": records}),
    })


def test_yonbip_token_in_query_and_match_confirmed():
    """规范：access_token 走 query；data 为数组；bsCode 一致 → confirmed。"""
    from datetime import datetime, timezone

    t = _yonbip_transport([
        {"enterpriseName": "虚构供应商甲有限公司", "bsCode": "91350100M000100Y43",
         "supplierLevel": ["A级"], "legalRepName": "张三"},
    ])
    client = SrmClient(YONBIP_CONFIG, SrmCredentials("k", "s"), transport=t)
    r = client.query(QuerySubject("S1", "虚构供应商甲有限公司", "91350100M000100Y43"),
                     datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.records[0].fields["供应商等级"] == "A级"
    assert r.records[0].subject_confirmation == "confirmed"
    token_req = t.requests[1]
    assert token_req.params.get("access_token") == "AT-1"
    assert token_req.form == {}  # 规范未定义请求体


def test_yonbip_match_fields_drops_unrelated_records():
    """接口返回多个租户时，仅保留主体键命中的记录。"""
    from datetime import datetime, timezone

    t = _yonbip_transport([
        {"enterpriseName": "无关企业A", "bsCode": "910000000000000000"},
        {"enterpriseName": "虚构供应商甲有限公司", "bsCode": "91350100M000100Y43"},
    ])
    client = SrmClient(YONBIP_CONFIG, SrmCredentials("k", "s"), transport=t)
    r = client.query(QuerySubject("S1", "虚构供应商甲有限公司", "91350100M000100Y43"),
                     datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.parsed_count == 1
    assert r.records[0].fields["企业名称"] == "虚构供应商甲有限公司"


def test_yonbip_name_only_hit_is_candidate():
    from datetime import datetime, timezone

    t = _yonbip_transport([
        {"enterpriseName": "同名不同码有限公司", "bsCode": "99000000000000000X"},
    ])
    client = SrmClient(YONBIP_CONFIG, SrmCredentials("k", "s"), transport=t)
    r = client.query(QuerySubject("S1", "同名不同码有限公司", None), datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.records[0].subject_confirmation == "candidate"


def test_yonbip_permission_code_204_blocked():
    from datetime import datetime, timezone

    t = _yonbip_transport([], code="204")
    client = SrmClient(YONBIP_CONFIG, SrmCredentials("k", "s"), transport=t)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "blocked"


def test_yonbip_system_error_203_failed():
    from datetime import datetime, timezone

    t = ScriptedTransport({
        "/auth/token.do": (200, {"code": "200", "data": {"access_token": "AT"}}),
        "/yonbip/cpu/tenant/query": (200, {"code": "203", "message": "系统错误"}),
    })
    client = SrmClient(YONBIP_CONFIG, SrmCredentials("k", "s"), transport=t)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "failed"


def test_yonbip_empty_data_no_match_verified():
    from datetime import datetime, timezone

    t = _yonbip_transport([])
    client = SrmClient(YONBIP_CONFIG, SrmCredentials("k", "s"), transport=t)
    r = client.query(QuerySubject("S1", "查无此企业", "91350100M000100Y43"), datetime.now(timezone.utc))
    assert r.status == "no_match_verified"


def test_yonbip_get_token_with_query_template():
    """实测形态：GET getAccessToken，appKey/appSecret 走 query 参数。"""
    from datetime import datetime, timezone

    cfg = dict(YONBIP_CONFIG)
    auth = dict(cfg["auth"])
    auth.pop("payload", None)
    auth["method"] = "get"
    auth["path"] = "/iuap-api-auth/open-auth/selfAppAuth/getAccessToken"
    auth["query_template"] = {"appKey": "{username}", "appSecret": "{password}", "timestamp": ""}
    auth["path"] = "/open-auth/getAccessToken"
    cfg["auth"] = auth
    t = ScriptedTransport({
        "/open-auth/getAccessToken": (200, {"code": "200", "data": {"access_token": "AT-9"}}),
        "/yonbip/cpu/tenant/query": (200, {"code": "200", "data": []}),
    })
    client = SrmClient(cfg, SrmCredentials("MY-APP-KEY", "MY-SECRET"), transport=t)
    r = client.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "no_match_verified"
    tok = t.requests[0]
    assert tok.method == "GET"
    assert tok.params["appKey"] == "MY-APP-KEY"
    assert tok.params["appSecret"] == "MY-SECRET"
