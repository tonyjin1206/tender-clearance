# 外部数据源（external-sources）

## 渠道范围（2026-09-09 决策）

**外部仅保留中国政府采购网，内部仅保留富奥 SRM。**
信用中国（北京）、军队采购网、公开工商信息、司法公开信息四类渠道已整体移除
（移除前的探测结论：信用中国接口有反爬挑战 40001、军采接口要求 RSA 签名头、
工商/司法公开入口有人机校验 —— 均属技术保护措施，当时已按约束未绕过）。

| 渠道 source_id | 名称 | 入口 | 凭据 | 自动化状态 |
|---|---|---|---|---|
| government_procurement | 中国政府采购网 | 用户指定人工入口：http://219.143.74.201/search/cr/；自动化公开表单：https://www.ccgp.gov.cn/cr/list | 无需账号密码 | ✅ 自动化通道已验证；指定 IP 入口需在当前网络单独记录可达性和证据截图 |
| srm | 富奥 SRM（用友 YonBIP，内网） | https://yonbip.fawer.com.cn/#/ | 系统账户（运行时提供，仅内存） | ✅ **端到端实测通过**（2026-09-09，无头后台）：登录→高级查询唯一命中→画像→身份核对→基本信息/司法风险/经营风险结构化提取。凭据仅运行时内存；验证码→人工接管（有头）；只读 |

## 政采网自动化通道（已启用）的技术说明

- **入口**：`POST https://www.ccgp.gov.cn/cr/list`，表单字段 `orgName`（企业名称）、
  `orgCode`（统一社会信用代码）、`enforceUnit`、`punishTime`/`punishTimeMax`、`gp`（页码）。
  该表单即网站面向公众的查询页，无需登录、无验证码、无签名头。
- **解析**：`response_type: html_table`，行正则 `<tr class="trShow">(.*?)</tr>`，
  10 列按 `field_map` 命名（序号/企业名称/统一社会信用代码/企业地址/行为情形/
  处罚结果/处罚依据/处罚日期/公布日期/执法单位）。
- **状态判定**：页面明示“没有该企业的相关记录” → `no_match_verified`；
  有记录 → `match`；出现验证码/登录/频率特征 → `blocked`；网络错误 → `failed`。
- **主体归属**：按 `orgCode` 查询且行内代码一致 → `confirmed`；仅按名称查询 →
  `candidate`（记录不自动归属供应商）。
- **限速与范围**：≥5 秒/次（配置 `rate_limit_seconds`），仅查询本项目供应商主体，
  默认只取第 1 页（`pages: 1`），更多页转人工核对。
- **证据留存**：命中记录转为 `ExternalRecord`（`record_kind: dishonesty`）+ B 级证据；
  响应摘要写入查询记录 detail。禁入期限载于“处罚结果”自由文本，**有效期判断
  需人工核对处罚决定原文**（适配器不解析期限语义）。

## 使用边界（必须遵守）

1. 对公开平台**只使用其允许的公开查询入口、正式 API 或人工导入的查询证据**；
   不规避验证码、登录、访问频率限制或网站技术保护措施（包括请求签名头）。
2. 适配器遇到以下任一情况立即停止自动化并标记状态：
   - 人机校验/验证码特征（`blocked_indicators`）→ `blocked`；
   - 401/403/登录跳转/反爬挑战（如 creditchina 的 40001）→ `blocked`；
   - 网络错误/超时 → `failed`；
   - 未配置已验证 endpoint 或解析规则 → `needs_manual_review`；
   - 仅名称无信用代码、且渠道仅支持按代码查询 → `needs_manual_review`（同名结果不得自动归属）。
3. 不登录未授权账户；不在 Skill/配置/输出/日志中保存账号、密码、Cookie。
4. 真实站点自动化变更时，必须重新确认：访问方式、授权范围、查询字段、限频规则、
   证据留存要求，并完成一次人工验收（本文件即政采网通道的验收记录）。

## 适配器架构

```python
class SourceAdapter(Protocol):
    source_id: str
    def query(self, subject: QuerySubject, as_of: datetime) -> AdapterResult: ...
```

- `HttpSourceAdapter`（`scripts/tc/sources.py`）：通用只读适配器，支持
  GET 查询参数与 POST 表单（`{name}`/`{uscc}` 占位符）、逐页抓取（`pages`/`page_param`）、
  两种解析器（`pattern` 正则命名组 / `html_table` 行×列）；`HttpTransport` 可注入，
  测试用替身覆盖 match / no_match_verified / no_result / blocked / failed /
  needs_manual_review 全部状态流转。
- `SrmAdapter`：按宿主注入选择浏览器会话客户端（`tc/srm_browser.py`，已验证）或
  HTTP/token 客户端（`tc/srm.py`，配置驱动），两者严格分离；均未配置时返回
  `needs_manual_review`；数据也经 `external-evidence/srm-authorized-export/` 导入。
- 渠道配置在 `rules/external-sources.yaml`（portal_url、endpoint、method、表单模板、
  解析规则、限速、阻断特征）。

## 查询流程

1. 先以统一社会信用代码查询；无代码时可用全称作人工复核候选，**不能把同名结果自动归属**。
2. 每次查询记录：主体键、查询时间、请求方式（正式 API/允许的公开网页/人工导入）、
   状态、原始响应/快照引用、解析记录、错误原因、适配器版本。
3. 同一供应商 × 渠道合并时保留信息量最高的状态：
   match > no_match_verified > blocked > failed > no_result > needs_manual_review > not_queried。
   已有人工导入证据的（源，供应商）不再重复发起实时查询。

## 人工查询 + 导入（creditchina / military / public_corporate / judicial）

1. 按上表入口逐供应商查询（不需要账号的公开页直接访问）；
2. 将结果按 `references/data-contract.md` 第 4 节的 JSON/CSV/快照契约放入对应子目录；
3. 运行 `import_external_evidence.py`；被阻断（验证码）就导入 `status: "blocked"` 的记录，
   **不要**为了取得“干净结果”而重试绕过。

## 富奥 SRM 浏览器会话模式（已验证）

由 Codex 于 2026-09-09 在真实页面验证（样本：`samples/srm_tencent_browser_session_validation_sanitized.json`），
Claude 完成流水线集成。要点：

- **客户端** `scripts/tc/srm_browser.py`（`SrmBrowserClient`）：只负责凭据内存生命周期、
  登录墙/验证码/权限阻断/主体歧义状态机、把浏览器读到的可见页面结构化为
  `AdapterResult`（`request_mode=browser_session`）。**不调用任何 HTTP 实现**，
  与 `tc/srm.py` 的 HTTP/token 客户端严格分离。
- **驱动桥接** `SrmBrowserDriver` 协议：宿主实现 open/login/search_subject/
  read_section/close 五个页面动作。已提供两种宿主：
  - `tc/srm_playwright_driver.py`（Playwright 真实浏览器）：登录页 CAS iframe
    结构已实测（#username/#password/#submit_btn_login，验证码 #inputCode 出现时
    → `manual` 人工接管，密码错误 → `blocked`）；Cookie 非持久化、close 即销毁；
  - 流水线集成：`SrmAdapter` 在宿主注册了驱动工厂（或环境变量
    `SRM_BROWSER_DRIVER=playwright`）时自动走浏览器模式，**默认无头后台**
    （用户只需对话中提供账号密码）；`SRM_BROWSER_HEADED=1` 为可选有头模式
    （验证码人工接管）；未注册时回退 HTTP/token 配置或 needs_manual_review。
- **页面导航顺序**（验证过的流程）：供应商档案 → 高级查询 → 供应商条件输入名称 →
  查询 → **必须恰好命中 1 家**（0 条或多条 → needs_manual_review，不自动选择）→
  打开档案 → 更全面企业信息 → 企业画像 → 核对名称与统一社会信用代码 →
  读取基本信息（含股东/对外投资入口计数）→ 司法风险 → 经营风险。
- **解读纪律**：分类计数只生成 `judicial_summary`/`operating_summary` 记录（不产生
  风险等级）；空分类/禁用分类/空 XHR 一律**不解释为"无风险"**；页面无法结构化 →
  `no_result`；结果只存页面证据引用（URL/页面标题/分类/快照），不存 Cookie/token/HTML。
- **运行方式**：
  - 单次查询自测：`scripts/srm_browser_selftest.py '<企业名或信用代码>'`
    （凭据走 `SRM_USER`/`SRM_PASSWORD` 环境变量或交互输入，不落盘不回显）；
  - 流水线：`external_query_mode: live` + `external_query_sources: [srm]` +
    `SRM_BROWSER_DRIVER=playwright`。
- **端到端实测已通过**（2026-09-09，无头后台，真实凭据）：全链路
  登录→检索→画像→三类页面读取均按真实 DOM 校准（应用搜索入口、
  yonicon-gaojichaxun 高级查询按钮、yssupplierInputcode 输入框、btnDirectSearch
  提交、div 网格+Shadow DOM 穿透读取、名称列截断按前缀匹配+画像页精确核对）。
  已验证"纯参数直达画像"不成立（portrait_id 必须实际观察），标准路径为最终方案。
