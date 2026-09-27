# 模块间接口契约（M2–M9）

版本：v0.2｜状态：草案（P00 交付物，P17 补充 M9）

## 0. 总则

1. 本文档定义 M2–M9 之间的数据对象与调用方向。**所有模块均不得修改 `sdl_m01/`**，只调用其公开接口。
2. M2–M9 一律使用 Python 3.11+ 标准库，不引入第三方依赖。
3. 数据访问必须经由 M1 的令牌角色：探索侧仅使用 `explorer`，确证侧仅使用 `confirmer`，保管侧使用 `custodian`。
4. 任何模块都不得读取、打印或持久化角色令牌正文。

## 1. 调用方向

```
M2 表示构造 ──> M3 模式搜索 ──> M4 假说构造 ──> M5 评估筛选 ──> M6 冻结确证
                    │                                  │              │
                    └──────────────> M7 主动取证 <──────┘              │
                                              M8 归档 <────────────────┘
                                                    │
                                          M9 自主取数 <── M7（建议）
```

M7 服务于下一轮取证，M8 保存历史并把可用信息送回探索。

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
