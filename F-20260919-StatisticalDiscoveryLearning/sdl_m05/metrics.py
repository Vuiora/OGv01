"""M5 多维评估指标 G/S/N/C（P09 交付物）。

本模块把《SDL算法框架说明》§4 的四个探索期指标落成**彼此独立、可单算、
可复核**的纯函数，并给出一个把它们**并列装配**（而非合并）的评估记录对象：

============  ==================================================  ================
指标          含义                                                本模块入口
============  ==================================================  ================
``G`` 预测增益 候选相对基线在同一份数据上的损失下降                 :func:`gain`
``S`` 稳定性   重采样下结构复现的比例（复用 P06 的判据）             :func:`stability`
``N`` 新颖性   相对**指定知识库版本** :math:`K^{(v)}` 的非重复程度   :func:`novelty`
``C`` 复杂度   公式长度近似（表达式节点数 + 可拟合参数个数）         :func:`complexity`
============  ==================================================  ================

三条验收标准与实现的对应关系
------------------------------

① **新颖性仅相对指定知识库版本** :math:`K^{(v)}` **定义并记录该版本号**

   新颖性没有「绝对」口径，只有「相对某个知识库快照」的口径（框架说明 §3：
   「新颖性只相对于指定知识库版本 :math:`K^{(v)}` 定义」）。因此本模块：

   - :class:`KnowledgeBase` **没有无版本构造路径**——``version`` 是必填且非空；
   - :func:`novelty` 的返回值里恒带 ``knowledge_version`` 字段；
   - :func:`evaluate_hypothesis` 产出的 :class:`Evaluation` 恒带
     ``knowledge_version``，且**另记** ``hypothesis_knowledge_version``
     （假说自己声明的、生成时所依据的版本，来自 ``provenance``）与
     ``knowledge_version_matches`` 标志。两者不一致时不报错、不静默混用，
     而是**如实记录为不一致**——这正是不把「相对旧版本新颖」误当成
     「相对当前版本新颖」的唯一可靠手段。

② **``C`` 明确标注为近似值而非严格 MDL**

   框架说明 §4 指出「MVP 可以先用公式长度和开发误差近似排序，但不能将该近似
   宣称为严格 MDL」。因此 :func:`complexity` 的返回值里带两个**机检标志**：
   ``is_formula_length_approximation=True`` 与 ``is_strict_mdl=False``，
   并附带 :data:`COMPLEXITY_NOTE` 原文，使该口径无法被下游悄悄改写。

③ **不在指标层做加权求和**

   本模块**没有任何**加权、总分、排序或筛选入口：不暴露 ``weights`` 参数
   （:func:`evaluate_hypothesis` 收到任何未预期的关键字参数即报错，
   锚定的报错信息会点明「加权属 P10 决策」），不定义 Pareto 优化方向常量
   （那是 P10 的验收条目），:class:`Evaluation` 也不含 total / score 字段。
   :meth:`Evaluation.dimension_names` 只**列出**四个维度的名字，供下游迭代，
   本身不做任何聚合。

设计边界（严格遵守 P09 增量卡）
----------------------------------

- **不做 Pareto 排序与候选池管理**（属 P10）：本模块没有 ``pareto_front``、
  ``top_k_reserved``、``select_freeze_candidates``，也不定义「最大化 G/S/N、
  最小化 C」的方向常量——把方向写死在指标层，等于把 P10 的决策提前替它做了。
- **不做确证检验**：不计算 p 值、不构造置信区间、不做错误率控制、不授予证据
  等级。``gain`` 是**开发期**的探索量，:data:`GAIN_NOTE` 随结果一起输出。
- **不修改 ``sdl_m01/``**：本模块不导入、不调用 M1 的任何接口，不读数据集、
  不碰令牌，不接触 V/C1 分区。上游的 ``RelationFit`` 与 ``StabilityReport``
  已由 P04/P06 保证只在探索分区 E 上产生，本模块**原样采信其口径并转录**，
  不重新访问数据。
- **不改写假说对象**：:class:`~sdl_m04.hypothesis.Hypothesis` 是不可变的，
  本模块只**读取**它，不填充其 ``development`` / ``complexity`` 槽位；
  要把评估结果写回假说，须由调用方走 P07 的 ``new_version`` 派生新版本。
- **不推断语义等价**：新颖性的「等价」判定只建立在 P02 规范化 AST 的
  结构比较与陈述文本的词元重合之上，属**机检下限**，不是语义理解。
  近义表达与已知推论可能漏判，见「已知限制」。

上游依赖
--------

- P02 ``sdl_m02.expressions``：``parse`` / ``to_canonical_json`` / ``depth`` /
  ``node_count``——新颖性的等价判定与复杂度的节点计数都复用其规范化结果，
  以保证「同一语义只有一种写法」这一前提在本层继续成立。
- P04 ``sdl_m03.equations``：``FitResult`` / ``RelationFit`` /
  ``formula_length`` / ``COMPLEXITY_NOTE``——``G`` 直接从其同数据对照里读取，
  ``C`` 沿用其长度近似口径（两处口径必须一致，否则 G/S/N/C 之间不可比）。
- P06 ``sdl_m03.stability``：``stability_score`` / ``StabilityReport``——
  ``S`` 只做口径转录与充分性透传，重采样本身不在本层重做。
- P08 ``sdl_m04.generate``：``GeneratedHypothesisPool``——池内假说可整批送进
  :func:`evaluate_hypothesis`；``knowledge_version`` 由 P08 必填生成，
  本模块据此做版本一致性核对。

已知限制
--------

1. **新颖性是「相对知识库」而非「相对世界」。** 知识库中尚无匹配项，**不等于**
   世界范围内首次发现（框架说明 §3）。本模块在 :data:`NOVELTY_NOTE` 中原样
   保留该声明，并随每个结果输出。
2. **等价判定是机检下限。** ``equivalent_expression`` 只认规范化 AST 逐节点
   相等，``equivalent_skeleton`` 只认常数位被抹平后的结构相等，
   ``known_corollary`` 只认「条目公式是假说公式的真子树」。换元、
   恒等式变形（如 ``X1/X2`` 与 ``X1·X2^-1`` 之外的形式）、量纲一致的
   重参数化都可能漏判为新颖。
3. **陈述相似度是词元 Jaccard，不是语义相似度。** 中文按单字与相邻二元组
   切分、西文按字母数字串切分，仅作为结构匹配失败后的兜底：其取值被压到
   :data:`STATEMENT_SIMILARITY_CAP` 以下，且低于
   :data:`DEFAULT_MIN_STATEMENT_SIMILARITY` 时直接判为无匹配，
   因此它永远不会顶替结构层的判据，也不会因共享功能字而误报相似。
4. **``G`` 依赖上游数据对照的前提。** 当基线损失为 0 时相对下降无定义：
   候选未更优记 0.0，候选更优记 ``inf``。该退化情形被原样记录
   （``relative_reduction_defined=False``），不静默改写为某个有限值。
5. **``C`` 不是严格 MDL。** 严格 MDL 要求一致的编码方式、噪声模型与数据表示，
   本层的长度近似不满足该前提；它只用于同一批候选之间的相对比较。
6. **``S`` 的充分性由 P06 判定。** 当 ``sufficient=False`` 时本模块仍透传
   一个 ``[0, 1]`` 的数值，但会同时置 ``stability_sufficient=False`` 并
   附带原因，**不建议**下游在此基础上比较候选。
7. **四个指标之间量纲不同，且本层不处理。** 若下游改用加权评分，须先按
   框架说明 §4 处理量纲并在**探索阶段固定权重**；这一决策不由本层作出。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

from sdl_m02.expressions import (
    ExpressionError,
    depth as expression_depth,
    parse as parse_expression,
    to_canonical_json,
)

from sdl_m03.equations import (
    COMPLEXITY_NOTE,
    FitResult,
    RelationFit,
    formula_length,
    render_expression,
)

from sdl_m03.stability import (
    StabilityError,
    StabilityReport,
    stability_score,
)

from sdl_m04.hypothesis import Hypothesis

__all__ = [
    # 异常
    "MetricsError",
    "MetricsInputError",
    "MetricsPolicyError",
    "MetricsVersionError",
    # 常量
    "METRICS_VERSION",
    "DIMENSION_NAMES",
    "GAIN_BASES",
    "GAIN_LOSSES",
    "DEFAULT_GAIN_BASIS",
    "DEFAULT_GAIN_LOSS",
    "DEFAULT_BASELINE_CHOICE",
    "NOVELTY_MATCH_KINDS",
    "MATCH_WEIGHTS",
    "STATEMENT_SIMILARITY_CAP",
    "DEFAULT_MIN_STATEMENT_SIMILARITY",
    "NOVELTY_NOTE",
    "GAIN_NOTE",
    "STABILITY_NOTE",
    "COMPLEXITY_NOTE",
    "MDL_FLAGS",
    "FORBIDDEN_KEYS",
    "EVALUATION_MODULE_VERSION",
    # 知识库
    "KnowledgeEntry",
    "KnowledgeBase",
    # 指标
    "gain",
    "stability",
    "novelty",
    "complexity",
    # 装配
    "Evaluation",
    "evaluate_hypothesis",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 交付物版本号，写入序列化结果，便于追溯指标口径的变更。
METRICS_VERSION = "P09-v1.0"

#: 四个维度的固定顺序。**只用于迭代与展示，不代表优先级、权重或排序。**
DIMENSION_NAMES: tuple[str, ...] = ("gain", "stability", "novelty", "complexity")

#: ``gain`` 的度量基准：相对下降 / 绝对损失差。
GAIN_BASES: tuple[str, ...] = ("relative", "absolute")
DEFAULT_GAIN_BASIS = "relative"

#: 可用于增益对照的损失名（均属「越低越好」）。
GAIN_LOSSES: tuple[str, ...] = ("mse", "sse", "rmse", "mae")
DEFAULT_GAIN_LOSS = "mse"

#: 基线选择口径：``hardest`` 表示取最难超越的基线（损失最小者）。
DEFAULT_BASELINE_CHOICE = "hardest"

#: 新颖性匹配的种类，按**由强到弱**排列。
NOVELTY_MATCH_KINDS: tuple[str, ...] = (
    "equivalent_expression",
    "equivalent_skeleton",
    "known_corollary",
    "statement_similarity",
    "none",
)

#: 各匹配种类对应的相似度取值。
#:
#: - ``equivalent_expression``：规范化 AST 逐节点相等 → 相似度 1.0，新颖性 0。
#:   这正是框架说明 §3 要求的「等价公式应去重」。
#: - ``equivalent_skeleton``：常数位被抹平后结构相等 → 0.85。
#:   对应「同一函数形式、仅常数不同」的参数微变体。
#: - ``known_corollary``：条目公式是假说公式的真子树 → 0.7。对应「已知推论」。
#: - ``statement_similarity``：陈述文本词元 Jaccard，**上限被压到 0.6**，
#:   保证它不可能顶替任何结构层判据。
MATCH_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "equivalent_expression": 1.0,
        "equivalent_skeleton": 0.85,
        "known_corollary": 0.7,
        "none": 0.0,
    }
)

#: 陈述相似度的上限（见 :data:`MATCH_WEIGHTS` 说明）。
STATEMENT_SIMILARITY_CAP = 0.6

#: 陈述相似度的**下限**：原始 Jaccard 低于此值时不判为匹配，直接归入 ``none``。
#:
#: 必要性：中文陈述里「的」「与」「为」这类高频单字几乎出现在任何句子中，
#: 若不设下限，两段毫不相干的陈述也会因共享一两个功能字而得到非零相似度，
#: 从而把「无匹配」误报成 ``statement_similarity``。该阈值只作用于兜底判据，
#: 不影响任何结构层判据。
DEFAULT_MIN_STATEMENT_SIMILARITY = 0.12

#: 新颖性口径说明，随每个结果一起输出。
NOVELTY_NOTE = (
    "新颖性只相对**指定知识库版本** K^(v) 定义：知识库中尚无匹配项，"
    "不等于世界范围内首次发现；近义表达、等价公式与已知推论均属已知，"
    "应被去重。本层的等价判定是规范化 AST 结构比较（机检下限），"
    "不是语义理解。"
)

#: 增益口径说明：开发期量，不是确证结论。
GAIN_NOTE = (
    "预测增益是**开发期**的探索量，只在探索分区 E 上计算，"
    "不构成确证检验、不计算 p 值、不授予证据等级。"
    "候选与基线必须建立在同一份数据上（由 P04 的同数据核验保证）。"
)

#: 稳定性口径说明。
STABILITY_NOTE = (
    "稳定性分数是重采样下结构复现的比例，落在 [0, 1]；"
    "它既不是错误率控制，也不构成证据等级，更不能替代独立确证。"
    "充分性由 P06 判定并经本层原样透传。"
)

#: 复杂度标注用的两个机检标志。**不可被下游改写**（它们刻意放在返回值里）。
MDL_FLAGS: Mapping[str, bool] = MappingProxyType(
    {"is_formula_length_approximation": True, "is_strict_mdl": False}
)

#: 禁止出现在知识库条目与评估记录里的键名（小写比较）。
#: 依据 ``INTERFACES.md`` §4.4：令牌正文不得写入日志、提示词或版本库。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
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

#: 明确拒绝的关键字参数：它们是「在指标层做加权/聚合」的入口。
_FORBIDDEN_KWARGS: frozenset[str] = frozenset(
    {
        "weights",
        "score_weights",
        "weighted_sum",
        "total_score",
        "aggregate_score",
        "combine",
        "combine_scores",
        "pareto",
        "pareto_front",
        "top_k",
        "top_k_reserved",
        "select",
        "select_freeze_candidates",
    }
)

#: 评估记录的模块版本（与 :data:`METRICS_VERSION` 同源，单列便于序列化核对）。
EVALUATION_MODULE_VERSION = METRICS_VERSION

#: 浮点比较容差。
_EPS = 1e-12

#: 数值统一保留位数，保证同一输入恒得逐字节相同的序列化结果。
_ROUND_DIGITS = 12

#: 西文词元：以字母开头的字母数字串。
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

#: 中日韩表意字符（按单字与相邻二元组切分，无需第三方分词器）。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: 识别参考引用用途的前缀（``E`` / ``V`` / ``C1`` …）。本模块只做**拒绝**用。
_RESTRICTED_REF_RE = re.compile(r"^\s*([A-Za-z_]+)")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class MetricsError(ValueError):
    """指标层的基类异常。"""


class MetricsInputError(MetricsError):
    """输入形态非法：类型不符、缺少必需证据、键名未知等。"""


class MetricsPolicyError(MetricsError):
    """口径越界：试图在指标层做加权/聚合/筛选，或引用受限分区。"""


class MetricsVersionError(MetricsInputError):
    """知识库版本缺失或不合法。

    单列一个子类，是因为验收标准 ① 把「版本绑定」单独提出，
    调用方与测试需要能把它从一般的输入错误中区分出来。
    """


# ---------------------------------------------------------------------------
# 通用校验工具
# ---------------------------------------------------------------------------


def _round(value: float, digits: int = _ROUND_DIGITS) -> float:
    """四舍五入到固定位数，使序列化结果逐字节稳定。"""
    return round(float(value), digits)


def _is_finite_number(value: Any) -> bool:
    """判定是否为有限实数（``bool`` 不算，避免 ``True`` 被当作 1.0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _require_nonempty_str(value: Any, label: str) -> str:
    """要求非空字符串（去空白后非空）。"""
    if not isinstance(value, str):
        raise MetricsInputError(
            f"{label} 必须是字符串，实际得到：{type(value).__name__}"
        )
    if not value.strip():
        raise MetricsInputError(f"{label} 不能是空白字符串")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """要求映射。"""
    if not isinstance(value, Mapping):
        raise MetricsInputError(
            f"{label} 必须是映射（dict），实际得到：{type(value).__name__}"
        )
    return value


def _reject_forbidden_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝令牌类键名，避免敏感正文被写入知识库或评估记录。"""
    for key in mapping:
        if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
            raise MetricsPolicyError(
                f"{label} 不允许包含令牌类字段 {key!r}；"
                "角色令牌正文不得写入任何记录（INTERFACES.md §4.4）"
            )


def _reject_aggregation_attempts(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝任何加权 / 聚合 / 筛选入口。

    验收标准 ③：本层不做加权求和。把「加权属 P10 决策」写进**报错信息**，
    使越界尝试在调用点就被明确告知，而不是被静默忽略。
    """
    for key in sorted(kwargs):
        lowered = key.strip().lower()
        if lowered in _FORBIDDEN_KWARGS or "weight" in lowered:
            raise MetricsPolicyError(
                f"{where} 不接受参数 {key!r}：本层只计算 G/S/N/C 四个彼此独立的"
                "指标，不做加权、不做总分、不做筛选；"
                "加权与 Pareto 排序属 P10 的决策，须在探索阶段固定权重后再做。"
            )
    if kwargs:  # pragma: no cover - 防御性兜底
        raise MetricsInputError(
            f"{where} 收到未预期的参数：" + ", ".join(repr(k) for k in sorted(kwargs))
        )


def _reject_restricted_ref(ref: Any, label: str) -> str:
    """拒绝非探索用途的数据引用。

    本模块不亲自读数据，因此这里是一道**廉价的前置防线**：若调用方把
    ``V`` / ``C1`` / ``Q`` 一类引用当作证据传进来，就地报错，
    避免「确证数据被当作开发期指标输入」这类难以察觉的口径污染。
    """
    text = _require_nonempty_str(ref, label)
    match = _RESTRICTED_REF_RE.match(text)
    purpose = match.group(1).upper() if match else ""
    if purpose in ("V", "C", "Q", "H") or purpose.startswith("C"):
        raise MetricsPolicyError(
            f"{label}={text!r} 指向非探索用途的分区（{purpose}）："
            "P09 的指标只在探索分区 E 上定义，不得引入开发评估或确证数据。"
        )
    return text


def _jsonable(value: Any) -> Any:
    """把内部结构递归还原为可 JSON 序列化的普通结构。"""
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    """规范式 JSON 文本（键排序、紧凑分隔符），用于稳定比对与摘要。"""
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


# ---------------------------------------------------------------------------
# 表达式工具：规范化、骨架化、子树枚举
# ---------------------------------------------------------------------------


def _parse_ast(text: Any) -> tuple | None:
    """尽力把文本解析为规范化 AST；不可解析时返回 ``None``。

    刻意**不抛异常**：假说对象里的 ``model.expression`` 只保证是字符串，
    不保证一定能被 P02 的受限语法接受（可能来自外部 LLM 的文本填充）。
    遇到不可解析的表达式时，新颖性退化为陈述文本比较，并在结果里记录原因，
    而不是让整个评估失败。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return parse_expression(text)
    except ExpressionError:
        return None
    except Exception:  # pragma: no cover - 防御性兜底：任何解析期异常都不致命
        return None


def _quantize_ast(ast: Any) -> Any:
    """把 AST 中的**常数位抹平**，得到结构骨架。

    用于识别「同一函数形式、仅常数不同」的参数微变体：``X1 + 2``、``X1 + 5``
    与 ``X1 - 3`` 的骨架相同，应被判为同一结构而非三个新颖结构。
    """
    if not isinstance(ast, tuple):
        return ast
    if ast and ast[0] == "num":
        return ("num", "<常数>")
    return tuple(
        _quantize_ast(child) if isinstance(child, tuple) else child for child in ast
    )


def _iter_subtrees(ast: Any) -> Iterator[tuple]:
    """枚举 AST 的全部子树（含自身）。"""
    if not isinstance(ast, tuple) or not ast:
        return
    yield ast
    for child in ast[1:]:
        yield from _iter_subtrees(child)


def _subtree_canonicals(ast: Any) -> frozenset[str]:
    """AST 全部子树的规范式 JSON 集合。"""
    out: set[str] = set()
    for node in _iter_subtrees(ast):
        try:
            out.add(to_canonical_json(node))
        except Exception:  # pragma: no cover - 防御性兜底
            continue
    return frozenset(out)


def _canonical_of_ast(ast: Any) -> str | None:
    """规范式 JSON；不可用时返回 ``None``。"""
    if not isinstance(ast, tuple):
        return None
    try:
        return to_canonical_json(ast)
    except Exception:  # pragma: no cover - 防御性兜底
        return None


# ---------------------------------------------------------------------------
# 陈述文本词元化
# ---------------------------------------------------------------------------


def _statement_tokens(text: Any) -> frozenset[str]:
    """把陈述文本切成词元集合。

    切分口径（无需第三方分词器）：

    - 西文：以字母开头的字母数字串，统一小写；
    - 中日韩：单字，加上相邻二元组（提升「比例变量」与「比例关系」
      这类共享语素的识别率）。

    词元集合用于 Jaccard 相似度，仅作结构匹配失败后的**兜底**。
    """
    if not isinstance(text, str) or not text:
        return frozenset()
    tokens = {match.group(0).lower() for match in _LATIN_TOKEN_RE.finditer(text)}
    cjk = _CJK_RE.findall(text)
    tokens.update(cjk)
    for index in range(len(cjk) - 1):
        tokens.add(cjk[index] + cjk[index + 1])
    return frozenset(tokens)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """词元集合的 Jaccard 相似度；任一侧为空时返回 0.0。"""
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeEntry:
    """知识库中的一条已知项。

    :param entry_id: 稳定标识，必填且非空。
    :param statement: 该已知项的文字陈述，必填且非空。
    :param kind: 种类标签，自由字符串（例如 ``relation`` / ``invariant``）；
        本模块只做非空校验，不判断标签是否恰当。
    :param expression: 该已知项的公式（字符串或 P02 的 AST 元组）。
        可为 ``None``——文字型已知项（例如领域约束、已发表的定性结论）
        没有公式，此时只能经陈述文本参与匹配。
    :param scope: 适用范围声明，可为空映射（表示未限定）。
    :param provenance: 来源信息，可为空映射；**不得**出现令牌类键名。
    """

    entry_id: str
    statement: str
    kind: str = "unknown"
    expression: Any = None
    scope: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.entry_id, "KnowledgeEntry.entry_id")
        _require_nonempty_str(self.statement, "KnowledgeEntry.statement")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise MetricsInputError("KnowledgeEntry.kind 必须是非空字符串")
        _require_mapping(self.scope, "KnowledgeEntry.scope")
        _reject_forbidden_keys(self.scope, "KnowledgeEntry.scope")
        _reject_forbidden_keys(self.provenance, "KnowledgeEntry.provenance")

        # 规范化表达式：AST 或文本都接受，统一落成规范式 JSON。
        ast: tuple | None = None
        raw = self.expression
        if isinstance(raw, tuple):
            ast = raw
        elif isinstance(raw, str):
            ast = _parse_ast(raw)

        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        object.__setattr__(self, "_ast", ast)
        object.__setattr__(self, "_canonical", _canonical_of_ast(ast))
        object.__setattr__(
            self, "_subtrees", _subtree_canonicals(ast) if ast is not None else frozenset()
        )
        object.__setattr__(self, "_tokens", _statement_tokens(self.statement))

    # -- 派生视图 ---------------------------------------------------------

    @property
    def ast(self) -> tuple | None:
        """规范化 AST；无公式或不可解析时为 ``None``。"""
        return self._ast  # type: ignore[attr-defined]

    @property
    def canonical_expression(self) -> str | None:
        """规范式 JSON 文本；无公式或不可解析时为 ``None``。"""
        return self._canonical  # type: ignore[attr-defined]

    @property
    def expression_parsed(self) -> bool:
        """是否携带**可解析**的公式。"""
        return self._ast is not None  # type: ignore[attr-defined]

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "statement": self.statement,
            "expression": (
                render_expression(self._ast)
                if self._ast is not None
                else (self.expression if isinstance(self.expression, str) else None)
            ),
            "canonical_expression": self._canonical,
            "expression_parsed": self.expression_parsed,
            "scope": _jsonable(self.scope),
            "provenance": _jsonable(self.provenance),
        }


@dataclass(frozen=True)
class KnowledgeBase:
    """某一版本的知识库快照，新颖性**只能**相对它定义。

    ``version`` 是必填且非空的——本类**没有无版本构造路径**。
    这是验收标准 ① 的第一道保障：新颖性若离开了明确的
    :math:`K^{(v)}`，其数值就没有可复核的含义（框架说明 §3）。

    :param version: 知识库版本号 :math:`K^{(v)}`，必填且非空。
    :param entries: 已知项序列；**允许为空**（新任务起步时知识库确实是空的），
        但空库会在 :func:`novelty` 的结果里被显式标注 ``knowledge_base_empty``，
        以免「没有可比的已知项」被误读为「已被验证为全新」。
    :param note: 版本说明（例如「截至 2026-09 的领域已知公式集」）。
    """

    version: str
    entries: tuple[KnowledgeEntry, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise MetricsVersionError(
                "KnowledgeBase.version 是必填字段且不能为空："
                "新颖性必须绑定到明确的知识库版本 K^(v)，"
                "缺少版本号的「新颖性」没有可复核的含义"
            )
        if not isinstance(self.note, str):
            raise MetricsInputError(
                f"KnowledgeBase.note 必须是字符串，实际得到：{type(self.note).__name__}"
            )
        items = tuple(self.entries)
        for index, item in enumerate(items):
            if not isinstance(item, KnowledgeEntry):
                raise MetricsInputError(
                    f"KnowledgeBase.entries[{index}] 必须是 KnowledgeEntry，"
                    f"实际得到：{type(item).__name__}"
                )
        identifiers = [item.entry_id for item in items]
        duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
        if duplicates:
            raise MetricsInputError(
                "KnowledgeBase 中存在重复的 entry_id：" + ", ".join(repr(d) for d in duplicates)
            )
        object.__setattr__(self, "entries", items)

    # -- 容器协议 ---------------------------------------------------------

    def __len__(self) -> int:
        """已知项条数。"""
        return len(self.entries)

    def __iter__(self) -> Iterator[KnowledgeEntry]:
        """按 ``entry_id`` 升序迭代，顺序完全确定。"""
        return iter(sorted(self.entries, key=lambda item: item.entry_id))

    def __contains__(self, entry_id: object) -> bool:
        return isinstance(entry_id, str) and any(
            item.entry_id == entry_id for item in self.entries
        )

    @property
    def is_empty(self) -> bool:
        """是否为空库。"""
        return not self.entries

    def get(self, entry_id: str) -> KnowledgeEntry:
        """按标识取已知项。

        :raises MetricsInputError: 标识不存在。
        """
        for item in self.entries:
            if item.entry_id == entry_id:
                return item
        raise MetricsInputError(f"知识库中不存在已知项：{entry_id!r}")

    def version_label(self) -> str:
        """版本标签，用于结果记录。"""
        return f"K^{{{self.version}}}"

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（条目按 ``entry_id`` 升序）。"""
        return {
            "metrics_version": METRICS_VERSION,
            "version": self.version,
            "version_label": self.version_label(),
            "size": len(self.entries),
            "note": self.note,
            "entries": [item.to_dict() for item in self],
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与指纹。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """知识库内容摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 新颖性匹配
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Match:
    """一条假说相对知识库的匹配结果（内部结构）。"""

    entry_id: str | None
    match_kind: str
    similarity: float
    detail: Mapping[str, Any]


def _compare_to_entry(
    *,
    canonical: str | None,
    skeleton: str | None,
    subtrees: frozenset[str],
    tokens: frozenset[str],
    entry: KnowledgeEntry,
) -> _Match:
    """把一条假说与一条知识库已知项比较，返回最强匹配。

    判据由强到弱（见 :data:`MATCH_WEIGHTS`）：

    1. ``equivalent_expression``——规范化 AST 逐节点相等；
    2. ``equivalent_skeleton``——常数位抹平后结构相等；
    3. ``known_corollary``——已知项的公式是假说公式的**真子树**
       （真子树即不含自身，避免与第 1 条重复计数）；
    4. ``statement_similarity``——陈述词元 Jaccard，上限受
       :data:`STATEMENT_SIMILARITY_CAP` 压制。
    """
    entry_canonical = entry.canonical_expression
    entry_ast = entry.ast

    # -- 判据 1：等价表达式 ---------------------------------------------
    if canonical is not None and entry_canonical is not None and canonical == entry_canonical:
        return _Match(
            entry.entry_id,
            "equivalent_expression",
            MATCH_WEIGHTS["equivalent_expression"],
            {"reason": "规范化 AST 逐节点相等（等价公式应去重）"},
        )

    # -- 判据 2：等价骨架（仅常数不同） --------------------------------
    if entry_ast is not None:
        entry_skeleton = _canonical_of_ast(_quantize_ast(entry_ast))
    else:
        entry_skeleton = None
    if (
        skeleton is not None
        and entry_skeleton is not None
        and skeleton == entry_skeleton
    ):
        return _Match(
            entry.entry_id,
            "equivalent_skeleton",
            MATCH_WEIGHTS["equivalent_skeleton"],
            {"reason": "常数位抹平后结构相等（同一函数形式的参数微变体）"},
        )

    # -- 判据 3：已知推论（条目公式为假说公式的真子树） -----------------
    if entry_canonical is not None and canonical is not None:
        if entry_canonical in subtrees and entry_canonical != canonical:
            return _Match(
                entry.entry_id,
                "known_corollary",
                MATCH_WEIGHTS["known_corollary"],
                {"reason": "已知项的公式是假说公式的真子树（属已知推论）"},
            )

    # -- 判据 4：陈述词元相似度（兜底，上限受压制、下限过滤） -------------
    similarity = _jaccard(tokens, entry._tokens)  # type: ignore[attr-defined]
    if similarity >= DEFAULT_MIN_STATEMENT_SIMILARITY:
        capped = min(similarity, STATEMENT_SIMILARITY_CAP)
        return _Match(
            entry.entry_id,
            "statement_similarity",
            capped,
            {
                "reason": (
                    "陈述文本词元重合（兜底判据，区间 [%s, %s]）"
                    % (DEFAULT_MIN_STATEMENT_SIMILARITY, STATEMENT_SIMILARITY_CAP)
                ),
                "raw_jaccard": _round(similarity),
            },
        )

    return _Match(None, "none", MATCH_WEIGHTS["none"], {"reason": "无任何匹配"})


def _novelty_of_ast(
    ast: tuple | None,
    tokens: frozenset[str],
    knowledge_base: KnowledgeBase,
) -> _Match:
    """在知识库中找出与给定假说最强的一条匹配。"""
    canonical = _canonical_of_ast(ast)
    skeleton = _canonical_of_ast(_quantize_ast(ast)) if ast is not None else None
    subtrees = _subtree_canonicals(ast) if ast is not None else frozenset()

    best: _Match | None = None
    for entry in knowledge_base:
        current = _compare_to_entry(
            canonical=canonical,
            skeleton=skeleton,
            subtrees=subtrees,
            tokens=tokens,
            entry=entry,
        )
        if best is None or current.similarity > best.similarity:
            best = current
        elif best is not None and current.similarity == best.similarity:
            # 同分时任取 ``entry_id`` 较小者，保证结果与迭代顺序无关（确定性）。
            if current.entry_id is not None and (
                best.entry_id is None or current.entry_id < best.entry_id
            ):
                best = current
    if best is None:  # 空知识库
        return _Match(None, "none", MATCH_WEIGHTS["none"], {"reason": "知识库为空，无可比项"})
    return best


# ---------------------------------------------------------------------------
# 指标一：预测增益 G
# ---------------------------------------------------------------------------


def _gain_from_losses(
    *,
    candidate_loss: float,
    baseline_loss: float,
    loss: str,
    basis: str,
    floor_at_zero: bool,
) -> dict:
    """由两个损失值计算增益，并记录退化情形。"""
    difference = baseline_loss - candidate_loss
    defined = abs(baseline_loss) > _EPS
    if defined:
        relative = difference / baseline_loss
    else:
        # 基线损失为 0：相对下降无定义。候选未更优记 0.0，
        # 候选更优记 inf（「无穷倍改善」），并置 defined=False 提醒复核。
        relative = 0.0 if difference <= 0.0 else float("inf")

    value = relative if basis == "relative" else difference
    floored = False
    if floor_at_zero and math.isfinite(value) and value < 0.0:
        value = 0.0
        floored = True

    return {
        "gain": _round(value) if math.isfinite(value) else value,
        "basis": basis,
        "loss": loss,
        "candidate_loss": _round(candidate_loss),
        "baseline_loss": _round(baseline_loss),
        "loss_difference": _round(difference),
        "relative_reduction": _round(relative) if math.isfinite(relative) else relative,
        "relative_reduction_defined": defined,
        "candidate_better": difference > 0.0,
        "floor_at_zero": bool(floor_at_zero),
        "floored_at_zero": floored,
    }


def _select_baseline(comparison: Mapping[str, Any], baseline_choice: str) -> dict:
    """从 P04 的同数据对照结果里选出用于增益的基线。

    :param baseline_choice: ``hardest``（默认，取最难超越的基线）或某类基线名
        （``constant`` / ``linear``）；也可显式传入 ``best_only`` 不接受。
    :raises MetricsInputError: 对照结果缺少所需信息。
    """
    if baseline_choice == DEFAULT_BASELINE_CHOICE:
        hardest = comparison.get("hardest_baseline")
        if not isinstance(hardest, Mapping):
            raise MetricsInputError(
                "对照结果缺少 hardest_baseline：无法确定用于增益的基线。"
                "请改用 baseline_choice 显式指定基线种类。"
            )
        return dict(hardest)

    blocks = comparison.get("baselines")
    if not isinstance(blocks, Mapping) or baseline_choice not in blocks:
        raise MetricsInputError(
            f"对照结果中不存在基线种类 {baseline_choice!r}；"
            f"可用种类：{', '.join(sorted(blocks)) if isinstance(blocks, Mapping) else '无'}"
            f"；或使用 {DEFAULT_BASELINE_CHOICE!r} 表示最难超越的基线"
        )
    block = blocks[baseline_choice]
    if isinstance(block, Mapping) and isinstance(block.get("best"), Mapping):
        return dict(block["best"])
    if isinstance(block, Mapping) and isinstance(block.get("single"), Mapping):
        return dict(block["single"])
    raise MetricsInputError(
        f"基线种类 {baseline_choice!r} 的对照块形状无法识别（缺少 best/single）"
    )


def gain(
    evidence: Any,
    *,
    loss: str = DEFAULT_GAIN_LOSS,
    basis: str = DEFAULT_GAIN_BASIS,
    baseline_choice: str = DEFAULT_BASELINE_CHOICE,
    floor_at_zero: bool = False,
) -> dict:
    """计算预测增益 ``G``。

    接受三种证据形态，均为 P04 的产物或与之等价的「同数据」对照：

    - :class:`~sdl_m03.equations.RelationFit`：直接读取其 ``comparison``。
      此时候选与基线同数据这一前提**已由 P04 核验**，本层不再重复；
    - ``(candidate, baseline)`` 二元组，两项都是
      :class:`~sdl_m03.equations.FitResult`。本层会**再次核验**
      ``data_fingerprint`` 与 ``n_used`` 一致，不一致就地报错；
    - 映射，需含 ``candidate_loss`` 与 ``baseline_loss`` 两个有限实数。

    :param evidence: 证据（见上）。
    :param loss: 用于对照的损失名，取值见 :data:`GAIN_LOSSES`。
    :param basis: ``"relative"``（默认，相对下降率）或 ``"absolute"``（损失差）。
    :param baseline_choice: ``"hardest"``（默认）或基线种类名；
        仅在使用 :class:`RelationFit` 时生效。
    :param floor_at_zero: 是否把负增益截断为 0。**默认关闭**——候选劣于基线是
        有信息量的结果，截断会把它伪装成「无功无过」。截断时会在结果里记录
        ``floored_at_zero=True``。
    :return: 含 ``gain`` 及完整对照明细的字典。
    :raises MetricsInputError: 证据形态非法、数据不一致或损失名不可用。
    :raises MetricsPolicyError: 引用了受限分区，或试图传入加权参数。
    """
    if loss not in GAIN_LOSSES:
        raise MetricsInputError(
            f"损失名 {loss!r} 不在可用集合 {', '.join(GAIN_LOSSES)} 内；"
            "本模块只接受「越低越好」的损失"
        )
    if basis not in GAIN_BASES:
        raise MetricsInputError(
            f"basis 必须是 {', '.join(GAIN_BASES)} 之一，实际得到：{basis!r}"
        )
    if not isinstance(floor_at_zero, bool):
        raise MetricsInputError(
            f"floor_at_zero 必须是布尔值，实际得到：{floor_at_zero!r}"
        )

    # -- 形态 A：P04 的 RelationFit --------------------------------------
    if isinstance(evidence, RelationFit):
        comparison = evidence.comparison
        selected = _select_baseline(comparison, baseline_choice)
        for key in ("baseline_mse", "candidate_mse"):
            if key not in selected:
                raise MetricsInputError(
                    f"基线对照块缺少 {key!r}，无法计算增益："
                    f"实际键为 {', '.join(sorted(selected))}"
                )
        # 该块由 P04 产出，损失固定为 mse；若调用方要别的损失，需改用形态 B/C。
        if loss != "mse":
            raise MetricsInputError(
                "RelationFit 的对照块只登记 mse；要按其它损失计算增益，"
                "请改用 (candidate, baseline) 形态的 FitResult 输入"
            )
        payload = _gain_from_losses(
            candidate_loss=float(selected["candidate_mse"]),
            baseline_loss=float(selected["baseline_mse"]),
            loss=loss,
            basis=basis,
            floor_at_zero=floor_at_zero,
        )
        payload["baseline_choice"] = baseline_choice
        payload["baseline_kind"] = selected.get("kind")
        payload["baseline_name"] = selected.get("name")
        payload["data_fingerprint"] = selected.get("data_fingerprint")
        payload["same_data_verified_by"] = "P04（relation fit 的同数据核验）"
        payload["note"] = GAIN_NOTE
        return payload

    # -- 形态 B：(candidate, baseline) 两个 FitResult ---------------------
    if (
        isinstance(evidence, Sequence)
        and not isinstance(evidence, (str, bytes, Mapping))
        and len(evidence) == 2
        and all(isinstance(item, FitResult) for item in evidence)
    ):
        candidate, baseline = evidence  # type: ignore[misc]
        if candidate.data_fingerprint != baseline.data_fingerprint:
            raise MetricsInputError(
                "候选与基线必须建立在同一份数据上，实测数据指纹不一致："
                f"candidate={candidate.data_fingerprint} != baseline={baseline.data_fingerprint}"
            )
        if candidate.n_used != baseline.n_used:
            raise MetricsInputError(
                "候选与基线必须使用相同的行集，实测行数不一致："
                f"{candidate.n_used} != {baseline.n_used}"
            )
        for item, label in ((candidate, "candidate"), (baseline, "baseline")):
            if loss not in item.metrics:
                raise MetricsInputError(
                    f"{label} 的 metrics 中不存在损失 {loss!r}；"
                    f"可用键：{', '.join(sorted(item.metrics))}"
                )
        payload = _gain_from_losses(
            candidate_loss=float(candidate.metrics[loss]),
            baseline_loss=float(baseline.metrics[loss]),
            loss=loss,
            basis=basis,
            floor_at_zero=floor_at_zero,
        )
        payload["baseline_choice"] = "explicit"
        payload["baseline_kind"] = baseline.kind
        payload["baseline_name"] = baseline.name
        payload["candidate_name"] = candidate.name
        payload["data_fingerprint"] = candidate.data_fingerprint
        payload["n_used"] = candidate.n_used
        payload["same_data_verified_by"] = "P09（本层复核指纹与行数）"
        payload["note"] = GAIN_NOTE
        return payload

    # -- 形态 C：显式损失映射 --------------------------------------------
    if isinstance(evidence, Mapping):
        _reject_forbidden_keys(evidence, "gain 证据")
        missing = [key for key in ("candidate_loss", "baseline_loss") if key not in evidence]
        if missing:
            raise MetricsInputError(
                "gain 的映射证据缺少字段：" + ", ".join(repr(key) for key in missing)
            )
        candidate_loss = evidence["candidate_loss"]
        baseline_loss = evidence["baseline_loss"]
        for value, label in (
            (candidate_loss, "candidate_loss"),
            (baseline_loss, "baseline_loss"),
        ):
            if not _is_finite_number(value):
                raise MetricsInputError(
                    f"{label} 必须是有限实数，实际得到：{value!r}"
                )
        payload = _gain_from_losses(
            candidate_loss=float(candidate_loss),
            baseline_loss=float(baseline_loss),
            loss=loss,
            basis=basis,
            floor_at_zero=floor_at_zero,
        )
        payload["baseline_choice"] = "explicit"
        payload["baseline_kind"] = evidence.get("baseline_kind")
        payload["baseline_name"] = evidence.get("baseline_name")
        payload["data_fingerprint"] = evidence.get("data_fingerprint")
        payload["same_data_verified_by"] = "调用方声明（本层无法复核）"
        payload["note"] = GAIN_NOTE
        return payload

    raise MetricsInputError(
        "gain 的证据必须是 RelationFit、(candidate, baseline) 两个 FitResult，"
        f"或含 candidate_loss / baseline_loss 的映射；实际得到：{type(evidence).__name__}"
    )


# ---------------------------------------------------------------------------
# 指标二：稳定性 S
# ---------------------------------------------------------------------------


def stability(evidence: Any) -> dict:
    """把 P06 的重采样证据折算为稳定性 ``S``，并透传其充分性判定。

    本层**不重做重采样**，也不修改 P06 的判据——它只做口径转录，
    外加两条本层必需的提醒：分数不是错误率控制、不构成证据等级。

    :param evidence: :class:`~sdl_m03.stability.StabilityReport`、
        含计数字段的映射，或布尔序列（口径见 P06 的 ``stability_score``）。
    :return: 含 ``stability`` / ``sufficient`` / ``reasons`` 等字段的字典。
    :raises MetricsInputError: 证据形态非法。
    """
    try:
        score = stability_score(evidence)
    except StabilityError as error:
        raise MetricsInputError(f"稳定性证据不可用：{error}") from error

    payload: dict[str, Any] = {
        "stability": _round(score),
        "note": STABILITY_NOTE,
    }
    if isinstance(evidence, StabilityReport):
        provenance = evidence.provenance if isinstance(evidence.provenance, Mapping) else {}
        reference = provenance.get("source_ref") or provenance.get("sample_fingerprint")
        if isinstance(reference, str) and reference.strip():
            # 若稳定性报告登记了来源引用，就地核验它不指向受限分区。
            _reject_restricted_ref(reference, "稳定性报告来源引用")
        payload.update(
            {
                "subject_kind": evidence.subject_kind,
                "subject_id": evidence.subject_id,
                "split_unit": evidence.split_unit,
                "group_field": evidence.group_field,
                "n_resamples": evidence.n_resamples,
                "n_effective": evidence.n_effective,
                "n_reproduced": evidence.n_reproduced,
                "sufficient": bool(evidence.sufficient),
                "reasons": list(evidence.reasons),
                "seed": evidence.seed,
                "criteria": _jsonable(evidence.criteria),
                "source_ref": reference if isinstance(reference, str) else None,
            }
        )
    elif isinstance(evidence, Mapping):
        payload.update(
            {
                "sufficient": bool(evidence.get("sufficient", False)),
                "reasons": list(evidence.get("reasons", ()) or ()),
                "n_effective": evidence.get("n_effective"),
                "n_reproduced": evidence.get("n_reproduced"),
            }
        )
    else:
        payload.update({"sufficient": False, "reasons": ["证据未附带充分性判定"]})
    return payload


# ---------------------------------------------------------------------------
# 指标三：新颖性 N
# ---------------------------------------------------------------------------


def novelty(
    hypothesis: Any,
    knowledge_base: KnowledgeBase,
    *,
    expression: Any = None,
) -> dict:
    """计算相对 **指定知识库版本** :math:`K^{(v)}` 的新颖性 ``N``。

    新颖性定义为「1 − 与知识库中最相似已知项的相似度」，相似度判据见
    :data:`MATCH_WEIGHTS`。返回结果**恒带** ``knowledge_version``，
    且当假说的 ``provenance.knowledge_version`` 与该版本不一致时会显式标注，
    而不是静默按其中一个口径作答。

    :param hypothesis: :class:`~sdl_m04.hypothesis.Hypothesis` 或等价字典。
    :param knowledge_base: :class:`KnowledgeBase`，其 ``version`` 即
        :math:`K^{(v)}`。**必填**——没有版本就没有可复核的新颖性。
    :param expression: 覆盖假说中提取的公式（AST 元组或文本）。
        少数假说（如 ``cluster`` 型）的 ``model`` 里没有公式，
        调用方若另有公式来源可在此显式给出。
    :return: 含 ``novelty`` / ``knowledge_version`` / ``match_kind`` 等字段的字典。
    :raises MetricsVersionError: 知识库版本缺失或不合法。
    :raises MetricsInputError: 假说形态非法或版本不一致。
    """
    if not isinstance(knowledge_base, KnowledgeBase):
        raise MetricsInputError(
            "knowledge_base 必须是 KnowledgeBase 实例："
            "新颖性必须绑定到明确的知识库版本 K^(v)"
        )
    if not knowledge_base.version.strip():  # pragma: no cover - 构造期已拦截
        raise MetricsVersionError("知识库版本不能为空")

    fields = _hypothesis_fields(hypothesis)

    if expression is None:
        ast, text, source = _expression_of(fields)
    elif isinstance(expression, tuple):
        ast, text, source = expression, None, "调用方显式提供（AST）"
        try:
            text = render_expression(expression)
        except Exception:  # pragma: no cover - 渲染失败不致命
            text = None
    elif isinstance(expression, str):
        ast = _parse_ast(expression)
        text, source = expression, "调用方显式提供（文本）"
        if ast is None:
            raise MetricsInputError(
                f"显式提供的表达式 {expression!r} 无法被受限语法解析；"
                "请确认它只使用加、减、乘、除、幂与受支持的一元变换"
            )
    else:
        raise MetricsInputError(
            f"expression 必须是 AST 元组、字符串或 None，实际得到：{type(expression).__name__}"
        )

    tokens = _statement_tokens(fields.get("statement"))
    match = _novelty_of_ast(ast, tokens, knowledge_base)

    value = max(0.0, min(1.0, 1.0 - float(match.similarity)))
    payload = {
        "novelty": _round(value),
        "knowledge_version": knowledge_base.version,
        "knowledge_version_label": knowledge_base.version_label(),
        "knowledge_base_size": len(knowledge_base),
        "knowledge_base_digest": knowledge_base.content_digest(),
        "knowledge_base_empty": knowledge_base.is_empty,
        "match_kind": match.match_kind,
        "max_similarity": _round(float(match.similarity)),
        "nearest_entry_id": match.entry_id,
        "matched_entry_ids": [match.entry_id] if match.entry_id is not None else [],
        "match_detail": _jsonable(match.detail),
        "expression_used": text,
        "expression_parsed": ast is not None,
        "expression_source": source,
        "note": NOVELTY_NOTE,
    }
    if knowledge_base.is_empty:
        payload["caveat"] = (
            "知识库为空：novelty=1.0 只表示「暂无已知项可比」，"
            "不表示已确认在世界范围内首次发现。"
        )
    return payload


# ---------------------------------------------------------------------------
# 指标四：复杂度 C
# ---------------------------------------------------------------------------


def complexity(
    evidence: Any = None,
    *,
    parameter_count: Any = None,
    ast: Any = None,
) -> dict:
    """计算复杂度 ``C`` —— **公式长度近似**，并显式标注它不是严格 MDL。

    接受四类证据（按优先级）：

    1. P04 的 ``FitResult.complexity`` 映射（含 ``expression_nodes`` /
       ``rendered_length`` / ``parameter_count`` / ``total``）——**直接采信**，
       以保证与其他 P04 产物的口径一致；
    2. AST 元组或表达式文本——经 :func:`~sdl_m03.equations.formula_length`
       计算长度分量；
    3. :class:`~sdl_m04.hypothesis.Hypothesis`（或字典）——从 ``model.expression``
       取公式，``model.coefficients`` 或 ``fitted_parameters`` 数出参数个数；
    4. 缺省——无任何公式信息时退化为对 ``statement`` 的字符长度计数，
       并置 ``degraded=True`` 提醒该值**不可**与公式型候选直接比较。

    :param evidence: 证据（见上；可为 ``None``）。
    :param parameter_count: 显式覆盖可拟合参数个数。
    :param ast: 显式覆盖 AST（优先于 ``evidence`` 中提取的公式）。
    :return: 含 ``complexity`` 与两个 MDL 标志的字典。
    :raises MetricsInputError: 证据形态非法或计数不是非负整数。
    """
    if parameter_count is not None:
        if isinstance(parameter_count, bool) or not isinstance(parameter_count, int):
            raise MetricsInputError(
                f"parameter_count 必须是非负整数，实际得到：{parameter_count!r}"
            )
        if parameter_count < 0:
            raise MetricsInputError(
                f"parameter_count 必须非负，实际得到：{parameter_count!r}"
            )

    payload: dict[str, Any] = {
        "complexity": None,
        "components": {},
        "degraded": False,
        "note": COMPLEXITY_NOTE,
    }
    payload.update(MDL_FLAGS)

    # -- 选定 AST ---------------------------------------------------------
    chosen_ast = ast
    if chosen_ast is not None and not isinstance(chosen_ast, tuple):
        if isinstance(chosen_ast, str):
            chosen_ast = _parse_ast(chosen_ast)
            if chosen_ast is None:
                raise MetricsInputError(
                    f"显式提供的表达式 {ast!r} 无法被受限语法解析"
                )
        else:
            raise MetricsInputError(
                f"ast 必须是 AST 元组或字符串，实际得到：{type(ast).__name__}"
            )

    # -- 形态 1：P04 的复杂度映射 ----------------------------------------
    if chosen_ast is None and isinstance(evidence, Mapping) and (
        "expression_nodes" in evidence or "total" in evidence
    ):
        _reject_forbidden_keys(evidence, "complexity 证据")
        nodes = _coerce_nonnegative_int(
            evidence.get("expression_nodes", 0), "complexity.expression_nodes"
        )
        rendered = _coerce_nonnegative_int(
            evidence.get("rendered_length", 0), "complexity.rendered_length"
        )
        parameters = _coerce_nonnegative_int(
            evidence.get("parameter_count", 0), "complexity.parameter_count"
        )
        declared_total = evidence.get("total")
        total = nodes + parameters
        if declared_total is not None:
            declared = _coerce_nonnegative_int(declared_total, "complexity.total")
            if declared != total:
                # P04 的常数/线性基线把 total 记作「节点数 + 参数数」的不同口径
                # （例如常数基线 total=1）。此处不推翻上游，只如实记录差异。
                payload["total_mismatch_with_upstream"] = {
                    "upstream_total": declared,
                    "recomputed_total": total,
                }
                total = declared
        payload["complexity"] = _round(float(total))
        payload["components"] = {
            "expression_nodes": nodes,
            "rendered_length": rendered,
            "parameter_count": parameters,
            "total": total,
        }
        payload["source"] = "P04 的复杂度映射（原样采信，口径一致）"
        if parameter_count is not None and parameter_count != parameters:
            payload["parameter_count_override_ignored"] = True
        return payload

    # -- 形态 2：AST / 表达式文本 ----------------------------------------
    if chosen_ast is None and isinstance(evidence, str):
        chosen_ast = _parse_ast(evidence)
        if chosen_ast is None:
            raise MetricsInputError(
                f"表达式文本 {evidence!r} 无法被受限语法解析，无法计算公式长度"
            )

    if chosen_ast is not None:
        length = formula_length(chosen_ast)
        nodes = _coerce_nonnegative_int(length["expression_nodes"], "expression_nodes")
        rendered = _coerce_nonnegative_int(length["rendered_length"], "rendered_length")
        parameters = parameter_count if parameter_count is not None else 0
        payload["complexity"] = _round(float(nodes + parameters))
        payload["components"] = {
            "expression_nodes": nodes,
            "rendered_length": rendered,
            "parameter_count": parameters,
            "total": nodes + parameters,
            "depth": expression_depth(chosen_ast),
            "rendered": render_expression(chosen_ast),
        }
        payload["source"] = "公式长度近似（表达式节点数 + 可拟合参数个数）"
        return payload

    # -- 形态 3：假说对象 -------------------------------------------------
    if isinstance(evidence, Hypothesis) or (
        isinstance(evidence, Mapping) and "statement" in evidence
    ):
        fields = _hypothesis_fields(evidence)
        ast_from_model, text, source = _expression_of(fields)
        if ast_from_model is not None:
            length = formula_length(ast_from_model)
            nodes = _coerce_nonnegative_int(length["expression_nodes"], "expression_nodes")
            rendered = _coerce_nonnegative_int(length["rendered_length"], "rendered_length")
            if parameter_count is not None:
                parameters = parameter_count
            else:
                parameters = _count_parameters(fields)
            payload["complexity"] = _round(float(nodes + parameters))
            payload["components"] = {
                "expression_nodes": nodes,
                "rendered_length": rendered,
                "parameter_count": parameters,
                "total": nodes + parameters,
                "depth": expression_depth(ast_from_model),
                "rendered": render_expression(ast_from_model),
            }
            payload["source"] = f"假说公式（{source}）+ 可拟合参数个数"
            return payload

        # 无公式：退化为陈述长度，并显式标注降级。
        statement = fields.get("statement")
        text_length = len(statement) if isinstance(statement, str) else 0
        parameters = parameter_count if parameter_count is not None else _count_parameters(fields)
        payload["complexity"] = _round(float(text_length + parameters))
        payload["components"] = {
            "expression_nodes": 0,
            "rendered_length": text_length,
            "parameter_count": parameters,
            "total": text_length + parameters,
            "rendered": None,
        }
        payload["degraded"] = True
        payload["source"] = (
            "降级：该假说没有公式，改为对陈述文本长度 + 可拟合参数个数计数；"
            "该值与公式型候选的复杂度**不可直接比较**"
        )
        return payload

    if evidence is None:
        raise MetricsInputError(
            "complexity 需要证据：FitResult 的复杂度映射、AST、表达式文本、"
            "假说对象或字典之一"
        )
    raise MetricsInputError(
        "complexity 无法从给定证据计算，证据类型："
        f"{type(evidence).__name__}；请提供 AST、表达式文本或假说对象"
    )


def _coerce_nonnegative_int(value: Any, label: str) -> int:
    """把计数强制为非负整数，非法即报错。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetricsInputError(f"{label} 必须是非负整数，实际得到：{value!r}")
    if value < 0:
        raise MetricsInputError(f"{label} 必须非负，实际得到：{value!r}")
    return value


def _count_parameters(fields: Mapping[str, Any]) -> int:
    """从假说字段里数出可拟合参数个数。

    优先 ``model.coefficients``；其次 ``fitted_parameters``；皆无则 0。
    只数**顶层**键，不递归——递归会把嵌套结构误算成多个自由参数。
    """
    model = fields.get("model")
    if isinstance(model, Mapping):
        coefficients = model.get("coefficients")
        if isinstance(coefficients, Mapping) and coefficients:
            return len(coefficients)
    fitted = fields.get("fitted_parameters")
    if isinstance(fitted, Mapping) and fitted:
        return len(fitted)
    return 0


# ---------------------------------------------------------------------------
# 假说字段提取
# ---------------------------------------------------------------------------


def _hypothesis_fields(hypothesis: Any) -> dict:
    """把假说统一取成字典（对象或映射两种形态都接受）。

    :raises MetricsInputError: 形态非法或缺少必需字段。
    """
    if isinstance(hypothesis, Hypothesis):
        return hypothesis.to_dict()
    if isinstance(hypothesis, Mapping):
        _reject_forbidden_keys(hypothesis, "假说字段")
        payload = dict(hypothesis)
    else:
        missing = [
            name
            for name in ("id", "version", "statement")
            if not hasattr(hypothesis, name)
        ]
        if missing:
            raise MetricsInputError(
                f"假说既不是 Hypothesis，也不是映射，也不具备 {'/'.join(missing)} 属性；"
                f"实际得到：{type(hypothesis).__name__}"
            )
        payload = {
            "id": getattr(hypothesis, "id"),
            "version": getattr(hypothesis, "version"),
            "type": getattr(hypothesis, "type", None),
            "statement": getattr(hypothesis, "statement"),
            "model": getattr(hypothesis, "model", None),
            "representation": getattr(hypothesis, "representation", None),
            "fitted_parameters": getattr(hypothesis, "fitted_parameters", {}),
            "provenance": getattr(hypothesis, "provenance", {}),
        }

    for required in ("id", "version", "statement"):
        if required not in payload:
            raise MetricsInputError(f"假说缺少必需字段 {required!r}")
    _require_nonempty_str(payload["id"], "假说 id")
    _require_nonempty_str(payload["version"], "假说 version")
    _require_nonempty_str(payload["statement"], "假说 statement")
    return payload


def _expression_of(fields: Mapping[str, Any]) -> tuple[tuple | None, str | None, str]:
    """从假说字段中提取公式。

    :return: ``(ast, 文本, 来源说明)``；无公式时 ``ast`` 为 ``None``。
    """
    model = fields.get("model")
    if isinstance(model, str) and model.strip():
        ast = _parse_ast(model)
        return ast, model, "model 文本"
    if isinstance(model, Mapping):
        for key in ("expression", "formula", "rendered_expression"):
            candidate = model.get(key)
            if isinstance(candidate, str) and candidate.strip():
                ast = _parse_ast(candidate)
                return ast, candidate, f"model.{key}"
            if isinstance(candidate, tuple):
                try:
                    text = render_expression(candidate)
                except Exception:  # pragma: no cover - 防御性兜底
                    text = None
                return candidate, text, f"model.{key}（AST）"
    return None, None, "（假说未携带公式）"


def _hypothesis_knowledge_version(fields: Mapping[str, Any]) -> str | None:
    """读取假说自己声明的知识库版本（来自 ``provenance``）。"""
    provenance = fields.get("provenance")
    if isinstance(provenance, Mapping):
        value = provenance.get("knowledge_version")
        if isinstance(value, str) and value.strip():
            return value
    return None


# ---------------------------------------------------------------------------
# 评估记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    """一条假说的多维评估记录（``INTERFACES.md`` §2.4 的 ``Evaluation``）。

    本对象**只并列存放**四个指标，**不含**任何加权总分、排名或筛选结果。
    :meth:`dimension_names` 只是把四个名字按固定顺序列出来供下游迭代，
    它不做聚合，也不定义优化方向（方向属 P10）。

    :param hypothesis_id: 关联假说 id。
    :param hypothesis_version: 关联假说版本号。
    :param hypothesis_type: 假说类型（可能为 ``None``，若上游未提供）。
    :param gain: 预测增益 G；证据不足时为 ``None``。
    :param stability: 稳定性 S；证据不足时为 ``None``。
    :param novelty: 新颖性 N；证据不足时为 ``None``。
    :param complexity: 复杂度 C；证据不足时为 ``None``。
    :param knowledge_version: 新颖性所相对的 K 版本，**必填且非空**。
    :param components: 四个维度的完整明细（不含任何合成分）。
    :param flags: 机检标志（近似性、版本一致性、证据充分性等）。
    :param note: 口径说明。
    """

    hypothesis_id: str
    hypothesis_version: str
    hypothesis_type: str | None
    gain: float | None
    stability: float | None
    novelty: float | None
    complexity: float | None
    knowledge_version: str
    components: Mapping[str, Any] = field(default_factory=dict)
    flags: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        _require_nonempty_str(self.hypothesis_id, "Evaluation.hypothesis_id")
        _require_nonempty_str(self.hypothesis_version, "Evaluation.hypothesis_version")
        if not isinstance(self.knowledge_version, str) or not self.knowledge_version.strip():
            raise MetricsVersionError(
                "Evaluation.knowledge_version 必填且非空："
                "新颖性必须绑定到明确的知识库版本 K^(v)"
            )
        for name in DIMENSION_NAMES:
            value = getattr(self, name)
            if value is None:
                continue
            if not _is_finite_number(value):
                raise MetricsInputError(
                    f"Evaluation.{name} 必须是有限实数或 None，实际得到：{value!r}"
                )
        if self.novelty is not None and not 0.0 <= float(self.novelty) <= 1.0:
            raise MetricsInputError(
                f"新颖性必须落在 [0, 1] 内，实际得到：{self.novelty!r}"
            )
        if self.stability is not None and not 0.0 <= float(self.stability) <= 1.0:
            raise MetricsInputError(
                f"稳定性必须落在 [0, 1] 内，实际得到：{self.stability!r}"
            )
        if self.complexity is not None and float(self.complexity) < 0.0:
            raise MetricsInputError(
                f"复杂度必须非负，实际得到：{self.complexity!r}"
            )
        for label, payload in (("components", self.components), ("flags", self.flags)):
            _require_mapping(payload, f"Evaluation.{label}")
            _reject_forbidden_keys(payload, f"Evaluation.{label}")
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))
        object.__setattr__(self, "flags", MappingProxyType(dict(self.flags)))

    # -- 只读视图 ---------------------------------------------------------

    @staticmethod
    def dimension_names() -> tuple[str, ...]:
        """四个维度的名字，按固定顺序。**仅列举，不排序、不聚合。**"""
        return DIMENSION_NAMES

    def dimension_values(self) -> dict[str, float | None]:
        """四个维度的取值字典。**不是总分，也不是排序键。**"""
        return {name: getattr(self, name) for name in DIMENSION_NAMES}

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        """证据不足、取值为 ``None`` 的维度名。"""
        return tuple(
            name for name in DIMENSION_NAMES if getattr(self, name) is None
        )

    @property
    def complete(self) -> bool:
        """四个维度是否都有取值。"""
        return not self.missing_dimensions

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典，字段与 ``INTERFACES.md`` §2.4 对齐。"""
        return {
            "metrics_version": METRICS_VERSION,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_version": self.hypothesis_version,
            "hypothesis_type": self.hypothesis_type,
            "gain": self.gain,
            "stability": self.stability,
            "novelty": self.novelty,
            "complexity": self.complexity,
            "knowledge_version": self.knowledge_version,
            "missing_dimensions": list(self.missing_dimensions),
            "complete": self.complete,
            "components": _jsonable(self.components),
            "flags": _jsonable(self.flags),
            "note": self.note,
            "no_aggregation_note": (
                "本记录并列存放 G/S/N/C 四个指标，不含加权总分、排名或筛选结果；"
                "加权与 Pareto 排序属 P10 的决策。"
            ),
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与摘要。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """评估记录内容摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"Evaluation({self.hypothesis_id}@{self.hypothesis_version}, "
            f"G={self.gain}, S={self.stability}, N={self.novelty}, C={self.complexity}, "
            f"K={self.knowledge_version!r})"
        )


# ---------------------------------------------------------------------------
# 装配：evaluate_hypothesis
# ---------------------------------------------------------------------------


def evaluate_hypothesis(
    hypothesis: Any,
    *,
    knowledge_base: KnowledgeBase,
    relation_fit: Any = None,
    stability_evidence: Any = None,
    complexity_evidence: Any = None,
    expression: Any = None,
    loss: str = DEFAULT_GAIN_LOSS,
    basis: str = DEFAULT_GAIN_BASIS,
    baseline_choice: str = DEFAULT_BASELINE_CHOICE,
    floor_at_zero: bool = False,
    parameter_count: Any = None,
    source_ref: Any = None,
    **_rejected: Any,
) -> Evaluation:
    """计算一条假说的 G/S/N/C，并**并列**装配为 :class:`Evaluation`。

    四个指标中任一项的证据缺失时，对应维度记为 ``None`` 并列入
    ``missing_dimensions``——**不填默认值、不做插补、更不因此扣分**。
    「没算」与「算出来是 0」必须可区分。

    :param hypothesis: :class:`~sdl_m04.hypothesis.Hypothesis` 或等价字典。
    :param knowledge_base: :class:`KnowledgeBase`；其版本即新颖性所相对的
        :math:`K^{(v)}`（验收标准 ①）。**必填**。
    :param relation_fit: P04 的 :class:`RelationFit`、``(candidate, baseline)``
        两个 :class:`FitResult`，或损失映射；用于计算 ``G``。
    :param stability_evidence: P06 的 :class:`StabilityReport`、计数映射或
        布尔序列；用于计算 ``S``。
    :param complexity_evidence: 计算 ``C`` 的证据；缺省时回退到假说自身。
    :param expression: 覆盖新颖性与复杂度所用的公式。
    :param loss: 增益对照的损失名。
    :param basis: 增益基准（``relative`` / ``absolute``）。
    :param baseline_choice: 基线选择（``hardest`` 或基线种类名）。
    :param floor_at_zero: 是否把负增益截断为 0（默认关闭）。
    :param parameter_count: 覆盖复杂度的参数个数。
    :param source_ref: 数据引用标识（仅用于登记口径，**不得**是 V/C 分区）。
    :return: :class:`Evaluation`。
    :raises MetricsPolicyError: 传入加权/聚合/筛选参数，或引用受限分区。
    :raises MetricsVersionError: 知识库版本缺失或不合法。
    :raises MetricsInputError: 其它输入问题。
    """
    _reject_aggregation_attempts(_rejected, "evaluate_hypothesis")

    if not isinstance(knowledge_base, KnowledgeBase):
        raise MetricsInputError(
            "knowledge_base 必须是 KnowledgeBase 实例："
            "评估记录必须绑定明确的知识库版本 K^(v)"
        )
    if source_ref is not None:
        _reject_restricted_ref(source_ref, "source_ref")

    fields = _hypothesis_fields(hypothesis)
    hypothesis_id = fields["id"]
    hypothesis_version = fields["version"]

    # -- G：预测增益 ------------------------------------------------------
    gain_dimension: float | None = None
    components: dict[str, Any] = {}
    if relation_fit is not None:
        gain_components = gain(
            relation_fit,
            loss=loss,
            basis=basis,
            baseline_choice=baseline_choice,
            floor_at_zero=floor_at_zero,
        )
        gain_dimension = float(gain_components["gain"])
        components["gain"] = gain_components

    # -- S：稳定性 --------------------------------------------------------
    stability_dimension: float | None = None
    if stability_evidence is not None:
        stability_components = stability(stability_evidence)
        stability_dimension = float(stability_components["stability"])
        components["stability"] = stability_components

    # -- N：新颖性（恒有值，因为它只依赖假说与知识库） --------------------
    novelty_components = novelty(hypothesis, knowledge_base, expression=expression)
    novelty_dimension = float(novelty_components["novelty"])
    components["novelty"] = novelty_components

    # -- C：复杂度 --------------------------------------------------------
    complexity_dimension: float | None = None
    if complexity_evidence is not None:
        complexity_components = complexity(
            complexity_evidence, parameter_count=parameter_count
        )
    else:
        complexity_components = complexity(
            hypothesis, parameter_count=parameter_count, ast=expression
        )
    complexity_dimension = float(complexity_components["complexity"])
    components["complexity"] = complexity_components

    # -- 版本一致性核对（验收标准 ①） -------------------------------------
    declared_version = _hypothesis_knowledge_version(fields)
    version_matches = declared_version is None or declared_version == knowledge_base.version

    # -- 机检标志 ---------------------------------------------------------
    flags: dict[str, Any] = {
        "is_formula_length_approximation": True,
        "is_strict_mdl": False,
        "novelty_bound_to_knowledge_version": True,
        "knowledge_version": knowledge_base.version,
        "hypothesis_knowledge_version": declared_version,
        "knowledge_version_matches": version_matches,
        "knowledge_base_empty": knowledge_base.is_empty,
        "no_weighted_total": True,
        "evidence_grade_granted": False,
    }
    if "stability" in components:
        flags["stability_sufficient"] = bool(
            components["stability"].get("sufficient", False)
        )
        flags["stability_reasons"] = list(
            components["stability"].get("reasons", ()) or ()
        )
    else:
        flags["stability_sufficient"] = None
        flags["stability_reasons"] = []
    if "gain" in components:
        flags["gain_relative_reduction_defined"] = bool(
            components["gain"].get("relative_reduction_defined", False)
        )
    flags["complexity_degraded"] = bool(
        components["complexity"].get("degraded", False)
    )
    if not version_matches:
        flags["knowledge_version_mismatch_note"] = (
            f"假说声明生成时依据 K^({declared_version})，"
            f"而本次新颖性相对 K^({knowledge_base.version}) 计算；"
            "两项均已如实记录，未做归一或替换。"
        )

    note = (
        "G/S/N/C 并列记录，彼此独立、量纲不同，本层不做任何加权或聚合；"
        "所有维度均为开发期探索量，不构成确证结论、不授予证据等级。"
    )

    return Evaluation(
        hypothesis_id=hypothesis_id,
        hypothesis_version=hypothesis_version,
        hypothesis_type=fields.get("type"),
        gain=_round(gain_dimension) if gain_dimension is not None else None,
        stability=(
            _round(stability_dimension) if stability_dimension is not None else None
        ),
        novelty=_round(novelty_dimension) if novelty_dimension is not None else None,
        complexity=(
            _round(complexity_dimension) if complexity_dimension is not None else None
        ),
        knowledge_version=knowledge_base.version,
        components=components,
        flags=flags,
        note=note,
    )


# ---------------------------------------------------------------------------
# 便捷工具
# ---------------------------------------------------------------------------


def evaluate_pool(
    pool: Any,
    *,
    knowledge_base: KnowledgeBase,
    fits: Mapping[str, Any] | None = None,
    stabilities: Mapping[str, Any] | None = None,
    **options: Any,
) -> tuple[Evaluation, ...]:
    """批量评估一个假说池（例如 P08 的 :class:`GeneratedHypothesisPool`）。

    这是一个**纯便利函数**：按池内顺序逐条调用 :func:`evaluate_hypothesis`，
    返回与池内顺序一致的元组。它**不做**筛选、排序或裁剪——那些属 P10。

    :param pool: ``GeneratedHypothesisPool`` 或任何可迭代的假说集合。
    :param knowledge_base: 知识库（版本即 :math:`K^{(v)}`）。
    :param fits: ``假说 id → 增益证据`` 的映射。
    :param stabilities: ``假说 id → 稳定性证据`` 的映射。
    :param options: 透传给 :func:`evaluate_hypothesis` 的其它参数。
    :return: :class:`Evaluation` 元组。
    :raises MetricsInputError: 池形态非法或缺少 ``id`` 字段。
    """
    if hasattr(pool, "hypotheses"):
        items: Iterable[Any] = pool.hypotheses
    elif isinstance(pool, Iterable) and not isinstance(pool, (str, bytes, Mapping)):
        items = pool
    else:
        raise MetricsInputError(
            f"pool 必须是假说池或可迭代的假说集合，实际得到：{type(pool).__name__}"
        )
    fit_map = dict(fits) if fits else {}
    stability_map = dict(stabilities) if stabilities else {}

    out: list[Evaluation] = []
    for index, item in enumerate(items):
        fields = _hypothesis_fields(item)
        hypothesis_id = fields["id"]
        out.append(
            evaluate_hypothesis(
                item,
                knowledge_base=knowledge_base,
                relation_fit=fit_map.get(hypothesis_id),
                stability_evidence=stability_map.get(hypothesis_id),
                **options,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 供测试与下游使用的静态事实
# ---------------------------------------------------------------------------

#: 本模块**不提供**的接口名。测试据此断言「P09 未越界实现 P10 的内容」。
#: 单列成一常量而非散落在测试里，是为了让「本层不做什么」也成为可机检的声明。
NOT_PROVIDED_BY_P09: tuple[str, ...] = (
    "pareto_front",
    "top_k_reserved",
    "select_freeze_candidates",
    "weighted_sum",
    "total_score",
    "aggregate_score",
)
