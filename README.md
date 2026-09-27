# SDL 模块 01：数据与证据协议

本实现将[模块01理论](SDL模块01-数据与证据协议.md)落为可运行的 Python SDK 和命令行工具：登记任务及观测语义，保留原始快照，按用途分区，约束角色访问，并管理冻结、确证读取、结果发布与历史归档。

**M1 管理证据使用，M6 负责统计检验。** 本项目不执行 p 值计算、Holm 校正或科学结论判定；检验族及误差预算的登记不等于统计前提已经成立。

## 快速运行

需要 Python 3.11 或更新版本；运行与测试只使用 Python 标准库。在本项目目录执行：

```powershell
python -m unittest discover -s tests -v
python -m sdl_m01 demo --output demo-output
```

`demo-output` 必须是不存在的新目录。重复演示时换一个目录名；工具不会覆盖已有证据库。无需先安装本包，也无需联网。

演示生成 **120 个合成批次、每批 8 条读数**，按组划分为 **72 / 24 / 24** 个探索、开发、确证批次。比例候选与均值基线只用探索资料估计参数，随后冻结方案、读取确证数据、记录外部评估、发布结果、建立历史视图。

结果明确记为 `inconclusive`：只计算了批次等权的描述性损失差，没有执行统计检验。比例关系是演示用的已知生成机制，不是 SDL 自动发现的理论。

主要输出：

| 文件 | 含义 |
|---|---|
| `spec.json`、`records.json` | 已登记设计与完整合成原始资料；属于保管侧输入 |
| `evidence.sqlite3` | 持久化快照、用途、状态和账本 |
| `credentials/*.token` | 四个角色各自的单令牌文件；仅供本地管理与接口演示 |
| `public-protocol.json` | 探索侧可读协议；确证资源使用不透明引用 |
| `E-view.json`、`V-view.json` | 探索、开发的可用与带标记记录 |
| `E-quality.json`、`V-quality.json` | 相应公开视图的质量报告 |
| `frozen-plan.json`、`binding.json` | 冻结计划及不可覆盖的绑定身份 |
| `external-evaluation.json`、`released-result.json` | 外部评估记录与已发布结果 |
| `historical-reference.json` | 后续可学习的历史视图；不构成新确证证据 |
| `audit-ledger.json`、`integrity.json` | 受限管理账本与完整性核对结果 |
| `demo-summary.json`、`说明.md` | 分区概况、凭据路径和演示说明 |

演示由单个受信任进程扮演四个角色，用来检查接口生命周期。它并没有建立四个操作系统安全域。

## 命令行

先建立新证据库。角色令牌不会打印到标准输出，而是分别写到指定目录：

```powershell
python -m sdl_m01 init --db evidence.sqlite3 --credentials-dir credentials
python -m sdl_m01 build --db evidence.sqlite3 --token-file credentials/custodian.token --spec demo-output/spec.json --records demo-output/records.json --output protocol.json
```

读取探索资料：

```powershell
$protocol = Get-Content -Raw -Encoding utf8 protocol.json | ConvertFrom-Json
python -m sdl_m01 read --db evidence.sqlite3 --token-file credentials/explorer.token --ref $protocol.resources.E --output exploration.json
python -m sdl_m01 quality --db evidence.sqlite3 --token-file credentials/explorer.token --ref $protocol.resources.E
python -m sdl_m01 verify --db evidence.sqlite3 --token-file credentials/auditor.token
```

也可以通过 `SDL_M01_TOKEN` 环境变量传入一个令牌；指定 `--token-file` 时优先读取文件。没有接受令牌正文的命令行参数，避免令牌进入 shell 命令历史和进程参数。

```powershell
$env:SDL_M01_TOKEN = (Get-Content -Raw credentials/auditor.token).Trim()
python -m sdl_m01 audit --db evidence.sqlite3 --output audit.json
Remove-Item Env:SDL_M01_TOKEN
```

除 `init`、`demo` 外，各命令均需要 `--db` 及一种身份输入。成功结果为 UTF-8 JSON，默认写到标准输出；`--output <文件>` 可写到文件。输入 JSON 可带 UTF-8 BOM；不允许 `NaN` 或无穷数。已处理的业务错误以 JSON 写到标准错误并返回非零退出码。`--help` 显示参数说明。

| 命令 | 专用参数 | 通常使用的角色 |
|---|---|---|
| `build` | `--spec <JSON> --records <JSON>` | custodian |
| `describe` | `--protocol <协议ID>` | 任一角色 |
| `read` | `--ref <资源引用>` | explorer / custodian，仅探索、开发或历史资料 |
| `quality` | `--ref <资源引用>` | explorer 仅公开用途；custodian / auditor 可读受限报告 |
| `bind` | `--ref <确证引用> --plan <JSON>` | confirmer |
| `consume` | `--binding <绑定ID> [--replay]` | confirmer |
| `record` | `--binding <绑定ID> --result <JSON>` | confirmer |
| `release` | `--binding <绑定ID>` | confirmer |
| `results` | `--binding <绑定ID>` | explorer 只能获得已发布结果 |
| `archive` | `--binding <绑定ID>` | custodian |
| `add-confirmation` | `--protocol <协议ID> --records <JSON> --purpose C2` | custodian |
| `compromise` | `--ref <确证引用> --reason <原因>` | custodian；不可逆 |
| `audit`、`verify` | 无额外参数 | custodian / auditor |

冻结计划可以参考演示的 `frozen-plan.json`，但必须替换为当前协议、当前 E 引用及实际候选，不能直接跨库复制绑定。完整字段见 [IMPLEMENTATION_CONTRACT.md](IMPLEMENTATION_CONTRACT.md)。`consume --replay` 只允许同一已冻结绑定的复算，不会重新封存批次，也不会生成独立证据。

## SDK 与数据契约

```python
from pathlib import Path
import json
from sdl_m01 import Module01, initialize

tokens = initialize("new-evidence.sqlite3")  # 已有数据库会被拒绝
custodian = Module01("new-evidence.sqlite3", tokens["custodian"])
explorer = Module01("new-evidence.sqlite3", tokens["explorer"])
confirmer = Module01("new-evidence.sqlite3", tokens["confirmer"])

spec = json.loads(Path("demo-output/spec.json").read_text(encoding="utf-8"))
records = json.loads(Path("demo-output/records.json").read_text(encoding="utf-8"))
protocol = custodian.build(spec, records)
exploration = explorer.read_dataset(protocol["resources"]["E"])
# 候选搜索、参数拟合与具体统计方法由后续模块实现。
# plan 必须含当前 protocol_id、拟合来源、候选、全部检验、采样和停止规则。
# binding = confirmer.bind_confirmation(protocol["resources"]["C1"], plan)
# confirmation = confirmer.consume_confirmation(binding["binding_id"])
# confirmer.record_evaluation(binding["binding_id"], external_result)
# confirmer.release_results(binding["binding_id"])
# history = custodian.archive_confirmation(binding["binding_id"])
```

`Module01` 每次操作内部管理数据库连接和事务，无需额外关闭。SDK 的 `initialize()` 返回秘密令牌，调用者应保存到受控位置；不要打印、提交版本库或加入模型提示词。CLI 为本地使用提供独立令牌文件。

规范 `spec` 登记 `task`、`schema`、`dependence`、`split`、`quality`、`confirmation` 和 `identification_gaps`。原始记录至少明确 `record_id`、`group_ids`、`environment`、带时区的 `event_time`、`available_time`、`values`、`units`、`source`。`available_time` 可以按字段提供；`prediction_time` 存在时，特征必须在预测时已经可获取，稍后才能取得的目标结果会被记录。

未知附加字段予以保留。异常值默认标记，单位冲突或身份不明进入隔离；不做静默插补，不用拟合残差删除反例。分析视图只返回 `usable` / `flagged` 记录，并增加 `_quality`，原始及隔离记录仍在受控快照中保留。

分区支持：

- `grouped`：按声明的身份字段关联组，先确定组用途，再应用记录质量状态。输入顺序变化不改变分区。
- `environment`：按环境留出，可使用 `environment_assignments` 指定用途。
- `temporal`：按有时区的时点及 `cutoffs` 划分，可指定 `gap_seconds`；尊重当时的信息可获取时间。同主体跨时间允许出现，不自动代表时间独立。

互斥组设计不能通过改记录 ID、重建协议或重新抽样，把旧数据变成新的确证证据。新确证批次由 `add_confirmation()` 登记；原内容、组身份和时间约束继续接受检查。

## 理论与实现的对应

| 理论对象 | 实现 |
|---|---|
| 任务契约、观测模式、未决条件 | 输入规范校验、版本化协议及识别缺口 |
| 原始与派生快照 | SQLite 内规范化 JSON、内容摘要及用途引用 |
| 质量描述 | 四种记录状态、显式缺失分母、单位/身份/时间检查、受限报告 |
| 分区与访问协议 | 三种分区策略，四种令牌角色，探索侧不获得封存摘要 |
| 冻结方案 | 假说、参数、预处理来源、指标、检验族、采样与停止规则的冻结摘要 |
| 确证状态转换 | `sealed → bound → used`，发布后建立 `historical` 视图；暴露标为 `compromised` |
| 原子证据消费 | 返回数据前提交 `used` 与消费事件，失败不能自动恢复封存身份 |
| 误差预算 | 按 `total_alpha / 2**round_index` 登记预算及唯一轮次；检验计算交给 M6 |
| 证据账本 | 成功/失败的已认证操作、快照摘要检查和哈希链核对 |

接口区分 `ValidationError`（输入或声明不完整）、`AccessDenied`（角色受限）、`StateError`（状态不允许）与 `IntegrityError`（存储内容与摘要不一致）。发布只能传递已登记结果；确证原始资料不会通过结果接口返回。归档保留原绑定已被消费的事实。

## 实现边界

这是**本地受信任 SDK 的逻辑访问边界**。令牌哈希与方法级权限能够约束接口调用，但不能阻止具有同一操作系统账户权限的程序直接读取数据库、原始输入或其他角色令牌。Windows 下令牌文件也依赖其所在目录的实际 ACL。数据库文件、保管器凭据及受限输出必须由可信管理侧持有；要接入不可信 LLM、插件或执行代码，需要独立服务与操作系统/容器/账户权限隔离。

哈希链可以发现与已存摘要不一致的修改，不是防管理员重写的不可篡改设施；也不证明观测真实或从未泄漏。实现检查声明是否完整、用途及状态是否一致，不能自动证明代表性、组间独立性、条件 p 值有效性或采样方案适用性。

本版本不含单位自动换算、插补器、通用领域规则表达式、统计检验执行器或完整的 M2—M8。可以登记这些后续活动所需的语义与计划；不能把登记成功解释为新理论成立。
