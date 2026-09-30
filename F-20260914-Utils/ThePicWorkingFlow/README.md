# 代码架构工作台：n8n + Docling + 大模型 API

把代码和文档转为结构化架构设计，再生成符合本项目风格的可编辑 draw.io。包含浏览器工作台、可调度 HTTP API、可导入的 n8n 工作流、Docling 文档接入，以及独立的 XML 风格检查器。

分析模型与设计模型可以分别使用不同厂商的 **OpenAI 兼容 Chat Completions API**。模型参数从本地 `.env` 读取；历史 smoke 测试中的模型响应使用模拟数据，实际运行使用用户配置的厂商接口。

## 文件入口

| 文件 | 用途 |
| --- | --- |
| `TheWorkingflow.drawio` | 本工作流的架构总图，以及 n8n、架构服务、Docling、大模型 API 四个模块的独立页 |
| `n8n/workflow.json` | 导入 n8n 的实际编排流程 |
| `compose.yaml` | 架构 API、n8n、Docling 三服务部署 |
| `.env.example` | 两阶段模型、服务认证和路径配置 |
| `examples/request.json` | 分析本项目 PLDA 代码的任务请求 |
| `schemas/architecture.schema.json` | 模型设计输出的结构约束 |
| `examples/workflow-quality.json` | 工作流架构图的检查报告 |
| `VALIDATION.md` | 实际验证结果和验证范围 |

## 处理流程

1. 工作台把任务提交给 n8n Webhook；创建任务后立即返回 `job_id`。
2. API 读取允许目录下的源代码，保存内容散列、路径及行号；Python 另外提取 AST 符号。PDF、Office、Markdown 和图片等文档交给 Docling 转为 Markdown。
3. 分析模型逐块提取职责、关系和来源证据；设计模型根据分析结果生成模块、子区域和有向连接的 JSON。
4. 渲染器从 `../TheStructure.drawio` 提取项目配色和区域风格，确定性生成原生 mxGraph 图形、总图、独立模块页及跳转链接。
5. 检查器重新读取生成的 XML，验证页面覆盖、模块归属、父子包含、节点重叠、边端点和颜色等约束。
6. 检查失败最多修复两轮；全部通过后才发布 draw.io 和报告。文件写入任务自己的输出目录，通过 API 下载。

模型输出 JSON，而不是直接拼接任意 draw.io XML。`observed` 节点必须有存在的源码路径和有效行号；`proposed` 与 `external` 用于设计建议及外部系统。文档证据使用 `doc:文件路径`，行号指 Docling 转换后的 Markdown。

## 本项目的模块与风格规则

设计要求固定为遵循 `C:\Users\Lenovo\Desktop\OGv01\F-20260914-Utils\TheStructure.drawio` 的架构表达思路：按职责组织层次，用区域包含体现模块关系，用带语义的彩色连线表达数据流，结合总图与模块独立页展示全局和细节。模板是设计参考，不是待分析项目的业务内容或实现证据。

一级模块定义：**除背景板外，不被其他区域包含的最大区域**。模块名称、数量、边界、组件与连接由实际代码和文档决定。工作台的“一级模块”默认留空，也可手动指定名称；不再预设模板中的三个模块或 PLDA 归属。

新任务默认只生成系统总览和各一级模块独立页，`preserve_page_ids` 默认为空，不复制模板中的原始页面或节点。API 仍支持显式指定 `preserve_page_ids` 以保留选定页面。内部子区域不新增一级模块页。

这里的 `TheWorkingflow.drawio` 描述的是工作流自身的四个一级模块，总计五页，不限制其他项目的模块数量。

| 连线类别 | 颜色 | 意义 |
| --- | --- | --- |
| `raw` | 蓝色 | 原始数据 |
| `standard` | 黑色 | 标准数据 |
| `analysis` | 红色 | 分析信息 |
| `dispatch` | 深绿色 | 执行任务 |
| `result` | 洋红色 | 返回结果 |
| `control` | 紫色虚线 | 控制或异常 |

总图必须包含全部一级模块和组件；每个一级模块必须独立成页。跨模块连接在独立页中显示为外部接口，并链接到对应模块页。检查器拒绝缺页、孤立组件、错误归属、越界、兄弟区域重叠、错误色彩或箭头等情况。代码语义是否解释正确仍需结合 `analysis.json`、`architecture.json` 和来源证据判断。

## 部署与首次配置

### Windows 本地启动（当前电脑）

本机依赖已安装，直接在本目录用 PowerShell 7.4 或以上启动：

```powershell
.\scripts\start-local.ps1
```

也可以使用项目根目录的一键脚本启动并打开工作台：

```powershell
.\start-project.ps1
```

不自动打开浏览器时使用 `-NoBrowser` 参数：

```powershell
.\start-project.ps1 -NoBrowser
```

脚本通过 Windows WMI 创建独立的隐藏后台进程，启动 API、Docling、n8n，自动导入本地 Header Auth 凭据及工作流并发布。启动命令会先返回，查看 `data/logs/startup.log`，出现 `Architecture Flow is ready` 后即可使用。后台进程独立于启动它的终端或任务会话。工作台地址是 `http://127.0.0.1:8080`，访问密钥使用 `.env` 的 `APP_API_KEY`；n8n 编排界面是 `http://127.0.0.1:5678`。首次进入 n8n 管理界面可设置自己的管理员账号。

本地运行数据保存在 `data/n8n` 与 `data/jobs`，日志在 `data/logs`，与 `.n8n-test` 中的旧 smoke 数据隔离。脚本会复用正在运行的受管理进程；修改 `.env` 后，先执行 `scripts/stop-local.ps1`，再重新启动。关闭时使用：

```powershell
.\scripts\stop-local.ps1
```

### Docker Compose 部署

需要 Docker Engine / Docker Desktop（Linux containers）与 Docker Compose。此电脑已安装本地测试所需的 Python、n8n、Docling；Docker Compose 部署尚未在此电脑实际执行。

在 PowerShell 7 中进入本目录：

```powershell
cd C:\Users\Lenovo\Desktop\OGv01\F-20260914-Utils\ThePicWorkingFlow
.\scripts\init-env.ps1
```

脚本创建 `.env`，自动生成工作台、Docling 和 n8n 加密所需的随机密钥。已存在的 `.env` 会保留。然后在本地编辑 `.env`：

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=填写厂商密钥
ANALYSIS_MODEL=deepseek-flash
DESIGN_MODEL=deepseek-flash
```

`LLM_BASE_URL` 是 API 基础地址，程序会追加 `/chat/completions`；厂商要求 `/v1` 时须包含它。两阶段分别覆盖 `ANALYSIS_BASE_URL` / `ANALYSIS_API_KEY` 与 `DESIGN_BASE_URL` / `DESIGN_API_KEY` 即可跨厂商。兼容接口不支持 `response_format: json_object` 时设 `LLM_JSON_MODE=false`，程序仍会严格解析和检查返回 JSON。

`.env` 已被 Git 忽略，密钥只在本地填写。`NO_PROXY` 保证本机及 Compose 内部服务地址不经过系统代理。

```powershell
docker compose up -d --build
docker compose ps
```

首次启动 Docling 会准备模型，耗时取决于下载速度；启动完成前文档转换不可用。查看 `docker compose logs -f docling`。

接着完成 n8n 配置：

1. 打开 `http://127.0.0.1:5678`，完成本地账号初始化。
2. 新建 **Header Auth** 凭据，名称建议 `Architecture Flow API`，Header Name 填 `X-API-Key`，Value 填本地 `.env` 的 `APP_API_KEY`。
3. 导入 `n8n/workflow.json`。在 Webhook 和所有 HTTP Request 节点中选择刚创建的凭据；文件中的 `CONFIGURE_HEADER_AUTH` 是待替换的占位 ID。
4. 保存并发布工作流。生产 Webhook 路径是 `/webhook/drawio-architecture`，不是测试路径 `/webhook-test/...`。
5. 打开 `http://127.0.0.1:8080`，确认工作台访问密钥和代码目录。“一级模块”留空即可根据代码自动划分，文档路径可按需填写，然后提交任务。

Compose 已把 API 地址设为 `architecture-api:8080`，把项目根目录只读挂载为 `/workspace`。输出存于 `architecture_data` 卷，可通过工作台或 API 下载。项目模板是输入参考文件，生成结果以新的任务产物交付。

## 作为可调度模块调用

所有 `/v1` 接口都使用 `X-API-Key: APP_API_KEY`。`/health` 与工作台页面可直接访问。任何支持 HTTP 的调度器都可以使用下面的接口。

| 接口 | 功能 |
| --- | --- |
| `POST /v1/launch` | 经 n8n 编排运行，返回 202 和任务 ID |
| `POST /v1/jobs` | 只创建持久化任务，供调度器逐阶段调用 |
| `POST /v1/jobs?run=true` | API 内直接执行，供本地开发验证 |
| `GET /v1/jobs/{job_id}` | 状态、错误、模型调用记录、下载地址 |
| `POST /v1/jobs/{job_id}/stages/{stage}` | 执行指定阶段 |
| `POST /v1/jobs/{job_id}/cancel` | 取消任务，阻止后续处理和发布 |
| `GET /v1/jobs/{job_id}/artifacts/{name}` | 下载已生成的允许类型产物 |
| `GET /v1/schema` | 获取设计 JSON Schema |

阶段名称依次为 `ingest`、`analyze`、`design`、`render`、`check`，然后 `publish`，或 `repair → render → check`，预算耗尽后 `reject`。模型 JSON/schema/证据错误在设计阶段内最多尝试三次；风格修复预算单独计算。

PowerShell 示例（密钥在当前终端会话中设置，不写入请求文件）：

```powershell
$flowHeaders = @{ 'X-API-Key' = $env:APP_API_KEY }
$flowRequest = Get-Content -LiteralPath .\examples\request.json -Raw -Encoding UTF8
$flowJob = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/launch -Headers $flowHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($flowRequest))
Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/jobs/$($flowJob.job_id)" -Headers $flowHeaders
```

`architecture.drawio` 只在任务成功后可下载。其他报告包括 `analysis.json`、`architecture.json`、`style-profile.json` 和 `quality.json`。每个任务还保留模板快照、代码快照和中间模型结果，便于追溯。

## 异常、容量与运行边界

- 上游 429 或 5xx 最多调用三次并退避；不会把厂商响应正文或认证头放入公开任务状态。
- 已完成阶段按任务修订缓存，n8n 重试不会重复执行成功的分析调用。创建与修复节点关闭自动重试，避免响应丢失后重复创建或重复修订。
- 取消会阻止后续结果发布；已经提交到厂商的远程计算可能继续完成。进程中断会把未完成的运行阶段标为失败，保留中间结果。
- 发布前重新检查实际 XML，避免使用过期的检查通过标志。
- 默认最多四个同时执行的 API 阶段，模型 HTTP 调用最多两个并发。源文件最多 300 个，代码与转换后文档合计 240,000 字符，单份文档最多 20 MiB，最多 8 个模块、80 个组件、120 条连接和五层内部嵌套。超限明确报错，需缩小分析目录。
- 当前持久化使用本地文件和单进程锁，API 必须运行 **一个 worker**。多主机队列和共享数据库是后续扩展方向；此实现没有宣称跨主机容错。

## 本地开发和验证

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[test]'
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\build_assets.py
.venv\Scripts\python.exe -m uvicorn architecture_flow.api:app --host 127.0.0.1 --port 8080 --workers 1
```

本地启动时保持当前目录为 `ThePicWorkingFlow`，以使 `.env` 相对路径正确解析。Docling 可单独安装 `docling-serve==1.32.0` 并通过 `python -m docling_serve run --host 127.0.0.1 --port 5001 --no-enable-ui` 启动。n8n 本地安装需 Node.js 24，使用 `n8n@2.38.7`；工作流所有 HTTP 节点的地址要改为 `127.0.0.1:8080`。

`scripts/smoke_integration.py` 用于隔离的集成验证：`prepare` 生成仅供测试的 n8n 导入文件和随机认证，`serve` 在 18080 启动测试 API 与假模型，`docling` 验证 15001 的真实文档转换，`verify` 通过 15678 的真实 n8n Webhook 执行流程。测试从不把代码发送到真实厂商。`.n8n-test` 与 `data` 均为本地测试/运行目录，不随源码提交。

## 接口依据

- [n8n HTTP Request 节点](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)
- [n8n 导入和导出](https://docs.n8n.io/build/manage-workflows/export-and-import/)
- [Docling Serve 官方仓库与 API 用法](https://github.com/docling-project/docling-serve)
- [Docling API Server](https://docling-project.github.io/docling/usage/api_server/)
