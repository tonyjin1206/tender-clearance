"""基于规则的字段候选提取（仅生成候选，供交叉匹配与人工复核）。

模型可协助理解，但字段识别本身是确定性正则 + 表格标签关联；
低置信度（OCR）结果不会进入精确匹配（见 normalize_and_match）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .normalize import to_halfwidth, uscc_check_digit, USCC_CHARSET
from .template_locator import normalize_template_label

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
    value: str  # 原始命中值（敏感项为完整原文，仅内存中使用；落盘展示按 redaction_mode 决定）
    context: str = ""  # 命中所依据的标签或说明
    confidence: float = 1.0
    via_label: bool = False
    label: str | None = None
    label_relation: str | None = None
    label_bbox: dict[str, float] | None = None
    value_bbox: dict[str, float] | None = None


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
    (r"法定代表人(?:/负责人)?|法人代表|法律代表人", "legal_rep_name"),
    (r"授权代表|委托代理人|被授权人|授权委托人|投标代表|经办人", "bid_agent_name"),
    (r"股东|出资人|投资人", "shareholder_name"),
    (r"联系人", "contact_name"),
    (r"(?:联系|办公|注册|通讯)?地址|住所", "address"),
    (r"联系电话|联系方式|电话|手机号?|移动电话|传真", "phone"),
    (r"电子邮箱|邮箱|电子邮件|Email|E-mail", "email"),
    (r"身份证号|身份证号码|公民身份号码|证件号码|证件号", "id_number"),
    (r"供应商名称|投标人名称|投标人|公司名称|单位名称|供应商全称|投标单位", "company_name"),
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

_PROJECT_NOISE = (
    "委托方", "甲方", "乙方", "单价", "总价", "数量", "单位：", "单位:",
    "投标保证金", "法定代表人", "授权代表", "身份证", "联系人",
)


def _plausible_project_name(value: str) -> bool:
    value = re.sub(r"\s+", "", str(value or "")).strip("：:，,；;。 ")
    if len(value) < 8 or len(value) > 120:
        return False
    if any(marker in value for marker in _PROJECT_NOISE):
        return False
    return any(marker in value for marker in ("项目", "招标", "采购", "工程", "服务", "货物"))


def _plausible_tenderer(value: str) -> bool:
    value = re.sub(r"\s+", "", str(value or "")).strip("：:，,；;。 ")
    return bool(value and len(value) <= 80 and re.search(r"(?:公司|厂|中心|单位)$", value))


def _cover_title_hits(clean: list[dict[str, Any]], page_conf: float) -> list[FieldHit]:
    """从封面顶部标题恢复未带标签的招标人和项目名称。"""
    full_text = "\n".join(str(block.get("text", "")) for block in clean)
    if "投标文件" not in full_text and "投标书" not in full_text:
        return []

    def top(block: dict[str, Any]) -> float:
        return float((block.get("bbox") or {}).get("y", 1.0))

    top_blocks = [
        block for block in sorted(clean, key=top)
        if top(block) <= 0.38 and str(block.get("text", "")).strip()
        and "项目编号" not in str(block.get("text", ""))
    ]
    hits: list[FieldHit] = []
    if top_blocks:
        first = str(top_blocks[0].get("text", "")).strip()
        if _plausible_tenderer(first):
            hits.append(FieldHit(
                "tenderer", first, context="OCR 封面顶部标题", confidence=min(
                    float(top_blocks[0].get("confidence", page_conf)), page_conf
                ), via_label=False, label_relation="cover_title",
            ))
    for index, left in enumerate(top_blocks):
        combined = str(left.get("text", "")).strip()
        for right in top_blocks[index + 1:index + 3]:
            if top(right) - top(left) > 0.10:
                break
            combined += str(right.get("text", "")).strip()
            if _plausible_project_name(combined):
                hits.append(FieldHit(
                    "project_name", combined, context="OCR 封面标题上下行合并",
                    confidence=min(
                        float(left.get("confidence", page_conf)),
                        float(right.get("confidence", page_conf)), page_conf,
                    ), via_label=False, label_relation="cover_title_join",
                ))
                return hits
    return hits

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
    "签章", "盖章", "职称", "终审", "进度", "鉴别", "报告", "执行", "中华人民共和国",
    "居民身份证", "有限公司", "股份",
}


def _clean_name(value: str) -> str:
    """去掉姓名值尾部的括号注记（如“李四（身份证：…）”）并校验形状。"""
    # 授权书正文常把“法人代表张三授权李四为全权代表”识别成一整段，
    # 先在动作词处截断，避免把两个人名和正文拼成一个伪姓名。
    t = re.split(r"[（(]|授权|为全权|为代表|为|参加|，|,|；|;", value)[0].strip().strip("：:，, ")
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
            span: tuple[int, int] | None = None, occupy: bool = False,
            label: str | None = None, label_relation: str | None = None) -> None:
        value = value.strip()
        if field == "project_name" and not _plausible_project_name(value):
            return
        if field == "tenderer" and not _plausible_tenderer(value):
            return
        key = (field, value)
        if key in seen_values:
            return
        seen_values.add(key)
        hits.append(FieldHit(
            field=field, value=value.strip(), context=context, via_label=via,
            confidence=conf, label=label, label_relation=label_relation,
        ))
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
            label_text = re.sub(r"\s*[:：].*$", "", m.group(0)).strip()
            add(fld, value, m.group(0)[:40], True, label=label_text, label_relation="inline_label")

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


def scan_ocr_blocks(
    blocks: list[dict[str, Any]],
    *,
    average_confidence: float | None = None,
    location_precision: str = "page_only",
) -> list[FieldHit]:
    """按 OCR 文本块的空间邻近关系生成字段候选。

    有坐标时只把同一行、位于标签右侧或紧邻下方的块作为值；没有坐标时
    仅允许降级的全文候选，且强制低置信度，避免错行文本被当成字段事实。
    """
    clean = [b for b in blocks if str(b.get("text", "")).strip()]
    page_conf = average_confidence if average_confidence is not None else 0.0
    if location_precision == "page_only" or not any(b.get("bbox") for b in clean):
        text = "\n".join(str(b.get("text", "")) for b in clean)
        hits = scan_inline(text)
        for hit in hits:
            hit.confidence = min(hit.confidence, page_conf, 0.5)
            hit.context = (hit.context + "；" if hit.context else "") + "OCR page_only 降级候选"
            hit.label_relation = hit.label_relation or "page_only_fallback"
        return hits

    # Vision 等 Provider 常把“投标人：公司名”作为一个完整文本块返回，
    # 此时没有可供空间配对的独立值块。先保留内联标签候选，再叠加下方的
    # 标签/值空间配对；后续归组会以商务标主体证据过滤招标人名称。
    inline_hits = scan_inline("\n".join(str(b.get("text", "")) for b in clean))
    for hit in inline_hits:
        hit.confidence = min(hit.confidence, 0.85)
        block_conf = [float(b.get("confidence", 0.0)) for b in clean if hit.value in str(b.get("text", ""))]
        if block_conf:
            hit.confidence = min(hit.confidence, max(block_conf))
        hit.context = (hit.context + "；" if hit.context else "") + "OCR inline block fallback"
        hit.label_relation = hit.label_relation or "inline_block"
        matching_blocks = [b for b in clean if hit.value in str(b.get("text", "")) and b.get("bbox")]
        if matching_blocks:
            hit.value_bbox = dict(matching_blocks[0]["bbox"])

    labels: list[tuple[int, str, str]] = []
    full_text = "\n".join(str(b.get("text", "")) for b in clean)
    authorization_page = bool(re.search(r"法人代表.{0,100}授权|授权.{0,100}法人代表", full_text))

    # 授权书通常把法人身份证放在左侧、授权代表身份证放在右侧。
    # 先按可见坐标建立角色映射，再处理“公民身份号码”这种通用标签，
    # 避免把两张身份证都挂到同一个 id_number 字段下。
    id_role_by_value: dict[str, str] = {}
    if authorization_page:
        id_blocks: list[tuple[float, float, str]] = []
        for block in clean:
            block_text = str(block.get("text", ""))
            box = block.get("bbox") or {}
            x = float(box.get("x", 0.5)) + float(box.get("width", 0)) / 2
            y = float(box.get("y", 0.5)) + float(box.get("height", 0)) / 2
            for match in _ID18_RE.finditer(block_text):
                id_blocks.append((x, y, match.group(0)))
        unique_positions = {}
        for x, y, value in id_blocks:
            unique_positions.setdefault(value, []).append((x, y))
        id_positions = [
            (value, sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points))
            for value, points in unique_positions.items()
        ]
        if len(id_positions) >= 2:
            # 横向并排证件按 x；上下排列证件按 y。两种都是授权书常见版式。
            spread_x = max(item[1] for item in id_positions) - min(item[1] for item in id_positions)
            axis = 1 if spread_x >= 0.12 else 2
            ordered = sorted(id_positions, key=lambda item: item[axis])
            id_role_by_value[ordered[0][0]] = "legal_rep_id"
            id_role_by_value[ordered[-1][0]] = "bid_agent_id"
    for idx, block in enumerate(clean):
        text = str(block.get("text", "")).strip()
        for pat, field in _LABEL_FIELD:
            if re.search(r"(?:^|[：:]|\s)" + pat + r"\s*(?:$|[：:])", text):
                labels.append((idx, text, field))
                break

    def center(b: dict[str, Any]) -> tuple[float, float]:
        box = b.get("bbox") or {}
        return (float(box.get("x", 0)) + float(box.get("width", 0)) / 2,
                float(box.get("y", 0)) + float(box.get("height", 0)) / 2)

    hits: list[FieldHit] = []
    used: set[tuple[str, str]] = set()
    for label_idx, label_text, field in labels:
        label = clean[label_idx]
        label_box = label.get("bbox") or {}
        lx, ly = center(label)
        lh = float(label_box.get("height", 0.03))
        candidates: list[tuple[float, dict[str, Any]]] = []
        for idx, value_block in enumerate(clean):
            if idx == label_idx:
                continue
            vx, vy = center(value_block)
            if vx < lx - 0.01:
                continue
            dy = abs(vy - ly)
            if dy <= max(lh * 2.5, 0.045):
                score = dy + max(0.0, vx - lx) * 0.05
            elif 0 <= vy - ly <= max(lh * 5, 0.12) and abs(vx - lx) <= 0.18:
                score = 0.2 + (vy - ly)
            else:
                continue
            candidates.append((score, value_block))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0])
        value_block = candidates[0][1]
        value = str(value_block.get("text", "")).strip()
        sub = scan_inline(value)
        selected = next((h for h in sub if h.field == field), None)
        if selected is None:
            selected = _coerce_label_value(field, value)
        if selected is None:
            continue
        if selected.field == "id_number" and selected.value in id_role_by_value:
            selected.field = id_role_by_value[selected.value]
        conf = min(
            float(label.get("confidence", page_conf)),
            float(value_block.get("confidence", page_conf)),
        )
        key = (selected.field, selected.value)
        if key in used:
            continue
        used.add(key)
        selected.confidence = conf
        selected.context = f"OCR 空间配对：{label_text[:30]} → {value[:60]}"
        selected.label = label_text
        selected.label_relation = "same_line_right" if abs(center(value_block)[1] - ly) <= max(lh * 2.5, 0.045) else "below_near"
        selected.label_bbox = dict(label_box)
        if value_block.get("bbox"):
            selected.value_bbox = dict(value_block["bbox"])
        hits.append(selected)
    seen = {(hit.field, hit.value) for hit in hits}
    spatial_name_fields = {hit.field for hit in hits if hit.field in {"legal_rep_name", "bid_agent_name"}}
    for hit in inline_hits:
        if hit.field == "id_number" and hit.value in id_role_by_value:
            hit.field = id_role_by_value[hit.value]
        if hit.field in {"legal_rep_name", "bid_agent_name"} and hit.field in spatial_name_fields:
            continue
        if (hit.field, hit.value) not in seen:
            hits.append(hit)
            seen.add((hit.field, hit.value))
    existing_fields = {hit.field for hit in hits}
    for title_hit in _cover_title_hits(clean, page_conf):
        if title_hit.field in existing_fields:
            continue
        hits.append(title_hit)
        existing_fields.add(title_hit.field)
    return hits


def scan_template_metric_blocks(
    blocks: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    *,
    average_confidence: float | None = None,
    location_precision: str = "page_only",
) -> list[FieldHit]:
    """按空白招标模板的指标标签，保守地配对 OCR 值块。

    模板只提供“锚点”，不替代投标文件中的实际内容。只有同一 OCR 块明确出现
    “标签：值”，或存在坐标且值块位于标签右侧/紧邻下方时才产出候选；page_only
    无法证明版面关系时不猜，避免再次把相邻指标错配。
    """
    clean = [b for b in blocks if str(b.get("text", "")).strip()]
    if not clean or not metrics:
        return []
    page_conf = average_confidence if average_confidence is not None else 0.0
    metric_items = [m for m in metrics if normalize_template_label(str(m.get("label", "")))]
    metric_items.sort(key=lambda m: len(normalize_template_label(str(m.get("label", "")))), reverse=True)

    def center(block: dict[str, Any]) -> tuple[float, float]:
        box = block.get("bbox") or {}
        return (float(box.get("x", 0)) + float(box.get("width", 0)) / 2,
                float(box.get("y", 0)) + float(box.get("height", 0)) / 2)

    def block_conf(block: dict[str, Any]) -> float:
        value = block.get("confidence", page_conf)
        try:
            return float(value)
        except (TypeError, ValueError):
            return page_conf

    def matching_metric(text: str, metric: dict[str, Any]) -> bool:
        normalized = normalize_template_label(text)
        target = normalize_template_label(str(metric.get("label", "")))
        return bool(target and (normalized == target or target in normalized))

    def explicit_value(text: str, label: str) -> str | None:
        # OCR 可能在标签中插入空格/标点，因此先按原文，再按规范化字符串判断。
        escaped = re.escape(label.strip())
        match = re.search(escaped + r"\s*[：:]\s*(.+)$", text.strip())
        if match:
            value = match.group(1).strip(" ：:，,；;。")
            return value or None
        if normalize_template_label(text).startswith(normalize_template_label(label)):
            remainder = text.strip()[len(label):].lstrip(" ：:—-\t")
            return remainder.strip("，,；;。 ") or None
        return None

    has_coordinates = location_precision != "page_only" and any(b.get("bbox") for b in clean)
    hits: list[FieldHit] = []
    used: set[tuple[str, str]] = set()
    for metric in metric_items:
        metric_id = str(metric.get("metric_id", "")).strip()
        label_text = str(metric.get("label", "")).strip()
        if not metric_id or not label_text:
            continue
        for label_index, label_block in enumerate(clean):
            text = str(label_block.get("text", "")).strip()
            if not matching_metric(text, metric):
                continue
            value: str | None = explicit_value(text, label_text)
            value_block: dict[str, Any] | None = label_block if value else None
            relation = "template_metric_inline"
            if value is None and has_coordinates:
                label_box = label_block.get("bbox") or {}
                lx, ly = center(label_block)
                lh = float(label_box.get("height", 0.03))
                candidates: list[tuple[float, dict[str, Any], str]] = []
                for index, candidate in enumerate(clean):
                    if index == label_index:
                        continue
                    candidate_text = str(candidate.get("text", "")).strip()
                    if any(matching_metric(candidate_text, other) for other in metric_items):
                        continue
                    vx, vy = center(candidate)
                    if vx < lx - 0.01:
                        continue
                    dy = abs(vy - ly)
                    if dy <= max(lh * 2.5, 0.045):
                        score = dy + max(0.0, vx - lx) * 0.05
                        candidate_relation = "template_metric_same_line_right"
                    elif 0 <= vy - ly <= max(lh * 5, 0.12) and abs(vx - lx) <= 0.18:
                        score = 0.2 + (vy - ly)
                        candidate_relation = "template_metric_below_near"
                    else:
                        continue
                    candidates.append((score, candidate, candidate_relation))
                if candidates:
                    candidates.sort(key=lambda item: item[0])
                    _, value_block, relation = candidates[0]
                    value = str(value_block.get("text", "")).strip().strip(" ：:，,；;。") or None
                    relation = candidate_relation
            if not value or len(value) > 240:
                continue
            key = (metric_id, value)
            if key in used:
                continue
            used.add(key)
            label_box = label_block.get("bbox") or None
            value_box = (value_block or {}).get("bbox") or None
            confidence = min(block_conf(label_block), block_conf(value_block or label_block))
            if not has_coordinates:
                confidence = min(confidence, page_conf, 0.5)
            hits.append(FieldHit(
                field=f"metric:{metric_id}",
                value=value,
                context=f"模板指标锚点：{label_text} → {value[:100]}",
                confidence=confidence,
                via_label=True,
                label=label_text,
                label_relation=relation,
                label_bbox=dict(label_box) if label_box else None,
                value_bbox=dict(value_box) if value_box else None,
            ))
            break
    return hits


def _coerce_label_value(field: str, value: str) -> FieldHit | None:
    """对姓名/地址等没有独立格式正则的字段做保守值校验。"""
    value = value.strip().strip("：:，,；;")
    if not value or len(value) > 80:
        return None
    if field in {"legal_rep_name", "bid_agent_name", "shareholder_name", "contact_name"}:
        value = _clean_name(value)
        return FieldHit(field, value, via_label=True) if _plausible_name(value) else None
    if field == "company_name" and not _COMPANY_RE.search(value):
        return None
    if field == "project_name" and _plausible_project_name(value):
        return FieldHit(field, value, via_label=True)
    if field == "tenderer" and _plausible_tenderer(value):
        return FieldHit(field, value, via_label=True)
    if field in {"address", "bid_date", "project_code"}:
        return FieldHit(field, value, via_label=True)
    return None


def _id_context(text: str, pos: int) -> str:
    window = text[max(0, pos - 20):pos]
    if re.search(r"身份证|证件号|身份證", window):
        if re.search(r"授权|委托|被授权", window):
            return "授权代表证件"
        return "身份证标签"
    return ""


_NAME_LABELS = {"legal_rep_name": re.compile(r"(法定代表人|法人代表|负责人)\s*[:：]?\s*([一-龥·]{2,12})"),
                "bid_agent_name": re.compile(r"(授权代表|委托代理人|被授权人|授权委托人|投标代表|经办人|授权(?!人|书|委托))\s*[:：]?\s*([一-龥·]{2,12})"),
                "shareholder_name": re.compile(r"(股东|出资人|投资人)\s*[:：]?\s*([一-龥·A-Za-z0-9（）()]{2,30})"),
                "contact_name": re.compile(r"(联系人)\s*[:：]?\s*([一-龥·]{2,12})")}


def _name_after_label(text: str, seen_values: set) -> list[FieldHit]:
    hits = []
    occupied: list[tuple[int, int]] = []
    for fld, rx in _NAME_LABELS.items():
        for m in rx.finditer(text):
            name = _clean_name(m.group(2))
            if not _plausible_name(name):
                continue
            span = (m.start(2), m.start(2) + len(name))
            if any(not (span[1] <= s or span[0] >= e) for s, e in occupied):
                continue
            if (fld, name) in seen_values:
                continue
            occupied.append(span)
            seen_values.add((fld, name))
            hits.append(FieldHit(
                field=fld, value=name, context=m.group(1), via_label=True,
                label=m.group(1), label_relation="inline_label",
            ))
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
    label_at: dict[str, tuple[str, str]] = {}
    for coord, text in cells:
        for rx, fld in _LABEL_RES:
            if rx.match(text):
                label_at[coord] = (fld, text)
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
    for coord, (fld, label_text) in label_at.items():
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
            hits.append(FieldHit(
                field=norm_fld, value=value, context=f"{sheet_name}!{coord}→{cand}",
                via_label=True, confidence=0.9, label=label_text,
                label_relation="xlsx_adjacent",
            ))
            break
    return hits


def uscc_looks_valid(text: str) -> bool:
    t = to_halfwidth(text)
    m = USCC_BODY_RE.search(t)
    if not m:
        return False
    code = m.group(0)
    return uscc_check_digit(code[:17]) == code[17]
