"""基于规则的字段候选提取（仅生成候选，供交叉匹配与人工复核）。

模型可协助理解，但字段识别本身是确定性正则 + 表格标签关联；
低置信度（OCR）结果不会进入精确匹配（见 normalize_and_match）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import to_halfwidth, uscc_check_digit, USCC_CHARSET

# --------------------------------------------------------------- 基础正则

USCC_BODY_RE = re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NP-RT-UW-Y]{18}(?![0-9A-Z])")

_ID18_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_ID15_RE = re.compile(r"(?<!\d)[1-9]\d{5}\d{9}(?!\d)")

_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LANDLINE_RE = re.compile(r"(?<!\d)0\d{2,3}[-－ ]?\d{7,8}(?!\d)")
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)
_COMPANY_RE = re.compile(
    r"[一-龥A-Za-z0-9（）()]{4,40}?(?:股份有限公司|有限责任公司|有限公司|集团公司)"
)

_ID18_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID18_CHECK = "10X98765432"


def id18_valid(number: str) -> bool:
    if len(number) != 18 or not re.match(r"^\d{17}[\dXx]$", number):
        return False
    s = sum(int(c) * w for c, w in zip(number[:17], _ID18_WEIGHTS))
    return _ID18_CHECK[s % 11] == number[17].upper()


@dataclass
class FieldHit:
    field: str  # 见 FIELD_NAMES
    value: str  # 原始命中值（敏感项为完整原文，仅内存中使用；落盘前由 EvidenceBuilder 脱敏）
    context: str = ""  # 命中所依据的标签或说明
    confidence: float = 1.0
    via_label: bool = False


FIELD_NAMES = {
    "uscc",
    "legal_rep_id",
    "bid_agent_id",
    "id_number",
    "legal_rep_name",
    "bid_agent_name",
    "shareholder_name",
    "contact_name",
    "company_name",
    "phone",
    "email",
    "address",
    # 仅从投标文件封面页提取的公共项
    "project_name",
    "project_code",
    "tenderer",
    "bid_date",
}

# --------------------------------------------------------------- 标签 → 字段

_LABEL_FIELD: list[tuple[str, str]] = [
    (r"招标人|采购人|招标单位|采购单位", "tenderer"),
    (r"项目名称|采购项目名称|投标项目名称", "project_name"),
    (r"项目编号|项目编码|采购项目编号", "project_code"),
    (r"投标日期|投标时间|递交日期|递交时间", "bid_date"),
    (r"统一社会信用代码|统一社会代码|信用代码", "uscc"),
    (r"法定代表人(?:/负责人)?|法律代表人", "legal_rep_name"),
    (r"授权代表|委托代理人|被授权人|授权委托人|投标代表|经办人", "bid_agent_name"),
    (r"股东|出资人|投资人", "shareholder_name"),
    (r"联系人", "contact_name"),
    (r"(?:联系|办公|注册|通讯)?地址|住所", "address"),
    (r"联系电话|联系方式|电话|手机号?|移动电话|传真", "phone"),
    (r"电子邮箱|邮箱|电子邮件|Email|E-mail", "email"),
    (r"身份证号|身份证号码|证件号码|证件号", "id_number"),
    (r"供应商名称|投标人名称|公司名称|单位名称|供应商全称|投标单位", "company_name"),
]

_LABEL_RES = [(re.compile(r"^\s*(?:【?\d*[】.]?\s*)?" + pat + r"\s*[:：]?\s*$"), fld) for pat, fld in _LABEL_FIELD]
# 内联“标签：值”仅用于非人名字段；人名走更严格的 _NAME_LABELS
_LABEL_INLINE_RES = [
    (re.compile(r"(?:" + pat + r")\s*[:：]\s*([^\s，,。；;【】]{2,60})"), fld)
    for pat, fld in _LABEL_FIELD
    if fld not in ("legal_rep_name", "bid_agent_name", "shareholder_name", "contact_name")
]

_PURE_VALUE_RES: list[tuple[re.Pattern[str], str]] = [
    (USCC_BODY_RE, "uscc"),
    (_EMAIL_RE, "email"),
    (_MOBILE_RE, "phone"),
    (_LANDLINE_RE, "phone"),
    (_COMPANY_RE, "company_name"),
]

_NAME_CHARS = re.compile(r"^[一-龥·A-Za-z]{2,8}$")

# 人名提取的强排除：label 后紧跟的不是姓名（正文延续、动作词、标点起头等）
_NAME_FORBIDDEN_FIRST = set("在的了与及或并但为，。；：、（)0123456789")
_NAME_STOPWORDS = {
    "在开标", "在评标", "在澄清", "开标", "评标", "澄清", "签字", "签章", "盖章",
    "日期", "代表", "人员", "参加", "身份", "号码", "在场", "身份證", "事项",
    "身份证件号码", "身份证号码", "身份证号", "证件号码", "证件号", "号码",
}
# OCR/文本层中“法定代表人/负责人”后面经常紧接表格标题或正文，
# 这些词虽然形状像中文姓名，但不能作为人员实体。保持在字段提取层过滤，
# 避免后续主体比对产生大量 ID-003W 噪声。
_NAME_FALSE_POSITIVE_PARTS = {
    "身份证", "证件", "邮政", "编码", "负责", "代表", "委托", "授权", "证明",
    "审核", "审查", "实施", "方案", "项目", "工程", "汇报", "编制", "签字",
    "签章", "盖章", "职称", "终审", "进度", "鉴别", "报告", "执行",
}


def _clean_name(value: str) -> str:
    """去掉姓名值尾部的括号注记（如“李四（身份证：…）”）并校验形状。"""
    t = re.split(r"[（(]", value)[0].strip().strip("：:，, ")
    return t


def _plausible_name(name: str) -> bool:
    if not name or not _NAME_CHARS.match(name):
        return False
    if name in _NAME_STOPWORDS:
        return False
    if any(part in name for part in _NAME_FALSE_POSITIVE_PARTS):
        return False
    if name[0] in _NAME_FORBIDDEN_FIRST:
        return False
    return True


def scan_inline(text: str) -> list[FieldHit]:
    """扫描一段文本：先按“标签：值”配对，再匹配独立格式值。

    去重规则：同一字段同一值只保留一次（优先带标签的命中）；
    已被身份证命中的文本区间不再作为统一社会信用代码候选。
    """
    hits: list[FieldHit] = []
    occupied_spans: list[tuple[int, int]] = []
    seen_values: set[tuple[str, str]] = set()

    def overlaps(span: tuple[int, int]) -> bool:
        return any(not (span[1] <= s or span[0] >= e) for s, e in occupied_spans)

    def add(field: str, value: str, context: str, via: bool, conf: float = 1.0,
            span: tuple[int, int] | None = None, occupy: bool = False) -> None:
        key = (field, value.strip())
        if key in seen_values:
            return
        seen_values.add(key)
        hits.append(FieldHit(field=field, value=value.strip(), context=context, via_label=via, confidence=conf))
        if span and occupy:
            occupied_spans.append(span)

    # 1) 身份证最优先（避免被 USCC/其他格式误吃）
    for m in _ID18_RE.finditer(text):
        num = m.group(0)
        ctx = _id_context(text, m.start())
        if id18_valid(num) or ctx:
            fld = "bid_agent_id" if "授权" in (ctx or "") or "委托" in (ctx or "") else "id_number"
            add(fld, num, ctx or "18位身份证格式与校验位通过", bool(ctx), span=m.span(), occupy=True)
    for m in _ID15_RE.finditer(text):
        ctx = _id_context(text, m.start())
        if ctx:
            add("id_number", m.group(0), ctx, True, span=m.span(), occupy=True)

    # 2) 内联“标签：值”
    for rx, fld in _LABEL_INLINE_RES:
        for m in rx.finditer(text):
            value = m.group(1).strip()
            if fld in ("legal_rep_name", "bid_agent_name", "shareholder_name", "contact_name"):
                value = _clean_name(value)
                if not _NAME_CHARS.match(value):
                    continue
            add(fld, value, m.group(0)[:40], True)

    # 3) 独立格式值（跳过已被身份证占用的区间；跳过同值重复）
    for rx, fld in _PURE_VALUE_RES:
        for m in rx.finditer(text):
            if overlaps(m.span()):
                continue
            value = m.group(0)
            if fld == "uscc":
                # 形似身份证（18 位数字且通过身份证校验）的字符串不作 USCC 候选
                if _ID18_RE.fullmatch(value) and id18_valid(value):
                    continue
                if uscc_check_digit(value[:17]) != value[17]:
                    continue  # 校验位不合格：不产生候选（仍会在“标签：值”路径记录格式异常）
            add(fld, value, "格式匹配", False)

    # 4) 人名（严格标签）
    hits.extend(_name_after_label(text, seen_values))
    return hits


def _id_context(text: str, pos: int) -> str:
    window = text[max(0, pos - 20):pos]
    if re.search(r"身份证|证件号|身份證", window):
        if re.search(r"授权|委托|被授权", window):
            return "授权代表证件"
        return "身份证标签"
    return ""


_NAME_LABELS = {"legal_rep_name": re.compile(r"(法定代表人|负责人)\s*[:：]?\s*([一-龥·]{2,12})"),
                "bid_agent_name": re.compile(r"(授权代表|委托代理人|被授权人|授权委托人|投标代表|经办人)\s*[:：]?\s*([一-龥·]{2,12})"),
                "shareholder_name": re.compile(r"(股东|出资人|投资人)\s*[:：]?\s*([一-龥·A-Za-z0-9（）()]{2,30})"),
                "contact_name": re.compile(r"(联系人)\s*[:：]?\s*([一-龥·]{2,12})")}


def _name_after_label(text: str, seen_values: set) -> list[FieldHit]:
    hits = []
    occupied: list[tuple[int, int]] = []
    for fld, rx in _NAME_LABELS.items():
        for m in rx.finditer(text):
            name = m.group(2).strip()
            if not _plausible_name(name):
                continue
            span = m.span(2)
            if any(not (span[1] <= s or span[0] >= e) for s, e in occupied):
                continue
            if (fld, name) in seen_values:
                continue
            occupied.append(span)
            seen_values.add((fld, name))
            hits.append(FieldHit(field=fld, value=name, context=m.group(1), via_label=True))
    return hits


# --------------------------------------------------------------- 表格关联

_CELL_RE = re.compile(r"([A-Z]+)(\d+)")


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _num_to_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def extract_from_cells(cells: list[list[str]], sheet_name: str) -> list[FieldHit]:
    """XLSX 单元格提取：独立格式值 + 左/上邻标签关联。

    cells: [coordinate, text] 列表（text 已 strip、非空）。
    """
    by_coord = {coord: text for coord, text in cells}
    hits: list[FieldHit] = []
    used: set[str] = set()

    # 1) 标签单元格登记
    label_at: dict[str, str] = {}
    for coord, text in cells:
        for rx, fld in _LABEL_RES:
            if rx.match(text):
                label_at[coord] = fld
                break

    # 2) 纯格式值（USCC / email / phone / company / id）
    for coord, text in cells:
        local_hits = scan_inline(text)
        if local_hits:
            for h in local_hits:
                h.context = h.context or f"{sheet_name}!{coord}"
                hits.append(h)
                if h.field != "company_name":  # 公司名可能同格多条，不占用
                    used.add(coord)

    # 3) 标签 → 右邻/下邻值
    for coord, fld in label_at.items():
        m = _CELL_RE.match(coord)
        if not m:
            continue
        col, row = _col_to_num(m.group(1)), int(m.group(2))
        candidates = [f"{_num_to_col(col + 1)}{row}", f"{_num_to_col(col + 2)}{row}", f"{col}{row + 1}"]
        candidates[2] = f"{m.group(1)}{row + 1}"
        for cand in candidates:
            text = by_coord.get(cand)
            if not text:
                continue
            if cand in label_at:
                continue
            value = text.strip()
            if not value or len(value) > 80:
                continue
            sub = scan_inline(value)
            norm_fld = fld
            if fld == "id_number":
                for h in sub:
                    if h.field == "bid_agent_id":
                        norm_fld = "bid_agent_id"
                        break
            elif sub and any(h.field in ("legal_rep_name", "bid_agent_name", "shareholder_name", "contact_name", "company_name") for h in sub):
                norm_fld = next(h.field for h in sub if h.field in ("legal_rep_name", "bid_agent_name", "shareholder_name", "contact_name", "company_name"))
            hits.append(FieldHit(field=norm_fld, value=value, context=f"{sheet_name}!{coord}→{cand}", via_label=True, confidence=0.9))
            break
    return hits


def uscc_looks_valid(text: str) -> bool:
    t = to_halfwidth(text)
    m = USCC_BODY_RE.search(t)
    if not m:
        return False
    code = m.group(0)
    return uscc_check_digit(code[:17]) == code[17]
