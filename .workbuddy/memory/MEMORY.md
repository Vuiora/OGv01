# SDL 项目长期记忆

本文件只记录**跨阶段复用**的规则与约定；逐阶段细节见 `.workbuddy/memory/YYYY-MM-DD.md`。

## 施工流程（分阶段循环）
- 检测：`python scripts/ds_stage_loop.py` → 读 `.workbuddy/ds-loop/last-report.md` 判定
  （updated / nochange / blocked / overlap / all_done）。
- 取工单：`python scripts/ds_stage_loop.py --work-order`；输出 `[LEASE-DENIED]` 即另一实例在做，立即停手。
- 门禁命令：`python -m unittest discover -s tests -v`。M01 有 **2 项既有失败**（`test_client_role_cannot_be_changed_to_bypass_authorization`、`test_failed_consumption_after_digest_check_remains_used_and_is_logged`），属放行项；**新增失败必须为 0**。
- 阶段配置在 `.workbuddy/ds-loop/stages.json`：每阶段声明 `outputs`（交付物）与 `tests`（本阶段测试文件，门禁校验其指纹相对基线必须变化）。**只许动这两个文件，多一个少一个都不行。**
- **动笔前先 `ls` 目标文件 + `cat state.json` 确认 current_stage**：工单反映调用瞬间状态，撰写期间协同实例可能已推进。
- 全部阶段完成后循环**幂等短路**（秒退、不重跑测试）。若发现「每次都报 updated 且跑满全量测试」，是 `output_fingerprints` 停在 `MISSING` 未回写所致（已于 09-27 修复）。
- **新增阶段（如 P18）的登记法**：① 向 `stages.json` 的 `stages` 追加配置；② 手工把 `state.json` 置为 `current_stage=<新阶段>`、`stage_status=in_progress`、`output_fingerprints`/`test_fingerprints` 置 `MISSING`、刷新 `baseline_epoch`——**这一步不可省**：若不重置，`stage_status` 仍是上一末阶段的 `done`，脚本走终态短路直接报 `all_done`，新阶段永远不会被检测。基线为 `MISSING` 时门禁天然视交付物为「本轮新增」（脚本第 279 行注释明确支持此路径）。③ 跑 `python scripts/ds_stage_loop.py --force-run --json`；④ 门禁通过后脚本自动回写真实指纹并置 `done`。
- **`last-report.md` 里「全部阶段已完成（P16 已通过）」这类文案已改为动态取 `{cur}`**（此前硬编码 P16，阶段推进后失真）。

## 门禁自身的匹配范围（元层缺陷，已连踩三例）
门禁是元层代码，匹配范围过宽会**自己把自己判失败**，症状是「阶段莫名 blocked」，极难定位。
- 三例共性：子串匹配 `skip/xfail`（命中边界断言）｜命中 `NOT_PROVIDED_BY_*` 常量｜`rglob("*")` 扫冻结目录命中 `__pycache__/*.pyc`（跑测试即刷新 mtime → 必判越界）。
- **冻结目录检查必须忽略非源码产物**：`__pycache__`/`.pytest_cache`/`.mypy_cache` 与 `*.pyc/*.pyo/*.pyd`（已实现 `_is_frozen_artifact`）。
- **通用自检法**：任何「扫描 X 判有害改动」的门禁，都要**两侧都测**——① 构造应被忽略的干扰项（构建产物/缓存），断言不触发；② 构造真实违规项，断言必被抓到。只测一侧可能修成空洞门禁。

## M1 归档与数据视图语义（跨阶段复用，写断言前必读）
- `archive_confirmation(binding_id) -> {"historical_ref": ...}`：仅当绑定已 `recorded`+`released` 时可归档，幂等。返回的 `historical_ref` 是**不透明数据引用**（形如 `data_<hex>`），**不带** `H_` 前缀——`H_<binding_id>` 是该数据集在协议下的**用途名**，要到 `describe(protocol_id)["resources"]` 里查，二者不是同一字段。别给引用强加前缀。
- 「已见」的判定基础是**内容级数据身份**：记录指纹**剔除 `record_id`**，所以换 `record_id` 重放同一批内容会被 M1 自己的闸门以「Previously registered content cannot be resealed」拒绝。想在**规则层**演练「复用已见数据」而不触发 M1 指纹闸门，应直接构造 `KnowledgeVersion` 调 `_grade_for`。
- M1 把**声明范围外**的环境裁掉：要测「新环境复现」，必须在数据配置 `task.environments` 里声明该环境（每环境单独建协议），否则记录被裁空。
- 同一研究轮次只能有一个冻结确证批次；同一阶段内多次端到端绑定须递增 `round_index`。

## 测试与断言规则（踩坑最多的两条）
1. **守护「不做 X」一律用 AST，绝不用源码文本匹配。** 已累计踩坑 **7 次**：docstring 与 `NOT_PROVIDED_BY_*` 常量里**必须**写出被禁名字（「本层不做 bind_confirmation」），文本匹配会把边界声明当违规。文本匹配只适用于检查「必须存在的声明」（如 `assertIn("不修改", text)`）。
2. **夹具必须先打印真实输出再写断言。** 三个具体教训：
   - **配额类测试**（上限/裁剪/k 个预留）夹具必须是**反链**——候选两两不可比。用单调链（后一个全面支配前一个）会让前沿恒为 1，测的是前沿规模而非配额。
   - **簇/变点类测试**要先用诊断脚本确认检测器的真实输出（k 是多少、分数多少），且簇内点云须各向同性，不留单调方向。
   - **夹具方法名不得撞 `unittest.TestCase` 的内部属性**（`_outcome` / `_feedErrorsToResult` 等）。命名为 `self._outcome` 会遮蔽 `TestCase._outcome`，报 `'_Outcome' object is not callable`，且报错点远离真因。
3. 写测试后建议做**定向变异测试**（故意反转方向、放宽定义、改默认值），确认断言能捕获——防止空洞断言。P17 实测 6/6 全部捕获。

## 跨层集成语义（P16 起复用，写集成/断言前必读）
- **M6 `execute_family` 内部 `replay=False` 是硬编码的。** 同一绑定第一次消费后分区迁 `used`；再消费必被 M1 以 `StateError`（「explicit replay is required」）拒绝。要在**同一次执行内**用外部评估器的产物重跑，必须在注入的 confirmer 转接器里把第二遍的 `replay` 提升为 `True`——这是 M1 的公开复核语义（`new_evidence=False`，不产生新证据、不改账本）。
- **P11 会把冻结计划的 `model` 序列化成文本**（`'coefficients={...}; expression=[...]; target=Y'`），且表达式是**规范化 JSON 字符串**。`from_ast` 只接受嵌套列表（元组/列表），字符串要先 `json.loads`。不解析就会把模型项**静默丢掉**，候选预测退化成常数预测（实测 p≈0.99）。
- **零模型（常数基线）必须取冻结截距**（即斜率置零的候选），不能取「每批次自身观测均值」——后者已看过该批全部数据，会把任何候选压成不显著，是假基线。
- **`effect_threshold` 必须为正**：M6 拒绝 `<= 0` 的计划（「最小有意义差异」）。缺省 0.1。
- **M7 的 `acquisition_plan` 对确证类目的（`confirmation`/`counterexample`）强制要求 `pre_sampling_freeze`**：必填 `frozen_candidates`（非空）、`frozen_protocol`（非空映射）、`freeze_before_sampling=True`（字面 True）。无「跳过」参数。
- **协议视图会随 `add_confirmation` 变**：不能只信启动时传入的 `resources` 快照，每轮须经 custodian 的公开 `describe` 刷新，否则「补了数据却报无可用确证数据」。
- **M1 分区切分会把批次分到 E/V/C**（60 组 → E 约 36 组）：放在分区外的「异常批次」在探索数据上**看不到**，反例清单会恒为空。测反例须让异常批次落在 E 内（本仓库合成数据缺省 `broken=(0,3)`）。
- 外部评估器返回的检验轨迹**键名必须在 M6 白名单内**（`p_value`/`effect`/`delta`/`notes`…；注意是 `notes` **不是** `note`），未知键一律拒绝。
- `evaluate_hypothesis` 可能因 `gain=inf` 抛 `MetricsInputError`：集成层须逐条 try/except 降级并入 `fit_skips`，**不得**让单个候选打断整轮。
- **AST 边界自检的假阳性来源**：本地变量名撞黑名单（如 `ledger = BudgetLedger()` 撞 M1 的账本入口名）。写 `self_check` 要收集本地绑定名（参数/赋值/自身定义）并剔除，仅比对真正的访问与 import。

## M9 自主取数语义（P17 起复用，写断言前必读）
- **M7/M9 是「建议层 / 执行层」补集**：`ACQUISITION_PLAN_EXECUTES_COLLECTION=False`（M7 只出建议）对 `COLLECTION_EXECUTES_COLLECTION=True`（M9 真取数）。M9 不授予等级、不跑统计。
- **闭环靠「上一轮」计划**：`AutonomousCollector.observe(round_index, plan)` 收当轮计划，`__call__(round_index, protocol_id)` 采的是 `round_index-1` 那一轮的计划。故**首轮必无建议**（如实记 `no_plan`），`trace()` 里 `driven_by_plan_round = round_index-1`。同轮 observe 的计划不影响同轮采样（不向后泄漏）。
- **六原因码必须分列**：`collected`｜`source_empty`（**采了为空**：设计齐备、真驱动了源、返回空）｜`no_mapping`/`no_plan`/`budget_exhausted`/`source_failed`（**没采**）。`NOT_COLLECTED_REASONS` 与「采了为空」互补。绝不用 0 条记录冒充「取过了」。
- **采样目录由调用方提供**：解析顺序 精确 `by_observation` → 按目的 `by_purpose` → 兜底 `default_design` → None（记 `no_mapping`）。M9 无法推断「某观测现实里怎么采」，绝不臆造设计。
- **`SyntheticSource` 每批用新号段**（`_allocate_start` 递增游标），否则重放同一批内容会被 M1 的内容级数据身份闸门拒绝。
- **`run_loop` 的 `acquisition_sink` 钩子**：只读、返回值被忽略、异常降级为一条 note 不打断循环；缺省 `None` 时行为与 P16 完全一致。**务必只调用一次**（放在 `round_records.append` 之前）。
- **`freeze_cap` 不能取 1**：M7 的 `divergence` 至少需要 2 个竞争假说，`freeze_cap=1` 时只冻结 1 个会抛 `AcquisitionInputError`。端到端最小可用预算：`max_candidates=60, freeze_cap=2, n_resamples=2`（两轮约 3.2s）。
- **测「规律提取」用轻量假对象**（只需 `version`/`entries` 与条目上的 `hypothesis_id`/`grade`/`status`/`round_index`/`evidence_identity` 等）；不要走 M8 构造器——其不变量极严（E2 须有先行确证、绑定与数据身份须与归档一致、等级与凭据须匹配）。

## M10 分析决策层语义（P18 起复用，写断言/扩展工具前必读）
- **M10 是「决策层」，M2–M9 是「算法层」**：M10 把既有公开接口封装为 LLM 可调用工具，**由 LLM 决定调用顺序与参数**，算法执行与校验。五个机检常量：`TOOLBOX_DECIDES_NOT_LLM=False`（决策归 LLM）、`TOOLBOX_ADDS_ALGORITHMS=False`、`TOOLBOX_GRANTS_EVIDENCE_GRADE=False`、`TOOLBOX_RUNS_STATISTICS=False`、`TOOLBOX_EXPLORATION_ONLY=True`。**这是设计意图的落点**——框架 §7 要求 LLM 自己用算法分析，而非仅润色文本（旧实现的 `text_enricher` 是偏差）。
- **句柄机制是核心约束**：`HandleStore` 把富对象（`Candidate`/`ExplorationSample`/`KnowledgeBase`/`Evaluation`）映射为不透明句柄 `h_<hex>`。`_require_handle` 只认 `h_` 前缀 → LLM 只能引用上游**真实产出**，无法构造内部对象绕过校验。保留句柄（`client:explorer`）不进 `kinds()`/计数器，也不可经 `_require_handle` 取出。**别给句柄强加类型前缀语义**（同 M1 `historical_ref`）。
- **工具名带模块前缀**（`m02.`–`m09.`），15 个；参数 schema 由 `inspect.signature` 派生（`_spec_params`），**不手写**以免漂移。`describe()` 返回 `ToolSpec` 对象（非 dict，要 `.to_dict()`）。
- **封存泄漏守卫必须校准**：不能直接复用 M8 的 `check_no_sealed_leak`——其 `ARCHIVE_SEALED_KEYS` 含 `source`/`source_ref`，而 M5 `complexity` 的**合法输出键**恰是 `source`，会误杀。M10 自建 `LEAK_GUARD_EXEMPT_KEYS={"source","source_ref"}`，`M10_SEALED_KEYS = ARCHIVE_SEALED_KEYS − 豁免`。**再次印证「宁窄勿宽」**。
- **两种知识对象不可混用**：M8 `KnowledgeVersion`（证据等级）≠ M5 `KnowledgeBase`（新颖度）。`m05.novelty` 要后者；误传前者被**类型守卫**拦为 `rejected`/`bad_handle`（比 `failed` 更准确）。
- **`fit_relation` 要同时接受 `Candidate` 与 `Ast` 句柄**（预拟合链用 Ast）；`gain_against` 需 `_as_fit_result()` 拆 `RelationFit.candidate`（`RelationFit` 是包装层）。
- 原因码：`unknown_tool`/`forbidden_argument`/`bad_handle`/`missing_argument`/`unexpected_argument`/`sealed_leak`/`tool_raised`。`FORBIDDEN_TOOL_ARGUMENTS` 14 个（`grade`/`p_value`/`conclusion`/`causal`/`supported`…），LLM 不能传这些参数名。
- **封装层参数名必须查 `describe()`，不可凭直觉**：`m03.fit_relation` 的拟合形式参数叫 **`relationship`**（不是 `ast`）；`m03.prepare_sample` 必填 `variables`+`target`；`m03.baseline_linear` 必填 `feature`；`m05.novelty` 要 M5 的 `KnowledgeBase`。写调用/示例/文档前一律先跑 `tb.describe(name).to_dict()` 核对。

## M11 LLM 驱动层语义（P19 起复用，写驱动/断言前必读）
- **M10 是「接口」，M11 是「真的去用它」**：M10 只交付工具目录 + 句柄 + 护栏，**不含 LLM**；M11 才读目录、组装工具 schema、调 endpoint、解析并执行工具调用、结果回喂。二者缺一则框架 §7 落空。
- **六常量全 False**：`DRIVER_DECIDES_FOR_LLM`（不替 LLM 决策）/`ADDS_ALGORITHMS`/`RUNS_STATISTICS`/`GRANTS_EVIDENCE_GRADE`/`BYPASSES_TOOLBOX`/`REQUIRES_FREE_CODE`。**一切工具调用经 `Toolbox.call()`**，继承 M10 四类护栏与封存泄漏检查。
- **凭据纪律**：仅从 `SDL_LLM_BASE_URL`/`SDL_LLM_API_KEY`/`SDL_LLM_MODEL`/`SDL_LLM_TIMEOUT` 环境变量读；`api_key` 在 `to_dict()` 掩码、**不进 `canonical_json()`/`content_digest()`/提示词**（指纹只反映「连哪、用哪个模型」）。测试断言：换密钥不改变 `content_digest()`。
- **离线可测靠依赖倒置**：对话抽象为 `LLMClient` Protocol（仅 `complete(messages, tools)->LLMReply`），测试注入脚本化假 client。同 M9 注入 source、M10 注入 explorer 的手法。
- **M12 的 `status` 不能直接塞进 M10 `ToolResult`**：M10 只认自己那套原因码，否则 `__post_init__` 抛 `ToolboxInputError`。须显式映射：`ok→(OK,None)`｜`rejected→(REJECTED,forbidden_argument)`｜其余→`(FAILED,tool_raised)`，原始状态留 `summary["sandbox_status"]`。
- **预算账实相符**：`DriverStep.tool_calls` 只记**真正执行过**的调用——预算用尽时被跳过的调用不得计入 `tool_call_count`。

## M12 隔离执行语义（P20 起复用，写沙箱/断言前必读）
- **硬顺序：先静态检查、不过绝不执行**。`run_code` 在 `check_code` 失败时**直接 return**，不进编译/执行分支（`SANDBOX_RUNS_UNCHECKED_CODE=False`，无「先跑再看」路径）。六常量全 False：`ADDS_ALGORITHMS`/`GRANTS_EVIDENCE_GRADE`/`ACCESSES_M1`/`ALLOWS_NETWORK`/`ALLOWS_FILESYSTEM`/`RUNS_UNCHECKED_CODE`。
- **dunder 名字须专门拦**：`return __builtins__` 是**裸 Name**、不在 `FORBIDDEN_NAMES` → 枚举法会漏。用「形如 `__x__` 一律禁」（`_is_dunder`）兜底（覆盖 `__builtins__`/`__loader__`/`__spec__`/`__package__`/`__name__`）。
- **导入靠白名单 + 受控 `__import__`**：`Import`/`ImportFrom` **不在** `FORBIDDEN_NODE_TYPES`（否则正常 `import math` 被拒）；由 `ALLOWED_MODULES` 白名单单独管。但 `exec` 执行 `import X` 需 `__import__` 内置 → 注入 `_make_importer`，只放行白名单、其余抛 `ImportError`。
- **限额用 `sys.settrace`**：`signal`/`threading`/`subprocess` 本身在禁表，故用纯 Python 的 trace 钩子做指令计数 + 墙钟判定。
- **`canonical_json()` 必须剔除 `duration_ms`**：墙钟耗时是运行期测量、非内容身份，否则同代码两次执行指纹不同、无法比对去重。补反向断言：结果不同→指纹必不同。
- **测试夹具勿用被禁的东西**：探针别用 `dir()`/`object()`（都在禁表）。测命名空间隔离改用「裸名引用」——第二段引用第一段定义的变量，正规行为是 `NameError`。

## 变异测试脚本的还原纪律（元层，09-27 踩坑）
- **症状**：变异「去掉超时判定」使墙钟用例真死循环 → 后台任务被强杀 → `finally` 还原**没跑** → 源码残留变异（`grep` 锚点为空即证据）→ 下一轮基线返回 124。
- **修法（通用）**：① 变异前把原文写**磁盘备份**（`.mutation-backup`），启动时检测不一致即自动还原；② `subprocess.run(..., timeout=60)` 加硬超时（超时=捕获）；③ `try/except BaseException` 内也还原。**任何「改文件→跑测试→还原」的脚本，还原必须落磁盘备份 + 对子进程设硬超时，不能只靠 finally。**
- **变异漏网 = 测试盲区**（非变异冗余）：M12 首轮 6 变异漏 2——「禁用名不拦」暴露越狱清单每条都是 call/import/dunder、缺**裸非法非-dunder 名字引用**；「去掉超时判定」暴露 `test_infinite_loop_times_out` 的指令计数先触发、**墙钟分支从未单独测到**。补用例后 6/6。

## 新阶段登记补充（09-27 二次踩坑）
- 推进逻辑在阶段通过后会**自动把下一阶段的 `output_fingerprints` 快照为真实指纹**（见 `ds_stage_loop.py` 第 888 行附近）。故「新增**末阶段**并让既有交付物参与本轮验收」时，必须在跑门禁**前**手工把该阶段指纹重置为 `MISSING`，否则交付物被判「未变」→ 报 `nochange`。
- 流程：`stages.json` 追加配置 → `state.json` 置 `current_stage=<新阶段>`/`in_progress`/指纹 `MISSING` → `--force-run` → 通过后脚本推进 → **若推进到的是「待验收的既有交付物阶段」，须再手工重置其指纹为 `MISSING`** → 再 `--force-run`。


## 模块约定
- 依赖方向：M2 → M3 → M4 → M5 → M6/M7 → M8 → M9 → **M10（封装以上全部，不反向依赖）** → **M11（驱动 M10，不反向依赖）** / **M12（被 M11 可选调用，独立沙箱）**（M9 在模块层不导入 `sdl_m01`，端到端编排函数体内惰性导入），`sdl_m01/` 是**冻结目录**（mtime 应恒为 09-19），任何阶段不得修改或导入它。**M11 是唯一允许网络调用的模块**（仅 stdlib `urllib` 或注入客户端）。
- 每个模块自带「本层不做什么」的机检常量（`NOT_PROVIDED_BY_Pxx`）+ 边界说明 note，随结果对象输出。
- 口径一致性优先：下游**原样采信**上游口径（如 C 采信 P04 的复杂度映射），不一致时只记录差异、不推翻。
- **「没算」与「算出来是 0」必须可区分**：证据缺失记 `None` 并列入 `missing_*`，**不插补、不填默认值、不因此扣分**。
- 结果对象一律提供 `to_dict()` / `canonical_json()` / `content_digest()`，用 `MappingProxyType` 冻结内部映射。
- 仅 Python 3.11+ 标准库，不引第三方依赖；注释与文档全中文；文件内容完整可运行，无省略号占位。

## 环境
- 运行 Python：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`。
- 脚本 stdout 在 PowerShell 工具下可能丢失，用 Bash 工具运行以稳定拿到输出与退出码。

## 版本控制（09-27 起）
- 远程仓库：<https://github.com/Vuiora/StatisticalDiscoveryLearning>（**私有**，分支 `main`，GitHub 账号 `Vuiora`）。
- `.gitignore` 已排除 `demo-output/`（含 `*.token` 与 `evidence.sqlite3`）、`__pycache__/`、`*.py[cod]` 等；提交前务必确认令牌与数据库未被纳入。
- `.workbuddy/`（ds-loop 台账 + memory 日志）随仓库提交，供过程可追溯；不含明文凭据。