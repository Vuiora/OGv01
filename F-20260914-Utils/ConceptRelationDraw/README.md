# ConceptRelationDraw

一个通过浏览器使用的文档知识图谱服务。以 **n8n 编排 + Docling 文档解析 + GPT / 兼容模型接口**，提取文档实际涉及的知识点，结合完整原文判断概念关系，并生成可复核、可导出的关系图。

## 服务与 UI

- **Web / API**：中文工作台、文件上传、任务进度、模型配置、知识图谱、引用定位、关系复核和导出。
- **n8n**：调度“解析 → 提取 → 全局关系 → 生成图谱”，阶段失败停止执行。
- **Docling**：解析 PDF、DOCX、PPTX、XLSX、HTML 和图片，输出 Markdown；原本就是 Markdown / UTF-8 TXT 的文件直接读入，避免无谓转换。
- **模型服务**：支持 OpenAI Responses API 和 Chat Completions 兼容接口；每一批关系分析均传入完整文档和全部已提取概念。

UI 使用浅色背景、绿色操作按钮、章节颜色区分。左侧是文档和章节，中间是可拖动、缩放、搜索的关系图，右侧是知识定义、关系解释、原文证据与复核操作。顶部的“服务配置”提供 GPT Base URL、模型名、协议和密钥配置。窄屏下图谱和证据上下排列。

## Windows 原生后台服务（当前电脑）

这台电脑未安装 Docker，已补充独立后台服务启动方式：API、n8n、Docling 各自运行 HTTP 服务，关闭启动终端后继续运行。启动入口会在找不到 Docker 时自动选择此方式：

```powershell
.\start-service.ps1 -Deployment Native
```

使用 PowerShell 7.4 以上。当前复用同级 `ThePicWorkingFlow` 已安装的 Docling Python 和 n8n 二进制依赖，但不修改该项目，也不共用其任务、工作流、凭据或数据库。服务数据和日志全部在本项目 `data/` 下。其他机器可向 `scripts/start-native.ps1` 传入 `-DoclingPython`、`-N8nCli`、`-NodeExecutable` 指定已安装的运行环境。

启动脚本自动导入凭据和工作流、发布 Webhook，并等待三个服务就绪。工作台为 `http://127.0.0.1:8092`，n8n 为 `http://127.0.0.1:5679`，Docling 为 `http://127.0.0.1:5002`。查看 `data/logs/native-startup.log`，出现 `ConceptRelationDraw services ready` 后可提交任务。首次 n8n / Docling 启动可能需要数分钟。

```powershell
.\scripts\stop-native.ps1
```

原生方式是当前用户的后台进程，不注册 Windows 系统服务、不配置开机自启；重启电脑后重新运行启动脚本。长期服务器部署仍推荐下面的 Docker Compose 方式。API 保存的模型设置可继续使用。

## Docker Compose 部署

需要 Docker Engine / Docker Desktop 和 Compose v2。首次 Docling 启动可能需要下载模型，建议为容器提供至少 8 GB 内存；扫描件 OCR 的耗时和资源需求取决于文档。

Windows PowerShell：

```powershell
cd ConceptRelationDraw
.\start-service.ps1 -Deployment Docker
```

Linux / macOS：

```bash
cd ConceptRelationDraw
python3 scripts/init_env.py
docker compose up -d --build
```

初始化脚本仅在 `.env` 不存在时创建服务密钥，不覆盖现有配置。数据保存在独立命名卷；三个容器配置 `restart: unless-stopped`。API 以非 root 用户、单 worker 运行。工作流锁为进程内锁，不应直接改成多 worker 或多 API 副本。

| 入口 | 地址 |
| --- | --- |
| 文档工作台 | http://localhost:8091 |
| n8n 管理界面 | http://localhost:5679 |
| Docling | 仅容器网络内 `http://docling:5001` |
| API 健康检查 | http://localhost:8091/health |
| API 契约 | http://localhost:8091/docs |

端口与同级项目隔离。默认仅绑定本机回环地址。在远程服务器部署时，通过 HTTPS 反向代理提供访问，保留服务认证；工作台与 API 使用同一来源。密钥共享意味着这是单信任域服务，并非多租户权限系统。

### 首次配置 n8n

1. 进入 n8n 管理界面，完成自己的管理员初始化。
2. 从文件导入 `n8n/workflow.json`。
3. 在 n8n 创建 **Header Auth** 凭据：Header Name 为 `X-API-Key`，Value 为 `.env` 中的 `APP_API_KEY`。
4. 把这个凭据绑定到 Webhook 节点和四个 HTTP Request 节点，替换导入文件中的 `CONFIGURE_HEADER_AUTH` 占位。
5. 发布工作流。生产 Webhook 路径为 `/webhook/concept-relation`，不是测试 Webhook。

Webhook 只接收任务 ID，并立即返回 202；文档内容和模型密钥不经过 n8n。每个 HTTP 节点依次调用 API 的对应阶段，完成状态会写入 SQLite。失败后可从分析记录打开任务，点击“重试未完成阶段”。已经完成的阶段直接返回原状态，不重复调用模型，失败原因保留在任务历史中；失败阶段本身会重新执行。

### 在 UI 配置 GPT API

1. 打开工作台 → **服务配置**。
2. 输入 `.env` 中的 `APP_API_KEY`，点击“连接服务”。
3. 选择 **OpenAI Responses API**，Base URL 为 `https://api.openai.com/v1`。
4. 填写你账号可用的模型 ID 和 API Key，点击“保存模型配置”。例如 `gpt-4.1`；这是配置示例，不代表你的账号一定具有访问权限或当前最新模型。
5. 点击“新建分析”，上传文档，开始分析。

也可以在 `.env` 中配置：

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1
LLM_API_STYLE=responses
LLM_API_KEY=你的API密钥
```

第三方兼容接口可设置 `LLM_API_STYLE=chat`，要求支持 `/chat/completions`、`max_completion_tokens` 和 JSON Object 输出。协议适配不意味着所有厂商接口都完全兼容。

思考模型的输出额度通常包含推理过程。若报告“模型未完整结束输出”，可增加 `.env` 的 `LLM_OUTPUT_TOKENS`，并用 `LLM_REASONING_EFFORT` 指定该模型支持的思考强度；留空则使用厂商默认值。当前 DeepSeek 服务配置使用 `none`、16,384 输出 tokens、每批 3 个源概念，避免额外思考占满输出额度。关系分析遇到明确的输出上限错误会自动拆小源概念批次，每次仍传入完整原文及全部概念。修改这些环境参数后需要重启 API 服务。

网页保存的配置优先于 `.env`，写入 `concept_data` 卷中的 `/app/data/model-config.json`，保存后即时生效。密钥不回传给浏览器；重复保存时留空保留密钥。切换接口主机时必须重新输入密钥。正在执行任务时禁止切换模型配置，避免单个任务混用模型。访问密钥仅保存在页面内存，刷新后需重新连接。模型密钥在服务卷中为明文文件，仅容器应用用户可读写，备份数据卷时应一并保护。

## 文档知识点的边界

1. 按 Markdown 标题组织知识部分，超长章节按字符预算继续分块并少量重叠。全文覆盖，不只取前几页。
2. 分块提取只考虑文档主题、核心方法、关键机制和主要结论所需的概念，不把每个名词都当知识点。附件命名、提交要求、一次性数值、无独立意义的公式符号等默认排除，除非它们就是文档主题。程序要求名称出现在本块的逐字引用中，所有引用须能精确定位。
3. 再用完整文档筛选重要概念，默认最多 `MAX_KEY_CONCEPTS=20` 个，不要求凑满，每个保留项附带重要性理由。只能选择通过证据校验的候选 ID。模型可合并同名同义的候选，保留全部原文证据；同名异义应保留不同节点。不同名称不会自动合并，同义词仍可能重复，合并含义仍需人工复核。
4. 对每一批源节点，模型接收完整原文、全部概念和章节索引。目标可以来自任意章节，不以同章节或相似词作为唯一候选条件。
5. 关系只允许连接筛选后的重要概念，只返回直接支持文档核心内容的联系；禁止用共现、共享背景、传递路径或外部常识补全。默认每个源概念最多 `MAX_RELATIONS_PER_CONCEPT=3` 条，程序按具体类型及置信度控制预算，并移除已有具体联系时重复的泛泛关联。随后再用完整文档复核联系的重要性、方向和冗余，只能保留现有关系 ID。引用必须存在于原文且覆盖两端概念名称；无支持引用、自连接、无效端点被拒收。未入选候选、超预算及复核未保留的关系均记录原因。
6. 无可靠关系的知识点保留为孤立点。不会强制把所有节点连通。
7. 每条通过程序校验的模型关系初始都是“待复核”，用户可以确认、否决或重置。**原文定位成功并不等于关系含义、因果方向、摘要准确性或提取完整性已被证明。**

概念定义与关系解释仍是模型判断。严格的名称和逐字引用规则可能漏掉需要代词指代或同义改写才能表达的关系，这是优先保证来源可核对的取舍。图谱没有外部知识补全、网络检索或知识库扩展。

PDF 解析可能在词内插入空格或换行。引用定位先做精确匹配，失败后只允许忽略空白重定位，再取回原文中的真实切片保存；标点和其他字符改写不予接受。界面与导出中的引用始终包含原文实际字符及位置，全文关系复核仍是模型判断，不能替代人工确认。

旧任务可点击左侧“重新提取重要概念”，复用已有 Docling 解析并创建新的 n8n 分析记录，原图谱和复核结果保留。该操作会重新调用已配置的模型。候选保存在 `candidates.json`，全文筛选结果保存在 `concepts.json`，关系复核前候选保存在 `relation-candidates.json`。两个筛选预算在 `.env` 配置，重启 API 后生效；重要概念上限范围为 1–100。

图谱默认显示**主干关系**：按已确认关系、具体关系类型及置信度选择连接每个连通部分的树形主干，分层排列节点并用直角折线避让。它只减少同时显示的连线，不删除其他有效关系，界面明确显示当前/总关系数。可切换**全部关系**或**聚焦所选概念**，补充关系用虚线显示。关系名称默认在悬停或选中时显示，也可全部开启；孤立重要概念始终保留。一般图在展开全部关系时仍可能出现交叉。

`MAX_DOCUMENT_CHARS` 默认 60,000，`MAX_PROMPT_CHARS` 默认 100,000，包含完整原文、概念索引、提示词和 JSON 序列化开销。**字符预算不等于 token 预算**，需要按所选模型尤其中文文本保守设置并给输出留空间。超预算明确失败，不用截断或摘要冒充全文上下文。大文档会因每批传入全文而增加成本。

## 输出

| 输出 | 内容 |
| --- | --- |
| 交互图谱 | 主干 / 全部 / 局部聚焦、原文证据、重要性理由、章节筛选、搜索高亮、缩放、平移与置信度筛选 |
| JSON | 全部知识点、关系、逐字引用、转换后行号、字符偏移、复核状态与拒收记录 |
| SVG | 可缩放矢量图 |
| Mermaid `.mmd` | 可嵌入 Markdown 的关系图定义 |
| draw.io `.drawio` | 可编辑的原生节点和连接 |

SVG、Mermaid、draw.io 保留全部概念，导出时可选择主干或全部未被否决的关系，忽略章节、搜索与置信度筛选。API 参数为 `?view=backbone`（默认）或 `?view=all`。SVG 与 draw.io 使用工作台相同的布局和避让路径；SVG 悬停连线显示关系说明，Mermaid 布局由阅读器决定。JSON 始终保留完整审计记录。原文偏移使用 Unicode code point，行号对应转换后的 Markdown，不能当作 PDF 原页码。

内置“机器学习基础”图谱完全由人工整理，专门用于未配置服务时查看 UI，页面明确标注。示例置信度也是演示值，不代表真实模型调用。

## 运维

```bash
docker compose ps
docker compose logs --tail=100 concept-api n8n docling
docker compose stop
docker compose start
docker compose up -d --build
```

`stop/start` 和正常重建保留命名卷。请勿使用 `docker compose down -v`，除非明确需要删除任务、密钥和 n8n 数据。服务启动会把之前未结束的任务标记为中断，页面可查看原因并重试未完成阶段。n8n 本身的调度失败可在 n8n 执行记录查看；工作台等待超过 90 秒时提示检查执行记录。

## 开发验证

正式入口是 Compose 服务。开发测试可以使用项目虚拟环境，不需要 Docling/GPT 的真实凭据：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

测试模拟 Docling、模型 API 和 n8n Webhook，验证数据契约和应用行为。真实厂商、OCR 质量和 Docker 运行验证范围见 `VALIDATION.md`。完整技术设计见 `docs/DESIGN.md`。
