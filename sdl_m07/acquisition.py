"""P14 交付物：主动取证与反例搜索（模块 M7）。

本模块回答主循环末尾的那个问题：**下一轮该去看什么？** 理论依据见
`SDL算法框架说明.md <../SDL算法框架说明.md>`_ §2 末段与 §5 伪代码：

    主动取证优先选择假说预测分歧大、接近适用边界、且采样成本可承担的观测。
    原型可用「候选预测分歧 / 采样成本」排序；它是启发式，只有另行定义假说概率、
    观测模型与信息量时，才称为预期信息增益。涉及确证的数据采集，必须在采样前
    固定候选及采样、检验方案。

    伪代码：``若仍需研究: 设计区分竞争假说或检验边界的下一轮取证``

接口约定见 `INTERFACES.md <../INTERFACES.md>`_ §1（M7 服务于下一轮取证）与 §4
（禁止事项）；框架说明 §2 给出 M7 的输入输出定位：

    M7 主动取证与反例搜索 ｜ 输入：竞争假说、边界条件、未决问题
    ｜ 输出：区分性观测、实验或复现计划及所需数据

三条验收标准的实现方式
----------------------

① **输出排序值明确标注为启发式，不得命名为预期信息增益。**

   本模块的排序依据是一个**单一比值**：候选预测分歧 / 采样成本。它被显式标注为
   启发式，而不是伪装成一条信息论结论：

   - 常量 :data:`RANKING_BASIS_HEURISTIC` 是排序依据的字面值，结果对象
     （:class:`DivergenceReport` / :class:`AcquisitionPlan`）在 ``to_dict()`` 中
     恒输出 ``ranking_basis`` 与 ``is_heuristic``，不随输入变化；
   - :data:`ACQUISITION_RANKING_IS_HEURISTIC` 恒为 ``True``；
   - 排序值字段命名为 ``heuristic_value``，**不存在**任何名为
     ``expected_information_gain`` / ``eig`` / ``mutual_information`` 的字段、
     函数或参数；:data:`FORBIDDEN_RANKING_NAMES` 把这一禁令变成可机检的声明，
     :func:`self_check` 用 AST 复核模块确实没有定义这些名字；
   - 任何试图向本模块传入假说概率、观测模型或信息量参数的调用，都会在调用点被
     :class:`AcquisitionPolicyError` 拒绝（:data:`FORBIDDEN_RANKING_KEYS`）。
     报错信息点明：只有另行定义假说概率与观测模型之后，才谈得上预期信息增益。

   口径原文随结果对象输出，见 :data:`RANKING_NOT_EIG_NOTE` 与
   :data:`HEURISTIC_DEFINITION_NOTE`。

② **涉及确证的数据采集，计划中必须写明采样前固定候选与方案。**

   「确证类取证」的判定是显式词表 :data:`CONFIRMATION_GRADE_PURPOSES`：
   确证取证（``confirmation``）与反例搜索（``counterexample``）——后者的目的
   正是寻找与假说预测**不相容**的观测，其结论会进入确证链条，因此同样受约束。
   纯探索取证（``exploration``）不受此约束。

   当 ``purpose`` 落在该词表内时，:func:`acquisition_plan` **强制**要求
   「采样前冻结」声明（参数 ``pre_sampling_freeze``），并逐项校验：

   ===========================  ===============================================
   ``freeze_before_sampling``   必须字面为 ``True``（在采样**之前**已固定）
   ``frozen_candidates``        非空的候选标识序列（冻结的是哪一批候选）
   ``frozen_protocol``          非空的采样与检验方案（怎么采样、怎么检验）
   ===========================  ===============================================

   缺任意一项即 :class:`AcquisitionPolicyError`。**本模块不提供关闭该校验的
   参数**——验收标准②是硬约束，不是可选开关。计划对象因此恒携带
   :data:`PRE_SAMPLING_FREEZE_NOTE`，并在 ``requires_pre_sampling_freeze``
   中如实上报本轮是否受该约束。

③ **计划为纯建议对象，不自动触发任何数据获取。**

   三重保证：

   - :data:`ACQUISITION_PLAN_EXECUTES_COLLECTION` 恒为 ``False``，
     :class:`AcquisitionPlan` 的 ``executes_collection`` 与 ``is_advisory``
     是常量属性，不参与构造；
   - 模块**不导入**任何数据访问路径，也不定义 :func:`_forbidden_access_names`
     所列的任一名字（``read_dataset`` / ``consume_confirmation`` / ``fetch`` /
     ``collect`` / ``sqlite3`` 等等）。:func:`self_check` 用 AST 复核这一点；
   - 本模块同样**不授予证据等级**（:data:`ACQUISITION_GRANTS_EVIDENCE_GRADE`
     恒为 ``False``）：计划的产出是「建议去看什么」，不是「看过了，结论是什么」。

关于「没算」与「算出来是 0」的可区分性
--------------------------------------

与 M5/M6 一致，本模块**不插补、不填默认值、不因此扣分**：

- **无法比较 → ``None``。** 某观测上给出预测的假说少于 2 个时，分歧不可计算，
  记为 :data:`UNDETERMINED_DIVERGENCE`（``None``），不填 0。
- **确实一致 → ``0.0``。** 多个假说都给出了预测且完全相同，分歧是**算出来的 0**：
  该观测对区分这些假说毫无帮助，这是有信息量的结论，必须与 ``None`` 区分。
- **采样成本未给出 → 不参与预算判断。** 缺失成本不被当作 0 成本（那会让未标价
  的观测无条件挤占预算），此类观测单列在 ``excluded_unknown_cost`` 中如实上报。
  原因见 :data:`MISSING_COST_POLICY`。

关于「不做跨维度加权总分」
--------------------------

本模块沿用 M5 建立的纪律：排序只用一个**显式比值**（分歧 ÷ 成本），
**不是**把多个维度折算成单一数值的加权和。「接近适用边界」这一条不参与加权，
它只作为**字典序平局裁决**使用（:data:`BOUNDARY_TIEBREAK_NOTE`）：分歧/成本
相同时，优先取更接近边界的观测。任何 ``weights`` / ``score`` 一类入参一律被
:class:`AcquisitionPolicyError` 拒绝。

主要入口
--------

=========================  ==========================================================
:func:`divergence`         逐观测计算竞争假说之间的预测分歧（启发式，非 EIG）。
:func:`acquisition_plan`   依「分歧 / 成本」排序并施加预算，产出下一轮取证计划。
=========================  ==========================================================

定位与边界（严格遵守 P14 增量卡）
----------------------------------

- 产物是**建议对象**：不执行采集、不授予证据等级、不构成确证结论。
- 依赖方向：本模块的入参是上游的假说池（如 P10 的筛选结果，经本模块 duck-typing
  规范化），本模块**不导入** ``sdl_m01`` 至 ``sdl_m06`` 的任何内容，以保持 M7
  与探索侧/确证侧的解耦。
- 不实现 P15（知识归档与假说更新）与 P16（端到端主循环集成）的内容，
  见 :data:`NOT_PROVIDED_BY_P14`。
- 不修改 ``sdl_m01/``，不引入第三方依赖，不读取或写入任何角色令牌、evidence
  数据库内容或受限质量报告。

已知限制
--------

1. **分歧是「预测值离散度」，不是概率意义上的信息量。** 它只要求各假说在同一
   观测上给出数值预测，不要求这些预测具备校准的概率含义；因此本模块的排序
   不能用于声称「该观测的信息增益是多少」。
2. **边界接近度依赖调用方提供的量纲。** ``boundary_distance`` 的比较阈值
   :data:`BOUNDARY_PROXIMITY_EPSILON` 是绝对阈值，未做量纲归一；调用方应
   自行把距离换算到可比尺度，或直接显式给出 ``near_boundary``。
3. **``normalized_divergence`` 的取值上界依赖定义。** 它按
   ``离散度 / (|预测| 的最大绝对值 + ε)`` 计算，落在 ``[0, 2]``（当最大绝对值
   为正时）；它只用于诊断，默认不参与排序。
4. **不校验预测的科学合理性。** 本模块能确认「同一条观测上各假说都给了数值
   预测」，但无法判断这些预测是否真的来自各自的假说模型——把假说变成预测值
   是上游（或调用方）的职责。
5. **预算是一次性贪心。** 依排序逐个取用直到预算耗尽，不做背包式全局最优；
   原型阶段的取证计划不需要该复杂度。
6. **不检测重复观测。** 同一 ``observation_id`` 在同一批输入中重复会被拒绝，
   但跨批次的重复（例如与历史取证计划撞车）需由 P15/P16 去重。
"""

from __future__ import annotations

import ast as _ast
import hashlib
import inspect
import json
import math
import pathlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

__all__ = [
    # 异常
    "AcquisitionError",
    "AcquisitionInputError",
    "AcquisitionPolicyError",
    # 常量：版本与排序口径
    "ACQUISITION_VERSION",
    "RANKING_BASIS_HEURISTIC",
    "ACQUISITION_RANKING_IS_HEURISTIC",
    "RANKING_NOT_EIG_NOTE",
    "HEURISTIC_DEFINITION_NOTE",
    "FORBIDDEN_RANKING_NAMES",
    "FORBIDDEN_RANKING_KEYS",
    "VALUE_MODE_SPREAD",
    "VALUE_MODE_NORMALIZED",
    "VALUE_MODES",
    "DIVERGENCE_EPSILON",
    "COMPARISON_TOLERANCE",
    # 常量：缺口口径
    "UNDETERMINED_DIVERGENCE",
    "MISSING_PREDICTION_POLICY",
    "MISSING_COST_POLICY",
    "BOUNDARY_TIEBREAK_NOTE",
    "BOUNDARY_PROXIMITY_EPSILON",
    # 常量：取证目的与「采样前冻结」
    "PURPOSE_EXPLORATION",
    "PURPOSE_CONFIRMATION",
    "PURPOSE_COUNTEREXAMPLE",
    "ACQUISITION_PURPOSES",
    "CONFIRMATION_GRADE_PURPOSES",
    "PURPOSE_FREEZE_REQUIREMENT",
    "PRE_SAMPLING_FREEZE_REQUIRED_KEYS",
    "PRE_SAMPLING_FREEZE_NOTE",
    "FREEZE_BEFORE_SAMPLING_FLAG",
    # 常量：建议对象与边界
    "ACQUISITION_PLAN_EXECUTES_COLLECTION",
    "ACQUISITION_GRANTS_EVIDENCE_GRADE",
    "PLAN_IS_ADVISORY_NOTE",
    "DEFAULT_MAX_ITEMS",
    "ACQUISITION_ITEM_NOTE",
    "SEALED_KEYS",
    "FORBIDDEN_TOKEN_KEYS",
    "NOT_PROVIDED_BY_P14",
    # 结果对象
    "ObservationDivergence",
    "DivergenceReport",
    "AcquisitionItem",
    "AcquisitionPlan",
    # 入口
    "divergence",
    "acquisition_plan",
    "self_check",
]

# ---------------------------------------------------------------------------
# 版本与排序口径（验收标准①）
# ---------------------------------------------------------------------------

#: 本模块的口径版本。
ACQUISITION_VERSION = "P14-v1.0"

#: 排序依据的字面值：启发式。恒出现在结果对象的 ``ranking_basis`` 字段中。
RANKING_BASIS_HEURISTIC = "heuristic"

#: 本模块的排序值是否为启发式。恒为 ``True``，是验收标准①的机检锚点。
ACQUISITION_RANKING_IS_HEURISTIC = True

#: 排序口径的原文说明（随每个结果对象输出）。
#:
#: 框架说明 §2 末段的表述是：「原型可用『候选预测分歧 / 采样成本』排序；
#: **它是启发式**，只有另行定义假说概率、观测模型与信息量时，才称为预期信息增益。」
#: 本模块严格停在前半句：**不引入**假说概率、观测模型或信息量，因此也**不**
#: 使用「预期信息增益」这一名称。
RANKING_NOT_EIG_NOTE = (
    "本模块的排序值是启发式——「候选预测分歧 / 采样成本」这一比值，"
    "而不是预期信息增益（EIG）。本模块没有假说先验概率、没有观测似然模型、"
    "没有信息量定义，因此不具备称为预期信息增益的前提；"
    "把它当作 EIG 使用是对框架说明 §2 末段的误读。"
)

#: 启发式分歧的定义说明。
HEURISTIC_DEFINITION_NOTE = (
    "启发式分歧 = 同一观测上各竞争假说预测值的离散度（最大值 − 最小值）。"
    "预测值少于 2 个时不可计算，记为 None（不填 0）；"
    "预测值齐备且完全相同时是算出来的 0，表示该观测无法区分这些假说。"
)

#: **禁止出现**的排序命名（验收标准①的机检声明）。
#:
#: 这些名字一旦出现在本模块的函数、类或字段命名中，就意味着把启发式比值
#: 冒充成了信息论结论。:func:`self_check` 用 AST 复核本模块确实没有定义它们。
FORBIDDEN_RANKING_NAMES: tuple[str, ...] = (
    "expected_information_gain",
    "information_gain",
    "mutual_information",
    "eig",
    "expected_utility",
    "posterior_entropy",
    "information_value",
)

#: **拒绝接受**的入参键名（验收标准①的调用点防线）。
#:
#: 显式名单之外，另加子串兜底，防止换个名字绕过。报错信息点明：只有在
#: 另行定义假说概率与观测模型之后，才谈得上预期信息增益。
FORBIDDEN_RANKING_KEYS: frozenset[str] = frozenset(
    {
        "expected_information_gain",
        "information_gain",
        "mutual_information",
        "entropy",
        "eig",
        "posterior",
        "prior",
        "hypothesis_probabilities",
        "observation_model",
        "likelihood",
        "utility",
        "expected_utility",
        "information",
    }
)

#: 参与排序的分歧口径：原始离散度（默认）。
VALUE_MODE_SPREAD = "spread"

#: 参与排序的分歧口径：按预测量级归一化后的离散度。
VALUE_MODE_NORMALIZED = "normalized"

#: 合法取值集合。
VALUE_MODES: tuple[str, ...] = (VALUE_MODE_SPREAD, VALUE_MODE_NORMALIZED)

#: 浮点比较容差，用于「分歧是否为 0」的判定与排序键量化。
COMPARISON_TOLERANCE = 1e-12

#: 归一化分歧的分母保护项，避免预测值全为 0 时除零。
DIVERGENCE_EPSILON = 1e-9

# ---------------------------------------------------------------------------
# 缺口口径：区分「没算」与「算出来是 0」
# ---------------------------------------------------------------------------

#: 「分歧不可计算」的表示值。刻意用 ``None`` 而非 ``float("nan")`` 或 0：
#: ``nan`` 会污染排序比较，0 会把「没算」伪装成「算出来一致」。
UNDETERMINED_DIVERGENCE: None = None

#: 缺预测的口径说明。
MISSING_PREDICTION_POLICY = (
    "某观测上给出数值预测的假说少于 2 个时，分歧**不可计算**，记为 None，"
    "不插补为 0、不按 0 参与排序。显式给出 None 与完全未给出预测一律视为"
    "「未给出预测」，同属「没算」这一类别。"
)

#: 缺采样成本的口径说明。
MISSING_COST_POLICY = (
    "未给出采样成本的观测**不被当作 0 成本**（否则未标价的观测会无条件挤占预算），"
    "也不被插补为默认成本。此类观测的启发式比值不可计算，不进入计划，"
    "单列在 excluded_unknown_cost 中如实上报。成本必须为正数：非正成本使"
    "「分歧 / 成本」失去意义，属输入错误。"
)

#: 边界平局裁决的说明。
BOUNDARY_TIEBREAK_NOTE = (
    "「接近适用边界」不参与加权：它只在启发式比值**相同**时作为字典序平局裁决"
    "使用（更接近边界的观测优先）。本模块不把边界接近度折算成分数，"
    "避免出现跨维度加权总分。"
)

#: 判定 ``boundary_distance`` 是否「接近边界」的绝对阈值。
#:
#: 这是量纲相关的绝对阈值，属已知限制；调用方可直接显式给出 ``near_boundary``
#: 以绕开该假设。
BOUNDARY_PROXIMITY_EPSILON = 1e-9

# ---------------------------------------------------------------------------
# 取证目的与「采样前冻结」（验收标准②）
# ---------------------------------------------------------------------------

#: 探索取证：用于构造新表示、搜索新模式的资料获取，不进入确证链条。
PURPOSE_EXPLORATION = "exploration"

#: 确证取证：为已冻结候选采集未见确证数据的取证。
PURPOSE_CONFIRMATION = "confirmation"

#: 反例搜索：专为寻找与假说预测不相容的观测而设计的取证。
PURPOSE_COUNTEREXAMPLE = "counterexample"

#: 合法取证目的集合。
ACQUISITION_PURPOSES: tuple[str, ...] = (
    PURPOSE_EXPLORATION,
    PURPOSE_CONFIRMATION,
    PURPOSE_COUNTEREXAMPLE,
)

#: **受「采样前冻结」约束**的取证目的（验收标准②的判定依据）。
#:
#: 反例搜索之所以在列：它的产物（与假说不相容的观测）会进入确证链条，
#: 若采样前不固定候选与检验方案，「先看数据再定标准」的口子就打开了。
CONFIRMATION_GRADE_PURPOSES: frozenset[str] = frozenset(
    {PURPOSE_CONFIRMATION, PURPOSE_COUNTEREXAMPLE}
)

#: 逐目的的冻结要求说明。
PURPOSE_FREEZE_REQUIREMENT: Mapping[str, str] = MappingProxyType(
    {
        PURPOSE_EXPLORATION: (
            "探索取证不进入确证链条，不要求采样前冻结声明；若调用方主动提供，"
            "本模块仍会按同一套规则校验并如实记录。"
        ),
        PURPOSE_CONFIRMATION: (
            "确证取证必须在采样前固定候选与采样、检验方案："
            "框架说明 §2 与 §6 要求候选和协议冻结之后才允许访问确证数据。"
        ),
        PURPOSE_COUNTEREXAMPLE: (
            "反例搜索的产物会进入确证链条，因此与确证取证同受「采样前冻结」约束。"
        ),
    }
)

#: 「采样前冻结」声明中必须出现的键（验收标准②的机检锚点）。
PRE_SAMPLING_FREEZE_REQUIRED_KEYS: tuple[str, ...] = (
    "frozen_candidates",
    "frozen_protocol",
    "freeze_before_sampling",
)

#: 「已在采样前固定」的字面标记位。
FREEZE_BEFORE_SAMPLING_FLAG = "freeze_before_sampling"

#: 「采样前冻结」的口径原文（随计划对象输出）。
PRE_SAMPLING_FREEZE_NOTE = (
    "涉及确证的取证，必须在采样前固定候选及采样、检验方案——"
    "这是框架说明 §2 末段与 §6 的硬约束，不是事后补写的说明。"
    "计划中的 freeze_before_sampling 必须字面为 True，"
    "并且 frozen_candidates（冻结了哪批候选）与 frozen_protocol"
    "（采样与检验方案）都必须非空。数据一旦看过，就不能再用它来确证"
    "临时改动的候选或标准。"
)

# ---------------------------------------------------------------------------
# 建议对象与边界（验收标准③）
# ---------------------------------------------------------------------------

#: 本模块的计划是否会触发数据获取。恒为 ``False``（验收标准③的机检锚点）。
ACQUISITION_PLAN_EXECUTES_COLLECTION = False

#: 本模块是否会授予证据等级。恒为 ``False``。
ACQUISITION_GRANTS_EVIDENCE_GRADE = False

#: 建议对象的定位说明。
PLAN_IS_ADVISORY_NOTE = (
    "本计划是**纯建议对象**：它只说明下一轮建议去看什么观测，"
    "不获取任何数据、不触发任何采集、不登记任何结果、不授予任何证据等级。"
    "执行采集与确证属 M6/M8 的职责，且必须另行取得相应角色的授权。"
)

#: 单次计划的条目数默认上限（原型配置，可配置）。
DEFAULT_MAX_ITEMS = 10

#: 条目上限的口径说明。
ACQUISITION_ITEM_NOTE = (
    f"取证计划条目数默认上限 {DEFAULT_MAX_ITEMS}，可经 max_items 配置。"
    "该值是原型配置而非契约给定值：框架说明只给出「每轮保留 50 个探索候选、"
    "最多冻结 5 个确证候选」两个数字，未对「建议观测数」设界，"
    "故此处取一个可调的小值，由调用方按采样能力调整。"
)

#: 封存记录字段名。**建议观测是「规格」，不是「记录」**：交付物不得携带
#: 封存原始记录的任何字段。命中即拒绝，防止把已见确证数据夹带进计划。
SEALED_KEYS: frozenset[str] = frozenset(
    {
        "values",
        "record_id",
        "record_ids",
        "operator_annotation",
        "_quality",
        "quality_report",
        "snapshot",
        "sealed_records",
        "confirmations",
        "rows",
    }
)

#: 令牌类键名。令牌正文不得进入本层输入、日志或版本库（INTERFACES.md §4.4）。
FORBIDDEN_TOKEN_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "tokens",
        "role_token",
        "raw_token",
        "token_bytes",
        "secret",
        "password",
        "credential",
    }
)

#: 本模块**不提供**的接口名。单列成常量而非散落在测试里，是为了让
#: 「本层不做什么」也成为可机检的声明。测试据此断言 P14 未越界实现其它阶段内容。
NOT_PROVIDED_BY_P14: tuple[str, ...] = (
    # M1 的数据访问与令牌角色（本模块无任何数据访问路径）
    "read_dataset",
    "consume_confirmation",
    "bind_confirmation",
    "record_evaluation",
    "release_results",
    "archive_confirmation",
    "initialize",
    # P11 的冻结计划生成
    "build_frozen_plan",
    # P12 的检验执行
    "execute_tests",
    "execute_family",
    "holm_adjust",
    # P13 的结果三分类与登记发布
    "classify_result",
    "record_and_release",
    # P15 的知识归档与假说更新
    "archive_round",
    "update_knowledge_version",
    "revise_or_retire",
    # P16 的端到端主循环
    "run_loop",
    "main_loop",
    # 数据获取动作（验收标准③：计划不自动执行采集）
    "fetch",
    "collect",
    "acquire_data",
    "request_data",
    "run_acquisition",
    # 直接访问数据库的连接入口（本模块无任何连接路径）
    "sqlite3",
    "connect",
    # 任何形式的跨维度加权聚合
    "weighted_sum",
    "total_score",
    "aggregate_score",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class AcquisitionError(ValueError):
    """本模块所有异常的基类（进而 ``ValueError``），便于按粒度捕获。"""


class AcquisitionInputError(AcquisitionError):
    """输入形态非法：字段缺失、类型不对、数值非有限、标识重复等。"""


class AcquisitionPolicyError(AcquisitionError):
    """口径违规：试图引入 EIG 类参数、缺少采样前冻结声明、越界命名等。"""


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """把任意嵌套结构转换为 JSON 兼容形态。"""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if value is None or isinstance(value, str):
        return value
    return repr(value)


def _canonical(value: Any) -> str:
    """规范式 JSON 文本（键排序、UTF-8 直出），用于稳定比对与指纹。"""
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _require_text(value: Any, label: str) -> str:
    """要求非空字符串，返回去除首尾空白后的结果。"""
    if not isinstance(value, str) or not value.strip():
        raise AcquisitionInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value.strip()


def _require_finite_float(value: Any, label: str) -> float:
    """要求有限实数（``bool`` 被显式拒绝，因其是 ``int`` 的子类）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AcquisitionInputError(
            f"{label} 必须是实数，实际得到：{value!r}（类型 {type(value).__name__}）"
        )
    number = float(value)
    if not math.isfinite(number):
        raise AcquisitionInputError(f"{label} 必须是有限实数，实际得到：{value!r}")
    return number


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """要求映射类型。"""
    if not isinstance(value, Mapping):
        raise AcquisitionInputError(
            f"{label} 必须是映射，实际得到：{value!r}（类型 {type(value).__name__}）"
        )
    return value


def _reject_token_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝输入里出现令牌类键名。"""
    for key in mapping:
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_TOKEN_KEYS:
            raise AcquisitionPolicyError(
                f"{label} 出现受限键名 {key!r}：令牌正文不得进入本层输入、"
                "日志或版本库（INTERFACES.md §4.4）。"
            )


def _reject_sealed_keys(value: Any, label: str) -> None:
    """深度扫描封存记录字段名。

    建议观测是**规格**而不是**记录**：一旦出现 ``values`` / ``record_id`` /
    ``snapshot`` 一类字段，说明调用方把已见的确证原始记录夹带进了取证计划。
    本模块在入口处拒绝，避免受限内容外泄到下一轮。
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in SEALED_KEYS:
                raise AcquisitionPolicyError(
                    f"{label} 出现封存记录字段 {key!r}：取证计划描述的是**待采集**"
                    "的观测规格，不得携带任何封存原始记录字段"
                    "（INTERFACES.md §4.3）。"
                )
            _reject_sealed_keys(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_sealed_keys(item, f"{label}[{index}]")


def _reject_eig_attempts(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝任何试图引入信息论量或加权总分的入参（验收标准①）。

    判定口径：显式名单 ``FORBIDDEN_RANKING_KEYS``，外加对
    ``information`` / ``entropy`` / ``posterior`` / ``utility`` / ``weigh`` /
    ``score`` / ``gain`` 的子串兜底，防止换个名字绕过。报错信息必须点明
    「本模块只做启发式排序，预期信息增益需要另行定义假说概率与观测模型」，
    使越界尝试在调用点就被明确告知。
    """
    substrings = (
        "information",
        "entropy",
        "posterior",
        "utility",
        "weigh",
        "score",
        "gain",
        "probabilit",
    )
    for key in sorted(kwargs):
        lowered = str(key).strip().lower()
        if lowered in FORBIDDEN_RANKING_KEYS or any(
            token in lowered for token in substrings
        ):
            raise AcquisitionPolicyError(
                f"{where} 拒绝参数 {key!r}：本模块的排序值是启发式"
                "（候选预测分歧 / 采样成本），不是预期信息增益。"
                "要计算预期信息增益，必须先另行定义假说先验概率、观测似然模型"
                "与信息量口径——那是另一层的工作，不在 M7 内"
                "（框架说明 §2 末段）。本模块同样不接受任何跨维度加权总分。"
            )


def _reject_unknown_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝一切未知的关键字参数。

    先走 :func:`_reject_eig_attempts` 的 EIG / 加权总分判定（给出针对性的
    报错），再把剩余的任何未声明参数一律拒绝。后端这一步是必要的：
    静默忽略未知参数会让调用方误以为某个口径开关生效了（例如
    ``require_freeze=False``），而实际上没有任何效果——这正是本模块
    最不能容许的静默失效。
    """
    _reject_eig_attempts(kwargs, where)
    if kwargs:
        unknown = sorted(str(key) for key in kwargs)
        raise AcquisitionInputError(
            f"{where} 收到未声明的参数 {unknown}：本模块不提供任何可开关的"
            "口径参数（尤其是无法关闭「采样前冻结」校验），"
            "因此未知参数一律拒绝而不静默忽略。可用的参数见函数签名。"
        )


# ---------------------------------------------------------------------------
# 输入规范化：假说池
# ---------------------------------------------------------------------------


def _identity_of(item: Any) -> str:
    """从假说对象或字典中取出稳定标识（``id@version``，无版本时即 ``id``）。"""
    if isinstance(item, str):
        return _require_text(item, "hypotheses 中的标识字符串")
    if isinstance(item, Mapping):
        identity = item.get("identity")
        if isinstance(identity, str) and identity.strip():
            return identity.strip()
        hypothesis_id = item.get("hypothesis_id", item.get("id"))
        version = item.get("hypothesis_version", item.get("version", "")) or ""
    else:
        identity = getattr(item, "identity", None)
        if isinstance(identity, str) and identity.strip():
            return identity.strip()
        hypothesis_id = getattr(item, "hypothesis_id", None)
        if hypothesis_id is None:
            hypothesis_id = getattr(item, "id", None)
        version = getattr(item, "hypothesis_version", None)
        if version is None:
            version = getattr(item, "version", "") or ""
    if hypothesis_id is None or not str(hypothesis_id).strip():
        raise AcquisitionInputError(
            f"假说条目 {item!r}（{type(item).__name__}）无法取出稳定标识："
            "竞争假说必须有稳定标识才能比较预测分歧。"
        )
    hypothesis_id = str(hypothesis_id).strip()
    version = "" if version is None else str(version).strip()
    return f"{hypothesis_id}@{version}" if version else hypothesis_id


def _normalize_hypotheses(hypotheses: Any) -> tuple[str, ...]:
    """把假说池规范化为保序的标识元组，并拒绝重复标识。"""
    if isinstance(hypotheses, (str, bytes, Mapping)):
        raise AcquisitionInputError(
            "hypotheses 必须是可迭代的假说集合（对象、字典或标识字符串），"
            f"实际得到单个 {type(hypotheses).__name__}。"
        )
    try:
        items = list(hypotheses)
    except TypeError as exc:  # pragma: no cover - 由上方类型检查兜住
        raise AcquisitionInputError(f"hypotheses 不可迭代：{exc}") from exc
    if len(items) < 2:
        raise AcquisitionInputError(
            f"至少需要 2 个竞争假说才能比较预测分歧，实际得到 {len(items)} 个。"
            "单个假说之间不存在「分歧」这一概念。"
        )
    identities = tuple(_identity_of(item) for item in items)
    seen: set[str] = set()
    duplicates: list[str] = []
    for identity in identities:
        if identity in seen and identity not in duplicates:
            duplicates.append(identity)
        seen.add(identity)
    if duplicates:
        raise AcquisitionInputError(
            f"竞争假说出现重复标识：{duplicates}。重复标识会让分歧计算重复计数，"
            "请先去重后再调用。"
        )
    return identities


# ---------------------------------------------------------------------------
# 输入规范化：建议观测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Observation:
    """内部规范化的建议观测。

    :param observation_id: 观测标识（不得重复）。
    :param predictions: 假说标识 → 预测值（``None`` 表示未给出预测）。
    :param sampling_cost: 采样成本（正实数；未给出为 ``None``）。
    :param boundary_distance: 与适用边界的距离（未给出为 ``None``）。
    :param explicit_near_boundary: 调用方显式给出的边界接近标记（未给出为 ``None``）。
    :param payload: JSON 兼容的原始明细，用于序列化。
    :param input_index: 输入序号，用于确定性排序。
    """

    observation_id: str
    predictions: Mapping[str, float | None]
    sampling_cost: float | None
    boundary_distance: float | None
    explicit_near_boundary: bool | None
    payload: Mapping[str, Any]
    input_index: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "predictions", MappingProxyType(dict(self.predictions))
        )
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def near_boundary(self) -> bool:
        """是否接近适用边界。

        显式标记优先于由距离推导；两者都未给出时为 ``False``（不猜测）。
        """
        if self.explicit_near_boundary is not None:
            return self.explicit_near_boundary
        if self.boundary_distance is None:
            return False
        return self.boundary_distance <= BOUNDARY_PROXIMITY_EPSILON


def _read_predictions(
    value: Any, hypotheses: tuple[str, ...], label: str
) -> dict[str, float | None]:
    """规范化预测映射，并拒绝指向未知假说的预测。"""
    mapping = _require_mapping(value, label)
    _reject_token_keys(mapping, label)
    _reject_sealed_keys(mapping, label)
    known = set(hypotheses)
    unknown = sorted(str(key) for key in mapping if str(key) not in known)
    if unknown:
        raise AcquisitionInputError(
            f"{label} 出现未在 hypotheses 中声明的标识：{unknown}。"
            "分歧只能在同一批竞争假说之间比较，指向未知假说的预测说明调用方"
            "的两份输入不同源，请先对齐。"
        )
    out: dict[str, float | None] = {}
    for key, item in mapping.items():
        name = str(key)
        if item is None:
            out[name] = None
            continue
        out[name] = _require_finite_float(item, f"{label}[{name!r}]")
    return out


def _normalize_observations(
    observations: Any, hypotheses: tuple[str, ...]
) -> tuple[_Observation, ...]:
    """把建议观测集合规范化为 :class:`_Observation` 元组。"""
    if isinstance(observations, (str, bytes, Mapping)) or observations is None:
        raise AcquisitionInputError(
            "observations 必须是可迭代的建议观测集合（映射或对象），"
            f"实际得到：{observations!r}（类型 {type(observations).__name__}）。"
        )
    try:
        items = list(observations)
    except TypeError as exc:  # pragma: no cover - 由上方类型检查兜住
        raise AcquisitionInputError(f"observations 不可迭代：{exc}") from exc

    normalized: list[_Observation] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        label = f"observations[{index}]"
        if isinstance(item, Mapping):
            body: Mapping[str, Any] = item
        else:
            body = {
                name: getattr(item, name, None)
                for name in (
                    "observation_id",
                    "id",
                    "predictions",
                    "predicted",
                    "sampling_cost",
                    "cost",
                    "boundary_distance",
                    "near_boundary",
                )
            }
        _reject_token_keys(body, label)
        _reject_sealed_keys(body, label)

        raw_id = body.get("observation_id", body.get("id"))
        observation_id = _require_text(raw_id, f"{label}.observation_id")
        if observation_id in seen:
            raise AcquisitionInputError(
                f"{label}.observation_id 与 observations[{seen[observation_id]}] "
                f"重复：{observation_id!r}。同一批次内重复标识会重复占用预算。"
            )
        seen[observation_id] = index

        raw_predictions = body.get("predictions", body.get("predicted"))
        if raw_predictions is None:
            raise AcquisitionInputError(
                f"{label} 缺少 predictions：分歧计算要求每条建议观测都写明"
                "各竞争假说在该观测上的预测值（无法给出者可显式写 None，"
                "表示「未给出预测」，与「预测值为 0」不同）。"
            )
        predictions = _read_predictions(
            raw_predictions, hypotheses, f"{label}.predictions"
        )

        raw_cost = body.get("sampling_cost", body.get("cost"))
        sampling_cost: float | None
        if raw_cost is None:
            sampling_cost = None
        else:
            sampling_cost = _require_finite_float(raw_cost, f"{label}.sampling_cost")
            if sampling_cost <= 0:
                raise AcquisitionInputError(
                    f"{label}.sampling_cost 必须为正数，实际得到 {sampling_cost!r}："
                    "非正成本使「分歧 / 成本」失去意义（见 MISSING_COST_POLICY）。"
                )

        raw_distance = body.get("boundary_distance")
        boundary_distance: float | None
        if raw_distance is None:
            boundary_distance = None
        else:
            boundary_distance = _require_finite_float(
                raw_distance, f"{label}.boundary_distance"
            )
            if boundary_distance < 0:
                raise AcquisitionInputError(
                    f"{label}.boundary_distance 必须非负，实际得到 "
                    f"{boundary_distance!r}。"
                )

        raw_near = body.get("near_boundary")
        explicit_near: bool | None
        if raw_near is None:
            explicit_near = None
        elif isinstance(raw_near, bool):
            explicit_near = raw_near
        else:
            raise AcquisitionInputError(
                f"{label}.near_boundary 必须是布尔值，实际得到：{raw_near!r}"
            )

        normalized.append(
            _Observation(
                observation_id=observation_id,
                predictions=predictions,
                sampling_cost=sampling_cost,
                boundary_distance=boundary_distance,
                explicit_near_boundary=explicit_near,
                payload=_jsonable(dict(body)),
                input_index=index,
            )
        )
    return tuple(normalized)


# ---------------------------------------------------------------------------
# 结果对象：逐观测分歧
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationDivergence:
    """单条建议观测上的竞争假说预测分歧（启发式）。

    :param observation_id: 观测标识。
    :param divergence: 原始离散度（最大值 − 最小值）；不可计算时为 ``None``。
    :param normalized_divergence: 按预测量级归一化后的离散度；不可计算时为 ``None``。
    :param participating: 给出数值预测的假说标识（保序）。
    :param missing_predictions: 未给出数值预测的假说标识（保序）。
    :param sampling_cost: 采样成本；未给出为 ``None``。
    :param heuristic_value: 启发式比值（分歧 ÷ 成本）；不可计算时为 ``None``。
    :param boundary_distance: 与适用边界的距离；未给出为 ``None``。
    :param near_boundary: 是否接近适用边界。
    :param value_mode: 参与排序的分歧口径（``spread`` / ``normalized``）。
    :param note: 口径说明。
    """

    observation_id: str
    divergence: float | None
    normalized_divergence: float | None
    participating: tuple[str, ...]
    missing_predictions: tuple[str, ...]
    sampling_cost: float | None
    heuristic_value: float | None
    boundary_distance: float | None
    near_boundary: bool
    value_mode: str
    note: str = HEURISTIC_DEFINITION_NOTE

    @property
    def comparable(self) -> bool:
        """分歧是否**可计算**（区别于「算出来是 0」）。"""
        return self.divergence is not None

    @property
    def unanimous(self) -> bool:
        """各假说预测是否**确实一致**（算出来的 0，而非没算）。"""
        return self.divergence is not None and self.divergence <= COMPARISON_TOLERANCE

    @property
    def rankable(self) -> bool:
        """是否具备参与排序的完整信息（分歧可算且成本已知）。"""
        return self.heuristic_value is not None

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（含启发式口径声明）。"""
        return {
            "acquisition_version": ACQUISITION_VERSION,
            "observation_id": self.observation_id,
            "divergence": self.divergence,
            "normalized_divergence": self.normalized_divergence,
            "participating": list(self.participating),
            "missing_predictions": list(self.missing_predictions),
            "sampling_cost": self.sampling_cost,
            "heuristic_value": self.heuristic_value,
            "boundary_distance": self.boundary_distance,
            "near_boundary": self.near_boundary,
            "value_mode": self.value_mode,
            "comparable": self.comparable,
            "unanimous": self.unanimous,
            "rankable": self.rankable,
            "ranking_basis": RANKING_BASIS_HEURISTIC,
            "is_heuristic": ACQUISITION_RANKING_IS_HEURISTIC,
            "is_expected_information_gain": False,
            "note": self.note,
        }


@dataclass(frozen=True)
class DivergenceReport:
    """一批建议观测上的分歧报告（启发式，非预期信息增益）。

    :param entries: 逐观测分歧，保输入顺序。
    :param hypotheses: 参与比较的竞争假说标识。
    :param value_mode: 参与排序的分歧口径。
    :param note: 口径说明。
    """

    entries: tuple[ObservationDivergence, ...]
    hypotheses: tuple[str, ...]
    value_mode: str
    note: str = RANKING_NOT_EIG_NOTE

    # -- 统计视图 --------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[ObservationDivergence]:
        return iter(self.entries)

    @property
    def comparable_ids(self) -> tuple[str, ...]:
        """分歧可计算的观测标识。"""
        return tuple(e.observation_id for e in self.entries if e.comparable)

    @property
    def undetermined_ids(self) -> tuple[str, ...]:
        """**分歧不可计算**（没算）的观测标识。"""
        return tuple(e.observation_id for e in self.entries if not e.comparable)

    @property
    def unanimous_ids(self) -> tuple[str, ...]:
        """**分歧算出来是 0**（各假说预测一致）的观测标识。"""
        return tuple(e.observation_id for e in self.entries if e.unanimous)

    @property
    def unknown_cost_ids(self) -> tuple[str, ...]:
        """采样成本未知的观测标识。"""
        return tuple(
            e.observation_id for e in self.entries if e.sampling_cost is None
        )

    def entry_of(self, observation_id: str) -> ObservationDivergence:
        """按标识取条目。"""
        for entry in self.entries:
            if entry.observation_id == observation_id:
                return entry
        raise AcquisitionInputError(f"报告中不存在观测 {observation_id!r}")

    def ranked_entries(self) -> tuple[ObservationDivergence, ...]:
        """按启发式口径排序的条目（可排序者在前，其余按标识字典序殿后）。"""
        return tuple(sorted(self.entries, key=_plan_sort_key))

    def ranked_ids(self) -> tuple[str, ...]:
        """按启发式口径排序的观测标识。"""
        return tuple(e.observation_id for e in self.ranked_entries())

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（恒含启发式声明）。"""
        return {
            "acquisition_version": ACQUISITION_VERSION,
            "hypotheses": list(self.hypotheses),
            "value_mode": self.value_mode,
            "entries": [entry.to_dict() for entry in self.entries],
            "comparable_ids": list(self.comparable_ids),
            "undetermined_ids": list(self.undetermined_ids),
            "unanimous_ids": list(self.unanimous_ids),
            "unknown_cost_ids": list(self.unknown_cost_ids),
            "ranking_basis": RANKING_BASIS_HEURISTIC,
            "is_heuristic": ACQUISITION_RANKING_IS_HEURISTIC,
            "is_expected_information_gain": False,
            "ranking_note": RANKING_NOT_EIG_NOTE,
            "heuristic_definition_note": HEURISTIC_DEFINITION_NOTE,
            "missing_prediction_policy": MISSING_PREDICTION_POLICY,
            "missing_cost_policy": MISSING_COST_POLICY,
            "boundary_tiebreak_note": BOUNDARY_TIEBREAK_NOTE,
            "plan_is_advisory_note": PLAN_IS_ADVISORY_NOTE,
            "acquisition_grants_evidence_grade": ACQUISITION_GRANTS_EVIDENCE_GRADE,
            "note": self.note,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与摘要。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """结果摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"DivergenceReport(entries={len(self.entries)}, "
            f"comparable={len(self.comparable_ids)}, "
            f"undetermined={len(self.undetermined_ids)}, "
            f"hypotheses={len(self.hypotheses)})"
        )


# ---------------------------------------------------------------------------
# 入口一：分歧计算
# ---------------------------------------------------------------------------


def divergence(
    hypotheses: Any,
    observations: Any,
    *,
    value_mode: str = VALUE_MODE_SPREAD,
    **rejected: Any,
) -> DivergenceReport:
    """逐观测计算竞争假说之间的**启发式**预测分歧（验收标准①）。

    计算规则（唯一、显式）：

    1. 在每条观测上取出各竞争假说给出的**数值**预测；显式 ``None`` 与完全未给出
       一律计为「未给出预测」，不插补。
    2. 给出数值预测的假说少于 2 个时，分歧**不可计算**，记 ``None``，
       并在 ``undetermined_ids`` 中如实上报——**不是 0**。
    3. 否则原始分歧 = 预测最大值 − 预测最小值；各假说预测完全相同时，
       这是**算出来的 0**，表示该观测无法区分这些假说。
    4. 归一化分歧 = 原始分歧 ÷ (预测绝对值的最大者 + ε)，用于跨尺度诊断，
       落在 ``[0, 2]``（当最大绝对值为正时）。

    本函数**不是**在算预期信息增益：它没有假说先验概率、没有观测似然模型、
    没有信息量定义（见 :data:`RANKING_NOT_EIG_NOTE`）。任何试图传入
    假说概率、观测模型、信息量或加权总分的参数都会在调用点被拒绝。

    :param hypotheses: 竞争假说集合（对象、字典或标识字符串），至少 2 个。
    :param observations: 建议观测集合；每条须含 ``observation_id`` 与
        ``predictions``，可选 ``sampling_cost`` / ``boundary_distance`` /
        ``near_boundary``。
    :param value_mode: 参与排序的分歧口径，取 ``spread``（默认）或 ``normalized``。
    :return: :class:`DivergenceReport`。
    :raises AcquisitionInputError: 输入形态非法、标识重复、成本非正等。
    :raises AcquisitionPolicyError: 试图引入 EIG 类参数，或输入含封存记录字段。
    """
    _reject_unknown_kwargs(rejected, "divergence")
    if value_mode not in VALUE_MODES:
        raise AcquisitionInputError(
            f"value_mode 必须是 {list(VALUE_MODES)} 之一，实际得到：{value_mode!r}"
        )
    identities = _normalize_hypotheses(hypotheses)
    normalized = _normalize_observations(observations, identities)

    entries: list[ObservationDivergence] = []
    for observation in normalized:
        values: list[tuple[str, float]] = [
            (name, float(value))
            for name, value in observation.predictions.items()
            if value is not None
        ]
        participating = tuple(name for name, _ in values)
        missing = tuple(
            name for name in identities if name not in set(participating)
        )

        raw_divergence: float | None
        normalized_divergence: float | None
        if len(values) < 2:
            raw_divergence = UNDETERMINED_DIVERGENCE
            normalized_divergence = UNDETERMINED_DIVERGENCE
        else:
            numbers = [value for _, value in values]
            spread = max(numbers) - min(numbers)
            scale = max(abs(value) for value in numbers)
            raw_divergence = spread if spread > COMPARISON_TOLERANCE else 0.0
            normalized_divergence = (
                raw_divergence / (scale + DIVERGENCE_EPSILON) if scale > 0 else 0.0
            )

        basis = (
            raw_divergence if value_mode == VALUE_MODE_SPREAD else normalized_divergence
        )
        heuristic_value: float | None
        if basis is None or observation.sampling_cost is None:
            heuristic_value = None
        else:
            heuristic_value = basis / observation.sampling_cost

        entries.append(
            ObservationDivergence(
                observation_id=observation.observation_id,
                divergence=raw_divergence,
                normalized_divergence=normalized_divergence,
                participating=participating,
                missing_predictions=missing,
                sampling_cost=observation.sampling_cost,
                heuristic_value=heuristic_value,
                boundary_distance=observation.boundary_distance,
                near_boundary=observation.near_boundary,
                value_mode=value_mode,
            )
        )

    return DivergenceReport(
        entries=tuple(entries),
        hypotheses=identities,
        value_mode=value_mode,
    )


# ---------------------------------------------------------------------------
# 结果对象：取证计划
# ---------------------------------------------------------------------------


def _plan_sort_key(entry: ObservationDivergence) -> tuple:
    """计划的确定性排序键。

    依次比较：

    1. 该观测是否具备完整的排序信息（分歧可算且成本已知）；
    2. 启发式比值**降序**（分歧大、成本低者优先）；
    3. 成本是否已知（已知者优先，避免未标价者挤占预算判断）；
    4. 是否接近适用边界（**平局裁决**，不参与加权）；
    5. 观测标识字典序（最终确定性裁决）。
    """
    determined = entry.heuristic_value is not None
    return (
        0 if determined else 1,
        -(entry.heuristic_value if determined else 0.0),
        0 if entry.sampling_cost is not None else 1,
        0 if entry.near_boundary else 1,
        entry.observation_id,
    )


def _validate_pre_sampling_freeze(value: Any, purpose: str) -> Mapping[str, Any]:
    """校验「采样前冻结」声明（验收标准②）。

    :raises AcquisitionPolicyError: 缺少任一笔填项，或标记位不为 ``True``。
    """
    body = _require_mapping(value, "pre_sampling_freeze")
    _reject_token_keys(body, "pre_sampling_freeze")
    _reject_sealed_keys(body, "pre_sampling_freeze")

    absent = [
        key for key in PRE_SAMPLING_FREEZE_REQUIRED_KEYS if body.get(key) is None
    ]
    if absent:
        raise AcquisitionPolicyError(
            f"取证目的 {purpose!r} 属确证类，pre_sampling_freeze 缺少必填项 "
            f"{absent}：必须在采样前固定候选与采样、检验方案"
            "（框架说明 §2 末段与 §6）。缺任一项即拒绝出计划。"
        )

    flag = body.get(FREEZE_BEFORE_SAMPLING_FLAG)
    if flag is not True:
        raise AcquisitionPolicyError(
            f"pre_sampling_freeze.{FREEZE_BEFORE_SAMPLING_FLAG} 必须字面为 True，"
            f"实际得到：{flag!r}。该标记位用于声明候选与方案在采样**之前**已固定；"
            "false 或其它取值意味着顺序无法保证，「先看数据再定标准」的口子会打开。"
        )

    candidates = body.get("frozen_candidates")
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise AcquisitionPolicyError(
            "pre_sampling_freeze.frozen_candidates 必须是非空的候选标识序列，"
            f"实际得到：{candidates!r}"
        )
    frozen_candidates = [_require_text(item, "frozen_candidates 中的候选标识") for item in candidates]
    if not frozen_candidates:
        raise AcquisitionPolicyError(
            "pre_sampling_freeze.frozen_candidates 不得为空："
            "「固定候选」至少要指明固定了哪一个候选。"
        )

    protocol = _require_mapping(
        body.get("frozen_protocol"), "pre_sampling_freeze.frozen_protocol"
    )
    _reject_sealed_keys(protocol, "pre_sampling_freeze.frozen_protocol")
    if not protocol:
        raise AcquisitionPolicyError(
            "pre_sampling_freeze.frozen_protocol 不得为空："
            "采样与检验方案必须写明（怎么采样、怎么检验、阈值为何）。"
        )

    return MappingProxyType(
        {
            "frozen_candidates": tuple(frozen_candidates),
            "frozen_protocol": MappingProxyType(dict(protocol)),
            "freeze_before_sampling": True,
            "frozen_at": body.get("frozen_at"),
            "error_budget": body.get("error_budget"),
            "stopping_rule": body.get("stopping_rule"),
        }
    )


@dataclass(frozen=True)
class AcquisitionItem:
    """取证计划中的一条建议。

    :param rank: 计划内序号（从 0 开始，按启发式口径排序）。
    :param observation_id: 观测标识。
    :param heuristic_value: 启发式比值（分歧 ÷ 成本）。
    :param divergence: 原始离散度。
    :param sampling_cost: 采样成本。
    :param near_boundary: 是否接近适用边界。
    :param purpose: 本条所属的取证目的。
    :param pre_sampling_freeze: 采样前冻结声明（探索类未提供时为 ``None``）。
    :param rationale: 入选依据的自然语言说明。
    """

    rank: int
    observation_id: str
    heuristic_value: float | None
    divergence: float | None
    sampling_cost: float | None
    near_boundary: bool
    purpose: str
    pre_sampling_freeze: Mapping[str, Any] | None
    rationale: str

    @property
    def confirmation_grade(self) -> bool:
        """本条是否属确证类取证（受「采样前冻结」约束）。"""
        return self.purpose in CONFIRMATION_GRADE_PURPOSES

    @property
    def freeze_declared(self) -> bool:
        """本条是否已携带采样前冻结声明。"""
        return self.pre_sampling_freeze is not None

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（含启发式声明）。"""
        return {
            "acquisition_version": ACQUISITION_VERSION,
            "rank": self.rank,
            "observation_id": self.observation_id,
            "heuristic_value": self.heuristic_value,
            "divergence": self.divergence,
            "sampling_cost": self.sampling_cost,
            "near_boundary": self.near_boundary,
            "purpose": self.purpose,
            "confirmation_grade": self.confirmation_grade,
            "freeze_declared": self.freeze_declared,
            "pre_sampling_freeze": _jsonable(self.pre_sampling_freeze),
            "rationale": self.rationale,
            "ranking_basis": RANKING_BASIS_HEURISTIC,
            "is_heuristic": ACQUISITION_RANKING_IS_HEURISTIC,
        }


@dataclass(frozen=True)
class AcquisitionPlan:
    """下一轮取证计划（**纯建议对象**，验收标准③）。

    :param items: 入选建议（按启发式口径排序）。
    :param purpose: 本轮取证目的。
    :param value_mode: 排序所依据的分歧口径。
    :param hypotheses: 参与比较的竞争假说标识。
    :param max_items: 本条计划的条目数上限。
    :param cost_budget: 采样成本预算（``None`` 表示不设限）。
    :param spent_cost: 入选建议消耗的成本合计。
    :param excluded_undetermined: 因分歧不可计算（没算）被排除的观测。
    :param excluded_unknown_cost: 因采样成本未知被排除的观测。
    :param excluded_over_budget: 因超出成本预算被排除的观测。
    :param excluded_over_limit: 因超出条目数上限被排除的观测。
    :param pre_sampling_freeze: 采样前冻结声明（确证类取证必有）。
    :param notes: 口径说明元组。
    """

    items: tuple[AcquisitionItem, ...]
    purpose: str
    value_mode: str
    hypotheses: tuple[str, ...]
    max_items: int
    cost_budget: float | None
    spent_cost: float
    excluded_undetermined: tuple[str, ...]
    excluded_unknown_cost: tuple[str, ...]
    excluded_over_budget: tuple[str, ...]
    excluded_over_limit: tuple[str, ...]
    pre_sampling_freeze: Mapping[str, Any] | None
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.pre_sampling_freeze is not None:
            object.__setattr__(
                self,
                "pre_sampling_freeze",
                MappingProxyType(dict(self.pre_sampling_freeze)),
            )

    # -- 常量属性（验收标准③的机检锚点） --------------------------------

    @property
    def executes_collection(self) -> bool:
        """本计划是否触发数据获取。恒为 ``False``。"""
        return ACQUISITION_PLAN_EXECUTES_COLLECTION

    @property
    def is_advisory(self) -> bool:
        """本计划是否为纯建议对象。恒为 ``True``。"""
        return not ACQUISITION_PLAN_EXECUTES_COLLECTION

    @property
    def grants_evidence_grade(self) -> bool:
        """本计划是否授予证据等级。恒为 ``False``。"""
        return ACQUISITION_GRANTS_EVIDENCE_GRADE

    @property
    def ranking_basis(self) -> str:
        """排序依据。恒为 ``heuristic``。"""
        return RANKING_BASIS_HEURISTIC

    @property
    def is_heuristic(self) -> bool:
        """排序值是否为启发式。恒为 ``True``。"""
        return ACQUISITION_RANKING_IS_HEURISTIC

    @property
    def requires_pre_sampling_freeze(self) -> bool:
        """本轮计划是否受「采样前冻结」约束（验收标准②）。"""
        return self.purpose in CONFIRMATION_GRADE_PURPOSES

    @property
    def pre_sampling_freeze_declared(self) -> bool:
        """本轮确证类计划是否已携带采样前冻结声明。"""
        if not self.requires_pre_sampling_freeze:
            return False
        return self.pre_sampling_freeze is not None

    # -- 视图 -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[AcquisitionItem]:
        return iter(self.items)

    def ids(self) -> tuple[str, ...]:
        """入选观测标识（按排序）。"""
        return tuple(item.observation_id for item in self.items)

    def remaining_budget(self) -> float | None:
        """剩余成本预算（未设预算时为 ``None``）。"""
        if self.cost_budget is None:
            return None
        return self.cost_budget - self.spent_cost

    def frozen_candidate_ids(self) -> tuple[str, ...]:
        """本次冻结的候选标识（未声明冻结时为空元组）。"""
        if self.pre_sampling_freeze is None:
            return ()
        return tuple(self.pre_sampling_freeze.get("frozen_candidates", ()))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（恒含启发式与建议定位声明）。"""
        return {
            "acquisition_version": ACQUISITION_VERSION,
            "items": [item.to_dict() for item in self.items],
            "count": len(self.items),
            "purpose": self.purpose,
            "purpose_requirement": PURPOSE_FREEZE_REQUIREMENT[self.purpose],
            "value_mode": self.value_mode,
            "hypotheses": list(self.hypotheses),
            "max_items": self.max_items,
            "cost_budget": self.cost_budget,
            "spent_cost": self.spent_cost,
            "remaining_budget": self.remaining_budget(),
            "excluded_undetermined": list(self.excluded_undetermined),
            "excluded_unknown_cost": list(self.excluded_unknown_cost),
            "excluded_over_budget": list(self.excluded_over_budget),
            "excluded_over_limit": list(self.excluded_over_limit),
            "pre_sampling_freeze": _jsonable(self.pre_sampling_freeze),
            "requires_pre_sampling_freeze": self.requires_pre_sampling_freeze,
            "pre_sampling_freeze_declared": self.pre_sampling_freeze_declared,
            "frozen_candidate_ids": list(self.frozen_candidate_ids()),
            "ranking_basis": RANKING_BASIS_HEURISTIC,
            "is_heuristic": ACQUISITION_RANKING_IS_HEURISTIC,
            "is_expected_information_gain": False,
            "executes_collection": ACQUISITION_PLAN_EXECUTES_COLLECTION,
            "is_advisory": self.is_advisory,
            "grants_evidence_grade": ACQUISITION_GRANTS_EVIDENCE_GRADE,
            "pre_sampling_freeze_note": PRE_SAMPLING_FREEZE_NOTE,
            "plan_is_advisory_note": PLAN_IS_ADVISORY_NOTE,
            "ranking_note": RANKING_NOT_EIG_NOTE,
            "boundary_tiebreak_note": BOUNDARY_TIEBREAK_NOTE,
            "item_note": ACQUISITION_ITEM_NOTE,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与摘要。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """结果摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"AcquisitionPlan(purpose={self.purpose!r}, items={len(self.items)}, "
            f"excluded={len(self.excluded_undetermined) + len(self.excluded_unknown_cost) + len(self.excluded_over_budget) + len(self.excluded_over_limit)})"
        )


# ---------------------------------------------------------------------------
# 入口二：取证计划
# ---------------------------------------------------------------------------


def _validate_purpose(purpose: Any) -> str:
    """校验取证目的落在受控词表内。"""
    if not isinstance(purpose, str) or purpose not in ACQUISITION_PURPOSES:
        raise AcquisitionInputError(
            f"purpose 必须是 {list(ACQUISITION_PURPOSES)} 之一，"
            f"实际得到：{purpose!r}"
        )
    return purpose


def _validate_max_items(max_items: Any) -> int:
    """校验条目数上限。"""
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise AcquisitionInputError(
            f"max_items 必须是整数，实际得到：{max_items!r}"
        )
    if max_items < 1:
        raise AcquisitionInputError(
            f"max_items 必须至少为 1，实际得到：{max_items!r}"
        )
    return max_items


def _validate_cost_budget(cost_budget: Any) -> float | None:
    """校验成本预算。"""
    if cost_budget is None:
        return None
    budget = _require_finite_float(cost_budget, "cost_budget")
    if budget <= 0:
        raise AcquisitionInputError(
            f"cost_budget 必须为正数，实际得到：{cost_budget!r}"
        )
    return budget


def acquisition_plan(
    source: DivergenceReport,
    *,
    purpose: str = PURPOSE_EXPLORATION,
    max_items: int = DEFAULT_MAX_ITEMS,
    cost_budget: float | None = None,
    pre_sampling_freeze: Mapping[str, Any] | None = None,
    **rejected: Any,
) -> AcquisitionPlan:
    """依「分歧 / 成本」排序并施加预算，产出下一轮取证计划。

    排序键是**单一显式比值**（分歧 ÷ 采样成本）加字典序平局裁决，
    **不是**跨维度加权总分；「接近适用边界」只作平局裁决，不被折算成分数
    （见 :data:`BOUNDARY_TIEBREAK_NOTE`）。

    入选条件是**三项同时成立**，任一不成立者单列上报、不插补：

    - 分歧可计算（不可算者入 ``excluded_undetermined``）；
    - 采样成本已知（未知者入 ``excluded_unknown_cost``；
      缺失成本不被当作 0 成本，见 :data:`MISSING_COST_POLICY`）；
    - 未超出 ``cost_budget`` 与 ``max_items``
      （超出者分别入 ``excluded_over_budget`` / ``excluded_over_limit``）。

    当 ``purpose`` 属确证类（:data:`CONFIRMATION_GRADE_PURPOSES`）时，
    ``pre_sampling_freeze`` **必填**且逐项校验（验收标准②）。
    本函数**不提供**关闭该校验的参数。

    本函数**不执行任何采集、不登记任何结果、不授予任何证据等级**
    （验收标准③）。

    :param source: :func:`divergence` 的返回值。
    :param purpose: 取证目的，取 :data:`ACQUISITION_PURPOSES` 之一。
    :param max_items: 条目数上限，至少为 1。
    :param cost_budget: 采样成本预算；``None`` 表示不设限。
    :param pre_sampling_freeze: 采样前冻结声明；确证类必填。
    :return: :class:`AcquisitionPlan`。
    :raises AcquisitionInputError: 输入形态非法或参数越界。
    :raises AcquisitionPolicyError: 确证类缺少采样前冻结声明，或试图引入 EIG 类参数。
    """
    _reject_unknown_kwargs(rejected, "acquisition_plan")
    if not isinstance(source, DivergenceReport):
        raise AcquisitionInputError(
            "source 必须是 divergence() 返回的 DivergenceReport，"
            f"实际得到：{type(source).__name__}。"
            "分歧必须在同一份报告内计算，不能在本层重算或换个口径。"
        )
    purpose = _validate_purpose(purpose)
    max_items = _validate_max_items(max_items)
    budget = _validate_cost_budget(cost_budget)

    needs_freeze = purpose in CONFIRMATION_GRADE_PURPOSES
    frozen: Mapping[str, Any] | None
    if pre_sampling_freeze is None:
        if needs_freeze:
            raise AcquisitionPolicyError(
                f"取证目的 {purpose!r} 属确证类，必须提供 pre_sampling_freeze："
                "涉及确证的数据采集，必须在采样前固定候选及采样、检验方案"
                f"（框架说明 §2 末段）。必填项："
                f"{list(PRE_SAMPLING_FREEZE_REQUIRED_KEYS)}。本模块不提供跳过该校验的参数。"
            )
        frozen = None
    else:
        frozen = _validate_pre_sampling_freeze(pre_sampling_freeze, purpose)

    excluded_undetermined: list[str] = []
    excluded_unknown_cost: list[str] = []
    excluded_over_budget: list[str] = []
    excluded_over_limit: list[str] = []

    items: list[AcquisitionItem] = []
    spent = 0.0
    seen: set[str] = set()

    for entry in source.ranked_entries():
        if entry.divergence is None:
            excluded_undetermined.append(entry.observation_id)
            continue
        if entry.sampling_cost is None or entry.heuristic_value is None:
            excluded_unknown_cost.append(entry.observation_id)
            continue
        if len(items) >= max_items:
            excluded_over_limit.append(entry.observation_id)
            continue
        if budget is not None and spent + entry.sampling_cost > budget + COMPARISON_TOLERANCE:
            excluded_over_budget.append(entry.observation_id)
            continue

        rank = len(items)
        rationale = _rationale_for(entry, purpose, rank)
        items.append(
            AcquisitionItem(
                rank=rank,
                observation_id=entry.observation_id,
                heuristic_value=entry.heuristic_value,
                divergence=entry.divergence,
                sampling_cost=entry.sampling_cost,
                near_boundary=entry.near_boundary,
                purpose=purpose,
                pre_sampling_freeze=frozen,
                rationale=rationale,
            )
        )
        spent += entry.sampling_cost
        seen.add(entry.observation_id)

    # 自检：排除清单与入选清单互斥且并集恰为报告全体观测。
    # 用显式校验而不是 assert，避免断言在 -O 下被剥离后失去保护。
    covered = set(seen) | set(excluded_undetermined) | set(excluded_unknown_cost)
    covered |= set(excluded_over_budget) | set(excluded_over_limit)
    all_ids = {entry.observation_id for entry in source.entries}
    if covered != all_ids:
        raise AcquisitionInputError(
            "内部对账失败：入选与排除清单的并集与报告观测集合不一致"
            f"（差异：{sorted(all_ids ^ covered)}）。"
        )

    notes = (
        RANKING_NOT_EIG_NOTE,
        HEURISTIC_DEFINITION_NOTE,
        BOUNDARY_TIEBREAK_NOTE,
        MISSING_PREDICTION_POLICY,
        MISSING_COST_POLICY,
        PLAN_IS_ADVISORY_NOTE,
        PRE_SAMPLING_FREEZE_NOTE,
        PURPOSE_FREEZE_REQUIREMENT[purpose],
        ACQUISITION_ITEM_NOTE,
        (
            "排序键：启发式比值（分歧 ÷ 采样成本）降序 → 成本已知者优先 → "
            "更接近边界者优先（平局裁决） → 观测标识字典序。"
            "全程无任何跨维度加权。"
        ),
    )

    return AcquisitionPlan(
        items=tuple(items),
        purpose=purpose,
        value_mode=source.value_mode,
        hypotheses=source.hypotheses,
        max_items=max_items,
        cost_budget=budget,
        spent_cost=spent,
        excluded_undetermined=tuple(excluded_undetermined),
        excluded_unknown_cost=tuple(excluded_unknown_cost),
        excluded_over_budget=tuple(excluded_over_budget),
        excluded_over_limit=tuple(excluded_over_limit),
        pre_sampling_freeze=frozen,
        notes=notes,
    )


def _rationale_for(entry: ObservationDivergence, purpose: str, rank: int) -> str:
    """为入选条目生成入选依据。"""
    parts = [
        f"按启发式比值 {entry.heuristic_value:.6g} 排序入选（第 {rank + 1} 位）",
        f"分歧 {entry.divergence:.6g}（{len(entry.participating)} 个假说给出预测）",
        f"采样成本 {entry.sampling_cost:.6g}",
    ]
    if entry.near_boundary:
        parts.append("接近适用边界（仅作平局裁决，未参与加权）")
    if entry.missing_predictions:
        parts.append(
            f"有 {len(entry.missing_predictions)} 个假说未给出预测（未插补，"
            "因此实际可比较的假说数少于全部）"
        )
    if purpose in CONFIRMATION_GRADE_PURPOSES:
        parts.append("确证类取证：候选与采样、检验方案已在采样前冻结")
    else:
        parts.append("探索类取证：不进入确证链条")
    return "；".join(parts) + "。"


# ---------------------------------------------------------------------------
# 边界守护：本模块不提供的数据访问与越界命名
# ---------------------------------------------------------------------------


def _forbidden_access_names() -> tuple[str, ...]:
    """本模块**不应存在**的数据访问入口名（验收标准③）。

    与 :data:`NOT_PROVIDED_BY_P14` 中列出的越界接口不同，本集合聚焦「读取与采集」：
    本模块只消费调用方给出的假说标识与建议观测规格，没有任何触碰数据的路径。
    测试用 AST 检查本模块是否定义了这些名字、或访问了这些属性——
    用 AST 而非源码文本匹配，避免把「边界声明本身」当成违规。
    """
    return (
        "read_dataset",
        "read_records",
        "load",
        "load_snapshot",
        "open_vault",
        "quality",
        "connect",
        "sqlite3",
        "fetch",
        "collect",
        "acquire_data",
        "request_data",
        "run_acquisition",
        "consume_confirmation",
    )


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查三项：

    1. 模块是否定义了 :data:`FORBIDDEN_RANKING_NAMES` 中的任何名字——
       那是把启发式比值冒充成预期信息增益（验收标准①）；
    2. 模块是否定义了 :data:`NOT_PROVIDED_BY_P14` 中的任何名字（越界实现）；
    3. 模块是否出现 :func:`_forbidden_access_names` 中的属性访问或导入
       （越界读取与采集，验收标准③）。

    刻意用 AST 而不是源码文本匹配：本模块的 docstring 与常量里**必须**写出
    ``read_dataset`` / ``expected_information_gain`` 等被禁名字（那是边界声明），
    文本匹配会把边界声明本身判成违规。

    :return: ``{"defined_forbidden_ranking", "defined_other_stages",
        "accessed_forbidden", "ok", "checked_names"}``。
    """
    source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
    tree = _ast.parse(source_path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
    }
    accessed: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, _ast.Name):
            accessed.add(node.id)
        elif isinstance(node, _ast.alias):
            accessed.add(node.name.split(".")[-1])

    defined_forbidden_ranking = tuple(
        sorted(defined & set(FORBIDDEN_RANKING_NAMES))
    )
    defined_other_stages = tuple(sorted(defined & set(NOT_PROVIDED_BY_P14)))
    accessed_forbidden = tuple(
        sorted(
            accessed
            & (set(_forbidden_access_names()) - set(NOT_PROVIDED_BY_P14))
        )
    )
    return {
        "defined_forbidden_ranking": defined_forbidden_ranking,
        "defined_other_stages": defined_other_stages,
        "accessed_forbidden": accessed_forbidden,
        "ok": not (
            defined_forbidden_ranking or defined_other_stages or accessed_forbidden
        ),
        "checked_names": len(NOT_PROVIDED_BY_P14) + len(FORBIDDEN_RANKING_NAMES),
    }
