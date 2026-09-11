# SRM 登录后企业画像直达 · 验证记录

> 依据《SRM登录后企业画像直达验证交接方案.md》执行，2026-09-09 完成。
> 验证对象：腾讯云计算（北京）有限责任公司。

## 结论

```text
结论：登录后企业画像直达可用。
认证依赖：依赖当前浏览器 SRM 登录态，不是免登录。
主体确认：供应商档案与企业画像名称、统一社会信用代码一致。
未登录对照：CAS 拦截（跳转 euc.yonyoucloud.com/cas/login）。
会话安全：未保存密码、Cookie、Token 或带 loginToken 的 URL。
```

## 验证记录（方案 §7 契约）

```json
{
  "verification_time": "2026-09-09T23:21:13",
  "login_state": "authenticated",
  "query_mode": "browser_session",
  "supplier_name": "腾讯云计算（北京）有限责任公司",
  "supplier_code": "B003477",
  "uscc": "911101085636549482",
  "match_count": 1,
  "deep_link_test": "passed",
  "unauthenticated_control": "passed_requires_authentication",
  "page_title": "用友云-企业画像",
  "visible_sections": ["基本信息", "司法风险", "经营风险"],
  "evidence_ref_without_token": "企业画像内层地址 portrait/detail/10/788a17ca…（白名单参数 name/creditCode/serviceCode/locale/refimestamp/isExcluself/tenantId/enterpriseType，已剔除任何令牌参数）；截图 samples/srm-dl-auth.png、samples/srm-dl-unauth.png",
  "failure_reason": null
}
```

## 执行过程

| 步骤 | 结果 |
|---|---|
| A. 登录（用户在弹出的浏览器窗口手动完成，脚本仅等待观察） | `authenticated` |
| B1-2. 打开供应商档案 | 成功 |
| B3-4. 高级查询面板填写企业名称并查询 | 成功（使用高级查询，非左侧关键词框） |
| B5. 命中条数 | **1（唯一命中）** |
| B6. 档案编码 | B003477（结果行可见；亦见同日验证截图） |
| B7. 打开档案 → 更全面企业信息 → 画像加载 | 成功 |
| B10. 画像主体核对 | 名称一致、统一社会信用代码一致 |
| B8. 内层地址捕获 | 白名单 8 参数构造，剔除令牌；未使用外层容器地址 |
| C1. 已登录新标签直达 | **passed**（`authenticated_deep_link`，标题"用友云-企业画像"，基本信息/司法风险/经营风险均可见） |
| C2. 未登录独立浏览器访问同址 | **CAS 拦截**（跳转 euc.yonyoucloud.com/cas/login；`requires_authentication`） |

## 验收标准对照（方案 §8）

- [x] 未登录访问 SRM 进入正常 CAS 登录流程（历史探测与本次对照一致）；
- [x] 登录成功后可进入供应商档案；
- [x] 查询使用"高级查询"面板；
- [x] 腾讯云查询唯一命中 B003477；
- [x] 点击"更全面企业信息"可加载企业画像；
- [x] 登录后新标签页访问不含 `loginToken` 的内层地址成功；
- [x] 企业名称与统一社会信用代码核对一致；
- [x] 未登录对照被 CAS 拦截；
- [x] 交付物不含密码、Cookie、Token、完整外层会话 URL；
- [x] 未修改原 Skill 代码，未调用 SRM HTTP API。

## 会话安全声明

- 登录由用户在浏览器窗口手动完成，脚本未读取、未记录凭据；
- 内层地址仅由白名单业务参数构造（name/creditCode/serviceCode/locale/
  refimestamp/isExcluself/tenantId/enterpriseType），不含任何令牌；
- 证据截图存于 `samples/srm-dl-auth.png`（已登录直达）与
  `samples/srm-dl-unauth.png`（未登录被拦），不含 Cookie/Token 信息。
