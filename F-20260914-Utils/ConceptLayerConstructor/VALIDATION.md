# 本地验证记录

## Docker 构建修复复测

用户日志显示下载截断后哈希校验失败：PyTorch 下载在 43.8/196.3 MB 停止，另一次 lxml 下载也未完成。Dockerfile 已升级 pip 至 26.2.1，配置下载恢复和连接重试，并拆分依赖安装层。保留 TLS 与哈希校验。Compose API 使用 `pull_policy: build`，Worker 使用 `pull_policy: never`。

- 故障注入测试通过：本地 HTTP 服务第一次只发送 300 KB，声明文件大小约 1 MB；新版 pip 发起 Range 续传，下载完成且最终 SHA-256 一致。
- 单独验证 `--only-deps` 可以在只复制项目元数据、尚未复制源码时执行。
- 官方 Docker Compose 2.40.3 的 `config --quiet` 校验通过；Ruff 与 PowerShell 启动脚本语法检查通过。
- 已确认本地队列为空并停止本地 API/Worker，释放 8000 端口，为切换容器方式做准备。
- 当前 Docker Desktop 虽报告运行中，`desktop-linux` 引擎管道不存在，`docker version` 无法连接服务端；需要恢复 Docker Desktop Linux 引擎后完成构建验收。
- 独立 WSL Docker 探测构建已通过系统依赖和 pip 升级；PyTorch 下载链路缓慢，探测已主动停止，避免继续占用下载资源。该探测的退出 137 来自主动停止，不是新的依赖安装结论。**本次尚未完成完整镜像构建，不将下载恢复测试等同于容器验收。**

以上状态更新优先于下文初次实现时的运行状态记录。

验证日期：2026-09-20。环境：Windows、Python 3.12，项目独立虚拟环境 `.venv`。

## 已通过

- `python -m pytest -q`：**13 项通过**。覆盖知识点提取与组装、三格式导出、引用核对、分块覆盖、适用版本区分、无环层级、节点完整性、HTTP 重试、鉴权、上传限制、路径约束、队列并发领取、中断恢复、真实 Worker 子进程与 HTTP 模型协议，以及真实 Docling Word 解析。
- 上传总量限制改为一致返回 HTTP 413 后，针对相关测试复测通过。
- `python -m ruff check clc tests scripts`：通过。
- `python -m compileall -q clc scripts tests`：通过。
- `python -m pip check`：无依赖冲突。
- 真实 Docling 解析生成的两页中文 PDF，返回 766 个 Markdown 字符，保留 `CFS` 文本。此验证实际初始化了 PDF 布局与 OCR 模型，不是解析接口替身。
- 中文 PDF 已使用嵌入字体重新生成，并经 Poppler 渲染、逐页查看两页图像；未发现缺字、重叠或裁切。QA 中间文件位于忽略目录 `tmp/qa/`。
- Word 文件经 OOXML / python-docx 内容检查，并成功由 Docling 再次解析。
- 两份 n8n 工作流通过 JSON 与节点连线静态检查；Compose 通过 YAML 服务结构检查。
- Windows 启动、停止、重启脚本实际运行成功。`/health` 返回 200，`/openapi.json` 返回 200，Worker 输出 ready。

当前服务入口为 `http://127.0.0.1:8000/docs`。模型保持未配置状态；API 密钥已随机生成在本地 `.env` 中，没有写入代码和工作流。

## 尚未验收的部分

- **真实大模型内容质量与供应商兼容性**：按用户要求，模型地址、模型名和密钥留待后续配置。端到端测试使用本机确定性 HTTP 测试模型，不能视为真实模型质量评测。
- **实际 n8n / Docker 联调**：本机没有 Docker，未构建镜像或执行 n8n 导入后的工作流。工作流参数依据官方节点文档与源码编写；需在 n8n 中选择 Header Auth 凭据后验收。
- **Word 逐页视觉检查**：已运行文档技能提供的 `render_docx.py`，因缺少 LibreOffice `soffice.exe` 失败。未把 Word 内容检查当作视觉检查。服务生成 DOCX 本身不依赖 LibreOffice。
- **复杂扫描件 OCR**：已验证普通中文 PDF；未对倾斜、低清晰度、多栏扫描件建立准确率评测。

测试中出现 Starlette/httpx 与 AnyIO 的弃用提示，不影响本次结果。后续依赖升级时应重新运行测试。

## 主要验证版本

| 组件 | 版本 |
| --- | --- |
| Docling | 2.129.0 |
| FastAPI | 0.141.1 |
| Pydantic | 2.13.5 |
| HTTPX | 0.28.1 |
| python-docx | 1.2.0 |
| ReportLab | 4.5.1 |
| pytest | 9.1.1 |

项目依赖声明采用兼容版本范围；该表记录本次测试版本，不代表已经锁定全部传递依赖或验收了所有后续版本。
