"""Markdown → PDF（中文）转换，基于 ReportLab Platypus。

- 字体：优先系统 Songti.ttc / Arial Unicode.ttf，最后退回 Adobe CID 字体
  STSong-Light（无需字体文件）；正文 CJK 换行；
- 支持报告所需元素：多级标题、段落、无序列表、表格（表头底纹、按内容分配列宽、
  单元格内自动换行）、分隔线、引用块与行内 **加粗**；
- 输入为已脱敏的报告 Markdown（render_report.py 产物），本模块不做二次脱敏。
"""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (HRFlowable, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

_FONT_CANDIDATES = [
    ("ReportSongti", "/System/Library/Fonts/Supplemental/Songti.ttc", 0),
    ("ReportArialUni", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
    ("ReportHiragino", "/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
]
_CID_FALLBACK = "STSong-Light"

_FONT_NAME: str | None = None


def _register_font() -> str:
    global _FONT_NAME
    if _FONT_NAME:
        return _FONT_NAME
    for name, path, idx in _FONT_CANDIDATES:
        try:
            if Path(path).exists():
                font = (TTFont(name, path) if idx is None
                        else TTFont(name, path, subfontIndex=idx))
                pdfmetrics.registerFont(font)
                _FONT_NAME = name
                return name
        except Exception:  # noqa: BLE001 - 单个字体失败继续尝试
            continue
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont(_CID_FALLBACK))
    _FONT_NAME = _CID_FALLBACK
    return _FONT_NAME


def _styles() -> dict[str, ParagraphStyle]:
    font = _register_font()

    def st(name, size, leading, **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading,
                              wordWrap="CJK", **kw)

    return {
        "h1": st("h1", 17, 24, spaceBefore=6, spaceAfter=10),
        "h2": st("h2", 13.5, 20, spaceBefore=14, spaceAfter=8),
        "h3": st("h3", 11.5, 17, spaceBefore=10, spaceAfter=6),
        "h4": st("h4", 10.5, 15, spaceBefore=8, spaceAfter=4),
        "body": st("body", 10, 15.5, spaceAfter=5),
        "bullet": st("bullet", 10, 15.5, leftIndent=14, spaceAfter=2),
        "quote": st("quote", 9.5, 14.5, leftIndent=16,
                    textColor=colors.HexColor("#555555"), spaceAfter=4),
        "cell": st("cell", 8.5, 12),
        "cell_head": st("cell_head", 8.5, 12),
    }


def _inline(md: str) -> str:
    """行内标记转 Paragraph 标签：转义 → 加粗 → 删除代码反引号。"""
    text = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = text.replace("`", "")
    return text


def _col_widths(rows: list[list[str]], usable: float) -> list[float]:
    """按内容宽度加权分配列宽；保证每列 ≥34pt（含 8pt 内边距），超宽时从最宽列回收。"""
    ncol = max(len(r) for r in rows)
    weights = []
    for i in range(ncol):
        w = max(stringWidth(r[i] if i < len(r) else "", _register_font(), 8.5)
                for r in rows)
        weights.append(max(w, 30))
    total = sum(weights)
    widths = [w / total * usable for w in weights] if total else [usable / ncol] * ncol

    min_col = 34.0
    if usable < min_col * ncol:  # 列太多：均分（Paragraph 仍可换行）
        return [usable / ncol] * ncol
    widths = [max(w, min_col) for w in widths]
    excess = sum(widths) - usable
    while excess > 0.5:
        widest = max(range(ncol), key=lambda i: widths[i])
        can_shave = widths[widest] - min_col
        if can_shave <= 0:
            break
        shave = min(can_shave, excess)
        widths[widest] -= shave
        excess -= shave
    return widths


def md_to_pdf(md_text: str, out_path: Path, title: str = "清标报告") -> Path:
    styles = _styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                            leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm,
                            title=title, author="tender-clearance")
    usable = A4[0] - 4 * cm

    story: list = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            if level == 2 and story:  # 每章另起一页
                story.append(PageBreak())
            story.append(Paragraph(_inline(m.group(2)), styles[f"h{level}"]))
            if level <= 2:
                story.append(HRFlowable(width="100%", thickness=0.6,
                                        color=colors.HexColor("#888888")))
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            story.append(HRFlowable(width="100%", thickness=0.6,
                                    color=colors.HexColor("#AAAAAA")))
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) \
                and re.match(r"^\|[\s:|-]+\|?\s*$", lines[i + 1].strip()):
            # 表格块
            tbl_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i].strip())
                i += 1
            rows = []
            for tl in tbl_lines:
                if re.match(r"^\|[\s:|-]+\|?\s*$", tl):
                    continue
                cells = [c.strip() for c in tl.strip("|").split("|")]
                rows.append([_inline(c) for c in cells])
            if rows:
                data = [[Paragraph(c, styles["cell_head"]) for c in rows[0]]]
                for r in rows[1:]:
                    data.append([Paragraph(c, styles["cell"]) for c in r])
                table = Table(data, colWidths=_col_widths(rows, usable), repeatRows=1)
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(table)
                story.append(Spacer(1, 6))
            continue

        if stripped.startswith(("- ", "* ")) or re.match(r"^\d+\.\s", stripped):
            story.append(Paragraph("• " + _inline(re.sub(r"^(- |\* |\d+\.\s)", "", stripped)),
                                   styles["bullet"]))
            i += 1
            continue

        if stripped.startswith(">"):
            story.append(Paragraph(_inline(stripped.lstrip("> ")), styles["quote"]))
            i += 1
            continue

        story.append(Paragraph(_inline(stripped), styles["body"]))
        i += 1

    doc.build(story)
    return out_path


