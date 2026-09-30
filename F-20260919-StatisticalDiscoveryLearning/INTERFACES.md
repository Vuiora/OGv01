# 模块间接口契约（M2–M12）

版本：v0.4｜状态：草案（P00 交付物，P17 补 M9，P18 补 M10，P19/P20 补 M11/M12）

## 0. 总则

1. 本文档定义 M2–M12 之间的数据对象与调用方向。**所有模块均不得修改 `sdl_m01/`**，只调用其公开接口。
2. M2–M10 与 M12 一律使用 Python 3.11+ 标准库，不引入第三方依赖。**M11（LLM 驱动层）是唯一允许做网络调用的模块**，且仅通过标准库 `urllib` 或以鸭子类型注入的 HTTP 客户端；它不引入任何第三方 SDK 依赖。
3. 数据访问必须经由 M1 的令牌角色：探索侧仅使用 `explorer`，确证侧仅使用 `confirmer`，保管侧使用 `custodian`。
4. 任何模块都不得读取、打印或持久化角色令牌正文。**LLM endpoint 的凭据（密钥）同样不得进入模型上下文、日志、提示词或版本库。**

## 1. 调用方向

```
M2 表示构造 ──> M3 模式搜索 ──> M4 假说构造 ──> M5 评估筛选 ──> M6 冻结确证
                    │                                  │              │
                    └──────────────> M7 主动取证 <──────┘              │
                                              M8 归档 <────────────────┘
                                                    │
                                          M9 自主取数 <── M7（建议）
                                                    │
                                          M10 分析决策 <── 编排以上全部（工具调用）
                                                    │
                                          M11 LLM 驱动 <── 喂工具目录、收工具调用
                                                    │
                                          M12 隔离执行 <── 运行 LLM 提交的分析代码
```

**M11 是「设计意图的最后一环」**：《SDL算法框架说明》§7 要求「LLM 辅助提出表示、候选解释、
竞争假说、可执行代码草案以及取证建议」。P18 交付的 M10 只是**给 LLM 用的接口**
（工具目录 + 句柄 + 护栏），**其本身不包含 LLM**；M11 才是真正去调 LLM、把工具目录喂过去、
解析其工具调用意图、经 `tb.call()` 执行、再把结果回喂的那一层。没有 M11，M10 是一座**没有访客的桥**。

**M12 是「代码草案」的安全前提**：框架 §2 末段明确「**LLM 生成的代码须经检查并在隔离执行环境运行**」。
M12 提供这一隔离环境——AST 静态检查 + 受限内置 + 资源限额 + **无 M1 访问**。

M7 服务于下一轮取证，M8 保存历史并把可用信息送回探索。

**M10 与 M2–M9 的分工是「决策层 / 算法层」**，二者互为补集且可机检：

| 层 | 职责 | 执行标志 |
|---|---|---|
| M2–M9（算法层） | 提供**可单独调用**的分析原语（枚举、拟合、检验、筛选、冻结、取证、归档、取数） | 各模块 `NOT_PROVIDED_BY_Pxx` 机检常量 |
| M10（`toolbox.py`） | 把上述原语包装成**工具箱**，由 **LLM 决定调用顺序**，算法层负责执行与校验 | `TOOLBOX_DECIDES_NOT_LLM = False`（M10 只登记与派发，不替 LLM 决策） |

M10 是《SDL算法框架说明》§7「LLM 辅助提出表示、候选解释、竞争假说、可执行代码草案以及取证建议」的**落地载体**：它不新增任何统计能力，只把已有的确定性原语暴露为 LLM 可编排的工具，使 LLM 成为**分析主体**而非文字润色器。

**边界不变**：LLM 经 M10 调用工具所产出的一切候选、假说、计划，仍须通过各层既有的机械闸门（定义域、单位、去重、冻结、误差预算）；M10 自身**不授予证据等级**（`TOOLBOX_GRANTS_EVIDENCE_GRADE = False`）、**不执行统计裁决**（`TOOLBOX_RUNS_STATISTICS = False`），裁决权仍在 M6/M8。

**M7 与 M9 的分工是「建议层 / 执行层」**，二者互为补集且可机检：

| 层 | 职责 | 执行标志 |
|---|---|---|
| M7（M7 `acquisition.py`） | 说「哪里最值得看」——出**纯建议**计划 | `ACQUISITION_PLAN_EXECUTES_COLLECTION = False` |
| M9（M9 `collection.py`） | 说「那就去取这些」——**真的**驱动数据源取数并交 M1 登记 | `COLLECTION_EXECUTES_COLLECTION = True` |

M9 原样采信 M7 的启发式排序（不重算、不重排），且**不授予任何证据等级**（`COLLECTION_GRANTS_EVIDENCE_GRADE = False`）、不执行统计检验（`COLLECTION_RUNS_STATISTICS = False`）。数据是否构成证据仍由 M8 机械判定。

## 2. 跨模块数据对象

### 2.1 Candidate（候选变量／表示，M2 产出）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `candidate_id` | str | 是 | 稳定标识，规范式哈希派生 |
| `expression` | dict | 是 | 规范化 AST |
| `depth` | int | 是 | 表达式深度，上限 3 |
| `node_count` | int | 是 | 节点数，受预算约束 |
| `domain` | dict | 是 | 定义域条件，如分母非零阈值 |
| `unit` | str | 否 | 量纲签名，缺失表示未定 |
| `provenance` | dict | 是 | 来源数据引用与构造版本 |

### 2.2 Pattern（模式，M3 产出）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pattern_id` | str | 是 | 稳定标识 |
| `kind` | str | 是 | `relation` / `cluster` / `changepoint` / `invariant` |
| `payload` | dict | 是 | 关系式或结构描述 |
| `metrics` | dict | 是 | 误差、残差、复杂度三项 |
| `stability` | float | 否 | 重采样稳定性，落在 [0,1] |
| `provenance` | dict | 是 | 使用的分区引用（仅 E） |

### 2.3 Hypothesis（假说，M4 产出）

字段定义遵循《SDL算法框架说明》§3 的 H 结构，要点：

| 字段 | 必填 | 约束 |
|---|---|---|
| `id` / `version` | 是 | 版本不可覆盖，新版本保留 `parent_ids` |
| `predictions` | 是 | 必须写明什么结果与假说不相容 |
| `null_hypotheses` | 是 | 至少一个 |
| `alternatives` | 否 | 竞争解释 |
| `representation` | 是 | 完整变换，含缺失值处理与标准化 |
| `provenance` | 是 | 缺失即报错，不得默认填充 |

### 2.4 Evaluation（评估记录，M5 产出）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hypothesis_id` | str | 是 | 关联假说 |
| `gain` | float | 是 | 预测增益 G |
| `stability` | float | 是 | 稳定性 S |
| `novelty` | float | 是 | 新颖性 N，须绑定 `knowledge_version` |
| `complexity` | float | 是 | 复杂度 C，标注为公式长度近似 |
| `knowledge_version` | str | 是 | 新颖性所相对的 K 版本 |

**约束**：本层不做加权求和；不得出现「新颖性加分抵消弱证据」的总分。

### 2.5 FrozenPlan（冻结计划，M6 产出）

字段与 `IMPLEMENTATION_CONTRACT.md` 的 Frozen plan 逐项一致：`protocol_id`、`round_index`、`hypotheses`、`preprocessing`、`primary_metric`、`test_family`、`effect_threshold`、`sampling_plan`、`stopping_rule`、`inference_unit`、`eligibility`、`quality_rules_version`、`assumptions`。

**约束**：`preprocessing.fit_dataset_refs` 只能指向 E；`test_family` 必须完整登记，不得事后增删。

### 2.6 ConfirmationOutcome（确证结果，M6 产出）

| 字段 | 取值 | 说明 |
|---|---|---|
| `status` | `supported` / `refuted` / `inconclusive` / `failed` | 不显著一律归 `inconclusive` |
| `metrics` | dict | 效应量与不确定性 |
| `notes` | str | 说明 |
| `code_version` | str | 可追溯版本 |

### 2.7 SamplingRequest / CollectionOutcome（自主取数，M9 产出）

**SamplingRequest**（由 M7 计划翻译而来，原样转录其排序）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `observation_id` | str | 是 | 对应的 M7 观测标识，原样保留 |
| `rank` | int | 是 | 在 M7 计划中的序号，**原样转录、不重排** |
| `observation_rank` | float \| None | 否 | M7 的启发式排序值（分歧 ÷ 成本） |
| `design` | dict | 是 | 采样设计（环境、批次数、读数），**由调用方的采样目录给出** |
| `purpose` | str | 是 | 本轮取证目的 |
| `provenance` | dict | 是 | 计划摘要等溯源信息 |

**CollectionOutcome**（一轮自主取数的完整产出）：

| 字段 | 取值 | 说明 |
|---|---|---|
| `requests` | tuple | 翻译得到的采样请求（按 M7 排序） |
| `results` | tuple | 逐请求的采集结果 |
| `reason_counts` | dict | 各原因码计数；**「没采」与「采了为空」分列** |
| `registered_refs` | tuple | 已交 M1 登记的数据引用 |

**原因码（验收纪律：二者绝不可混淆）**：

| 原因码 | 类别 | 含义 |
|---|---|---|
| `collected` | 已取到 | 设计齐备、驱动了数据源、取回非空 |
| `source_empty` | **采了为空** | 设计齐备、确实去取了、但返回空 |
| `no_mapping` | 没采 | 该观测在采样目录中没有匹配设计 |
| `no_plan` | 没采 | 上游未给出任何建议 |
| `budget_exhausted` | 没采 | 采集预算用尽 |
| `source_failed` | 没采 | 数据源取数抛错，如实降级 |

**约束**：采样目录（观测标识 → 现实采样方式）**由调用方提供**，M9 无法推断；目录查不到且无兜底时如实记为**没采**（`no_mapping`），绝不臆造设计。

### 2.8 ToolSpec / ToolResult（分析决策层，M10 产出）

**ToolSpec**（一个可被 LLM 调用的分析原语）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | 是 | 工具唯一名，如 `m02.enumerate_candidates` |
| `module` | str | 是 | 所属算法模块（M2–M9） |
| `summary` | str | 是 | 一句话职责，供 LLM 选择工具 |
| `parameters` | dict | 是 | 参数 schema（名/类型/必填/默认），**与真实签名逐项一致** |
| `returns` | str | 是 | 返回对象的**会话句柄类型**名，如 `CandidateList` |
| `requires_handles` | tuple | 是 | 本工具需要的输入句柄类型 |
| `produces_handle` | bool | 是 | 调用后是否产出可复用句柄 |

**ToolResult**（一次工具调用的结果）：

| 字段 | 取值 | 说明 |
|---|---|---|
| `tool` | str | 被调用的工具名 |
| `status` | `ok` / `rejected` / `failed` | `rejected` 表示被边界守卫拦下（越权），`failed` 表示算法层自身报错 |
| `handle` | str \| None | 产出对象的会话句柄；无可复用时为 `None` |
| `summary` | dict | 结果的结构化摘要（**不含**确证分区内容） |
| `reason` | str \| None | `rejected` / `failed` 时的原因码 |

**会话句柄（handle）机制**：算法层多数函数接收**富对象**（`Candidate`、`ExplorationSample`、`KnowledgeBase`、`Evaluation`），无法经 JSON 直接传参。M10 因此为每个产出对象分配**不透明会话句柄**（形如 `h_<hex>`），LLM 只能凭句柄引用对象，从而：

1. 不必、也不能构造伪造的算法层内部对象；
2. 句柄仅在本决策会话内有效，不可跨会话传递；
3. 句柄是**不透明数据引用**——与 M1 的 `historical_ref` 同理，**不带类型前缀语义**，不可解析。

**约束**：M10 只做「登记 + 派发 + 边界守卫」，**不替 LLM 决定调用顺序**（`TOOLBOX_DECIDES_NOT_LLM = False`），不新增统计能力，不授予证据等级。

**调用者须知（参数名以真实签名为准，不得凭直觉推断）**：工具参数名由被封装函数的 `inspect.signature` 派生，个别名字与直觉不同，写调用前应先 `describe(name)` 查 schema。已知易错点：

| 工具 | 易错 | 正确 |
|---|---|---|
| `m03.fit_relation` | 以为拟合形式的参数叫 `ast` | 参数名是 **`relationship`**，可传 Ast 句柄或表达式文本 |
| `m03.prepare_sample` | 只传 `protocol_id` | 必填 **`variables`（列表）与 `target`**，否则被 `missing_argument` 拒 |
| `m03.baseline_linear` | 以为自动选特征 | 必填 **`feature`**（如 `"X1"`） |
| `m05.novelty` | 以为要 M8 的 `KnowledgeVersion` | 需 **M5 的 `KnowledgeBase`**（新颖度口径），两者不可互换 |

### 2.9 EndpointConfig / DriverTrace（LLM 驱动层，M11 产出）

**EndpointConfig**（LLM 连接配置，**只从环境变量或显式注入读取，绝不落盘、绝不入日志**）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `base_url` | str | 是 | OpenAI 兼容接口的基地址（如 `https://.../v1`） |
| `model` | str | 是 | 模型名 |
| `api_key` | str | 否 | 密钥。**只存在于内存**；`to_dict()` 中一律以 `"***"` 掩码，且不参与 `content_digest()` |
| `timeout` | float | 否 | 单次请求超时秒数，缺省 60 |
| `max_retries` | int | 否 | 网络失败重试次数，缺省 2 |

**ToolCall**（LLM 请求执行一次工具调用）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 工具名（须在 M10 目录内，否则 `unknown_tool`） |
| `arguments` | dict | 传给工具的具名参数；句柄以 `h_<hex>` 字符串形式给出 |

**DriverStep**（一轮「LLM 提议 → 工具执行 → 结果回喂」的记录）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `round_index` | int | 从 1 开始的轮次 |
| `assistant_text` | str \| None | LLM 本轮的自然语言说明（**不作为证据**） |
| `tool_calls` | tuple[ToolCall] | 本轮请求的工具调用 |
| `results` | tuple | 每个调用的 `ToolResult` |
| `stop_reason` | str \| None | 触发停止的原因码（见下） |

**停止原因码**（`DriverStopReason`）：`budget_exhausted`（轮次/调用预算用尽）｜`no_tool_call`（LLM 不再请求工具，视为完成）｜`llm_error`（endpoint 报错且重试耗尽）｜`max_rounds`（达到轮次上限）｜`fatal`（不可恢复错误）。

**DriverSession**（一次完整驱动会话结果）：`to_dict()` / `canonical_json()` / `content_digest()`；含 `steps`、`final_text`、`stop_reason`、`toolbox_digest`（所用工具目录的内容指纹，保证可追溯）。**DriverSession 不含任何证据等级字段。**

### 2.10 SandboxVerdict / SandboxRun（隔离代码执行，M12 产出）

**代码检查的判定层次**（框架 §2「须经检查」）：静态检查（AST）→ 若通过则隔离执行。

**SandboxVerdict**（静态检查结论）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `allowed` | bool | 是否允许执行 |
| `violations` | tuple[dict] | 每条含 `rule`（规则名）、`detail`、`lineno` |
| `checked_rules` | tuple[str] | 本次实际启用的规则名，便于审计 |

**SandboxRun**（隔离执行结果）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `ok` / `rejected` / `failed` / `timeout` | `rejected` = 静态检查未过；`failed` = 运行期报错 |
| `verdict` | SandboxVerdict | 静态检查结论 |
| `value` | Any | 受限序列化后的返回值（仅允许 JSON 基础类型） |
| `stdout` | str | 捕获的输出（长度受限） |
| `error` | str \| None | 异常类型与消息（截断） |
| `duration_ms` | float | 实际耗时 |

**隔离手段（全部必须齐备，缺一不可）**：

1. **AST 白名单**：只允许 `math`、`statistics`、`json`、`itertools`、`functools`、`collections`、`operator`、`decimal`、`fractions`、`random`（确定性种子）等纯计算模块；**禁止** `os`、`sys`、`subprocess`、`socket`、`urllib`、`importlib`、`ctypes`、`pickle`、`shutil`、`pathlib`、`open`、`eval`、`exec`、`compile`、`__import__`、`globals`、`locals`、`getattr`（对 dunder）、`vars` 等。
2. **受限内置命名空间**：不提供 `open`/`__import__`/`eval`/`exec`/`compile`/`input`/`breakpoint`。
3. **资源限额**：最大指令数（trace 计数）、墙钟超时、递归深度、输出长度。
4. **无 M1 访问**：M12 **不导入** `sdl_m01`，**不接受**任何 M1 令牌或数据引用；它只做纯计算，输入输出均为 JSON 基础类型。

## 3. 与 M1 的对接点

| 用途 | M1 接口 | 角色 |
|---|---|---|
| 建立证据库 | `initialize(db_path)` | — |
| 登记设计与原始资料 | `build(spec, records)` | custodian |
| 读取探索/开发资料 | `read_dataset(ref)` | explorer |
| 查看质量报告 | `quality(ref)` | explorer（E/V）｜custodian/auditor（受限） |
| 冻结并绑定 | `bind_confirmation(ref, plan)` | confirmer |
| 消费确证数据 | `consume_confirmation(binding_id)` | confirmer |
| 记录外部评估 | `record_evaluation(binding_id, result)` | confirmer |
| 发布结果 | `release_results(binding_id)` | confirmer |
| 归档 | `archive_confirmation(binding_id)` | custodian |
| **自主取数登记** | `add_confirmation(protocol_id, records, purpose)` | custodian |

标注「自主取数登记」的一行是 M9 的唯一入湖路径：M9 自身**不直连存储**，取数经注入的 `source`、登记经注入的 `registrar`（鸭子类型，实现 `add_confirmation` 即可）。因此 M9 在模块层不导入 `sdl_m01`。

## 4. 禁止事项

1. 不得修改 `sdl_m01/` 下任何文件。
2. 不得绕过令牌直接访问数据库或原始资料。
3. 不得在冻结前读取确证分区内容。
4. 不得把令牌、受限质量报告写入日志、提示词或版本库。
5. 不得用 LLM 文本合理性替代统计证据、授予证据等级。
6. M9 不得替 M7 重排建议顺序、不得授予证据等级、不得执行统计检验；不得用「0 条记录」冒充「已经取过」。
7. **M10 不得替 LLM 决策、不得新增统计能力、不得授予证据等级。** 它只登记与派发 M2–M9 的既有公开接口；LLM 经 M10 产出的一切仍须通过各层机械闸门。M10 不得让 LLM 直接构造算法层内部对象（必须经会话句柄）；不得把确证分区内容带出（`_reject_sealed_leak`）。
8. **M11 不得授予证据等级、不得新增算法、不得绕过 M10 直接调用算法层。** LLM 的一切工具调用必须经 `Toolbox.call()`（从而受四类护栏与封存泄漏检查约束）；M11 不得把 LLM 的自然语言当证据；不得让 endpoint 凭据进入日志、提示词或版本库；不得让 LLM 看到确证分区内容。M11 自身不做统计裁决。
9. **M12 不得访问 M1、不得触网、不得触碰文件系统与进程。** 它只做纯计算；不得导入 `sdl_m01`；不得接受令牌或数据引用；静态检查失败的代码**一律不得执行**（不得「先试跑再说」）；不得因 `try/except` 而放行违规代码。
