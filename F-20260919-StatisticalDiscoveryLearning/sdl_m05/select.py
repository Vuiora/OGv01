"""P10 交付物：Pareto 筛选与候选池管理（模块 M5）。

本模块是**探索阶段的决策层**：它把 P09 并列产出的四个指标（G/S/N/C）
组合成筛选结论。组合方式**只有一种**——Pareto 支配关系（偏序），
不引入任何跨维度加权总分。理论依据见
`SDL算法框架说明.md <../SDL算法框架说明.md>`_ §4：

.. math::

    \\max (G(H), S(H), N(H; K^{(v)})), \\qquad \\min C(H).

框架说明 §4 同时给出**契约级约束**：

    *确证不使用「新颖性加分抵消弱证据」的总分。候选必须通过有效测试、
    误差控制、预定效应标准与领域约束。*

本模块把该约束实现为三条硬防线：

1. **方向显式声明。** 四个维度的优化方向写在 :data:`OPTIMIZATION_DIRECTIONS`
   里，是冻结常量，不提供运行时覆盖入口。任何调用方想改方向都改不了。
2. **只做偏序，不做全序。** 支配关系（:func:`dominates`）要求「所有维度都不
   更差、且至少一个维度严格更好」。**没有**任何把四个维度折算成单一数值的
   函数；前沿内部的取舍由**轮转式预约**（:data:`RESERVE_ROTATION`）完成——
   逐维度按序择优，彼此不折算。
3. **阻断所有总分入口。** 任何形如 ``weights`` / ``total_score`` /
   ``novelty_bonus`` 的关键字参数一律被 :class:`SelectionPolicyError` 拒绝，
   报错信息点明确证阶段的原则。见 :data:`FORBIDDEN_SCORE_KEYS`。

关于「缺维度」的口径（与 P09 一致）：P09 允许某维度为 ``None`` 表示**证据不足**
（「没算」与「算出来是 0」必须可区分）。本模块**不插补、不填默认值、不因此扣分**：
带 ``None`` 的评估记录不参与支配比较，也不进入 Pareto 前沿，而是单列在
:attr:`ParetoFront.incomplete` 中如实上报。若要改变这一口径，只能由调用方在
:func:`select_freeze_candidates` 中显式开启 ``allow_incomplete``，且此类候选恒排在
完备候选之后并被标记。

本模块的定位与边界：

- 产物是**建议对象**，不授予任何证据等级，不触发任何确证流程
  （:data:`SELECTION_GRANTS_EVIDENCE_GRADE` 恒为 ``False``）。
- 不修改 ``sdl_m01/``、不读取确证分区、不导入第三方依赖。
- 不实现 P11 及之后的内容：冻结计划生成、确证绑定、检验执行、结果发布、
  主动取证均不在本模块内（见 :data:`NOT_PROVIDED_BY_P10`）。

主要入口：

====================  ==========================================================
:func:`pareto_front`  按显式方向求 Pareto 前沿与分层，缺维度者单列上报。
:func:`top_k_reserved`  在前沿内部按**声明的轮转顺序**预约 k 个代表性候选。
:func:`select_freeze_candidates`  生成冻结阶段候选清单，上限可配置（默认 5）。
====================  ==========================================================

术语约定（全文一致）：

- **支配（dominate）**：偏序关系，A 支配 B ⇔ A 在所有维度上按各自方向均不劣于 B，
  且至少有一个维度严格优于 B。
- **前沿（front）**：不被任何其它候选支配的候选集合，即 Pareto 第一层。
- **层（layer / rank）**：反复剥离前沿所得的层级，rank 0 为前沿。
- **预约（reserve）**：从某一层里按轮转顺序挑出的代表性候选，用于覆盖不同维度。
- **冻结候选（freeze candidate）**：交给 P11 生成冻结计划的候选，数量受上限约束。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

__all__ = [
    # 异常
    "SelectionError",
    "SelectionInputError",
    "SelectionPolicyError",
    "SelectionVersionError",
    # 常量：方向和口径
    "SELECT_VERSION",
    "DIMENSIONS",
    "DIRECTION_MAXIMIZE",
    "DIRECTION_MINIMIZE",
    "MAXIMIZED_DIMENSIONS",
    "MINIMIZED_DIMENSIONS",
    "OPTIMIZATION_DIRECTIONS",
    "DIRECTIONS_NOTE",
    "COMPARISON_TOLERANCE",
    "DOMINANCE_NOTE",
    "NO_TOTAL_SCORE_NOTE",
    "MISSING_DIMENSION_POLICY",
    "RESERVE_ROTATION",
    "RESERVE_NOTE",
    # 常量：冻结上限与确证原则
    "DEFAULT_FREEZE_CAP",
    "FREEZE_CAP_HARD_LIMIT",
    "FREEZE_CAP_NOTE",
    "CONFIRMATION_USES_NOVELTY_BONUS",
    "CONFIRMATION_POLICY",
    "SELECTION_GRANTS_EVIDENCE_GRADE",
    "SELECTION_SCOPE_NOTE",
    "FORBIDDEN_SCORE_KEYS",
    "FORBIDDEN_TOKEN_KEYS",
    "NOT_PROVIDED_BY_P10",
    # 方向与支配
    "direction_of",
    "is_better",
    "dominates",
    "compare_pair",
    # 分层与筛选
    "pareto_layers",
    "pareto_front",
    "top_k_reserved",
    "select_freeze_candidates",
    # 结果对象
    "DominancePair",
    "ParetoFront",
    "SelectedCandidate",
    "ReserveSelection",
    "FreezeSelection",
]


# ---------------------------------------------------------------------------
# 常量：优化方向（验收标准①）
# ---------------------------------------------------------------------------

#: 交付物版本号，写入序列化结果，便于追溯筛选口径的变更。
SELECT_VERSION = "P10-v1.0"

#: 四个维度的固定顺序，与 P09 的 :data:`~sdl_m05.metrics.DIMENSION_NAMES` 一致。
DIMENSIONS: tuple[str, ...] = ("gain", "stability", "novelty", "complexity")

#: 方向取值字面量。
DIRECTION_MAXIMIZE = "maximize"
DIRECTION_MINIMIZE = "minimize"

#: 越大越好的三个维度：预测增益 G、稳定性 S、相对新颖性 N。
MAXIMIZED_DIMENSIONS: tuple[str, ...] = ("gain", "stability", "novelty")

#: 越小越好的维度：复杂度 C。
MINIMIZED_DIMENSIONS: tuple[str, ...] = ("complexity",)

#: **优化方向的显式声明（验收标准①的核心）。**
#:
#: 依据框架说明 §4 的 :math:`\max (G,S,N)` 与 :math:`\min C`。
#: 这是一个**冻结常量**：本模块不提供任何覆盖它的参数或函数。之所以做成
#: 不可覆盖的常量，是因为方向一旦可以被调用方临时改写，「探索阶段固定方向」
#: 这一前提就失效了，而框架说明要求方向在探索阶段就固定下来。
OPTIMIZATION_DIRECTIONS: Mapping[str, str] = MappingProxyType(
    {name: DIRECTION_MAXIMIZE for name in MAXIMIZED_DIMENSIONS}
    | {name: DIRECTION_MINIMIZE for name in MINIMIZED_DIMENSIONS}
)

#: 方向声明的文字说明，随结果对象一并输出，便于复核。
DIRECTIONS_NOTE = (
    "优化方向显式声明并冻结：gain/stability/novelty 取最大化，complexity 取最小化；"
    "本模块不提供任何运行时改写方向的入口。方向依据框架说明 §4 的 "
    "max(G,S,N) 与 min C，且只在探索阶段使用。"
)

#: 数值比较容差：两值之差不超过该值时视为**等价**（谁都不更好）。
#:
#: 引入容差是为了避免浮点噪声制造出虚假的严格支配——例如 1e-16 级别的
#: 差异不应被判定为「严格更好」。容差只影响「是否严格更好」的判定，
#: 不影响「谁更差」的判定（不劣于的比较对称地放宽为容差内的等价）。
COMPARISON_TOLERANCE = 1e-12

#: 支配关系的语义说明。
DOMINANCE_NOTE = (
    "支配是偏序：A 支配 B 当且仅当 A 在所有维度上按各自方向均不劣于 B，"
    "且至少一个维度严格优于 B（差值超过比较容差）。"
    "偏序不是全序——不可比的候选同时留在前沿，这正是「新颖性不能抵消弱增益」"
    "在算法上的体现。"
)

#: 拒绝总分逻辑的口径说明。
NO_TOTAL_SCORE_NOTE = (
    "本模块只做 Pareto 偏序筛选与配额裁剪，不计算任何跨维度加权总分："
    "没有任何函数把 G/S/N/C 折算成单一数值，前沿内部的取舍由轮转式预约完成，"
    "各维度之间不互相折算。"
)

#: 缺维度（``None``）的处理口径，作为可机检的声明单列。
MISSING_DIMENSION_POLICY = (
    "带 None 维度的评估记录不参与支配比较、不进入 Pareto 前沿，"
    "而是单列在 incomplete 中如实上报：不插补、不填默认值、不因此扣分。"
    "只有调用方显式开启 allow_incomplete 时，此类候选才可能被选中，"
    "且恒排在完备候选之后并被标记。"
)

#: 前沿内部预约候选的轮转维度顺序。
#:
#: 轮转是**替代加权总分**的取舍机制：第 i 个预留名额按本元组的第 i 个维度择优，
#: 循环使用，使预约结果覆盖不同维度，而不把任何维度折算成别的维度。
RESERVE_ROTATION: tuple[str, ...] = ("gain", "stability", "novelty", "complexity")

#: 轮转预约的语义说明。
RESERVE_NOTE = (
    "轮转式预约：按声明的维度顺序逐次择优，使预留名额分布在不同维度上。"
    "该机制不做跨维度加权，任何维度都不会被折算成别的维度；"
    "同维度平分时以候选标识的字典序作确定性 tie-break。"
)


# ---------------------------------------------------------------------------
# 常量：冻结候选上限与此阶段的契约级约束（验收标准②③）
# ---------------------------------------------------------------------------

#: 冻结候选的数量上限默认值：5（验收标准②）。
DEFAULT_FREEZE_CAP = 5

#: 上限的硬边界，锚定框架说明 §2 的原型配置「每轮保留 50 个探索候选」。
#: 冻结候选是交给确证阶段的少量样本，越过探索候选的总量上界没有意义。
FREEZE_CAP_HARD_LIMIT = 50

#: 冻结上限的口径说明。
FREEZE_CAP_NOTE = (
    "冻结候选上限可配置（参数 max_candidates），默认 5，"
    f"硬边界 {FREEZE_CAP_HARD_LIMIT}（锚定框架说明 §2 每轮探索候选上界）。"
    "候选不足时如实记录 shortfall，不用低质量候选凑数。"
)

#: **契约级约束的机检声明。** 确证阶段是否使用「新颖性加分」来抵消弱证据。
#:
#: 恒为 ``False``，且本模块没有任何代码路径可以把它改成 ``True``：
#: 它是字面常量而非配置项。框架说明 §4 与 ``INTERFACES.md`` §2.4 均要求
#: 「不得出现新颖性加分抵消弱证据的总分」。
CONFIRMATION_USES_NOVELTY_BONUS: bool = False

#: 确证政策声明：整份声明随筛选结果输出，使「本模块没有越界改写确证原则」
#: 成为可机检的事实，而不只是一句文档承诺。
CONFIRMATION_POLICY: Mapping[str, Any] = MappingProxyType(
    {
        "uses_weighted_total": False,
        "uses_novelty_bonus_to_offset_weak_evidence": False,
        "novelty_treated_as_compensable": False,
        "requires_valid_test": True,
        "requires_error_control": True,
        "requires_predefined_effect_threshold": True,
        "requires_domain_constraints": True,
        "policy_note": (
            "确证不使用「新颖性加分抵消弱证据」的总分。候选必须通过有效测试、"
            "误差控制、预定效应标准与领域约束；本模块的新颖性只在探索阶段参与"
            "Pareto 筛选，且以「不可比」而非「可补偿」的方式参与——"
            "新颖性高的候选不会因此免于在其它维度被支配。"
        ),
    }
)

#: 本模块产物是否授予证据等级。恒为 ``False``。
SELECTION_GRANTS_EVIDENCE_GRADE: bool = False

#: 本模块的作用域声明。
SELECTION_SCOPE_NOTE = (
    "本模块输出的是探索阶段的筛选建议对象，不授予证据等级、不构成确证结论、"
    "不触发任何数据获取或确证流程；确证门槛、效果阈值与检验族的确定属 P11–P13。"
)

#: 明确拒绝的关键字参数：它们是「引入总分或让新颖性抵消弱证据」的入口。
FORBIDDEN_SCORE_KEYS: frozenset[str] = frozenset(
    {
        "weights",
        "score_weights",
        "weighted_sum",
        "weighted_score",
        "total_score",
        "aggregate_score",
        "combine_scores",
        "novelty_bonus",
        "novelty_weight",
        "weak_evidence_offset",
        "offset_score",
        "bonus",
        "penalty",
    }
)

#: 不得出现在输入记录里的键名（小写比较）。
#: 依据 ``INTERFACES.md`` §4.4：令牌正文不得写入日志、提示词或版本库。
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

#: 本模块**不提供**的接口名。测试据此断言「P10 未越界实现其它阶段的内容」。
#: 单列成常量而非散落在测试里，是为了让「本层不做什么」也成为可机检的声明。
NOT_PROVIDED_BY_P10: tuple[str, ...] = (
    # P09 的指标入口（本模块只消费，不重算）
    "gain",
    "stability",
    "novelty",
    "complexity",
    "evaluate_hypothesis",
    # P11 的冻结计划生成与确证绑定
    "build_frozen_plan",
    "bind",
    "bind_confirmation",
    # P12 的检验执行与消费
    "consume_confirmation",
    "record_evaluation",
    "execute_tests",
    # P13 的结果发布
    "release_results",
    "classify_result",
    # P14 的主动取证
    "divergence",
    "acquisition_plan",
    # 任何形式的加权聚合
    "weighted_sum",
    "total_score",
    "aggregate_score",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SelectionError(ValueError):
    """本模块所有异常的基类，继承自 :class:`ValueError`。"""


class SelectionInputError(SelectionError):
    """输入形态非法（缺字段、类型不对、重复标识等）。"""


class SelectionPolicyError(SelectionError):
    """调用方尝试引入总分逻辑，或触碰确证阶段的契约级约束。"""


class SelectionVersionError(SelectionInputError):
    """知识库版本 :math:`K^{(v)}` 缺失或跨版本混用。

    新颖性只在同一个 :math:`K^{(v)}` 下可比；跨版本比较属于口径污染，
    必须就地拒绝而不是静默沿用。
    """


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

#: 数值取整位数，与 P09 保持一致，便于跨模块比对。
_ROUND_DIGITS = 12


def _round(value: float, digits: int = _ROUND_DIGITS) -> float:
    """四舍五入到固定位数，消除浮点尾差对序列化结果的影响。"""
    return round(float(value), digits)


def _is_finite_number(value: Any) -> bool:
    """是否为有限实数（``bool`` 不算，它是 ``int`` 的子类但语义不同）。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _require_nonempty_str(value: Any, label: str) -> str:
    """要求非空字符串（去空白后非空）。"""
    if not isinstance(value, str) or not value.strip():
        raise SelectionInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """要求映射类型。"""
    if not isinstance(value, Mapping):
        raise SelectionInputError(f"{label} 必须是映射，实际得到：{type(value).__name__}")
    return value


def _reject_forbidden_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝输入记录里出现令牌类键名。"""
    for key in mapping:
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_TOKEN_KEYS:
            raise SelectionPolicyError(
                f"{label} 出现受限键名 {key!r}：令牌正文不得进入本层输入、"
                "日志或版本库（INTERFACES.md §4.4）。"
            )


def _reject_score_attempts(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝任何加权 / 总分 / 加分抵消入口（验收标准③）。

    判定口径与 P09 一致：显式禁用名单，外加对 ``weight`` / ``score``
    子串的兜底，防止换个名字绕过。报错信息必须点明「确证不使用新颖性加分
    抵消弱证据」这一契约级约束，使越界尝试在调用点就被明确告知。
    """
    for key in sorted(kwargs):
        lowered = key.strip().lower()
        if (
            lowered in FORBIDDEN_SCORE_KEYS
            or "weight" in lowered
            or "score" in lowered
        ):
            raise SelectionPolicyError(
                f"{where} 不接受参数 {key!r}：本模块只做 Pareto 偏序筛选，"
                "不做加权总分；确证不使用「新颖性加分抵消弱证据」的总分，"
                "候选必须通过有效测试、误差控制、预定效应标准与领域约束。"
            )
    if kwargs:  # pragma: no cover - 防御性兜底
        raise SelectionInputError(
            f"{where} 收到未预期的参数：" + ", ".join(repr(k) for k in sorted(kwargs))
        )


def _jsonable(value: Any) -> Any:
    """把任意嵌套结构转换为 JSON 兼容形态。"""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _canonical(value: Any) -> str:
    """规范式 JSON 文本（键排序、UTF-8 直出），用于稳定比对与指纹。"""
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _identity_of_source(item: Any) -> str:
    """取原始输入对象的标识，口径与内部规范化完全一致（``id@version``）。"""
    if isinstance(item, Mapping):
        identity = item.get("identity")
        if isinstance(identity, str) and identity:
            return identity
        hypothesis_id = item.get("hypothesis_id", item.get("id", ""))
        version = item.get("hypothesis_version", item.get("version", "")) or ""
    else:
        identity = getattr(item, "identity", None)
        if isinstance(identity, str) and identity:
            return identity
        hypothesis_id = getattr(item, "hypothesis_id", getattr(item, "id", ""))
        version = getattr(item, "hypothesis_version", getattr(item, "version", "")) or ""
    return f"{hypothesis_id}@{version}" if version else str(hypothesis_id)


def _validate_rotation(rotation: Any) -> tuple[str, ...]:
    """校验轮转维度序列：非空、无重复、且均为已知维度。"""
    if isinstance(rotation, (str, bytes)) or not isinstance(rotation, Iterable):
        raise SelectionInputError(
            f"rotation 必须是维度的可迭代序列，实际得到：{rotation!r}"
        )
    names = tuple(rotation)
    if not names:
        raise SelectionInputError("rotation 不能为空：轮转顺序必须至少含一个维度")
    for name in names:
        if name not in OPTIMIZATION_DIRECTIONS:
            raise SelectionInputError(
                f"rotation 含未知维度 {name!r}；已知维度为 {list(DIMENSIONS)}"
            )
    if len(set(names)) != len(names):
        raise SelectionInputError(f"rotation 含重复维度：{list(names)}")
    return names


# ---------------------------------------------------------------------------
# 输入规范化
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """内部规范化的候选记录。

    :param identity: 候选唯一标识，形如 ``id@version``（无版本号时即 ``id``）。
    :param hypothesis_id: 假说 id。
    :param hypothesis_version: 假说版本号（可为空串，表示上游未提供）。
    :param values: 四个维度的取值（允许为 ``None``，表示证据不足）。
    :param missing: 取值为 ``None`` 的维度名元组。
    :param knowledge_version: 新颖性所相对的 :math:`K^{(v)}`。
    :param payload: JSON 兼容的记录明细，用于序列化。
    :param source: 原始输入对象（供调用方取回，例如 P07 的假说引用）。
    :param input_index: 输入序号，用于如实还原调用方给定的顺序。
    """

    identity: str
    hypothesis_id: str
    hypothesis_version: str
    values: Mapping[str, float | None]
    missing: tuple[str, ...]
    knowledge_version: str
    payload: Mapping[str, Any]
    source: Any
    input_index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def complete(self) -> bool:
        """四个维度是否都有取值。"""
        return not self.missing

    def sort_key(self) -> tuple[str, int]:
        """确定性排序键：先按标识字典序，再按输入序号。"""
        return (self.identity, self.input_index)


def _as_mapping_candidate(item: Mapping[str, Any], index: int) -> _Candidate:
    """把字典形态的评估记录规范化为 :class:`_Candidate`。"""
    _reject_forbidden_keys(item, f"evaluations[{index}]")
    hypothesis_id = item.get("hypothesis_id", item.get("id"))
    if hypothesis_id is None:
        raise SelectionInputError(
            f"evaluations[{index}] 缺少 hypothesis_id 字段："
            "候选必须有稳定标识才能参与筛选"
        )
    hypothesis_id = _require_nonempty_str(
        hypothesis_id, f"evaluations[{index}].hypothesis_id"
    )
    version = item.get("hypothesis_version", item.get("version", ""))
    if version is None:
        version = ""
    if not isinstance(version, str):
        raise SelectionInputError(
            f"evaluations[{index}].hypothesis_version 必须是字符串，"
            f"实际得到：{version!r}"
        )
    knowledge_version = item.get("knowledge_version")
    if knowledge_version is None or not str(knowledge_version).strip():
        raise SelectionVersionError(
            f"evaluations[{index}] 缺少 knowledge_version："
            "新颖性必须绑定到明确的知识库版本 K^(v)（P09 验收标准①），"
            "筛选层据此拒绝跨版本比较"
        )
    knowledge_version = str(knowledge_version).strip()
    payload = _jsonable(dict(item))
    return _build_candidate(
        hypothesis_id=hypothesis_id,
        version=version,
        knowledge_version=knowledge_version,
        reader=lambda name: item.get(name),
        payload=payload,
        source=item,
        index=index,
    )


def _as_object_candidate(item: Any, index: int) -> _Candidate:
    """把对象形态的评估记录（如 P09 的 ``Evaluation``）规范化为 :class:`_Candidate`。"""
    hypothesis_id = getattr(item, "hypothesis_id", None)
    if hypothesis_id is None:
        hypothesis_id = getattr(item, "id", None)
    if hypothesis_id is None:
        raise SelectionInputError(
            f"evaluations[{index}]（{type(item).__name__}）缺少 hypothesis_id 属性"
        )
    hypothesis_id = _require_nonempty_str(
        hypothesis_id, f"evaluations[{index}].hypothesis_id"
    )
    version = getattr(item, "hypothesis_version", None)
    if version is None:
        version = getattr(item, "version", "") or ""
    if not isinstance(version, str):
        raise SelectionInputError(
            f"evaluations[{index}].hypothesis_version 必须是字符串，实际得到：{version!r}"
        )
    knowledge_version = getattr(item, "knowledge_version", None)
    if knowledge_version is None or not str(knowledge_version).strip():
        raise SelectionVersionError(
            f"evaluations[{index}] 缺少 knowledge_version："
            "新颖性必须绑定到明确的知识库版本 K^(v)（P09 验收标准①），"
            "筛选层据此拒绝跨版本比较"
        )
    knowledge_version = str(knowledge_version).strip()
    reader = lambda name: getattr(item, name, None)  # noqa: E731 - 局部短读取器
    payload: Mapping[str, Any]
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        try:
            payload = _jsonable(to_dict())
        except Exception:  # pragma: no cover - 上游 to_dict 异常时降级
            payload = {
                "hypothesis_id": hypothesis_id,
                "hypothesis_version": version,
                "knowledge_version": knowledge_version,
            }
    else:
        payload = {
            "hypothesis_id": hypothesis_id,
            "hypothesis_version": version,
            "knowledge_version": knowledge_version,
            "dimensions": {
                name: reader(name) for name in DIMENSIONS
            },
        }
    return _build_candidate(
        hypothesis_id=hypothesis_id,
        version=version,
        knowledge_version=knowledge_version,
        reader=reader,
        payload=payload,
        source=item,
        index=index,
    )


def _build_candidate(
    *,
    hypothesis_id: str,
    version: str,
    knowledge_version: str,
    reader: Any,
    payload: Mapping[str, Any],
    source: Any,
    index: int,
) -> _Candidate:
    """按统一口径构造 :class:`_Candidate`（读值、校验取值范围、计算缺维度）。"""
    values: dict[str, float | None] = {}
    missing: list[str] = []
    for name in DIMENSIONS:
        raw = reader(name)
        if raw is None:
            values[name] = None
            missing.append(name)
            continue
        if not _is_finite_number(raw):
            raise SelectionInputError(
                f"evaluations[{index}].{name} 必须是有限实数或 None，"
                f"实际得到：{raw!r}"
            )
        values[name] = _round(float(raw))
    # 取值范围与 P09 的 Evaluation 约束保持一致（此处是廉价前置防线）。
    for name, bound in (("novelty", (0.0, 1.0)), ("stability", (0.0, 1.0))):
        value = values[name]
        if value is not None and not bound[0] <= value <= bound[1]:
            raise SelectionInputError(
                f"evaluations[{index}].{name} 必须落在 [{bound[0]}, {bound[1]}] 内，"
                f"实际得到：{value!r}"
            )
    complexity = values["complexity"]
    if complexity is not None and complexity < 0.0:
        raise SelectionInputError(
            f"evaluations[{index}].complexity 必须非负，实际得到：{complexity!r}"
        )
    identity = f"{hypothesis_id}@{version}" if version else hypothesis_id
    return _Candidate(
        identity=identity,
        hypothesis_id=hypothesis_id,
        hypothesis_version=version,
        values=values,
        missing=tuple(missing),
        knowledge_version=knowledge_version,
        payload=payload,
        source=source,
        input_index=index,
    )


def _normalize(evaluations: Any) -> tuple[_Candidate, ...]:
    """把输入集合规范化为候选元组，并校验标识唯一与知识库版本一致由调用方决定。

    :raises SelectionInputError: 输入不是可迭代集合、或存在重复标识。
    """
    if isinstance(evaluations, (str, bytes)) or not isinstance(evaluations, Iterable):
        raise SelectionInputError(
            "evaluations 必须是评估记录的可迭代集合，实际得到："
            f"{type(evaluations).__name__}"
        )
    items = list(evaluations)
    records: list[_Candidate] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if isinstance(item, Mapping):
            record = _as_mapping_candidate(item, index)
        elif item is None or isinstance(item, (str, bytes, int, float, bool)):
            raise SelectionInputError(
                f"evaluations[{index}] 不是评估记录：{item!r}"
            )
        else:
            record = _as_object_candidate(item, index)
        if record.identity in seen:
            raise SelectionInputError(
                f"evaluations 中出现重复候选标识 {record.identity!r}："
                "同一假说同版本的评估记录只能出现一次，否则前沿分层会有歧义"
            )
        seen.add(record.identity)
        records.append(record)
    return tuple(records)


def _check_knowledge_versions(
    records: Sequence[_Candidate], *, enabled: bool
) -> str | None:
    """校验所有候选共用同一个 :math:`K^{(v)}`。

    :param enabled: 是否启用该校验（关闭时仅返回首个版本号供登记）。
    :return: 唯一版本号；输入为空时返回 ``None``。
    :raises SelectionVersionError: 启用校验且出现多个版本号。
    """
    versions = sorted({item.knowledge_version for item in records})
    if not versions:
        return None
    if enabled and len(versions) > 1:
        raise SelectionVersionError(
            "输入含多个知识库版本 "
            + ", ".join(repr(v) for v in versions)
            + "：新颖性只在同一个 K^(v) 下可比，跨版本比较属于口径污染。"
            "如需强行比较，请显式传入 require_single_knowledge_version=False "
            "并自行承担口径不一致的后果。"
        )
    return versions[0]


# ---------------------------------------------------------------------------
# 方向与支配（验收标准①）
# ---------------------------------------------------------------------------


def direction_of(dimension: str) -> str:
    """返回某维度的优化方向（``maximize`` / ``minimize``）。

    :raises SelectionInputError: 维度名未知。
    """
    if dimension not in OPTIMIZATION_DIRECTIONS:
        raise SelectionInputError(
            f"未知维度 {dimension!r}；已知维度为 {list(DIMENSIONS)}"
        )
    return OPTIMIZATION_DIRECTIONS[dimension]


def _compare_values(left: float, right: float, direction: str) -> int:
    """按方向比较两个数值：左更优返回 ``1``，右更优返回 ``-1``，等价返回 ``0``。"""
    if abs(float(left) - float(right)) <= COMPARISON_TOLERANCE:
        return 0
    if direction == DIRECTION_MAXIMIZE:
        return 1 if left > right else -1
    return 1 if left < right else -1


def is_better(dimension: str, left: Any, right: Any) -> bool:
    """按该维度的**声明方向**判断 ``left`` 是否严格优于 ``right``。

    :param dimension: 维度名。
    :param left: 左候选（评估记录或字典）。
    :param right: 右候选（评估记录或字典）。
    :return: 严格更优则为 ``True``；等价（差值在容差内）为 ``False``。
    :raises SelectionInputError: 维度未知，或任一侧该维度取值缺失。
    """
    direction = direction_of(dimension)
    left_value = _dimension_value(left, dimension)
    right_value = _dimension_value(right, dimension)
    if left_value is None or right_value is None:
        raise SelectionInputError(
            f"维度 {dimension!r} 在比较双方之一缺失："
            "缺维度表示证据不足，不能当作可比较的数值使用"
            "（不插补、不填默认值）"
        )
    return _compare_values(left_value, right_value, direction) > 0


def _dimension_value(item: Any, dimension: str) -> float | None:
    """从评估记录（对象或字典）里取某维度取值。"""
    if isinstance(item, Mapping):
        raw = item.get(dimension)
    else:
        raw = getattr(item, dimension, None)
    if raw is None:
        return None
    if not _is_finite_number(raw):
        raise SelectionInputError(
            f"维度 {dimension!r} 的取值不是有限实数：{raw!r}"
        )
    return _round(float(raw))


def _dominates(winner: _Candidate, loser: _Candidate) -> bool:
    """规范化记录之间的支配判定（内部实现，双方必须均完备）。"""
    strictly_better_somewhere = False
    for name in DIMENSIONS:
        verdict = _compare_values(
            winner.values[name],  # type: ignore[arg-type]
            loser.values[name],  # type: ignore[arg-type]
            OPTIMIZATION_DIRECTIONS[name],
        )
        if verdict < 0:
            return False
        if verdict > 0:
            strictly_better_somewhere = True
    return strictly_better_somewhere


def dominates(left: Any, right: Any) -> bool:
    """判断 ``left`` 是否**支配** ``right``。

    支配是偏序：所有维度均不劣、且至少一个维度严格更优。

    :raises SelectionInputError: 任一侧存在缺维度（证据不足的候选不可比较）。
    """
    left_value = _normalize_one(left, "dominates(left)")
    right_value = _normalize_one(right, "dominates(right)")
    for label, record in (("left", left_value), ("right", right_value)):
        if not record.complete:
            raise SelectionInputError(
                f"dominates 的 {label} 候选 {record.identity!r} 缺维度 "
                f"{list(record.missing)}：{MISSING_DIMENSION_POLICY}"
            )
    return _dominates(left_value, right_value)


def _normalize_one(item: Any, label: str) -> _Candidate:
    """规范化单个候选，用于逐对比较的公开入口。"""
    if isinstance(item, Mapping):
        _reject_forbidden_keys(item, label)
        return _as_mapping_candidate(item, 0)
    if item is None or isinstance(item, (str, bytes, int, float, bool)):
        raise SelectionInputError(f"{label} 不是评估记录：{item!r}")
    return _as_object_candidate(item, 0)


@dataclass(frozen=True)
class DominancePair:
    """一对候选的逐维度比较结果，用于解释「谁支配谁」以及「为何不可比」。

    :param left_identity: 左候选标识。
    :param right_identity: 右候选标识。
    :param per_dimension: ``维度 → 比较结论``，取值为 ``"left"`` / ``"right"`` / ``"tie"``。
    :param left_dominates: 左是否支配右。
    :param right_dominates: 右是否支配左。
    :param comparable: 是否存在支配关系（两者皆否即为不可比）。
    :param reason: 结论的自然语言说明。
    """

    left_identity: str
    right_identity: str
    per_dimension: Mapping[str, str]
    left_dominates: bool
    right_dominates: bool
    comparable: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "per_dimension", MappingProxyType(dict(self.per_dimension))
        )

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "select_version": SELECT_VERSION,
            "left_identity": self.left_identity,
            "right_identity": self.right_identity,
            "per_dimension": _jsonable(self.per_dimension),
            "directions": _jsonable(OPTIMIZATION_DIRECTIONS),
            "left_dominates": self.left_dominates,
            "right_dominates": self.right_dominates,
            "comparable": self.comparable,
            "reason": self.reason,
        }


def compare_pair(left: Any, right: Any) -> DominancePair:
    """比较一对候选，给出逐维度结论与支配判定。

    这是**解释性**入口：把「为何 A 支配 B」或「为何两者不可比」摊开，
    使「新颖性不能抵消弱证据」在结果层面可复核。

    :raises SelectionInputError: 任一侧缺维度。
    """
    left_record = _normalize_one(left, "compare_pair(left)")
    right_record = _normalize_one(right, "compare_pair(right)")
    for label, record in (("left", left_record), ("right", right_record)):
        if not record.complete:
            raise SelectionInputError(
                f"compare_pair 的 {label} 候选 {record.identity!r} 缺维度 "
                f"{list(record.missing)}：{MISSING_DIMENSION_POLICY}"
            )
    per_dimension: dict[str, str] = {}
    for name in DIMENSIONS:
        verdict = _compare_values(
            left_record.values[name],  # type: ignore[arg-type]
            right_record.values[name],  # type: ignore[arg-type]
            OPTIMIZATION_DIRECTIONS[name],
        )
        per_dimension[name] = {1: "left", -1: "right", 0: "tie"}[verdict]
    left_dominates = _dominates(left_record, right_record)
    right_dominates = _dominates(right_record, left_record)
    if left_dominates:
        reason = (
            f"{left_record.identity} 在所有维度上均不劣于 {right_record.identity}，"
            "且至少一个维度严格更优，故前者支配后者。"
        )
    elif right_dominates:
        reason = (
            f"{right_record.identity} 在所有维度上均不劣于 {left_record.identity}，"
            "且至少一个维度严格更优，故前者支配后者。"
        )
    else:
        reason = (
            "两者互不支配（各自在某些维度更优）：偏序下不可比，"
            "将同时保留在前沿，不折算为单一分数比较。"
        )
    return DominancePair(
        left_identity=left_record.identity,
        right_identity=right_record.identity,
        per_dimension=per_dimension,
        left_dominates=left_dominates,
        right_dominates=right_dominates,
        comparable=left_dominates or right_dominates,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 分层与前沿
# ---------------------------------------------------------------------------


def _layers_of(records: Sequence[_Candidate]) -> tuple[tuple[_Candidate, ...], ...]:
    """对完备记录做快速非支配分层，层内按标识字典序确定顺序。

    层 0 为 Pareto 前沿；剥离层 0 后再取前沿即层 1，依此类推。
    """
    remaining = list(records)
    layers: list[tuple[_Candidate, ...]] = []
    while remaining:
        current = [
            record
            for record in remaining
            if not any(
                _dominates(other, record)
                for other in remaining
                if other.identity != record.identity
            )
        ]
        if not current:  # pragma: no cover - 有限集合上不可能发生，防御性兜底
            raise SelectionError(
                "分层失败：未能从剩余候选中剥离出非被支配集合，请检查输入是否被并发修改"
            )
        current.sort(key=lambda record: record.sort_key())
        layers.append(tuple(current))
        taken = {record.identity for record in current}
        remaining = [record for record in remaining if record.identity not in taken]
    return tuple(layers)


def _front_dominators(
    layers: Sequence[Sequence[_Candidate]],
) -> dict[str, tuple[str, ...]]:
    """记录每个非前沿候选被哪些**前沿**候选支配（仅统计层 0 的支配者）。"""
    if not layers:
        return {}
    front = layers[0]
    result: dict[str, tuple[str, ...]] = {}
    for layer_index, layer in enumerate(layers):
        if layer_index == 0:
            continue
        for record in layer:
            result[record.identity] = tuple(
                sorted(
                    other.identity
                    for other in front
                    if _dominates(other, record)
                )
            )
    return result


@dataclass(frozen=True)
class ParetoFront:
    """一次 Pareto 筛选的完整结果。

    本对象**只承载偏序结构与层号**，不含任何总分、名次分或加权排名。

    :param front: 前沿成员（层 0），原始输入对象。
    :param dominated: 被支配的完备候选，原始输入对象。
    :param incomplete: 缺维度候选，单列上报，不参与任何比较。
    :param layers: 分层结果（每层为原始输入对象元组），层 0 即 ``front``。
    :param ranks: ``候选标识 → 层号``。
    :param dominated_by: ``非前沿候选标识 → 支配它的前沿候选标识元组``。
    :param directions: 本结果所用方向（恒等于冻结的声明）。
    :param knowledge_version: 本次比较所依据的 :math:`K^{(v)}`。
    :param input_order: ``候选标识 → 输入序号``，用于还原调用方给定的顺序。
    :param note: 口径说明。
    """

    front: tuple[Any, ...]
    dominated: tuple[Any, ...]
    incomplete: tuple[Any, ...]
    layers: tuple[tuple[Any, ...], ...]
    ranks: Mapping[str, int]
    dominated_by: Mapping[str, tuple[str, ...]]
    directions: Mapping[str, str]
    knowledge_version: str | None
    input_order: Mapping[str, int]
    note: str = DOMINANCE_NOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "ranks", MappingProxyType(dict(self.ranks)))
        object.__setattr__(
            self, "dominated_by", MappingProxyType(dict(self.dominated_by))
        )
        object.__setattr__(self, "directions", MappingProxyType(dict(self.directions)))
        object.__setattr__(
            self, "input_order", MappingProxyType(dict(self.input_order))
        )

    # -- 只读视图 ---------------------------------------------------------

    @property
    def size(self) -> int:
        """参与分层（即完备）的候选数量。"""
        return len(self.front) + len(self.dominated)

    @property
    def front_size(self) -> int:
        """前沿规模。"""
        return len(self.front)

    @property
    def layer_count(self) -> int:
        """层数。"""
        return len(self.layers)

    @property
    def total_input_count(self) -> int:
        """输入候选总数（含缺维度者）。"""
        return self.size + len(self.incomplete)

    def front_ids(self) -> tuple[str, ...]:
        """前沿成员的标识（按结果顺序）。"""
        return self.identities_of_layer(0)

    def identities_of_layer(self, rank: int) -> tuple[str, ...]:
        """指定层号的成员标识，按结果顺序；层号越界时返回空元组。"""
        return tuple(
            _identity_of_source(item) for item in self.layers[rank]
        ) if 0 <= rank < len(self.layers) else ()

    #: 保留旧名作为别名，语义相同。
    ranks_inv = identities_of_layer
    @property
    def incomplete_ids(self) -> tuple[str, ...]:
        """缺维度候选的标识。"""
        return tuple(_identity_of_source(item) for item in self.incomplete)

    def rank_of(self, identity: str) -> int | None:
        """某候选的层号；缺维度者返回 ``None``。"""
        return self.ranks.get(identity)

    def dominators_of(self, identity: str) -> tuple[str, ...]:
        """支配某候选的前沿成员标识（无则返回空元组）。"""
        return tuple(self.dominated_by.get(identity, ()))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "select_version": SELECT_VERSION,
            "directions": _jsonable(self.directions),
            "knowledge_version": self.knowledge_version,
            "front": [_jsonable(item) for item in self._payloads_of(self.front)],
            "dominated": [
                _jsonable(item) for item in self._payloads_of(self.dominated)
            ],
            "incomplete_ids": list(self.incomplete_ids),
            "front_size": self.front_size,
            "layer_count": self.layer_count,
            "ranks": _jsonable(self.ranks),
            "dominated_by": _jsonable(self.dominated_by),
            "input_count": self.total_input_count,
            "missing_dimension_policy": MISSING_DIMENSION_POLICY,
            "dominance_note": self.note,
            "no_total_score_note": NO_TOTAL_SCORE_NOTE,
            "confirmation_policy": _jsonable(CONFIRMATION_POLICY),
            "select_grants_evidence_grade": SELECTION_GRANTS_EVIDENCE_GRADE,
        }

    def _payloads_of(self, items: Sequence[Any]) -> tuple[Any, ...]:
        """取原始对象的 JSON 明细（优先用 ``to_dict()``）。"""
        out: list[Any] = []
        for item in items:
            to_dict = getattr(item, "to_dict", None)
            if callable(to_dict):
                out.append(to_dict())
            elif isinstance(item, Mapping):
                out.append(dict(item))
            else:
                out.append(
                    {
                        name: getattr(item, name, None)
                        for name in ("hypothesis_id", "hypothesis_version")
                    }
                )
        return tuple(out)

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与摘要。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """结果摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __len__(self) -> int:
        return self.front_size

    def __iter__(self) -> Iterator[Any]:
        return iter(self.front)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"ParetoFront(front={self.front_size}, dominated={len(self.dominated)}, "
            f"incomplete={len(self.incomplete)}, layers={self.layer_count})"
        )


def pareto_layers(evaluations: Any) -> tuple[tuple[Any, ...], ...]:
    """返回分层结果（层 0 为 Pareto 前沿）。

    缺维度候选不出现在任何层中：它们不可比较。

    :param evaluations: 评估记录的可迭代集合（对象或字典）。
    :return: 每层为原始输入对象的元组，层内按标识字典序。
    """
    records = _normalize(evaluations)
    complete = [record for record in records if record.complete]
    layers = _layers_of(complete)
    return tuple(tuple(record.source for record in layer) for layer in layers)


def pareto_front(
    evaluations: Any,
    *,
    require_single_knowledge_version: bool = True,
) -> ParetoFront:
    """按**显式声明的方向**求 Pareto 前沿与分层（验收标准①）。

    实现要点：

    - 方向取自冻结常量 :data:`OPTIMIZATION_DIRECTIONS`，本函数不提供覆盖入口。
    - 只做偏序：不可比的候选同时保留在前沿，不折算成单一分数。
    - 缺维度候选不参与比较，单列于 :attr:`ParetoFront.incomplete`。

    :param evaluations: 评估记录的可迭代集合（对象或字典）。
    :param require_single_knowledge_version: 是否要求所有候选共用同一个
        :math:`K^{(v)}`（默认 ``True``）。关闭会使新颖性跨版本比较，属口径污染，
        仅供诊断用途。
    :return: :class:`ParetoFront`。
    :raises SelectionVersionError: 知识库版本缺失或跨版本混用。
    :raises SelectionInputError: 输入形态非法或存在重复标识。
    """
    records = _normalize(evaluations)
    knowledge_version = _check_knowledge_versions(
        records, enabled=require_single_knowledge_version
    )
    complete = [record for record in records if record.complete]
    incomplete = sorted(
        (record for record in records if not record.complete),
        key=lambda record: record.sort_key(),
    )
    layers = _layers_of(complete)
    ranks: dict[str, int] = {}
    for rank, layer in enumerate(layers):
        for record in layer:
            ranks[record.identity] = rank
    return ParetoFront(
        front=tuple(record.source for record in layers[0]) if layers else (),
        dominated=tuple(
            record.source for layer in layers[1:] for record in layer
        ),
        incomplete=tuple(record.source for record in incomplete),
        layers=tuple(
            tuple(record.source for record in layer) for layer in layers
        ),
        ranks=ranks,
        dominated_by=_front_dominators(layers),
        directions=dict(OPTIMIZATION_DIRECTIONS),
        knowledge_version=knowledge_version,
        input_order={record.identity: record.input_index for record in records},
    )


# ---------------------------------------------------------------------------
# 预约与冻结候选
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectedCandidate:
    """一个被选中的候选及其入选依据。

    :param identity: 候选标识。
    :param hypothesis_id: 假说 id。
    :param evaluation: 原始评估记录对象。
    :param rank: 所属层号；缺维度入选者为 ``None``。
    :param rotation_dimension: 使其入选的轮转维度；缺维度入选者为 ``None``。
    :param rationale: 入选依据的自然语言说明。
    :param incomplete_dimensions: 缺维度名元组（完备候选为空元组）。
    """

    identity: str
    hypothesis_id: str
    evaluation: Any
    rank: int | None
    rotation_dimension: str | None
    rationale: str
    incomplete_dimensions: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """被选候选是否是完备的（四维均有取值）。"""
        return not self.incomplete_dimensions

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        to_dict = getattr(self.evaluation, "to_dict", None)
        payload = to_dict() if callable(to_dict) else _jsonable(self.evaluation)
        return {
            "select_version": SELECT_VERSION,
            "identity": self.identity,
            "hypothesis_id": self.hypothesis_id,
            "rank": self.rank,
            "rotation_dimension": self.rotation_dimension,
            "rationale": self.rationale,
            "complete": self.complete,
            "incomplete_dimensions": list(self.incomplete_dimensions),
            "evaluation": _jsonable(payload),
        }


def _select_from_layer(
    layer: Sequence[_Candidate],
    *,
    limit: int,
    rotation: Sequence[str],
    picked: set[str],
    rank: int,
) -> list[SelectedCandidate]:
    """在单层内按轮转顺序择优，最多选 ``limit`` 个。

    轮转是**替代加权总分**的机制：第 i 个名额按轮转序列的第 i 个维度择优，
    循环使用。任何维度都不会被折算成别的维度；同维度平分时按标识字典序
    作确定性 tie-break。
    """
    selected: list[SelectedCandidate] = []
    cursor = 0
    # 循环上界：轮转序列长度的整数倍足以覆盖该层全部成员。
    max_steps = len(rotation) * (len(layer) + 1)
    steps = 0
    while len(selected) < limit and steps < max_steps:
        dimension = rotation[cursor % len(rotation)]
        cursor += 1
        steps += 1
        direction = OPTIMIZATION_DIRECTIONS[dimension]
        best: _Candidate | None = None
        for record in layer:
            if record.identity in picked:
                continue
            value = record.values[dimension]
            if value is None:  # pragma: no cover - 层内候选恒完备
                continue
            if best is None:
                best = record
                continue
            verdict = _compare_values(value, best.values[dimension], direction)  # type: ignore[arg-type]
            if verdict > 0 or (
                verdict == 0 and record.sort_key() < best.sort_key()
            ):
                best = record
        if best is None:
            break
        picked.add(best.identity)
        selected.append(
            SelectedCandidate(
                identity=best.identity,
                hypothesis_id=best.hypothesis_id,
                evaluation=best.source,
                rank=rank,
                rotation_dimension=dimension,
                rationale=(
                    f"第 {rank} 层（rank={rank}）轮转维度 {dimension}："
                    f"该维度按 {direction} 方向取值最优"
                    "（同维度平分时按候选标识字典序 tie-break）；"
                    "本选择不做跨维度加权，不折算为总分。"
                ),
                incomplete_dimensions=(),
            )
        )
    return selected


@dataclass(frozen=True)
class ReserveSelection:
    """轮转式预约的结果。

    :param reserved: 被预约的候选（按预约顺序）。
    :param requested_k: 请求的预约数量。
    :param available: 可用于预约的候选数量（前沿规模）。
    :param shortfall: 实际缺口 = ``max(0, requested_k - 实际数量)``。
    :param rotation: 本次使用的轮转维度顺序。
    :param directions: 方向声明。
    :param knowledge_version: 本次比较所依据的 :math:`K^{(v)}`。
    :param note: 口径说明。
    """

    reserved: tuple[SelectedCandidate, ...]
    requested_k: int
    available: int
    shortfall: int
    rotation: tuple[str, ...]
    directions: Mapping[str, str]
    knowledge_version: str | None
    note: str = RESERVE_NOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "directions", MappingProxyType(dict(self.directions)))

    @property
    def count(self) -> int:
        """实际预约数量。"""
        return len(self.reserved)

    def ids(self) -> tuple[str, ...]:
        """被预约候选的标识。"""
        return tuple(item.identity for item in self.reserved)

    def by_dimension(self) -> Mapping[str, tuple[str, ...]]:
        """``轮转维度 → 因该维度入选的候选标识``。"""
        grouped: dict[str, list[str]] = {name: [] for name in self.rotation}
        for item in self.reserved:
            if item.rotation_dimension is not None:
                grouped[item.rotation_dimension].append(item.identity)
        return MappingProxyType({k: tuple(v) for k, v in grouped.items()})

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "select_version": SELECT_VERSION,
            "reserved": [item.to_dict() for item in self.reserved],
            "count": self.count,
            "requested_k": self.requested_k,
            "available": self.available,
            "shortfall": self.shortfall,
            "rotation": list(self.rotation),
            "directions": _jsonable(self.directions),
            "knowledge_version": self.knowledge_version,
            "by_dimension": {k: list(v) for k, v in self.by_dimension().items()},
            "reserve_note": self.note,
            "no_total_score_note": NO_TOTAL_SCORE_NOTE,
            "confirmation_policy": _jsonable(CONFIRMATION_POLICY),
            "select_grants_evidence_grade": SELECTION_GRANTS_EVIDENCE_GRADE,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与摘要。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """结果摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[SelectedCandidate]:
        return iter(self.reserved)


def top_k_reserved(
    source: Any,
    k: int | None = None,
    *,
    rotation: Sequence[str] = RESERVE_ROTATION,
    require_single_knowledge_version: bool = True,
    **rejected: Any,
) -> ReserveSelection:
    """在前沿内部预约 ``k`` 个代表性候选（验收标准③的实现手段）。

    取舍机制是**轮转**而非加权总分：第 i 个名额按 ``rotation`` 的第 i 个维度
    择优，循环使用，使预约覆盖不同维度。这样既不需要把四个维度折算成一个数，
    也不会让某一维度的高分掩盖另一维度的低分。

    :param source: :class:`ParetoFront`，或评估记录的可迭代集合
        （后者会先调用 :func:`pareto_front`）。
    :param k: 预约数量；``None`` 表示取满整个前沿。
    :param rotation: 轮转维度顺序，默认 :data:`RESERVE_ROTATION`。
    :param require_single_knowledge_version: 是否要求共用同一个 :math:`K^{(v)}`。
    :return: :class:`ReserveSelection`。
    :raises SelectionPolicyError: 传入加权 / 总分类参数。
    :raises SelectionInputError: ``k`` 非法或轮转序列非法。
    """
    _reject_score_attempts(rejected, "top_k_reserved")
    rotation_names = _validate_rotation(rotation)

    if isinstance(source, ParetoFront):
        front = source
    else:
        front = pareto_front(
            source,
            require_single_knowledge_version=require_single_knowledge_version,
        )
    front_records = _normalize(front.front)

    if k is None:
        requested = len(front_records)
    else:
        if isinstance(k, bool) or not isinstance(k, int):
            raise SelectionInputError(
                f"k 必须是整数或 None，实际得到：{k!r}"
            )
        if k < 0:
            raise SelectionInputError(f"k 必须非负，实际得到：{k!r}")
        requested = k

    picked: set[str] = set()
    reserved = _select_from_layer(
        front_records,
        limit=requested,
        rotation=rotation_names,
        picked=picked,
        rank=0,
    )
    return ReserveSelection(
        reserved=tuple(reserved),
        requested_k=requested,
        available=len(front_records),
        shortfall=max(0, requested - len(reserved)),
        rotation=rotation_names,
        directions=dict(OPTIMIZATION_DIRECTIONS),
        knowledge_version=front.knowledge_version,
    )


@dataclass(frozen=True)
class FreezeSelection:
    """交给冻结阶段的候选清单。

    本对象是**建议**：不授予证据等级、不触发任何确证流程。

    :param candidates: 入选候选（按选择顺序）。
    :param cap: 本次使用的上限。
    :param selected_count: 实际入选数量。
    :param shortfall: 缺口 = ``max(0, cap - 实际数量)``（候选不足时如实记录）。
    :param front_size: 输入的前沿规模。
    :param layer_count: 输入层数。
    :param included_lower_layers: 是否允许从低层（被支配层）回填。
    :param allow_incomplete: 是否允许缺维度候选入选。
    :param excluded_incomplete_ids: 因缺维度被排除的候选标识。
    :param selected_from_front: 入选者中来自前沿（rank 0）的数量。
    :param rotation: 本次使用的轮转维度顺序。
    :param directions: 方向声明。
    :param knowledge_version: 本次比较所依据的 :math:`K^{(v)}`。
    :param note: 口径说明。
    """

    candidates: tuple[SelectedCandidate, ...]
    cap: int
    selected_count: int
    shortfall: int
    front_size: int
    layer_count: int
    included_lower_layers: bool
    allow_incomplete: bool
    excluded_incomplete_ids: tuple[str, ...]
    selected_from_front: int
    rotation: tuple[str, ...]
    directions: Mapping[str, str]
    knowledge_version: str | None
    note: str = FREEZE_CAP_NOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "directions", MappingProxyType(dict(self.directions)))

    def ids(self) -> tuple[str, ...]:
        """入选候选的标识。"""
        return tuple(item.identity for item in self.candidates)

    def complete_ids(self) -> tuple[str, ...]:
        """入选者中维度完备的候选标识。"""
        return tuple(item.identity for item in self.candidates if item.complete)

    def incomplete_ids(self) -> tuple[str, ...]:
        """入选者中缺维度的候选标识。"""
        return tuple(item.identity for item in self.candidates if not item.complete)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "select_version": SELECT_VERSION,
            "candidates": [item.to_dict() for item in self.candidates],
            "cap": self.cap,
            "selected_count": self.selected_count,
            "shortfall": self.shortfall,
            "front_size": self.front_size,
            "layer_count": self.layer_count,
            "included_lower_layers": self.included_lower_layers,
            "allow_incomplete": self.allow_incomplete,
            "excluded_incomplete_ids": list(self.excluded_incomplete_ids),
            "selected_from_front": self.selected_from_front,
            "rotation": list(self.rotation),
            "directions": _jsonable(self.directions),
            "knowledge_version": self.knowledge_version,
            "freeze_cap_note": self.note,
            "no_total_score_note": NO_TOTAL_SCORE_NOTE,
            "confirmation_policy": _jsonable(CONFIRMATION_POLICY),
            "select_grants_evidence_grade": SELECTION_GRANTS_EVIDENCE_GRADE,
            "scope_note": SELECTION_SCOPE_NOTE,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与摘要。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """结果摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __len__(self) -> int:
        return self.selected_count

    def __iter__(self) -> Iterator[SelectedCandidate]:
        return iter(self.candidates)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"FreezeSelection(cap={self.cap}, selected={self.selected_count}, "
            f"shortfall={self.shortfall}, front={self.front_size})"
        )


def select_freeze_candidates(
    evaluations: Any,
    *,
    max_candidates: int = DEFAULT_FREEZE_CAP,
    allow_incomplete: bool = False,
    include_lower_layers: bool = False,
    rotation: Sequence[str] = RESERVE_ROTATION,
    require_single_knowledge_version: bool = True,
    **rejected: Any,
) -> FreezeSelection:
    """生成冻结候选清单，数量上限可配置（验收标准②）。

    选择顺序（每一步都不涉及跨维度加权）：

    1. **前沿优先。** 先在 rank 0 层内按轮转顺序择优，直到用满上限。
    2. **低层回填（可选）。** 仅当 ``include_lower_layers=True`` 时才从
       rank 1、2…依层回填；层序本身就是偏序的延伸，不是总分排名。
       默认关闭——此时前沿不足会在 ``shortfall`` 中如实记录，而**不**用
       低层候选凑数。
    3. **缺维度候选（可选）。** 仅当 ``allow_incomplete=True`` 时才可能入选，
       且恒排在完备候选之后并被标记 ``complete=False``。

    :param evaluations: 评估记录的可迭代集合（对象或字典）。
    :param max_candidates: 冻结候选上限，默认 :data:`DEFAULT_FREEZE_CAP`（即 5）；
        取值范围 ``1..FREEZE_CAP_HARD_LIMIT``。
    :param allow_incomplete: 是否允许缺维度候选入选（默认 ``False``）。
    :param include_lower_layers: 前沿不足时是否从低层回填（默认 ``False``）。
    :param rotation: 轮转维度顺序。
    :param require_single_knowledge_version: 是否要求共用同一个 :math:`K^{(v)}`。
    :return: :class:`FreezeSelection`。
    :raises SelectionPolicyError: 传入加权 / 总分类参数。
    :raises SelectionInputError: 上限非法或轮转序列非法。
    :raises SelectionVersionError: 知识库版本缺失或跨版本混用。
    """
    _reject_score_attempts(rejected, "select_freeze_candidates")
    rotation_names = _validate_rotation(rotation)

    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise SelectionInputError(
            f"max_candidates 必须是正整数，实际得到：{max_candidates!r}"
        )
    if not 1 <= max_candidates <= FREEZE_CAP_HARD_LIMIT:
        raise SelectionInputError(
            f"max_candidates 必须落在 1..{FREEZE_CAP_HARD_LIMIT} 内，"
            f"实际得到：{max_candidates!r}"
            f"（默认 {DEFAULT_FREEZE_CAP}，硬边界锚定框架说明 §2 每轮探索候选上界）"
        )

    # 先把输入物化为列表：本函数需要两次遍历（一次求前沿、一次还原原始对象），
    # 若调用方传入的是生成器，二次遍历会得到空集合。
    if isinstance(evaluations, (str, bytes)) or not isinstance(evaluations, Iterable):
        raise SelectionInputError(
            "evaluations 必须是评估记录的可迭代集合，实际得到："
            f"{type(evaluations).__name__}"
        )
    items = list(evaluations)

    front = pareto_front(
        items,
        require_single_knowledge_version=require_single_knowledge_version,
    )
    records_all = _normalize(items)
    by_identity = {record.identity: record for record in records_all}

    # 分层记录的内部形态：按层号还原规范记录，层内顺序与 ParetoFront 一致。
    layer_records: list[list[_Candidate]] = [
        [by_identity[identity] for identity in front.identities_of_layer(rank)]
        for rank in range(front.layer_count)
    ]

    picked: set[str] = set()
    chosen: list[SelectedCandidate] = []

    # 第 1 步：前沿
    if layer_records:
        chosen.extend(
            _select_from_layer(
                layer_records[0],
                limit=max_candidates,
                rotation=rotation_names,
                picked=picked,
                rank=0,
            )
        )
    # 第 2 步：低层回填（可选）
    if include_lower_layers:
        for rank, layer in enumerate(layer_records[1:], start=1):
            if len(chosen) >= max_candidates:
                break
            chosen.extend(
                _select_from_layer(
                    layer,
                    limit=max_candidates - len(chosen),
                    rotation=rotation_names,
                    picked=picked,
                    rank=rank,
                )
            )
    # 第 3 步：缺维度候选（可选），恒排在最后并被标记
    excluded_incomplete: list[str] = []
    if allow_incomplete:
        incomplete_records = sorted(
            (record for record in records_all if not record.complete),
            key=lambda record: record.sort_key(),
        )
        for record in incomplete_records:
            if len(chosen) >= max_candidates:
                excluded_incomplete.append(record.identity)
                continue
            if record.identity in picked:
                continue
            picked.add(record.identity)
            chosen.append(
                SelectedCandidate(
                    identity=record.identity,
                    hypothesis_id=record.hypothesis_id,
                    evaluation=record.source,
                    rank=None,
                    rotation_dimension=None,
                    rationale=(
                        "该候选缺维度 "
                        f"{list(record.missing)}（证据不足），"
                        "因调用方显式开启 allow_incomplete 而入选，"
                        "恒排在完备候选之后；不参与支配比较，"
                        "入选不代表其证据强度获得任何认定。"
                    ),
                    incomplete_dimensions=record.missing,
                )
            )
    else:
        excluded_incomplete = [
            record.identity for record in records_all if not record.complete
        ]

    from_front = sum(1 for item in chosen if item.rank == 0)
    return FreezeSelection(
        candidates=tuple(chosen),
        cap=max_candidates,
        selected_count=len(chosen),
        shortfall=max(0, max_candidates - len(chosen)),
        front_size=front.front_size,
        layer_count=front.layer_count,
        included_lower_layers=include_lower_layers,
        allow_incomplete=allow_incomplete,
        excluded_incomplete_ids=tuple(excluded_incomplete),
        selected_from_front=from_front,
        rotation=rotation_names,
        directions=dict(OPTIMIZATION_DIRECTIONS),
        knowledge_version=front.knowledge_version,
    )
