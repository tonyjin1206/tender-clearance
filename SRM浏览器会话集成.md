# 富奥 SRM 浏览器会话集成说明

> 交给 Claude 集成。版本：`srm-browser/0.1.0`。  
> 目标：用户只在运行时提供 SRM 用户名和密码；Skill 通过浏览器完成只读查询；凭据、Cookie、Token 和 `loginToken` 不落盘。

## 1. 结论与边界

本方案使用浏览器会话，不调用 SRM HTTP API，不调用 `requests`、`curl`、`fetch`，
也不复用已经观察到的 `getDimensBycfId` XHR。浏览器能力只做以下事情：

1. 打开 `https://yonbip.fawer.com.cn/`；
2. 在登录页输入用户运行时提供的用户名和密码；
3. 登录后在供应商档案/企业画像页面搜索企业；
4. 读取页面可见的基本信息、司法风险、经营风险；
5. 将脱敏的页面事实和证据引用交给 Skill 下游。

验证码、二次验证、登录墙、权限阻断、页面无法确认主体时停止，不绕过、不猜测、不重试规避。

## 2. 文件与入口

核心实现：

```text
skills/tender-clearance/scripts/tc/srm_browser.py
```

入口类：`SrmBrowserClient`。  
桥接协议：`SrmBrowserDriver`。  
查询模式：`browser_session`。  
现有 `scripts/tc/srm.py` 是 HTTP/token 客户端，不得接入本方案。

## 3. Claude 需要提供的浏览器桥接

Claude 的浏览器工具实现以下接口即可。接口只表达页面动作，不允许内部直接请求 SRM 接口：

```python
class SrmBrowserDriver:
    def open(self, portal_url: str) -> None: ...

    def login(self, username: str, password: str) -> BrowserLoginResult: ...

    def search_subject(self, subject: QuerySubject) -> BrowserSubjectResult: ...

    def read_section(self, section: Literal["basic", "judicial", "operating"]):
        ...

    def close(self) -> None: ...
```

`login()` 返回：

```text
authenticated  登录成功，可看到供应商档案或企业画像
blocked        验证码、登录墙、权限不足、账号被阻断
failed         页面/浏览器错误
manual         需要用户接管或页面无法自动确认
```

`search_subject()` 必须返回：企业名称、统一社会信用代码、主体确认状态和脱敏证据引用。

```text
confirmed      统一社会信用代码或页面唯一主体信息确认一致
candidate      只有名称命中，不能直接归属
unconfirmed    无法确认主体，停止读取风险结果
```

`read_section()` 返回 `BrowserSectionResult`：

```python
BrowserSectionResult(
    section="basic" | "judicial" | "operating",
    fields={...},              # 基本信息字段
    records=[{...}],            # 页面可见记录
    counts={...},               # 页面分类计数
    evidence_ref="...",        # URL/页面标题/快照引用，不含 Cookie/token
    structured=True | False,
    detail="...",
)
```

## 4. 运行时凭据规则

Claude 每次执行前询问：

```text
请输入富奥 SRM 用户名：
请输入富奥 SRM 密码：
```

调用方式：

```python
from datetime import datetime, timezone
from tc.srm_browser import BrowserCredentials, SrmBrowserClient
from tc.sources import QuerySubject

credentials = BrowserCredentials(username, password)
client = SrmBrowserClient(
    driver_factory=lambda: claude_browser_driver,
    credentials=credentials,
)
result = client.query(
    QuerySubject(
        supplier_id="runtime-query",
        name="腾讯云计算（北京）有限责任公司",
        uscc="911101085636549482",
    ),
    datetime.now(timezone.utc),
)
```

`SrmBrowserClient` 会在 `finally` 中清空凭据并关闭浏览器桥接。Claude 不得：

- 把密码写入 Skill、配置、项目文件、Shell 历史、日志、异常、截图或结果；
- 保存 Cookie、Token、`loginToken`、完整 HTML 或未经脱敏的个人联系方式；
- 把凭据拼入 URL、命令行或 HTTP 请求；
- 将密码传给除 SRM 登录页以外的任何站点。

## 5. 已验证的页面导航顺序

以下顺序已在腾讯云真实页面验证。Claude 应按可见文字/可访问名称定位，不要依赖固定
元素序号，也不要从地址栏复制包含会话参数的 URL：

1. 打开供应商档案；
2. 打开“高级查询”；
3. 在“供应商”条件中输入企业名称，点击“查询”；
4. 结果必须恰好命中一个供应商，核对供应商名称和档案编码；
5. 打开该供应商档案，点击“更全面企业信息”；
6. 在“企业画像”页面核对企业名称和统一社会信用代码；
7. 读取“基本信息”及页面已展开的股东/工商股东/对外投资表格；
8. 点击“司法风险”，读取分类数量、禁用状态和可见表格；
9. 点击“经营风险”，读取分类数量、禁用状态和可见表格；
10. 返回结构化结果并保留页面证据引用。

如果供应商查询命中 0 条或多条，状态为 `needs_manual_review`；不得选第一个相似名称。
如果页面内嵌 iframe，允许读取其可见内容，但不得把 iframe 地址中的会话参数复制到结果。

## 6. 三类页面读取标准

### 5.1 基本信息

读取页面实际可见字段，优先保留：

```text
企业名称、统一社会信用代码、法定代表人、注册资本、实缴资本、成立日期、企业状态、
企业类型、注册地址、所属行业、人员规模、参保人数、曾用名、股东/发起人入口计数、
工商股东入口计数、对外投资入口计数、主要人员入口计数、变更记录入口计数
```

主体确认成功后，至少生成一条 `record_kind=registration`。

### 5.2 司法风险

进入企业画像的司法风险页面，读取页面分类名称、页面显示数量和当前可见记录。重点分类：

```text
法律诉讼、历史被执行人、法院公告、开庭公告、失信被执行人、被执行人、股权冻结、
限制高消费、终本案件
```

只显示分类数量而未读取列表时，生成 `judicial_summary`；页面没有结构化结果时返回
`no_result`，不能写成“无司法风险”。

### 5.3 经营风险

读取经营风险页面的分类按钮、启用/禁用状态、页面数量和可见记录。重点分类：

```text
经营异常、行政处罚、严重违法、欠税公告、动产抵押、股权出质、司法拍卖、清算信息、
知识产权出质、税收违法、环保处罚
```

页面分类全部禁用、没有记录或读取失败，都不能解释为“无经营风险”；必须按实际页面状态
写入 `no_result`、`blocked` 或 `needs_manual_review`。

## 7. 结果状态

| 状态 | 含义 |
|---|---|
| `match` | 主体已确认，至少取得一项可追溯页面事实 |
| `no_result` | 页面已打开，但没有可机读/可定位的结构化结果；不等于无风险 |
| `blocked` | 验证码、登录墙、权限不足、限频或访问阻断 |
| `failed` | 浏览器或页面执行错误 |
| `needs_manual_review` | 缺凭据、主体未确认、需要人工接管 |

结果中的 `request_mode` 固定为 `browser_session`。`response_ref` 只存 URL、页面标题、
分类或快照引用；不存请求头、Cookie、Token 或完整 HTML。

## 8. 已验证事实与页面/接口分离

2026-09-09 的腾讯云脱敏样本已确认：

- 基本信息页面可取得企业画像主字段；
- 司法风险页面能显示法律诉讼、历史被执行人、法院公告、开庭公告分类及数量；
- 经营风险页面能显示股权出质分类及页面记录；
- 风险页面可见事实与某条空 XHR 不一致，因此空 XHR 不能当作无风险结论。

本次浏览器运行的脱敏结果见：

```text
samples/srm_tencent_browser_session_validation_sanitized.json
```

本次页面验证结果为 `match`，主体确认为 `confirmed`：

| 页面 | 实际页面结果 |
|---|---|
| 基本信息 | 企业画像主字段可见；统一社会信用代码与供应商档案主体一致 |
| 股东/出资 | 发起人/股东 1 条；工商股东 1 条；对外投资入口显示 37 条 |
| 司法风险 | 法律诉讼 72、历史被执行人 1、法院公告 13、开庭公告 266 |
| 经营风险 | 股权出质 6 条；本页其他列出的经营风险分类为禁用状态 |

已验证的是“登录后浏览器页面工作流”。本次没有重新提交用户名/密码，浏览器已有有效
登录会话；因此账号密码提交端点、密码处理算法和 Cookie 有效期仍然是 `[待确认]`。
这不影响浏览器会话适配器的页面读取验证，但 Claude 首次运行仍应按第 4 节向用户询问
运行时凭据。

本方案只采信浏览器页面事实；如 Claude 同时观察到 Network/XHR，必须单独标记为接口证据，
不能用空接口覆盖页面可见数据。

## 9. 本地验收

```bash
cd skills/tender-clearance
.venv/bin/python -m pytest tests/test_srm_browser.py -q
.venv/bin/python -m pytest tests -q
```

验收重点：

1. 不提供凭据时不打开浏览器；
2. 登录阻断不重试、不继续读数据；
3. 查询完成后凭据被清空；
4. 结果不含密码；
5. 空页面返回 `no_result`，不返回“无风险”；
6. 浏览器模式不依赖 `srm.py` 的 HTTP 实现。
