"""P13 交付物：结果三分类、记录与发布（模块 M6）。

本模块是**确证执行链的收尾**：P12 已经在冻结计划的约束下原子化消费了某轮封存
确证分区、完整执行了检验族并给出机械的「是否超过本轮阈值」判定；本模块把这个
机械判定转换成**有语义的三分类结论**，把结论登记进证据库，并在登记之后发布
给探索侧。理论依据见
`DS协作指南-阶段划分与提示词.md <../DS协作指南-阶段划分与提示词.md>`_ §3 P13 与
§4.3「禁止让 ds 直接给出证据等级或统计结论」；
接口约定见 `INTERFACES.md <../INTERFACES.md>`_ §2.6 与 §3，
契约原文见 `IMPLEMENTATION_CONTRACT.md <../IMPLEMENTATION_CONTRACT.md>`_ 中
``record_evaluation`` 与 ``release_results`` 两条：

* ``record_evaluation(binding_id, result)``：result 为
  ``{status: supported|refuted|inconclusive|failed, metrics:{}, notes:"...",
  code_version:"..."}``；*records external M6 results, does not compute p values
  or certify truth*；失败尝试被保留，最终结果不可变。
* ``release_results(binding_id)``：发布已登记结果给探索侧，*never raw sealed
  records*。

三条验收标准的实现方式
-----------------------

① **不显著一律归 ``inconclusive``，不得判为反驳、也不得据此认定零假说成立。**
   分类规则在 :func:`classify_result` 中实现，是**穷尽且互斥**的五条：

   1. ``available=False``（该条检验未产出统计结果）→ :data:`STATUS_FAILED`；
   2. 已产出且 ``p > 阈值``（不显著）→ :data:`STATUS_INCONCLUSIVE`；
   3. 显著但方向未披露（``effect`` 缺失或为 0）→ :data:`STATUS_INCONCLUSIVE`；
   4. 显著且方向与预定方向一致 → :data:`STATUS_SUPPORTED`；
   5. 显著且方向与预定方向相反 → :data:`STATUS_REFUTED`。

   规则 2 是本条验收的核心：**不显著只说明「没能排除零假说」，既不构成反驳，
   也不构成对零假说的证明**——「没有证据」不等于「证明没有」。
   规则 3 是它的必要补充：方向未披露时**不猜测方向**，宁可归为证据不足。
   规则 1 与规则 2 严格区分「没算」（``failed``）与「算出来不显著」
   （``inconclusive``），与 P12 的 :data:`sdl_m06.execute.COMPLETE_FAMILY_BOUNDARY_NOTE`
   口径一致。

   相关口径以常量形式随结果对象输出：:data:`NON_SIGNIFICANT_NOTE`、
   :data:`INCONCLUSIVE_NOTE`、:data:`REFUTED_REQUIRES_DIRECTION_NOTE`、
   :data:`UNAVAILABLE_IS_FAILED_NOTE`。

② **``status`` 取值严格限于契约四值。**
   :data:`RESULT_STATUSES` 是唯一合法取值集合，且与 M1 的
   ``record_evaluation`` 校验分支**逐字一致**（M1 侧同样只接受这四个字符串）。
   :class:`Classification` / :class:`HypothesisOutcome` /
   :class:`RoundClassification` 三个结果对象都在 ``__post_init__`` 中拒绝越界取值；
   :func:`aggregate_status` 的输出也只在四值内取值。
   登记载荷由 :func:`build_evaluation_payload` **逐键显式装配**，键集恒等于
   :data:`RESULT_PAYLOAD_FIELDS`（契约原文的四项），不做任何扩张。

③ **发布只含已登记结果，绝不外泄封存原始记录。**
   三重保证：

   - **来路**：本模块**没有**任何读取确证记录的路径。它只消费 P12 的结果对象
     （其携带的只是「计划的零假说/备择原文」与「预先声明的统计事实」，
     不是封存原始记录）。模块内不存在 :func:`_forbidden_access_names` 所列的
     任何数据访问入口，也不调用 ``consume_confirmation`` / ``read_dataset``
     （见 :data:`NOT_PROVIDED_BY_P13`）。
   - **出闸**：任何即将登记或已经发布的载荷都要过 :func:`check_no_sealed_leak`
     的**深度键名扫描**（:data:`SEALED_LEAK_KEYS`）——``values`` / ``record_id`` /
     ``operator_annotation`` / ``_quality`` / ``snapshot`` 一类原始记录字段一旦出现
     即拒绝（:class:`ReleaseError`），而不是「靠代码写对」。
   - **对账**：:func:`record_and_release` 在发布后逐条核对发布内容——
     每一条都必须是合法结果载荷、都必须归属于同一 ``binding_id``、
     且本次登记的那一条必须原样出现在发布内容中（:func:`_check_only_registered`）。
     发布内容中多出任何非结果对象都会被拒绝。

本模块的定位与边界
------------------

- **不计算任何统计量。** p 值、效应量、阈值全部由 P12 与外部预先声明的检验方案
  给出；本模块只做「机械判定 → 语义结论」的映射与登记。
  （契约：*This records external M6 results, does not compute p values or certify
  truth.*）
- **不授予证据等级。** E0/E1/E2 的判定与因果升级属 P15 的归档职责；
  本模块输出的 ``status`` 是**本轮检验结论**，不是知识等级，也不是因果断言
  （:data:`NO_EVIDENCE_GRADE_NOTE`）。
- **不归档、不做主动取证。** 归档与假说更新属 P15（``archive_round`` /
  ``update_knowledge_version`` / ``revise_or_retire``），主动取证属 P14
  （``divergence`` / ``acquisition_plan``）。这些入口名列入
  :data:`NOT_PROVIDED_BY_P13`，并由测试用 AST 机检「本层未定义它们」。
- **不重算执行口径。** 不重新实现 :func:`sdl_m06.execute.execute_family` /
  ``holm_adjust`` / ``alpha_for_round``，只引用其结果。
- 不修改 ``sdl_m01/``，不读取/打印/写入角色令牌、evidence 数据库内容或受限质量报告，
  不引入第三方依赖。

术语约定（全文一致）
--------------------

- **机械判定（significant）**：P12 给出的 ``p ≤ 本轮 Holm 阈值``，是**纯数值事实**。
- **结论（status）**：本模块给出的语义标签，取值限于 :data:`RESULT_STATUSES`。
- **方向（direction）**：效应量的符号，``positive`` / ``negative``；
  未披露时为 ``None``（此时不推断方向）。
- **预定方向（predicted_direction）**：冻结计划的备择所声明的效应方向。
  计划族的零假说/备择模板（P11 :data:`sdl_m06.freeze.ALTERNATIVE_TEMPLATE`）
  声明的是「效应**超过**预定效应阈值」，故缺省为 ``positive``；
  若调用方的声明相反，须显式传 ``predicted_direction="negative"``。
- **登记（record）**：把结论写入证据库的 evaluations 表，一次绑定**只登记一条**
  非失败结论（M1 的不可变约束）。
- **发布（release）**：把已登记结论开放给探索侧读取；发布内容仍只是结论载荷，
  不含任何封存原始记录。
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

from sdl_m06.execute import (
    FamilyExecution,
    TestExecution,
    canonical_json,
)

__all__ = [
    # 异常
    "ReportError",
    "ReportInputError",
    "ClassificationError",
    "ReleaseError",
    # 常量：版本与四值状态
    "REPORT_VERSION",
    "STATUS_SUPPORTED",
    "STATUS_REFUTED",
    "STATUS_INCONCLUSIVE",
    "STATUS_FAILED",
    "RESULT_STATUSES",
    "STATUS_LABELS",
    # 常量：分类口径
    "DEFAULT_PREDICTED_DIRECTION",
    "DIRECTION_VALUES",
    "DIRECTION_POSITIVE",
    "DIRECTION_NEGATIVE",
    "CONTRACT_STATUS_NOTE",
    "NON_SIGNIFICANT_NOTE",
    "INCONCLUSIVE_NOTE",
    "REFUTED_REQUIRES_DIRECTION_NOTE",
    "UNAVAILABLE_IS_FAILED_NOTE",
    "DIRECTION_NOTE",
    "AGGREGATION_NOTE",
    "PRIMARY_SCOPE_NOTE",
    "NO_EVIDENCE_GRADE_NOTE",
    "SEALED_BOUNDARY_NOTE",
    "RELEASE_SCOPE_NOTE",
    # 常量：载荷形状与白名单
    "RESULT_PAYLOAD_FIELDS",
    "RELEASE_STAMP_FIELDS",
    "METRICS_KEYS",
    "FACT_INPUT_KEYS",
    "SEALED_LEAK_KEYS",
    "FORBIDDEN_CALL_KEYS",
    "NOT_PROVIDED_BY_P13",
    # 入口
    "classify_result",
    "record_and_release",
    # 辅助
    "direction_of",
    "aggregate_status",
    "build_evaluation_payload",
    "check_no_sealed_leak",
    "is_result_payload",
    "self_check",
    # 结果对象
    "Classification",
    "HypothesisOutcome",
    "RoundClassification",
    "RecordReleaseReceipt",
]


# ---------------------------------------------------------------------------
# 常量：版本与契约四值状态（验收标准②）
# ---------------------------------------------------------------------------

#: 交付物版本号。写入登记载荷的 ``code_version``，便于追溯分类口径的变更。
REPORT_VERSION = "P13-v1.0"

#: 契约四值（与 M1 ``record_evaluation`` 的校验分支逐字一致）。
STATUS_SUPPORTED = "supported"
STATUS_REFUTED = "refuted"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_FAILED = "failed"

#: **合法 status 取值集合**，顺序与契约原文一致。
RESULT_STATUSES: tuple[str, ...] = (
    STATUS_SUPPORTED,
    STATUS_REFUTED,
    STATUS_INCONCLUSIVE,
    STATUS_FAILED,
)

#: 供人阅读的中文标签。只用于 ``notes`` 文本，**不进入**结构化字段——
#: 结构化字段一律使用契约英文值，避免出现两套并行取值。
STATUS_LABELS: Mapping[str, str] = MappingProxyType(
    {
        STATUS_SUPPORTED: "支持（本轮检验支持该假说的预定方向）",
        STATUS_REFUTED: "反驳（本轮检验给出与预定方向相反的显著效应）",
        STATUS_INCONCLUSIVE: "证据不足（未达显著，或显著但方向未披露）",
        STATUS_FAILED: "失败（该轮检验未产出可判定的统计结果）",
    }
)

#: 契约四值的口径说明（验收标准②的可读形式）。
CONTRACT_STATUS_NOTE = (
    "status 取值严格限于契约四值 supported / refuted / inconclusive / failed，"
    "不得引入第五种取值，也不得用中文或近义词作为结构化取值"
    "（中文标签只允许出现在 notes 文本中）。"
)


# ---------------------------------------------------------------------------
# 常量：分类口径（验收标准①）
# ---------------------------------------------------------------------------

#: 方向取值。刻意只有两个：不给「未定」留一个可被误读为「中性结论」的取值。
DIRECTION_POSITIVE = "positive"
DIRECTION_NEGATIVE = "negative"
DIRECTION_VALUES: tuple[str, ...] = (DIRECTION_POSITIVE, DIRECTION_NEGATIVE)

#: 预定方向缺省值。P11 的零假说/备择模板声明的是「效应**超过**预定效应阈值」，
#: 因此缺省方向为正向；声明相反时必须显式覆盖。
DEFAULT_PREDICTED_DIRECTION = DIRECTION_POSITIVE

#: **验收标准①的核心口径**：不显著只说明没能排除零假说。
NON_SIGNIFICANT_NOTE = (
    "不显著（p > 本轮 Holm 阈值）一律归为 inconclusive（证据不足）："
    "它既不构成对假说的反驳，也不构成对零假说的证明。"
    "明确地说：不得判为反驳，也不得据此认定零假说成立。"
    "「没有证据」不等于「证明没有」——不得把 inconclusive 读成「已证否」，"
    "也不得把 inconclusive 读成「零假说成立」。"
)

#: ``inconclusive`` 的完整含义。
INCONCLUSIVE_NOTE = (
    "inconclusive 表示本轮数据不足以在预定效应阈值上作出方向性判断。"
    "它是一条**待补证据**的记录，而不是一个否定性结论："
    "后续轮次可以重新设计取样与检验（见 P14 的取证计划）后再次确证。"
)

#: 显著但方向未披露时的处理口径。
REFUTED_REQUIRES_DIRECTION_NOTE = (
    "显著但方向未披露（效应量缺失或为 0）时归为 inconclusive："
    "本模块不猜测方向。反驳必须建立在「显著且方向与预定方向相反」之上，"
    "仅有显著性不足以判定反驳。"
)

#: 「没算」与「不显著」的区分口径。
UNAVAILABLE_IS_FAILED_NOTE = (
    "未产出统计结果的检验记为 failed，而不是 inconclusive："
    "failed 表示「这一轮没算出来」，inconclusive 表示「算出来了但不显著」。"
    "二者在外部后果上截然不同——后者可以据此判断该轮设计是否足够敏感，"
    "前者只能说明执行失败。严禁把 failed 混记为 inconclusive。"
)

#: 方向判定的口径。
DIRECTION_NOTE = (
    "方向由效应量的符号确定：> 0 为 positive，< 0 为 negative，等于 0 或缺失为 None。"
    "「显著」已经包含了对预定效应阈值 δ 的比较（由 P12 的检验族给出），"
    "因此本模块的方向判定只负责符号，不重复比较幅度。"
)

#: 聚合口径（验收标准①②在整族层面的落地）。
AGGREGATION_NOTE = (
    "整族结论由各假说结论聚合：全部为 failed → failed；"
    "方向性结论只有 supported → supported，只有 refuted → refuted；"
    "若同时出现 supported 与 refuted（方向冲突），归为 inconclusive——"
    "证据互相冲突时不得择优采信，也不得按多数表决产生一个方向性结论。"
)

#: 主要指标口径。
PRIMARY_SCOPE_NOTE = (
    "整族结论优先由主要指标的检验聚合；若某假说未登记主要指标检验，"
    "则退化为使用其全部检验，并在结果对象中把 scope 标为 all_tests，"
    "使「用了哪一批检验」成为可读事实而非隐含行为。"
)

#: 「本层不给证据等级」的口径。
NO_EVIDENCE_GRADE_NOTE = (
    "本模块只输出本轮检验结论（status），不授予 E0/E1/E2 证据等级，"
    "也不做任何因果升级——证据等级的记录与历史身份管理属 P15 的归档职责。"
    "LLM 的文本合理性不得替代统计证据来授予等级。"
)


# ---------------------------------------------------------------------------
# 常量：发布边界（验收标准③）
# ---------------------------------------------------------------------------

#: 封存边界的口径说明。
SEALED_BOUNDARY_NOTE = (
    "发布只传递已登记的结果载荷（status / metrics / notes / code_version），"
    "绝不返回封存确证的原始记录。本模块没有任何读取原始记录的路径："
    "它只消费 P12 的结果对象，且所有出闸载荷都要过封存键名扫描。"
)

#: 发布范围说明。
RELEASE_SCOPE_NOTE = (
    "发布是「开放已登记结论给探索侧读取」这一步，它不构成对结论真实性的担保："
    "记录外部评估不等于证明结论为真（契约原文：does not certify truth）。"
    "发布之后原始绑定仍保留应有状态，归档由保管侧另行完成。"
)

#: **契约规定的登记载荷字段**：恰好四项，逐项对应 M1 的校验分支。
RESULT_PAYLOAD_FIELDS: tuple[str, ...] = (
    "status",
    "metrics",
    "notes",
    "code_version",
)

#: M1 在登记/发布时额外加盖的字段。它们由证据库生成，不由本模块写入，
#: 但在发布内容里合法存在，故单列为「印记字段」而不算越界扩张。
RELEASE_STAMP_FIELDS: frozenset[str] = frozenset({"binding_id", "recorded_by"})

#: ``metrics`` 的**顶层允许键**。嵌套层允许出现假说 id 作为键
#: （例如 ``{"hypotheses": {"h1": "supported"}}``），故校验只作用于顶层。
METRICS_KEYS: frozenset[str] = frozenset(
    {
        "test_count",
        "available_count",
        "supported_count",
        "refuted_count",
        "inconclusive_count",
        "failed_count",
        "significant_count",
        "primary_metric",
        "primary_status",
        "scope",
        "min_p_value",
        "max_p_value",
        "hypotheses",
        "round_index",
        "alpha",
    }
)

#: 以「事实映射」形态调用 :func:`classify_result` 时允许的键。
FACT_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "test_id",
        "hypothesis_id",
        "metric",
        "subgroup",
        "available",
        "p_value",
        "threshold",
        "effect",
    }
)

#: **封存原始记录的键名特征**。出现任一即拒绝登记/发布。
#:
#: 这是一道「宁可拒绝」的闸门：本模块的输出载荷根本不需要这些字段，
#: 因此把它们全部列为禁区不会牺牲任何合法能力，却能挡住
#: 「顺手把记录正文塞进 metrics 一起发布」这条最容易发生的泄漏路径。
SEALED_LEAK_KEYS: frozenset[str] = frozenset(
    {
        # 记录正文与标识
        "values",
        "records",
        "records_body",
        "raw",
        "raw_records",
        "dataset",
        "dataset_ref",
        "record_id",
        "group_ids",
        "operator_annotation",
        # 封存与质量
        "snapshot",
        "snapshot_hash",
        "sealed_counts",
        "sealed_report",
        "quality_report",
        "_quality",
        # 来源与时间戳（原始资料的定位信息）
        "source",
        "source_ref",
        "available_time",
        "event_time",
        "units",
        # 令牌
        "token",
        "role_token",
        "token_body",
    }
)

#: 明确拒绝的关键字参数。三类：
#:
#: 1. **记录读取类**：本模块只能消费 P12 的结果对象，不接受任何数据入口；
#: 2. **筛选类**：不容许「只登记成功项」这类选择性上报；
#: 3. **令牌类**：不得进入本层输入。
FORBIDDEN_CALL_KEYS: frozenset[str] = frozenset(
    {
        "records",
        "raw",
        "dataset",
        "read_dataset",
        "quality",
        "only",
        "exclude",
        "subset",
        "filter",
        "select",
        "drop",
        "token",
        "role_token",
    }
)

#: 本模块**不提供**的接口名。测试据此断言「P13 未越界实现其它阶段的内容」。
#:
#: 注意：本模块自己的 :func:`classify_result` / :func:`record_and_release`
#: **不在此列**，它们是本阶段的交付内容。
NOT_PROVIDED_BY_P13: tuple[str, ...] = (
    # P14 的主动取证
    "divergence",
    "acquisition_plan",
    # P15 的归档与假说更新
    "archive_round",
    "archive_confirmation",
    "update_knowledge_version",
    "revise_or_retire",
    # P16 的主循环集成
    "loop",
    "run_pipeline",
    # P12 的执行口径（本模块只引用其结果，不重算）
    "execute_family",
    "holm_adjust",
    "alpha_for_round",
    # P11 的构造（本模块不重建计划、不做绑定）
    "build_frozen_plan",
    "bind",
    # P09/P10 的度量与筛选
    "evaluate_hypothesis",
    "pareto_front",
    # M1 的数据读取与消费入口（本模块一条都不用）
    "consume_confirmation",
    "read_dataset",
    "read_records",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ReportError(ValueError):
    """本模块所有异常的基类，继承自 :class:`ValueError`。"""


class ReportInputError(ReportError):
    """输入形态非法（类型不对、缺字段、键名受限、参数越界等）。"""


class ClassificationError(ReportError):
    """分类无法进行或口径不一致（缺失可判定信息、判定事实互相矛盾等）。

    单列一个子类，是为了让「分类器拒绝给出结论」与「结论是 inconclusive」
    这两件截然不同的事**在异常类型上就可区分**：
    前者说明输入不足以支撑任何结论，后者本身就是一个合法结论。
    """


class ReleaseError(ReportError):
    """登记或发布越界（载荷含封存字段、发布内容与已登记结果不一致等）。

    验收标准③的可机检形式：一旦发布内容里出现封存原始记录的特征键，
    或与已登记结果对不上，就以此异常中止，而不是把可疑载荷交出去。
    """


# ---------------------------------------------------------------------------
# 基础校验工具
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """是否为有限实数（排除 ``bool``——它在 Python 里是 ``int`` 的子类）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_number(value: Any, label: str) -> float:
    """校验有限实数并返回 ``float``。

    :raises ReportInputError: 非有限实数。
    """
    if not _is_number(value):
        raise ReportInputError(f"{label} 必须是有限实数，实际得到：{value!r}")
    return float(value)


def _require_text(value: Any, label: str) -> str:
    """校验非空字符串。

    :raises ReportInputError: 非字符串或全为空白。
    """
    if not isinstance(value, str) or not value.strip():
        raise ReportInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """校验映射类型。

    :raises ReportInputError: 非映射。
    """
    if not isinstance(value, Mapping):
        raise ReportInputError(f"{label} 必须是映射，实际得到：{type(value).__name__}")
    return value


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    """校验序列类型（排除 ``str`` / ``bytes`` 这类「像序列的字符串」）。

    :raises ReportInputError: 不是可迭代序列。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReportInputError(f"{label} 必须是序列，实际得到：{type(value).__name__}")
    return value


def _check_status(value: Any, label: str) -> str:
    """校验 status 落在契约四值内（验收标准②）。

    :raises ClassificationError: 非字符串、空白或取值越界。
    """
    if not isinstance(value, str) or not value.strip():
        raise ClassificationError(
            f"{label} 必须是契约四值之一的非空字符串，实际得到：{value!r}。"
            + CONTRACT_STATUS_NOTE
        )
    if value not in RESULT_STATUSES:
        raise ClassificationError(
            f"{label} 取值 {value!r} 不在契约四值内（{list(RESULT_STATUSES)}）："
            + CONTRACT_STATUS_NOTE
        )
    return value


def _validate_direction(value: Any, label: str = "predicted_direction") -> str:
    """校验方向取值。

    :raises ReportInputError: 不是 ``positive`` / ``negative``。
    """
    text = _require_text(value, label)
    if text not in DIRECTION_VALUES:
        raise ReportInputError(
            f"{label} 必须是 {list(DIRECTION_VALUES)} 之一，实际得到：{value!r}"
        )
    return text


def _plain(value: Any) -> Any:
    """把只读包装（``MappingProxyType``）等还原为普通可序列化对象。"""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _reject_forbidden_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝受限的关键字参数（见 :data:`FORBIDDEN_CALL_KEYS`）。

    :raises ReportInputError: 传入受限键名。
    """
    for key in kwargs:
        lowered = str(key).lower()
        if lowered in FORBIDDEN_CALL_KEYS:
            raise ReportInputError(
                f"{where} 不接受参数 {key!r}：" + _forbidden_reason(lowered)
            )


def _forbidden_reason(key: str) -> str:
    """给出某个受限键被拒绝的原因（可读的中文说明）。"""
    if key in ("records", "raw", "dataset", "read_dataset", "quality"):
        return (
            "本模块只消费 P12 的结果对象，不接受任何数据入口；"
            "封存确证的原始记录不得进入登记与发布路径。" + SEALED_BOUNDARY_NOTE
        )
    if key in ("only", "exclude", "subset", "filter", "select", "drop"):
        return (
            "登记与发布不接受筛选参数：整轮检验结论必须完整登记，"
            "不得只上报成功项（失败与不可得项都要如实进入结果）。"
        )
    return "令牌类键名不得进入本层输入。"


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """单条检验的三分类结论（验收标准①②的最小载体）。

    :param test_id: 检验标识（与冻结计划的 ``test_family`` 一致）。
    :param hypothesis_id: 所属假说。
    :param status: 契约四值之一。
    :param metric: 指标（用于主要指标筛选）。
    :param subgroup: 子群。
    :param available: 该条检验是否产出了统计结果。
        ``False`` 表示「没算」，此时 :attr:`status` 恒为 ``failed``。
    :param p_value: 原始 p 值（未产出时为 ``None``）。
    :param threshold: 本轮该条检验的判定阈值
        （P12 的 ``α_t / (m - k + 1)``；未产出时为 ``None``）。
    :param significant: 机械判定（``p ≤ threshold``）；未产出时为 ``None``。
        **它不构成结论**，只是结论的输入事实。
    :param effect: 效应量（未披露时为 ``None``）。
    :param direction: 效应量符号（``positive`` / ``negative``）；未披露时为 ``None``。
    :param predicted_direction: 该假说备择声明的方向。
    :param reasons: 分类依据的可读说明（逐条）。
    """

    test_id: str
    hypothesis_id: str
    status: str
    metric: str = ""
    subgroup: str = ""
    available: bool = False
    p_value: float | None = None
    threshold: float | None = None
    significant: bool | None = None
    effect: float | None = None
    direction: str | None = None
    predicted_direction: str = DEFAULT_PREDICTED_DIRECTION
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_status(self.status, "Classification.status")
        if self.direction is not None and self.direction not in DIRECTION_VALUES:
            raise ClassificationError(
                f"Classification.direction 必须是 None 或 {list(DIRECTION_VALUES)} 之一，"
                f"实际得到：{self.direction!r}"
            )
        _validate_direction(self.predicted_direction, "Classification.predicted_direction")
        if not self.available and self.status != STATUS_FAILED:
            raise ClassificationError(
                "未产出统计结果的检验只能归为 failed，实际得到："
                f"{self.status!r}。" + UNAVAILABLE_IS_FAILED_NOTE
            )
        if self.available and self.status == STATUS_FAILED:
            raise ClassificationError(
                "已产出统计结果的检验不得归为 failed：failed 专指「没算出来」。" 
                + UNAVAILABLE_IS_FAILED_NOTE
            )
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def directional(self) -> bool:
        """是否为方向性结论（``supported`` / ``refuted``）。"""
        return self.status in (STATUS_SUPPORTED, STATUS_REFUTED)

    @property
    def label(self) -> str:
        """该结论的中文标签（只用于展示）。"""
        return STATUS_LABELS[self.status]

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "test_id": self.test_id,
            "hypothesis_id": self.hypothesis_id,
            "status": self.status,
            "metric": self.metric,
            "subgroup": self.subgroup,
            "available": self.available,
            "p_value": self.p_value,
            "threshold": self.threshold,
            "significant": self.significant,
            "effect": self.effect,
            "direction": self.direction,
            "predicted_direction": self.predicted_direction,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class HypothesisOutcome:
    """单个假说的轮次结论（登记载荷的内容来源）。

    :param hypothesis_id: 假说标识。
    :param status: 该假说的整族结论，取值限契约四值。
    :param metrics: 概要计数（只含计数与最值，**不含任何记录正文**）。
    :param test_ids: 参与聚合的检验标识（按名排序）。
    :param supported_test_ids / refuted_test_ids / inconclusive_test_ids /
        failed_test_ids: 按结论分组后的检验标识。
    :param scope: ``primary_metric``（用了主要指标检验）或 ``all_tests``（退化）。
    :param notes: 口径说明。
    """

    hypothesis_id: str
    status: str
    metrics: Mapping[str, Any] = MappingProxyType({})
    test_ids: tuple[str, ...] = ()
    supported_test_ids: tuple[str, ...] = ()
    refuted_test_ids: tuple[str, ...] = ()
    inconclusive_test_ids: tuple[str, ...] = ()
    failed_test_ids: tuple[str, ...] = ()
    scope: str = "all_tests"
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_status(self.status, "HypothesisOutcome.status")
        if self.scope not in ("primary_metric", "all_tests"):
            raise ReportInputError(
                "HypothesisOutcome.scope 必须是 primary_metric 或 all_tests，"
                f"实际得到：{self.scope!r}"
            )
        object.__setattr__(self, "metrics", MappingProxyType(_plain(dict(self.metrics))))
        for name in (
            "test_ids",
            "supported_test_ids",
            "refuted_test_ids",
            "inconclusive_test_ids",
            "failed_test_ids",
            "notes",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def label(self) -> str:
        """该结论的中文标签（只用于展示）。"""
        return STATUS_LABELS[self.status]

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "hypothesis_id": self.hypothesis_id,
            "status": self.status,
            "metrics": _plain(self.metrics),
            "test_ids": list(self.test_ids),
            "supported_test_ids": list(self.supported_test_ids),
            "refuted_test_ids": list(self.refuted_test_ids),
            "inconclusive_test_ids": list(self.inconclusive_test_ids),
            "failed_test_ids": list(self.failed_test_ids),
            "scope": self.scope,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RoundClassification:
    """一次确证轮次的完整分类结果（验收标准①②③的统一载体）。

    :param binding_id: 绑定标识（来自 P11/P12）。
    :param round_index: 研究轮次 :math:`t`（**不是**对话轮次）。
    :param alpha: 本轮误差预算 :math:`\\alpha_t`。
    :param aggregate_status: 整轮结论（由各假说结论聚合，见 :data:`AGGREGATION_NOTE`）。
    :param classifications: 逐条检验的结论（顺序与 P12 的执行结果一致）。
    :param outcomes: 逐假说的结论（顺序按首次出现）。
    :param primary_metric: 使用的主要指标；``None`` 表示「全部检验视作主要指标」。
    :param code_version: 分类口径版本。
    :param notes: 口径说明元组。
    """

    binding_id: str
    round_index: int
    alpha: float
    aggregate_status: str
    classifications: tuple[Classification, ...]
    outcomes: tuple[HypothesisOutcome, ...]
    primary_metric: str | None = None
    code_version: str = REPORT_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_status(self.aggregate_status, "RoundClassification.aggregate_status")
        object.__setattr__(self, "classifications", tuple(self.classifications))
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "notes", tuple(self.notes))

    # -- 只读视图 ---------------------------------------------------------

    @property
    def test_count(self) -> int:
        """检验总数（恒等于 P12 的族规模）。"""
        return len(self.classifications)

    @property
    def available_count(self) -> int:
        """产出了统计结果的检验数。"""
        return sum(1 for item in self.classifications if item.available)

    @property
    def supported_count(self) -> int:
        """结论为 ``supported`` 的检验数。"""
        return sum(1 for item in self.classifications if item.status == STATUS_SUPPORTED)

    @property
    def refuted_count(self) -> int:
        """结论为 ``refuted`` 的检验数。"""
        return sum(1 for item in self.classifications if item.status == STATUS_REFUTED)

    @property
    def inconclusive_count(self) -> int:
        """结论为 ``inconclusive`` 的检验数。"""
        return sum(1 for item in self.classifications if item.status == STATUS_INCONCLUSIVE)

    @property
    def failed_count(self) -> int:
        """结论为 ``failed`` 的检验数。"""
        return sum(1 for item in self.classifications if item.status == STATUS_FAILED)

    @property
    def statuses(self) -> tuple[str, ...]:
        """逐条结论（便于测试一次性核对取值合法性）。"""
        return tuple(item.status for item in self.classifications)

    def outcome_of(self, hypothesis_id: str) -> HypothesisOutcome:
        """按假说标识取结论。

        :raises ReportInputError: 该假说不在本轮结果内。
        """
        for item in self.outcomes:
            if item.hypothesis_id == hypothesis_id:
                return item
        raise ReportInputError(f"假说 {hypothesis_id!r} 不在本轮分类结果内。")

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "binding_id": self.binding_id,
            "round_index": self.round_index,
            "alpha": self.alpha,
            "aggregate_status": self.aggregate_status,
            "classifications": [item.to_dict() for item in self.classifications],
            "outcomes": [item.to_dict() for item in self.outcomes],
            "primary_metric": self.primary_metric,
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        """规范 JSON（键排序、无多余空格），供持久化与摘要使用。"""
        return canonical_json(self.to_dict())

    def content_digest(self) -> str:
        """本次分类结果的规范摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordReleaseReceipt:
    """登记与发布的结果凭证（验收标准③的可核对形式）。

    :param binding_id: 绑定标识。
    :param status: 已登记结论的 status（限契约四值）。
    :param payload: 本模块提交给 ``record_evaluation`` 的载荷（契约四项）。
    :param recorded: ``record_evaluation`` 的返回值（含证据库加盖的印记字段）。
    :param released: ``release_results`` 的返回值（已发布内容）。
    :param published: 已发布内容中的结果条数（逐条为结果载荷，只读视图）。
    :param payload_digest: :attr:`payload` 的规范摘要，供事后核对登记内容未被替换。
    :param code_version: 登记口径版本。
    :param notes: 口径说明元组。
    """

    binding_id: str
    status: str
    payload: Mapping[str, Any]
    recorded: Mapping[str, Any]
    released: Mapping[str, Any]
    published: tuple[Mapping[str, Any], ...] = ()
    payload_digest: str = ""
    code_version: str = REPORT_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_status(self.status, "RecordReleaseReceipt.status")
        object.__setattr__(self, "payload", MappingProxyType(_plain(dict(self.payload))))
        object.__setattr__(self, "recorded", MappingProxyType(_plain(dict(self.recorded))))
        object.__setattr__(self, "released", MappingProxyType(_plain(dict(self.released))))
        object.__setattr__(
            self,
            "published",
            tuple(MappingProxyType(_plain(dict(item))) for item in self.published),
        )
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def public(self) -> dict:
        """**探索侧可见的内容**：只含已登记的结果载荷，不含任何其它字段。

        这是本模块对外承诺的「发布面」：把已发布内容中逐条的契约字段挑出来，
        印记字段（``binding_id`` / ``recorded_by``）不进入该视图。
        """
        return {
            "binding_id": self.binding_id,
            "results": [
                {
                    key: _plain(item.get(key))
                    for key in RESULT_PAYLOAD_FIELDS
                }
                for item in self.published
            ],
        }

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "binding_id": self.binding_id,
            "status": self.status,
            "payload": _plain(self.payload),
            "recorded": _plain(self.recorded),
            "released": _plain(self.released),
            "published": [_plain(item) for item in self.published],
            "payload_digest": self.payload_digest,
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        """规范 JSON（键排序、无多余空格），供持久化与摘要使用。"""
        return canonical_json(self.to_dict())

    def content_digest(self) -> str:
        """登记发布凭证的规范摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 验收标准①：三分类
# ---------------------------------------------------------------------------


def direction_of(effect: Any) -> str | None:
    """由效应量符号判定方向（见 :data:`DIRECTION_NOTE`）。

    等于 0 或缺失一律返回 ``None``——「方向为零」不是一个方向，
    把它读成中性结论会给「无效应」披上方向性外衣。

    :param effect: 效应量（``None`` 表示未披露）。
    :return: ``positive`` / ``negative`` / ``None``。
    :raises ReportInputError: 效应量不是有限实数且不为 ``None``。
    """
    if effect is None:
        return None
    value = _require_number(effect, "effect")
    if value > 0:
        return DIRECTION_POSITIVE
    if value < 0:
        return DIRECTION_NEGATIVE
    return None


def aggregate_status(statuses: Any) -> str:
    """把若干条结论聚合成一条结论（见 :data:`AGGREGATION_NOTE`）。

    规则（穷尽）：

    - 空输入 → ``failed``（没有任何可聚合的结论，就不是一次成功的执行）；
    - 全部为 ``failed`` → ``failed``；
    - 方向性结论只有 ``supported`` → ``supported``；
    - 方向性结论只有 ``refuted`` → ``refuted``；
    - 方向性结论同时含 ``supported`` 与 ``refuted`` → ``inconclusive``（冲突不择优）；
    - 其余（含 ``inconclusive`` 且无方向冲突）→ ``inconclusive``。

    :param statuses: 结论字符串序列。
    :return: 契约四值之一。
    :raises ReportInputError: 输入不是序列，或含非字符串项。
    :raises ClassificationError: 含契约四值以外的取值。
    """
    items = _require_sequence(statuses, "statuses")
    values: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ReportInputError(
                f"statuses[{index}] 必须是字符串，实际得到：{type(item).__name__}"
            )
        values.append(_check_status(item, f"statuses[{index}]"))
    if not values:
        return STATUS_FAILED
    if all(value == STATUS_FAILED for value in values):
        return STATUS_FAILED
    directional = {
        value for value in values if value in (STATUS_SUPPORTED, STATUS_REFUTED)
    }
    if directional == {STATUS_SUPPORTED}:
        return STATUS_SUPPORTED
    if directional == {STATUS_REFUTED}:
        return STATUS_REFUTED
    if directional:
        # 方向冲突：证据互相打架时不得择优，也不得多数表决。
        return STATUS_INCONCLUSIVE
    return STATUS_INCONCLUSIVE


def _classify_facts(
    facts: Mapping[str, Any],
    *,
    predicted_direction: str,
    test_id: str = "",
    hypothesis_id: str = "",
    metric: str = "",
    subgroup: str = "",
) -> Classification:
    """按「事实映射」形态给出单条结论（验收标准①的核心实现）。

    :raises ReportInputError: 键名未知、缺可判定字段或数值越界。
    :raises ClassificationError: 判定事实互相矛盾。
    """
    for key in facts:
        if key not in FACT_INPUT_KEYS:
            raise ReportInputError(
                f"事实映射含未知键 {key!r}：允许的键为 {sorted(FACT_INPUT_KEYS)}。"
                "未知键一律拒绝，避免调用方以为某个判定口径已生效、实际被静默忽略。"
            )
    resolved_test_id = str(facts.get("test_id", test_id) or test_id)
    resolved_hypothesis_id = str(facts.get("hypothesis_id", hypothesis_id) or hypothesis_id)
    resolved_metric = str(facts.get("metric", metric) or metric)
    resolved_subgroup = str(facts.get("subgroup", subgroup) or subgroup)

    raw_p = facts.get("p_value")
    if "available" in facts:
        available = facts["available"]
        if not isinstance(available, bool):
            raise ReportInputError(
                f"available 必须是布尔值，实际得到：{available!r}"
            )
    else:
        available = raw_p is not None

    if not available:
        if raw_p is not None:
            raise ClassificationError(
                "available=False 却给出了 p_value：二者矛盾，无法判定该条检验究竟"
                "「没算」还是「算出了结果」。"
            )
        return Classification(
            test_id=resolved_test_id,
            hypothesis_id=resolved_hypothesis_id,
            status=STATUS_FAILED,
            metric=resolved_metric,
            subgroup=resolved_subgroup,
            available=False,
            p_value=None,
            threshold=None,
            significant=None,
            effect=None,
            direction=None,
            predicted_direction=predicted_direction,
            reasons=(UNAVAILABLE_IS_FAILED_NOTE,),
        )

    if raw_p is None:
        raise ClassificationError(
            "available=True 但缺少 p_value：没有 p 值就无法判定显著性，"
            "本模块不猜测、也不插补。"
        )
    p_value = _require_number(raw_p, "p_value")
    if not 0.0 <= p_value <= 1.0:
        raise ReportInputError(f"p_value 必须落在 [0, 1] 内，实际得到：{raw_p!r}")

    if "threshold" not in facts or facts["threshold"] is None:
        raise ClassificationError(
            "available=True 但缺少 threshold：本轮该条检验的判定阈值由 P12 的"
            "检验族给出（α_t / (m - k + 1)），缺失时无法判定显著性，"
            "本模块拒绝用其它数值代替。"
        )
    threshold = _require_number(facts["threshold"], "threshold")
    if not 0.0 < threshold <= 1.0:
        raise ReportInputError(
            f"threshold 必须落在 (0, 1] 内，实际得到：{facts['threshold']!r}"
        )

    significant = p_value <= threshold
    effect = facts.get("effect")
    if effect is not None:
        effect = _require_number(effect, "effect")
    direction = direction_of(effect)

    if not significant:
        return Classification(
            test_id=resolved_test_id,
            hypothesis_id=resolved_hypothesis_id,
            status=STATUS_INCONCLUSIVE,
            metric=resolved_metric,
            subgroup=resolved_subgroup,
            available=True,
            p_value=p_value,
            threshold=threshold,
            significant=False,
            effect=effect,
            direction=direction,
            predicted_direction=predicted_direction,
            reasons=(NON_SIGNIFICANT_NOTE, INCONCLUSIVE_NOTE),
        )

    if direction is None:
        return Classification(
            test_id=resolved_test_id,
            hypothesis_id=resolved_hypothesis_id,
            status=STATUS_INCONCLUSIVE,
            metric=resolved_metric,
            subgroup=resolved_subgroup,
            available=True,
            p_value=p_value,
            threshold=threshold,
            significant=True,
            effect=effect,
            direction=None,
            predicted_direction=predicted_direction,
            reasons=(REFUTED_REQUIRES_DIRECTION_NOTE, INCONCLUSIVE_NOTE),
        )

    if direction == predicted_direction:
        return Classification(
            test_id=resolved_test_id,
            hypothesis_id=resolved_hypothesis_id,
            status=STATUS_SUPPORTED,
            metric=resolved_metric,
            subgroup=resolved_subgroup,
            available=True,
            p_value=p_value,
            threshold=threshold,
            significant=True,
            effect=effect,
            direction=direction,
            predicted_direction=predicted_direction,
            reasons=(
                "显著且效应方向与备择声明的方向一致，故归为支持。",
                NO_EVIDENCE_GRADE_NOTE,
            ),
        )

    return Classification(
        test_id=resolved_test_id,
        hypothesis_id=resolved_hypothesis_id,
        status=STATUS_REFUTED,
        metric=resolved_metric,
        subgroup=resolved_subgroup,
        available=True,
        p_value=p_value,
        threshold=threshold,
        significant=True,
        effect=effect,
        direction=direction,
        predicted_direction=predicted_direction,
        reasons=(
            "显著但效应方向与备择声明的方向相反，故归为反驳。",
            "反驳必须同时具备「显著」与「方向相反」两个条件；仅有显著性不足以反驳。",
            NO_EVIDENCE_GRADE_NOTE,
        ),
    )


def _classify_test(item: TestExecution, predicted_direction: str) -> Classification:
    """由 P12 的单条执行记录给出结论。

    :raises ClassificationError: 缺失可判定信息，或 P12 的机械判定与阈值不一致。
    """
    if not item.available:
        return Classification(
            test_id=item.test_id,
            hypothesis_id=item.hypothesis_id,
            status=STATUS_FAILED,
            metric=item.metric,
            subgroup=item.subgroup,
            available=False,
            p_value=None,
            threshold=None,
            significant=None,
            effect=None,
            direction=None,
            predicted_direction=predicted_direction,
            reasons=tuple(item.reasons) + (UNAVAILABLE_IS_FAILED_NOTE,),
        )
    if item.p_value is None or item.holm_threshold is None:
        raise ClassificationError(
            f"检验 {item.test_id!r} 标记为可得，却缺少 p 值或本轮阈值："
            "P12 的执行结果内部不一致，本模块拒绝继续分类。"
        )
    recomputed = item.p_value <= item.holm_threshold
    if item.significant is not None and bool(item.significant) != recomputed:
        raise ClassificationError(
            f"检验 {item.test_id!r} 的机械判定（{item.significant!r}）与"
            f"「p ≤ 阈值」的复算结果（{recomputed!r}）不一致："
            "判定口径被改写，本模块拒绝在其之上给出结论。"
        )
    return _classify_facts(
        {
            "available": True,
            "p_value": item.p_value,
            "threshold": item.holm_threshold,
            "effect": item.effect,
        },
        predicted_direction=predicted_direction,
        test_id=item.test_id,
        hypothesis_id=item.hypothesis_id,
        metric=item.metric,
        subgroup=item.subgroup,
    )


def _metrics_of(classifications: Sequence[Classification]) -> dict:
    """由若干结论计算概要计数（只含计数与最值，绝不含记录正文）。"""
    available = [item for item in classifications if item.available]
    p_values = [item.p_value for item in available if item.p_value is not None]
    return {
        "test_count": len(classifications),
        "available_count": len(available),
        "supported_count": sum(
            1 for item in classifications if item.status == STATUS_SUPPORTED
        ),
        "refuted_count": sum(
            1 for item in classifications if item.status == STATUS_REFUTED
        ),
        "inconclusive_count": sum(
            1 for item in classifications if item.status == STATUS_INCONCLUSIVE
        ),
        "failed_count": sum(
            1 for item in classifications if item.status == STATUS_FAILED
        ),
        "significant_count": sum(
            1 for item in classifications if item.significant is True
        ),
        "min_p_value": min(p_values) if p_values else None,
        "max_p_value": max(p_values) if p_values else None,
    }


def _build_outcome(
    hypothesis_id: str,
    items: Sequence[Classification],
    primary_metric: str | None,
) -> HypothesisOutcome:
    """把一个假说名下的逐条结论聚合成 :class:`HypothesisOutcome`。

    :raises ClassificationError: 该假说名下没有任何检验（不可能完成聚合）。
    """
    if not items:
        raise ClassificationError(
            f"假说 {hypothesis_id!r} 名下没有任何检验结论，无法形成轮次结论。"
        )
    scoped = list(items)
    scope = "all_tests"
    notes: list[str] = [AGGREGATION_NOTE]
    if primary_metric is not None:
        primary = [item for item in items if item.metric == primary_metric]
        if primary:
            scoped = primary
            scope = "primary_metric"
        else:
            notes.append(
                f"该假说未登记主要指标（{primary_metric!r}）的检验，"
                "按口径退化为使用其全部检验；scope 已标为 all_tests。"
                + PRIMARY_SCOPE_NOTE
            )
    status = aggregate_status([item.status for item in scoped])
    metrics = _metrics_of(scoped)
    metrics["primary_status"] = status
    metrics["scope"] = scope
    metrics["primary_metric"] = primary_metric
    return HypothesisOutcome(
        hypothesis_id=hypothesis_id,
        status=status,
        metrics=metrics,
        test_ids=tuple(sorted(item.test_id for item in scoped)),
        supported_test_ids=tuple(
            sorted(item.test_id for item in scoped if item.status == STATUS_SUPPORTED)
        ),
        refuted_test_ids=tuple(
            sorted(item.test_id for item in scoped if item.status == STATUS_REFUTED)
        ),
        inconclusive_test_ids=tuple(
            sorted(item.test_id for item in scoped if item.status == STATUS_INCONCLUSIVE)
        ),
        failed_test_ids=tuple(
            sorted(item.test_id for item in scoped if item.status == STATUS_FAILED)
        ),
        scope=scope,
        notes=tuple(notes),
    )


def _group_by_hypothesis(
    classifications: Sequence[Classification],
) -> list[tuple[str, list[Classification]]]:
    """按假说分组，保留首次出现顺序（使结果与输入顺序一致、可复现）。"""
    order: list[str] = []
    buckets: dict[str, list[Classification]] = {}
    for item in classifications:
        if item.hypothesis_id not in buckets:
            buckets[item.hypothesis_id] = []
            order.append(item.hypothesis_id)
        buckets[item.hypothesis_id].append(item)
    return [(key, buckets[key]) for key in order]


def classify_result(
    subject: Any,
    *,
    primary_metric: str | None = None,
    predicted_direction: str = DEFAULT_PREDICTED_DIRECTION,
    binding_id: str | None = None,
    round_index: int | None = None,
    alpha: float | None = None,
) -> Classification | RoundClassification:
    """把 P12 的检验输出归入支持 / 反驳 / 证据不足三类。

    接受三种输入形态：

    1. :class:`sdl_m06.execute.FamilyExecution`（整轮检验族）→ 返回
       :class:`RoundClassification`，含逐条结论与逐假说结论；
    2. :class:`sdl_m06.execute.TestExecution`（单条检验）→ 返回
       :class:`Classification`；
    3. **事实映射**（``{"p_value":…, "threshold":…, "effect":…, "available":…}``）
       → 返回 :class:`Classification`。允许的键见 :data:`FACT_INPUT_KEYS`。

    **注意**：本函数不接受「裸的记录列表」——真实确证的记录只能经 P12 的
    消费路径进入执行层。传入记录类关键字会被 :data:`FORBIDDEN_CALL_KEYS` 拒绝。

    :param subject: 上述三种形态之一。
    :param primary_metric: 用于聚合整轮结论的主要指标。``None`` 表示
        把全部检验视作主要指标（单指标单子群的常见情形）。
    :param predicted_direction: 备择声明的效应方向，缺省 ``positive``。
    :param binding_id: 整轮结果的绑定标识；``FamilyExecution`` 形态下缺省取其自带值。
    :param round_index: 研究轮次；``FamilyExecution`` 形态下缺省取其自带值。
    :param alpha: 本轮预算 :math:`\\alpha_t`；``FamilyExecution`` 形态下缺省取其自带值。
    :return: :class:`Classification` 或 :class:`RoundClassification`。
    :raises ReportInputError: 输入形态非法、参数越界或含受限键名。
    :raises ClassificationError: 缺失可判定信息，或判定事实互相矛盾。
    """
    _reject_forbidden_kwargs(
        {
            "primary_metric": primary_metric,
            "predicted_direction": predicted_direction,
            "binding_id": binding_id,
            "round_index": round_index,
            "alpha": alpha,
        },
        "classify_result",
    )
    direction = _validate_direction(predicted_direction)
    if primary_metric is not None:
        primary_metric = _require_text(primary_metric, "primary_metric")

    if isinstance(subject, FamilyExecution):
        return _classify_family(
            subject,
            primary_metric=primary_metric,
            predicted_direction=direction,
            binding_id=binding_id,
            round_index=round_index,
            alpha=alpha,
        )
    if isinstance(subject, TestExecution):
        return _classify_test(subject, direction)
    if isinstance(subject, Mapping):
        _check_no_token_keys(subject, "subject")
        return _classify_facts(subject, predicted_direction=direction)
    raise ReportInputError(
        "classify_result 的输入必须是 P12 的 FamilyExecution / TestExecution，"
        f"或事实映射；实际得到：{type(subject).__name__}。"
    )


def _classify_family(
    execution: FamilyExecution,
    *,
    primary_metric: str | None,
    predicted_direction: str,
    binding_id: str | None,
    round_index: int | None,
    alpha: float | None,
) -> RoundClassification:
    """把整轮执行结果分类成 :class:`RoundClassification`。

    :raises ReportInputError: 绑定标识 / 轮次 / 预算缺失或类型非法。
    """
    resolved_binding = (
        _require_text(binding_id, "binding_id")
        if binding_id is not None
        else _require_text(getattr(execution, "binding_id", None), "execution.binding_id")
    )
    if round_index is None:
        raw_index = getattr(execution, "round_index", None)
    else:
        raw_index = round_index
    if isinstance(raw_index, bool) or not isinstance(raw_index, int):
        raise ReportInputError(f"round_index 必须是整数，实际得到：{raw_index!r}")

    if alpha is None:
        raw_alpha = getattr(execution, "alpha", None)
    else:
        raw_alpha = alpha
    resolved_alpha = _require_number(raw_alpha, "alpha")
    if not 0.0 < resolved_alpha < 1.0:
        raise ReportInputError(
            f"alpha（本轮预算 α_t）必须严格落在 (0, 1) 内，实际得到：{resolved_alpha!r}"
        )

    classifications = tuple(
        _classify_test(item, predicted_direction) for item in execution.results
    )
    outcomes = tuple(
        _build_outcome(hypothesis_id, items, primary_metric)
        for hypothesis_id, items in _group_by_hypothesis(classifications)
    )
    aggregate = aggregate_status([item.status for item in outcomes])
    notes = (
        CONTRACT_STATUS_NOTE,
        NON_SIGNIFICANT_NOTE,
        UNAVAILABLE_IS_FAILED_NOTE,
        AGGREGATION_NOTE,
        PRIMARY_SCOPE_NOTE,
        NO_EVIDENCE_GRADE_NOTE,
        SEALED_BOUNDARY_NOTE,
    )
    return RoundClassification(
        binding_id=resolved_binding,
        round_index=raw_index,
        alpha=resolved_alpha,
        aggregate_status=aggregate,
        classifications=classifications,
        outcomes=outcomes,
        primary_metric=primary_metric,
        code_version=REPORT_VERSION,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 验收标准③：登记载荷与封存闸门
# ---------------------------------------------------------------------------


def _check_no_token_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝任何疑似携带令牌正文的键。

    :raises ReportInputError: 命中 :data:`SEALED_LEAK_KEYS` 中的令牌类键名。
    """
    for key in mapping:
        if isinstance(key, str) and key.lower() in (
            "token",
            "role_token",
            "token_body",
        ):
            raise ReportInputError(
                f"{label} 含受限键名 {key!r}：令牌正文不得进入本层输入、日志或提示词。"
            )


def check_no_sealed_leak(value: Any, label: str) -> None:
    """深度扫描载荷，拒绝任何封存原始记录的特征键（验收标准③的闸门）。

    递归遍历映射与序列；键名（小写后）命中 :data:`SEALED_LEAK_KEYS` 即抛错。
    刻意只扫**键名**而不扫字符串值：键名是结构性的、可穷举的边界，
    而字符串值里出现「values」之类的词可能是正常的中文说明，
    按文本猜疑会产生大量假阳性并把闸门变成噪声。

    :param value: 待检查的对象（任意嵌套结构）。
    :param label: 出错信息中的位置说明。
    :raises ReleaseError: 命中封存键名。
    """
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if isinstance(key, str) and key.lower() in SEALED_LEAK_KEYS:
                    raise ReleaseError(
                        f"{label} 含封存字段名 {key!r}："
                        "发布只含已登记的结果载荷，绝不外泄封存原始记录。"
                        + SEALED_BOUNDARY_NOTE
                    )
                stack.append(item)
        elif isinstance(current, (list, tuple, set, frozenset)):
            for item in current:
                stack.append(item)
    return None


def _check_metrics_keys(metrics: Mapping[str, Any], label: str) -> None:
    """校验 ``metrics`` 的顶层键在白名单内。

    :raises ReportInputError: 出现白名单外的顶层键。
    """
    unknown = sorted(key for key in metrics if key not in METRICS_KEYS)
    if unknown:
        raise ReportInputError(
            f"{label} 含白名单外的顶层键 {unknown}："
            f"允许的键为 {sorted(METRICS_KEYS)}。"
            "metrics 只承载计数与最值，不承载任何记录正文。"
        )


def _auto_notes(subject: RoundClassification | HypothesisOutcome) -> str:
    """生成登记的说明文本（只含状态与计数，绝不含记录正文）。"""
    if isinstance(subject, RoundClassification):
        head = (
            f"本模块按 P13 口径对绑定 {subject.binding_id} 的第 t={subject.round_index} "
            f"轮确证结果作三分类（α_t={subject.alpha!r}，"
            f"主要指标={subject.primary_metric!r}）。"
            "逐假说结论："
            + "；".join(
                f"{item.hypothesis_id} → {item.status}" for item in subject.outcomes
            )
            + f"。整轮结论：{subject.aggregate_status}。"
        )
    else:
        head = (
            f"本模块按 P13 口径对假说 {subject.hypothesis_id} 的检验结果作三分类："
            f"结论为 {subject.status}（scope={subject.scope}）。"
        )
    tail = (
        "口径要点：不显著一律归为 inconclusive（证据不足），既不得判为反驳，"
        "也不得据此认定零假说成立；未产出统计结果的检验归为 failed（没算），"
        "与 inconclusive（算出来不显著）严格区分；"
        "本记录只含结论、计数与最值，不含任何封存确证的原始记录。"
    )
    return head + tail


def build_evaluation_payload(
    subject: RoundClassification | HypothesisOutcome,
    *,
    notes: str | None = None,
    code_version: str = REPORT_VERSION,
) -> dict:
    """装配提交给 M1 ``record_evaluation`` 的登记载荷（验收标准②）。

    键集**恒等于** :data:`RESULT_PAYLOAD_FIELDS`（契约原文的四项），
    逐键显式装配，不做任何扩张；装配后自检键集与白名单一致。

    :param subject: :class:`RoundClassification` 或 :class:`HypothesisOutcome`。
    :param notes: 自定义说明文本；``None`` 时按口径自动生成。
        自定义文本会被追加到自动生成的口径说明之前，而不是替换它——
        核心口径（不显著归证据不足等）不允许被调用方删除。
    :param code_version: 登记口径版本，必须是非空字符串。
    :return: ``{"status", "metrics", "notes", "code_version"}``。
    :raises ReportInputError: 输入类型非法、键集不符或 metrics 越界。
    """
    if not isinstance(subject, (RoundClassification, HypothesisOutcome)):
        raise ReportInputError(
            "登记对象必须是 RoundClassification（整轮）或 HypothesisOutcome"
            f"（单假说）；实际得到：{type(subject).__name__}。"
            "单条检验的 Classification 不足以构成一次确证的登记对象。"
        )
    version = _require_text(code_version, "code_version")

    if isinstance(subject, RoundClassification):
        metrics = _metrics_of(subject.classifications)
        metrics["primary_status"] = subject.aggregate_status
        metrics["primary_metric"] = subject.primary_metric
        metrics["scope"] = (
            "primary_metric" if subject.primary_metric is not None else "all_tests"
        )
        metrics["round_index"] = subject.round_index
        metrics["alpha"] = subject.alpha
        metrics["hypotheses"] = {
            item.hypothesis_id: item.status for item in subject.outcomes
        }
    else:
        metrics = dict(subject.metrics)

    _check_metrics_keys(metrics, "metrics")
    auto = _auto_notes(subject)
    if notes is None:
        text = auto
    else:
        text = _require_text(notes, "notes") + " | " + auto

    payload = {
        "status": subject.status if isinstance(subject, HypothesisOutcome) else subject.aggregate_status,
        "metrics": _plain(metrics),
        "notes": text,
        "code_version": version,
    }
    if set(payload) != set(RESULT_PAYLOAD_FIELDS):
        raise ReportInputError(
            f"登记载荷的键集 {sorted(payload)} 与契约 {list(RESULT_PAYLOAD_FIELDS)} "
            "不一致：载荷字段必须与契约逐项对应。"
        )
    _check_status(payload["status"], "payload.status")
    if not isinstance(payload["metrics"], dict):
        raise ReportInputError("payload.metrics 必须是普通字典（M1 要求是对象）。")
    if not isinstance(payload["notes"], str):
        raise ReportInputError("payload.notes 必须是字符串（M1 要求是字符串）。")
    check_no_sealed_leak(payload, "待登记载荷")
    return payload


def is_result_payload(value: Any) -> bool:
    """判断一个对象是否为合法的**结果载荷**（契约四字段 + 允许的印记字段）。

    这是「发布内容里只应有结果」这一承诺的可机检判据。

    :param value: 待判断对象。
    :return: 是否合法。
    """
    if not isinstance(value, Mapping):
        return False
    keys = {key for key in value}
    if not set(RESULT_PAYLOAD_FIELDS) <= keys:
        return False
    if keys - set(RESULT_PAYLOAD_FIELDS) - set(RELEASE_STAMP_FIELDS):
        return False
    status = value.get("status")
    if not isinstance(status, str) or status not in RESULT_STATUSES:
        return False
    if not isinstance(value.get("metrics"), Mapping):
        return False
    if not isinstance(value.get("notes"), str):
        return False
    version = value.get("code_version")
    return isinstance(version, str) and bool(version.strip())


def _contract_digest(payload: Mapping[str, Any]) -> str:
    """对载荷的**契约四字段**取规范摘要（忽略证据库加盖的印记字段）。"""
    subset = {key: _plain(payload[key]) for key in RESULT_PAYLOAD_FIELDS if key in payload}
    return hashlib.sha256(canonical_json(subset).encode("utf-8")).hexdigest()


def _published_evaluations(released: Any, binding_id: str) -> tuple[dict, ...]:
    """从 ``release_results`` 的返回值中取出已发布的结果条数。

    :raises ReleaseError: 返回形态非法（不是映射、缺 ``evaluations`` 列表）。
    """
    body = _require_mapping(released, "release_results 的返回值")
    evaluations = body.get("evaluations")
    if not isinstance(evaluations, list):
        raise ReleaseError(
            "release_results 的返回值缺少 evaluations 列表："
            f"实际得到 {type(evaluations).__name__}。发布内容必须可逐条核对。"
        )
    published: list[dict] = []
    for index, item in enumerate(evaluations):
        if not isinstance(item, Mapping):
            raise ReleaseError(
                f"已发布内容第 {index} 条不是结果载荷（{type(item).__name__}）："
                + RELEASE_SCOPE_NOTE
            )
        if not is_result_payload(item):
            raise ReleaseError(
                f"已发布内容第 {index} 条不是合法的结果载荷："
                "发布只含已登记结果，出现其它形态说明发布越界。"
                + SEALED_BOUNDARY_NOTE
            )
        stamped = item.get("binding_id")
        if str(stamped or "") != binding_id:
            raise ReleaseError(
                f"已发布内容第 {index} 条归属绑定 {stamped!r}，"
                f"与本次绑定的 {binding_id!r} 不一致：发布不得混入他轮结果。"
            )
        published.append(_plain(dict(item)))
    return tuple(published)


def _check_only_registered(
    published: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
) -> None:
    """核对本次登记的结果原样出现在发布内容中（验收标准③的对账）。

    发布内容可以包含同一绑定上**先前登记过的失败尝试**（M1 会保留它们），
    但**必须**包含本次登记的那一条，且每一条都必须已归属该绑定。

    :raises ReleaseError: 本次登记的结果未出现在发布内容中。
    """
    target = _contract_digest(payload)
    for item in published:
        if _contract_digest(item) == target:
            return None
    raise ReleaseError(
        "发布内容中找不到本次已登记的结果：登记与发布之间可能被替换。"
        "发布必须原样携带已登记结果。" + RELEASE_SCOPE_NOTE
    )


# ---------------------------------------------------------------------------
# 主入口：登记与发布（验收标准②③）
# ---------------------------------------------------------------------------


def _client_capabilities(module_confirmer: Any) -> tuple[Any, Any]:
    """探测注入的 confirmer 模块是否具备登记与发布能力。

    本模块**只**使用这两条接口；模块内不存在任何读取确证内容的入口。

    :raises ReportInputError: 缺少 ``record_evaluation`` 或 ``release_results``。
    """
    record = getattr(module_confirmer, "record_evaluation", None)
    release = getattr(module_confirmer, "release_results", None)
    missing = [
        name
        for name, func in (("record_evaluation", record), ("release_results", release))
        if not callable(func)
    ]
    if missing:
        raise ReportInputError(
            f"注入的 confirmer 模块缺少接口 {missing}"
            f"（实际类型 {type(module_confirmer).__name__}）："
            "登记与发布必须经 M1 的公开接口完成。"
        )
    return record, release


def record_and_release(
    module_confirmer: Any,
    binding: Any,
    subject: RoundClassification | HypothesisOutcome,
    *,
    notes: str | None = None,
    code_version: str = REPORT_VERSION,
) -> RecordReleaseReceipt:
    """把轮次结论登记进证据库，随后发布给探索侧（验收标准②③）。

    执行顺序（顺序本身可核对）：

    1. **装配载荷**：:func:`build_evaluation_payload` 逐键装配契约四字段；
    2. **出闸扫描**：:func:`check_no_sealed_leak` 深度扫描待登记载荷；
    3. **登记**：调用 ``record_evaluation(binding_id, payload)``。
       M1 要求绑定对应的数据集已处于 ``used`` 状态（即 P12 已消费），
       并保留失败尝试、拒绝覆盖既有最终结论；
    4. **再扫描**：对登记返回值与发布返回值**再次**深度扫描（防止证据库
       或调用方在中间环节掺入记录正文）；
    5. **发布**：调用 ``release_results(binding_id)``；
    6. **对账**：逐条核对发布内容都是合法结果载荷、都归属同一绑定，
       且本次登记的结果原样出现（:func:`_check_only_registered`）。

    本函数**不读取**任何确证记录，也**不计算**任何统计量；它只做登记与发布。

    :param module_confirmer: 以 confirmer 角色构造的 M1 模块（鸭子类型即可）。
    :param binding: 含 ``binding_id`` 的绑定描述（``bind`` 的返回值）。
    :param subject: :class:`RoundClassification` 或 :class:`HypothesisOutcome`。
    :param notes: 自定义说明文本（可选）；核心口径说明始终会追加，不可删除。
    :param code_version: 登记口径版本。
    :return: :class:`RecordReleaseReceipt`。
    :raises ReportInputError: 输入形态非法、含受限键名或参数越界。
    :raises ReleaseError: 载荷含封存字段，或发布内容与已登记结果对不上。
    """
    _reject_forbidden_kwargs(
        {
            "binding": binding,
            "subject": subject,
            "notes": notes,
            "code_version": code_version,
        },
        "record_and_release",
    )
    if not isinstance(subject, (RoundClassification, HypothesisOutcome)):
        raise ReportInputError(
            "只能登记整族轮次结果（RoundClassification）或单假说结论"
            f"（HypothesisOutcome）；实际得到：{type(subject).__name__}。"
            "单条检验的 Classification 不足以构成一次确证的登记对象。"
        )
    body = _require_mapping(binding, "binding")
    _check_no_token_keys(body, "binding")
    # 绑定描述同样要过封存闸门：调用方可能把原始记录顺手挂在 binding 上一起传进来，
    # 若只扫载荷会漏掉这条路径。
    check_no_sealed_leak(body, "binding")
    binding_id = _require_text(body.get("binding_id"), "binding.binding_id")

    payload = build_evaluation_payload(subject, notes=notes, code_version=code_version)
    record_fn, release_fn = _client_capabilities(module_confirmer)

    recorded = record_fn(binding_id, payload)
    recorded = _require_mapping(recorded, "record_evaluation 的返回值")
    check_no_sealed_leak(recorded, "已登记结果")

    released = release_fn(binding_id)
    check_no_sealed_leak(released, "已发布结果")
    published = _published_evaluations(released, binding_id)
    _check_only_registered(published, payload)

    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    notes_out = (
        CONTRACT_STATUS_NOTE,
        SEALED_BOUNDARY_NOTE,
        RELEASE_SCOPE_NOTE,
        NO_EVIDENCE_GRADE_NOTE,
        "登记与发布之间做了两次封存键名扫描与一次发布内容对账；"
        "任一环节发现封存字段或归属不符都会中止，不会把可疑载荷交出去。",
    )
    return RecordReleaseReceipt(
        binding_id=binding_id,
        status=payload["status"],
        payload=payload,
        recorded=_plain(dict(recorded)),
        released=_plain(dict(released)),
        published=published,
        payload_digest=digest,
        code_version=payload["code_version"],
        notes=notes_out,
    )


# ---------------------------------------------------------------------------
# 边界守护：本模块不提供的数据访问与摆放位置
# ---------------------------------------------------------------------------


def _forbidden_access_names() -> tuple[str, ...]:
    """本模块**不应存在**的数据访问入口名。

    与 :data:`NOT_PROVIDED_BY_P13` 中列出的越界接口不同，本集合聚焦「读取」：
    本模块只消费 P12 的结果对象，没有任何读取封存记录的路径。
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
    )


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查两项：

    1. 模块是否定义了 :data:`NOT_PROVIDED_BY_P13` 中的任何名字（越界实现）；
    2. 模块是否出现 :func:`_forbidden_access_names` 中的属性访问或导入（越界读取）。

    刻意用 AST 而不是源码文本匹配：本模块的 docstring 与常量里**必须**写出
    ``classify_result`` / ``read_dataset`` 等被禁名字（那是边界声明），
    文本匹配会把边界声明本身判成违规。

    :return: ``{"defined_forbidden", "accessed_forbidden", "ok", "checked_names"}``。
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
    defined_forbidden = tuple(sorted(defined & set(NOT_PROVIDED_BY_P13)))
    accessed_forbidden = tuple(
        sorted(accessed & (set(_forbidden_access_names()) - set(NOT_PROVIDED_BY_P13)))
    )
    return {
        "defined_forbidden": defined_forbidden,
        "accessed_forbidden": accessed_forbidden,
        "ok": not defined_forbidden and not accessed_forbidden,
        "checked_names": len(NOT_PROVIDED_BY_P13),
    }
