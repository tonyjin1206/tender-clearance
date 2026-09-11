# SRM 逆向取数 · AI 交接文档

> 交接对象：Codex。
> 请先完整阅读本文档再动手。分五部分：**你的任务 / 已验证事实 / 背景与可参考资产 /
> 必须交付的结果 / 硬约束**。方法完全由你自行决定——本文只约定"要什么"，不约定"怎么做"。

---

## 1. 你的任务（仅此一项，其余勿动）

对富奥 SRM（用友 YonBIP 私有化部署，`https://yonbip.fawer.com.cn/`，"富奥股份SRM采购平台"）
做登录链路与查询接口的逆向，用普通**系统账户的用户名+密码**（用户运行时提供）实现
以下三类数据的**程序化只读获取**：

1. **企业信息**（供应商档案：企业名称、统一社会信用代码、地址、法定代表人、供应商等级等）；
2. **股东信息**（供应商的股东/出资结构，若 SRM 内有此数据）；
3. **经营风险**（司法诉讼、失信/处罚等，若 SRM 内有此数据）。

**背景**：Skill 中开放 API（appKey/appSecret 换 access_token）已验证可行，但用户认为
token 对一般用户太难，决定改走**网页登录会话**的逆向。你的产出是一份"取数规格包"，
由交接方（Claude）集成进清标流水线——**你不需要改 Skill 的代码**，把规格与验证结果
交回来即可。

系统归属：用户自己授权使用的内部系统，用户拥有账户并可提供测试凭据。仅只读查询，
不得创建/修改/提交 SRM 内任何业务数据。

---

## 2. 已验证事实（2026-09-09，可直接采信；建议复测）

- 站点可达（注意：本机 shell 可能带 `http_proxy/https_proxy=127.0.0.1:7890` 且该代理
  可能未运行，测试请绕过：`curl --noproxy '*'` 或 `session.trust_env=False`）。
- 门户为 React SPA + CAS SSO。根 HTML 内嵌服务前缀映射（同域名）：auth-fe、
  orgcenter-u8c、`iuap-uuas-user`、`upc-fe-supplier`（供应商门户）、`mdf-node`/`mdf-fe`、
  `iuap-apcom-workbench` 等。
- 开放 API（已实测存在，供核对语义用）：
  - 取 token：`GET /iuap-api-auth/open-auth/selfAppAuth/getAccessToken`，
    query 参数 `appKey`/`appSecret`（camelCase，缺参有明确报错）；
  - 企业信息：`POST /yonbip/cpu/tenant/query?access_token=<令牌>`，JSON 体；
    无 token 时 302 跳 `https://yonbip.fawer.com.cn/login?service=...`（CAS）；
  - 响应形如 `{code:"200", message, data:[...]}`，`data` 为数组；企业信息字段：
    enterpriseName、tradeName[]、provinceName、cityName、address、bsCode（工商代码）、
    bsName、bsRegisterTime、legalRepName、legalRepMobile、supplierLevel[]、
    platRegisterTime、tenantId；错误码 201 / 203（系统错误）/ 204（权限问题），
    随 HTTP 200 返回。
- **用户手里有 SRM 的 API 文档**（如《获取token》《获取企业信息》等章节），卡壳时
  可直接向用户索要对应章节核对——逆向结论最好与文档互相印证。
- 登录页资源在 `/iuap-apcom-workbench/ucf-wh/yonbiplogin/20241216-101855/`
  （main.js 为 loader，chunk：mainEntry.js / experienceLogin.js / vendors.js）；
  `mainEntry.js` 中有 `urlMaker`（接口统一前缀 `/iuap-apcom-workbench`）、
  `$YHT_SSO/cas/login?sysid=yonbip&mode=light...`、`/login_light`。
  **尚未定位**：账号密码提交端点、密码加密方案、会话载体。应用内查询页是 SPA，
  地址栏看不到真实接口 URL，需 devtools Network 抓 XHR。

---

## 3. 背景与可参考资产（了解即可，用不用随你）

- Skill 目录：`skills/tender-clearance/`（Python 3.12 venv 在其 `.venv`）。
  其中 `scripts/tc/srm.py` 是配置驱动的 SRM 客户端、`rules/srm-api.yaml` 是其配置、
  `scripts/srm_selftest.py` 是自测 CLI、`tests/test_srm.py` 有状态机替身测试、
  `scripts/srm_proxy.py` 是一个本地反代（127.0.0.1:8899 → SRM，用于配合桌面预览面板
  抓登录链路；**有已知 bug**：Set-Cookie 改写处对 requests Cookie 对象 item 赋值抛
  `TypeError`，导致空响应）。这些是上一阶段（Claude）的产物，你可参考、可弃用；
  你的交付物不依赖它们。
- 用户可提供的协助（需要时直接提）：
  - API 文档章节、内网环境说明；
  - 测试凭据（建议可重置的测试账号；或用户自己在终端跑你给的复现脚本，
    把**脱敏**输出发回）；
  - 浏览器配合：登录后打开目标查询页，把关键 XHR 复制为 cURL 给你。

---

## 4. 必须交付的结果（交接方将直接消费）

请交付一个**取数规格包**（建议整理为一个 markdown 文档 + 若干 JSON 样本文件，
放在工作区内即可，路径告知用户）：

1. **登录链路规格**
   - 凭据提交：端点（path）、方法、Content-Type、请求体字段名；
   - 密码处理：明文 / 加密（算法、密钥或公钥来源、编码方式）；
   - 必需请求头、CSRF/验证码机制（如有）；
   - 登录成功的判别方式（状态码/响应键/跳转）；
   - 会话载体：后续请求靠什么认证（哪些 Cookie、或 token 放哪个头/参数），
     会话有效期与续期（如观察到）。
2. **每类数据的查询接口规格**（企业信息 / 股东信息 / 经营风险，各一节）
   - path、method、请求体/参数字段（**特别注明是否支持按企业名称或统一社会信用
     代码过滤**，及字段名）；
   - 响应结构：记录列表在 JSON 中的路径、单条记录的字段清单（字段名 → 含义）；
   - 分页方式（如为列表页）。
3. **脱敏真实响应样本**：每类数据至少 1 份真实响应（JSON），脱敏规则：
   企业名可保留；统一社会信用代码保留（非个人敏感）；**手机号/身份证号/银行账户
   一律删除或掩码**；不得包含 Cookie/token 等凭据。
4. **最小复现脚本**：一段 curl 或 python requests 的命令序列（凭据用
   `$SRM_USER`/`$SRM_PASSWORD` 占位），在能连通内网的终端上可直接复现
   "登录 → 查询 → 打印记录"；交接方将用它在内网环境复跑验证。
5. **实测结论**：日期、用的测试账号类型、每类数据的查询结果状态
   （查到 / 查无 / 权限不足）、限频或权限限制的观察。

**质量口径**：规格中的每个字段名、路径都应来自你实际抓到的请求/响应（或与用户提供的
API 文档互相印证），不要凭 YonBIP 通用经验推测；推测与实测务必分开标注。

---

## 5. 硬约束（不可违反）

1. **凭据仅存内存**：用户名/密码/Cookie/token 不得写入你产出的文档、样本、脚本
   里的实际值、日志或 git；复现脚本一律用 `$SRM_USER`/`$SRM_PASSWORD` 占位。
2. **敏感个人信息**：样本与规格中不得出现完整手机号、身份证号、银行账户（删除或掩码）；
   采样时优先剔除此类字段。
3. **只读**：只做查询类操作，不得创建/修改/提交 SRM 内任何业务数据，
   不得触发流程/审批/消息类接口。
4. **限速与克制**：探测请求保持低频；查询仅限用户提供测试主体，不大范围遍历。
5. 不向 SRM 写入或上传任何文件；不下载批量数据。

---

## 6. 完成后

把取数规格包的路径告知用户（例如 `SRM逆向取数规格.md` + `samples/*.json`）。
交接方（Claude）会据此更新 `rules/srm-api.yaml`、`scripts/tc/srm.py`（增加网页会话
模式）并接入清标流水线，无需你再改代码；若你顺手给出了配置建议，Claude 会核对后采纳。

—— 文档末尾请追加你的交接备注：执行日期、方法概述、遇到的限制、未尽事项。
