"""宿主 OCR 契约的校验、指纹和缓存工具。

Core 只处理契约对象，不导入 OCR 引擎、模型或浏览器运行时。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canon import canonical_json, content_hash, stable_id
from .models import (
    OCRCapabilities,
    OCRCapabilitiesFile,
    OCRJob,
    OCRResult,
)


REQUIRED_LANGUAGES = {"zh-Hans", "en"}
REQUIRED_INPUT_FORMATS = {"pdf_page", "png", "jpeg", "jpg"}


def validate_capabilities(capabilities: OCRCapabilities) -> list[str]:
    """返回拒绝原因；空列表表示满足 Core 最低能力。"""
    reasons: list[str] = []
    if not REQUIRED_LANGUAGES.issubset(set(capabilities.languages)):
        reasons.append("缺少 zh-Hans、en 或数字识别能力声明")
    if not REQUIRED_INPUT_FORMATS.intersection(set(capabilities.input_formats)):
        reasons.append("不支持 PDF 页或 PNG/JPEG 输入")
    if not capabilities.returns_confidence:
        reasons.append("未返回置信度")
    if not capabilities.returns_block_coordinates and capabilities.location_precision != "page_only":
        reasons.append("坐标能力声明不一致")
    if not capabilities.provider_id or not capabilities.provider_version:
        reasons.append("缺少 provider_id/provider_version")
    if not capabilities.model_names:
        reasons.append("缺少实际模型版本声明")
    return reasons


def capability_file(capabilities: OCRCapabilities, *, checked_at: datetime | None = None) -> OCRCapabilitiesFile:
    reasons = validate_capabilities(capabilities)
    return OCRCapabilitiesFile(
        capabilities=capabilities,
        checked_at=checked_at or datetime.now(timezone.utc),
        accepted=not reasons,
        rejection_reasons=reasons,
    )


def provider_fingerprint(result: OCRResult, job: OCRJob) -> str:
    """缓存键中的 Provider 指纹；不包含结果文本。"""
    return content_hash({
        "provider": result.provider.model_dump(mode="json"),
        "languages": job.languages,
        "mode": job.mode,
        "location_precision": result.location_precision,
    })[:24]


def job_for_page(
    *,
    document_id: str,
    source_sha256: str,
    page: int,
    page_image_sha256: str,
    input_ref: str,
) -> OCRJob:
    return OCRJob(
        contract_version="ocr-job.v1",
        job_id=stable_id("OCRJ", {
            "document_id": document_id,
            "source_sha256": source_sha256,
            "page": page,
            "page_image_sha256": page_image_sha256,
        }),
        document_id=document_id,
        source_sha256=source_sha256,
        page=page,
        page_image_sha256=page_image_sha256,
        input_ref=input_ref,
    )


def validate_result_against_job(result: OCRResult, job: OCRJob) -> list[str]:
    """严格校验结果是否对应任务；返回所有错误而非静默修正。"""
    errors: list[str] = []
    if result.contract_version != "ocr-result.v1":
        errors.append("contract_version 不匹配")
    if result.job_id != job.job_id:
        errors.append("job_id 不匹配")
    if result.document_id != job.document_id:
        errors.append("document_id 不匹配")
    if result.source_sha256 != job.source_sha256:
        errors.append("source_sha256 不匹配")
    if result.page != job.page:
        errors.append("page 不匹配")
    if result.status == "succeeded":
        if not result.blocks:
            errors.append("成功结果缺少 blocks")
        if result.average_confidence is None:
            errors.append("成功结果缺少 average_confidence")
    return errors


def cache_path(cache_root: Path, job: OCRJob, result: OCRResult) -> Path:
    return (
        cache_root
        / "ocr"
        / job.source_sha256
        / str(job.page)
        / f"{provider_fingerprint(result, job)}.json"
    )


def safe_input_ref(project_dir: Path, path: Path, page: int) -> str:
    """生成不带绝对路径的受控页引用。"""
    rel = path.relative_to(project_dir).as_posix()
    return f"{rel}#page={page}"


def result_to_json(result: OCRResult) -> dict[str, Any]:
    return result.model_dump(mode="json")

