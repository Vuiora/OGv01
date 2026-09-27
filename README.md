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

本版本不含单位自动换算、插补器、通用领域规则表达式或统计检验执行器。可以登记这些后续活动所需的语义与计划；不能把登记成功解释为新理论成立。

## 上层模块（M2–M12）

`README.md` 上文描述的是 M1（数据与证据协议）。在其之上，本项目还实现了完整的发现回路：

| 模块 | 文件 | 职责 |
|---|---|---|
| M2 | `sdl_m02/` | 受限表达式、候选枚举、表示构造 |
| M3 | `sdl_m03/` | 关系/方程搜索、结构模式、稳定性评估 |
| M4 | `sdl_m04/` | 假说对象与模式→假说生成 |
| M5 | `sdl_m05/` | G/S/N/C 指标与 Pareto 筛选 |
| M6 | `sdl_m06/` | 冻结计划、检验执行、结果分类发布 |
| M7 | `sdl_m07/acquisition.py` | 主动取证**建议**（纯建议对象，不执行采集） |
| M8 | `sdl_m08/archive.py` | 知识归档、证据等级机械判定（E0/E1/E2） |
| M9 | `sdl_m09/collection.py` | 自主取数**执行器**：把 M7 建议变成真的数据 |
| M10 | `sdl_m10/toolbox.py` | 分析**决策层接口**：把 M2–M9 封装为 LLM 可调用的工具目录 |
| M11 | `sdl_m11/driver.py` | LLM **驱动层**：真正把工具目录交给 LLM，编排并执行其工具调用 |
| M12 | `sdl_m12/sandbox.py` | **隔离代码执行环境**：对 LLM 提交的代码先静态检查、后受限执行 |
| 主循环 | `sdl_pipeline/loop.py` | 端到端调度（P16） |

**M7 与 M9 的分工**是「建议层 / 执行层」：M7 说「哪里最值得看」，M9 说「那就去取这些」。
M9 原样采信 M7 的启发式排序，不授予证据等级、不执行统计检验。

**M10 与 M2–M9 的分工**是「决策层 / 算法层」：M10 把既有公开接口封装为工具目录，
**由 LLM 决定调用顺序与参数**，算法负责执行与校验。这是框架设计意图的落点——
[SDL算法框架说明.md](SDL算法框架说明.md) §7 要求「LLM 辅助提出表示、候选解释、竞争假说、
可执行代码草案以及取证建议」，即 **LLM 自己使用已有算法做分析**，而不是仅做文本润色。

**M10 / M11 / M12 三者合起来才补全框架 §7**：M10 只是**接口**（工具目录 + 句柄 + 护栏），
本身不含 LLM；**M11 才是真正发起对话、解析并执行工具调用的一环**；**M12 则是让 LLM 能安全
提交代码草案的前提**（框架同处写明「LLM 生成的代码须经检查并在隔离执行环境运行」）。


M10 **不放松任何证据闸门**：不新增统计能力（`TOOLBOX_ADDS_ALGORITHMS=False`）、
不授予证据等级（`TOOLBOX_GRANTS_EVIDENCE_GRADE=False`）、不执行统计检验
（`TOOLBOX_RUNS_STATISTICS=False`）、不替 LLM 决策（`TOOLBOX_DECIDES_NOT_LLM=False`）。

```python
from sdl_m10.toolbox import build_default_toolbox

tb = build_default_toolbox(explorer=explorer)   # explorer 来自 M1，用于读探索分区 E
print(tb.names())                               # 15 个工具（m02.–m09.）
print(tb.describe("m03.fit_relation").to_dict())  # 参数 schema 由 inspect.signature 派生

# 一条完整分析链：LLM 逐步决定「调用哪个算法、传什么参数」，算法负责执行与校验
ast  = tb.call("m02.parse_expression", text="X1 / X2")                            # 提出比例结构
samp = tb.call("m03.prepare_sample", protocol_id=pid,
               variables=["X1", "X2"], target="Y")                                # 读 E 分区
fit  = tb.call("m03.fit_relation", sample=samp.handle, relationship=ast.handle)   # 拟合
base = tb.call("m03.baseline_linear", sample=samp.handle, feature="X1")           # 线性基线
gain = tb.call("m03.gain_against", candidate=fit.handle, baseline=base.handle)
print(gain.summary["candidate_better"], gain.summary["relative_reduction"])
```

工具名统一带模块前缀（`m02.`–`m09.`），便于 LLM 从目录中辨认算法归属。
链路上的中间产物（Ast、样本、拟合结果）都以句柄（`h_<hex>`）在各调用间传递。

**句柄机制**：M2–M9 的算法接收富对象（`Candidate`、`ExplorationSample`、`KnowledgeBase` 等），
无法经 JSON 传递。M10 为其分配不透明句柄（形如 `h_<hex>`），LLM **只能引用上游真实产出**，
无法凭空构造内部对象绕过校验——这类似于 M1 中 `historical_ref` 的不透明引用语义。

**端到端自主发现演示**（含合成验证标注）：

```python
from sdl_m09.collection import run_autonomous_discovery
run_autonomous_discovery("demo-output/p17-autonomous", groups=60)
```

产物：`regularities.json`（规律清单）、`collection.json`（逐轮取数）、`collection-trace.json`（建议→采样因果链）、`discovery-archive.json`、`说明.md`。**合成数据不宣称任何现实理论或因果结论。**

> M9 的采样目录（观测标识 → 现实采样方式）**由调用方提供**；目录缺失时如实记为「没采」，绝不臆造设计。「没采」与「采了为空」始终分列，绝不用 0 条记录冒充「已经取过」。

### M11 驱动层：把工具目录真正交给 LLM

M11 读取 M10 的工具目录，组装成 LLM 可读的工具 schema，向 endpoint 发起对话，解析 LLM
请求的工具调用，经 `Toolbox.call()` 执行后再把结果回喂，循环至 LLM 收手或触达预算。

```python
from sdl_m11.driver import EndpointConfig, HttpLLMClient, run_driver
from sdl_m10.toolbox import build_default_toolbox

cfg = EndpointConfig.from_env()          # 凭据只从 SDL_LLM_* 环境变量读，绝不落盘
client = HttpLLMClient(cfg)
session = run_driver(client=client, toolbox=build_default_toolbox(explorer=explorer),
                     user_task="在探索数据里找一个能解释 Y 的比例关系，并给出竞争解释。")
print(session.stop_reason, session.tool_call_count)
```

M11 **不替 LLM 决策、不新增算法、不跑统计、不授予证据等级**（`DRIVER_DECIDES_FOR_LLM=False`
等），一切工具调用必须经 `Toolbox.call()`（`DRIVER_BYPASSES_TOOLBOX=False`）。
**凭据纪律**：`api_key` 只存在于内存，不入 `to_dict()`、不入规范化形式、不入 `content_digest()`、
不入提示词。**离线可测**：对话被抽象为 `LLMClient` 协议，测试注入脚本化假 client 即可跑通整条链。

**跑一次真实端到端分析**（真 M1 金库 + 真 endpoint 驱动；密钥只走环境变量）：

```bash
export SDL_LLM_BASE_URL=https://llm.ujn.edu.cn/v1
export SDL_LLM_API_KEY=...          # 不落盘、不入库
export SDL_LLM_MODEL=Qwen3.8-Flash-Next
python scripts/demo_m11_live.py      # 产物写入 demo-output/p19-live/
```

一次实测运行中，LLM 自主完成：提出候选 `X1+X2`/`X1*X2` → 落 AST 句柄 → 制备样本（E 分区 72 行）
→ **依据残差自己把假说改成 `X1^2`** → 拟合 → 建常数/线性基线 → `gain_against` → 报出证据地位
「仅为探索性发现，不构成证据；V 分区未触碰，是否确证由系统机制判定」。这正是设计意图：
**LLM 是分析主体，但无权授予证据**。

### M12 隔离代码执行：让 LLM 安全地提交代码草案

框架要求「LLM 生成的代码须经检查并在隔离执行环境运行」。M12 接收一段代码，**先做 AST 静态检查，
只有完全通过的代码才在受限运行时里执行；检查不过一律不执行**（不存在「先跑再看」）。

```python
from sdl_m12.sandbox import check_code, run_code

v = check_code("import os\ndef main():\n    return os.getcwd()\n")
print(v.allowed, v.violations)           # False，命中「禁止模块与名字」
run = run_code("def main(xs):\n    return {'n': len(xs), 's': sum(xs)}\n",
               entry="main", inputs={"xs": [1, 2, 3]})
print(run.status, run.value)             # ok {'n': 3, 's': 6}
```

隔离措施：白名单导入（纯计算模块）、受限内置、`sys.settrace` 指令数与墙钟限额、无 M1 访问、
无网络、无文件系统、无子进程。状态码 `ok` / `rejected` / `failed` / `timeout`。
M12 与 M10/M11 一样**不新增算法、不授予证据等级**。当 M11 以 `allow_code_submission=True`
启用时，M12 作为 `m12.run_code` 工具暴露给 LLM（**默认关闭**）。

