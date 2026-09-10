"""核心数据模型（方案 5.2）。

pydantic 模型是唯一事实来源；`schemas/*.schema.json` 由 `gen_schemas.py`
从这些模型生成并提交。所有中间与最终结构化输出均按对应 Schema 校验。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 公共枚举 -------------------------------------------------------------------

EvidenceStrength = Literal["A", "B", "C", "D"]
AlertLevel = Literal["I", "II", "III"]
SubjectConfirmation = Literal["confirmed", "candidate", "unconfirmed"]
ExternalQueryStatus = Literal[
    "match",              # 已查到匹配记录
    "no_match_verified",  # 已成功执行查询且未返回匹配项（仅此含义，见方案 5.2）
    "no_result",          # 查询执行但返回不可判定结果（如页面无结构化数据）
    "not_queried",        # 未查询（未启用/无授权/超出范围）
    "blocked",            # 验证码、登录墙、限流、访问控制等受阻
    "failed",             # 网络/系统错误
    "needs_manual_review",  # 主体未确认或缺少主体键，转人工查询/导入
]
QueryMode = Literal[
    "official_api", "browser_session", "public_web", "manual_import", "none"
]
SourceId = Literal[
    "government_procurement",
    "srm",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 运行信息 --------------------------------------------------------------------


class RunInfo(_Strict):
    run_id: str = Field(description="本次运行的唯一标识（uuid），允许跨运行不同")
    started_at: datetime = Field(description="运行开始时间（允许跨运行不同）")
    tool_version: str
    rules_version: str
    python_version: str
    project_id: str
    redaction_mode: str = Field(description="脱敏模式；当前固定 standard")
    id_digest_salt_fingerprint: str = Field(
        description="证件摘要盐的指纹（sha256 前 8 位），不保存盐本身"
    )


# 项目配置 --------------------------------------------------------------------


class ProjectConfig(_Strict):
    project_id: str
    project_name: str
    bid_deadline: datetime | None = Field(
        default=None,
        description="投标截止时间；判断处罚/禁入是否处于有效期的唯一时间基准",
    )
    run_date: date | None = None
    supplier_directory_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="bids/ 下供应商目录名 → 报告用显示名（可选）",
    )
    procurement_rules_source: str = Field(
        default="unspecified",
        description="资格/否决条款来源（文件相对路径或 unspecified）",
    )
    procurement_clauses_mapping: str | None = Field(
        default=None,
        description="已确认的条款映射 YAML（项目根相对路径）；存在时命中的外部风险发现"
                    "自动附对应条款引用（仅注释，不改变等级）",
    )
    external_query_mode: Literal["offline", "manual_import", "live"] = Field(
        default="offline",
        description="offline：不做任何外部访问；manual_import：仅导入证据；live：允许已启用适配器访问（需授权）",
    )
    external_query_sources: list[SourceId] = Field(
        default_factory=list,
        description="live 模式下明确启用的渠道；缺省为空，即全部 not_queried",
    )
    redaction_mode: Literal["standard"] = "standard"
    retention_policy: str = Field(default="project_local")
    id_digest_salt: str = Field(
        default="tender-clearance-default-salt-v1",
        description="证件号受控摘要盐；不写入任何输出（只写指纹）",
        exclude=True,
    )
    ocr_provider: Literal["none", "mock", "paddle"] = Field(
        default="none",
        description="OCR 适配器；none 时扫描页进入人工核对；paddle 需安装 "
                    "paddleocr/paddlepaddle（可选依赖），OCR 字段走低置信度通道",
    )


# 清单 ------------------------------------------------------------------------


class DocumentRecord(_Strict):
    document_id: str
    relative_path: str = Field(description="相对项目根目录的 POSIX 路径")
    supplier_dir: str | None = Field(default=None, description="位于 bids/ 下时的供应商目录名")
    category: Literal["bid", "procurement", "external_evidence", "other"]
    bid_subtype: Literal["business", "technical", "bid_schedule", "cover", "unknown"] = "unknown"
    media_type: str
    size_bytes: int
    page_count: int | None = None
    sha256: str
    extraction_status: Literal[
        "ok", "partial", "failed", "skipped", "password_protected",
        "corrupt", "unsupported", "empty",
    ] = "ok"
    status_detail: str | None = None
    modified_at: datetime | None = Field(
        default=None,
        description="文件系统修改时间；不能单独证明文档创作时间",
    )


class ExtractionAnomaly(_Strict):
    document_id: str | None = None
    relative_path: str | None = None
    anomaly: str
    detail: str | None = None


class InventoryFile(_Strict):
    run: RunInfo
    documents: list[DocumentRecord]
    anomalies: list[ExtractionAnomaly]
    supplier_dirs: list[str] = Field(default_factory=list)


# 证据 ------------------------------------------------------------------------

EvidenceLocationKind = Literal[
    "pdf_page", "pdf_metadata", "pdf_xmp", "pdf_form", "pdf_embedded_file",
    "docx_body", "docx_table", "docx_header_footer", "docx_property",
    "xlsx_cell", "image_exif", "image_xmp", "url", "snapshot", "import_record",
]


class Evidence(_Strict):
    evidence_id: str
    source_type: Literal[
        "bid_document", "procurement_document", "external_evidence",
        "metadata", "derived",
    ]
    document_id: str | None = None
    location: dict[str, Any] = Field(
        description='定位，如 {"kind":"pdf_page","page":3}、{"kind":"xlsx_cell","sheet":"开标一览表","cell":"B2"}、{"kind":"url","url":"..."}',
    )
    field: str = Field(description="字段名，如 uscc / legal_rep_name / phone / producer")
    raw_value: str = Field(description="原文摘录；证件号/手机号已脱敏")
    normalized_value: str | None = None
    sha256: str | None = Field(default=None, description="来源文件或快照哈希")
    collected_at: datetime
    method: str = Field(description="提取方法，如 pdf_text_layer / xlsx_cell / image_exif / manual_import")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    strength: EvidenceStrength
    note: str | None = None


class EvidenceFile(_Strict):
    run: RunInfo
    evidence: list[Evidence]


# 主体 ------------------------------------------------------------------------


class Party(_Strict):
    party_id: str
    name: str
    role: Literal["legal_rep", "bid_agent", "shareholder", "contact", "project_manager"]
    id_digest: str | None = Field(default=None, description="证件号受控摘要（sha256 前 16 位）")
    id_mask: str | None = None
    supplier_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confirmation: SubjectConfirmation = "unconfirmed"


class ContactItem(_Strict):
    kind: Literal["phone", "email", "address"]
    normalized_value: str
    raw_masked: str
    supplier_id: str
    evidence_ids: list[str]


class Supplier(_Strict):
    supplier_id: str
    directory_name: str
    display_name: str
    declared_name: str | None = None
    normalized_name: str | None = None
    name_search_key: str | None = None
    uscc: str | None = None
    uscc_status: Literal["present_valid", "present_invalid_format", "absent"] = "absent"
    # 同一供应商材料中出现多个代码时，保留全部候选；不得任意取排序后的一个。
    uscc_candidates: list[str] = Field(default_factory=list)
    uscc_evidence_ids: list[str] = Field(default_factory=list)
    confirmation: SubjectConfirmation = "unconfirmed"
    confirmation_note: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EntitiesFile(_Strict):
    run: RunInfo
    suppliers: list[Supplier]
    parties: list[Party]
    contacts: list[ContactItem]
    notes: list[str] = Field(default_factory=list)


# 内容提取 --------------------------------------------------------------------


class FieldRecord(_Strict):
    document_id: str
    relative_path: str
    supplier_dir: str | None = None
    field: str
    value_masked: str = Field(description="展示值；证件号/手机号已脱敏")
    normalized: str | None = None
    digest: str | None = Field(default=None, description="证件号受控比对摘要（仅证件字段）")
    confidence: float = 1.0
    method: str
    location: dict[str, Any]
    evidence_id: str
    low_confidence: bool = False
    category: Literal["bid", "procurement"] = "bid"


class DocumentContent(_Strict):
    document_id: str
    relative_path: str
    supplier_dir: str | None = None
    status: str
    text_units: int = 0
    scanned_pages: list[int] = Field(default_factory=list)
    ocr_pages: list[int] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class ContentFile(_Strict):
    run: RunInfo
    documents: list[DocumentContent]
    fields: list[FieldRecord]
    anomalies: list[ExtractionAnomaly]


# 交叉匹配矩阵 ----------------------------------------------------------------


class CrossMatch(_Strict):
    match_id: str
    field: Literal[
        "uscc", "legal_rep_id_digest", "bid_agent_id_digest",
        "legal_rep_name", "bid_agent_name", "shareholder_name", "phone", "email",
        "contact_name", "address", "company_name",
    ]
    supplier_a: str
    supplier_b: str
    match_type: Literal["exact", "fuzzy", "candidate"]
    value_a: str = Field(description="脱敏后的比对值")
    value_b: str
    similarity: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class MatchesFile(_Strict):
    run: RunInfo
    matches: list[CrossMatch]


# 文件属性 --------------------------------------------------------------------


class MetadataField(_Strict):
    field: str
    raw_value: str
    info_class: Literal["unique_id", "limited_identifying", "common_or_modifiable"]
    evidence_id: str | None = None
    note: str | None = None


class DocumentMetadata(_Strict):
    document_id: str
    relative_path: str
    fields: list[MetadataField]
    page_image_fingerprints: list[str] = Field(
        default_factory=list, description="扫描页图像感知哈希（ahash），仅作线索"
    )


class MetadataFile(_Strict):
    run: RunInfo
    documents: list[DocumentMetadata]
    anomalies: list[ExtractionAnomaly] = Field(default_factory=list)


# 外部证据 --------------------------------------------------------------------


class ExternalQuery(_Strict):
    query_id: str
    source_id: SourceId
    subject_supplier_id: str | None = None
    subject_key: dict[str, Any] = Field(description='主体键，如 {"uscc": "..."} 或 {"name": "..."}')
    query_mode: QueryMode
    queried_at: datetime | None = None
    status: ExternalQueryStatus
    record_count: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str | None = None
    adapter_version: str | None = None


class ExternalRecord(_Strict):
    record_id: str
    source_id: SourceId
    supplier_id: str | None = None
    subject_confirmation: SubjectConfirmation = "unconfirmed"
    subject_name: str | None = None
    subject_uscc: str | None = None
    record_kind: Literal[
        "dishonesty", "penalty", "judicial_case", "ownership",
        "registration", "judicial_summary", "operating_risk", "operating_summary",
        "other",
    ]
    fields: dict[str, Any] = Field(
        description="原始记录字段，如案号/处罚决定文号/当事人/日期/禁入期限等",
    )
    effective_from: date | None = None
    effective_to: date | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class OwnershipRelation(_Strict):
    relation_id: str
    from_party_name: str | None = None
    from_party_digest: str | None = None
    from_company_name: str | None = None
    from_company_uscc: str | None = None
    to_company_name: str
    to_company_uscc: str | None = None
    share_ratio: float | None = None
    relation_date: date | None = None
    relation_end_date: date | None = None
    supplier_id: str | None = None
    subject_confirmation: SubjectConfirmation = "unconfirmed"
    evidence_ids: list[str] = Field(default_factory=list)


class ExternalEvidenceFile(_Strict):
    run: RunInfo
    queries: list[ExternalQuery]
    records: list[ExternalRecord]
    ownership: list[OwnershipRelation] = Field(default_factory=list)


# 风险发现 --------------------------------------------------------------------


class Finding(_Strict):
    finding_id: str
    rule_id: str
    rules_version: str
    domain: Literal[
        "identity", "metadata", "ownership", "judicial",
        "dishonesty", "coverage",
    ]
    level: AlertLevel
    evidence_strength: EvidenceStrength
    supplier_ids: list[str] = Field(default_factory=list)
    subject_confirmation: SubjectConfirmation = "confirmed"
    fact: str = Field(description="发现事实（脱敏、中性表述，不作违法/串标认定）")
    evidence_ids: list[str] = Field(default_factory=list)
    related_ids: list[str] = Field(
        default_factory=list, description="关联的 match_id / query_id / record_id"
    )
    status: Literal["open", "human_review_required", "resolved"] = "open"
    recommendation: str = Field(default="", description="建议复核动作")
    procurement_clause_note: str | None = Field(
        default=None,
        description="与采购文件资格条款的匹配结果；无条款时为“资格影响待采购人确认”",
    )
    missing_materials: list[str] = Field(default_factory=list)


class CoverageEntry(_Strict):
    supplier_id: str
    source_id: SourceId
    status: ExternalQueryStatus
    detail: str | None = None
    query_id: str | None = None


class FindingsFile(_Strict):
    run: RunInfo
    findings: list[Finding]
    coverage: list[CoverageEntry]
    human_review_queue: list[str] = Field(
        default_factory=list, description="必须人工复核的 finding_id"
    )


# 报告包 ----------------------------------------------------------------------


class ReportBundle(_Strict):
    run: RunInfo
    project: ProjectConfig
    inventory: InventoryFile
    entities: EntitiesFile
    matches: MatchesFile
    metadata: MetadataFile
    external: ExternalEvidenceFile
    findings: FindingsFile
    evidence: EvidenceFile


SOURCE_LABELS: dict[str, str] = {
    "government_procurement": "中国政府采购网",
    "srm": "富奥SRM",
}

ROLE_LABELS: dict[str, str] = {
    "legal_rep": "法定代表人",
    "bid_agent": "授权代表",
    "shareholder": "股东",
    "contact": "联系人",
    "project_manager": "项目负责人",
}
