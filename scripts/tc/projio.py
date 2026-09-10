"""项目目录读写、运行信息与证据构建。"""

from __future__ import annotations

import hashlib
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .canon import stable_id
from .models import Evidence, EvidenceStrength, ProjectConfig, RunInfo
from .normalize import id_digest, mask_id_number, redact_text

REQUIRED_PROJECT_FIELDS = [
    "project_id",
    "project_name",
    "bid_deadline",
    "run_date",
    "external_query_mode",
    "redaction_mode",
    "retention_policy",
]


class ProjectError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_project_config(project_dir: Path) -> ProjectConfig:
    cfg_path = project_dir / "project.yaml"
    if not cfg_path.exists():
        raise ProjectError(f"缺少 project.yaml：{cfg_path}")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProjectError(f"project.yaml 不是有效 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError("project.yaml 内容必须是键值映射")
    missing = [k for k in REQUIRED_PROJECT_FIELDS if data.get(k) in (None, "")]
    if missing:
        raise ProjectError(f"project.yaml 缺少必填字段：{', '.join(missing)}")
    # 日期字段允许写 date 或 ISO 字符串
    for key in ("bid_deadline",):
        if isinstance(data.get(key), str):
            data[key] = _parse_yaml_dt(data[key])
    return ProjectConfig(**data)


def _parse_yaml_dt(text: str) -> datetime:
    t = str(text).strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(t)
    except ValueError as exc:
        raise ProjectError(f"无法解析时间“{text}”，请使用 ISO 8601（如 2026-09-01T09:00:00+08:00）") from exc


def ensure_output_dirs(project_dir: Path) -> tuple[Path, Path]:
    out = project_dir / "output"
    interim = out / "interim"
    interim.mkdir(parents=True, exist_ok=True)
    return out, interim


def salt_fingerprint(salt: str) -> str:
    return hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8]


def make_run_info(cfg: ProjectConfig, rules_version: str) -> RunInfo:
    from . import __version__

    return RunInfo(
        run_id=uuid.uuid4().hex,
        started_at=utcnow(),
        tool_version=__version__,
        rules_version=rules_version,
        python_version=platform.python_version(),
        project_id=cfg.project_id,
        redaction_mode=cfg.redaction_mode,
        id_digest_salt_fingerprint=salt_fingerprint(cfg.id_digest_salt),
    )


def load_run_info(interim_dir: Path) -> RunInfo:
    """从已有中间产物恢复 RunInfo（跨阶段保持同一 run_id）。"""
    import json

    for name in ("inventory.json",):
        p = interim_dir / name
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return RunInfo(**data["run"])
    raise ProjectError("未找到 inventory.json；请先运行 inventory.py 创建项目清单")


def evidence_id_for(source_type: str, document_id: str | None, location: dict, field: str, method: str, raw_value: str) -> str:
    return stable_id(
        "EV",
        {
            "source_type": source_type,
            "document_id": document_id,
            "location": location,
            "field": field,
            "method": method,
            "value": raw_value,
        },
    )


class EvidenceBuilder:
    """集中创建证据：稳定 ID、强制脱敏、统一采集时间。"""

    def __init__(self, run: RunInfo, salt: str) -> None:
        self.run = run
        self.salt = salt
        self._items: list[Evidence] = []

    def add(
        self,
        *,
        source_type: str,
        document_id: str | None,
        location: dict[str, Any],
        field: str,
        raw_value: str,
        normalized_value: str | None = None,
        method: str,
        confidence: float | None = None,
        strength: EvidenceStrength,
        note: str | None = None,
        sha256: str | None = None,
        sensitive_id: str | None = None,
        sensitive_phone: str | None = None,
    ) -> Evidence:
        """sensitive_id / sensitive_phone：出现于摘录中的完整敏感值，将被掩码替换。"""
        display = raw_value
        if sensitive_id:
            display = display.replace(sensitive_id, mask_id_number(sensitive_id))
        if sensitive_phone:
            display = redact_text(display, [], [sensitive_phone])
        ev_id = evidence_id_for(source_type, document_id, location, field, method, display)
        ev = Evidence(
            evidence_id=ev_id,
            source_type=source_type,  # type: ignore[arg-type]
            document_id=document_id,
            location=location,
            field=field,
            raw_value=display,
            normalized_value=normalized_value,
            sha256=sha256,
            collected_at=self.run.started_at,
            method=method,
            confidence=confidence,
            strength=strength,  # type: ignore[arg-type]
            note=note,
        )
        self._items.append(ev)
        return ev

    def digest_of(self, id_number: str) -> str:
        return id_digest(id_number, self.salt)

    def extend(self, items: list[Evidence]) -> None:
        self._items.extend(items)

    @property
    def items(self) -> list[Evidence]:
        return self._items
