# 数据契约（data-contract）

## 1. 项目输入目录

```text
<project>/
├── project.yaml
├── bids/
│   ├── 供应商A/
│   │   ├── 开标一览表.xlsx
│   │   ├── 商务标.pdf
│   │   └── 技术标.pdf
│   └── 供应商B/...
├── procurement/
│   └── 采购文件.pdf
├── external-evidence/
│   ├── government-procurement/  # 中国政府采购网
│   └── srm-authorized-export/   # 富奥SRM 授权导出
└── output/
```

## 2. project.yaml

```yaml
project_id: PRJ-2026-001            # 必填
project_name: XX 园区综合运维服务采购  # 必填
bid_deadline: 2026-08-31T09:00:00+08:00  # 必填；判断处罚/禁入有效期的唯一时间基准
run_date: 2026-09-09                # 必填
supplier_directory_mapping:          # 可选：目录名 → 报告显示名
  供应商A: 北京某某科技有限公司
procurement_rules_source: procurement/采购文件.pdf  # 或 unspecified
external_query_mode: offline         # offline / manual_import / live
external_query_sources: []           # live 模式下明确启用的渠道
redaction_mode: standard
retention_policy: project_local
id_digest_salt: <项目内固定的随机串>   # 可选；只写指纹进输出，绝不外泄
ocr_provider: none                   # none / mock（mock 仅测试）
```

`bid_deadline` 缺失时禁止产生“有效期内”的结论。

## 3. 结构化对象（JSON Schema 见 schemas/，由 tc/models.py 生成）

| 对象 | 必填字段 | 要点 |
|---|---|---|
| document | document_id、relative_path、media_type、size_bytes、sha256、extraction_status | 原文件不被脚本修改 |
| evidence | evidence_id、source_type、location、field、raw_value、collected_at、method、strength | 定位可为 PDF 页码、Office 属性、表格坐标、URL、快照、导入记录号；raw_value 已脱敏 |
| supplier | supplier_id、directory_name、declared_name、uscc/uscc_status、confirmation | confirmation ∈ confirmed/candidate/unconfirmed；不可强制填充信用代码 |
| party | party_id、name、role、id_digest/id_mask、evidence_ids | 角色含 legal_rep/bid_agent/shareholder/contact/project_manager |
| external_query | query_id、source_id、subject_key、query_mode、queried_at、status、record_count | status ∈ match/no_match_verified/no_result/not_queried/blocked/failed/needs_manual_review |
| external_record | record_id、source_id、record_kind、fields、subject_confirmation | 主体未确认时 supplier_id 必须为空 |
| ownership | relation_id、from_party/from_company、to_company、share_ratio、relation_date/end_date | 有向边：股东/控制人 → 被投资企业 |
| finding | finding_id、rule_id、domain、level、evidence_strength、supplier_ids、fact、evidence_ids、status | finding 不存没有证据的断言（COV-001 除外） |
| coverage | supplier_id、source_id、status | 供应商 × 渠道全覆盖 |

**`no_match_verified` 的唯一含义**：在记录的渠道、主体键和查询时间下，已成功执行查询
但未返回匹配项。它绝不等同于“该企业不存在其他风险”。

## 4. 外部证据导入契约（import_external_evidence.py）

### JSON（推荐）

```json
{
  "channel": "government_procurement",
  "uscc": "91350100M000100Y43",
  "supplier_name": "北京某某科技有限公司",
  "supplier_directory": "供应商A",
  "queried_at": "2026-09-01T10:00:00+08:00",
  "query_mode": "manual_import",
  "querier": "查询人姓名",
  "source_url": "查询入口 URL",
  "status": "match",
  "evidence_strength": "B",
  "records": [
    {
      "record_kind": "dishonesty",
      "subject_uscc": "91350100M000100Y43",
      "fields": {"当事人": "...", "行为": "...", "处罚": "...", "处罚决定文号": "...", "处理日期": "2026-01-12"},
      "effective_from": "2026-01-12",
      "effective_to": "2027-01-11"
    }
  ],
  "ownership": [
    {
      "from_party_name": "李四",
      "from_party_id_number": "110101197001014118",
      "to_company_name": "北京某某科技有限公司",
      "to_company_uscc": "91350100M000100Y43",
      "share_ratio": 60,
      "relation_date": "2020-01-01",
      "relation_end_date": null
    }
  ]
}
```

- `status` 取值与 external_query.status 一致；`blocked`/`failed`/`needs_manual_review`
  的导入同样有效且必须如实展示（T07）。
- 主体归属：`uscc` 与供应商一致 → confirmed；仅名称一致 → candidate（记录不归属该供应商）；
  都没有 → unconfirmed。

### CSV

必需列：`record_kind`；推荐列：`uscc`/`subject_name`、`queried_at`、`effective_from`、
`effective_to`；其余非空列进入 `fields`。每个主体组生成一条 query 记录，状态为 `match`。

### 快照（PDF/HTML/PNG/JPG）

同名 `.meta.json` 必须包含：`queried_by`、`queried_at`、`url_or_channel`、`subject`、
`visible_result`（可选 `uscc`、`status`）。缺任一项时证据强度封顶 C。

## 5. 规范化规则

- 企业名称：NFKC、去空白派生检索键；保留原文；名称相似只是候选。
- 统一社会信用代码：GB 32100-2015 校验位（字符集 `0123456789ABCDEFGHJKLMNPQRTUWXY`，
  权重 3^(i-1) mod 31）；不合格记“格式异常”，不自动纠正。
- 联系方式：固话/手机去分隔符、分机单列；**手机号的规范化值为受控摘要**（P+sha256 前 16 位），
  展示用掩码 `138****0001`；不同手机号尾号相同不算匹配。
- 地址：去空白标点的比对键 + 行政区划层级键（省+市+区县）；后者只作 III 级线索。
- 身份证件号：受控比对摘要 = sha256(salt + 完整号码) 前 16 位；展示掩码 `1101**********7258`；
  完整号码不写入结果、日志、异常堆栈。
- 日期时间：统一 ISO 8601；无时区元数据按 UTC 比较；文件系统时间不能单独证明创作时间。

## 6. ID 稳定性

所有 ID = `<前缀>-` + sha256(规范化 JSON)[:12]。证据 ID 由
(source_type, document_id, location, field, method, 展示值) 派生；发现 ID 由
(rule_id, 规则键) 派生 —— 修改一条原始证据只影响其相关发现。
