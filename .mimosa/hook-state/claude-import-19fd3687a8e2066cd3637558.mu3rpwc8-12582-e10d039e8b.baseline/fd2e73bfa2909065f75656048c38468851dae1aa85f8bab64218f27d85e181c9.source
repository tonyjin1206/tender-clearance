#!/usr/bin/env python3
"""从 tc.models 生成 schemas/*.schema.json（pydantic 模型是唯一事实来源）。

重新生成：python gen_schemas.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import BaseModel

from tc import models
from tc.canon import write_json

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

# 方案要求的五个契约名 + 过程产物 Schema
EXPORTS = {
    "project-manifest.schema.json": models.ProjectConfig,
    "evidence.schema.json": models.EvidenceFile,
    "supplier.schema.json": models.EntitiesFile,
    "finding.schema.json": models.FindingsFile,
    "external-query.schema.json": models.ExternalEvidenceFile,
    "inventory.schema.json": models.InventoryFile,
    "content.schema.json": models.ContentFile,
    "matches.schema.json": models.MatchesFile,
    "metadata.schema.json": models.MetadataFile,
    "report-bundle.schema.json": models.ReportBundle,
    "ocr-capabilities.schema.json": models.OCRCapabilitiesFile,
    "ocr-job.schema.json": models.OCRJobFile,
    "ocr-result.schema.json": models.OCRResultFile,
}


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for name, model in EXPORTS.items():
        assert issubclass(model, BaseModel)
        write_json(SCHEMA_DIR / name, model.model_json_schema())
        print(f"generated {name}  <- {model.__name__}")


if __name__ == "__main__":
    main()
