"""外部证据与查询测试：T07 / T08 / 适配器状态机（测试替身）。"""

from __future__ import annotations

import json

import pytest


def test_t07_channel_statuses_truthful(alpha_findings, alpha_project):
    """T07：政采命中（含禁入期）；SRM 未启用查询时如实 not_queried；不显示“无失信”。"""
    cov = {(c["supplier_id"], c["source_id"]): c for c in alpha_findings["coverage"]}
    sup = {s["directory_name"]: s["supplier_id"]
           for s in json.loads((alpha_project / "output/interim/entities.json").read_text(encoding="utf-8"))["suppliers"]}
    a = sup["huaxin-chuangyuan"]
    assert cov[(a, "government_procurement")]["status"] == "match"
    # SRM 渠道有同名主体的导入记录（无信用代码）→ 如实要求人工确认，而非 not_queried
    assert cov[(a, "srm")]["status"] == "needs_manual_review"
    # 渠道收缩后：不存在已移除渠道的覆盖记录
    removed = {"creditchina", "military_procurement", "public_corporate", "judicial"}
    assert not any(sid in removed for _, sid in cov)

    dis = [f for f in alpha_findings["findings"] if f["rule_id"] == "DIS-003"]
    assert dis and dis[0]["level"] == "II"
    assert "2026-01-12" in dis[0]["fact"] and "2027-01-11" in dis[0]["fact"]
    # 采购条款未提供 → 资格影响待采购人确认
    assert dis[0]["procurement_clause_note"] and "待采购人确认" in dis[0]["procurement_clause_note"]

    # 不得出现“无失信/未发现失信”结论
    for f in alpha_findings["findings"]:
        assert "无失信" not in f["fact"]
        assert "未发现失信" not in f["fact"]


def test_t08_samename_judicial_not_attributed(alpha_findings):
    """T08：同名司法案件无信用代码 → 待确认、最高 III 级、不归属供应商。"""
    jud = [f for f in alpha_findings["findings"] if f["rule_id"] == "JUD-001"]
    assert jud
    f = jud[0]
    assert f["subject_confirmation"] == "unconfirmed"
    assert f["supplier_ids"] == []
    assert f["level"] == "III"
    assert f["status"] == "human_review_required"


def test_offline_mode_never_touches_network(alpha_project):
    """offline 模式：全部渠道 not_queried（导入命中的除外），无任何网络语义。"""
    import subprocess
    import sys
    import tempfile
    import shutil
    from pathlib import Path

    from tc.pipeline import run_stage

    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "p"
        shutil.copytree(alpha_project, dst)
        shutil.rmtree(dst / "output")
        (dst / "output").mkdir()
        for stage in ("inventory.py", "extract_content.py", "extract_metadata.py",
                      "normalize_and_match.py", "import_external_evidence.py", "query_sources.py",
                      "assess_risk.py"):
            run_stage(stage, [], dst)
        ext = json.loads((dst / "output/interim/external.json").read_text(encoding="utf-8"))
        live = [q for q in ext["queries"] if q["query_mode"] in ("official_api", "public_web")]
        assert live == []


class FakeTransport:
    """测试替身：模拟渠道响应，验证适配器状态机（接收 HttpRequest）。"""

    def __init__(self, status_code=200, body=""):
        self.status_code = status_code
        self.body = body
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        return self.status_code, self.body


def _adapter(transport, endpoint="https://example.invalid/search", **kw):
    from tc.sources import HttpSourceAdapter, SourceConfig

    cfg = SourceConfig(
        source_id="government_procurement",
        label="测试渠道",
        query_mode="public_web",
        endpoint=endpoint,
        params_name="kw",
        record_pattern=r"<record>行为=(?P<行为>[^<]+)</record>",
        no_match_marker="没有找到",
        blocked_indicators=["captcha", "验证码"],
        **kw,
    )
    return HttpSourceAdapter(cfg, transport=transport)


# 与政采网 /cr/list 相同结构的合成 HTML（企业/代码均为虚构）
_CCGP_ROW = (
    '<tr class="trShow"><td>{seq}</td>'
    '<td><a href="javascript:void(0);"><font color="blue">{name}</font></a></td>'
    '<td>{uscc}</td><td>{addr}</td>'
    '<td><p title="{act}">{act}</p></td>'
    '<td><p title="{penalty}">{penalty}</p></td>'
    '<td><p title="{basis}">{basis}</p></td>'
    '<td>2026-08-12</td><td>2026-09-07 15:56</td><td>虚构市财政局</td></tr>'
)
_CCGP_PAGE = (
    "<html><body><table id=\"tableInfo\"><tr><th>序号</th></tr>" + _CCGP_ROW + "</table>"
    "<div class='alert_info'>查询结果： 政府采购严重违法失信行为记录名单中没有该企业的相关记录</div></body></html>"
)


def _ccgp_adapter(transport):
    from tc.sources import HttpSourceAdapter, SourceConfig

    cfg = SourceConfig(
        source_id="government_procurement",
        label="中国政府采购网（测试）",
        query_mode="public_web",
        endpoint="https://www.ccgp.gov.cn/cr/list",
        method="POST",
        params_name="orgName",
        params_uscc="orgCode",
        form_fields={"orgName": "{name}", "orgCode": "{uscc}", "enforceUnit": "",
                     "punishTime": "", "punishTimeMax": ""},
        page_param="gp",
        pages=1,
        response_type="html_table",
        row_pattern='<tr class="trShow">(.*?)</tr>',
        field_map=["序号", "企业名称", "统一社会信用代码", "企业地址", "行为情形",
                   "处罚结果", "处罚依据", "处罚日期", "公布日期", "执法单位"],
        blocked_indicators=["captcha", "验证码", "请登录", "访问频率"],
        no_match_marker="没有该企业的相关记录",
        rate_limit_seconds=0.0,
    )
    return HttpSourceAdapter(cfg, transport=transport)


def test_ccgp_table_match_and_form_rendering():
    """政采网：POST 表单按 {name}/{uscc} 渲染；命中行按列名解析；信用代码一致 → confirmed。"""
    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    body = _CCGP_PAGE.format(
        seq=1, name="虚构测试有限公司", uscc="91350100M000100Y43",
        addr="虚构省虚构市虚构路1号", act="提供虚假材料谋取中标（测试）",
        penalty="罚款1元（测试）", basis="《测试依据》第一条",
    )
    t = FakeTransport(200, body)
    r = _ccgp_adapter(t).query(
        QuerySubject("S1", "虚构测试有限公司", "91350100M000100Y43"), datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.parsed_count == 1
    rec = r.records[0]
    assert rec.fields["企业名称"] == "虚构测试有限公司"
    assert rec.fields["统一社会信用代码"] == "91350100M000100Y43"
    assert rec.fields["处罚结果"].startswith("罚款1元")
    assert rec.subject_confirmation == "confirmed"
    req = t.requests[0]
    assert req.method == "POST"
    assert req.form["orgCode"] == "91350100M000100Y43"
    assert req.form["orgName"] == "虚构测试有限公司"
    assert req.form["gp"] == "1"


def test_ccgp_table_name_only_is_candidate():
    """政采网：仅按名称查询时，命中记录为候选（同名不得自动归属）。"""
    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    body = _CCGP_PAGE.format(
        seq=1, name="同名不同码测试有限公司", uscc="91350100M000100Y43",
        addr="虚构省虚构市虚构路2号", act="测试行为", penalty="测试处罚", basis="《测试》",
    )
    t = FakeTransport(200, body)
    r = _ccgp_adapter(t).query(
        QuerySubject("S1", "同名不同码测试有限公司", None), datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.records[0].subject_confirmation == "candidate"


def test_ccgp_table_no_match_verified():
    """政采网：页面明示“没有该企业的相关记录” → no_match_verified。"""
    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    body = (
        "<html><body><table id=\"tableInfo\"></table>"
        "<div>查询结果： 政府采购严重违法失信行为记录名单中没有该企业的相关记录</div></body></html>"
    )
    t = FakeTransport(200, body)
    r = _ccgp_adapter(t).query(
        QuerySubject("S1", "不存在的公司XYZQ", "91350100M000100Y43"), datetime.now(timezone.utc))
    assert r.status == "no_match_verified"



def test_adapter_match_parses_records():
    t = FakeTransport(200, "<record>行为=提供虚假材料</record><record>行为=串通投标</record>")
    from datetime import datetime, timezone

    r = _adapter(t).query(__import__("tc.sources", fromlist=["QuerySubject"]).QuerySubject("S1", "某公司", "91350100M000100Y43"), datetime.now(timezone.utc))
    assert r.status == "match"
    assert r.parsed_count == 2
    assert len(t.requests) == 1


def test_adapter_captcha_blocked():
    t = FakeTransport(200, "请输入 captcha 验证码")
    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    r = _adapter(t).query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "blocked"


def test_adapter_403_blocked():
    t = FakeTransport(403, "forbidden")
    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    r = _adapter(t).query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "blocked"


def test_adapter_network_failed():
    def boom(url, params):
        raise ConnectionError("network down")

    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    r = _adapter(boom).query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "failed"


def test_adapter_no_match_verified():
    t = FakeTransport(200, "查询结果：没有找到相关记录")
    from datetime import datetime, timezone
    from tc.sources import QuerySubject

    r = _adapter(t).query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "no_match_verified"


def test_adapter_without_endpoint_needs_manual_review():
    from datetime import datetime, timezone
    from tc.sources import QuerySubject, SourceConfig, HttpSourceAdapter

    cfg = SourceConfig(source_id="judicial", label="司法")
    r = HttpSourceAdapter(cfg, transport=FakeTransport()).query(
        QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "needs_manual_review"


def test_adapter_name_only_subject_needs_manual_review_for_uscc_only_channel():
    """仅支持按信用代码查询的渠道：只有名称时不得把同名结果自动归属。"""
    from datetime import datetime, timezone
    from tc.sources import QuerySubject, SourceConfig, HttpSourceAdapter

    cfg = SourceConfig(source_id="x", label="x", query_mode="official_api",
                       endpoint="https://example.invalid/api", params_name="", params_uscc="uscc",
                       record_pattern=r"x")
    r = HttpSourceAdapter(cfg, transport=FakeTransport()).query(
        QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "needs_manual_review"


def test_srm_adapter_default_needs_config_and_never_touches_network():
    """SRM 默认（auth.mode=none）：needs_manual_review 且零网络访问。"""
    from datetime import datetime, timezone
    from tc.sources import QuerySubject, SrmAdapter

    def boom(req):
        raise AssertionError("SRM 未配置时不得发起任何网络访问")

    a = SrmAdapter(client=__import__("tc.srm", fromlist=["SrmClient"]).SrmClient({}, __import__("tc.srm", fromlist=["SrmCredentials"]).SrmCredentials(), transport=boom))
    r = a.query(QuerySubject("S1", "某公司", None), datetime.now(timezone.utc))
    assert r.status == "needs_manual_review"


def test_csv_import_contract(tmp_path, alpha_project):
    """CSV 导入：必需列 record_kind；其余列进入 fields；无信用代码主体不归属。"""
    import shutil
    from pathlib import Path

    from tc.pipeline import run_stage

    dst = tmp_path / "csv-proj"
    shutil.copytree(alpha_project, dst)

    csv_path = dst / "external-evidence/government-procurement/gov-csv-hit.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "record_kind,subject_name,行为,处罚决定文号,处罚日期,queried_at\n"
        "dishonesty,中恒泰达（北京）工程管理有限公司,提供虚假材料（演示）,政采处〔2026〕1号,2026-05-01,2026-09-01\n",
        encoding="utf-8",
    )
    run_stage("import_external_evidence.py", [], dst)
    run_stage("query_sources.py", [], dst)
    run_stage("assess_risk.py", [], dst)
    ext = json.loads((dst / "output/interim/external.json").read_text(encoding="utf-8"))
    gov = [r for r in ext["records"] if r["source_id"] == "government_procurement"
           and r["subject_confirmation"] == "candidate"]
    assert gov, "CSV 导入未生效"
    # 同名 → 候选，不归属（supplier_id 置空）
    assert gov[0]["supplier_id"] is None
    assert gov[0]["subject_confirmation"] == "candidate"
    assert gov[0]["fields"]["处罚决定文号"] == "政采处〔2026〕1号"
    findings = json.loads((dst / "output/interim/findings.json").read_text(encoding="utf-8"))
    dis3 = [f for f in findings["findings"] if f["rule_id"] == "DIS-003"
            and f["subject_confirmation"] != "confirmed"]
    assert dis3 and dis3[0]["level"] == "III" and dis3[0]["supplier_ids"] == []
