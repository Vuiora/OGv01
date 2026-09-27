"""P11 交付物：冻结计划生成与确证绑定（模块 M6）。

本模块是**确证阶段的入口**：它把 P10 选出的探索候选转成 M1 可以接受的
**冻结计划**（frozen plan），并调用 M1 的 ``bind_confirmation`` 完成绑定。
理论依据见 `DS协作指南-阶段划分与提示词.md <../DS协作指南-阶段划分与提示词.md>`_
§3 P11 与 §4「确证门槛」，字段定义见
`IMPLEMENTATION_CONTRACT.md <../IMPLEMENTATION_CONTRACT.md>`_ 的 *Frozen plan*，
接口约定见 `INTERFACES.md <../INTERFACES.md>`_ §2.5。

三条验收标准的实现方式
-----------------------

① **计划字段与契约逐项对应，无多余无缺漏。**
   字段清单冻结为常量 :data:`FROZEN_PLAN_FIELDS`（13 项）、
   :data:`HYPOTHESIS_ENTRY_FIELDS`（8 项）、:data:`TEST_ENTRY_FIELDS`（5 项）、
   :data:`PREPROCESSING_FIELDS`（2 项）。:func:`build_frozen_plan` 逐键显式装配，
   不做「顺手带上下游字段」的扩张；装配完成后再做一次
   :func:`validate_frozen_plan` 自检，字段集与契约不一致即报错。

② **``fit_dataset_refs`` 只指向 E。**
   缺省值取公开协议里的探索分区引用（``resources["E"]``）；若调用方显式给出，
   逐一比对，凡不属于该协议探索分区的引用一律拒绝（:class:`PlanFieldError`）。
   注意这里的口径比 M1 的校验更严：M1 允许 ``H_`` 历史视图参与拟合，
   但本阶段契约要求「只指向 E」，故历史视图也被拒绝。

③ **检验族完整登记。**
   检验族按「假说 × 指标 × 子群」的**笛卡尔积**机械展开，因此完备性是
   **构造性的**，而不是靠人工核对：每个假说至少登记一条检验，
   每个已声明指标与已声明子群都被登记。
   :func:`family_coverage` 从登记结果反解出实际覆盖的
   ``(假说, 指标, 子群)`` 三元组，供下游逐条比对；
   :func:`check_family_completeness` 用它与声明集合对账。

④ **绑定前不读 C 分区内容。**
   :func:`bind` 只做一件事——把计划交给注入的 confirmer 模块调用
   ``bind_confirmation``。它**不**调用 ``read_dataset`` / ``consume_confirmation``，
   也不导入 ``sdl_m01``；对「确证数据在哪、有多少条、内容如何」没有任何访问路径。
   绑定结果里的 ``plan_digest`` 会被 :func:`frozen_plan_digest` 复算比对，
   用于确认「冻结内容没有被替换」（这一步只读绑定元数据，不读 C 内容）。

本模块的定位与边界
------------------

- **本轮不调用 ``consume_confirmation``。** 消费属 P12，且必须与 ``used``
  状态提交原子化；在 P11 泄漏一次消费会让该轮确证数据提前失效。
  该接口名列入 :data:`NOT_PROVIDED_BY_P11`。
- 不校验统计有效性：本模块只保证「计划被完整登记」，
  代表性、组间独立性、条件 p 值有效性等前提属外部义务（见协作指南 §5.5），
  须在 ``identification_gaps`` 中显式登记并由最终输出披露
  （:data:`STATISTICAL_VALIDITY_NOTE`）。
- 不计算 :math:`\alpha` 与任何 p 值（属 P12），不做结果分类与发布（属 P13），
  不做主动取证（属 P14），不归档（属 P15）。
- 不修改 ``sdl_m01/``，不读取/打印/写入角色令牌、evidence 数据库内容或受限质量报告，
  不引入第三方依赖。

术语约定（全文一致）
--------------------

- **候选（candidate）**：P10 选出的待确证对象；本模块要求它**携带假说对象**
  （或由调用方经 ``hypotheses=`` 显式提供），因为它要冻结的是假说的
  ``representation`` / ``model`` / ``parameters``，而非仅评估分数。
- **声明（declaration）**：调用方按假说 id 给出的确证口径，含指标、子群、
  零假说、备择、方法、预测、效应阈值。声明缺失时按假说自身字段反推，
  **不凭空编造统计前提**。
- **指标（metric）**：参与声明的主要度量名称；契约要求全部登记进检验族。
- **子群（subgroup）**：参与声明的分析子集；「整体」用 :data:`DEFAULT_SUBGROUP` 表示。
- **效应阈值（effect threshold）**：预定的最小有意义差异；**必须显式声明**，
  不提供默认值——「预定」若可被默认值填上，就不再是预定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    # 异常
    "FreezeError",
    "FreezeInputError",
    "PlanFieldError",
    "PlanCompletenessError",
    "PlanBindingError",
    # 常量：版本与字段清单
    "FREEZE_VERSION",
    "FROZEN_PLAN_FIELDS",
    "HYPOTHESIS_ENTRY_FIELDS",
    "TEST_ENTRY_FIELDS",
    "PREPROCESSING_FIELDS",
    "ROUND_INDEX_MIN",
    "ROUND_INDEX_MAX",
    # 常量：口径与声明
    "DEFAULT_SUBGROUP",
    "TEST_ID_DELIMITER",
    "DEFAULT_METHOD_TEXT",
    "NULL_TEMPLATE",
    "ALTERNATIVE_TEMPLATE",
    "FIT_REF_POLICY_NOTE",
    "FAMILY_COMPLETENESS_NOTE",
    "DEFAULT_JUSTIFICATION_TEXT",
    "STATISTICAL_VALIDITY_NOTE",
    "BINDING_SEQUENCE_NOTE",
    "DECLARATION_KEYS",
    "CARRIER_METADATA_KEYS",
    "FORBIDDEN_TOKEN_KEYS",
    "FORBIDDEN_CALL_KEYS",
    "NOT_PROVIDED_BY_P11",
    # 入口
    "build_frozen_plan",
    "bind",
    # 校验与对账
    "validate_frozen_plan",
    "family_coverage",
    "check_family_completeness",
    "frozen_plan_digest",
    "canonical_json",
    # 结果对象
    "HypothesisPlanView",
    "PlanDeclaration",
]


# ---------------------------------------------------------------------------
# 常量：版本与字段清单（验收标准①）
# ---------------------------------------------------------------------------

#: 交付物版本号，写入绑定来源说明，便于追溯冻结口径的变更。
FREEZE_VERSION = "P11-v1.0"

#: **冻结计划的顶层字段清单，顺序与 :file:`IMPLEMENTATION_CONTRACT.md` 一致。**
#:
#: 契约原文（*Frozen plan*）的字段按此顺序列出；本模块逐键装配，
#: 并在装配后断言 ``set(plan) == set(FROZEN_PLAN_FIELDS)``，
#: 使「无多余无缺漏」成为可机检的事实。
FROZEN_PLAN_FIELDS: tuple[str, ...] = (
    "protocol_id",
    "round_index",
    "hypotheses",
    "preprocessing",
    "primary_metric",
    "test_family",
    "effect_threshold",
    "sampling_plan",
    "stopping_rule",
    "inference_unit",
    "eligibility",
    "quality_rules_version",
    "assumptions",
)

#: 计划内每个假说条目的字段清单（契约的 ``hypotheses`` 元素形状）。
HYPOTHESIS_ENTRY_FIELDS: tuple[str, ...] = (
    "id",
    "version",
    "statement",
    "prediction",
    "scope",
    "representation",
    "model",
    "parameters",
)

#: 计划内每条检验的字段清单（契约的 ``test_family`` 元素形状）。
TEST_ENTRY_FIELDS: tuple[str, ...] = (
    "id",
    "hypothesis_id",
    "null",
    "alternative",
    "method",
)

#: ``preprocessing`` 的字段清单。
PREPROCESSING_FIELDS: tuple[str, ...] = ("steps", "fit_dataset_refs")

#: 轮次取值范围，与 M1 的 ``_validate_plan`` 保持一致（二者必须同步）。
ROUND_INDEX_MIN = 1
ROUND_INDEX_MAX = 1022


# ---------------------------------------------------------------------------
# 常量：口径与声明
# ---------------------------------------------------------------------------

#: 「整体（不分层）」子群的标识。若调用方声明了真正的子群，
#: 就应当同时声明整体口径，避免「只报子群、不报整体」的选择性报告。
DEFAULT_SUBGROUP = "overall"

#: 检验族标识的分隔符。检验条目本身只有契约规定的 5 个字段，
#: 因此「指标」与「子群」的登记信息**编码在 ``id`` 里**以便反解对账。
#: 该分隔符不得出现在假说 id、指标名或子群名中（构造时校验）。
TEST_ID_DELIMITER = "::"

#: 缺省检验方法描述。方法必须**预先前置声明**，不允许在检验之后补写；
#: 本阶段只登记，不执行（执行属 P12）。
DEFAULT_METHOD_TEXT = (
    "外部预先声明的批次级检验：按冻结的 inference_unit 聚合后，"
    "在声明的子群内检验效应是否超过预定效应阈值；"
    "本轮只登记方法与对照设计，不执行任何检验（执行与误差预算属 P12）。"
)

#: 零假说模板（当假说自身未携带零假说时使用该模板反推）。
NULL_TEMPLATE = (
    "在指标 {metric} 上、子群 {subgroup} 内，不存在超过预定效应阈值 "
    "{delta} 的效应。"
)

#: 备择（方向性）模板。检验须针对**最小有意义差异**构造，
#: 不得退化为「是否大于零」（见协作指南 §3 P12 注）。
ALTERNATIVE_TEMPLATE = (
    "在指标 {metric} 上、子群 {subgroup} 内，效应超过预定效应阈值 "
    "{delta}（针对最小有意义差异构造，而非「大于零」）。"
)

#: 拟合引用口径说明（验收标准②）。
FIT_REF_POLICY_NOTE = (
    "preprocessing.fit_dataset_refs 只允许指向公开协议登记的**探索分区 E**；"
    "开发分区 V 与确证分区 C1/C2… 一律拒绝，历史视图 H_ 亦不在本阶段许可范围内"
    "（本阶段口径严于 M1：M1 允许 H_ 参与拟合，本模块按契约只认 E）。"
)

#: 检验族完备性口径说明（验收标准③）。
FAMILY_COMPLETENESS_NOTE = (
    "检验族按「假说 × 指标 × 子群」的笛卡尔积展开，完备性由构造保证："
    "每个假说至少一条检验，且每个已声明指标与已声明子群都被登记。"
    "登记结果可由 family_coverage 反解为 (假说, 指标, 子群) 三元组逐条对账；"
    "冻结之后不得增删检验——事后增删会使确证退化为可选报告。"
)

#: 假设论证缺省文本。它只说明「沿用协议登记」，不声称已被验证。
DEFAULT_JUSTIFICATION_TEXT = (
    "沿用数据协议已登记的依赖假设；本阶段不新增、不修改任何统计前提。"
    "该假设的有效性属外部义务，须在 identification_gaps 中显式登记并在最终输出披露。"
)

#: 统计有效性的边界声明（协作指南 §5.5）。
STATISTICAL_VALIDITY_NOTE = (
    "P11 只保证「计划被完整登记」，不能保证代表性、组间独立性、"
    "条件 p 值有效性或采样方案适用性；这些前提属外部义务，"
    "需在 identification_gaps 中显式登记并由最终输出披露。"
    "登记本身不构成任何统计结论，也不授予证据等级。"
)

#: 绑定顺序说明：先绑定、后消费，且消费属 P12。
BINDING_SEQUENCE_NOTE = (
    "正确顺序是「先 bind_confirmation 冻结计划 → 再由 P12 原子化 consume」。"
    "本模块只做前半步：它没有任何读取确证分区内容的路径，"
    "也不调用 consume_confirmation（消费必须与 used 状态提交原子化，属 P12）。"
)

#: 声明映射（按假说 id）允许的键。未知键一律拒绝，避免调用方以为
#: 某个口径已经生效、实际却被静默忽略。
DECLARATION_KEYS: frozenset[str] = frozenset(
    {
        "hypothesis_id",
        "prediction",
        "primary_metric",
        "metrics",
        "subgroups",
        "effect_threshold",
        "null",
        "alternative",
        "method",
        "assumptions",
    }
)

#: **载体元数据键**：当候选条目是「P10 评估记录 + 假说对象」的连接形态时，
#: 条目里会夹带评估维度与筛选痕迹。这些键既不是声明、也不该触发
#: 「未知键」报错——它们是上游记录的固有字段。
#:
#: 之所以显式列出而不是「凡不认识就放过」：那样会把拼错的声明键
#: （例如 ``effect_thresold``）静默吞掉，正是 :data:`DECLARATION_KEYS`
#: 白名单要防的事。只有本集合与白名单之外的名字才报错。
CARRIER_METADATA_KEYS: frozenset[str] = frozenset(
    {
        # P09 的四维指标与版本绑定（见 INTERFACES.md §2.4）
        "gain",
        "stability",
        "novelty",
        "complexity",
        "knowledge_version",
        # 候选标识类
        "id",
        "version",
        "hypothesis_version",
        # P10 筛选痕迹（见 sdl_m05.select）
        "rank",
        "rotation_dimension",
        "rationale",
        "complete",
        "incomplete_dimensions",
        "evaluation",
        # 通用来源信息
        "provenance",
        "source",
        "source_ref",
        "code_version",
        "pattern_ids",
    }
)

#: 不得出现在任何输入里的键名（小写比较）。依据 ``INTERFACES.md`` §4.4：
#: 令牌正文不得写入日志、提示词或版本库。
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

#: 明确拒绝的关键字参数。两类：
#:
#: 1. **消费与执行类**（``consume`` / ``alpha`` / p 值）：属 P12，本阶段调用即越界；
#: 2. **令牌类**：不得进入本层输入。
FORBIDDEN_CALL_KEYS: frozenset[str] = frozenset(
    {
        "consume",
        "consume_confirmation",
        "replay",
        "alpha",
        "p_value",
        "holm",
        "status",
        "result",
        "read_dataset",
        "quality",
    }
)

#: 本模块**不提供**的接口名。测试据此断言「P11 未越界实现其它阶段的内容」。
#: 单列成常量而非散落在测试里，是为了让「本层不做什么」也成为可机检的声明。
NOT_PROVIDED_BY_P11: tuple[str, ...] = (
    # P12 的检验执行与消费（消费必须原子化，属 P12）
    "consume_confirmation",
    "execute_family",
    "holm_adjust",
    "alpha_for_round",
    # P13 的结果记录与发布
    "classify_result",
    "record_and_release",
    "record_evaluation",
    "release_results",
    # P14 的主动取证
    "divergence",
    "acquisition_plan",
    # P15 的归档
    "archive_round",
    "update_knowledge_version",
    # P09/P10 的度量与筛选（本模块只消费，不重算）
    "evaluate_hypothesis",
    "pareto_front",
    "select_freeze_candidates",
    # 任何数据读取入口（本模块没有任何读 C 内容的路径）
    "read_dataset",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class FreezeError(ValueError):
    """本模块所有异常的基类，继承自 :class:`ValueError`。"""


class FreezeInputError(FreezeError):
    """输入形态非法（缺假说、缺协议、类型不对、键名受限等）。"""


class PlanFieldError(FreezeError):
    """计划字段与契约不符（多余、缺漏、类型错、引用越界）。"""


class PlanCompletenessError(FreezeError):
    """检验族登记不完整，或声明的口径无法满足（缺效应阈值等）。

    单列一个子类，是因为验收标准③把「检验族完整登记」作为独立一条：
    调用方与测试需要能把它与一般的字段错误区分开。
    """


class PlanBindingError(FreezeError):
    """绑定过程失败（注入模块缺接口、计划未通过自检、摘要不一致）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _thaw(value: Any) -> Any:
    """把冻结结构（``MappingProxyType`` / 元组）还原为 JSON 兼容的可变结构。

    P07 的假说对象内部用 ``MappingProxyType`` 深度冻结，直接交给
    ``json.dumps`` 会失败；这里统一还原为 ``dict`` / ``list``。
    """
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _jsonable(value: Any) -> Any:
    """把任意嵌套结构转换为 JSON 兼容形态（与 :func:`_thaw` 同义，保留旧名）。"""
    return _thaw(value)


def canonical_json(value: Any) -> str:
    """规范式 JSON 文本，口径与 M1 的 ``store.canonical`` **完全一致**。

    三种口径必须逐项相同，否则 :func:`frozen_plan_digest` 无法与 M1 记录的
    ``plan_digest`` 对账：``sort_keys=True``、``separators=(",", ":")``、
    ``ensure_ascii=False``、``allow_nan=False``。
    """
    try:
        return json.dumps(
            _thaw(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PlanFieldError(
            "计划必须只含有限、JSON 兼容的取值（allow_nan=False 口径）。"
        ) from exc


def frozen_plan_digest(plan: Any) -> str:
    """冻结计划的 SHA-256 摘要，口径与 M1 的 ``digest(canonical(plan))`` 一致。

    用于把本地生成的计划与绑定记录里的 ``plan_digest`` 对账——
    「冻结内容没有被替换」这件事因此可机械复核。
    """
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


def _require_nonempty_str(value: Any, label: str) -> str:
    """要求非空字符串（去空白后非空）。"""
    if not isinstance(value, str) or not value.strip():
        raise FreezeInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """要求映射类型。"""
    if not isinstance(value, Mapping):
        raise FreezeInputError(
            f"{label} 必须是映射，实际得到：{type(value).__name__}"
        )
    return value


def _is_finite_number(value: Any) -> bool:
    """是否为有限实数（``bool`` 不算，它是 ``int`` 的子类但语义不同）。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _as_text(value: Any, label: str) -> str:
    """把字符串或映射转成一段非空文本（映射按键排序渲染）。"""
    if isinstance(value, str):
        return _require_nonempty_str(value, label)
    if isinstance(value, Mapping):
        if not value:
            return "未限定"
        parts: list[str] = []
        for key in sorted(value, key=str):
            parts.append(f"{key}={_render_scalar(value[key])}")
        text = "; ".join(parts)
        return text if text.strip() else "未限定"
    if isinstance(value, (list, tuple)):
        if not value:
            return "未限定"
        return "; ".join(_render_scalar(item) for item in value)
    raise FreezeInputError(
        f"{label} 必须是字符串、映射或序列，实际得到：{type(value).__name__}"
    )


def _render_scalar(value: Any) -> str:
    """把标量渲染为短文本；嵌套结构退化为规范式 JSON。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        return "null"
    try:
        return canonical_json(value)
    except FreezeError:  # pragma: no cover - 防御性兜底
        return repr(value)


def _require_sequence(value: Any, label: str) -> list[Any]:
    """要求序列（字符串与映射不作为序列接受）。"""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise FreezeInputError(
            f"{label} 必须是可迭代序列，实际得到：{type(value).__name__}"
        )
    return list(value)


def _check_no_forbidden_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝输入里出现令牌类键名。"""
    for key in mapping:
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_TOKEN_KEYS:
            raise FreezeInputError(
                f"{label} 出现受限键名 {key!r}：令牌正文不得进入本层输入、"
                "日志或版本库（INTERFACES.md §4.4）。"
            )


def _reject_forbidden_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝越界或受限的关键字参数，并兜住任何未预期参数。"""
    for key in sorted(kwargs):
        lowered = key.strip().lower()
        if lowered in FORBIDDEN_CALL_KEYS or lowered in FORBIDDEN_TOKEN_KEYS:
            raise FreezeError(
                f"{where} 不接受参数 {key!r}：确证消费与误差预算属 P12，"
                "本阶段只生成并绑定冻结计划；令牌正文亦不得进入本层输入。"
            )
    if kwargs:  # pragma: no cover - 防御性兜底
        raise FreezeInputError(
            f"{where} 收到未预期的参数：" + ", ".join(repr(key) for key in sorted(kwargs))
        )


def _slug_check(value: str, label: str) -> str:
    """校验标识中不含检验族分隔符，保证 ``id`` 可被确定性反解。"""
    if TEST_ID_DELIMITER in value:
        raise FreezeInputError(
            f"{label} 不能包含分隔符 {TEST_ID_DELIMITER!r}："
            "检验族标识用该分隔符编码「假说 / 指标 / 子群」以便对账。"
        )
    return value


def _test_id(hypothesis_id: str, metric: str, subgroup: str) -> str:
    """按固定格式生成检验标识：``t::<假说>::<指标>::<子群>``。"""
    return TEST_ID_DELIMITER.join(("t", hypothesis_id, metric, subgroup))


def _parse_test_id(test_id: str) -> tuple[str, str, str]:
    """由检验标识反解 ``(假说, 指标, 子群)``。

    :raises PlanFieldError: 标识格式不符（说明检验族被外部改写）。
    """
    parts = str(test_id).split(TEST_ID_DELIMITER)
    if len(parts) != 4 or parts[0] != "t":
        raise PlanFieldError(
            f"检验标识 {test_id!r} 不符合 "
            f"'t{TEST_ID_DELIMITER}<假说>{TEST_ID_DELIMITER}<指标>"
            f"{TEST_ID_DELIMITER}<子群>' 格式：检验族可能被外部改写。"
        )
    return parts[1], parts[2], parts[3]


# ---------------------------------------------------------------------------
# 协议视图（只读公开协议，不触碰任何确证分区内容）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProtocolView:
    """从公开协议（``build()`` / ``describe()`` 的产物）提取的只读视图。

    只读的是**协议元信息**：任务口径、依赖口径、质量规则版本、确证政策与
    分区引用。本视图不包含任何记录正文，也不触达确证分区的内容。

    :param protocol_id: 协议标识。
    :param exploration_refs: 探索分区 E 的引用（可有多条）。
    :param sampling_plan: 采样方案（M1 要求与协议逐字一致）。
    :param stopping_rule: 停止规则（M1 要求与协议逐字一致）。
    :param inference_unit: 推断单位（M1 要求与协议一致）。
    :param eligibility: 目标资格口径（M1 要求与协议一致）。
    :param quality_rules_version: 质量规则版本（M1 要求与协议一致）。
    :param declared_assumptions: 协议登记的依赖假设名列表。
    """

    protocol_id: str
    exploration_refs: tuple[str, ...]
    sampling_plan: str
    stopping_rule: str
    inference_unit: str
    eligibility: str
    quality_rules_version: str
    declared_assumptions: tuple[str, ...]


def _protocol_view(protocol: Any) -> _ProtocolView:
    """把公开协议规范化为 :class:`_ProtocolView`，并校验必填口径齐全。

    :raises FreezeInputError: 协议不是映射、缺关键段，或缺探索分区引用。
    """
    proto = _require_mapping(protocol, "protocol")
    _check_no_forbidden_keys(proto, "protocol")

    protocol_id = _require_nonempty_str(proto.get("protocol_id"), "protocol.protocol_id")

    resources = _require_mapping(proto.get("resources"), "protocol.resources")
    exploration: list[str] = []
    # resources 的形态是 ``{purpose: ref}``（M1 排除了 Q 分区）。
    for purpose, ref in resources.items():
        if str(purpose) == "E":
            if isinstance(ref, str) and ref.strip():
                exploration.append(ref)
            else:
                raise FreezeInputError(
                    f"protocol.resources['E'] 必须是非空引用字符串，实际得到：{ref!r}"
                )
    if not exploration:
        raise FreezeInputError(
            "protocol.resources 缺少探索分区 E 的引用："
            "无法确定 fit_dataset_refs（" + FIT_REF_POLICY_NOTE + "）"
        )

    confirmation = _require_mapping(
        proto.get("confirmation_policy"), "protocol.confirmation_policy"
    )
    dependence = _require_mapping(proto.get("dependence"), "protocol.dependence")
    task = _require_mapping(proto.get("task"), "protocol.task")

    declared_assumptions: list[str] = []
    raw_assumptions = dependence.get("assumptions")
    if raw_assumptions is not None:
        for index, item in enumerate(_require_sequence(raw_assumptions, "dependence.assumptions")):
            declared_assumptions.append(
                _require_nonempty_str(item, f"dependence.assumptions[{index}]")
            )

    return _ProtocolView(
        protocol_id=protocol_id,
        exploration_refs=tuple(exploration),
        sampling_plan=_require_nonempty_str(
            confirmation.get("sampling_plan"), "confirmation.sampling_plan"
        ),
        stopping_rule=_require_nonempty_str(
            confirmation.get("stopping_rule"), "confirmation.stopping_rule"
        ),
        inference_unit=_require_nonempty_str(
            dependence.get("inference_unit"), "dependence.inference_unit"
        ),
        eligibility=_require_nonempty_str(task.get("eligibility"), "task.eligibility"),
        quality_rules_version=_require_nonempty_str(
            proto.get("quality_rules_version"), "protocol.quality_rules_version"
        ),
        declared_assumptions=tuple(declared_assumptions),
    )


def _validate_round_index(round_index: Any) -> int:
    """校验研究轮次。

    轮次决定跨轮误差预算 :math:`\\alpha_t=\\alpha/2^{t}`（P12 实现），
    因此取值范围必须与 M1 的校验一致：整数、``1..1022``。
    轮次与「对话轮次」无关，不得混用。
    """
    if isinstance(round_index, bool) or not isinstance(round_index, int):
        raise FreezeInputError(f"round_index 必须是整数，实际得到：{round_index!r}")
    if not ROUND_INDEX_MIN <= round_index <= ROUND_INDEX_MAX:
        raise FreezeInputError(
            f"round_index 必须落在 {ROUND_INDEX_MIN}..{ROUND_INDEX_MAX} 内，"
            f"实际得到：{round_index!r}"
        )
    return round_index


# ---------------------------------------------------------------------------
# 假说视图与声明（输入规范化）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanDeclaration:
    """按假说 id 给出的确证口径声明。

    :param hypothesis_id: 对应的假说 id。
    :param prediction: 可证伪预测文本；``None`` 表示按假说自身反推。
    :param primary_metric: 该假说的主要指标；``None`` 表示按 ``metrics`` 首项。
    :param metrics: 参与声明的全部指标（**全部会被登记进检验族**）。
    :param subgroups: 参与声明的全部子群（**全部会被登记进检验族**）。
    :param effect_threshold: 预定效应阈值；``None`` 表示回退到全局阈值。
    :param null: 零假说文本；``None`` 表示按假说自身反推。
    :param alternative: 备择文本；``None`` 表示按模板生成。
    :param method: 检验方法文本；``None`` 表示取缺省方法描述。
    :param assumptions: 该假说声明的前提（可空）。
    """

    hypothesis_id: str
    prediction: str | None = None
    primary_metric: str | None = None
    metrics: tuple[str, ...] = ()
    subgroups: tuple[str, ...] = ()
    effect_threshold: float | None = None
    null: str | None = None
    alternative: str | None = None
    method: str | None = None
    assumptions: tuple[str, ...] = ()


def _normalize_declarations(declarations: Any) -> dict[str, PlanDeclaration]:
    """把声明输入规范化为 ``假说 id → PlanDeclaration``。

    接受映射（``{id: {...}}``）或序列（元素须含 ``hypothesis_id``）。
    未知键一律拒绝：静默忽略会让调用方误以为某个口径已生效。
    """
    if declarations is None:
        return {}
    items: list[Mapping[str, Any]] = []
    if isinstance(declarations, Mapping):
        for key, value in declarations.items():
            entry = _require_mapping(value, f"declarations[{key!r}]")
            body = dict(entry)
            body.setdefault("hypothesis_id", key)
            items.append(body)
    else:
        for index, entry in enumerate(
            _require_sequence(declarations, "declarations")
        ):
            body = dict(_require_mapping(entry, f"declarations[{index}]"))
            items.append(body)

    result: dict[str, PlanDeclaration] = {}
    for index, body in enumerate(items):
        _check_no_forbidden_keys(body, f"declarations[{index}]")
        # 载体元数据（P09 指标、P10 筛选痕迹等）允许出现但一律不参与口径：
        # 它们只说明「这条候选是从哪来的」，不构成任何确证声明。
        unknown = sorted(
            set(body) - DECLARATION_KEYS - CARRIER_METADATA_KEYS
        )
        if unknown:
            raise FreezeInputError(
                f"declarations[{index}] 含未知键："
                + ", ".join(repr(key) for key in unknown)
                + f"；允许的键为 {sorted(DECLARATION_KEYS)}"
            )
        hypothesis_id = _slug_check(
            _require_nonempty_str(
                body.get("hypothesis_id"), f"declarations[{index}].hypothesis_id"
            ),
            f"declarations[{index}].hypothesis_id",
        )
        if hypothesis_id in result:
            raise FreezeInputError(
                f"declarations 中假说 id {hypothesis_id!r} 重复："
                "同一假说的确证口径只能声明一次，否则登记结果会有歧义。"
            )
        result[hypothesis_id] = PlanDeclaration(
            hypothesis_id=hypothesis_id,
            prediction=_optional_text(body.get("prediction"), "prediction"),
            primary_metric=_optional_text(body.get("primary_metric"), "primary_metric"),
            metrics=_text_tuple(body.get("metrics"), "metrics"),
            subgroups=_text_tuple(body.get("subgroups"), "subgroups"),
            effect_threshold=_optional_number(
                body.get("effect_threshold"), "effect_threshold"
            ),
            null=_optional_text(body.get("null"), "null"),
            alternative=_optional_text(body.get("alternative"), "alternative"),
            method=_optional_text(body.get("method"), "method"),
            assumptions=_text_tuple(body.get("assumptions"), "assumptions"),
        )
    return result


def _optional_text(value: Any, label: str) -> str | None:
    """可选文本：``None`` 原样返回，否则要求非空字符串。"""
    if value is None:
        return None
    return _require_nonempty_str(value, label)


def _optional_number(value: Any, label: str) -> float | None:
    """可选有限实数：``None`` 原样返回，否则要求有限数值。"""
    if value is None:
        return None
    if not _is_finite_number(value):
        raise FreezeInputError(f"{label} 必须是有限实数或 None，实际得到：{value!r}")
    return float(value)


def _text_tuple(value: Any, label: str) -> tuple[str, ...]:
    """可选文本序列：``None`` 返回空元组，否则逐项校验非空且无分隔符。"""
    if value is None:
        return ()
    out: list[str] = []
    for index, item in enumerate(_require_sequence(value, label)):
        text = _require_nonempty_str(item, f"{label}[{index}]")
        out.append(_slug_check(text, f"{label}[{index}]"))
    if len(set(out)) != len(out):
        raise FreezeInputError(f"{label} 含重复项：{out}")
    return tuple(out)


@dataclass(frozen=True)
class HypothesisPlanView:
    """一个假说在冻结计划中所需的字段视图。

    内容全部来自 P07 的假说对象（或其 ``to_dict()`` 产物），本层只做
    **转录与文本化**，不改变任何口径、不新增任何经验断言。

    :param id: 假说 id。
    :param version: 假说版本号。
    :param statement: 假说陈述。
    :param prediction: 可证伪预测（所有预测合并为契约要求的单字段文本）。
    :param scope: 作用域文本，由 ``scope`` 映射渲染。
    :param representation: 完整变换声明（JSON 兼容映射）。
    :param model: 模型描述文本。
    :param parameters: 拟合参数映射。
    :param raw_predictions: 原始预测条目（用于逐条渲染，不进入计划）。
    :param raw_nulls: 原始零假说条目。
    :param raw_alternatives: 原始竞争解释条目。
    :param declared_metrics: 假说自身声明的指标（可空）。
    :param declared_subgroups: 假说自身声明的子群（可空）。
    :param declared_primary_metric: 假说自身声明的主要指标（可空）。
    :param declared_effect_threshold: 假说自身声明的效应阈值（可空）。
    :param declared_method: 假说自身声明的方法（可空）。
    """

    id: str
    version: str
    statement: str
    prediction: str
    scope: str
    representation: Mapping[str, Any]
    model: str
    parameters: Mapping[str, Any]
    raw_predictions: tuple[str, ...] = ()
    raw_nulls: tuple[str, ...] = ()
    raw_alternatives: tuple[str, ...] = ()
    declared_metrics: tuple[str, ...] = ()
    declared_subgroups: tuple[str, ...] = ()
    declared_primary_metric: str | None = None
    declared_effect_threshold: float | None = None
    declared_method: str | None = None

    def entry(self, prediction: str, scope: str, model: str) -> dict:
        """生成契约要求的假说条目（字段与 :data:`HYPOTHESIS_ENTRY_FIELDS` 一致）。"""
        return {
            "id": self.id,
            "version": self.version,
            "statement": self.statement,
            "prediction": prediction,
            "scope": scope,
            "representation": _thaw(self.representation),
            "model": model,
            "parameters": _thaw(self.parameters),
        }


# ---------------------------------------------------------------------------
# 假说输入的收集与规范化
# ---------------------------------------------------------------------------


def _entries_of(value: Any) -> tuple[list[Any], Mapping[str, Any] | None]:
    """拆出「假说条目列表」与「外层声明」。

    支持四种输入形态：

    1. 单条假说（P07 对象，或含 ``id`` 的映射）；
    2. ``{"hypotheses": [...]}`` / ``{"hypothesis": ...}`` 的包装映射
       （外层其余键视为该假说的声明）；
    3. 可迭代的候选集合（如 P10 的 ``FreezeSelection``）；
    4. 上述任一形态混用。

    :return: ``(条目列表, 外层声明或 None)``。
    """
    if isinstance(value, Mapping):
        if "hypotheses" in value and "hypothesis" not in value:
            outer = {k: v for k, v in value.items() if k != "hypotheses"}
            return list(_require_sequence(value["hypotheses"], "hypotheses")), (
                outer or None
            )
        if "hypothesis" in value:
            outer = {k: v for k, v in value.items() if k != "hypothesis"}
            return [value["hypothesis"]], (outer or None)
        return [value], None
    if isinstance(value, (str, bytes)):
        raise FreezeInputError(
            f"candidate 不能是字符串或字节串，实际得到：{value!r}"
        )
    if isinstance(value, Iterable):
        return list(value), None
    return [value], None


def _is_hypothesis_like(value: Any) -> bool:
    """判断对象是否像一条假说（含 ``statement`` 与 ``representation``）。"""
    if isinstance(value, Mapping):
        return "statement" in value and "representation" in value
    return hasattr(value, "statement") and hasattr(value, "representation")


def _extract_hypothesis(item: Any, index: int) -> tuple[Any, Mapping[str, Any] | None]:
    """从候选条目中取出假说对象与随附声明。

    候选条目可以是：P07 假说、含 ``hypothesis`` 的映射（P10 评估记录与假说
    的连接形态）、含 ``hypothesis_id`` 的声明映射等。

    :raises FreezeInputError: 条目未携带假说对象——本模块必须冻结
        ``representation`` / ``model`` / ``parameters``，仅有评估分数不足。
    """
    if isinstance(item, Mapping):
        _check_no_forbidden_keys(item, f"candidate[{index}]")
        if "hypothesis" in item:
            body = dict(item)
            inner = body.pop("hypothesis")
            return inner, (body or None)
        if _is_hypothesis_like(item):
            return item, None
        hypothesis_id = item.get("hypothesis_id") or item.get("id")
        raise FreezeInputError(
            f"candidate[{index}] 未携带假说对象"
            f"（仅见标识 {hypothesis_id!r}）："
            "冻结计划必须登记假说的 representation / model / parameters，"
            "仅有评估记录不足以冻结；请把假说对象一并放入候选，"
            "或用 hypotheses= 显式提供。"
        )
    if _is_hypothesis_like(item):
        return item, None
    hypothesis_id = getattr(item, "hypothesis_id", None) or getattr(item, "id", None)
    raise FreezeInputError(
        f"candidate[{index}]（{type(item).__name__}）未携带假说对象"
        f"（仅见标识 {hypothesis_id!r}）：请用 hypotheses= 显式提供假说对象。"
    )


def _read(value: Any, name: str, default: Any = None) -> Any:
    """从假说对象或映射里读一个字段。"""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _hypothesis_view(value: Any, index: int) -> HypothesisPlanView:
    """把假说（P07 对象或映射）规范化为 :class:`HypothesisPlanView`。"""
    _check_no_forbidden_keys(
        value if isinstance(value, Mapping) else {}, f"hypothesis[{index}]"
    )
    hypothesis_id = _slug_check(
        _require_nonempty_str(_read(value, "id"), f"hypothesis[{index}].id"),
        f"hypothesis[{index}].id",
    )
    version = _read(value, "version")
    if not isinstance(version, str) or not version.strip():
        raise FreezeInputError(
            f"hypothesis[{index}].version 必须是非空字符串，实际得到：{version!r}"
        )
    statement = _require_nonempty_str(
        _read(value, "statement"), f"hypothesis[{index}].statement"
    )

    representation = _read(value, "representation")
    if not isinstance(representation, Mapping):
        raise FreezeInputError(
            f"hypothesis[{index}].representation 必须是映射（完整变换声明），"
            f"实际得到：{type(representation).__name__}"
        )
    representation = _thaw(representation)

    model_raw = _read(value, "model")
    if model_raw is None:
        raise FreezeInputError(
            f"hypothesis[{index}].model 未声明："
            "冻结计划要求写明模型描述，缺省即报错而不是留空（P07 亦不默认填充）。"
        )
    model_text = _as_text(model_raw, f"hypothesis[{index}].model")

    parameters = _read(value, "fitted_parameters", {})
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, Mapping):
        raise FreezeInputError(
            f"hypothesis[{index}].fitted_parameters 必须是映射，"
            f"实际得到：{type(parameters).__name__}"
        )
    parameters = _thaw(parameters)

    scope_text = _as_text(_read(value, "scope", {}), f"hypothesis[{index}].scope")

    raw_predictions = _text_entries(
        _read(value, "predictions", ()), f"hypothesis[{index}].predictions"
    )
    raw_nulls = _text_entries(
        _read(value, "null_hypotheses", ()), f"hypothesis[{index}].null_hypotheses"
    )
    raw_alternatives = _text_entries(
        _read(value, "alternatives", ()), f"hypothesis[{index}].alternatives"
    )

    # 假说自身的 ``confirmation_plan`` 槽位：P08 留空，调用方可在此前置声明口径。
    plan_slot = _read(value, "confirmation_plan")
    declared_metrics, declared_subgroups = _plan_slot_scope(plan_slot, index)

    return HypothesisPlanView(
        id=hypothesis_id,
        version=version,
        statement=statement,
        prediction=_derive_prediction(raw_predictions, hypothesis_id),
        scope=scope_text,
        representation=representation,
        model=model_text,
        parameters=parameters,
        raw_predictions=raw_predictions,
        raw_nulls=raw_nulls,
        raw_alternatives=raw_alternatives,
        declared_metrics=declared_metrics,
        declared_subgroups=declared_subgroups,
        declared_primary_metric=_plan_slot_text(plan_slot, "primary_metric"),
        declared_effect_threshold=_plan_slot_number(plan_slot, "effect_threshold"),
        declared_method=_plan_slot_text(plan_slot, "method"),
    )


def _plan_slot_scope(plan_slot: Any, index: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """从假说的 ``confirmation_plan`` 槽位读指标与子群声明。"""
    if not isinstance(plan_slot, Mapping):
        return (), ()
    metrics = _text_tuple(plan_slot.get("metrics"), f"hypothesis[{index}].metrics")
    subgroups = _text_tuple(
        plan_slot.get("subgroups"), f"hypothesis[{index}].subgroups"
    )
    return metrics, subgroups


def _plan_slot_text(plan_slot: Any, key: str) -> str | None:
    """从 ``confirmation_plan`` 槽位读一个可选文本。"""
    if not isinstance(plan_slot, Mapping):
        return None
    value = plan_slot.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise FreezeInputError(f"hypothesis.confirmation_plan.{key} 必须是非空字符串")


def _plan_slot_number(plan_slot: Any, key: str) -> float | None:
    """从 ``confirmation_plan`` 槽位读一个可选有限实数。"""
    if not isinstance(plan_slot, Mapping):
        return None
    value = plan_slot.get(key)
    if value is None:
        return None
    if not _is_finite_number(value):
        raise FreezeInputError(
            f"hypothesis.confirmation_plan.{key} 必须是有限实数，实际得到：{value!r}"
        )
    return float(value)


def _text_entries(value: Any, label: str) -> tuple[str, ...]:
    """把预测 / 零假说 / 竞争解释条目渲染为文本元组。

    条目可以是字符串，也可以是含 ``incompatible_with`` / ``statement`` /
    ``null_id`` 等键的映射；映射按可读顺序渲染为一段文本。
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise FreezeInputError(f"{label} 必须是条目序列，实际得到字符串：{value!r}")
    out: list[str] = []
    for index, item in enumerate(_require_sequence(value, label)):
        if isinstance(item, str):
            out.append(_require_nonempty_str(item, f"{label}[{index}]"))
            continue
        if isinstance(item, Mapping):
            if not item:
                raise FreezeInputError(f"{label}[{index}] 不能是空映射")
            preferred = (
                "incompatible_with",
                "statement",
                "null_statement",
                "alternative_statement",
                "description",
                "null_id",
            )
            keys = [key for key in preferred if key in item]
            keys.extend(
                sorted(key for key in item if key not in keys)
            )
            out.append("; ".join(f"{key}={_render_scalar(item[key])}" for key in keys))
            continue
        raise FreezeInputError(
            f"{label}[{index}] 必须是字符串或映射，实际得到：{type(item).__name__}"
        )
    return tuple(out)


def _derive_prediction(raw_predictions: Sequence[str], hypothesis_id: str) -> str:
    """由假说的可证伪预测派生契约要求的单字段 ``prediction``。

    P07 要求每条预测写明「什么结果与假说不相容」；本层把这些条目串联为
    一段文本，**保留可证伪性**，不做语义改写。
    """
    if not raw_predictions:
        raise PlanCompletenessError(
            f"假说 {hypothesis_id!r} 未声明任何可证伪预测："
            "冻结计划必须有 prediction 字段（写明什么结果与假说不相容），"
            "缺省即报错而不是留空。"
        )
    if len(raw_predictions) == 1:
        return raw_predictions[0]
    return "；".join(
        f"({index + 1}) {text}" for index, text in enumerate(raw_predictions)
    )


# ---------------------------------------------------------------------------
# 检验族（验收标准③）
# ---------------------------------------------------------------------------


def _family_pairs(view: HypothesisPlanView, declaration: PlanDeclaration | None) -> dict:
    """确定一个假说的指标、子群、零假说、备择、方法与效应阈值。

    优先级：调用方声明 > 假说的 ``confirmation_plan`` 槽位 > 按假说自身反推。
    反推只做**转录或模板替换**，不编造任何统计前提。
    """
    metrics = (
        declaration.metrics
        if declaration and declaration.metrics
        else view.declared_metrics
    )
    subgroups = (
        declaration.subgroups
        if declaration and declaration.subgroups
        else view.declared_subgroups
    )
    if not subgroups:
        subgroups = (DEFAULT_SUBGROUP,)

    primary_metric = (
        (declaration.primary_metric if declaration else None)
        or view.declared_primary_metric
        or (metrics[0] if metrics else None)
    )

    threshold = None
    if declaration and declaration.effect_threshold is not None:
        threshold = declaration.effect_threshold
    elif view.declared_effect_threshold is not None:
        threshold = view.declared_effect_threshold

    null_text = (declaration.null if declaration else None) or (
        view.raw_nulls[0] if view.raw_nulls else None
    )
    alternative_text = (declaration.alternative if declaration else None) or (
        view.raw_alternatives[0] if view.raw_alternatives else None
    )
    method_text = (declaration.method if declaration else None) or view.declared_method

    return {
        "metrics": tuple(metrics),
        "subgroups": tuple(subgroups),
        "primary_metric": primary_metric,
        "effect_threshold": threshold,
        "null": null_text,
        "alternative": alternative_text,
        "method": method_text,
    }


def _family_entries(
    view: HypothesisPlanView, resolved: Mapping[str, Any], delta: float
) -> list[dict]:
    """按「指标 × 子群」笛卡尔积展开该假说的检验条目。"""
    entries: list[dict] = []
    for metric in resolved["metrics"]:
        for subgroup in resolved["subgroups"]:
            null_text = resolved["null"] or NULL_TEMPLATE.format(
                metric=metric, subgroup=subgroup, delta=_render_scalar(delta)
            )
            alternative_text = resolved["alternative"] or ALTERNATIVE_TEMPLATE.format(
                metric=metric, subgroup=subgroup, delta=_render_scalar(delta)
            )
            entries.append(
                {
                    "id": _test_id(view.id, metric, subgroup),
                    "hypothesis_id": view.id,
                    "null": null_text,
                    "alternative": alternative_text,
                    "method": resolved["method"] or DEFAULT_METHOD_TEXT,
                }
            )
    return entries


def family_coverage(plan: Any) -> dict:
    """反解检验族，给出实际覆盖的 ``(假说, 指标, 子群)`` 三元组。

    这是验收标准③的对账入口：契约的检验条目只有 5 个字段，
    因此「指标 / 子群」的登记信息编码在 ``id`` 中，本函数按固定格式解析。

    :param plan: 冻结计划（或含 ``test_family`` 的映射）。
    :return: ``{"pairs": [(h,m,s), ...], "hypothesis_ids": (...), "metrics": (...),
        "subgroups": (...), "test_ids": (...)}``，各项均已排序，便于稳定比对。
    :raises PlanFieldError: 计划结构非法或检验标识格式不符。
    """
    body = _require_mapping(plan, "plan")
    family = body.get("test_family")
    if not isinstance(family, list):
        raise PlanFieldError("plan.test_family 必须是列表。")
    pairs: list[tuple[str, str, str]] = []
    for index, test in enumerate(family):
        if not isinstance(test, Mapping):
            raise PlanFieldError(f"plan.test_family[{index}] 必须是映射。")
        test_id = test.get("id")
        if not isinstance(test_id, str) or not test_id.strip():
            raise PlanFieldError(f"plan.test_family[{index}].id 必须是非空字符串。")
        pairs.append(_parse_test_id(test_id))
    return {
        "pairs": tuple(sorted(pairs)),
        "test_ids": tuple(sorted(str(t.get("id")) for t in family)),
        "hypothesis_ids": tuple(sorted({pair[0] for pair in pairs})),
        "metrics": tuple(sorted({pair[1] for pair in pairs})),
        "subgroups": tuple(sorted({pair[2] for pair in pairs})),
    }


def check_family_completeness(plan: Any, expected: Any) -> dict:
    """把检验族的实际覆盖与声明集合对账（验收标准③的可机检形式）。

    :param plan: 冻结计划。
    :param expected: ``{假说 id: {"metrics": [...], "subgroups": [...]}}`` 的映射，
        或 :class:`PlanDeclaration` 的映射，或 :func:`family_coverage` 的产物。
    :return: ``{"expected_pairs", "actual_pairs", "missing", "unexpected"}``。
    :raises PlanCompletenessError: 存在未登记的声明项，或声明与登记不一致。
    """
    coverage = family_coverage(plan)
    actual = set(coverage["pairs"])

    expected_pairs: set[tuple[str, str, str]] = set()
    if isinstance(expected, Mapping) and "pairs" in expected:
        expected_pairs = {tuple(pair) for pair in expected["pairs"]}  # type: ignore[misc]
    elif isinstance(expected, Mapping):
        for hypothesis_id, spec in expected.items():
            if isinstance(spec, PlanDeclaration):
                metrics = spec.metrics or ("(未声明指标)",)
                subgroups = spec.subgroups or (DEFAULT_SUBGROUP,)
            else:
                body = _require_mapping(spec, f"expected[{hypothesis_id!r}]")
                metrics = tuple(body.get("metrics") or ())
                subgroups = tuple(body.get("subgroups") or (DEFAULT_SUBGROUP,))
            for metric in metrics:
                for subgroup in subgroups:
                    expected_pairs.add((str(hypothesis_id), str(metric), str(subgroup)))
    else:
        expected_pairs = {
            tuple(pair) for pair in _require_sequence(expected, "expected")  # type: ignore[misc]
        }

    missing = sorted(expected_pairs - actual)
    unexpected = sorted(actual - expected_pairs)
    if missing:
        raise PlanCompletenessError(
            "检验族登记不完整，缺少 "
            + ", ".join(f"(假说={h}, 指标={m}, 子群={s})" for h, m, s in missing)
            + f"；{FAMILY_COMPLETENESS_NOTE}"
        )
    return {
        "expected_pairs": tuple(sorted(expected_pairs)),
        "actual_pairs": coverage["pairs"],
        "missing": (),
        "unexpected": tuple(unexpected),
    }


# ---------------------------------------------------------------------------
# 预处理与假设
# ---------------------------------------------------------------------------


def _derive_preprocessing_steps(views: Sequence[HypothesisPlanView]) -> list[dict]:
    """由各假说的 ``representation`` 派生预处理步骤清单。

    派生是**转录**：每条步骤写明来源假说，使「冻结的预处理」与「假说声明的
    变换」可逐条对应。相同声明只登记一次，输出顺序确定（按名称与声明排序）。
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for view in views:
        for name in sorted(view.representation, key=str):
            specification = _render_scalar(view.representation[name])
            seen.setdefault((str(name), specification), []).append(view.id)
    steps: list[dict] = []
    for (name, specification), sources in sorted(seen.items()):
        steps.append(
            {
                "name": name,
                "specification": specification,
                "source_hypotheses": sorted(sources),
                "note": "由假说 representation 转录；本模块不执行任何变换。",
            }
        )
    return steps


def _normalize_assumptions(
    assumptions: Any, justifications: Any, protocol_view: _ProtocolView
) -> list[dict]:
    """规范化假设清单。

    ``assumptions`` 缺省时取协议登记的依赖假设；论证文本由调用方给出，
    缺省则使用 :data:`DEFAULT_JUSTIFICATION_TEXT`（只说明「沿用协议登记」，
    不声称已被验证）。
    """
    mapping: dict[str, str] = {}
    if justifications is not None:
        body = _require_mapping(justifications, "assumption_justifications")
        for key, value in body.items():
            mapping[_require_nonempty_str(str(key), "assumption_justifications 键")] = (
                _require_nonempty_str(value, f"assumption_justifications[{key!r}]")
            )

    entries: list[dict] = []
    if assumptions is None:
        names = list(protocol_view.declared_assumptions)
    else:
        names = []
        for index, item in enumerate(_require_sequence(assumptions, "assumptions")):
            if isinstance(item, str):
                names.append(_require_nonempty_str(item, f"assumptions[{index}]"))
                continue
            body = _require_mapping(item, f"assumptions[{index}]")
            name = _require_nonempty_str(body.get("name"), f"assumptions[{index}].name")
            justification = _require_nonempty_str(
                body.get("justification"), f"assumptions[{index}].justification"
            )
            entries.append({"name": name, "justification": justification})

    if not entries and not names:
        raise PlanCompletenessError(
            "计划必须登记显式假设及其论证："
            "M1 拒绝空假设清单，且「代表性 / 组间独立性」等前提必须可追溯。"
        )
    for name in names:
        entries.append(
            {
                "name": name,
                "justification": mapping.get(name, DEFAULT_JUSTIFICATION_TEXT),
            }
        )
    # 去重（同名只保留首次出现），顺序确定。
    deduped: list[dict] = []
    seen_names: set[str] = set()
    for entry in entries:
        if entry["name"] in seen_names:
            continue
        seen_names.add(entry["name"])
        deduped.append(entry)
    return deduped


# ---------------------------------------------------------------------------
# 字段自检（验收标准①②）
# ---------------------------------------------------------------------------


def _require_exact_fields(item: Mapping[str, Any], fields: Sequence[str], label: str) -> None:
    """断言映射的键集与契约字段清单**完全一致**（无多余无缺漏）。"""
    expected = set(fields)
    actual = set(item)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise PlanFieldError(
        f"{label} 字段与契约不符；缺少 {missing}，多出 {extra}；"
        f"契约要求恰为 {list(fields)}"
    )


def validate_frozen_plan(
    plan: Any,
    *,
    expected_protocol_id: str | None = None,
    exploration_refs: Sequence[str] | None = None,
) -> dict:
    """对冻结计划做**离线**结构自检（不访问任何数据、不读确证内容）。

    校验项：

    1. 顶层字段集与 :data:`FROZEN_PLAN_FIELDS` 完全一致；
    2. ``hypotheses`` 非空、条目字段集与 :data:`HYPOTHESIS_ENTRY_FIELDS` 一致、
       ``id`` 唯一、文本字段非空、``representation`` / ``parameters`` 为映射；
    3. ``preprocessing`` 字段集与 :data:`PREPROCESSING_FIELDS` 一致，
       且 ``fit_dataset_refs`` 仅指向探索分区 E（验收标准②）；
    4. ``test_family`` 非空、条目字段集与 :data:`TEST_ENTRY_FIELDS` 一致、
       ``id`` 唯一、引用的假说均存在、且每个假说至少一条检验；
    5. ``round_index`` 落在合法范围；``effect_threshold`` 为有限实数；
       文本口径字段非空；
    6. ``assumptions`` 非空且每条含非空 ``name`` 与 ``justification``。

    :param plan: 待校验计划。
    :param expected_protocol_id: 若给出，则要求计划的 ``protocol_id`` 与之相同。
    :param exploration_refs: 探索分区 E 的允许引用；给出时 ``fit_dataset_refs``
        必须是其子集。
    :return: 校验摘要（含假说数、检验数、覆盖三元组与工具版本）。
    :raises PlanFieldError: 结构不符。
    :raises PlanCompletenessError: 检验族或假设登记不完整。
    """
    body = _require_mapping(plan, "plan")
    _check_no_forbidden_keys(body, "plan")
    _require_exact_fields(body, FROZEN_PLAN_FIELDS, "冻结计划")

    protocol_id = _require_nonempty_str(body["protocol_id"], "plan.protocol_id")
    if expected_protocol_id is not None and protocol_id != expected_protocol_id:
        raise PlanFieldError(
            f"计划归属协议 {protocol_id!r}，与目标协议 {expected_protocol_id!r} 不符："
            "跨协议绑定会让他人的协议口径被冻结。"
        )
    _validate_round_index(body["round_index"])

    hypotheses = body["hypotheses"]
    if not isinstance(hypotheses, list) or not hypotheses:
        raise PlanCompletenessError(
            "plan.hypotheses 必须是非空列表：M1 要求有限非空的假说清单。"
        )
    hypothesis_ids: set[str] = set()
    for index, entry in enumerate(hypotheses):
        if not isinstance(entry, Mapping):
            raise PlanFieldError(f"plan.hypotheses[{index}] 必须是映射。")
        _require_exact_fields(
            entry, HYPOTHESIS_ENTRY_FIELDS, f"plan.hypotheses[{index}]"
        )
        hypothesis_id = _slug_check(
            _require_nonempty_str(entry["id"], f"hypotheses[{index}].id"),
            f"hypotheses[{index}].id",
        )
        if hypothesis_id in hypothesis_ids:
            raise PlanFieldError(
                f"plan.hypotheses 中 id {hypothesis_id!r} 重复："
                "M1 要求假说标识唯一。"
            )
        hypothesis_ids.add(hypothesis_id)
        for field in ("version", "statement", "prediction", "scope", "model"):
            _require_nonempty_str(entry[field], f"hypotheses[{index}].{field}")
        for field in ("representation", "parameters"):
            if not isinstance(entry[field], Mapping):
                raise PlanFieldError(
                    f"hypotheses[{index}].{field} 必须是映射，"
                    f"实际得到：{type(entry[field]).__name__}"
                )

    preprocessing = body["preprocessing"]
    if not isinstance(preprocessing, Mapping):
        raise PlanFieldError("plan.preprocessing 必须是映射。")
    _require_exact_fields(preprocessing, PREPROCESSING_FIELDS, "plan.preprocessing")
    steps = preprocessing["steps"]
    if not isinstance(steps, list):
        raise PlanFieldError("plan.preprocessing.steps 必须是列表（允许为空列表）。")
    fit_refs = preprocessing["fit_dataset_refs"]
    if not isinstance(fit_refs, list) or not fit_refs:
        raise PlanFieldError(
            "plan.preprocessing.fit_dataset_refs 必须是非空列表："
            "必须声明拟合所用的探索引用。"
        )
    allowed = None if exploration_refs is None else {str(ref) for ref in exploration_refs}
    for index, ref in enumerate(fit_refs):
        ref_text = _require_nonempty_str(
            ref, f"preprocessing.fit_dataset_refs[{index}]"
        )
        if allowed is not None and ref_text not in allowed:
            raise PlanFieldError(
                f"fit_dataset_refs[{index}]={ref_text!r} 不是探索分区 E 的引用："
                + FIT_REF_POLICY_NOTE
            )

    for field in (
        "primary_metric",
        "sampling_plan",
        "stopping_rule",
        "inference_unit",
        "eligibility",
        "quality_rules_version",
    ):
        _require_nonempty_str(body[field], f"plan.{field}")
    if not _is_finite_number(body["effect_threshold"]):
        raise PlanFieldError(
            f"plan.effect_threshold 必须是有限实数，实际得到：{body['effect_threshold']!r}"
        )

    family = body["test_family"]
    if not isinstance(family, list) or not family:
        raise PlanCompletenessError(
            "plan.test_family 必须是非空列表：M1 要求完整的检验族被冻结。"
        )
    test_ids: set[str] = set()
    covered: set[str] = set()
    for index, test in enumerate(family):
        if not isinstance(test, Mapping):
            raise PlanFieldError(f"plan.test_family[{index}] 必须是映射。")
        _require_exact_fields(test, TEST_ENTRY_FIELDS, f"plan.test_family[{index}]")
        test_id = _require_nonempty_str(test["id"], f"test_family[{index}].id")
        if test_id in test_ids:
            raise PlanFieldError(f"plan.test_family 中 id {test_id!r} 重复。")
        test_ids.add(test_id)
        hypothesis_id = _require_nonempty_str(
            test["hypothesis_id"], f"test_family[{index}].hypothesis_id"
        )
        if hypothesis_id not in hypothesis_ids:
            raise PlanFieldError(
                f"test_family[{index}] 引用了未登记的假说 {hypothesis_id!r}。"
            )
        covered.add(hypothesis_id)
        for field in ("null", "alternative", "method"):
            _require_nonempty_str(test[field], f"test_family[{index}].{field}")
    uncovered = sorted(hypothesis_ids - covered)
    if uncovered:
        raise PlanCompletenessError(
            "以下假说没有任何已登记的检验：" + ", ".join(uncovered) + "；"
            "M1 要求每个被声明的假说都有对应检验，" + FAMILY_COMPLETENESS_NOTE
        )

    assumptions = body["assumptions"]
    if not isinstance(assumptions, list) or not assumptions:
        raise PlanCompletenessError(
            "plan.assumptions 必须是非空列表：需要显式假设及其论证。"
        )
    for index, assumption in enumerate(assumptions):
        if not isinstance(assumption, Mapping):
            raise PlanFieldError(f"plan.assumptions[{index}] 必须是映射。")
        _require_nonempty_str(assumption.get("name"), f"assumptions[{index}].name")
        _require_nonempty_str(
            assumption.get("justification"), f"assumptions[{index}].justification"
        )

    coverage = family_coverage(body)
    return {
        "freeze_version": FREEZE_VERSION,
        "protocol_id": protocol_id,
        "round_index": body["round_index"],
        "hypothesis_count": len(hypotheses),
        "test_count": len(family),
        "fit_dataset_refs": [str(ref) for ref in fit_refs],
        "preprocessing_step_count": len(steps),
        "assumption_count": len(assumptions),
        "family_coverage": coverage,
        "plan_digest": frozen_plan_digest(body),
    }


# ---------------------------------------------------------------------------
# 主入口：冻结计划生成
# ---------------------------------------------------------------------------


def build_frozen_plan(
    candidate: Any,
    protocol_id: str,
    round_index: int,
    *,
    protocol: Any,
    declarations: Any = None,
    hypotheses: Any = None,
    preprocessing_steps: Any = None,
    fit_dataset_refs: Any = None,
    effect_threshold: Any = None,
    primary_metric: Any = None,
    assumptions: Any = None,
    assumption_justifications: Any = None,
    default_method: Any = None,
    **rejected: Any,
) -> dict:
    """由 P10 的冻结候选生成 M1 可接受的**冻结计划**（验收标准①②③）。

    流程（顺序固定，确保可复现）::

        校验协议与轮次 → 收集假说视图 → 归并声明
        → 展开检验族（指标 × 子群）→ 派生预处理与假设
        → 装配 13 个契约字段 → 离线自检

    :param candidate: 候选；可以是单条「携带假说」的候选、候选集合
        （如 P10 的 ``FreezeSelection``，但每个元素须携带假说），
        或 ``{"hypotheses": [...]}`` / ``{"hypothesis": ...}`` 的包装。
    :param protocol_id: 目标协议标识；须与 ``protocol["protocol_id"]`` 相同。
    :param round_index: 研究轮次（``1..1022``），决定跨轮误差预算。
    :param protocol: **必填**公开协议（``build()`` / ``describe()`` 的产物）。
        本模块只读其元信息段（task / dependence / confirmation_policy /
        quality_rules_version / resources），不读取任何分区内容。
    :param declarations: 按假说 id 的确证口径声明（见 :class:`PlanDeclaration`）。
        未声明的项按假说自身字段反推。
    :param hypotheses: 显式假说对象序列；当候选只携带评估记录时使用。
    :param preprocessing_steps: 覆盖派生的预处理步骤清单。
    :param fit_dataset_refs: 拟合引用；缺省为协议的探索分区 E。
        显式给出时必须是探索分区引用的子集（验收标准②）。
    :param effect_threshold: 全局预定效应阈值。**无默认值**：
        假说声明与调用方均未给出时报错（「预定」不可由默认值填充）。
    :param primary_metric: 计划的主要指标；缺省由各假说声明归并确定。
    :param assumptions: 覆盖假设清单（字符串或 ``{name, justification}``）。
    :param assumption_justifications: ``{假设名: 论证}``，用于补齐论证文本。
    :param default_method: 缺省检验方法描述。
    :return: **恰好含 :data:`FROZEN_PLAN_FIELDS` 十三个键**的 JSON 兼容计划字典。
    :raises FreezeInputError: 输入形态非法、缺假说或参数越界。
    :raises PlanFieldError: 字段或引用与契约不符。
    :raises PlanCompletenessError: 检验族 / 假设 / 效应阈值登记不完整。
    """
    _reject_forbidden_kwargs(rejected, "build_frozen_plan")
    if default_method is not None:
        default_method = _require_nonempty_str(default_method, "default_method")

    proto = _protocol_view(protocol)
    target_protocol_id = _require_nonempty_str(protocol_id, "protocol_id")
    if target_protocol_id != proto.protocol_id:
        raise PlanFieldError(
            f"传入的 protocol_id={target_protocol_id!r} 与公开协议 "
            f"{proto.protocol_id!r} 不一致：冻结计划必须绑定到明确且唯一的协议。"
        )
    round_value = _validate_round_index(round_index)

    decl_map = _normalize_declarations(declarations)

    # -- 收集假说视图 ------------------------------------------------------
    items, outer_declaration = _entries_of(candidate)
    if hypotheses is not None:
        # 显式给出假说时**只认显式列表**：candidate 退化为「声明载体」，
        # 其中缺少 representation / model 的评估记录不会再触发报错。
        # 这样「P10 候选 + 显式假说对象」是最自然的调用方式。
        if isinstance(hypotheses, Mapping):
            body = _require_mapping(hypotheses, "hypotheses")
            items = [body["hypothesis"]] if "hypothesis" in body else list(
                _require_sequence(body.get("hypotheses"), "hypotheses.hypotheses")
            )
        else:
            items = _require_sequence(hypotheses, "hypotheses")
    views: list[HypothesisPlanView] = []
    inline_declarations: dict[str, dict] = {}
    for index, item in enumerate(items):
        if item is None:
            raise FreezeInputError(f"candidate[{index}] 为 None，不是有效候选。")
        raw_hypothesis, inline = _extract_hypothesis(item, index)
        view = _hypothesis_view(raw_hypothesis, index)
        if inline:
            inline_declarations[view.id] = dict(inline)
        views.append(view)
    if not views:
        raise FreezeInputError(
            "未收集到任何假说：冻结计划必须至少登记一个假说。"
        )
    if outer_declaration:
        inline_declarations.setdefault(views[0].id, dict(outer_declaration))

    # 内联声明并入外部声明（外部声明优先，因其更明确）。
    merged_declarations: dict[str, PlanDeclaration] = {}
    for hypothesis_id, body in inline_declarations.items():
        merged_declarations.update(_normalize_declarations({hypothesis_id: body}))
    for hypothesis_id, declaration in decl_map.items():
        merged_declarations[hypothesis_id] = declaration

    unknown_declarations = sorted(set(merged_declarations) - {v.id for v in views})
    if unknown_declarations:
        raise FreezeInputError(
            "声明引用了未提供的假说 id："
            + ", ".join(repr(item) for item in unknown_declarations)
            + "；声明的假说必须与登记进计划的假说一致。"
        )

    # 假说 id 唯一性（M1 亦要求）。
    seen_ids: set[str] = set()
    for view in views:
        if view.id in seen_ids:
            raise FreezeInputError(
                f"候选集合中假说 id {view.id!r} 重复："
                "同一假说在计划中只能登记一次（版本差异请用不同 id 或先派生新版本）。"
            )
        seen_ids.add(view.id)

    # -- 归并每个假说的口径 ------------------------------------------------
    resolved_per_view: list[dict] = []
    for view in views:
        resolved = _family_pairs(view, merged_declarations.get(view.id))
        if not resolved["metrics"]:
            raise PlanCompletenessError(
                f"假说 {view.id!r} 未声明任何指标："
                "检验族必须登记实际用于声明的全部指标；"
                "请经 declarations 或假说 confirmation_plan.metrics 显式声明。"
            )
        resolved_per_view.append(resolved)

    # -- 效应阈值：必须显式且唯一 -----------------------------------------
    declared_thresholds: dict[str, float] = {}
    for view, resolved in zip(views, resolved_per_view):
        if resolved["effect_threshold"] is not None:
            declared_thresholds[view.id] = float(resolved["effect_threshold"])
    if effect_threshold is not None:
        if not _is_finite_number(effect_threshold):
            raise FreezeInputError(
                f"effect_threshold 必须是有限实数，实际得到：{effect_threshold!r}"
            )
        delta = float(effect_threshold)
        conflicts = {
            key: value for key, value in declared_thresholds.items() if value != delta
        }
        if conflicts:
            raise PlanCompletenessError(
                "效应阈值冲突：调用方给出 "
                f"{delta!r}，但假说 "
                + ", ".join(f"{k!r}={v!r}" for k, v in sorted(conflicts.items()))
                + " 声明了不同取值。计划只有单一 effect_threshold 字段，"
                "口径必须一致；请显式统一后再冻结。"
            )
    elif declared_thresholds:
        distinct = sorted(set(declared_thresholds.values()))
        if len(distinct) > 1:
            raise PlanCompletenessError(
                "各假说声明的效应阈值不一致："
                + ", ".join(f"{k!r}={v!r}" for k, v in sorted(declared_thresholds.items()))
                + "；计划只有单一 effect_threshold 字段，必须统一。"
            )
        delta = distinct[0]
    else:
        raise PlanCompletenessError(
            "未声明 effect_threshold：预定效应标准必须显式给出"
            "（经 effect_threshold= 参数或假说的 confirmation_plan.effect_threshold），"
            "本模块**不提供默认值**——能被默认值填上的阈值就不再是预定的。"
        )
    if delta <= 0:
        raise PlanCompletenessError(
            f"effect_threshold 必须为正数（最小有意义差异），实际得到：{delta!r}"
        )

    # -- 主要指标 ----------------------------------------------------------
    if primary_metric is not None:
        primary = _require_nonempty_str(primary_metric, "primary_metric")
    else:
        candidates = sorted(
            {
                resolved["primary_metric"]
                for resolved in resolved_per_view
                if resolved["primary_metric"]
            }
        )
        if not candidates:
            raise PlanCompletenessError(
                "无法确定 primary_metric："
                "请经 primary_metric= 参数或假说声明显式给出。"
            )
        if len(candidates) > 1:
            raise PlanCompletenessError(
                "各假说的主要指标不一致："
                + ", ".join(repr(item) for item in candidates)
                + "；计划只有单一 primary_metric 字段，请显式统一。"
            )
        primary = candidates[0]
    for view, resolved in zip(views, resolved_per_view):
        if primary not in resolved["metrics"]:
            raise PlanCompletenessError(
                f"主要指标 {primary!r} 未出现在假说 {view.id!r} 的指标声明 "
                f"{list(resolved['metrics'])} 中："
                "主要指标必须作为检验族的一项被完整登记。"
            )

    # -- 自适应默认方法 ----------------------------------------------------
    effective_default_method = default_method or DEFAULT_METHOD_TEXT

    # -- 装配假说条目与检验族 ----------------------------------------------
    hypothesis_entries: list[dict] = []
    test_family: list[dict] = []
    for view, resolved in zip(views, resolved_per_view):
        hypothesis_entries.append(
            view.entry(prediction=view.prediction, scope=view.scope, model=view.model)
        )
        if resolved["method"] is None:
            resolved = dict(resolved)
            resolved["method"] = effective_default_method
        test_family.extend(_family_entries(view, resolved, delta))

    # -- 预处理 ------------------------------------------------------------
    if preprocessing_steps is None:
        steps = _derive_preprocessing_steps(views)
    else:
        steps = _normalize_steps(preprocessing_steps)

    fit_refs = _resolve_fit_refs(fit_dataset_refs, proto)

    # -- 假设 --------------------------------------------------------------
    assumption_entries = _normalize_assumptions(
        assumptions, assumption_justifications, proto
    )

    # -- 逐键装配（无多余、无缺漏） ----------------------------------------
    plan: dict = {
        "protocol_id": proto.protocol_id,
        "round_index": round_value,
        "hypotheses": hypothesis_entries,
        "preprocessing": {"steps": steps, "fit_dataset_refs": fit_refs},
        "primary_metric": primary,
        "test_family": test_family,
        "effect_threshold": delta,
        "sampling_plan": proto.sampling_plan,
        "stopping_rule": proto.stopping_rule,
        "inference_unit": proto.inference_unit,
        "eligibility": proto.eligibility,
        "quality_rules_version": proto.quality_rules_version,
        "assumptions": assumption_entries,
    }

    # 自检：字段集与契约一致、检验族完整、拟合引用只认 E。
    validate_frozen_plan(
        plan,
        expected_protocol_id=proto.protocol_id,
        exploration_refs=proto.exploration_refs,
    )
    check_family_completeness(
        plan,
        {
            view.id: {
                "metrics": list(resolved["metrics"]),
                "subgroups": list(resolved["subgroups"]),
            }
            for view, resolved in zip(views, resolved_per_view)
        },
    )
    return plan


def _normalize_steps(steps: Any) -> list[Any]:
    """规范化显式给出的预处理步骤清单。"""
    out: list[Any] = []
    for index, item in enumerate(_require_sequence(steps, "preprocessing_steps")):
        if isinstance(item, str):
            out.append(_require_nonempty_str(item, f"preprocessing_steps[{index}]"))
            continue
        body = _require_mapping(item, f"preprocessing_steps[{index}]")
        if not body:
            raise FreezeInputError(f"preprocessing_steps[{index}] 不能是空映射。")
        out.append(_thaw(body))
    return out


def _resolve_fit_refs(fit_dataset_refs: Any, proto: _ProtocolView) -> list[str]:
    """确定拟合引用：缺省取探索分区 E，显式给出时必须是 E 的子集（验收标准②）。

    :raises PlanFieldError: 引用为空、非字符串，或不属于探索分区。
    """
    if fit_dataset_refs is None:
        return list(proto.exploration_refs)
    refs = _require_sequence(fit_dataset_refs, "fit_dataset_refs")
    if not refs:
        raise PlanFieldError(
            "fit_dataset_refs 不能为空：必须声明拟合所用的探索引用。"
        )
    allowed = set(proto.exploration_refs)
    out: list[str] = []
    for index, ref in enumerate(refs):
        text = _require_nonempty_str(ref, f"fit_dataset_refs[{index}]")
        if text not in allowed:
            raise PlanFieldError(
                f"fit_dataset_refs[{index}]={text!r} 不是探索分区 E 的引用"
                f"（本协议探索引用为 {sorted(allowed)}）：" + FIT_REF_POLICY_NOTE
            )
        if text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# 主入口：确证绑定（不消费）
# ---------------------------------------------------------------------------


def bind(
    module_confirmer: Any,
    c_ref: Any,
    plan: Any,
    *,
    verify_digest: bool = True,
) -> dict:
    """调用 M1 的 ``bind_confirmation`` 完成冻结绑定（**不消费**）。

    本函数是「冻结」这一步的全部实现，它刻意做得极窄：

    1. 先做**离线自检**（:func:`validate_frozen_plan`）——计划不合法就地失败，
       **连 confirmer 都不碰**，避免把半成品计划提交到证据库；
    2. 只调用注入模块的 ``bind_confirmation(c_ref, plan)``；
    3. 用 :func:`frozen_plan_digest` 复算摘要，与返回的 ``plan_digest`` 比对，
       确认冻结内容确实是刚提交的那一份；
    4. **不**调用 ``consume_confirmation`` / ``read_dataset`` / ``quality``：
       本模块没有任何读取确证分区内容的路径，消费属 P12 且必须原子化。

    :param module_confirmer: 以 confirmer 角色构造的 M1 模块（鸭子类型即可）。
    :param c_ref: 目标确证分区的引用标识。
    :param plan: :func:`build_frozen_plan` 的产物。
    :param verify_digest: 是否复算摘要并比对（默认 ``True``）。
    :return: M1 的绑定结果（含 ``binding_id`` / ``plan_digest`` / ``alpha`` / ``state``）。
    :raises PlanBindingError: 注入模块缺接口、返回形态非法或摘要不一致。
    :raises PlanFieldError: 计划未通过离线自检。
    """
    if not isinstance(verify_digest, bool):
        raise FreezeInputError(
            f"verify_digest 必须是布尔值，实际得到：{verify_digest!r}"
        )
    # 第一道：离线自检。不合法就在这里失败，绝不把坏计划送进证据库。
    validate_frozen_plan(plan)

    c_ref_text = _require_nonempty_str(c_ref, "c_ref")

    binder = getattr(module_confirmer, "bind_confirmation", None)
    if not callable(binder):
        raise PlanBindingError(
            "注入的 confirmer 模块没有 bind_confirmation 方法"
            f"（实际类型 {type(module_confirmer).__name__}）："
            "本模块只通过 M1 的公开接口完成绑定。"
        )

    expected_digest = frozen_plan_digest(plan)
    result = binder(c_ref_text, plan)
    if not isinstance(result, Mapping):
        raise PlanBindingError(
            f"bind_confirmation 应返回映射，实际得到：{type(result).__name__}"
        )
    if verify_digest:
        reported = result.get("plan_digest")
        if reported != expected_digest:
            raise PlanBindingError(
                "绑定记录的 plan_digest 与本地复算摘要不一致："
                f"记录为 {reported!r}，本地为 {expected_digest!r}；"
                "冻结内容可能在提交前后被改写，请核查后在人工确认下重跑。"
            )
    return _thaw(result)
