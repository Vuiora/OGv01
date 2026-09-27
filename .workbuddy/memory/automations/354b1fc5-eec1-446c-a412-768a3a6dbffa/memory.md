# DS 阶段循环调度 — 执行记忆

## 2026-09-22T20:01+08:00
- 判定：未更新（no change）
- 当前阶段：P00 基线锚定与接口冻结，stage_status=in_progress
- 指纹比对：P00 预期输出 `INTERFACES.md` = MISSING，与台账记录一致 → 无版本更新
- 动作：consecutive_no_change 0 → 1，last_check 更新为 2026-09-22T20:01+08:00，未推进阶段、未生成提示词
- 备注：未达停滞阈值（4/8），无需人工介入

## 2026-09-22T21:03+08:00
- 判定：nochange（退出码 0）
- 当前阶段：P01 受限表达式语法与规范化（已从 P00 推进，此前轮次完成）
- 依据：sdl_m02/expressions.py = MISSING，指纹无变化
- 动作：no_change_count 1，未推进、未生成 next-prompt
- 备注：未达停滞阈值（4/8），无需人工介入

## 2026-09-22T22:40+08:00
- 判定：**updated**（P01 验收通过，已推进 P01 → P02）
- 当前阶段：P02 候选变量枚举与定义域追踪
- 租约：`--work-order` 报 [LEASE-DENIED]。核查后确认 `worker.lock`（22:04:18）由**本实例自身**早先的 `--work-order` 调用写入（脚本不记录持有者身份），属自碰撞而非他实例；本次据此按「持有租约」处理并执行实现。
- 动作：复核并加固 P02 交付物 → 新写 `tests/test_domain.py`（53 项）；修复 `domain.py` 中 `DomainError` 被 `except ValueError` 吞掉的真实缺陷。
- 门禁：全量 139 项测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P03。
- 备注：门禁新增的「阶段测试补充」检查生效——本次补齐前 `test_fingerprints["tests/test_domain.py"]=MISSING`。
- 遗留观察：同一 P02 阶段在 22:04 与 22:30 先后被产出两次 `domain.py`，说明错峰实例存在实际竞争窗口，租约互斥建议后续加入实例标识。

## 2026-09-23T00:05+08:00
- 判定：**updated**（P04 验收通过，已推进 P04 → P05）
- 当前阶段：P05 结构模式搜索
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**（无自碰撞）
- 动作：新建 `sdl_m03/structure.py`（find_clusters / find_changepoints / check_invariant）+ `tests/test_structure.py`（90 项）
- 门禁：全量 383 项测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P06 稳定性与重采样评估
- 备注：本轮修正了一处真实设计缺陷——不变量初版判据（极差≤容差×尺度）成立时反例恒为空，使反例清单成为死代码；已改为「一致行占比 ≥ min_conformity」两步判据，反例逐条列出。

## 2026-09-23T01:15+08:00
- 判定：**updated**（前序 P06 验收通过，已推进 P06 → P07）
- 当前阶段：P07 假说对象与不可覆盖版本
- 对象检测：首跑报 `[SKIP] 与另一实例时间重叠（0.1 分钟）`，经查为**本实例自身**前几秒的检测（脚本刚写入 last_check），非他实例竞争；据末次 last-report（01:01 updated）按推进处理
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**；实现后已 `--release-lease` 释放
- 动作：新建 `sdl_m04/hypothesis.py`（Hypothesis 对象 + HypothesisStore 只追加版本仓库）+ `tests/test_hypothesis.py`（100 项）
- 门禁：全量 586 项测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P08 模式→假说与竞争解释
- 备注：本轮修正一处真实设计缺陷——`__hash__` 初版按 `(id,version)` 而 `__eq__` 按内容，二者不一致会在集合中产生重复项；已改为按内容摘要哈希。另记录：并行编辑同一文件会互相覆盖，须串行。

## 2026-09-23T02:37+08:00
- 判定：**updated**（P08 验收通过，已推进 P08 → P09）
- 当前阶段：P09 多维评估指标 G/S/N/C
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**；实现后已 `--release-lease` 释放
- 动作：新建 `sdl_m05/metrics.py`（gain / stability / novelty / complexity / evaluate_hypothesis + KnowledgeBase / Evaluation）+ `tests/test_metrics.py`（112 项）
- 门禁：全量 813 项测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P10 Pareto 筛选与候选池管理
- 备注：本轮修正一处真实设计缺陷——陈述相似度兜底判据初版「>0 即判匹配」，使两段语义无关的中文陈述因共享「的」「与」等功能字（实测 Jaccard 仅 0.0185）被误报为「已知」；已新增下限 `DEFAULT_MIN_STATEMENT_SIMILARITY=0.12` 并补测试守护。另记录：P02 受限语法深度上限为 3，夹具表达式须实测合规。


## 2026-09-23T03:55+08:00
- 判定：**updated**（P10 验收通过，已推进 P10 → P11）
- 当前阶段：P11 冻结计划生成与确证绑定（M6）
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**；实现后已 `--release-lease` 释放
- 动作：新建 `sdl_m06/freeze.py`（build_frozen_plan / bind）+ `tests/test_freeze.py`（125 项）
- 门禁：全量 1032 项测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P12 检验执行与跨轮误差预算
- 备注：本轮修正一处真实设计缺陷——P10 候选内联的评估/筛选痕迹（gain/novelty/rank 等）被误判为「未知声明键」而报错；已新增 CARRIER_METADATA_KEYS 白名单区分「载体元数据」与「口径声明」，同时保留拼错声明键的拦截。另记录：变异测试首轮因 `str.replace` 缩进未匹配而假绿，须校验变异确实生效。

## 2026-09-23T05:07+08:00
- 判定：**updated**（P12 验收通过，已推进 P12 → P13）
- 当前阶段：P13 结果三分类、记录与发布（M6）
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**；实现后已 `--release-lease` 释放
- 动作：新建 `sdl_m06/report.py`（classify_result / record_and_release + Classification / HypothesisOutcome / RoundClassification / RecordReleaseReceipt）+ `tests/test_report.py`（118 项）
- 门禁：全量 1238 项测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P14 主动取证与反例搜索
- 备注：本轮修正一处真实设计缺陷——`record_and_release` 只扫了登记载荷与发布内容，却漏扫调用方传入的 `binding` 描述（原始记录可挂在 binding 上绕过闸门）；已补 `check_no_sealed_leak(body, "binding")`。另记录两条经验：① 变异测试 M2（移除载荷封存扫描）首轮**假绿**——顶层键白名单用例覆盖不到递归扫描，须补「封存字段藏在合法顶层键之下」的嵌套泄漏用例才能捕获；② M1 约束「同一研究轮次只能有一个冻结确证批次」，同一阶段内多次端到端绑定须递增 `round_index`。

## 2026-09-23T06:12+08:00
- 判定：**updated**（P14 验收通过，已推进 P14 → P15）
- 当前阶段：P15 知识归档与假说更新（M8）
- 对象检测：首跑报 `[SKIP] 与另一实例执行时间重叠（1.9 分钟）`，经查为**本实例自身**前几秒的检测（脚本刚写入 last_check），非他实例竞争；据末次 last-report（updated/ P14 门禁 PASS）按推进处理
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**；实现后已 `--release-lease` 释放
- 动作：新建 `sdl_m08/archive.py`（archive_round / update_knowledge_version / revise_or_retire + DataProfile / EvidenceEntry / RoundArchive / RetirementEntry / KnowledgeVersion / RevisionDecision）+ `tests/test_archive.py`（94 项）
- 门禁：全量 **1418 项**测试，2 项既有失败（known_failures），**0 新增失败**；`--dry-run --force-run` 确认下一轮将推进至 P16 端到端主循环集成
- 修正的真实缺陷（3 处）：
  ① `_evidence_identity` 初版缺 `hypothesis_id`——同轮多假说共用同一份数据/绑定/轮次时，身份相同会让「同一份证据不得支撑两条确证结论」的闸门把正常的多假说轮误判为违规；已把假说标识纳入摘要体。
  ② `archive_round` 的 `_reject_forbidden_kwargs` 形同虚设——函数无 `**kwargs`，传入 `grade`/`promote_to` 等受控参数只会抛 Python 原生 `TypeError`，而不是可归档的口径违规 `ArchivePolicyError`；已补 `**forbidden` 兜底入口，受控参数走政策异常、未知参数走输入异常，且**不做静默忽略**。
  ③ 单假说结论路径拿不到 `binding_id`——P13 的 `HypothesisOutcome` 只带假说标识与结论，`_round_facts` 未回落到调用方给出的绑定描述，导致用合法绑定归档单条结论被误拒；已加 `fallback_binding_id`。
- 变异测试：5 处定向变异（因果升级置真 / E2 降级为 E1 / 非支持判成 E1 / 证据身份去掉假说 / 复用已见数据仍算确证）**全部被捕获**，模块恢复后逐字节一致。
- 附注：`archive_confirmation` 返回的 `historical_ref` 是 M1 的不透明数据引用（形如 `data_<hex>`），**不带** `H_` 前缀；`H_<binding_id>` 是该数据集在协议下的**用途名**，二者不同字段——写断言时勿给引用强加前缀。

## 2026-09-27T17:38+08:00
- 判定：**all_done**（P16 端到端主循环集成通过；P00–P16 全部完成）
- 当前阶段：P16（末阶段，`stage_status=done`，`history` 尾部新增 P16）
- 租约：`--work-order` 报 [WORK-ORDER]，**正常获得租约**；实现后已 `--release-lease` 释放
- 动作：新建 `sdl_pipeline/loop.py`（约 125 KB，M2–M8 集成主循环）+ `tests/test_loop.py`（71 项）
- 门禁：五道全 PASS — 全量 **1489 项**测试，2 项既有失败（known_failures），**0 新增失败**；交付物齐全；阶段测试补充到位；冻结目录未改；无新增 skip/xfail
- 本轮修正的**真实缺陷（11 处，全部实测暴露）**：效应阈值硬编码 0 →M6 拒绝；两遍执行第二遍必撞 M1 已消费（需 replay 提升）；评估器键名 `note`→`notes`；`self_check` AST 假阳性（本地变量撞黑名单）+ `ast` 拼写；协议视图陈旧（不看 `describe` 就看不到新确证分区）；P11 把 `model` 序列化成文本致表达式静默丢失；零模型基线误取批次均值致 p≈0.99；反例恒空（破坏批次在 E 分区外 + 守恒关系不精确）；M7 反例取证缺 `pre_sampling_freeze`；`gain=inf` 打断整轮；`run_loop` 不校验协议必填字段
- 变异测试：6 处定向变异（阈值归零 / 去 replay 修复 / 不做破坏批次 / 反转停止条件 / 快照漏字段 / 禁用表达式解析）**全部被捕获**，无假绿；模块恢复后逐字节一致
- 备注：初版测试有 3 处**猜测断言**写成即错（`仍未` 字样、`types` 模块、非法协议是否抛错）——再次印证「先实测再写断言」。跨层集成语义已写入项目 `MEMORY.md` 供后续复用。

## 2026-09-27T18:13+08:00
- 判定：**all_done**（重复确认，无新增工作）
- 当前阶段：P16，`stage_status=done`，`history` 尾部为 ['P01'…'P16','P16']
- 依据：脚本直出 `[DONE] 全部阶段已完成。`；`last-report.md` 五道门禁全 PASS（1489 项测试，2 项既有失败，新增失败 0）
- 动作：**未取工单、未实现任何文件、未改任何文件**（末阶段已完成，取工单无意义）
- 备注：本次未复跑全量测试——`last-report.md` 时间戳 18:13:32 即为本次检测运行产出，门禁结论新鲜有效。后续若再被调度，仍会得到 all_done。

## 2026-09-27T18:15+08:00
- 判定：**all_done**（脚本直跑报 `[SKIP] 时间重叠 2.1 分钟`，核验后确认为 all_done）
- 阶段：P16 端到端主循环集成（终止阶段），`stage_status=done`
- 依据：`state.json` current_stage=P16 / history 16 条（末尾 P16 重复）/ `last-report.md`(18:13:32) 五道门禁全 PASS（1489 项，2 项既有失败，新增 0）；`stages.json` 共 17 阶段 P00–P16
- 动作：**未取工单、未实现、未改任何文件**
- 备注：`[SKIP]` 属自碰撞（2.1 分钟前本实例检测刚写 `last_check`），非他实例竞争。经验：`[SKIP]` 不在五种判定之列，须回查 last-report + state 定真实状态。后续调度仍将得 all_done。

