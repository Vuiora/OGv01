# 验证记录

日期：2026-09-15。环境：Windows、Python 3.14.7、Node.js 24.19.0。

## 后续：真实配置启动验证

用户填写 `.env` 后，通过 `scripts/start-local.ps1` 启动正式本地实例，并自动导入认证、工作流及发布版本：

- API `127.0.0.1:8080`、Docling `127.0.0.1:5001`、n8n `127.0.0.1:5678` 的健康检查均返回 200。
- 真实厂商模型列表确认 `deepseek-flash` 可用；实际 Chat Completions 请求返回 200，成功解析 JSON。此连通性测试共使用 89 tokens。
- 配置认证后的 Docling 再次成功转换 `DESIGN.md`，返回 6,525 字符 Markdown；API 的认证 Schema 接口返回 200。
- 工作台已在浏览器打开，本地服务持续后台运行。README 中重复填写的明文密钥已恢复为占位符，实际配置保留在 `.env`。

以下为此前集成 smoke 的记录。完整架构生成的 smoke 使用模拟模型；后续启动验证使用真实模型做最小连通性测试，没有自动启动完整项目的架构分析任务。

## 已完成

| 检查 | 结果 |
| --- | --- |
| 自动化测试 | **15 passed**；覆盖完整 API 流程、模型设计纠错、阶段重试缓存、访问认证、路径与数据预算、第二页保留、缺页/错色/孤立节点/越界/坏链接/重叠、取消、发布前重新检查、修复成功及预算耗尽、长名称嵌套 |
| Python 依赖检查 | `pip check`：No broken requirements found |
| 项目代码接入 | PLDA 目录读取 22 个源文件、136,373 字符，0 个读取遗漏，符合默认预算 |
| 真实 Docling | 安装并启动 `docling-serve 1.32.0`；通过 `/v1/convert/source` 转换项目 `DESIGN.md`，获得 6,525 字符 Markdown |
| 真实 n8n 导入 | `n8n 2.38.7 import:workflow` 成功；修复导入时缺少工作流 ID 的问题 |
| 真实 n8n 执行 | 独立测试实例发布工作流，真实 Webhook → HTTP API → Docling → 模型兼容接口 → 渲染 → 检查 → 发布全链路成功 |
| 产物下载 | draw.io、架构 JSON、检查报告、分析报告、风格配置五种文件均通过认证接口下载成功 |
| 原生图形导出 | 用已安装的 draw.io 桌面程序导出五页 PNG，人工查看总图及各模块布局 |
| 工作台浏览器检查 | 页面布局、表单可访问名称、编排方式切换、无效密钥错误提示和提交按钮恢复正常 |

自动化测试产生一项 Starlette 对 AnyIO 别名的上游弃用提示；测试无失败。

## 集成 smoke 的准确范围

最终结果保存于 `examples/integration-smoke-report.json`，相应文件为 `examples/integration-smoke.drawio`。测试实际经过发布的 n8n Webhook，包含立即响应任务 ID 的节点；没有跳过编排直接调用 Python 管线。

最终一次热启动运行约 **0.206 秒**返回任务 ID，约 **3.473 秒**完成流程和下载检查；初次成功运行约 8 秒。这里只用于验证连接和数据格式，不代表真实大模型性能。

**n8n 和 Docling 是真实服务，大模型是本地模拟的 OpenAI 兼容 HTTP 接口。** 模拟模型明确返回固定的 proposed 架构，不代表已经由真实大模型分析本项目。测试服务地址分别为 15678、15001、18080，使用独立的 `.n8n-test` 状态和随机测试认证。

真实 Docling 本次验证输入为 Markdown；PDF、图片 OCR 以及 Office 输入的质量尚未逐格式实测。服务首次启动已下载所需模型，不能把首次模型下载时间计入上述热运行耗时。

## 仍需实际环境验证

- 真实厂商接口已通过上述最小连通性与 JSON 格式检查；完整项目的真实模型输出质量、费用及上下文限制仍需实际任务验证。
- `compose.yaml` 已提供，宿主机没有 Docker，因此未执行容器构建和三容器部署。已验证的是本机安装版 n8n + Docling + Python API。
- 当前检查器保证规定的结构和样式约束，不证明模型对源码职责与数据流的理解一定正确。

## 复验方式

日常代码验证在 `ThePicWorkingFlow` 目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe scripts\build_assets.py
```

集成验证使用 `scripts/smoke_integration.py`。先 `prepare`，再在不同终端启动 15001 的 Docling、`serve` 模式的测试 API 和独立 n8n。n8n 的准备命令在 `.n8n-test` 目录执行：

```powershell
$env:N8N_USER_FOLDER = Join-Path $PWD 'state'
$env:N8N_DIAGNOSTICS_ENABLED = 'false'
node node_modules\n8n\bin\n8n import:credentials --input=smoke-credentials.json
node node_modules\n8n\bin\n8n import:workflow --input=smoke-workflow.json
node node_modules\n8n\bin\n8n publish:workflow --id=ArchitectureFlowSmoke
$env:N8N_PORT = '15678'
$env:N8N_LISTEN_ADDRESS = '127.0.0.1'
$env:N8N_SECURE_COOKIE = 'false'
$env:NO_PROXY = 'localhost,127.0.0.1'
node node_modules\n8n\bin\n8n start
```

等 `/healthz/readiness` 返回 200 后，在工作流目录执行 `scripts/smoke_integration.py verify`。首次 n8n 启动耗时数分钟；本次启动中的可选 Python runner 缺失和 MCP 目录请求超时未阻止此流程，它没有使用 Python Code 节点或 MCP 目录。

本地 `.env` 已配置，正式本地服务已启动；后续可通过工作台提交真实架构分析任务。
