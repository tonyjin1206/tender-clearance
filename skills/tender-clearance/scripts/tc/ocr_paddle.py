"""可选的本地 PaddleOCR Provider 适配层（不属于 tender-clearance Core）。

- 保留此文件仅为迁移期兼容和历史测试定位；Core 不导入、不运行本模块；
- 生产 OCR 应由宿主 Agent 的独立 Provider Skill 提供 `ocr-result.v1`；
- 引擎进程内单例（模型加载慢，只初始化一次）；
- 输入为页面渲染的 PNG 字节，输出文本、置信度和可选的块坐标；
- OCR 文本进入既有低置信度通道：字段 confidence<1、不参与精确匹配、进人工核对；
- 本模块不做任何脱敏外加工，脱敏仍由提取/报告层完成。

请勿在 Core 环境中安装或调用此模块；迁移完成后可在 Provider 包中移除。
"""

from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Mapping
from typing import Any

from .progress import emit_progress


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def engine_options(lang: str = "ch") -> dict[str, Any]:
    """返回扫描标书默认的轻量配置。

    真实商务扫描件是规整的 A4 页面；默认关闭文档方向、展平和文本行方向
    三个额外模型，避免每页重复做与页面无关的预处理。遇到旋转、弯曲或
    复杂版面时，可设置 ``TC_OCR_LAYOUT_PREPROCESS=1`` 恢复这些能力。
    模型名只接受显式环境覆盖，避免 Provider 偷换成速度更快但识别质量较低
    的 mobile 模型。
    """
    options: dict[str, Any] = {"lang": lang}
    layout_preprocess = _env_bool("TC_OCR_LAYOUT_PREPROCESS", False)
    options.update({
        "use_doc_orientation_classify": layout_preprocess,
        "use_doc_unwarping": layout_preprocess,
        "use_textline_orientation": layout_preprocess,
    })
    for env_name, option_name in (
        ("TC_OCR_MODEL_DET", "text_detection_model_name"),
        ("TC_OCR_MODEL_REC", "text_recognition_model_name"),
        ("TC_OCR_MODEL_TEXTLINE_ORI", "textline_orientation_model_name"),
    ):
        model_name = os.environ.get(env_name)
        if model_name:
            options[option_name] = model_name
    for env_name, option_name in (
        ("TC_OCR_DET_LIMIT_SIDE_LEN", "text_det_limit_side_len"),
        ("TC_OCR_REC_BATCH_SIZE", "text_recognition_batch_size"),
    ):
        value = os.environ.get(env_name)
        if value:
            try:
                options[option_name] = int(value)
            except ValueError:
                pass
    return options


def _result_value(result: Any, key: str, default: Any = None) -> Any:
    """兼容 PaddleOCR 3.x 的 dict、OCRResult 对象和测试替身。"""
    if isinstance(result, Mapping):
        return result.get(key, default)
    getter = getattr(result, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return getattr(result, key, default)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _normalise_bbox(box: Any, width: int, height: int) -> dict[str, float] | None:
    """把 Paddle 的像素坐标转成契约使用的左上角归一化矩形。"""
    values = box.tolist() if hasattr(box, "tolist") else box
    if isinstance(values, Mapping):
        values = [values.get(k) for k in ("x1", "y1", "x2", "y2")]

    def flatten(items: Any) -> list[Any]:
        if isinstance(items, (list, tuple)):
            result: list[Any] = []
            for item in items:
                result.extend(flatten(item))
            return result
        return [items]

    try:
        flat = [float(v) for v in flatten(values)]
    except (TypeError, ValueError):
        return None
    if len(flat) == 4:
        xs, ys = flat[0::2], flat[1::2]
    elif len(flat) >= 8 and len(flat) % 2 == 0:
        xs, ys = flat[0::2], flat[1::2]
    else:
        return None
    x1, x2 = sorted((max(0.0, min(float(width), min(xs))), max(0.0, min(float(width), max(xs)))))
    y1, y2 = sorted((max(0.0, min(float(height), min(ys))), max(0.0, min(float(height), max(ys)))))
    if x2 <= x1 or y2 <= y1 or width <= 0 or height <= 0:
        return None
    return {
        "x": round(x1 / width, 6),
        "y": round(y1 / height, 6),
        "width": round((x2 - x1) / width, 6),
        "height": round((y2 - y1) / height, 6),
    }


def _parse_result(raw: Any, width: int, height: int) -> list[dict[str, Any]]:
    """解析 3.x/2.x 返回值，保留每个文本块的坐标。"""
    blocks: list[dict[str, Any]] = []
    results = _as_list(raw)
    for result in results:
        texts = _as_list(_result_value(result, "rec_texts"))
        scores = _as_list(_result_value(result, "rec_scores"))
        boxes = _as_list(_result_value(result, "rec_boxes"))
        if not boxes:
            boxes = _as_list(_result_value(result, "rec_polys"))
        if texts:
            for index, value in enumerate(texts):
                text = str(value).strip()
                if not text:
                    continue
                try:
                    confidence = float(scores[index])
                except (IndexError, TypeError, ValueError):
                    confidence = 0.0
                item: dict[str, Any] = {
                    "text": text,
                    "confidence": round(max(0.0, min(1.0, confidence)), 6),
                }
                if index < len(boxes):
                    bbox = _normalise_bbox(boxes[index], width, height)
                    if bbox:
                        item["bbox"] = bbox
                blocks.append(item)
            continue

        # PaddleOCR 2.x: [[box, (text, score)], ...]
        for line in (result or []):
            try:
                box, recognised = line[0], line[1]
                text, confidence = recognised[0], recognised[1]
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            text = str(text).strip()
            if not text:
                continue
            item = {
                "text": text,
                "confidence": round(max(0.0, min(1.0, float(confidence))), 6),
            }
            bbox = _normalise_bbox(box, width, height)
            if bbox:
                item["bbox"] = bbox
            blocks.append(item)

    # OCR 返回顺序在不同版本/后端不完全稳定；统一成阅读顺序，方便标签配对。
    blocks.sort(key=lambda item: (
        (item.get("bbox") or {}).get("y", 1.0),
        (item.get("bbox") or {}).get("x", 1.0),
    ))
    return blocks


def _fake_blocks() -> tuple[list[dict[str, Any]], float] | None:
    fake = os.environ.get("TC_OCR_FAKE_TEXT")
    if not fake:
        return None
    try:
        data = json.loads(fake)
        text = str(data["text"])
        confidence = float(data.get("confidence", 0.8))
        blocks = data.get("blocks")
        if isinstance(blocks, list):
            return blocks, confidence
        return ([{"text": text, "confidence": confidence}], confidence)
    except (ValueError, KeyError, TypeError):
        return ([{"text": fake, "confidence": 0.8}], 0.8)

_ENGINE = None
_ENGINE_LANG = "ch"


def get_engine(lang: str = "ch"):
    """返回进程内单例 PaddleOCR 引擎；未安装 paddleocr 时抛出带指引的异常。

    兼容 2.x（use_angle_cls/show_log）与 3.x（use_textline_orientation）两代 API。
    """
    global _ENGINE, _ENGINE_LANG
    if _ENGINE is not None and _ENGINE_LANG == lang:
        return _ENGINE
    try:
        from paddleocr import PaddleOCR  # noqa: PLC0415 - 重依赖按需加载
    except ImportError as exc:  # noqa: TRY302
        raise RuntimeError(
            "Core 不提供 PaddleOCR；请由宿主 Agent 安装并声明独立 OCR Provider，"
            "再通过 ocr-result.v1 导入（或将扫描页保留为人工复核）"
        ) from exc
    try:
        _ENGINE = PaddleOCR(**engine_options(lang))
    except (TypeError, ValueError):
        _ENGINE = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
    _ENGINE_LANG = lang
    return _ENGINE


def set_engine(fake) -> None:
    """测试注入替身引擎（提供 .ocr(img_bytes, cls=True) 接口）。"""
    global _ENGINE, _ENGINE_LANG
    _ENGINE = fake
    _ENGINE_LANG = "ch"


def reset_engine() -> None:
    global _ENGINE
    _ENGINE = None


def _ocr_image_bytes_with_blocks(
    png: bytes,
    lang: str = "ch",
) -> tuple[list[dict[str, Any]], float]:
    """实际执行单页 OCR；外层函数负责发出页完成事件。"""
    fake = _fake_blocks()
    if fake is not None:
        return fake

    engine = get_engine(lang)
    import numpy as _np
    from PIL import Image as _Image

    with _Image.open(io.BytesIO(png)) as image:
        image = image.convert("RGB")
        img = _np.array(image)
    height, width = img.shape[:2]
    raw = None
    if hasattr(engine, "predict"):
        try:
            raw = engine.predict(img)
        except Exception:  # noqa: BLE001 - 退回旧 API
            raw = None
    if raw is None:
        try:
            raw = engine.ocr(img, cls=True)
        except TypeError:
            raw = engine.ocr(img)
    blocks = _parse_result(raw, width, height)
    scores = [float(block["confidence"]) for block in blocks]
    confidence = (sum(scores) / len(scores)) if scores else 0.0
    return blocks, round(confidence, 4)


def ocr_image_bytes_with_blocks(
    png: bytes,
    lang: str = "ch",
    *,
    page_number: int | None = None,
    total_pages: int | None = None,
    document_id: str | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """对单页图像 OCR，并在完成后发送一条不含原文的模型过程事件。"""
    started = time.monotonic()
    try:
        blocks, confidence = _ocr_image_bytes_with_blocks(png, lang)
    except Exception as exc:  # noqa: BLE001 - 先记录失败页，再交给调用方处理
        emit_progress(
            "ocr",
            "failed",
            event="page_failed",
            message="OCR 页面识别失败",
            page=page_number,
            total_pages=total_pages,
            document_id=document_id,
            error_type=type(exc).__name__,
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
        raise
    emit_progress(
        "ocr",
        "running",
        event="page_completed",
        message="OCR 页面识别完成",
        page=page_number,
        total_pages=total_pages,
        document_id=document_id,
        block_count=len(blocks),
        elapsed_seconds=round(time.monotonic() - started, 1),
    )
    return blocks, confidence


def ocr_image_bytes(png: bytes, lang: str = "ch") -> tuple[str, float]:
    """对单页图像 OCR，返回 (按行拼接的文本, 平均置信度)。

    测试钩子：设置环境变量 TC_OCR_FAKE_TEXT（JSON：{"text":…,"confidence":…}）时
    不启动引擎，直接返回注入文本 —— 供子进程流水线测试使用。

    结果解析同时兼容 2.x（[[box,(text,score)], …]）与 3.x（dict 含
    rec_texts/rec_scores 的 OCRResult）两种返回形态。
    """
    blocks, confidence = ocr_image_bytes_with_blocks(png, lang)
    lines = [str(block["text"]) for block in blocks if str(block.get("text", "")).strip()]
    text = "\n".join(lines)
    return text, confidence
