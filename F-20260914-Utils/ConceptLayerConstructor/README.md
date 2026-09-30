# Concept Layer Constructor

CLC 使用 **Docling + 大模型 API + n8n**，把资料编纂为可循序阅读的知识层级文档。每个知识点保存为一个独立 Markdown 文件，另外生成总目录、关系图数据，以及合并的 Markdown、Word 和 PDF。

以“调度策略与 CFS 关系”为例，`SCHED_NORMAL` 标记为策略，`CFS` 标记为实现算法，用 `CFS → implements → SCHED_NORMAL` 表达实现关系。目录负责组织阅读顺序，关系负责解释概念之间的联系，避免把实现算法误当成策略的同义词。

## 功能与边界

* 输入：UTF-8 `.md` / `.txt`、`.pdf`、Word `.docx`。旧 `.doc` 需先另存为 `.docx`。
* PDF 与 DOCX 由 Docling 解析；已有 Markdown 直接读取，避免不必要的格式转换。PDF 首次解析可能下载模型，扫描件识别质量取决于 OCR 和原始清晰度。
* 分块提取知识点，再基于摘要与引用统一规划目录。每个知识点保存类型、解释、适用范围、易混点、示例和逐字原文引用。
* 校验 JSON 结构、引用是否在对应原文分块中、节点是否遗漏、父节点是否存在、是否有环、最大深度。**逐字引用存在不代表模型解释必然正确，内容仍需复核。**
* 相同名称、类型和适用范围的知识点合并；不同范围保留。同义词消歧目前由模型识别辅助，程序不做激进的语义合并。
* 输出：独立 `nodes/*.md`、`index.md`、`knowledge.md`、`knowledge.docx`、`knowledge.pdf`、`graph.json`、规范化来源及分块索引。无论选择什么格式，总会保留原子 Markdown。
* SQLite 持久化队列，API 与 Worker 分离。Worker 中断后，下一次队列扫描将过期任务标记失败；不会自动重复收费请求，需要重新提交。HTTP 429、5xx 和非法结构响应最多重试两次。
* 当前按任务建立知识库，不支持跨任务自动合库、增量同步或多人编辑。Word/PDF 是合并阅读版，MD 是可编辑的原子知识载体；修改 MD 后暂不自动同步到 Word/PDF。

## 目录

```text
clc/
  api.py          上传、状态查询、鉴权、下载
  parse.py        Docling 解析、分块、引文核对
  llm.py          OpenAI 兼容 chat/completions 客户端
  models.py       知识点、目录、关系的数据契约
  build.py        提取、去重、规划与校验
  export.py       Markdown、DOCX、PDF 导出
  store.py        SQLite 任务队列
  worker.py       任务领取、租约与进程超时
  run_job.py      单任务处理进程
n8n/
  text-to-knowledge.json
  file-to-knowledge.json
scripts/
  init_env.py     生成本地配置与随机服务密钥
  start.ps1      后台启动 Windows API 与 Worker
  stop.ps1       根据进程身份停止本项目服务
  build_workflows.py
examples/linux-scheduling.md
tests/test_pipeline.py
compose.yaml
Dockerfile
```

## 配置大模型

```powershell
python scripts/init_env.py
```

该命令只在 `.env` 不存在时创建文件，生成 `CLC_API_KEY` 和 `N8N_ENCRYPTION_KEY`，不打印密钥、不覆盖已有配置。编辑 `.env`：

```dotenv
CLC_LLM_BASE_URL=https://你的供应商地址/v1
CLC_LLM_API_KEY=你的密钥
CLC_LLM_MODEL=该供应商支持的模型名称
CLC_LLM_JSON_MODE=true
```

接口会调用 `${CLC_LLM_BASE_URL}/chat/completions`。如果供应商不接受 `response_format`，将 `CLC_LLM_JSON_MODE=false`；模型仍必须输出 JSON。无须安装特定厂商 SDK。本地无鉴权兼容服务可将大模型密钥留空。选择能够稳定输出结构化 JSON、输出长度足够的模型。

**输入资料的文字及引文会发送到所配置的大模型 API。** 原始文件保存在本地任务目录；不自动搜索外部资料。每个任务约调用“分块数 + 1”次 API，重试会增加调用次数。未配置模型时会明确失败，不会用模拟结果替代真实生成。

## Windows 本地运行

### 交互工作台

启动服务后打开 [知识层级工作台](http://127.0.0.1:8000/)。本机访问时自动完成认证，无需填写 `CLC_API_KEY`。上传文件或粘贴文字，选择导出格式并点击「生成知识层级」。页面自动查询处理进度；完成后可展开知识目录，查看解释、原文依据和关系，并下载 ZIP、Word、PDF 或 Markdown。

本机工作台通过 HttpOnly 会话 Cookie 自动认证，服务密钥不会写入网页。任务 ID 保留在页面地址的 `#` 后，收藏该地址或刷新页面即可自动查询。默认仅对 localhost、127.0.0.1 和 ::1 启用；可设置 `CLC_WEB_AUTO_AUTH=false` 恢复手动输入。其他地址及独立 API 客户端仍需服务密钥。页面显示 API 连接状态，不代表 Worker 已启动。模型参数仍在 `.env` 配置，修改后需要重启 API 与 Worker。原有 [API 文档](http://127.0.0.1:8000/docs) 继续可用。

### 安装与启动

使用 Python 3.11–3.13，建议 3.12；本项目不支持 Python 3.14。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[docling,dev]"
.\.venv\Scripts\python.exe scripts/init_env.py
# 编辑 .env 后启动
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。查看运行日志 `data/runtime/`。修改 `.env` 后停止再启动，以让 API 和 Worker 同时加载新配置。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

也可以在两个终端分别以前台方式运行，均从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn clc.api:create_app --factory --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m clc.worker
```

只试用 Markdown 输入可安装 `.[dev]`，省去 Docling 与机器学习依赖。服务默认只监听本机；`CLC_API_KEY` 非空时所有任务接口均需 `X-API-Key` 请求头，`/health` 和 `/docs` 无需该请求头。健康接口报告 API 与依赖状态，不代表 Worker 或大模型可用。

## Docker Compose 与 n8n

需要先安装并启动 Docker Desktop 或 Docker Engine / Compose。首次镜像构建包含 CPU PyTorch 和 Docling，耗时和磁盘占用会明显大于普通 Web 服务。

```powershell
python scripts/init_env.py
# 填写 .env 的模型参数。如果之前启动过本地服务，先释放 8000 端口：
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
docker compose up --build -d
```

`scripts/start.ps1` 用于 Windows 本地进程模式，`docker compose up` 用于容器模式；两种方式使用同一端口，选择其中一种即可。

### 构建故障排查

如果任务出现 `IncompleteSnapshotError`，表示 Docling 解析模型的本地缓存不完整，且此次无法从 Hugging Face 补齐。首次解析 PDF 需要下载布局、表格等模型；Worker 必须能访问 `huggingface.co` 及其文件下载服务。程序会有限重试并保留已有下载进度，仍失败时请检查 Docker 的网络或代理，再重新提交。不要删除 `docling_models` 卷，否则已下载的模型也会丢失。模型下载及 CPU 解析可能需要数分钟。

如果日志出现 `THESE PACKAGES DO NOT MATCH THE HASHES`，先查看对应下载是否完成。例如 `torch` 显示 `43.8/196.3 MB` 即停止，说明收到了不完整文件，而不是应用代码编译出错。不要修改预期哈希或关闭校验。

Dockerfile 已将 pip 从基础镜像中的旧版本升级到 **26.2.1**，设置连接重试和断点续传次数，并把 PyTorch、Docling 依赖及应用安装分成独立层。后续步骤失败时，已成功的 PyTorch 层可以复用。构建时保持 TLS 和哈希校验开启；pip 的下载恢复修复见 [官方变更记录](https://pip.pypa.io/en/stable/news/)。

需要单独构建和保存完整日志时，在项目根目录执行：

```powershell
docker compose --progress plain build api 2>&1 | Tee-Object -FilePath docker-build.log
# 仅在上一条构建成功后启动
docker compose up -d --no-build
docker compose ps
```

Compose 已明确设置 API 从本地构建，Worker 只使用本地镜像；`clc:local` 不需要从 Docker Hub 下载。如果更改前看到 `pull access denied for clc`，不是要求你创建远端仓库或登录 Docker。`docker ai` 是可选辅助功能，本项目启动不依赖它。

若仍有下载失败，请保留 `docker-build.log` 中第一个错误及对应包的下载记录，检查 Docker Desktop 使用的代理/网络链路。上述重试不能修复持续返回错误内容的代理。重新构建时通常无需 `--no-cache`，也无需删除数据卷。

* CLC API：[http://localhost:8000/docs](http://localhost:8000/docs)
* n8n：[http://localhost:5678](http://localhost:5678)，首次访问创建管理员账户。
* 在 n8n 导入 `n8n/text-to-knowledge.json` 和/或 `n8n/file-to-knowledge.json`。
* 新建 **Header Auth** 凭据：Name 为 `X-API-Key`，Value 为 `.env` 中 `CLC_API_KEY`。在 `Submit`、`Status`、`Download` 三个 HTTP 节点选择它。
* `Receive` Webhook 同样需要选择 Header Auth 凭据；可以使用独立入口密钥。调用 Webhook 时带上相应请求头。
* 保存并发布工作流（旧版界面为激活）。文本入口 `/webhook/clc-text`；文件入口 `/webhook/clc-file`。
* 当前文件工作流接收一个 multipart 字段 `files`。API 本身支持同时上传多个文件，扩展 n8n 批量上传时可增加表单文件参数。

工作流路径为 `Webhook → 提交任务 → 返回 202 → 等待 10 秒 → 查询状态`；运行中继续等待，成功下载 ZIP 至 n8n 执行记录，失败进入明确的错误节点。Webhook 返回任务 ID 和相对 API URL，**不会在同一次响应里等待整本资料编纂完成**。工作流最长运行两小时，避免 Worker 未启动时无限轮询；生成任务本身默认一小时超时。

默认服务地址 `http://api:8000` 用于 Compose 网络。若 n8n 位于 Docker 而 CLC 在宿主机，请把三个 HTTP 节点改为 `http://host.docker.internal:8000`，并配置宿主机监听与访问规则。普通本机 n8n 可使用 `http://127.0.0.1:8000`。

`N8N_IMAGE` 默认使用官方 `stable` 通道；部署验收后建议在 `.env` 固定实际版本或镜像摘要。示例只映射回环地址；对外部署需另行配置 HTTPS、反向代理和访问控制。不要把示例中的本地 HTTP Cookie 配置直接搬到公网。

## API 示例

以下命令从项目根目录运行；`$env:CLC_API_KEY` 请设置为 `.env` 的服务密钥。

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/jobs `
  -H "X-API-Key: $env:CLC_API_KEY" `
  -F "files=@examples/linux-scheduling.md" `
  -F "title=调度策略与 CFS" -F "formats=md,docx,pdf"
```

文本接口使用 JSON，适合聊天摘录、笔记和 n8n 的文本节点：

```http
POST /v1/jobs/text
X-API-Key: <服务密钥>
Content-Type: application/json

{"title":"调度策略与 CFS","text":"这里放完整资料……","formats":["md","docx","pdf"]}
```

|接口|用途|
|-|-|
|`POST /v1/jobs`|multipart：一个或多个 `files`，可选 `title` / `formats`|
|`POST /v1/jobs/text`|JSON 文本任务|
|`GET /v1/jobs/{id}`|`queued` / `running` / `succeeded` / `failed`、阶段、错误与下载列表|
|`GET /v1/jobs/{id}/graph`|层级与带类型的知识关系|
|`GET /v1/jobs/{id}/bundle`|成功后下载完整 ZIP|
|`GET /v1/jobs/{id}/files/{path}`|成功后下载单个产物|

响应中的 URL 为相对于 CLC API 的路径。下载请求同样需要服务密钥。未成功任务的产物返回 409，防止读取未完成文档。

## 产物与依据

```text
index.md                 层级目录及知识点链接
knowledge.md             合并阅读版
knowledge.docx           Word 阅读版
knowledge.pdf            PDF 阅读版
graph.json               完整机器可读图
nodes/k-<hash>.md         一个知识点一个文件
nodes/g-<group>.md        一个阅读分类一个文件，标记 synthetic
sources/s001.md          Docling 标准化的输入资料
sources/manifest.json    来源名称、SHA-256、字数等
sources/chunks.json      原文块及在标准化 Markdown 中的字符偏移
```

知识点 ID 由标题、类型、适用范围构造，文件名不直接使用模型给出的路径。引用定位以规范化 Markdown 的分块和字符范围为准，**不是 PDF 原始页码定位**。`parent_id` 表示阅读树；`relations` 独立保存 `is_a`、`part_of`、`implements`、`handled_by`、`depends_on`、`contrasts_with`、`uses`。

Word/PDF 保留正文、标题、列表、代码与来源标识；Markdown 保留可点击文件导航。该版本不复刻源 PDF/Word 的版式，不嵌入原始图片或复杂表格，PDF 对代码块采用可换行文本。PDF 嵌入中文字体子集：Windows 自动使用微软雅黑，Docker 使用文泉驿正黑。其他系统可设置 `CLC_PDF_FONT` 为含中文字形的 TrueType TTF/TTC；Linux 也可安装 `fonts-wqy-zenhei`。Docker 自定义字体需挂载文件并使用容器路径。不支持 PostScript/CFF 轮廓字体，缺少字体时会明确报错。

## 资源限制与运维

默认每次 10 个文件、合计 40 MB、每份最多 300 页；任务标准化文字上限 40 万字符、80 个分块、300 个知识点。超限时明确失败，不会静默截断。可在 `.env` 调整 `CLC_MAX_*`、`CLC_CHUNK_CHARS`、`CLC_PLAN_CHARS` 和 `CLC_JOB_TIMEOUT`，同时注意供应商上下文与输出长度限制。

本机数据保存在 `data/`，Compose 数据保存在 `clc_data` / `n8n_data` / `docling_models` 卷。知识文档没有自动清理策略，请按自己的保存周期备份与清理；停止服务后可备份整个数据目录。不要在运行中仅拷贝 SQLite 主文件而遗漏 WAL。n8n 的执行记录默认保留 7 天，CLC 产物保留时间不受此设置影响。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check clc tests scripts
```

端到端测试使用本机 HTTP 测试模型，检查真实 HTTP 协议、工作进程、队列状态、知识图与 ZIP 导出，不调用付费模型。另有真实 Docling DOCX 集成测试；未安装 Docling 时该项跳过。本地另行验证了真实 Docling 中文 PDF 解析及 PDF 视觉输出，详见 [验证记录](VALIDATION.md)。n8n JSON 静态校验不等于实际 n8n 导入执行验收；真实供应商、扫描 PDF OCR 和 Docker 部署需要在配置好的运行环境验收。

实现参考：[Docling 转换接口](https://docling-project.github.io/docling/reference/document_converter/)、[Docling 支持格式](https://docling-project.github.io/docling/usage/supported_formats/)、[n8n HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)、[n8n Webhook](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/)。

