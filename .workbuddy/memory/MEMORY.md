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
2. **夹具必须先打印真实输出再写断言。** 两个具体教训：
   - **配额类测试**（上限/裁剪/k 个预留）夹具必须是**反链**——候选两两不可比。用单调链（后一个全面支配前一个）会让前沿恒为 1，测的是前沿规模而非配额。
   - **簇/变点类测试**要先用诊断脚本确认检测器的真实输出（k 是多少、分数多少），且簇内点云须各向同性，不留单调方向。
3. 写测试后建议做**定向变异测试**（故意反转方向、放宽定义、改默认值），确认断言能捕获——防止空洞断言。

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

## 模块约定
- 依赖方向：M2 → M3 → M4 → M5 → M6/M7 → M8，`sdl_m01/` 是**冻结目录**（mtime 应恒为 09-19），任何阶段不得修改或导入它。
- 每个模块自带「本层不做什么」的机检常量（`NOT_PROVIDED_BY_Pxx`）+ 边界说明 note，随结果对象输出。
- 口径一致性优先：下游**原样采信**上游口径（如 C 采信 P04 的复杂度映射），不一致时只记录差异、不推翻。
- **「没算」与「算出来是 0」必须可区分**：证据缺失记 `None` 并列入 `missing_*`，**不插补、不填默认值、不因此扣分**。
- 结果对象一律提供 `to_dict()` / `canonical_json()` / `content_digest()`，用 `MappingProxyType` 冻结内部映射。
- 仅 Python 3.11+ 标准库，不引第三方依赖；注释与文档全中文；文件内容完整可运行，无省略号占位。

## 环境
- 运行 Python：`C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`。
- 脚本 stdout 在 PowerShell 工具下可能丢失，用 Bash 工具运行以稳定拿到输出与退出码。
