# 模块间接口契约（M2–M8）

版本：v0.1｜状态：草案（P00 交付物）

## 0. 总则

1. 本文档定义 M2–M8 之间的数据对象与调用方向。**所有模块均不得修改 `sdl_m01/`**，只调用其公开接口。
2. M2–M8 一律使用 Python 3.11+ 标准库，不引入第三方依赖。
3. 数据访问必须经由 M1 的令牌角色：探索侧仅使用 `explorer`，确证侧仅使用 `confirmer`，保管侧使用 `custodian`。
4. 任何模块都不得读取、打印或持久化角色令牌正文。

## 1. 调用方向

```
M2 表示构造 ──> M3 模式搜索 ──> M4 假说构造 ──> M5 评估筛选 ──> M6 冻结确证
                    │                                  │              │
                    └──────────────> M7 主动取证 <──────┘              │
                                              M8 归档 <────────────────┘
```

M7 服务于下一轮取证，M8 保存历史并把可用信息送回探索。

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

## 4. 禁止事项

1. 不得修改 `sdl_m01/` 下任何文件。
2. 不得绕过令牌直接访问数据库或原始资料。
3. 不得在冻结前读取确证分区内容。
4. 不得把令牌、受限质量报告写入日志、提示词或版本库。
5. 不得用 LLM 文本合理性替代统计证据、授予证据等级。
