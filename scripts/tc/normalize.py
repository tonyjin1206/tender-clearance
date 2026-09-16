"""规范化函数：企业名称、统一社会信用代码、联系方式、地址、证件号。

原则（方案 5.3）：
- 保留原文，只派生“检索键 / 比对键”；名称相似只是候选匹配。
- 统一社会信用代码校验长度与校验位，不合格记“格式异常”，不自动纠正。
- 证件号提供受控比对摘要与展示掩码；最终输出是否保留原值由项目 `redaction_mode`
  和调用方授权决定，日志与异常仍不得包含凭据。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime


def to_halfwidth(text: str) -> str:
    """全角转半角、兼容字符规范化（NFKC），并统一空白。"""
    t = unicodedata.normalize("NFKC", text)
    return t.replace("　", " ")


# ------------------------------------------------------ 统一社会信用代码 USCC

USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
# GB 32100-2015：Wi = 3^(i-1) mod 31
USCC_WEIGHTS = [pow(3, i, 31) for i in range(17)]
USCC_RE = re.compile(r"[0-9A-HJ-NP-RT-UW-Y]{18}")

_ORG_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司",
    "（集团）", "(集团)",
)


def clean_uscc(raw: str | None) -> str:
    """去除空格与常见混淆字符，统一大写。不纠正内容。"""
    if not raw:
        return ""
    t = to_halfwidth(raw).upper()
    t = t.replace(" ", "").replace("\t", "")
    return t


def uscc_check_digit(code17: str) -> str | None:
    """按 GB 32100-2015 计算第 18 位校验字符。输入为前 17 位。"""
    if len(code17) != 17:
        return None
    total = 0
    for ch, w in zip(code17, USCC_WEIGHTS):
        v = USCC_CHARSET.find(ch)
        if v < 0:
            return None
        total += v * w
    return USCC_CHARSET[(31 - total % 31) % 31]


def validate_uscc(raw: str) -> tuple[str, bool, str]:
    """返回 (规范值, 校验是否通过, 状态说明)。"""
    code = clean_uscc(raw)
    if not code:
        return "", False, "absent"
    if len(code) != 18:
        return code, False, "length_error"
    if any(USCC_CHARSET.find(ch) < 0 for ch in code):
        return code, False, "charset_error"
    expect = uscc_check_digit(code[:17])
    if expect is None or expect != code[17]:
        return code, False, "check_digit_error"
    return code, True, "valid"


def uscc_valid(code: str) -> bool:
    return validate_uscc(code)[1]


# ------------------------------------------------------------------ 企业名称

_ORG_FORM_RE = re.compile(
    r"(股份有限公司|有限责任公司|有限公司|公司|合伙企业（普通合伙）|"
    r"合伙企业（有限合伙）|普通合伙|特殊普通合伙|事务所|集团)$"
)


def normalize_company_name(raw: str | None) -> str:
    """名称比对键：全角半角、去空白、统一大写括号。保留原文由调用方负责。"""
    if not raw:
        return ""
    t = to_halfwidth(raw)
    t = re.sub(r"\s+", "", t)
    return t.strip()


def company_search_key(raw: str | None) -> str:
    """检索键：在规范化基础上去掉常见组织形式后缀与地区前缀提示（仅用于候选检索）。"""
    t = normalize_company_name(raw)
    for _ in range(3):
        m = _ORG_FORM_RE.search(t)
        if m:
            t = t[: m.start()]
        else:
            break
    # 去掉常见括号注记
    t = re.sub(r"[（(][^）)]*[）)]$", "", t)
    return t


def name_similarity(a: str, b: str) -> float:
    """0-1 的简单相似度：最长公共子序列比例；核心名包含关系按高相似处理。

    仅用于产生候选线索，不用于认定同一主体。
    """
    ka, kb = company_search_key(a), company_search_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    # 核心名包含（如“华信创远” ⊂ “华信创远信息技术”）：强候选信号
    if (ka in kb or kb in ka) and min(len(ka), len(kb)) / max(len(ka), len(kb)) >= 0.4:
        return 0.9
    m, n = len(ka), len(kb)
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            if ka[i - 1] == kb[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev = cur
    lcs = prev[n]
    return (2 * lcs) / (m + n)


# ------------------------------------------------------------------ 联系方式

_PHONE_STRIP_RE = re.compile(r"[\s\-－–—‐\.（）()]")


def normalize_phone(raw: str | None) -> tuple[str, str]:
    """返回 (主号, 分机)。手机号/固话去分隔符；分机单列。"""
    if not raw:
        return "", ""
    t = to_halfwidth(raw)
    m = re.search(r"(?:转|分机|ext\.?|x\.?)\s*(\d{1,6})\s*$", t, re.IGNORECASE)
    ext = ""
    if m:
        ext = m.group(1)
        t = t[: m.start()]
    main = _PHONE_STRIP_RE.sub("", t)
    main = main.lstrip("+86")
    return main, ext


def normalize_email(raw: str | None) -> str:
    if not raw:
        return ""
    return to_halfwidth(raw).strip().lower().replace(" ", "")


_ADDR_STRIP_RE = re.compile(r"[\s\.。,，、;；:：\-－–—]")


def normalize_address(raw: str | None) -> str:
    """地址比对键：去空白与标点，全角半角。行政区划粒度匹配仅作低级线索。"""
    if not raw:
        return ""
    return _ADDR_STRIP_RE.sub("", to_halfwidth(raw))


def address_region_key(raw: str | None) -> str:
    """行政区划层级键（省+市+区县前缀），只允许作 III 级线索。

    取到尽可能具体的层级（如“北京市海淀区”），避免所有“北京市”企业互相撞键。
    """
    t = normalize_address(raw)
    m = re.match(r"^(.*?(?:省|自治区))?", t)
    prov = m.group(1) or ""
    rest = t[len(prov):]
    m2 = re.match(r"^(.*?市)?", rest)
    city = m2.group(1) or ""
    rest2 = rest[len(city):]
    m3 = re.match(r"^(.*?(?:区|县|旗))?", rest2)
    dist = m3.group(1) or ""
    return (prov + city + dist).strip()


# ------------------------------------------------------ 身份证件号（受控摘要）

_DEFAULT_ID_SALT = "tender-clearance-default-salt-v1"


def mask_id_number(raw: str) -> str:
    """证件号展示掩码：保留前 4 后 4，中间固定 10 个星号。"""
    digits = re.sub(r"\s", "", to_halfwidth(raw)).upper()
    if len(digits) < 8:
        return "*" * len(digits)
    return f"{digits[:4]}{'*' * 10}{digits[-4:]}"


def id_digest(raw: str, salt: str = _DEFAULT_ID_SALT) -> str:
    """按项目盐生成受控比对摘要：sha256(salt + 完整号码) 前 16 位十六进制。

    盐来自 project.yaml 的 id_digest_salt（项目内固定），保证同项目可重复运行、
    跨项目不可直接反查。调用方仍须按 redaction_mode 决定展示原值或摘要。
    """
    digits = re.sub(r"\s", "", to_halfwidth(raw)).upper()
    return hashlib.sha256((salt + digits).encode("utf-8")).hexdigest()[:16]


def redact_text(text: str, id_numbers: list[str], phones: list[str]) -> str:
    """把摘录文本中的完整证件号/手机号替换为掩码（防御性二次脱敏）。"""
    out = text
    for v in id_numbers:
        if v:
            out = out.replace(v, mask_id_number(v))
    for p in phones:
        if p:
            mp = re.sub(r"(\d{3})\d{4}(\d{4})", r"\1****\2", p)
            out = out.replace(p, mp)
    return out


# ------------------------------------------------------------------ 日期时间

def parse_pdf_date(raw: str | None) -> datetime | None:
    """解析 PDF 元数据日期（D:YYYYMMDDHHmmSS+TZ）。"""
    if not raw:
        return None
    t = raw.strip()
    if t.startswith("D:"):
        t = t[2:]
    m = re.match(
        r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([+\-Z])?(\d{2})?'?(\d{2})?'?",
        t,
    )
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2) or 1)
    d = int(m.group(3) or 1)
    h = int(m.group(4) or 0)
    mi = int(m.group(5) or 0)
    s = int(m.group(6) or 0)
    tz = m.group(7)
    tzinfo = None
    if tz in ("+", "-"):
        from datetime import timedelta, timezone

        sign = 1 if tz == "+" else -1
        oh = int(m.group(8) or 0)
        om = int(m.group(9) or 0)
        tzinfo = timezone(sign * timedelta(hours=oh, minutes=om))
    elif tz == "Z":
        from datetime import timezone

        tzinfo = timezone.utc
    from datetime import timezone

    dt = datetime(y, mo, d, h, mi, s, tzinfo=tzinfo)
    if dt.tzinfo is None:
        # 无时区信息的元数据时间按 UTC 记录并保留原样字符串由调用方备注
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_date(raw: str | None) -> date | None:
    """解析 YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日。"""
    if not raw:
        return None
    t = to_halfwidth(raw).strip()
    t = t.replace("年", "-").replace("月", "-").replace("日", "")
    m = re.match(r"^(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})", t)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
