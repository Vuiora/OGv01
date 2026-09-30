# SDL 项目长期记忆

只记**跨阶段复用**的规则与约定；逐阶段细节见 `.workbuddy/memory/YYYY-MM-DD.md`。

## 施工流程（分阶段循环）
- 检测：`python scripts/ds_stage_loop.py` → 读 `.workbuddy/ds-loop/last-report.md`（updated/nochange/blocked/overlap/all_done）。
- 取工单：`--work-order`；输出 `[LEASE-DENIED]` 即另一实例在做，立即停手。
- 门禁：`python -m unittest discover -s tests -v`。M01 有 **2 项既有失败**（`test_client_role_cannot_be_changed_to_bypass_authorization`、`test_failed_consumption_after_digest_check_remains_used_and_is_logged`）属放行项；**新增失败必须为 0**。
- `stages.json` 每阶段声明 `outputs`/`tests`，门禁校验其指纹相对基线必须变化。
- **动笔前先 `ls` 目标文件 + `cat state.json` 确认 current_stage**（工单反映调用瞬间状态，协同实例可能已推进）。
- 全阶段完成后循环**幂等短路**（秒退）。若「每次报 updated 且跑满全量测试」= `output_fingerprints` 停在 `MISSING` 未回写（09-27 已修）。
- **新增阶段登记法**：① `stages.json` 追加配置；② 手工置 `state.json` 的 `current_stage=<新阶段>`/`stage_status=in_progress`/`output_fingerprints`、`test_fingerprints` 为 `MISSING`/刷新 `baseline_epoch`——**不可省**，否则 `stage_status` 仍是上阶段 `done`，脚本走终态短路报 `all_done`；③ `--force-run --json`；④ 通过后脚本自动回写真实指纹并置 `done`。
- **推进逻辑会在阶段通过后自动快照下一阶段的指纹**（`ds_stage_loop.py` 约第 888 行）。故「新增**末阶段**并让既有交付物参与验收」时，须在跑门禁**前**手工把该阶段指纹重置为 `MISSING`，否则报 `nochange`；若推进到的是「待验收的既有交付物阶段」，须再重置其指纹后重跑。
- `last-report.md` 文案已改为动态取 `{cur}`（原硬编码 P16 会失真）。

## 门禁自身的匹配范围（元层缺陷，连踩三例）
门禁匹配范围过宽会**自己把自己判失败**，症状「阶段莫名 blocked」，极难定位。
- 三例共性：子串匹配 `skip/xfail` 命中边界断言｜命中 `NOT_PROVIDED_BY_*` 常量｜`rglob("*")` 扫冻结目录命中 `__pycache__/*.pyc`（跑测试即刷 mtime → 必判越界）。
- 冻结目录检查必须忽略非源码产物：`__pycache__`/`.pytest_cache`/`.mypy_cache` 与 `*.pyc/*.pyo/*.pyd`（已实现 `_is_frozen_artifact`）。
- **通用自检法**：任何「扫描 X 判有害改动」的门禁都要**两侧都测**——① 构造应忽略的干扰项（构建产物/缓存）断言不触发；② 构造真实违规项断言必被抓。只测一侧可能修成空洞门禁。

## 测试与断言规则
1. **守护「不做 X」一律用 AST，绝不文本匹配源码**（已踩 7 次）：docstring 与 `NOT_PROVIDED_BY_*` 常量里**必须**写出被禁名字，文本匹配会把边界声明当违规。文本匹配只用于检查「必须存在的声明」。
2. **夹具先打印真实输出再写断言**：
   - 配额类（上限/裁剪/k 预留）夹具必须是**反链**（候选两两不可比）；单调链会让前沿恒为 1，测的是前沿规模而非配额。
   - 簇/变点类先用诊断脚本确认检测器真实输出（k、分数），且簇内点云各向同性。
   - 夹具方法名不得撞 `unittest.TestCase` 内部属性（`_outcome`/`_feedErrorsToResult`）——`self._outcome` 遮蔽后报 `'_Outcome' object is not callable`，报错点远离真因。
3. 写测试后做**定向变异测试**（反转方向/放宽定义/改默认值）确认断言能捕获（P17 6/6、M11 8/8、M12 6/6）。
4. **变异漏网 = 测试盲区**，去补用例别删变异。M12 首轮漏 2：「禁用名不拦」暴露越狱清单每条都是 call/import/dunder、缺**裸非法非-dunder 名字引用**；「去掉超时判定」暴露指令计数先触发、**墙钟分支从未单独测到**。
5. **变异脚本还原纪律**：必须落**磁盘备份**（`.mutation-backup`，启动时不一致即自动还原）+ `subprocess.run(..., timeout=60)` 硬超时（超时=捕获）+ `except BaseException` 内也还原。**不能只靠 finally**——进程被强杀时 finally 不跑，会残留变异（踩过：下一轮基线返回 124）。

## M1 归档与数据视图语义（写断言前必读）
- `archive_confirmation(binding_id) -> {"historical_ref": ...}`：仅绑定已 `recorded`+`released` 时可归档，幂等。`historical_ref` 是**不透明数据引用**（`data_<hex>`），**不带** `H_` 前缀——`H_<binding_id>` 是该数据集在协议下的**用途名**，到 `describe(protocol_id)["resources"]` 里查。别给引用强加前缀。
- 「已见」判定基于**内容级数据身份**：记录指纹**剔除 `record_id`**，故换 `record_id` 重放同批内容会被 M1 以「Previously registered content cannot be resealed」拒绝。想在规则层演练「复用已见数据」而不触发指纹闸门，直接构造 `KnowledgeVersion` 调 `_grade_for`。
- M1 把**声明范围外**环境裁掉：测「新环境复现」须在 `task.environments` 声明该环境（每环境单独建协议）。
- 同一研究轮次只能有一个冻结确证批次；同阶段多次端到端绑定须递增 `round_index`。
- M1 分区会把批次分到 E/V/C（60 组 → E 约 36）：放分区外的「异常批次」在探索数据上**看不到**，反例清单恒为空。测反例须让异常批次落在 E 内（缺省 `broken=(0,3)`）。

## M9 自主取数语义（P17 起）
- **M7/M9 是「建议层/执行层」补集**：`ACQUISITION_PLAN_EXECUTES_COLLECTION=False`（M7 只出建议）对 `COLLECTION_EXECUTES_COLLECTION=True`（M9 真取数）。M9 不授予等级、不跑统计。
- **闭环靠「上一轮」计划**：`observe(round_index, plan)` 收当轮计划，`__call__(round_index, protocol_id)` 采的是 `round_index-1` 的计划。故**首轮必无建议**（记 `no_plan`），`trace()` 里 `driven_by_plan_round=round_index-1`；同轮 observe 不影响同轮采样（不向后泄漏）。
- **六原因码分列**：`collected`｜`source_empty`（**采了为空**：设计齐备、真驱动源、返回空）｜`no_mapping`/`no_plan`/`budget_exhausted`/`source_failed`（**没采**）。绝不用 0 条记录冒充「取过了」。
- **采样目录由调用方提供**：精确 `by_observation` → 按目的 `by_purpose` → 兜底 `default_design` → None（记 `no_mapping`）。M9 不臆造设计。
- **`SyntheticSource` 每批用新号段**（`_allocate_start` 递增），否则重放同批内容被 M1 内容级身份闸门拒绝。
- **`run_loop` 的 `acquisition_sink` 钩子**：只读、返回值被忽略、异常降级为一条 note；缺省 `None` 时与 P16 一致。**只调用一次**（在 `round_records.append` 之前）。
- **`freeze_cap` 不能取 1**：M7 `divergence` 至少需 2 个竞争假说。端到端最小预算：`max_candidates=60, freeze_cap=2, n_resamples=2`（两轮约 3.2s）。
- 测「规律提取」用轻量假对象，别走 M8 构造器（不变量极严）。

## M10 分析决策层语义（P18 起）
- **M10 是决策层，M2–M9 是算法层**：把既有公开接口封装为 LLM 可调用工具，**由 LLM 决定调用顺序与参数**。五个机检常量：`TOOLBOX_DECIDES_NOT_LLM=False`、`ADDS_ALGORITHMS`/`GRANTS_EVIDENCE_GRADE`/`RUNS_STATISTICS=False`、`EXPLORATION_ONLY=True`。这是框架 §7 的落点（LLM 自己用算法分析，非润色文本）。
- **句柄机制是核心约束**：`HandleStore` 把富对象映射为不透明句柄 `h_<hex>`；`_require_handle` 只认 `h_` 前缀 → LLM 只能引用上游**真实产出**，无法构造内部对象绕过校验。保留句柄（`client:explorer`）不进 `kinds()`/计数器。别给句柄强加类型前缀语义。
- 工具名带模块前缀（`m02.`–`m09.`）共 15 个；参数 schema 由 `inspect.signature` 派生（`_spec_params`），**不手写**以免漂移。`describe()` 返回 `ToolSpec`（要 `.to_dict()`）。
- **封存泄漏守卫必须校准**：不能复用 M8 `check_no_sealed_leak`——其 `ARCHIVE_SEALED_KEYS` 含 `source`/`source_ref`，而 M5 `complexity` 合法输出键恰是 `source`，会误杀。M10 自建 `LEAK_GUARD_EXEMPT_KEYS={"source","source_ref"}`（**宁窄勿宽**）。
- **两种知识对象不可混用**：M8 `KnowledgeVersion`（证据等级）≠ M5 `KnowledgeBase`（新颖度）。`m05.novelty` 要后者；误传被类型守卫拦为 `rejected`/`bad_handle`。
- **参数名必须查 `describe()`，不可凭直觉**：`m03.fit_relation` 的拟合形式参数叫 **`relationship`**（非 `ast`）；`m03.prepare_sample` 必填 `variables`+`target`；`m03.baseline_linear` 必填 `feature`。写调用/文档前一律先跑 `tb.describe(name).to_dict()`。
- 原因码：`unknown_tool`/`forbidden_argument`/`bad_handle`/`missing_argument`/`unexpected_argument`/`sealed_leak`/`tool_raised`。`FORBIDDEN_TOOL_ARGUMENTS` 14 个（`grade`/`p_value`/`conclusion`/`causal`/`supported`…）。

## M11 LLM 驱动层语义（P19 起）
- **M10 是「接口」，M11 是「真的去用它」**：M10 只交付工具目录+句柄+护栏，**不含 LLM**；M11 读目录、组装工具 schema、调 endpoint、解析并执行工具调用、结果回喂。
- **六常量全 False**：`DRIVER_DECIDES_FOR_LLM`/`ADDS_ALGORITHMS`/`RUNS_STATISTICS`/`GRANTS_EVIDENCE_GRADE`/`BYPASSES_TOOLBOX`/`REQUIRES_FREE_CODE`。**一切工具调用经 `Toolbox.call()`**。
- **凭据纪律**：仅从 `SDL_LLM_BASE_URL`/`SDL_LLM_API_KEY`/`SDL_LLM_MODEL`/`SDL_LLM_TIMEOUT` 读；`api_key` 在 `to_dict()` 掩码、**不进 `canonical_json()`/`content_digest()`/提示词**。断言：换密钥不改变 `content_digest()`。
- **离线可测靠依赖倒置**：`LLMClient` Protocol（仅 `complete(messages, tools)->LLMReply`），测试注入脚本化假 client。
- **M12 的 `status` 不能直接塞进 M10 `ToolResult`**（`__post_init__` 抛 `ToolboxInputError`）。须映射：`ok→(OK,None)`｜`rejected→(REJECTED,forbidden_argument)`｜其余→`(FAILED,tool_raised)`，原始状态留 `summary["sandbox_status"]`。
- **预算账实相符**：`DriverStep.tool_calls` 只记**真正执行过**的调用，被跳过的不得计入 `tool_call_count`。
- **工具 schema 的泛型映射**（真实 endpoint 才暴露）：`Sequence[str]`/`Iterable[float]` 必须映射为 `array`+`items`，用精确匹配会 fallthrough 成 `string` → LLM 拿到错 schema，`prepare_sample`/`enumerate_candidates` 全不可用。已实现 `_split_generic`/`_json_type_for`（含 `_SEQUENCE_BASES`/`_MAPPING_BASES`）。

## M12 隔离执行语义（P20 起）
- **硬顺序：先静态检查、不过绝不执行**。`run_code` 在 `check_code` 失败时**直接 return**（`RUNS_UNCHECKED_CODE=False`，无「先跑再看」）。六常量全 False：`ADDS_ALGORITHMS`/`GRANTS_EVIDENCE_GRADE`/`ACCESSES_M1`/`ALLOWS_NETWORK`/`ALLOWS_FILESYSTEM`/`RUNS_UNCHECKED_CODE`。
- **dunder 名字须专门拦**：`return __builtins__` 是**裸 Name**、不在 `FORBIDDEN_NAMES` → 枚举法会漏。用 `_is_dunder`（形如 `__x__` 一律禁）兜底。
- **导入靠白名单 + 受控 `__import__`**：`Import`/`ImportFrom` **不在** `FORBIDDEN_NODE_TYPES`（否则正常 `import math` 被拒）；由 `ALLOWED_MODULES` 单独管，注入 `_make_importer` 只放行白名单。
- **限额用 `sys.settrace`**（`signal`/`threading`/`subprocess` 本身在禁表）：纯 Python trace 钩子做指令计数 + 墙钟判定。
- **`canonical_json()` 必须剔除 `duration_ms`**（墙钟是运行期测量非内容身份，否则同代码两次指纹不同）。补反向断言：结果不同→指纹必不同。
- **测试夹具勿用被禁的东西**：探针别用 `dir()`/`object()`（都在禁表）；测命名空间隔离用「裸名引用」（第二段引用第一段变量 → `NameError`）。

## 跨层集成语义（P16 起，写集成前必读）
- **M6 `execute_family` 内部 `replay=False` 硬编码**：同一绑定首次消费后分区迁 `used`，再消费被 M1 以「explicit replay is required」拒绝。要在同一次执行内用外部评估器产物重跑，须在注入的 confirmer 里把第二遍 `replay` 提升为 `True`（`new_evidence=False`，不改账本）。
- **P11 把冻结计划的 `model` 序列化成文本**，表达式是**规范化 JSON 字符串**；`from_ast` 只接受嵌套列表，字符串须先 `json.loads`，否则模型项**静默丢掉**、候选退化成常数预测。
- **零模型（常数基线）必须取冻结截距**（斜率置零的候选），不能取「每批次自身观测均值」（看过该批全部数据，是假基线）。
- **`effect_threshold` 必须为正**（M6 拒绝 `<=0`），缺省 0.1。
- **M7 的 `acquisition_plan` 对确证类目（`confirmation`/`counterexample`）强制 `pre_sampling_freeze`**：必填 `frozen_candidates`（非空）、`frozen_protocol`（非空映射）、`freeze_before_sampling=True`（字面 True）。无「跳过」参数。
- **协议视图随 `add_confirmation` 变**：每轮须经 custodian 公开 `describe` 刷新，不能只信启动快照。
- 外部评估器返回的检验轨迹键名须在 M6 白名单内（`p_value`/`effect`/`delta`/`notes`…；是 `notes` 非 `note`），未知键一律拒绝。
- `evaluate_hypothesis` 可能因 `gain=inf` 抛 `MetricsInputError`：集成层逐条 try/except 降级入 `fit_skips`，不得打断整轮。
- **AST 边界自检假阳性来源**：本地变量名撞黑名单（如 `ledger = BudgetLedger()`）。`self_check` 要收集本地绑定名（参数/赋值/自身定义）并剔除，只比对真正的访问与 import。

## 模块约定
- 依赖方向：M2→M3→M4→M5→M6/M7→M8→M9→**M10**（封装以上全部，不反向依赖）→**M11**（驱动 M10）/ **M12**（被 M11 可选调用，独立沙箱）。`sdl_m01/` 是**冻结目录**（mtime 恒为 09-19），任何阶段不得修改或导入。**M11 是唯一允许网络调用的模块**（仅 stdlib `urllib` 或注入客户端）。
- 每模块自带「本层不做什么」的机检常量（`NOT_PROVIDED_BY_Pxx`）+ 边界说明 note，随结果对象输出。
- 口径一致性优先：下游**原样采信**上游口径，不一致时只记录差异、不推翻。
- 「没算」与「算出来是 0」必须可区分：证据缺失记 `None` 并入 `missing_*`，不插补、不填默认值。
- 结果对象一律提供 `to_dict()`/`canonical_json()`/`content_digest()`，内部映射用 `MappingProxyType` 冻结。
- 仅 Python 3.11+ 标准库，不引第三方依赖；注释与文档全中文；文件完整可运行，无省略号占位。

## 环境与版本控制
- 运行 Python：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`。脚本 stdout 在 PowerShell 下可能丢失，**用 Bash 运行**。
- 远程仓库：<https://github.com/Vuiora/StatisticalDiscoveryLearning>（**私有**，分支 `main`）。`.gitignore` 已排除 `demo-output/`（含 `*.token`、`evidence.sqlite3`）、`__pycache__/`、`*.py[cod]`、`*.mutation-backup`；提交前确认令牌与数据库未纳入。`.workbuddy/`（ds-loop 台账 + memory 日志）随仓库提交，不含明文凭据。
