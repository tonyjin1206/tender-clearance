#!/usr/bin/env python3
"""生成完全虚构、脱敏的测试项目 project-alpha（T01–T12 夹具）。

所有企业、人名、证件号、电话均为程序生成的虚构数据；
证件号为合法校验位的假号码。严禁使用真实投标文件。

场景矩阵：
- T01  三家供应商（A/B/C），字段分散在 XLSX 开标表与 PDF 授权书，各字段可回溯到坐标/页码；
- T02  A、B 的授权代表证件号完全相同（I 级 ID-002），C 不同；
- T03  A、B 的商务标 PDF 仅 Producer=WPS 且创建时间相近（III 级 META-003）；
- T04  A、B 扫描件含相同设备序列号（I 级候选 META-002），C 仅相同型号（III 级）；
- T05  D 与 A 名称相近但信用代码不同（仅候选/III，不合并记录）；
- T06  外部导入股权：王建国→(A,B) 已结束（历史）；赵敏→(B,C) 覆盖投标截止日（II 级 OWN-001）；
- T07  政采网命中 A（含禁入期）；信用中国导入 status=blocked；军采网无文件（not_queried）；
- T08  司法同名案件、无信用代码（unconfirmed，最高 III，不归属）；
- T09  A 扫描件 XMP mock-OCR 出错位信用代码（低置信度 → 不参与匹配，进人工核对）；
- T10  标书内含完整证件号/手机号（输出必须只有掩码/摘要）；
- T11  密码保护 / 损坏 / 零字节文件（不崩溃，记录原因）；
- T12  由测试运行两次对比（固定输入 → 结果一致）。
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import warnings

import fitz  # noqa: N813  (pymupdf 旧名，仅测试夹具使用)

warnings.filterwarnings("ignore", message=".*deprecated.*")
from PIL import Image
from PIL import Image as PILImage

FIXTURE_ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------- 虚构数据

BID_DEADLINE = datetime(2026, 8, 31, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))

ID17_BASE = {
    "agent_shared": "11010119920312061",   # A、B 授权代表（同一个人，虚构）
    "agent_c": "44030119950722033",        # C 授权代表
    "legal_a": "11010119750314861",        # 各方法定代表人
    "legal_b": "31010419721209552",
    "legal_c": "44030519800817637",
    "legal_d": "11010119760929829",
}

SUPPLIERS = [
    {
        "dir": "huaxin-chuangyuan",
        "name": "北京华信创远科技有限公司",
        "uscc17": "91110108MA01CWY70",      # + 自动校验位
        "legal": "钱建国", "legal_id17": ID17_BASE["legal_a"],
        "agent": "张伟", "agent_id17": ID17_BASE["agent_shared"],
        "phone": "13801380001",
        "landline": "010-62001101",
        "email": "bid@huaxin-cy-example.cn",
        "address": "北京市海淀区北清路68号院1号楼12层1201",
        "contact": "刘敏",
        "author": "华信投标一组",
        "editor": "华信商务组",
    },
    {
        "dir": "zhongheng-taida",
        "name": "中恒泰达（北京）工程管理有限公司",
        "uscc17": "91110105MA02RTB41",
        "legal": "孙立军", "legal_id17": ID17_BASE["legal_b"],
        "agent": "张伟", "agent_id17": ID17_BASE["agent_shared"],
        "phone": "13901390002",
        "landline": "010-83002202",
        "email": "zhengbu@zhongheng-td-example.cn",
        "address": "北京市朝阳区建国路88号院3号楼1702",
        "contact": "赵倩",
        "author": "中恒经营部",
        "editor": "中恒商务组",
    },
    {
        "dir": "yuntu-zhilian",
        "name": "云图智联信息技术有限公司",
        "uscc17": "91440306MA03KLM26",
        "legal": "周海涛", "legal_id17": ID17_BASE["legal_c"],
        "agent": "陈晓峰", "agent_id17": ID17_BASE["agent_c"],
        "phone": "13602580003",
        "landline": "0755-33003303",
        "email": "toubiao@yuntu-zl-example.cn",
        "address": "深圳市南山区科技园南区高新南一道15号",
        "contact": "林芳",
        "author": "云图方案部",
        "editor": "云图商务组",
    },
    {
        "dir": "huaxin-info",
        "name": "北京华信创远信息技术有限公司",
        "uscc17": "91110108MA01CYY98",
        "legal": "马文斌", "legal_id17": ID17_BASE["legal_d"],
        "agent": "高翔", "agent_id17": "11010119881104451",
        "phone": "13801380004",
        "landline": "010-62004404",
        "email": "bid@huaxin-info-example.cn",
        "address": "北京市海淀区上地信息路2号创业园D座8层808",
        "contact": "许静",
        "author": "华信信息投标部",
        "editor": "华信信息商务组",
    },
]

PROJECT = {
    "project_id": "PRJ-DEMO-2026-001",
    "project_name": "虚构示范项目：园区综合运维服务采购",
    "bid_deadline": "2026-08-31T09:00:00+08:00",
    "run_date": "2026-09-09",
    "supplier_directory_mapping": {},
    "procurement_rules_source": "unspecified",
    "external_query_mode": "offline",
    "external_query_sources": [],
    "redaction_mode": "standard",
    "retention_policy": "project_local",
    "ocr_provider": "mock",
}


# --------------------------------------------------------------- 工具函数

USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
ID_CHECK = "10X98765432"
ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def uscc_check(code17: str) -> str:
    total = sum(USCC_CHARSET.find(c) * pow(3, i, 31) for i, c in enumerate(code17))
    return USCC_CHARSET[(31 - total % 31) % 31]


def uscc_of(code17: str) -> str:
    return code17 + uscc_check(code17)


def id18_of(id17: str) -> str:
    s = sum(int(c) * w for c, w in zip(id17, ID_WEIGHTS))
    return id17 + ID_CHECK[s % 11]


def pdf_date(dt: datetime) -> str:
    return "D:" + dt.strftime("%Y%m%d%H%M%S") + "+08'00'"


def set_xmp(doc: fitz.Document, xml: str) -> None:
    doc.set_xml_metadata(xml)


# --------------------------------------------------------------- 文件生成


def make_xlsx(path: Path, s: dict, created: datetime) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "开标一览表"
    rows = [
        ["开标一览表", ""],
        ["项目名称", PROJECT["project_name"]],
        ["供应商名称", s["name"]],
        ["统一社会信用代码", uscc_of(s["uscc17"])],
        ["法定代表人", s["legal"]],
        ["授权代表", s["agent"]],
        ["联系电话", s["phone"]],
        ["固定电话", s["landline"]],
        ["电子邮箱", s["email"]],
        ["注册地址", s["address"]],
        ["联系人", s["contact"]],
        ["投标报价（元）", {"huaxin-chuangyuan": "4860000", "zhongheng-taida": "4720000",
                            "yuntu-zhilian": "5150000", "huaxin-info": "4990000"}[s["dir"]]],
        ["服务期", "三年"],
    ]
    for r, row in enumerate(rows, start=1):
        ws.cell(row=r, column=1, value=row[0])
        ws.cell(row=r, column=2, value=row[1])
    props = wb.properties
    props.creator = s["editor"]
    props.lastModifiedBy = "审核-" + s["dir"][:6]
    props.created = created
    props.modified = created + timedelta(hours=3)
    props.title = f"{s['name']}投标文件-开标一览表"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def make_bid_pdf(path: Path, s: dict, created: datetime, producer: str, with_agent_id: bool = True) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    text = f"""授 权 委 托 书

本授权委托书声明：我 {s['legal']} 系 "{s['name']}" 的法定代表人，
现授权委托本单位在职职工 {s['agent']} 作为我方授权代表，
以我方名义参加 "{PROJECT['project_name']}"（项目编号：{PROJECT['project_id']}）的投标活动。
授权代表在开标、评标、澄清和合同谈判过程中所签署的一切文件和处理与之有关的一切事务，
我方均予以承认，并承担相应的法律责任。

授权代表身份证件号码：{id18_of(s["agent_id17"]) if with_agent_id else "（见随附证件复印件）"}
法定代表人身份证件号码：{id18_of(s["legal_id17"])}

法定代表人（签字）：{s['legal']}
授权代表（签字）：{s['agent']}

供应商名称（盖章）：{s['name']}
统一社会信用代码：{uscc_of(s['uscc17'])}
注册地址：{s['address']}
联系电话：{s['landline']}；{s['phone']}
电子邮箱：{s['email']}

日期：2026 年 8 月 {created.day} 日
"""
    page.insert_textbox(fitz.Rect(50, 60, 545, 700), text, fontsize=11, fontname="china-s")
    doc.set_metadata({
        "creator": producer,
        "producer": producer,
        "author": s["author"],
        "title": f"{PROJECT['project_name']} 投标文件（商务部分）",
        "creationDate": pdf_date(created),
        "modDate": pdf_date(created + timedelta(hours=1)),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def make_tech_docx(path: Path, s: dict, created: datetime) -> None:
    import docx

    d = docx.Document()
    d.add_heading(f"{PROJECT['project_name']} 技术方案", level=1)
    d.add_paragraph(f"供应商：{s['name']}")
    d.add_paragraph("本项目服务团队由项目经理 1 名、运维工程师 4 名组成，提供 7×24 小时响应。")
    d.add_paragraph(f"服务承诺：一般故障 2 小时内响应，重大故障 30 分钟内到位。")
    d.add_paragraph(f"联系人：{s['contact']}，电话：{s['phone']}")
    core = d.core_properties
    core.author = s["author"]
    core.last_modified_by = s["author"]
    core.title = f"{PROJECT['project_name']} 技术方案"
    core.created = created
    core.modified = created + timedelta(days=2)
    core.comments = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))


def _exif_bytes(make_: str, model: str, serial: str) -> bytes:
    img = PILImage.new("RGB", (400, 560), (235, 235, 235))
    exif = img.getexif()
    exif[0x010F] = make_
    exif[0x0110] = model
    exif[0x0131] = "NScan 2.1"
    ifd = exif.get_ifd(0x8769)
    ifd[0xA431] = serial
    ifd[0x9003] = "2026:08:20 10:15:00"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes(), quality=82)
    return buf.getvalue()


def make_scan_pdf(path: Path, s: dict, serial: str, model: str, ocr_text: str | None,
                  ocr_confidence: float | None) -> None:
    """扫描件：整页图片、无文本层；设备序列号写入图片 EXIF；可选 mock OCR 文本进 XMP。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    jpeg = _exif_bytes("Canon", model, serial)
    page.insert_image(fitz.Rect(60, 40, 535, 800), stream=jpeg)
    xmp = f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about="" xmlns:tiff="http://ns.adobe.com/tiff/1.0/">
      <tiff:Make>Canon</tiff:Make>
      <tiff:Model>{model}</tiff:Model>
      <aux:SerialNumber="{serial}" xmlns:aux="http://ns.adobe.com/exif/1.0/aux/"/>
    </rdf:Description>
"""
    if ocr_text:
        xmp += (f'    <rdf:Description rdf:about="" xmlns:tender="https://tender-clearance.example/ns/">\n'
                f'      <tender:ocr page="1" confidence="{ocr_confidence}">{ocr_text}</tender:ocr>\n'
                f'    </rdf:Description>\n')
    xmp += """  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    set_xmp(doc, xmp)
    doc.set_metadata({
        "creator": model,
        "producer": model,
        "author": "",
        "creationDate": pdf_date(datetime(2026, 8, 20, 10, 15, 0)),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def make_procurement_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    text = f"""{PROJECT['project_name']} 采购文件（节选，虚构）

项目编号：{PROJECT['project_id']}

第 5.2 条 资格要求：投标人须为在中华人民共和国境内注册的独立法人，
持有有效统一社会信用代码的营业执照；未被列入失信被执行人、
重大税收违法案件当事人名单、政府采购严重违法失信行为记录名单。

第 5.3 条 否决条款：经查实投标人与其他投标人存在串通投标情形的，
其投标将被否决。（本文件为虚构演示数据。）
"""
    page.insert_textbox(fitz.Rect(50, 60, 545, 780), text, fontsize=11, fontname="china-s")
    doc.set_metadata({
        "creator": "Microsoft Word 16.0",
        "producer": "Microsoft® Word 2019",
        "author": "采购中心",
        "title": f"{PROJECT['project_name']} 采购文件",
        "creationDate": pdf_date(datetime(2026, 8, 1, 14, 0, 0)),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def make_broken_files(base: Path) -> None:
    c = base / "bids" / "yuntu-zhilian"
    c.mkdir(parents=True, exist_ok=True)
    # 密码保护
    doc = fitz.open()
    doc.new_page()
    doc.save(str(c / "附件-授权书加密版.pdf"), encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="demo", user_pw="demo")
    doc.close()
    # 损坏
    (c / "历史业绩损坏文件.pdf").write_bytes(b"%PDF-1.7\n%broken for fixture" + b"\x00" * 200)
    # 零字节
    (c / "营业执照空文件.pdf").write_bytes(b"")


# --------------------------------------------------------------- 外部证据


def make_external_evidence(base: Path) -> None:
    uscc_a = uscc_of(SUPPLIERS[0]["uscc17"])
    uscc_b = uscc_of(SUPPLIERS[1]["uscc17"])
    uscc_c = uscc_of(SUPPLIERS[2]["uscc17"])
    uscc_d = uscc_of(SUPPLIERS[3]["uscc17"])

    # T07：政府采购严重违法失信（命中 A，禁入期覆盖投标截止日）
    gov = {
        "channel": "government_procurement",
        "supplier_directory": "huaxin-chuangyuan",
        "uscc": uscc_a,
        "queried_at": "2026-09-01T10:00:00+08:00",
        "query_mode": "manual_import",
        "querier": "王审（合规部）",
        "source_url": "https://www.ccgp.gov.cn/（人工查询，虚构示例）",
        "status": "match",
        "records": [
            {
                "record_kind": "dishonesty",
                "subject_uscc": uscc_a,
                "fields": {
                    "当事人": "北京华信创远科技有限公司（虚构）",
                    "行为": "提供虚假材料谋取中标（虚构示例）",
                    "处罚": "列入不良行为记录名单，禁止参加政府采购活动",
                    "处罚决定文号": "财采罚〔2026〕DEMO-001号",
                    "依据": "《政府采购法》第七十七条（示例）",
                    "处理日期": "2026-01-12",
                },
                "effective_from": "2026-01-12",
                "effective_to": "2027-01-11",
            }
        ],
    }
    _write(base / "external-evidence/government-procurement/gov-hit-A.json", gov)

    # T06：股权关系（含历史关系与有效关系）——来源：SRM 企业画像授权导出
    own = {
        "channel": "srm",
        "query_mode": "manual_import",
        "queried_at": "2026-09-01T11:00:00+08:00",
        "querier": "王审（合规部）",
        "source_url": "国家企业信用信息公示系统（人工查询，虚构示例）",
        "status": "match",
        "records": [],
        "ownership": [
            {
                "from_party_name": "王建国",
                "from_party_id_number": id18_of("11010119700101411"),
                "to_company_name": "北京华信创远科技有限公司",
                "to_company_uscc": uscc_a,
                "share_ratio": 60,
                "relation_date": "2018-03-01",
                "relation_end_date": "2025-06-30",
            },
            {
                "from_party_name": "王建国",
                "from_party_id_number": id18_of("11010119700101411"),
                "to_company_name": "中恒泰达（北京）工程管理有限公司",
                "to_company_uscc": uscc_b,
                "share_ratio": 55,
                "relation_date": "2019-01-01",
                "relation_end_date": "2024-12-31",
            },
            {
                "from_party_name": "赵敏",
                "from_party_id_number": id18_of("11010119820325662"),
                "to_company_name": "中恒泰达（北京）工程管理有限公司",
                "to_company_uscc": uscc_b,
                "share_ratio": 51,
                "relation_date": "2021-05-01",
            },
            {
                "from_party_name": "赵敏",
                "from_party_id_number": id18_of("11010119820325662"),
                "to_company_name": "云图智联信息技术有限公司",
                "to_company_uscc": uscc_c,
                "share_ratio": 60,
                "relation_date": "2020-08-01",
            },
        ],
    }
    _write(base / "external-evidence/srm-authorized-export/ownership-demo.json", own)

    # T08：同名司法案件，无信用代码（不归属）——来源：SRM 授权导出
    jd = {
        "channel": "srm",
        "query_mode": "manual_import",
        "queried_at": "2026-09-01T14:00:00+08:00",
        "querier": "李查（法务）",
        "status": "match",
        "records": [
            {
                "record_kind": "judicial_case",
                "subject_name": "北京华信创远科技有限公司",
                "fields": {
                    "案号": "（2025）京0108民初DEMO888号",
                    "法院": "北京市海淀区人民法院",
                    "案由": "买卖合同纠纷",
                    "当事人": "北京华信创远科技有限公司（同名待核）",
                    "立案日期": "2025-11-20",
                }
            }
        ],
    }
    _write(base / "external-evidence/srm-authorized-export/judicial-samename.json", jd)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------- 主流程


def build(root: Path = FIXTURE_ROOT) -> Path:
    base = root / "project-alpha"
    # 清理旧输出
    import shutil

    if base.exists():
        shutil.rmtree(base)
    (base / "procurement").mkdir(parents=True)
    (base / "output").mkdir()

    cfg = dict(PROJECT)
    cfg["supplier_directory_mapping"] = {s["dir"]: s["name"] for s in SUPPLIERS}
    import yaml as _yaml
    (base / "project.yaml").write_text(
        "# 测试项目配置（全部为虚构数据）\n" + _yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    make_procurement_pdf(base / "procurement" / "采购文件.pdf")

    # A、B 商务标：Producer=WPS，创建时间相近（T03）；C、D 不同工具与时间
    times = {
        "huaxin-chuangyuan": datetime(2026, 8, 28, 9, 10, 0),
        "zhongheng-taida": datetime(2026, 8, 28, 9, 35, 0),
        "yuntu-zhilian": datetime(2026, 8, 26, 15, 40, 0),
        "huaxin-info": datetime(2026, 8, 27, 11, 5, 0),
    }
    producers = {
        "huaxin-chuangyuan": "WPS Office",
        "zhongheng-taida": "WPS Office",
        "yuntu-zhilian": "Microsoft Word 16.0",
        "huaxin-info": "LibreOffice 7.6",
    }
    for s in SUPPLIERS:
        d = base / "bids" / s["dir"]
        make_xlsx(d / "开标一览表.xlsx", s, times[s["dir"]] - timedelta(days=1))
        make_bid_pdf(d / "商务标-授权书.pdf", s, times[s["dir"]], producers[s["dir"]])
        make_tech_docx(d / "技术标.docx", s, times[s["dir"]] - timedelta(days=2))

    # T04：扫描件 —— A、B 相同序列号；C 仅相同型号；D 独立序列号+型号
    make_scan_pdf(base / "bids/huaxin-chuangyuan/营业执照扫描件.pdf",
                  SUPPLIERS[0], serial="CNFJ1234567", model="iR-ADV C3320",
                  ocr_text=None, ocr_confidence=None)
    make_scan_pdf(base / "bids/zhongheng-taida/营业执照扫描件.pdf",
                  SUPPLIERS[1], serial="CNFJ1234567", model="iR-ADV C3320",
                  ocr_text=None, ocr_confidence=None)
    # T09：C 的扫描件 XMP mock-OCR 输出错误位信用代码（低置信度）
    bad_uscc = list(uscc_of(SUPPLIERS[2]["uscc17"]))
    bad_uscc[-1] = "0" if bad_uscc[-1] != "0" else "1"
    make_scan_pdf(base / "bids/yuntu-zhilian/资质证书扫描件.pdf",
                  SUPPLIERS[2], serial="CNQZ7654321", model="iR-ADV C3320",
                  ocr_text=f"资质证书 统一社会信用代码：{''.join(bad_uscc)} 有效期至2029-08-01",
                  ocr_confidence=0.55)
    make_scan_pdf(base / "bids/huaxin-info/营业执照扫描件.pdf",
                  SUPPLIERS[3], serial="CNFY2468135", model="iR-ADV C5550",
                  ocr_text=None, ocr_confidence=None)

    make_broken_files(base)
    make_external_evidence(base)
    return base


if __name__ == "__main__":
    p = build()
    print(f"fixtures ready → {p}")
