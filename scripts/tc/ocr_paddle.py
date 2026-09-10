"""PaddleOCR 适配器（生产 OCR，方案 §10.4）。

- 懒加载：仅当 `ocr_provider: paddle` 时导入 paddleocr（重依赖不装也能离线运行）；
- 引擎进程内单例（模型加载慢，只初始化一次）；
- 输入为页面渲染的 PNG 字节，输出 (全文, 平均置信度)；
- OCR 文本进入既有低置信度通道：字段 confidence<1、不参与精确匹配、进人工核对；
- 本模块不做任何脱敏外加工，脱敏仍由提取/报告层完成。
"""

from __future__ import annotations

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
            "未安装 PaddleOCR：请执行 "
            "`uv pip install -p .venv/bin/python paddleocr paddlepaddle` "
            "（或在 project.yaml 将 ocr_provider 改回 none/mock）"
        ) from exc
    try:
        _ENGINE = PaddleOCR(use_textline_orientation=True, lang=lang)
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


def ocr_image_bytes(png: bytes, lang: str = "ch") -> tuple[str, float]:
    """对单页图像 OCR，返回 (按行拼接的文本, 平均置信度)。

    测试钩子：设置环境变量 TC_OCR_FAKE_TEXT（JSON：{"text":…,"confidence":…}）时
    不启动引擎，直接返回注入文本 —— 供子进程流水线测试使用。

    结果解析同时兼容 2.x（[[box,(text,score)], …]）与 3.x（dict 含
    rec_texts/rec_scores 的 OCRResult）两种返回形态。
    """
    import json as _json
    import os as _os

    fake = _os.environ.get("TC_OCR_FAKE_TEXT")
    if fake:
        try:
            data = _json.loads(fake)
            return str(data["text"]), float(data.get("confidence", 0.8))
        except (ValueError, KeyError):
            return fake, 0.8

    engine = get_engine(lang)
    import io

    import numpy as _np
    from PIL import Image as _Image

    img = _np.array(_Image.open(io.BytesIO(png)).convert("RGB"))
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

    lines: list[str] = []
    scores: list[float] = []
    for block in raw or []:
        if isinstance(block, dict) and "rec_texts" in block:  # 3.x OCRResult
            lines += [str(t) for t in block.get("rec_texts", []) if str(t).strip()]
            scores += [float(s) for s in block.get("rec_scores", [])]
            continue
        for line in (block or []):
            try:
                text, score = line[1]
            except (TypeError, ValueError, IndexError):
                continue
            if str(text).strip():
                lines.append(str(text).strip())
                scores.append(float(score))
    text = "\n".join(lines)
    conf = (sum(scores) / len(scores)) if scores else 0.0
    return text, round(conf, 4)
