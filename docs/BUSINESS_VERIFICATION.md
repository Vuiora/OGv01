# 业务实现验证记录

日期：2026-09-30，Windows / Python 3.12。与初次历史迁移的 [VERIFICATION.md](VERIFICATION.md) 分开记录。

## 离线测试

| 范围 | 结果 | 验证内容 |
| --- | --- | --- |
| 根业务包 | 21 通过 | 跨项目主循环、冻结先于消费、每个假说独立统计、禁止重新封存、归档重试不碰 C、历史数据排除与真实随机森林训练、表达式攻击拒绝、API 实际提交与下载边界、工具传输、结构桥、原生架构页面 |
| SDL 全量 | 1,747 通过，1,870 子测试通过 | M1–M12 与循环回归；原有两个 M1 问题修复，两个冻结目录测试改为验证实际行为前后的文件内容 |
| TPWF | 19 通过 | 分析/设计/渲染/校验流程回归 |
| CLC | 20 通过、3 跳过 | 结构、引用、任务与导出回归；跳过的是未安装 Docling 的可选解析测试 |
| CRD | 48 通过 | 解析、筛选、引用、关系和任务回归 |

以上 1,855 个用例通过，另有 3 个可选用例跳过。根业务包也在新建 `.venv`、仅安装 `.[test]` 的环境下完整通过（21 项），避免依赖旧迁移环境中额外安装的库。

现有 FastAPI/Starlette TestClient 给出 httpx 弃用提示，不影响本次结果。没有宣称全部子项目或 GPU/NPU 都经过这次验收；原 PLDA 在 Python 3.12 的时间戳格式测试差异仍记录于历史验证报告。

## 实时 UJN 验证

共同接口为 `https://llm.ujn.edu.cn/v1/chat/completions`、模型 `deepseek-v41-flash`。没有使用离线固定输出来替代失败调用。

1. JSON 连接和 function calling 协议均实际调用通过。
2. 合成观测的 MTBMT → 实时 LLM 表达式 → SDL → PLDA → 冻结确证 → 归档完成。任务 `81cbbcfa493e471e8c27d2f9a8b647d5` 最终为 SUCCEEDED，保留科学结果和开放问题。
3. 归档后实际执行 Self Renew 的只读工具取证、AgentRunner、`final_result` 和引用校验：一次成功模型请求，11,955 tokens。两条发现只表示待复核意见；人工静态核对驳回了一条关于“无预算”的行为推断，确认另一条基线重复计算为低影响优化机会，详见 `artifacts/business/review-adjudication.json`。这验证了“引用存在”不能替代行为验证。
4. 逐任务实时 TPWF 架构生产完成，6 个模块、7 页。设计的第一次结果未满足既定约束，第二次通过契约与 XML 门禁。该重试使用已有发现归档，`new_confirmation_evidence=false`。
5. 单独验收 Markdown 文档分支：真实 CLC 层级、CRD 重要概念和关系筛选。CRD 网络超时后保留失败任务，新任务复用已通过的解析/提取阶段再分析关系；最终没有保留关系边，未强行补全图谱。具体数量、来源和重试任务 ID 见 `artifacts/business/live-validation.json`。这条分支单独验证，没有宣称所有文档阶段与科学确证在同一次任务中重新执行。
6. 独立业务架构 `artifacts/business/architecture/` 同样使用实时 TPWF 分析与设计，全部源文件散列与当前实现匹配。实际在浏览器检查了总览和 SDL 模块页；预览支持分页、适应窗口和缩放。原生 draw.io 仍是编辑源。
7. 新环境启动本机 API，`/health` 与认证后的 `/v1/application` 均返回 200；真实任务提交和归档下载已由业务测试覆盖。

网络超时、模型输出截断、首次评估器字段错误和预算超限的尝试均留在本机验证目录，未改成成功。成功产物的摘要提交到 `artifacts/business/`；含原始输入快照的作业目录、证据数据库、配置和模型密钥不进入 Git。

## 可复验命令

```powershell
.\tools\business\setup.ps1
.\.venv\Scripts\python.exe -m pytest -q

# SDL 回归，在项目目录中执行
cd F-20260919-StatisticalDiscoveryLearning
..\.venv\Scripts\python.exe -m pytest -q
cd ..

# 追溯导入基线与 54 个原始提交；后续源码修改允许存在
.\.venv\Scripts\python.exe tools/monorepo/import_workspace.py verify --repo . --revision 04e4cff --source ..

# 从原生 XML 重建本地预览
.\.venv\Scripts\python.exe tools/business/preview_architecture.py artifacts/business/architecture/architecture.drawio
```

原子数据登记、防重新封存和 alpha 记录在同一输出根目录内有效。分布式登记、生产 n8n 部署、PDF/DOCX 模型解析、多轮新数据自动采集、自动研发合并和硬件加速不是本次已验证能力。

发布前再次核对：导入基线 1,224 个文件、54 个原始提交、原工作区 Git 状态和 `git fsck` 全部通过；当前待提交文件中没有匹配本机 LLM 密钥、API 访问令牌或 UJN 密钥模式，实际 `.env` 被 Git 忽略。GitHub API 确认 `Vuiora/OGv01` 为 PRIVATE。
