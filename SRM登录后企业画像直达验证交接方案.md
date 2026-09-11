# SRM 登录后企业画像直达验证方案

> 交给 Claude 执行。目标：验证“先正常登录 SRM，再访问带供应商参数的企业画像页面”是否可行。只做浏览器页面验证，不改原 Skill 代码，不调用 SRM HTTP API。

## 1. 验证结论

已在 Chrome 中验证以下流程可行：

1. 打开富奥 SRM；
2. 用户自行完成正常登录；
3. 进入“供应商档案”；
4. 打开“高级查询”；
5. 在“供应商”条件中输入企业名称并查询；
6. 唯一命中后打开供应商档案；
7. 点击“更全面企业信息”；
8. 在登录态下，新开标签页访问带供应商名称、统一社会信用代码等参数的企业画像地址；
9. 企业画像页面成功显示基本信息、司法/经营风险入口及分类数据。

本次验证对象：腾讯云计算（北京）有限责任公司。供应商档案编码为 `B003477`，统一社会信用代码为 `911101085636549482`。

## 2. 关键判断

该页面不是公共免登录页面。

- 未登录访问 SRM 入口会跳转 CAS 登录页；
- 登录完成后，带供应商参数的企业画像深链可以在同一浏览器登录态下打开；
- 页面地址中可能出现外层 `loginToken`，不得复制、记录、输出或交给下游；
- 可用于验证的内层地址是企业画像路径加业务查询参数，但它仍依赖浏览器已有登录态；
- “直达页面”只表示减少页面点击，不表示绕过认证。

## 3. 地址模板

Claude 可以根据当前页面实际观察到的内层地址构造以下模板。不得从外层容器地址复制 `loginToken` 或 Cookie。

```text
https://c2.yonyoucloud.com/iuap-data-ep/intellid/portrait/detail/10/{portrait_id}
  ?name={url_encoded_supplier_name}
  &creditCode={uscc}
  &serviceCode={observed_service_code}
  &locale=zh_CN
  &refimestamp={current_or_observed_timestamp}
  &isExcluself=true
  &tenantId={observed_tenant_id}
  &enterpriseType=0
```

参数来源必须是当前登录会话中实际观察到的页面或供应商档案，不得猜测 `portrait_id`、租户标识或业务编码。

如果无法安全取得不含会话令牌的内层地址，应退回标准人工路径“供应商档案 → 更全面企业信息”，不得拼接或复制外层 URL。

## 4. Claude 的执行步骤

### A. 登录阶段

1. 打开 `https://yonbip.fawer.com.cn/`；
2. 由用户在 SRM 登录页输入用户名、密码及必要验证码；
3. Claude 只等待并观察登录结果，不代用户读取或保存密码；
4. 只有看到 SRM 首页、供应商档案或其他已授权页面后，才进入下一步。

登录结果：

```text
authenticated  已进入 SRM 且页面可操作
blocked        CAS、验证码、二次验证、权限不足或登录墙阻断
manual         需要用户接管
failed         浏览器或页面错误
```

### B. 页面查询阶段

1. 点击“供应商档案”；
2. 点击“高级查询”展开条件面板；
3. 在“供应商”字段输入企业名称；
4. 点击查询按钮；
5. 结果必须恰好一条；
6. 核对供应商名称、供应商编码及统一社会信用代码；
7. 打开供应商档案，点击“更全面企业信息”；
8. 从当前页面读取不含令牌的企业画像内层地址；
9. 新开标签页，在同一已登录浏览器中访问该地址；
10. 核对企业名称和统一社会信用代码与供应商档案一致。

### C. 直达验证阶段

至少执行两次对照：

| 场景 | 操作 | 预期判定 |
|---|---|---|
| 已登录直达 | 登录后新开标签访问内层企业画像地址 | 能显示目标企业画像，记为 `authenticated_deep_link` |
| 未登录直达 | 在无 SRM 登录态的独立浏览器/隐身会话中访问同一地址 | 跳 CAS、拒绝访问或空页面，记为 `requires_authentication` |

未登录对照不得使用用户密码，也不得尝试绕过 CAS、验证码、权限检查或 Cookie 校验。若没有独立未登录浏览器，只能报告“已验证登录后直达，未登录对照 `[待确认]`”。

## 5. 主体确认与结果状态

只有以下条件同时满足，才可将结果标记为 `match`：

- 登录状态已确认；
- 高级查询唯一命中；
- 供应商名称一致；
- 统一社会信用代码一致；
- 企业画像页面成功加载；
- 页面证据可追溯，且不含密码、Cookie、Token 或完整会话 URL。

状态定义：

```text
match                  登录后直达成功，主体已确认
requires_authentication 未登录直达被 CAS/权限拦截
needs_manual_review    多条命中、主体不一致、地址参数无法确认
blocked                验证码、权限、限频或访问控制阻断
failed                 浏览器或页面错误
```

## 6. 安全边界

Claude 不得：

- 从历史对话、文件、日志或浏览器密码库提取用户名/密码；
- 代用户填写或提交密码，除非产品明确提供安全的用户接管流程；
- 保存密码、Cookie、`loginToken`、完整外层 URL 或未经脱敏的页面快照；
- 把会话参数写入 Markdown、JSON、日志、命令行或报告；
- 使用 Network/XHR、反向接口或旧 HTTP 客户端代替页面登录和查询；
- 把登录后直达误写为“免登录”；
- 在多条相似供应商中自行选择第一条。

## 7. 交付给上游的验证记录

Claude 输出一份脱敏 Markdown 或 JSON，至少包含：

```text
verification_time
login_state
query_mode = browser_session
supplier_name
supplier_code
uscc_masked_or_allowed
match_count
deep_link_test = passed | blocked | needs_manual_review
unauthenticated_control = passed | blocked | not_tested
page_title
visible_sections
evidence_ref_without_token
failure_reason
```

推荐结论格式：

```text
结论：登录后企业画像直达可用。
认证依赖：依赖当前浏览器 SRM 登录态，不是免登录。
主体确认：供应商档案与企业画像名称、统一社会信用代码一致。
未登录对照：CAS 拦截 / 未测试。[二选一，按实际验证填写]
会话安全：未保存密码、Cookie、Token 或带 loginToken 的 URL。
```

## 8. 验收标准

- [ ] 未登录访问 SRM 会进入正常 CAS 登录流程；
- [ ] 登录成功后可进入供应商档案；
- [ ] 查询使用“高级查询”，不是左侧普通关键词框；
- [ ] 腾讯云查询唯一命中 `B003477`；
- [ ] 点击“更全面企业信息”可加载企业画像；
- [ ] 登录后新标签页访问不含 `loginToken` 的内层地址成功；
- [ ] 企业名称与统一社会信用代码核对一致；
- [ ] 未登录对照被 CAS/权限拦截，或明确标记为未测试；
- [ ] 交付物不含密码、Cookie、Token、完整外层会话 URL；
- [ ] 不修改原 Skill 代码，不调用 SRM HTTP API。
