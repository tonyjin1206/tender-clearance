# 富奥 SRM 网页会话逆向取数规格

> 实测日期：2026-09-09  
> 测试主体：丹东富田精工机械有限公司  
> 测试入口：富奥股份 SRM 供应商档案 → 供应商画像 → 更全面企业信息  
> 范围：只读页面查询与 Network/XHR 观察；没有创建、修改、提交、审批或消息操作。

## 结论

当前已经确认企业画像页使用 `getDimensBycfId` 查询维度数据，成功请求的公共形状为：

```text
GET https://c2.yonyoucloud.com/iuap-data-ep/intellid/outside/custom/getDimensBycfId
```

固定参数：`isAjax=1`、`dataId`、`cfId`、`companyName`、`dataCode=10`、`source=public`、`enterpriseType=0`、`funcCode=ent_info_view`；另外带当前画像页的 `tabId`，并未观察到分页参数进入该 XHR。实测 HTTP 状态为 `200`，业务响应外层为 `status/result/msg/ext/...`。

企业基本信息已经取得一份真实 Network 响应；股东数据在基本信息页真实渲染，页面入口的维度 ID 已取得，但独立股东响应没有在本轮 Network 日志中单独出现；经营风险页真实渲染了 3 条股权出质记录，但本轮捕获到的风险 XHR 返回的是成功但空的 `datas`，与页面表格不是同一份可直接复用的响应。因此下文把“已捕获的 XHR”和“页面展示证据”分开，不把推断写成接口事实。

## 1. 登录链路

### 已观察

- 门户是 React SPA + CAS SSO；根页面进入后加载供应商档案及企业画像 iframe。
- 登录资源前缀为 `/iuap-apcom-workbench/ucf-wh/yonbiplogin/20241216-101855/`。
- 登录前端代码中观察到 `$YHT_SSO/cas/login?sysid=yonbip&mode=light` 与 `/login_light` 字样。
- 已登录页面把一个短期 `loginToken` 放入外层 iframe 的 `service` URL；该值属于凭据，未写入本文档、样本或脚本。
- 登录成功的页面级判据：供应商档案可打开，企业画像 iframe 加载成功，目标企业基本信息及风险页可见。

### 尚未确认，禁止按常见 YonBIP 经验补写

本轮是在已登录浏览器中开始，未捕获账号密码提交动作，所以以下项目均为 `[待确认]`：

| 项目 | 结论 |
|---|---|
| 凭据提交 path/method/Content-Type | `[待确认]` |
| 请求体字段名及密码算法 | `[待确认]`；不能据此假定明文、RSA 或 AES |
| 公钥、盐、编码方式 | `[待确认]` |
| CSRF、验证码及失败重试 | `[待确认]` |
| 登录响应字段及精确成功码 | `[待确认]` |
| Cookie 名称、有效期、续期方式 | `[待确认]`；仅确认浏览器会话可继续访问 |

因此当前可交接的自动化边界是“复用已登录会话查询”，不是“已验证的用户名/密码自动登录”。需要完整登录规格时，应在测试账号退出后清空 Network 日志，再由用户手动登录一次并提供脱敏 HAR，或在浏览器 Network 中补抓提交请求。

## 2. 企业信息

### 实测请求

```text
GET /iuap-data-ep/intellid/outside/custom/getDimensBycfId
tabId=0
dataId=f1dd4bd1785ecf875d26df335e08b897
cfId=xf808081675a42dd01675d50979b0002
companyName=丹东富田精工机械有限公司
dataCode=10
source=public
enterpriseType=0
funcCode=ent_info_view
```

HTTP `200`。完整 URL 见脱敏样本的 `request` 对象；样本不含 Cookie、token 或联系方式。

### 响应结构

- 外层：`status`、`result`、`msg`、`ext`、`level`、`errorCode`、`displayCode`、`message`、`detailMsg`、`traceId`。
- `result.viewType`：`basictable`。
- `result.datas`：单个企业对象，不是列表；本实测返回 `name`、`creditCode`、`regLocation`、`legalPersonName`、`regCapital`、`actualCapital`、`estiblishTime`、`regStatus`、`companyOrgType`、行业字段、经营范围、人员规模等。
- `result.metaAttrs`：字段元数据列表，含 `attrName`、`viewName`、`viewType`、`type`、`rank`、`fetchDataColumn` 等。
- 页面同时展示“发起人/股东”“工商股东”“对外投资”“主要人员”“变更记录”表格。已确认页面字段见 `samples/srm_shareholders_observed_sanitized.json`；这些表格的独立 XHR path 本轮未确认。

### 过滤与分页

- 页面请求按 `dataId`、`companyName` 取数；本轮未观察到按统一社会信用代码的 XHR 参数。
- 基本信息主对象不是分页列表。
- 页面表格显示分页控件，默认每页 5 条；发起人/股东 1 条、工商股东 1 条、对外投资 4 条、主要人员 3 条、变更记录 41 条（变更记录显示 9 页）。对应表格查询参数及后端分页接口 `[待确认]`。

## 3. 股东信息

### 页面入口（已观察）

基本信息页显示：

- 发起人/股东入口：`.../portrait/detail/10/8a865144726507bc0172650eb5cd01f4`，1 条；
- 工商股东入口：`.../portrait/detail/10/8a865144726507gs0172650eb5cd0120`，1 条；
- 对外投资入口：`.../portrait/detail/10/8a865144726507bc0172650eb5cd01f5`，4 条。

这些是页面链接中的维度标识，不把它们直接声称为已经捕获的 `getDimensBycfId` XHR `cfId`。本轮点击这些入口没有产生新的可见 Network 行，因此独立请求 path、method、参数和原始 JSON 均为 `[待确认]`。

### 页面真实结果

- 发起人/股东：1 条；投资金额 3500 万人民币；认缴比例 100%；认缴日期 2023-10-13；出资方式货币。
- 工商股东：1 条；自然人股东；证照类型为居民身份证，但证件号码在页面为空；持股比例 1.0；认缴出资额 3500 万人民币；首次持股日期 2019-09-05。
- 对外投资：4 条，页面展示被投资企业、法人、注册资本、统一社会信用代码、投资金额、投资占比、成立日期、状态、公司类型。

页面字段及脱敏后的真实观测记录见 `samples/srm_shareholders_observed_sanitized.json`。个人姓名已掩码，证件号码、电话和邮箱未写入样本。

## 4. 经营风险

### 本轮实际捕获的 XHR

```text
GET /iuap-data-ep/intellid/outside/custom/getDimensBycfId
tabId=2
dataId=f1dd4bd1785ecf875d26df335e08b897
cfId=8a865144726507bc0172650ed3160282
companyName=丹东富田精工机械有限公司
dataCode=10
source=public
enterpriseType=0
funcCode=ent_info_view
```

HTTP `200`，业务 `status=1`、`msg=执行成功`，但 `result.datas` 为 `{number:0,size:5,content:null,totalElements:0}`；该真实响应见 `samples/srm_risk_xhr_sanitized.json`。它不能直接作为股权出质记录接口使用。

### 页面展示结果

经营风险页实际可见的分类按钮中，经营异常、行政处罚、行政处罚（信用中国）、严重违法、欠税公告、动产抵押、司法拍卖、清算信息、知识产权出质、税收违法、环保处罚、环保处罚等均为 disabled；“股权出质 (3)”可用。

股权出质入口链接的维度 ID 为 `8a865144726507bc0172650ed316027f`，页面真实展示 3 条记录，字段为：登记日期、出质股权标的企业、出质人、质权人、状态、出质股权数额、登记编号、操作。脱敏页面样本见 `samples/srm_risk_ui_sanitized.json`。

股权出质对应的独立 JSON 响应及分页参数本轮未抓到，标记为 `[待确认]`。不要把 `60282` 的空响应与 `6027f` 的页面链接混用。

## 5. 会话复现脚本边界

`srm_replay.py` 只复用浏览器会话 Cookie 调用已经确认的只读 XHR，不保存 Cookie/token，也不输出响应中的凭据和联系方式。由于用户名/密码提交端点没有在本轮被实测，脚本不会把 `SRM_USER`/`SRM_PASSWORD` 猜测性地发送到任何 path；这也是当前交付中唯一不能声称“登录→查询全链路可直接复现”的部分。

示例（值只在当前 shell/进程内提供，不要写入文件）：

```bash
export SRM_USER='<运行时用户名>'
export SRM_PASSWORD='<运行时密码>'
export SRM_COOKIE='<从已登录浏览器复制的脱敏前 Cookie，仅在运行时环境中提供>'
python3 srm_replay.py --kind basic --name '丹东富田精工机械有限公司'
python3 srm_replay.py --kind risk --name '丹东富田精工机械有限公司'
```

脚本会拒绝在没有 `SRM_COOKIE` 时伪造“已登录”，并以脱敏 JSON 打印结果；不会把 Cookie 写盘。

## 6. 交接备注

- 方法：已登录 Chrome 页面查询 + DevTools Network 清空日志后逐项点击页面标签，记录请求 path、参数、状态和响应结构；未抓取或保存凭据。
- 查到：企业基本信息真实 XHR；企业基本信息页股东/对外投资/人员/变更表格真实渲染；经营风险页股权出质 3 条真实渲染。
- 查无/不可用：本测试主体的多数风险分类按钮 disabled；当前捕获的风险 XHR 成功但为空。
- 限制：shell 直连当前无法解析 SRM 域名；浏览器会话的登录提交动作已发生在本轮开始前，故网页登录端点、密码处理、Cookie 名称/有效期未验证。
- 未尽：补抓退出后重新登录的登录请求；补抓股东表格和 `6027f` 股权出质维度的独立 XHR；确认分页参数与会话续期。

## 7. 腾讯云计算（北京）有限责任公司补充实测

> 实测日期：2026-09-09  
> 企业名称：腾讯云计算（北京）有限责任公司  
> 统一社会信用代码：911101085636549482  
> 企业画像 `dataId`：`788a17caf10f6b629cc58c92f0d0d0f4`

### 7.1 基本信息

基本信息真实 XHR：

```text
GET /iuap-data-ep/intellid/outside/custom/getDimensBycfId
tabId=0
dataId=788a17caf10f6b629cc58c92f0d0d0f4
cfId=xf808081675a42dd01675d50979b0002
companyName=腾讯云计算（北京）有限责任公司
dataCode=10
source=public
enterpriseType=0
funcCode=ent_info_view
```

HTTP `200`，响应 `status=1`、`msg=执行成功`，`result.viewType=basictable`，`result.datas` 为单个企业对象。脱敏真实响应见 `samples/srm_tencent_basic_response_sanitized.json`。

页面与响应中已确认的重点字段：

| 字段 | 实测值 |
|---|---|
| 企业名称 | 腾讯云计算（北京）有限责任公司 |
| 法定代表人 | 李强 |
| 注册资本 / 实缴资本 | 104250.000000 万人民币 / 104250.000000 万人民币 |
| 成立日期 | 2010-10-21 |
| 企业状态 | 存续（在营、开业、在册） |
| 企业类型 | 有限责任公司(法人独资) |
| 一级/二级行业 | 信息传输、软件和信息技术服务业 / 互联网和相关服务 |
| 注册地址 | 北京市海淀区西北旺东路10号院西区9号楼4层101 |
| 人员规模 / 参保人数 | 1539 / 1539 |
| 曾用名 | 北京康盛新创科技有限责任公司 |
| 供应商画像附带表格 | 发起人/股东 1 条、工商股东 1 条、对外投资 37 条、分支机构 10 条、主要人员 5 条、变更记录 35 条 |

原始响应中的 `contactEmail`、`contactMobile`、`contactTel` 已从样本删除；法定代表人姓名在样本中掩码。

### 7.2 司法风险

司法风险页真实显示以下数据量：

| 分类 | 数量 | 页面字段 |
|---|---:|---|
| 法律诉讼 | 72 | 发布日期、案件名称、案由、原告、被告、案号、金额、状态 |
| 历史被执行人 | 1 | 立案日期、案号、被执行人名称、执行法院、执行标的 |
| 法院公告 | 13 | 刊登日期、原告、当事人、法院名、公告类型名称、公告号 |
| 开庭公告 | 266 | 开庭日期、案号、案由、原告/上诉人、被告/被上诉人、当事人、法院、法庭 |

页面还显示“失信被执行人”“历史失信被执行人”“被执行人”“股权冻结”“限制高消费”“终本案件”为 disabled。

页面入口的维度标识已观察到：法律诉讼 `9a865144726507bc0172650ec5160315`、历史被执行人 `8b865144326507bc0172650ec5160244`、法院公告 `8a865144726507bc0172650ec5160255`、开庭公告 `8a865144726507bc0172650ec43a0247`。本轮清空 Network 后切换司法风险没有产生新的可见 XHR 行，因此这些是页面链接 ID，不直接宣称为已验证的 `getDimensBycfId` `cfId`；独立司法 JSON path 仍为 `[待确认]`。

页面首屏部分记录及脱敏规则见 `samples/srm_tencent_judicial_ui_sanitized.json`。样本仅用于证明页面实际返回内容和字段，不替代后端原始响应。

### 7.3 经营风险

切换经营风险后捕获到真实 XHR：

```text
GET /iuap-data-ep/intellid/outside/custom/getDimensBycfId
tabId=2
dataId=788a17caf10f6b629cc58c92f0d0d0f4
cfId=8a865144726507bc0172650ed3160282
companyName=腾讯云计算（北京）有限责任公司
dataCode=10
source=public
enterpriseType=0
funcCode=ent_info_view
```

HTTP `200`，业务 `status=1`，但 `result.datas` 为 `{number:0,size:5,content:null,totalElements:0}`。这与经营风险页实际展示的股权出质列表不同，故样本分成两份：

- `samples/srm_tencent_risk_xhr_sanitized.json`：真实 XHR 空响应；
- `samples/srm_tencent_operating_risk_ui_sanitized.json`：页面真实渲染数据。

股权出质入口维度 ID 为 `8a865144726507bc0172650ed316027f`，页面显示 6 条，全部状态为“无效”，登记日期均为 2022-07-12，字段为登记日期、出质股权标的企业、出质人、质权人、状态、出质股权数额、登记编号。其他经营风险分类按钮均为 disabled。独立股权出质 JSON 响应和分页参数仍为 `[待确认]`。

### 7.4 对清标接入的实际意义

腾讯云样本证明：

1. `getDimensBycfId` 的企业基本信息响应可直接作为结构化企业画像来源；
2. 司法风险与经营风险的页面列表数据可能来自画像页初始化数据或另一条未在标签切换时显式出现的请求，不能只依赖标签切换时捕获到的空响应；
3. 接入层应同时保留“接口原始响应”和“页面可见分类/数量”，当两者不一致时标记待复核，不应把空 `datas` 解释为无风险。

腾讯云本次补充的最小复现仍可使用 `srm_replay.py` 的会话复用方式，例如：

```bash
export SRM_COOKIE='<运行时提供，不写文件>'
python3 srm_replay.py --kind basic --name '腾讯云计算（北京）有限责任公司' \
  --data-id '788a17caf10f6b629cc58c92f0d0d0f4'
python3 srm_replay.py --kind risk --name '腾讯云计算（北京）有限责任公司' \
  --data-id '788a17caf10f6b629cc58c92f0d0d0f4'
```
