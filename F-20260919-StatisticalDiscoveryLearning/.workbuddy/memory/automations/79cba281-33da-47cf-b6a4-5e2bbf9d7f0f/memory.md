# 自动化执行记忆 · DS 阶段循环调度（每30分钟检查·错峰）

任务 ID：`79cba281-33da-47cf-b6a4-5e2bbf9d7f0f`｜触发：每小时 :28
协同任务：`354b1fc5-eec1-446c-a412-768a3a6dbffa`（每小时 :58），二者共用 `DS循环进度.md`

## 执行历史（简）

### 2026-09-22 20:03
- 判定：**跳过（与另一实例执行时间重叠）**。
- 依据：台账 `last_check = 2026-09-22T20:01+08:00`，与本次执行时刻（20:03）相差仅 2 分钟，落入 10 分钟去重窗口。
- 动作：未做版本检测、未做验收、**未修改台账**、未生成提示词。
- 台账现状（只读）：current_stage = P00，stage_status = in_progress，consecutive_no_change = 1，交付物 INTERFACES.md 记为 MISSING。
- 备注：本次为该错峰实例的首条记录（记忆文件首次创建）。

### 2026-09-22 21:04
- 判定：**nochange**（P01 受限表达式语法与规范化，连续未变更 = 1）。
- 依据：交付物 `sdl_m02/expressions.py` 指纹 MISSING，无版本更新。
- 动作：未推进阶段；已由脚本写入 last-report.md / loop-log.jsonl / state.json。无提示词生成。退出码 0。
- 台账现状（只读）：current_stage = P01，stage_status = in_progress，baseline_started_at = 20:16:22，prev_handoff = null。
- 备注：紧接着的一次重跑返回 `[SKIP] 重叠 2.5 分钟`，符合 10 分钟去重窗口预期，属正常现象。

### 2026-09-22 22:42
- 判定：**updated**（P02 验收通过 → 推进 P03）。
- 依据：`sdl_m02/domain.py` 指纹 `a9c7b729a9c9 → b5f706e080e9`；门禁 5 项全 PASS（139 tests，已知失败 2、新增 0）。
- 动作（第一段）：仅检测与推进，未实现增量。
- 第二段：`--work-order` 取得 **P03** 租约并开始撰写 `sdl_m02/represent.py`。
- **结果：本次未产出 P03 交付物。** 撰写期间另一错峰实例已落地该文件，工具层以「文件已存在」拒绝覆盖（正确的互斥保护）。判定为 **overlap-by-delivery**，已停止并改为人工复核。

### 2026-09-22 23:36
- 判定：入口为 **updated**（P02 → P03），随后 **P03 由另一实例在 23:16 完成并通过门禁 → P04**。
- 关键事实：P03 交付物 `sdl_m02/represent.py`（指纹 `437f63d5978e`）与 `tests/test_represent.py`（52 项全绿）**由协同实例产出**，本实例未修改任何文件。
- 本实例产出：取得 **P04** 租约，落地 `sdl_m03/equations.py` + `tests/test_equations.py`（102 项全绿）。
- 测试：全量 **293 项，2 项既有失败（known_failures），新增失败 0**；`sdl_m01/` mtime 仍为 09-19，冻结保护未被触碰。
- 门禁（只读调用脚本内部函数复核）：阶段测试补充 ✓｜禁止模式 AST 检查 ✓｜交付物齐全 ✓。

## 注意事项
- 写入台账前必须先读 `last_check`；10 分钟内的重复触发一律跳过。
- 验收门禁命令：`python -m unittest discover -s tests -v`。
- 排查提示：用 PowerShell 工具运行本脚本时 stdout 可能被丢弃，改用 Bash 工具可稳定拿到输出与退出码。
- **必须实测确认「轮到我的到底是哪个阶段」**：脚本的 `--work-order` 输出只反映调用瞬间的 state；若在撰写期间协同实例完成验收并推进，该工单即已过期。**动笔前先 `ls` 目标文件、`cat state.json` 确认 current_stage**，避免覆盖对方交付物。
- **不要为防重叠加参数**：脚本内置 10 分钟去重 + worker.lock 工作租约。工具层拒绝写入已存在的交付物即是保护生效；此时应停止并上报，不要强行改写。
- **夹具必须实测**（P06 教训）：写「X 簇/Y 个变点」类测试前，先用诊断脚本打印检测器在夹具上的**真实输出**（k 是多少、分数多少），再据实写断言。簇内点云要各向同性，不要留单调方向——否则 k-means 会把簇切碎并拿到更高分离度。
- **边界守护断言一律用 AST**，不要用源码文本匹配：文档里的「不做 X」声明会被文本匹配误判为违规（已累计踩坑 4 次）。

### 2026-09-23 00:38
- 判定：**updated**（P05 结构模式搜索验收通过 → 推进 P06 稳定性与重采样评估）。
- 门禁：486 项测试、已知失败 2（M01 基线）、新增失败 0；交付物齐全；冻结目录未改动；无新增 skip/xfail。
- 动作：`--work-order` 取得 **P06 租约**并完成实现，落地 `sdl_m03/stability.py` + `tests/test_stability.py`（103 项全绿）。
- 本实例产出：`resample_evaluate` / `stability_score` / `resolve_split_unit` / `group_key_of` / `StabilityReport`；三条验收标准（分组级重采样、分数 [0,1]、seed 可复现）均有专项测试。
- 测试：全量 **486 项，2 项既有失败，新增失败 0**；`sdl_m01/` mtime 仍为 09-19，冻结保护未被触碰。
- 备注：本文件此前最后一条记录为 09-22 23:36（P04）；期间 23:41–00:05 的 P05 推进由协同实例完成。

### 2026-09-23 02:10
- 判定：**updated**（P07 假说对象与不可覆盖版本验收通过 → 推进 P08 模式到假说与竞争解释）。
- 门禁：701 项测试、已知失败 2（M01 基线）、新增失败 0；交付物齐全；冻结目录未改动；无新增 skip/xfail。
- 动作：`--work-order` 取得 **P08 租约**并完成实现，落地 `sdl_m04/generate.py` + `tests/test_generate.py`（115 项全绿）。
- 本实例产出：`hypotheses_from_patterns` / `attach_nulls` / `attach_alternatives` / `GenerateBudget` / `GeneratedHypothesisPool`；三条验收标准（可证伪 predictions、至少一零假说、池大小受契约约束）均有专项测试。
- 契约判定要点：`INTERFACES.md` §2 只约束池内条目**字段形状**、未给数量上界，故池上界锚定框架说明 §2 原型配置「每轮保留 50 个探索候选」并使其可配置。
- 测试：全量 **701 项，2 项既有失败，新增失败 0**；`sdl_m01/` mtime 仍为 09-19，冻结保护未被触碰。
- 备注：本文件此前最后一条记录为 09-23 00:38（P06）；P07 的推进与实现由协同实例完成。

### 2026-09-23 03:19
- 判定：**updated**（P09 多维评估指标 G/S/N/C 验收通过 → 推进 P10 Pareto 筛选与候选池管理）。
- 门禁：**907 项测试、已知失败 2（M01 基线）、新增失败 0**；交付物齐全；冻结目录未改动；无新增 skip/xfail。
- 动作：`--work-order` 取得 **P10 租约**并完成实现，落地 `sdl_m05/select.py` + `tests/test_select.py`（94 项全绿）。
- 本实例产出：`pareto_front` / `pareto_layers` / `top_k_reserved` / `select_freeze_candidates` / `dominates` / `is_better` / `compare_pair` / `direction_of`；结果对象 `ParetoFront` / `ReserveSelection` / `FreezeSelection` / `SelectedCandidate` / `DominancePair`。
- 三条验收标准实现口径：
  ① 方向写入冻结常量 `OPTIMIZATION_DIRECTIONS`（G/S/N maximize、C minimize），三个入口均无方向覆盖参数；
  ② `max_candidates` 默认 `DEFAULT_FREEZE_CAP=5`，可配置区间 1..`FREEZE_CAP_HARD_LIMIT`(50，锚定框架 §2)，前沿不足时如实记 `shortfall` 而非用低层候选凑数；
  ③ 用**轮转式预约**（`RESERVE_ROTATION`）替代加权总分，`FORBIDDEN_SCORE_KEYS` + `weight`/`score` 子串兜底拒绝一切总分入口，`CONFIRMATION_USES_NOVELTY_BONUS` 为字面 False 常量且无改写路径。
- 关键设计判断：**缺维度（None）候选不插补为 0**，不参与支配比较、不进前沿，单列 `incomplete`；只有显式 `allow_incomplete` 才可入选且恒排末位。
- 测试质量保障：做了 **8 组变异测试**（方向反转／默认上限改 3／支配定义放宽／缺维度插补为 0／上限失效／强行按 novelty 单维排序／跨版本不拒绝／权重参数放行），**全部被测试捕获**，确认断言非空洞。
- 备注：本文件此前最后一条记录为 09-23 02:10（P08）；P09 的推进与实现由协同实例完成。

### 2026-09-23 04:35
- 判定：**updated**（P11 冻结计划生成与确证绑定验收通过 → 推进 P12 检验执行与跨轮误差预算）。
- 门禁：**1120 项测试、已知失败 2（M01 基线）、新增失败 0**；交付物齐全；冻结目录未改动；无新增 skip/xfail。
- 动作：`--work-order` 取得 **P12 租约**并完成实现，落地 `sdl_m06/execute.py` + `tests/test_execute.py`（**88 项全绿**）。
- 本实例产出：`alpha_for_round`（= `math.ldexp(α,-t)`，签名无替代口径参数）/ `holm_adjust`（标准 step-down + 单调化，空输入返回空）/ `execute_family` / `family_test_ids` / `check_family_coherence` / `summarize_family` / `self_check`；结果对象 `ExecutionOrder` / `HolmEntry` / `HolmResult` / `TestExecution` / `FamilyExecution`。
- 四条验收口径：① α_t 严格为 total_alpha/2**t，下溢即拒；② 消费顺序由 `_StepLedger` **实际步进**测量（非构造常量），并有多处反向用例；③ 族用 P11 `family_coverage` 双向严格对账，无轨迹时逐条占位，「没算」与「不显著」可区分；④ Holm 对空输入返回空、`None` 不插补。
- **关键设计修正**：α 的数值来源从「计划字段」改为「公开协议 `confirmation_policy.total_alpha`」——计划里塞非契约字段会破坏 P11 的 13 字段逐项对应不变量。轮次三方 + α 两方对账，任一不符即 `SequenceOrderError`。
- 测试质量：**11 组定向变异全部被捕获（0 空洞）**；首轮 2 组空洞（严格小于边界、去掉离线自检）已诊断并补测。变异脚本放仓库外 `/tmp/sdl-p12/mutation_p12.py`（本轮只许动交付物）。
- 备注：本文件此前最后一条记录为 09-23 03:19（P10）；P11 的推进与实现由协同实例完成。

### 2026-09-23 06:08
- 判定：**updated**（P13 结果三分类验收通过 → 推进 P14 主动取证与反例搜索）。
- 门禁：**1324 项测试、已知失败 2（M01 基线）、新增失败 0**；交付物齐全；冻结目录未改动（`sdl_m01/` mtime 仍 09-19）；无新增 skip/xfail。
- 动作：`--work-order` 取得 **P14 租约**并完成实现，落地 `sdl_m07/acquisition.py`（新建包）+ `tests/test_acquisition.py`（**86 项全绿**）。
- 本实例产出：`divergence` / `acquisition_plan` / `self_check`；结果对象 `ObservationDivergence` / `DivergenceReport` / `AcquisitionItem` / `AcquisitionPlan`。
- 三条验收口径：① 排序是**单一比值**「分歧 ÷ 采样成本」+ 常量 `RANKING_BASIS_HEURISTIC`，结果对象恒输出启发式声明与 `is_expected_information_gain=False`；EIG 类命名/入参双层拒绝；② 确证类目的 `{confirmation, counterexample}` 强制 `pre_sampling_freeze`（标记须字面 True、候选与方案须非空），**无关闭参数**；③ 计划恒为建议对象、无采集入口（AST 自检）。
- 关键设计：新增 `_reject_unknown_kwargs`，把**任何未声明参数**也一并拒绝——静默忽略会让调用方误以为 `require_freeze=False` 生效。边界接近度**只作平局裁决**不参与加权。
- 测试质量：**13 组定向变异全部被捕获（0 空洞）**；首轮 1 组变异属语义等价（已改为真实缺陷形态）。变异脚本放仓库外临时目录，本轮只动交付物。
- 备注：本文件此前最后一条记录为 09-23 04:35（P12）；P13 的推进与实现由协同实例完成。

### 2026-09-27 16:56
- 判定：**overlap**（与另一错峰实例执行时间重叠 0.2 分钟），本次安静退出。
- 依据：脚本返回 `[SKIP] 与另一实例执行时间重叠（0.2 分钟）`；只读核对 `last-report.md` 显示协同实例已于 16:56:08 完成检测、判定 nochange。
- 动作：未做验收、未取工单、**未实现任何增量、未修改任何文件**（含台账）。
- 台账现状（只读）：current_stage = P16「端到端主循环集成」，stage_status = in_progress，no_change_count = 3，交付物 `sdl_pipeline/loop.py` 记为 MISSING。
- 备注：本文件此前最后一条记录为 09-23 06:08（P14）；P15 的推进与实现由协同实例完成。P16 尚未有任何实例落地交付物。

### 2026-09-27 18:13
- 判定：**all_done**（全部 16 个阶段 P00–P16 已完成，循环终止，不再调度后续轮次）。
- 依据：脚本返回 `[DONE] 全部阶段已完成。`；`last-report.md`（18:13:32）判定 all_done；P16 交付物 `sdl_pipeline/loop.py` 指纹由 MISSING → `6d74a8597d62`。
- 门禁：**1489 项测试、已知失败 2（M01 基线）、新增失败 0**；交付物齐全；冻结目录未改动（`sdl_m01/` mtime 仍 09-19）；无新增 skip/xfail。
- 动作：**未调用 `--work-order`、未取租约、未实现任何增量、未修改任何文件**（含台账）。本次为纯上报。
- 台账现状（只读）：current_stage = P16，stage_status = **done**，no_change_count = 3，history 末两条均为 P16 done（17:38、18:13）。
- 备注：P16 的落地与验收由协同实例于 09-27 17:14–17:44 完成（`sdl_pipeline/loop.py` 124,880 字节、`tests/test_loop.py` 37,136 字节）。**本项目阶段循环至此收官**；后续若需继续，应新增阶段至 `stages.json` 并将 dispatch 开关交由人工决策。

### 2026-09-27 18:15
- 判定：**overlap**（与另一错峰实例执行时间重叠 2.2 分钟）→ 安静退出，本次不做事。
- 依据：脚本返回 `[SKIP] 与另一实例执行时间重叠（2.2 分钟），跳过。`；只读核对 `last-report.md`（18:13:32）为 **all_done**，P16 已通过（`sdl_pipeline/loop.py` 指纹 `6d74a8597d62`），门禁 1489 项测试、已知失败 2、新增失败 0。
- 动作：**未调用 `--work-order`、未取租约、未实现增量、未修改任何文件**（含台账）。
- 备注：去重窗口内正常跳过；项目已于 18:13 收官，后续触发预计亦为 all_done 或 overlap。

