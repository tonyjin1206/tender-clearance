"""文件格式解析：PDF、DOCX、XLSX、图片。

只读访问原始文件；所有函数返回纯数据结构。解析失败以 ParseIssue 报告，
不抛出未捕获异常（失败显性化，方案 2.1）。
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .normalize import parse_pdf_date, to_halfwidth


@dataclass
class ParseIssue:
    kind: str  # password_protected / corrupt / unsupported / empty / partial / ocr_unavailable
    detail: str


@dataclass
class PageImage:
    page: int
    index: int
    ext: str
    sha256: str
    width: int
    height: int
    ahash: str | None = None
    exif: dict[str, str] = field(default_factory=dict)


@dataclass
class PdfPage:
    page: int
    text: str
    has_text_layer: bool
    image_count: int
    ocr_text: str = ""            # OCR 识别文本（仅无文本层页）
    ocr_confidence: float | None = None


@dataclass
class ParsedPdf:
    pages: list[PdfPage] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    xmp_xml: str = ""
    embedded_files: list[dict[str, str]] = field(default_factory=list)
    form_fields: list[dict[str, str]] = field(default_factory=list)
    images: list[PageImage] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


_MEDIA_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".htm": "text/html",
    ".eml": "message/rfc822",
}

SNIFF_MAGIC = {
    b"%PDF": "application/pdf",
    b"PK\x03\x04": "application/zip",
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
}


def detect_media_type(path: Path) -> str:
    head = b""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        pass
    for magic, mt in SNIFF_MAGIC.items():
        if head.startswith(magic):
            if mt == "application/zip":
                ext = path.suffix.lower()
                if ext in (".docx", ".xlsx"):
                    return _MEDIA_BY_EXT[ext]
                return "application/zip"
            return mt
    return _MEDIA_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


# ------------------------------------------------------------------ PDF


def import_fitz():
    """优先导入 PyMuPDF 新模块名，兼容旧版 ``fitz``。"""
    import warnings

    try:
        import pymupdf

        return pymupdf
    except ImportError:
        pass
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*deprecated.*")
        import fitz

        return fitz


def parse_pdf(
    path: Path,
    ocr_provider: str = "none",
    *,
    inspect_pages: bool = True,
) -> ParsedPdf:
    """解析 PDF 文本/属性；生产 OCR 不在 Core 中执行。

    ``mock`` 仅供虚构夹具读取 XMP 中预置的 OCR 结果，真实宿主结果必须走
    ``ocr-result.v1`` 导入。历史 ``paddle`` 参数被视为不可用，不会触发任何
    引擎、模型或页面渲染。
    """
    fitz = import_fitz()

    out = ParsedPdf()
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - 解析失败必须显性化而不是中断
        kind = "password_protected" if "password" in str(exc).lower() or "encryption" in str(exc).lower() else "corrupt"
        out.issues.append(ParseIssue(kind, f"无法打开 PDF：{exc}"))
        return out
    try:
        if doc.needs_pass:
            out.issues.append(ParseIssue("password_protected", "PDF 受密码保护，未尝试破解"))
            return out
        md = doc.metadata or {}
        out.metadata = {k: str(v) for k, v in md.items() if v}
        try:
            xmp = doc.get_xml_metadata()
            out.xmp_xml = xmp.decode("utf-8", "replace") if isinstance(xmp, bytes) else str(xmp or "")
        except Exception:  # noqa: BLE001
            out.xmp_xml = ""
        # 嵌入文件
        try:
            for i in range(doc.embfile_count()):
                info = doc.embfile_info(i)
                out.embedded_files.append(
                    {"name": str(info.get("filename", f"emb-{i}")), "sha256": hashlib.sha256(doc.embfile_get(i)).hexdigest()}
                )
        except Exception:  # noqa: BLE001
            pass
        if inspect_pages:
            for pno in range(doc.page_count):
                page = doc.load_page(pno)
                try:
                    text = page.get_text("text") or ""
                except Exception:  # noqa: BLE001
                    text = ""
                    out.issues.append(ParseIssue("partial", f"第 {pno + 1} 页文本提取失败"))
                imgs = page.get_images(full=True)
                pdf_page = PdfPage(
                    page=pno + 1, text=text, has_text_layer=bool(text.strip()), image_count=len(imgs)
                )
                out.pages.append(pdf_page)
                for idx, img in enumerate(imgs):
                    xref = img[0]
                    try:
                        raw = doc.extract_image(xref)
                        data = raw["image"]
                        out.images.append(_describe_image(data, raw.get("ext", ""), pno + 1, idx))
                    except Exception:  # noqa: BLE001
                        out.issues.append(ParseIssue("partial", f"第 {pno + 1} 页第 {idx + 1} 张图片提取失败"))
                if ocr_provider == "mock":
                    _mock_ocr_from_xmp(out, pno + 1)
            # 表单
            try:
                for pno in range(doc.page_count):
                    for w in doc.load_page(pno).widgets() or []:
                        out.form_fields.append(
                            {"page": str(pno + 1), "name": str(w.field_name), "value": str(w.field_value)}
                        )
            except Exception:  # noqa: BLE001
                pass
    finally:
        doc.close()
    return out


_XMP_OCR_RE = re.compile(
    r"<tender:ocr[^>]*page=\"(\d+)\"[^>]*confidence=\"([\d.]+)\"[^>]*>(.*?)</tender:ocr>",
    re.DOTALL,
)


def _mock_ocr_from_xmp(out: ParsedPdf, page: int) -> None:
    """测试用 mock OCR：从 XMP 的 tender:ocr 标记读取文本与置信度。

    生产环境应替换为经批准的 OCR 服务适配器；接口形状保持一致。
    """
    for m in _XMP_OCR_RE.finditer(out.xmp_xml):
        if int(m.group(1)) == page:
            out.issues.append(
                ParseIssue("ocr_mock_used", f"第 {page} 页使用 mock OCR（仅测试环境）")
            )


def get_mock_ocr_text(xmp_xml: str, page: int) -> tuple[str, float] | None:
    for m in _XMP_OCR_RE.finditer(xmp_xml):
        if int(m.group(1)) == page:
            return m.group(3), float(m.group(2))
    return None


def _describe_image(data: bytes, ext: str, page: int, index: int) -> PageImage:
    import hashlib

    from PIL import Image

    info = PageImage(
        page=page,
        index=index,
        ext=ext,
        sha256=hashlib.sha256(data).hexdigest(),
        width=0,
        height=0,
    )
    try:
        img = Image.open(io.BytesIO(data))
        info.width, info.height = img.size
        info.ahash = _ahash(img)
        exif = img.getexif()
        named = {
            "Make": 0x010F,
            "Model": 0x0110,
            "Software": 0x0131,
            "Artist": 0x013B,
            "HostComputer": 0x013C,
        }
        for name, tag in named.items():
            v = exif.get(tag)
            if v:
                info.exif[name] = str(v).strip()
        exif_ifd = exif.get_ifd(0x8769)
        for name, tag in {
            "BodySerialNumber": 0xA431,
            "ImageUniqueID": 0xA420,
            "DateTimeOriginal": 0x9003,
        }.items():
            v = exif_ifd.get(tag)
            if v:
                info.exif[name] = str(v).strip()
    except Exception:  # noqa: BLE001 - 图片损坏不应中断整体提取
        pass
    return info


def _ahash(img: Any) -> str:
    try:
        small = img.convert("L").resize((8, 8))
        px = list(small.getdata())
        avg = sum(px) / len(px)
        bits = "".join("1" if p >= avg else "0" for p in px)
        return f"{int(bits, 2):016x}"
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ DOCX

_WML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class ParsedDocx:
    paragraphs: list[str] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)  # {"index":i,"rows":[[...]]}
    header_footer_text: list[str] = field(default_factory=list)
    core_properties: dict[str, str] = field(default_factory=dict)
    app_properties: dict[str, str] = field(default_factory=dict)
    custom_properties: dict[str, str] = field(default_factory=dict)
    issues: list[ParseIssue] = field(default_factory=list)


def parse_docx(path: Path) -> ParsedDocx:
    import docx

    out = ParsedDocx()
    try:
        d = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        out.issues.append(ParseIssue("corrupt", f"无法打开 DOCX：{exc}"))
        return out
    try:
        out.paragraphs = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        for ti, table in enumerate(d.tables):
            rows = []
            for row in table.rows:
                rows.append([cell.text.strip() for cell in row.cells])
            out.tables.append({"index": ti, "rows": rows})
        for section in d.sections:
            for part in (section.header, section.footer):
                texts = [p.text for p in part.paragraphs if p.text.strip()]
                out.header_footer_text.extend(texts)
        cp = d.core_properties
        out.core_properties = {
            "author": cp.author or "",
            "last_modified_by": cp.last_modified_by or "",
            "title": cp.title or "",
            "subject": cp.subject or "",
            "keywords": cp.keywords or "",
            "comments": cp.comments or "",
            "category": cp.category or "",
            "created": cp.created.isoformat() if cp.created else "",
            "modified": cp.modified.isoformat() if cp.modified else "",
            "revision": str(cp.revision or ""),
        }
        out.core_properties = {k: v for k, v in out.core_properties.items() if v}
        _read_docx_props(path, out)
    except Exception as exc:  # noqa: BLE001
        out.issues.append(ParseIssue("partial", f"DOCX 部分内容提取失败：{exc}"))
    return out


def _read_docx_props(path: Path, out: ParsedDocx) -> None:
    """直接读取 docProps/app.xml 与 custom.xml（python-docx 未完整暴露）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            if "docProps/core.xml" in zf.namelist():
                root = ElementTree.fromstring(zf.read("docProps/core.xml"))
                core_map = {
                    "creator": "creator", "lastModifiedBy": "last_modified_by", "title": "title",
                    "subject": "subject", "keywords": "keywords", "description": "comments",
                    "created": "created", "modified": "modified", "revision": "revision",
                }
                for child in root:
                    tag = child.tag.rsplit("}", 1)[-1]
                    key = core_map.get(tag)
                    if key and child.text:
                        out.core_properties[key] = child.text.strip()
            if "docProps/app.xml" in zf.namelist():
                root = ElementTree.fromstring(zf.read("docProps/app.xml"))
                for child in root:
                    tag = child.tag.rsplit("}", 1)[-1]
                    if tag in ("Application", "Company", "AppVersion", "Template", "TotalTime", "HeadingPairs", "TitlesOfParts"):
                        out.app_properties[tag] = (child.text or "").strip()
            if "docProps/custom.xml" in zf.namelist():
                root = ElementTree.fromstring(zf.read("docProps/custom.xml"))
                for prop in root:
                    tag = prop.tag.rsplit("}", 1)[-1]
                    if tag == "property":
                        name = prop.attrib.get("name", "")
                        value = ""
                        for v in prop:
                            value = (v.text or "").strip()
                        out.custom_properties[name] = value
    except Exception as exc:  # noqa: BLE001
        out.issues.append(ParseIssue("partial", f"Office 扩展属性读取失败：{exc}"))


def parse_docx_metadata(path: Path) -> ParsedDocx:
    """只读取 DOCX 属性 XML，不加载正文、表格或媒体。"""
    out = ParsedDocx()
    _read_docx_props(path, out)
    return out


# ------------------------------------------------------------------ XLSX


@dataclass
class ParsedXlsx:
    sheets: list[dict[str, Any]] = field(default_factory=list)  # {"name":s,"cells":[[coord,value]]}
    properties: dict[str, str] = field(default_factory=dict)
    issues: list[ParseIssue] = field(default_factory=list)


def parse_xlsx(path: Path, *, inspect_cells: bool = True) -> ParsedXlsx:
    import openpyxl

    out = ParsedXlsx()
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        out.issues.append(ParseIssue("corrupt", f"无法打开 XLSX：{exc}"))
        return out
    try:
        props = wb.properties
        for key in (
            "creator", "lastModifiedBy", "title", "subject", "description", "keywords",
            "category", "company", "lastPrinted", "created", "modified", "revision",
        ):
            value = getattr(props, key, None)
            if value:
                out.properties[key] = str(value)
        if inspect_cells:
            for ws in wb.worksheets:
                cells: list[list[str]] = []
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value is None:
                            continue
                        text = str(cell.value).strip()
                        if text:
                            cells.append([cell.coordinate, text])
                out.sheets.append({"name": ws.title, "cells": cells})
    except Exception as exc:  # noqa: BLE001
        out.issues.append(ParseIssue("partial", f"XLSX 部分内容提取失败：{exc}"))
    finally:
        wb.close()
    return out


# ---------------------------------------------------------- 可复用解析缓存

def serialize_parsed(parsed: Any) -> dict[str, Any]:
    """把一次性解析结果转换为 JSON 可存储结构。

    内容提取和属性提取需要的底层文档对象完全相同；缓存这个纯数据快照后，
    后续阶段不再重新打开/解码同一份 PDF、DOCX 或 XLSX。缓存不包含文件句柄、
    运行时凭据或模型结果。
    """
    if isinstance(parsed, ParsedPdf):
        return {"kind": "pdf", "value": asdict(parsed)}
    if isinstance(parsed, ParsedDocx):
        return {"kind": "docx", "value": asdict(parsed)}
    if isinstance(parsed, ParsedXlsx):
        return {"kind": "xlsx", "value": asdict(parsed)}
    raise TypeError(f"不支持缓存的解析结果类型：{type(parsed).__name__}")


def deserialize_parsed(payload: Any) -> Any | None:
    """读取解析缓存；旧版本或损坏缓存返回 None，由调用方安全回退重解析。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), dict):
        return None
    value = payload["value"]
    try:
        kind = payload.get("kind")
        if kind == "pdf":
            return ParsedPdf(
                pages=[PdfPage(**item) for item in value.get("pages", [])],
                metadata=dict(value.get("metadata", {})),
                xmp_xml=str(value.get("xmp_xml", "")),
                embedded_files=list(value.get("embedded_files", [])),
                form_fields=list(value.get("form_fields", [])),
                images=[PageImage(**item) for item in value.get("images", [])],
                issues=[ParseIssue(**item) for item in value.get("issues", [])],
            )
        if kind == "docx":
            return ParsedDocx(
                paragraphs=list(value.get("paragraphs", [])),
                tables=list(value.get("tables", [])),
                header_footer_text=list(value.get("header_footer_text", [])),
                core_properties=dict(value.get("core_properties", {})),
                app_properties=dict(value.get("app_properties", {})),
                custom_properties=dict(value.get("custom_properties", {})),
                issues=[ParseIssue(**item) for item in value.get("issues", [])],
            )
        if kind == "xlsx":
            return ParsedXlsx(
                sheets=list(value.get("sheets", [])),
                properties=dict(value.get("properties", {})),
                issues=[ParseIssue(**item) for item in value.get("issues", [])],
            )
    except (TypeError, ValueError, KeyError):
        return None
    return None


# ------------------------------------------------------------------ 通用文本

def read_text_file(path: Path) -> tuple[str, ParseIssue | None]:
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except Exception as exc:  # noqa: BLE001
        return "", ParseIssue("corrupt", f"文本读取失败：{exc}")


def office_created_modified(props: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    created = _iso_or_none(props.get("created"))
    modified = _iso_or_none(props.get("modified"))
    return created, modified


def _iso_or_none(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


__all__ = [
    "ParseIssue",
    "PageImage",
    "PdfPage",
    "ParsedPdf",
    "ParsedDocx",
    "ParsedXlsx",
    "detect_media_type",
    "parse_pdf",
    "parse_docx",
    "parse_docx_metadata",
    "parse_xlsx",
    "read_text_file",
    "office_created_modified",
    "get_mock_ocr_text",
    "serialize_parsed",
    "deserialize_parsed",
    "parse_pdf_date",
    "to_halfwidth",
]
