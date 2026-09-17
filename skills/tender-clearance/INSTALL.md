# tender-clearance 安装分层

## Core（Windows PowerShell）

Core 只负责本地文件盘点、一次性解析、OCR 契约导入、确定性规则和报告。
它不安装 OCR 模型、浏览器或外部查询客户端。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-core.txt
```

首次安装时，如由本 Skill 包装安装命令，请使用以下形式；过程事件写入 stderr，供宿主 Agent
显示为可折叠模型信息，每 10 秒一条，不作为普通用户答复：

```powershell
py scripts\install_environment.py -- py -3.12 -m pip install -r requirements-core.txt
```

## 按需扩展

```powershell
.\.venv\Scripts\python.exe -m pip install ".[review]"     # DOCX
.\.venv\Scripts\python.exe -m pip install ".[workpaper]"  # DOCX + Excel
.\.venv\Scripts\python.exe -m pip install ".[live]"       # 显式外部刷新
.\.venv\Scripts\python.exe -m pip install ".[srm]" # SRM 浏览器会话适配器
```

如用户提供空白招标文件 Word 模板，模板输入是可选的，但应明确告知用户：提供模板可大幅提高
指标定位和 OCR 识别准确度。可在 `project.yaml` 指定项目根相对路径：

```yaml
tender_template_path: procurement/空白招标文件模板.docx
```

预检会在发现模板时检查 `python-docx`；没有模板则不要求该依赖，继续使用通用 OCR 兜底并保留
人工复核提示。多个可能模板不会自动猜选。

SRM人工登录模式（推荐用于需要验证码、二次验证或不希望脚本接触凭据的场景）：

```bash
SRM_BROWSER_MANUAL_LOGIN=1 python scripts/query_sources.py <项目目录> --refresh
```

该模式会打开独立的可见浏览器窗口，不要求、不读取、不填充账号密码；用户需在窗口内完成登录和验证码。程序会提示等待登录、登录成功、浏览器关闭、有限重开和超时人工复核。可用 `SRM_MANUAL_LOGIN_TIMEOUT_SECONDS` 调整人工登录等待时间。

生产 OCR 不通过本包安装。宿主 Agent 需声明并提供 `ocr.capabilities.v1`，然后把
`ocr-result.v1` 结果交给 `scripts/import_ocr_results.py`；默认要求本地处理。质量优先
的参考实现使用 PaddleOCR `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`，并返回块坐标；
如果客户已有本地 Agent OCR，可复用其能力，但必须经过能力声明和真实扫描页验收。

宿主 Agent 不应把 OCR 原文或置信度打印到普通消息；结果写入项目中间产物后，由 Core
生成 `process-workpaper.json` 和不含 OCR 块/置信度数值的 `report-input.json`。正式报告
前每个 OCR 任务必须有终态，缺失任务会阻断正式报告。

## 安装检查

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py <项目目录> --profile report
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe <quick_validate.py> .
```

实际安装前先运行 `preflight.py`，把缺失依赖和技术标跳过策略一次性提交确认；只安装确认
的项目。预检后流水线不在阶段中途请求输入，也不在报告渲染时补装依赖。生产 OCR、模型和
浏览器仍由独立 Provider/适配器按授权提供，不随 Core 安装。
