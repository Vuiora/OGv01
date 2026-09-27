"""M4 模式到假说与竞争解释（P08 交付物）。

本模块把 M3 产出的**模式对象**（``INTERFACES.md`` §2.2 的 ``Pattern``）转成
P07 定义的**结构化假说**（``INTERFACES.md`` §2.3 的 ``Hypothesis``），并按框架说明
§3 的要求为每个假说补齐**零假说**与**竞争解释（替代解释）**，最后得到一个
**经去重且大小受限的假说池**。

它在流水线中的位置::

    P04/P05（M3 模式）──┐
                        ├─> P08（本模块）──> P09（M5 指标 G/S/N/C）
    P07（M4 假说对象）──┘                        └─> P10（Pareto 筛选）

公开接口
--------

- :func:`hypotheses_from_patterns`：模式序列 → 受预算约束的假说池。
- :func:`attach_nulls`：为既有假说补齐（或替换）零假说，经版本链产生新版本。
- :func:`attach_alternatives`：为既有假说补齐（或替换）竞争解释，同样经版本链。
- :class:`GenerateBudget` / :class:`GeneratedHypothesisPool`：预算与池容器。
- :data:`DEFAULT_MAX_HYPOTHESES` / :data:`CONTRACT_POOL_NOTE`：池上界的取值与依据。

三条验收标准与实现对应关系
--------------------------

① **每个假说含可证伪的 ``predictions``（什么结果与它不相容）**
   本模块**不接受调用方直接给出 ``predictions``**——它由模式内容机械派生
   （见 :func:`_predictions_for`），每条预测都必须写明 ``incompatible_with``。
   这样做是刻意的：若允许外部文本直接充当预测，可证伪性就退化为「撰写者的
   自觉」，无法机检。派生后的条目仍会被 P07 的
   :func:`~sdl_m04.hypothesis.require_provenance` 同族校验把关
   （``predictions`` 非空、每条含非空 ``incompatible_with``）。

② **每个假说至少一个零假说**
   两类零假说恒被同时生成：

   - 与主假说**直接对立**的零假说（如「无子群结构」「无变点」「无守恒中心」）；
   - 与**机制无关的平凡**零假说（如「任意等规模划分都能达到同等分离度」
     「稳定中心仅由天然有界性造成」）。

   后者用于排除「有结构就一定是发现」的误读。:func:`attach_nulls` 可对
   外部传入的假说做同样的补齐；缺省时按假说自身反推，不引入外部数据。

③ **生成的假说池经去重后大小受契约约束**
   ``INTERFACES.md`` §2 只约束池内**条目形状**（§2.1–§2.3 的字段表），
   未直接给出假说池的数量上界。本模块因此把上界**锚定到框架说明 §2 的原型配置**
   「每轮保留 50 个探索候选」——即 :data:`DEFAULT_MAX_HYPOTHESES`，并使其可配置
   （:class:`GenerateBudget`）。依据与口径见 :data:`CONTRACT_POOL_NOTE`。

   去重分两级，由 ``dedup_mode`` 选择：

   - ``"exact"``（默认）：按内容判等（数值按 :data:`ROUND_DIGITS` 位量化后比较），
     合并「同一模式被多阶段重复产出」造成的重复条目；
   - ``"skeleton"``：在内容判等之外，进一步把数值折叠为占位符后判等，
     即合并框架说明 §2 所警示的**参数微变体**（同结构、仅常数不同）。

   两级去重都在**裁剪之前**完成，且保留的条目是**确定性**选出的（按
   ``pattern_id`` 字典序取最小者），不做任何基于指标的择优——择优属 P10。

设计边界（严格遵守 P08 增量卡）
--------------------------------

- **不计算任何指标**（属 P09）：本模块不产生 ``gain`` / ``novelty`` /
  ``stability`` / ``complexity`` 的数值，``complexity`` / ``development`` /
  ``confirmation_plan`` 三个槽位一律留空——它们分别属 P09 与 M6（P11）。
  模式里**已有的**稳定性字段只被**原样转录**到假说来源信息里备查，
  本模块不对它做任何换算、比较或排序。
- **不做 Pareto 筛选或冻结候选**（属 P10/M6）：本模块只保证池大小不超过预算，
  不排序、不选优、不生成确证计划。
- **不读数据、不碰令牌**：本模块不导入 ``sdl_m01``，不访问任何数据库、
  数据集或质量报告。``provenance.data_ids`` 只是**引用标识字符串**，
  由调用方或模式自身的来源信息提供，不携带任何记录正文。
- **不授予证据等级**：生成物的 ``status`` 恒为 ``draft``（生命周期标记，
  不是证据等级）。证据等级（E0/E1/E2）属 P15 的发现档案。
- **不修改 ``sdl_m01/``**：本模块不导入也不触碰 M1 的任何文件。

LLM 文本的定位（工单显式要求）
------------------------------

可选参数 ``text_enricher`` 允许调用方注入一个外部文本生成器（例如 LLM）。
其输出**只能**用于填充受控的文本字段（见 :data:`LLM_FIELD_FILL_ALLOWED_KEYS`），
并且：

1. 输出中若出现任何**证据等级 / 置信度 / 状态**类键名
   （见 :data:`EVIDENCE_GRADE_KEY_HINTS`），立即抛
   :class:`FieldFillError`——文本不得据此授予证据等级；
2. 被采纳的字段与其**文本摘要**记入 ``provenance["field_fill"]``，
   含生成器标识、模型版本与 SHA-256 摘要，使每段文本可追溯；
3. 假说的 ``status`` 与 ``evidence_log`` 不因文本填充发生任何变化。

已知限制
--------

1. **陈述文本是确定性模板渲染，不是自然语言理解。** 模板按模式种类分派
   （见 :func:`_statement_for`），能保证「说了什么、与什么不相容」可机检，
   但无法保证措辞在具体学科语境下的贴切。措辞打磨属人工复核。
2. **预测的语义恰当性不做校验。** 与 P07 的立场一致：本模块能保证
   ``incompatible_with`` 存在且非空，不能判断该不相容声明在科学上是否恰当。
3. **``skeleton`` 去重是有损的。** 它假设「同结构、仅常数不同」的假说可视为
   同一假说。当常数本身承载语义（例如簇心坐标就是结论）时，该假设不成立，
   因此默认关闭，由调用方按需启用。
4. **合并后的条目只保留一个代表。** 被合并的模式标识记录在
   ``provenance["merged_pattern_ids"]`` 中，但它们的数值细节不再保留——
   若需完整历史，应保留合并前的池。
5. **不保证 ``pattern_id`` 之间的语义不冲突。** 去重只在**本次调用**的输入内
   进行，不跨轮次、不跨协议、不与 P15 归档对账。
6. **``scope`` 不做语义推导。** 本模块不会从模式载荷«猜出»对象、环境与时间范围；
   ``scope`` 由调用方显式声明，缺省时为空映射（即「未限定」，这一状态本身
   会被记录在来源信息中，不会被伪装成已限定）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from sdl_m04.hypothesis import (
    HYPOTHESIS_TYPES,
    Hypothesis,
    HypothesisStore,
    HypothesisValidationError,
)

__all__ = [
    # 异常
    "GenerateError",
    "PatternInputError",
    "FieldFillError",
    # 常量
    "GENERATE_VERSION",
    "SUPPORTED_PATTERN_KINDS",
    "KIND_TO_HYPOTHESIS_TYPE",
    "DEFAULT_MAX_HYPOTHESES",
    "DEFAULT_MAX_NULLS",
    "DEFAULT_MAX_ALTERNATIVES",
    "DEFAULT_MAX_PREDICTIONS",
    "DEDUP_MODES",
    "ROUND_DIGITS",
    "CONTRACT_POOL_NOTE",
    "LLM_FIELD_FILL_ALLOWED_KEYS",
    "EVIDENCE_GRADE_KEY_HINTS",
    "GENERATOR_ID",
    # 容器
    "GenerateBudget",
    "GeneratedHypothesisPool",
    # 主入口
    "hypotheses_from_patterns",
    "attach_nulls",
    "attach_alternatives",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 交付物版本号，写入池的序列化结果，便于追溯生成口径的变更。
GENERATE_VERSION = "P08-v1.0"

#: 生成器标识，写入每条假说的 ``provenance.generator``（可追溯）。
GENERATOR_ID = "sdl_m04/generate.py"

#: 本模块接受的模式种类。与 ``INTERFACES.md`` §2.2 的 ``Pattern.kind`` 对齐，
#: 也与 ``sdl_m03.structure.PATTERN_KINDS`` 一致（一致性由测试跨模块比对）。
SUPPORTED_PATTERN_KINDS: tuple[str, ...] = (
    "relation",
    "cluster",
    "changepoint",
    "invariant",
)

#: 模式种类 → 假说类型。四者同名，这是刻意对齐的：``INTERFACES.md`` §2.2 的
#: ``Pattern.kind`` 与 P07 的``HYPOTHESIS_TYPES`` 前四项逐项相同，
#: 因此映射是恒等式而非「近似翻译」。
KIND_TO_HYPOTHESIS_TYPE: Mapping[str, str] = {
    kind: kind for kind in SUPPORTED_PATTERN_KINDS
}

#: **假说池数量上界的默认值**。依据：框架说明 §2 的原型配置
#: 「每轮保留 50 个探索候选」。该数字是原型取值，应在探索资料上调试，
#: 因此 :class:`GenerateBudget` 允许覆盖。依据见 :data:`CONTRACT_POOL_NOTE`。
DEFAULT_MAX_HYPOTHESES = 50

#: 单个假说允许附带的零假说条数上界（含直接对立与平凡两类）。
DEFAULT_MAX_NULLS = 4

#: 单个假说允许附带的竞争解释条数上界。
DEFAULT_MAX_ALTERNATIVES = 3

#: 单个假说允许附带的可证伪预测条数上界。
DEFAULT_MAX_PREDICTIONS = 3

#: 去重模式：``exact``（内容判等）与 ``skeleton``（内容判等 + 结构骨架判等）。
DEDUP_MODES: tuple[str, ...] = ("exact", "skeleton")

#: 内容判等时的数值量化位数。取 6 位与仓库内其他模块的展示精度一致；
#: 目的是让「同一模式被不同阶段重复产出」造成的浮点尾差不算作差异。
ROUND_DIGITS = 6

#: 池上界的契约依据说明（写入池的来源信息，使「受契约约束」可被复核）。
CONTRACT_POOL_NOTE = (
    "INTERFACES.md §2 约束假说池条目的字段形状（§2.1 候选、§2.2 模式、§2.3 假说），"
    "未直接给出数量上界；本模块把上界锚定到 SDL算法框架说明.md §2 的原型配置"
    "「每轮保留 50 个探索候选」，默认值见 DEFAULT_MAX_HYPOTHESES，可由 GenerateBudget 覆盖。"
)

#: 允许被外部文本生成器（例如 LLM）填充的字段白名单。
#: 仅限文本性字段；任何结构性字段（预测、零假说、来源信息）都不在名单内。
LLM_FIELD_FILL_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "statement",
        "plain_explanation",
        "alternatives",
    }
)

#: 一旦外部文本中出现这些键名，立即拒绝：文本不得授予证据等级。
#: 依据框架说明 §3 与 §2 末段——「LLM 不能凭文本合理性授予证据等级」。
EVIDENCE_GRADE_KEY_HINTS: frozenset[str] = frozenset(
    {
        "evidence_level",
        "evidence_grade",
        "evidence_tier",
        "grade",
        "confidence",
        "confidence_level",
        "status",
        "supported",
        "refuted",
        "confirmed",
        "score",
    }
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class GenerateError(ValueError):
    """模式到假说转换过程中的基类异常。"""


class PatternInputError(GenerateError):
    """输入模式不合法：形态错误、种类未知、缺标识或缺来源引用。"""


class FieldFillError(GenerateError):
    """外部文本生成器（LLM）的输出违反了字段填充约束。

    单列一个子类，是因为「文本不得授予证据等级」在工单中被单独提出，
    调用方与测试需要能把它从一般的输入错误中区分出来。
    """


# ---------------------------------------------------------------------------
# 数值与容器辅助
# ---------------------------------------------------------------------------


def _round(value: float, digits: int = ROUND_DIGITS) -> float:
    """四舍五入并把 ``-0.0`` 归一为 ``0.0``，保证序列化稳定。"""
    result = round(float(value), digits)
    return 0.0 if result == 0 else result


def _is_finite_number(value: Any) -> bool:
    """是否为有限实数（布尔不算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _plain(value: Any) -> Any:
    """把只读视图 / 元组等结构还原为普通 JSON 兼容结构。

    P07 的假说对象会把嵌套容器深度冻结（映射 → 只读视图、序列 → 元组）。
    本模块在读取模式载荷与假说字段时统一先还原，避免出现
    「元组与列表比较不相等」这类由容器类型造成的假差异。
    """
    if isinstance(value, Mapping):
        return {str(key): _plain(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    """规范式 JSON 文本（键排序、紧凑分隔符），用于稳定比对与摘要。"""
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any, length: int = 16) -> str:
    """对规范式 JSON 取 SHA-256 摘要（取前 ``length`` 位十六进制）。"""
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:length]


def _fmt(value: Any) -> str:
    """把数值渲染为简短、稳定的文本片段（用于陈述句）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    rounded = _round(float(value))
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return str(int(rounded))
    return f"{rounded:g}"


def _quantize(value: Any) -> Any:
    """递归量化数值，供内容判等使用（把浮点尾差折叠掉）。"""
    if isinstance(value, Mapping):
        return {str(key): _quantize(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_quantize(item) for item in value]
    if _is_finite_number(value):
        return _round(float(value))
    return value


#: 骨架化时替换数值的占位符。选一个不可能出现在载荷中的字符。
_SKELETON_PLACEHOLDER = "#"


def _skeletonize(value: Any) -> Any:
    """递归把数值折叠为占位符，得到**结构骨架**。

    与 P03 的表示骨架同构的思路：参数微变体（仅常数不同）拥有相同骨架，
    因而可被判为「同一假说」。注意这是**有损**操作，故默认不启用
    （见模块文档「已知限制 3」）。
    """
    if isinstance(value, Mapping):
        return {str(key): _skeletonize(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_skeletonize(item) for item in value]
    if _is_finite_number(value):
        return _SKELETON_PLACEHOLDER
    return value


# ---------------------------------------------------------------------------
# 模式视图：把「对象形态」与「字典形态」统一
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PatternView:
    """模式的最小只读视图。

    只保留转换所必需的六个要素，刻意**不**暴露 ``stability`` 之外的任何
    可推算量，以免本模块越界去做 P09 的指标计算。

    :param pattern_id: 稳定标识，必填且非空。
    :param kind: 模式种类，必须落在 :data:`SUPPORTED_PATTERN_KINDS`。
    :param payload: 结构描述（普通 JSON 兼容字典）。
    :param metrics: 误差 / 残差 / 复杂度三项（原样保留，本模块不重算）。
    :param provenance: 模式来源信息（用于派生假说的数据引用）。
    :param stability: 模式已有的稳定性数值（**原样转录**，本模块不换算）。
    """

    pattern_id: str
    kind: str
    payload: Mapping[str, Any]
    metrics: Mapping[str, Any]
    provenance: Mapping[str, Any]
    stability: float | None


def _as_pattern_view(pattern: Any, index: int) -> _PatternView:
    """把一条输入模式规范化为 :class:`_PatternView`。

    接受两种形态：

    - **对象形态**：任何具备 ``pattern_id`` / ``kind`` / ``payload`` 属性的对象
      （例如 ``sdl_m03.structure.StructurePattern``）。本模块**不导入** M3，
      只做鸭子类型判定，以免在模块之间引入运行时耦合。
    - **字典形态**：``Pattern.to_dict()`` 的产物或等价字典。

    :raises PatternInputError: 形态不合法、种类未知或缺标识。
    """
    where = f"patterns[{index}]"
    if isinstance(pattern, Mapping):
        source = _plain(pattern)
    else:
        missing = [
            name
            for name in ("pattern_id", "kind", "payload")
            if not hasattr(pattern, name)
        ]
        if missing:
            raise PatternInputError(
                f"{where} 既不是映射，也不是具备 {'/'.join(missing)} 属性的模式对象；"
                f"实际得到：{type(pattern).__name__}"
            )
        source = {
            "pattern_id": getattr(pattern, "pattern_id"),
            "kind": getattr(pattern, "kind"),
            "payload": _plain(getattr(pattern, "payload")),
            "metrics": _plain(getattr(pattern, "metrics", {}) or {}),
            "provenance": _plain(getattr(pattern, "provenance", {}) or {}),
            "stability": getattr(pattern, "stability", None),
        }

    pattern_id = source.get("pattern_id")
    if not isinstance(pattern_id, str) or not pattern_id.strip():
        raise PatternInputError(f"{where}.pattern_id 必须是非空字符串")

    kind = source.get("kind")
    if kind not in SUPPORTED_PATTERN_KINDS:
        raise PatternInputError(
            f"{where}.kind 必须是 {', '.join(SUPPORTED_PATTERN_KINDS)} 之一，"
            f"实际得到：{kind!r}"
        )

    payload = source.get("payload")
    if not isinstance(payload, Mapping) or not payload:
        raise PatternInputError(f"{where}.payload 必须是非空映射（结构描述）")

    metrics = source.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}

    provenance = source.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}

    stability = source.get("stability")
    if stability is not None:
        if not _is_finite_number(stability) or not 0.0 <= float(stability) <= 1.0:
            raise PatternInputError(
                f"{where}.stability 必须是落在 [0, 1] 的实数或 None，"
                f"实际得到：{stability!r}"
            )
        stability = _round(float(stability))

    return _PatternView(
        pattern_id=pattern_id,
        kind=str(kind),
        payload=dict(payload),
        metrics=dict(metrics),
        provenance=dict(provenance),
        stability=stability,
    )


def _data_refs_of(view: _PatternView) -> list[str]:
    """从模式来源信息中提取数据引用标识（**只取标识，不取正文**）。

    优先顺序（第一个非空者胜出）：

    1. ``provenance["source_ref"]``：M3 记录的探索分区引用；
    2. ``provenance["data_refs"]`` / ``provenance["data_ids"]``：引用标识列表；
    3. ``provenance["sample_fingerprint"]``：样本指纹（前缀 ``sample:``）；
    4. ``provenance["sample_rows"]``：仅行数时**不**作为数据引用——
       行数不足以定位数据，宁可让调用方显式提供。

    找不到任何可用引用时返回空列表，由调用方决定是否报错
    （见 :func:`hypotheses_from_patterns` 的 ``data_ids`` 参数）。
    """
    provenance = view.provenance
    single = provenance.get("source_ref")
    if isinstance(single, str) and single.strip():
        return [single]

    for key in ("data_refs", "data_ids"):
        refs = provenance.get(key)
        if isinstance(refs, (list, tuple)) and refs:
            cleaned = [
                item for item in refs if isinstance(item, str) and item.strip()
            ]
            if cleaned:
                return cleaned

    fingerprint = provenance.get("sample_fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return [f"sample:{fingerprint}"]
    return []


# ---------------------------------------------------------------------------
# 预算与去重模式
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateBudget:
    """假说生成的资源预算。

    :param max_hypotheses: 假说池的数量上界（**硬约束**，池大小不超过它）。
        默认取框架说明 §2 的原型配置「每轮保留 50 个探索候选」。
    :param max_nulls: 单个假说允许附带的零假说条数上界。
    :param max_alternatives: 单个假说允许附带的竞争解释条数上界。
    :param max_predictions: 单个假说允许附带的可证伪预测条数上界。

    注意：``max_hypotheses`` 约束的是**去重之后**的最终池，
    而不是生成过程中的中间数量——去重的意义正在于此。
    """

    max_hypotheses: int = DEFAULT_MAX_HYPOTHESES
    max_nulls: int = DEFAULT_MAX_NULLS
    max_alternatives: int = DEFAULT_MAX_ALTERNATIVES
    max_predictions: int = DEFAULT_MAX_PREDICTIONS

    def __post_init__(self) -> None:
        for label, value in (
            ("max_hypotheses", self.max_hypotheses),
            ("max_nulls", self.max_nulls),
            ("max_alternatives", self.max_alternatives),
            ("max_predictions", self.max_predictions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GenerateError(f"{label} 必须是正整数，实际得到：{value!r}")


def _as_budget(budget: Any) -> GenerateBudget:
    """把 ``None`` / :class:`GenerateBudget` / 字段字典统一为预算对象。"""
    if budget is None:
        return GenerateBudget()
    if isinstance(budget, GenerateBudget):
        return budget
    if isinstance(budget, Mapping):
        allowed = {"max_hypotheses", "max_nulls", "max_alternatives", "max_predictions"}
        unknown = sorted(set(budget) - allowed)
        if unknown:
            raise GenerateError(
                "budget 包含未知字段：" + ", ".join(repr(key) for key in unknown)
            )
        return GenerateBudget(**{key: budget[key] for key in budget})
    raise GenerateError(
        f"budget 必须是 GenerateBudget、字段字典或 None，"
        f"实际得到：{type(budget).__name__}"
    )


# ---------------------------------------------------------------------------
# 陈述 / 模型 / 参数：按模式种类分派
# ---------------------------------------------------------------------------


def _features_text(features: Any) -> str:
    """把特征名序列渲染为 ``a、b、c`` 形式。"""
    if isinstance(features, (list, tuple)) and features:
        return "、".join(str(item) for item in features)
    return "全部参与变量"


def _mapping_text(mapping: Any) -> str:
    """把映射渲染为 ``k=v`` 列表；空映射返回「（未给出）」。"""
    if not isinstance(mapping, Mapping) or not mapping:
        return "（未给出）"
    return "，".join(f"{key}={_fmt(mapping[key])}" for key in sorted(mapping, key=str))


def _cluster_material(view: _PatternView) -> dict:
    """簇模式的假说素材。"""
    payload = view.payload
    features = payload.get("features") or []
    center = payload.get("center") or {}
    index = payload.get("cluster_index")
    k = payload.get("k")
    size = payload.get("size")
    selection = payload.get("selection") or {}

    statement = (
        f"在探索数据上，特征 {_features_text(features)} 支持 k={_fmt(k)} 个子群结构；"
        f"子群 {_fmt(index)}（n={_fmt(size)}）的中心约为 {_mapping_text(center)}。"
    )
    model = {
        "model_kind": "partition_centroid",
        "k": k,
        "features": list(features),
        "cluster_index": index,
        "center": center,
        "center_zscore": payload.get("center_zscore"),
        "selection": selection,
    }
    parameters = {}
    if isinstance(center, Mapping) and center:
        parameters["center"] = center
    for key in ("k", "size", "cluster_index"):
        if _is_finite_number(payload.get(key)):
            parameters[key] = _round(float(payload[key]))
    predictions = [
        {
            "incompatible_with": (
                f"在独立样本上按同一特征与同一 k={_fmt(k)} 重新聚类后，"
                f"无法复现子群 {_fmt(index)} 的中心（中心偏移超过容差）"
                "或成员不一致度超过门槛，则与本假说不相容。"
            ),
            "expectation": "同种划分在重采样下可复现：中心位置与成员集合的重现率高于门槛。",
            "observable": "独立样本上的子群中心坐标与成员集合的重现率。",
        },
        {
            "incompatible_with": (
                "若在独立样本上把 k 增大到 k+1 后该子群的分裂得到一致的、可复现的更优分离度，"
                "说明该子群并非单一结构，则与本假说不相容。"
            ),
            "expectation": "在 k 附近取值时，该子群的分离度不出现可复现的系统性改善。",
            "observable": "k 与 k+1 两处的分离度差及其重采样波动。",
        },
    ]
    nulls = [
        {
            "null_id": "null-no-subgroup",
            "statement": (
                f"k=1：特征 {_features_text(features)} 上不存在子群结构，"
                "观测到的划分仅由随机波动造成。"
            ),
            "rationale": "与主假说直接对立的零假说：无分组结构。",
        },
        {
            "null_id": "null-random-partition",
            "statement": (
                "分区与特征取值无关：把各行随机指派为同等规模的若干组，"
                "即可得到同等或更优的分离度。"
            ),
            "rationale": "排除「任意等规模划分都能达到该分离度」这一平凡解释。",
        },
    ]
    alternatives = [
        {
            "alternative_id": "alt-latent-confounder",
            "statement": (
                "子群差异由未纳入分析的环境或批次变量造成，"
                "而非该特征组合的内在结构。"
            ),
            "distinguishing_observation": (
                "按该未纳入变量分层后重算子群；若分离度消失，该竞争解释被支持；"
                "若分层后分离度保持，则被削弱。"
            ),
            "rationale": "分层是可执行的区分性观测，成本低于新增采样。",
        },
        {
            "alternative_id": "alt-measurement-drift",
            "statement": (
                "子群划分反映测量漂移或仪器批次效应，而非对象本身的差异。"
            ),
            "distinguishing_observation": (
                "按测量时间或仪器批号分层重算中心；若中心随批次整体平移，"
                "该竞争解释被支持。"
            ),
            "rationale": "漂移是可观测的技术性来源，需在确证前排除。",
        },
    ]
    return {
        "statement": statement,
        "model": model,
        "parameters": parameters,
        "predictions": predictions,
        "nulls": nulls,
        "alternatives": alternatives,
    }


def _changepoint_material(view: _PatternView) -> dict:
    """变点模式的假说素材。"""
    payload = view.payload
    threshold = payload.get("threshold")
    order_by = payload.get("order_by") or "给定排序键"
    shift = payload.get("mean_shift")
    position = payload.get("position")
    before = payload.get("before") or {}
    after = payload.get("after") or {}

    statement = (
        f"沿 {order_by} 排序后，在 {_fmt(threshold)} 处存在均值跳变，"
        f"幅度约为 {_fmt(shift)}（左段均值 {_fmt(before.get('mean'))} → "
        f"右段均值 {_fmt(after.get('mean'))}）。"
    )
    model = {
        "model_kind": "piecewise_constant",
        "order_by": order_by,
        "threshold": threshold,
        "position": position,
        "before": before,
        "after": after,
        "segments_total": payload.get("segments_total"),
    }
    parameters = {}
    for key in ("threshold", "position", "mean_shift"):
        if _is_finite_number(payload.get(key)):
            parameters[key] = _round(float(payload[key]))
    predictions = [
        {
            "incompatible_with": (
                "在独立样本上沿同一定序重算，跳变幅度趋零、方向反转，"
                "或跳变位置漂移到与当前阈值不重叠的区间，则与本假说不相容。"
            ),
            "expectation": "跳变位置在独立样本上稳定落在当前阈值附近的容差区间内，且方向一致。",
            "observable": "独立样本上的跳变位置与幅度（含符号）。",
        },
        {
            "incompatible_with": (
                "若移除按当前准则判定的少数极端行后跳变即消失，说明该变点由个别点驱动，"
                "则与本假说不相容。"
            ),
            "expectation": "跳变在剔除极端值后仍然存在且量级相当。",
            "observable": "逐点剔除（leave-one-out）后的跳变幅度序列。",
        },
    ]
    nulls = [
        {
            "null_id": "null-no-changepoint",
            "statement": (
                "该区间内序列为单一平稳段，不存在变点；"
                "检测到的跳变由噪声与选择准则共同造成。"
            ),
            "rationale": "与主假说直接对立的零假说：无分段结构。",
        },
        {
            "null_id": "null-smooth-trend",
            "statement": (
                "序列为连续单调趋势；跳变是把平滑趋势误判为分段取常值的假象。"
            ),
            "rationale": "排除「趋势 + 分段」之间的模型误判这一平凡解释。",
        },
    ]
    alternatives = [
        {
            "alternative_id": "alt-single-outlier",
            "statement": "跳变由单个极端观测（或极少数点）造成，而非系统性分段。",
            "distinguishing_observation": (
                "定位区间边界处的极端点并单独检验；若跳变完全由该点贡献，"
                "该竞争解释被支持。"
            ),
            "rationale": "极端点是最常见的伪变点来源。",
        },
        {
            "alternative_id": "alt-order-coupling",
            "statement": "排序键与采样顺序耦合：跳变反映的是采样过程的变化。",
            "distinguishing_observation": (
                "改用以采集时间以外的排序键重算；若跳变随之移动或消失，"
                "该竞争解释被支持。"
            ),
            "rationale": "排序键的选择本身可能引入人为分段。",
        },
    ]
    return {
        "statement": statement,
        "model": model,
        "parameters": parameters,
        "predictions": predictions,
        "nulls": nulls,
        "alternatives": alternatives,
    }


def _invariant_material(view: _PatternView) -> dict:
    """近似守恒量模式的假说素材。"""
    payload = view.payload
    expression = payload.get("expression") or "该组合量"
    estimate = payload.get("estimate")
    conformity = payload.get("conformity")
    floor = payload.get("min_conformity")
    interval = payload.get("interval")
    counterexamples = payload.get("counterexample_count")

    interval_text = ""
    if isinstance(interval, (list, tuple)) and len(interval) == 2:
        interval_text = f"，允许区间 [{_fmt(interval[0])}, {_fmt(interval[1])}]"

    statement = (
        f"在探索数据上，{expression} 近似守恒：中心估计为 {_fmt(estimate)}"
        f"{interval_text}，一致行占比 {_fmt(conformity)}（门槛 {_fmt(floor)}），"
        f"已记录反例 {_fmt(counterexamples)} 例。"
    )
    model = {
        "model_kind": "near_conserved_quantity",
        "expression": expression,
        "estimate": estimate,
        "interval": interval,
        "core_interval": payload.get("core_interval"),
        "spread": payload.get("spread"),
        "tolerance": payload.get("tolerance"),
        "basis": payload.get("basis"),
        "criterion": payload.get("criterion"),
        "coverage": payload.get("coverage"),
        "n_used": payload.get("n_used"),
        "n_rows": payload.get("n_rows"),
    }
    parameters = {}
    for key in ("estimate", "conformity", "min_conformity", "coverage", "spread"):
        if _is_finite_number(payload.get(key)):
            parameters[key] = _round(float(payload[key]))
    predictions = [
        {
            "incompatible_with": (
                "在独立样本上一致行占比低于门槛，或出现偏差超过容差的**系统性**（方向一致）"
                "反例，则与本假说不相容。"
            ),
            "expectation": "独立样本上一致行占比不低于门槛，且超容差反例不呈系统性方向。",
            "observable": "独立样本上的一致行占比与超容差反例的方向分布。",
        },
        {
            "incompatible_with": (
                "若把容差放大到覆盖该量的全部实际取值范围后「守恒」自然成立，"
                "说明该陈述不携带信息，则与本假说不相容。"
            ),
            "expectation": "容差显著小于该量的实际取值范围（即陈述有约束力）。",
            "observable": "容差与该量取值范围、尺度之比。",
        },
    ]
    nulls = [
        {
            "null_id": "null-no-conservation",
            "statement": (
                f"该量在噪声下无固定中心：{expression} 随行漂移，"
                "观测到的稳定中心是子区间选择与容差设定的产物。"
            ),
            "rationale": "与主假说直接对立的零假说：无守恒中心。",
        },
        {
            "null_id": "null-trivial-bound",
            "statement": (
                "稳定中心仅由该量的天然有界性造成（有界系统的平凡结果），"
                "不代表任何守恒关系。"
            ),
            "rationale": "排除「有界即守恒」这一平凡解释。",
        },
    ]
    alternatives = [
        {
            "alternative_id": "alt-domain-restriction",
            "statement": (
                "守恒性是定义域限制造成的人为筛选：在允许区间之外该量并不守恒。"
            ),
            "distinguishing_observation": (
                "在定义域之外（但仍在物理可达范围内）采样；若大量行偏离该中心，"
                "该竞争解释被支持。"
            ),
            "rationale": "定义域剪裁是最容易被忽视的人工来源。",
        },
        {
            "alternative_id": "alt-arithmetic-identity",
            "statement": (
                "该稳定性由表达式自身的算术恒等式或代数约束造成，"
                "对研究对象不构成经验陈述。"
            ),
            "distinguishing_observation": (
                "对表达式做符号或量纲审查；若稳定性可由恒等式直接推出，"
                "该竞争解释被支持。"
            ),
            "rationale": "恒等式造成的「守恒」不是发现。",
        },
    ]
    return {
        "statement": statement,
        "model": model,
        "parameters": parameters,
        "predictions": predictions,
        "nulls": nulls,
        "alternatives": alternatives,
    }


def _relation_material(view: _PatternView) -> dict:
    """关系模式的假说素材。

    M3 的 ``structure.py`` 不产出 ``relation`` 模式（该种类由 P04 的关系搜索
    声明，见 ``INTERFACES.md`` §2.2 的 ``kind`` 词表）。本模块按「字典形态」
    接收它，因此对载荷键名做宽容读取，不假设固定形状。
    """
    payload = view.payload
    expression = payload.get("expression") or payload.get("name") or "候选关系"
    target = payload.get("target") or payload.get("target_field") or "目标量"
    feature = payload.get("feature")
    coefficients = payload.get("coefficients") or payload.get("parameters") or {}

    feature_text = f"（自变量 {feature}）" if feature else ""
    statement = (
        f"在探索数据上，候选关系 {expression}{feature_text} 对 {target} 的预测"
        "优于常数基线，且超出简单线性基线可解释的范围。"
    )
    model = {
        "model_kind": "relation_candidate",
        "expression": expression,
        "target": target,
        "feature": feature,
        "coefficients": coefficients,
    }
    parameters = dict(coefficients) if isinstance(coefficients, Mapping) else {}
    predictions = [
        {
            "incompatible_with": (
                "在独立样本上该关系的预测误差不低于常数基线（即增益消失或为负），"
                "则与本假说不相容。"
            ),
            "expectation": "独立样本上的预测增益为正，且不低于在探索数据上的量级。",
            "observable": "独立样本上的预测误差与常数基线误差之差。",
        },
        {
            "incompatible_with": (
                "若该关系在独立样本上的增益可由简单线性基线完全复现"
                "（两者差异落在波动范围内），则与本假说不相容。"
            ),
            "expectation": "该关系的增益显著超出简单线性基线。",
            "observable": "该关系与简单线性基线的误差对照。",
        },
    ]
    nulls = [
        {
            "null_id": "null-no-relation",
            "statement": (
                f"{target} 与 {expression} 之间不存在超出常数基线的预测关系。"
            ),
            "rationale": "与主假说直接对立的零假说：无预测关系。",
        },
        {
            "null_id": "null-linear-only",
            "statement": (
                "该关系可被简单线性基线完全解释，额外结构项不贡献任何预测增益。"
            ),
            "rationale": "排除「复杂化的公式写了等于没写」这一平凡情形。",
        },
    ]
    alternatives = [
        {
            "alternative_id": "alt-spurious-trend",
            "statement": "该关系是共同趋势造成的伪相关，而非变量间的稳定依赖。",
            "distinguishing_observation": (
                "在时间或批次上分层后重算；若关系在层内消失，该竞争解释被支持。"
            ),
            "rationale": "共同趋势是伪相关的常见来源。",
        },
        {
            "alternative_id": "alt-missing-variable",
            "statement": "该关系由未纳入的第三个变量驱动，属遗漏变量造成的替代关系。",
            "distinguishing_observation": (
                "纳入该候选变量后重算；若原关系的增益被吸收，该竞争解释被支持。"
            ),
            "rationale": "遗漏变量会制造看似可用的替代关系。",
        },
    ]
    return {
        "statement": statement,
        "model": model,
        "parameters": parameters,
        "predictions": predictions,
        "nulls": nulls,
        "alternatives": alternatives,
    }


#: 模式种类 → 素材构造函数。四个种类与 :data:`SUPPORTED_PATTERN_KINDS` 一一对应。
_MATERIAL_BUILDERS: Mapping[str, Callable[[_PatternView], dict]] = {
    "cluster": _cluster_material,
    "changepoint": _changepoint_material,
    "invariant": _invariant_material,
    "relation": _relation_material,
}


# ---------------------------------------------------------------------------
# 表示声明的缺省口径（按模式种类分派，可被调用方覆盖）
# ---------------------------------------------------------------------------


def _representation_for(kind: str, override: Any) -> dict:
    """给出该模式的**完整变换**声明。

    ``INTERFACES.md`` §2.3 要求 ``representation`` 写全缺失值处理与标准化，
    以免「只冻结公式、却继续修改数据处理」。本模块给出的缺省声明
    **跟随检测器的实际做法**（例如 M3 的聚类在标准化后的空间上进行，
    因此其标准化声明为 z-score），并允许调用方整体覆盖。

    缺省声明是**显式写出的声明**，不是「留空由下游补」——留空会被
    P07 的校验直接拒绝。
    """
    if override is not None:
        if not isinstance(override, Mapping):
            raise GenerateError(
                f"representation 必须是映射，实际得到：{type(override).__name__}"
            )
        return dict(override)

    missing_value_handling = "drop_rows_with_missing_required_fields"
    if kind == "cluster":
        standardization = "zscore_on_E_fit_only"
    elif kind == "changepoint":
        standardization = "none_declared_for_ordering_key"
    elif kind == "invariant":
        standardization = "none_declared_raw_scale_tolerance"
    else:
        standardization = "none_declared_candidate_expression"
    return {
        "missing_value_handling": missing_value_handling,
        "standardization": standardization,
        "note": (
            "缺省声明随模式种类的检测器口径给出；调用方可整体覆盖。"
            "本模块不据此执行任何变换，也不据此计算任何指标。"
        ),
    }


# ---------------------------------------------------------------------------
# 外部文本填充（LLM 仅作字段填充）
# ---------------------------------------------------------------------------


def _apply_field_fill(
    payload_text: Mapping[str, Any],
    enricher: Callable[[dict], Any] | None,
    *,
    context: dict,
) -> tuple[dict, dict | None]:
    """调用外部文本生成器并把结果限制在受控文本字段内。

    :param payload_text: 待填充的可变文本字典（``statement`` / ``alternatives``）。
    :param enricher: 外部文本生成器；``None`` 表示不使用。
    :param context: 传给生成器的**只读**上下文（仅含模式标识、种类、结构化陈述
        与预测文本；不含任何数据正文、令牌或质量报告）。
    :return: ``(填充后的文本字典, 追溯记录或 None)``。
    :raises FieldFillError: 输出不是映射、含未知键、含证据等级类键名，
        或含空文本。
    """
    if enricher is None:
        return dict(payload_text), None

    raw = enricher(dict(context))
    if not isinstance(raw, Mapping):
        raise FieldFillError(
            f"text_enricher 必须返回映射，实际得到：{type(raw).__name__}"
        )

    # 两道关卡的顺序是刻意的：先查「证据等级类键名」再查白名单，
    # 使违反「文本不得授予证据等级」这一硬约束时给出**专门**的错误信息，
    # 而不是被淹没在泛化的「未知键」报错里。
    # 该检查正在保护 :data:`EVIDENCE_GRADE_KEY_HINTS` 中的每一个键名：
    # 白名单与提示词表**可能**随 P09+ 的字段扩展而变动，
    # 但只要键名还在 :data:`EVIDENCE_GRADE_KEY_HINTS` 里，这道关卡就独立成立。
    raw_keys = {str(key) for key in raw}
    lowered = {key.strip().lower() for key in raw_keys}
    graded = sorted(lowered & EVIDENCE_GRADE_KEY_HINTS)
    if graded:
        raise FieldFillError(
            "text_enricher 的输出包含证据等级 / 置信度类键名："
            + ", ".join(repr(key) for key in graded)
            + "；文本仅作字段填充，不得据此授予任何证据等级"
        )

    unknown = sorted(raw_keys - LLM_FIELD_FILL_ALLOWED_KEYS)
    if unknown:
        raise FieldFillError(
            "text_enricher 的输出包含不允许的键："
            + ", ".join(repr(key) for key in unknown)
            + "；允许的键为 " + ", ".join(sorted(LLM_FIELD_FILL_ALLOWED_KEYS))
        )

    filled = dict(payload_text)
    accepted: dict[str, Any] = {}
    if "statement" in raw:
        text = raw["statement"]
        if not isinstance(text, str) or not text.strip():
            raise FieldFillError("text_enricher.statement 必须是非空字符串")
        filled["statement"] = text
        accepted["statement"] = text

    if "plain_explanation" in raw:
        text = raw["plain_explanation"]
        if not isinstance(text, str) or not text.strip():
            raise FieldFillError("text_enricher.plain_explanation 必须是非空字符串")
        accepted["plain_explanation"] = text

    if "alternatives" in raw:
        values = raw["alternatives"]
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
            raise FieldFillError("text_enricher.alternatives 必须是非空序列")
        texts: list[str] = []
        for index, item in enumerate(values):
            if not isinstance(item, str) or not item.strip():
                raise FieldFillError(
                    f"text_enricher.alternatives[{index}] 必须是非空字符串"
                )
            texts.append(item)
        accepted["alternatives"] = texts

    trace = {
        "used": True,
        "allowed_fields": sorted(accepted),
        "text_digest": _digest(accepted),
        "note": (
            "外部文本仅用于字段填充；不据此授予任何证据等级，"
            "证据等级属 P15 的发现档案。"
        ),
        "context_keys": sorted(context),
    }
    for key in ("model", "model_version", "provider", "prompt_digest"):
        value = getattr(enricher, key, None)
        if isinstance(value, str) and value.strip():
            trace[key] = value
    return filled, trace


# ---------------------------------------------------------------------------
# 构造单条假说载荷
# ---------------------------------------------------------------------------


def _resolve_data_ids(
    view: _PatternView,
    explicit: Sequence[str] | None,
) -> list[str]:
    """决定该假说的 ``provenance.data_ids``。

    ``explicit`` 优先（调用方按协议给出的探索分区引用）；否则回退到模式自身
    来源信息中的引用标识。两者皆无时**报错**——来源不明就不得构造假说，
    这与 P07 对 ``provenance`` 的立场一致。

    :raises PatternInputError: 无法确定任何数据引用标识。
    """
    if explicit is not None:
        if isinstance(explicit, (str, bytes)) or not isinstance(explicit, Sequence):
            raise GenerateError("data_ids 必须是字符串序列，而不是单个字符串")
        cleaned = [item for item in explicit if isinstance(item, str) and item.strip()]
        if not cleaned:
            raise GenerateError("data_ids 不能为空序列")
        return list(cleaned)

    refs = _data_refs_of(view)
    if refs:
        return refs
    raise PatternInputError(
        f"无法为模式 {view.pattern_id!r} 确定数据引用：其来源信息中没有 "
        "source_ref / data_refs / sample_fingerprint；"
        "请显式传入 data_ids，或让模式携带可追溯的数据引用。"
        "（不提供缺省值——来源不明不得构造假说）"
    )


def _build_payload(
    view: _PatternView,
    *,
    knowledge_version: str,
    code_version: str,
    scope: Mapping[str, Any],
    explicit_scope: bool,
    representation: Any,
    data_ids: Sequence[str] | None,
    budget: GenerateBudget,
    text_enricher: Callable[[dict], Any] | None,
) -> dict:
    """由一条模式构造一条假说的**序列化载荷**（尚未实例化为对象）。

    先把载荷构造完整再实例化，是为了让去重能在**对象创建之前**完成，
    从而避免「同一内容被实例化两次、产生两个相同标识的对象」。
    """
    material = _MATERIAL_BUILDERS[view.kind](view)

    statement = material["statement"]
    alternatives = [dict(item) for item in material["alternatives"]]

    text_box = {
        "statement": statement,
        "alternatives": [item["statement"] for item in alternatives],
    }
    context = {
        "pattern_id": view.pattern_id,
        "pattern_kind": view.kind,
        "structured_statement": statement,
        "predictions": [item["incompatible_with"] for item in material["predictions"]],
        "null_statements": [item["statement"] for item in material["nulls"]],
    }
    if text_enricher is not None:
        filled, trace = _apply_field_fill(text_box, text_enricher, context=context)
        statement = filled["statement"]
        if "alternatives" in filled:
            texts = list(filled["alternatives"])
            for index, item in enumerate(alternatives):
                if index < len(texts):
                    item["statement"] = texts[index]
    else:
        trace = None

    # 零假说与竞争解释按预算截断；截断顺序固定（按生成顺序），确保可复现。
    nulls = [dict(item) for item in material["nulls"]][: budget.max_nulls]
    alternatives = alternatives[: budget.max_alternatives]
    predictions = [dict(item) for item in material["predictions"]][
        : budget.max_predictions
    ]

    refs = _resolve_data_ids(view, data_ids)

    provenance: dict[str, Any] = {
        "data_ids": refs,
        "knowledge_version": knowledge_version,
        "generator": GENERATOR_ID,
        "code_version": code_version,
        # 追溯信息：模式标识与种类、生成口径版本、检测器标识
        "pattern_ids": [view.pattern_id],
        "pattern_kinds": [view.kind],
        "detector": view.provenance.get("detector"),
        "pattern_module_version": view.provenance.get("module_version"),
        "pattern_sample_fingerprint": view.provenance.get("sample_fingerprint"),
        "pattern_stability": view.stability,
        "pattern_metrics": _plain(view.metrics),
        "scope_declared": explicit_scope,
        "pool_bound": {
            "max_hypotheses": budget.max_hypotheses,
            "note": CONTRACT_POOL_NOTE,
        },
        "generate_version": GENERATE_VERSION,
        "evidence_grade_note": (
            "本假说为探索阶段草稿（status=draft，仅生命周期标记）；"
            "未授予任何证据等级，证据等级属 P15 的发现档案。"
        ),
    }
    if trace is not None:
        provenance["field_fill"] = trace

    fit = material.get("parameters") or {}
    payload = {
        "id": f"h-{view.kind}-{_digest({'kind': view.kind, 'payload': view.payload})}",
        "version": "1",
        "parent_ids": [],
        "type": KIND_TO_HYPOTHESIS_TYPE[view.kind],
        "statement": statement,
        "scope": dict(scope),
        "representation": _representation_for(view.kind, representation),
        "model": material["model"],
        "fitted_parameters": fit,
        "predictions": predictions,
        "null_hypotheses": nulls,
        "alternatives": alternatives,
        "assumptions": [
            {
                "name": "探索阶段草稿",
                "justification": (
                    "本假说由 M3 模式经 P08 机械派生，用于探索与开发评估；"
                    "不构成确证结论。"
                ),
            },
            {
                "name": "完整变换已声明",
                "justification": (
                    "representation 已写明缺失值处理与标准化，"
                    "冻结后不得再修改数据处理（框架说明 §3）。"
                ),
            },
        ],
        "complexity": None,
        "provenance": provenance,
        "development": None,
        "confirmation_plan": None,
        "evidence_log": [],
        "status": "draft",
    }
    return payload


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


#: 内容判等时恒被排除的字段。
#: ``id`` 由内容派生，必然一致；``provenance`` 携带追溯信息（模式标识、
#: 合并记录、池上界说明），把它纳入判等会让「内容相同、来源不同」的条目
#: 无法合并——而这正是本模块要去除的重复来源。
_DEDUP_EXCLUDED_FIELDS: frozenset[str] = frozenset({"id", "provenance"})

#: 骨架判等时**额外**排除的字段。
#: ``statement`` 是把参数值渲染成文字的陈述句（例如「中心约为 X1=1」），
#: 数值以字符串形式嵌入，骨架化对它无效。若把它纳入骨架键，
#: 「同结构、仅常数不同」的参数微变体将永远不会被判为重复，
#: 骨架去重形同虚设。结构性字段（``model`` / ``fitted_parameters`` /
#: ``predictions`` / ``null_hypotheses`` / ``alternatives``）已足以承载骨架。
_SKELETON_EXTRA_EXCLUDED_FIELDS: frozenset[str] = frozenset({"statement"})


def _content_key(payload: Mapping[str, Any]) -> str:
    """内容判等键：量化数值后比较，忽略标识与来源追溯信息。"""
    stripped = {
        key: payload[key] for key in payload if key not in _DEDUP_EXCLUDED_FIELDS
    }
    return _canonical(_quantize(stripped))


def _skeleton_key(payload: Mapping[str, Any]) -> str:
    """结构骨架判等键：把数值折叠为占位符后再比较。

    除 :data:`_DEDUP_EXCLUDED_FIELDS` 外，另排除
    :data:`_SKELETON_EXTRA_EXCLUDED_FIELDS`（见其说明）。
    结果只保留「结构与文本骨架」，因此同结构、仅常数不同的假说共享同一键。
    """
    excluded = _DEDUP_EXCLUDED_FIELDS | _SKELETON_EXTRA_EXCLUDED_FIELDS
    stripped = {key: payload[key] for key in payload if key not in excluded}
    return _canonical(_skeletonize(stripped))


def _dedup_payloads(
    payloads: Sequence[dict],
    *,
    mode: str,
) -> tuple[list[dict], int, int]:
    """对假说载荷去重，返回 ``(去重后的载荷, 精确合并数, 骨架合并数)``。

    保留规则是**纯确定性**的：按 ``pattern_ids`` 的首项字典序排序后，
    先到者保留，后来者并入其 ``provenance["merged_pattern_ids"]``。
    刻意**不**依据任何指标择优——择优属 P10（Pareto 筛选）。

    :raises GenerateError: ``mode`` 不在 :data:`DEDUP_MODES` 内。
    """
    if mode not in DEDUP_MODES:
        raise GenerateError(
            f"dedup_mode 必须是 {' / '.join(DEDUP_MODES)} 之一，实际得到：{mode!r}"
        )

    ordered = sorted(payloads, key=lambda item: _canonical(item["provenance"]["pattern_ids"]))

    kept: list[dict] = []
    exact_index: dict[str, dict] = {}
    skeleton_index: dict[str, dict] = {}
    exact_merges = 0
    skeleton_merges = 0

    for payload in ordered:
        content = _content_key(payload)
        if content in exact_index:
            _merge_into(exact_index[content], payload)
            exact_merges += 1
            continue
        if mode == "skeleton":
            skeleton = _skeleton_key(payload)
            if skeleton in skeleton_index:
                _merge_into(skeleton_index[skeleton], payload)
                skeleton_merges += 1
                continue
            skeleton_index[skeleton] = payload
        exact_index[content] = payload
        kept.append(payload)

    for payload in kept:
        provenance = payload["provenance"]
        provenance["dedup_mode"] = mode
        provenance["merged_pattern_ids"] = sorted(
            set(provenance["pattern_ids"]) | set(provenance.get("merged_pattern_ids", []))
        )
        provenance["merged_pattern_count"] = len(provenance["merged_pattern_ids"])
    return kept, exact_merges, skeleton_merges


def _merge_into(kept: dict, merged: Mapping[str, Any]) -> None:
    """把被合并载荷的模式标识并入保留载荷的来源信息。"""
    provenance = kept["provenance"]
    provenance.setdefault("merged_pattern_ids", [])
    provenance["merged_pattern_ids"] = sorted(
        set(provenance["merged_pattern_ids"])
        | set(merged["provenance"]["pattern_ids"])
    )


# ---------------------------------------------------------------------------
# 池容器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedHypothesisPool:
    """一次模式→假说转换的完整结果。

    :param hypotheses: 经去重与裁剪的假说元组，顺序**确定**（先按
        ``pattern_ids`` 首项、再按 ``id`` 排序）。
    :param budget: 本次使用的预算。
    :param provenance: 生成口径与计数摘要（不含任何数据正文）。
    """

    hypotheses: tuple[Hypothesis, ...]
    budget: GenerateBudget
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.hypotheses)

    def __iter__(self) -> Iterator[Hypothesis]:
        return iter(self.hypotheses)

    def __contains__(self, identity: object) -> bool:
        return isinstance(identity, str) and any(
            item.identity == identity for item in self.hypotheses
        )

    def ids(self) -> tuple[str, ...]:
        """池中全部假说 id（按池内顺序）。"""
        return tuple(item.id for item in self.hypotheses)

    def by_id(self, hypothesis_id: str) -> Hypothesis:
        """按 id 取假说。

        :raises GenerateError: id 不在池中。
        """
        for item in self.hypotheses:
            if item.id == hypothesis_id:
                return item
        raise GenerateError(f"池中不存在假说 id：{hypothesis_id!r}")

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "generate_version": GENERATE_VERSION,
            "size": len(self.hypotheses),
            "budget": {
                "max_hypotheses": self.budget.max_hypotheses,
                "max_nulls": self.budget.max_nulls,
                "max_alternatives": self.budget.max_alternatives,
                "max_predictions": self.budget.max_predictions,
            },
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "provenance": _plain(self.provenance),
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本，用于稳定比对与指纹。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """池内容摘要（规范式 JSON 的 SHA-256 十六进制串）。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_store(self, store: HypothesisStore | None = None) -> HypothesisStore:
        """把池内假说登记进 P07 的只追加版本仓库并返回该仓库。

        这是与 P07 的**版本链**对接的入口：登记后的假说可通过
        :meth:`~sdl_m04.hypothesis.HypothesisStore.revise` 派生新版本，
        而池内对象本身保持不变。

        :raises HypothesisValidationError: 仓库中已存在同标识版本
            （由 P07 的只追加保护抛出，如实传递）。
        """
        target = HypothesisStore() if store is None else store
        for item in self.hypotheses:
            target.register(item)
        return target


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def hypotheses_from_patterns(
    patterns: Iterable[Any],
    *,
    knowledge_version: str,
    scope: Mapping[str, Any] | None = None,
    representation: Mapping[str, Any] | None = None,
    data_ids: Sequence[str] | None = None,
    code_version: str = GENERATE_VERSION,
    budget: GenerateBudget | Mapping[str, Any] | None = None,
    dedup_mode: str = "exact",
    text_enricher: Callable[[dict], Any] | None = None,
) -> GeneratedHypothesisPool:
    """把 M3 的模式序列转成**受预算约束、经去重**的假说池（验收标准 ①②③）。

    流程（顺序固定，确保可复现）::

        规范化模式 → 按 pattern_id 去重 → 逐条构造假说载荷
        → 两级内容去重 → 按预算裁剪 → 实例化（P07 校验）

    :param patterns: 模式序列；每项可以是模式对象（鸭子类型）或 ``to_dict()``
        产物的字典。
    :param knowledge_version: **必填**，新颖性所相对的知识库版本 $K^{(v)}$。
        无缺省值：P09 要用它绑定新颖性口径，凭空取默认值会让该绑定失去意义。
    :param scope: 假说的作用域（对象、环境、时间、排除条件）。缺省为空映射，
        即「未限定」——该状态会被记录在来源信息中，不会被伪装成已限定。
    :param representation: 覆盖缺省的完整变换声明（缺省声明随模式种类给出）。
    :param data_ids: 覆盖假说的数据引用标识。缺省时从模式自身的来源信息派生；
        两者皆无时**报错**，不提供缺省值。
    :param code_version: 生成代码版本，写入 ``provenance.code_version``。
    :param budget: :class:`GenerateBudget` 或字段字典。
    :param dedup_mode: ``"exact"``（默认）或 ``"skeleton"``，见模块文档 ③。
    :param text_enricher: 可选的外部文本生成器（例如 LLM）。其输出仅用于填充
        受控文本字段，且被记入 ``provenance.field_fill`` 以便追溯。
    :return: :class:`GeneratedHypothesisPool`。
    :raises PatternInputError: 模式形态不合法、种类未知或来源引用缺失。
    :raises FieldFillError: 外部文本违反填充约束。
    :raises GenerateError: 参数不合法。
    """
    if not isinstance(knowledge_version, str) or not knowledge_version.strip():
        raise GenerateError(
            "knowledge_version 是必填字段且不能为空："
            "新颖性必须绑定到明确的知识库版本 K^(v)"
        )
    if not isinstance(code_version, str) or not code_version.strip():
        raise GenerateError("code_version 必须是非空字符串")
    if scope is not None and not isinstance(scope, Mapping):
        raise GenerateError(f"scope 必须是映射或 None，实际得到：{type(scope).__name__}")
    if text_enricher is not None and not callable(text_enricher):
        raise GenerateError("text_enricher 必须是可调用对象或 None")

    box = _as_budget(budget)
    scope_mapping = dict(scope) if scope else {}

    views: list[_PatternView] = []
    seen_pattern_ids: list[str] = []
    pattern_duplicates = 0
    for index, pattern in enumerate(patterns):
        view = _as_pattern_view(pattern, index)
        if view.pattern_id in seen_pattern_ids:
            # 同一 pattern_id 重复出现：内容必然相同（标识由内容派生），
            # 只保留一份，避免后续实例化出两个同标识对象。
            pattern_duplicates += 1
            continue
        seen_pattern_ids.append(view.pattern_id)
        views.append(view)

    if not views:
        raise PatternInputError("patterns 为空：没有可转换的模式")

    payloads = [
        _build_payload(
            view,
            knowledge_version=knowledge_version,
            code_version=code_version,
            scope=scope_mapping,
            explicit_scope=bool(scope),
            representation=representation,
            data_ids=data_ids,
            budget=box,
            text_enricher=text_enricher,
        )
        for view in views
    ]

    deduped, exact_merges, skeleton_merges = _dedup_payloads(payloads, mode=dedup_mode)
    given = len(deduped)
    capped = deduped[: box.max_hypotheses]

    hypotheses = tuple(Hypothesis.from_dict(item) for item in capped)

    provenance = {
        "origin": "hypotheses_from_patterns",
        "generator": GENERATOR_ID,
        "generate_version": GENERATE_VERSION,
        "code_version": code_version,
        "knowledge_version": knowledge_version,
        "dedup_mode": dedup_mode,
        "pool_basis": CONTRACT_POOL_NOTE,
        "scope_declared": bool(scope),
        "representation_source": (
            "caller_override" if representation is not None else "generator_default_by_kind"
        ),
        "data_ids_source": (
            "caller_explicit" if data_ids is not None else "pattern_provenance"
        ),
        "used_text_enricher": text_enricher is not None,
        "counts": {
            "patterns_input": len(seen_pattern_ids) + pattern_duplicates,
            "patterns_unique": len(views),
            "pattern_duplicates_dropped": pattern_duplicates,
            "hypotheses_built": len(payloads),
            "exact_merges": exact_merges,
            "skeleton_merges": skeleton_merges,
            "after_dedup": given,
            "capped_away": given - len(capped),
            "final": len(capped),
        },
        "access_role": "explorer",
        "evidence_grade_note": (
            "本池为探索阶段草稿池；池大小由预算约束，池内条目未授予任何证据等级。"
        ),
    }
    return GeneratedHypothesisPool(
        hypotheses=hypotheses, budget=box, provenance=provenance
    )


# ---------------------------------------------------------------------------
# 补齐零假说与竞争解释
# ---------------------------------------------------------------------------

#: 假说类型 → 该类假说缺失零/竞争解释时的**派生用**文本。
#: 注意：这里的文本是按类型给出的**通用反推**，不是对具体数据的断言。
_NULL_BY_TYPE: Mapping[str, tuple[tuple[str, str], ...]] = {
    "cluster": (
        (
            "null-no-subgroup",
            "零假说：所声明的分组结构不存在，观测到的划分与随机波动不可区分。",
        ),
        (
            "null-random-partition",
            "零假说：把样本随机指派为同等规模的若干组即可得到同等分离度。",
        ),
    ),
    "changepoint": (
        ("null-no-changepoint", "零假说：所声明的区间内为单一平稳段，不存在变点。"),
        (
            "null-smooth-trend",
            "零假说：序列为连续趋势，所声明的不连续点是分段拟合的假象。",
        ),
    ),
    "invariant": (
        (
            "null-no-conservation",
            "零假说：所声明的量没有固定中心，观测到的稳定性来自区间选择与容差设定。",
        ),
        (
            "null-trivial-bound",
            "零假说：观测到的稳定中心仅由该量的天然有界性造成，不代表守恒关系。",
        ),
    ),
    "relation": (
        ("null-no-relation", "零假说：所声明的变量与目标之间不存在超出常数基线的预测关系。"),
        ("null-linear-only", "零假说：所声明的关系可被简单线性基线完全解释。"),
    ),
}

#: 假说类型 → 该类假说缺失竞争解释时的通用候选。
_ALTERNATIVE_BY_TYPE: Mapping[str, tuple[dict, ...]] = {
    "cluster": (
        {
            "alternative_id": "alt-latent-confounder",
            "statement": "竞争解释：分组差异由未纳入分析的环境或批次变量造成。",
            "distinguishing_observation": "按该变量分层后重算；若分离度消失则本解释被支持。",
            "rationale": "分层是可执行的区分性观测。",
        },
    ),
    "changepoint": (
        {
            "alternative_id": "alt-single-outlier",
            "statement": "竞争解释：跳变由个别极端观测造成，而非系统性分段。",
            "distinguishing_observation": "逐点剔除后重算；若跳变消失则本解释被支持。",
            "rationale": "极端点是最常见的伪变点来源。",
        },
    ),
    "invariant": (
        {
            "alternative_id": "alt-domain-restriction",
            "statement": "竞争解释：稳定性来自定义域限制造成的人为筛选。",
            "distinguishing_observation": "在定义域之外采样；若大量行偏离则该解释被支持。",
            "rationale": "定义域剪裁容易被忽视。",
        },
        {
            "alternative_id": "alt-arithmetic-identity",
            "statement": "竞争解释：稳定性由表达式自身的算术恒等式造成，不构成经验陈述。",
            "distinguishing_observation": "做符号或量纲审查；若可由恒等式推出则该解释被支持。",
            "rationale": "恒等式造成的守恒不是发现。",
        },
    ),
    "relation": (
        {
            "alternative_id": "alt-spurious-trend",
            "statement": "竞争解释：该关系是共同趋势造成的伪相关。",
            "distinguishing_observation": "按时间或批次分层后重算；若关系消失则本解释被支持。",
            "rationale": "共同趋势是伪相关的常见来源。",
        },
    ),
}

#: 兜底：假说类型不在上表内（例如 ``composite``）时使用的通用条目。
_GENERIC_NULL: tuple[tuple[str, str], ...] = (
    (
        "null-no-effect",
        "零假说：所声明的结构或关系不存在，观测到的规律与随机波动不可区分。",
    ),
)

_GENERIC_ALTERNATIVE: tuple[dict, ...] = (
    {
        "alternative_id": "alt-unspecified-confounder",
        "statement": "竞争解释：观测到的规律由未纳入的第三因素造成，而非所声明的结构。",
        "distinguishing_observation": "纳入候选第三因素后重算；若规律被吸收则本解释被支持。",
        "rationale": "遗漏因素是替代解释的默认来源，需在确证前排除。",
    },
)


def _as_entry_list(value: Any, label: str) -> list[Any]:
    """把条目输入规范化为列表（拒绝字符串被当成条目序列）。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GenerateError(
            f"{label} 必须是条目序列（每个条目为字符串或非空映射），"
            f"实际得到：{type(value).__name__}"
        )
    items = list(value)
    if not items:
        raise GenerateError(f"{label} 不能为空序列")
    return items


def _entries_summary(entries: Sequence[Any]) -> list[Any]:
    """把条目归一为可比较的普通结构（用于幂等判定）。"""
    return _plain(list(entries))


def _attach(
    hypothesis: Hypothesis,
    *,
    field_name: str,
    entries: Sequence[Any],
    derived: Sequence[Any],
    mode: str,
) -> Hypothesis:
    """把条目写入假说的指定字段，经版本链产生新版本。

    为什么必须经版本链：P07 的契约规定「已发布版本不可原地修改，修改需生成新版本」。
    本模块不另辟蹊径，一律走
    :meth:`~sdl_m04.hypothesis.Hypothesis.new_version`，使补齐动作本身
    也留下 ``parent_ids`` 追溯痕迹。

    幂等性：若目标字段的当前内容与待写入内容**完全一致**，直接返回原对象，
    不产生无意义的新版本。

    :raises GenerateError: 条目为空或不是序列。
    :raises sdl_m04.hypothesis.HypothesisImmutabilityError: 假说已归档（终态）。
    """
    if mode not in DEDUP_MODES:
        raise GenerateError(f"未知的 mode：{mode!r}")
    items = list(entries) if entries is not None else list(derived)
    if not items:
        raise GenerateError(f"{field_name} 不能为空：至少需要一条")

    current = _entries_summary(getattr(hypothesis, field_name))
    if current == _entries_summary(items):
        return hypothesis

    if hypothesis.is_retired:
        raise HypothesisValidationError(
            f"{hypothesis.identity} 已归档，是终态，不可再补齐{field_name}"
        )
    return hypothesis.new_version(changes={field_name: items})


def attach_nulls(
    hypothesis: Hypothesis,
    nulls: Sequence[Any] | None = None,
    *,
    max_nulls: int = DEFAULT_MAX_NULLS,
) -> Hypothesis:
    """为假说补齐（或替换）零假说，返回**新版本**（验收标准 ②）。

    ``nulls`` 缺省时，按假说自身的 ``type`` 从其**已声明的结构**反推通用零假说
    （见 :data:`_NULL_BY_TYPE`）。这是「反推」而非「凭空添加数据结论」：
    零假说只否定已声明的结构，不引入任何新的经验断言。

    :param hypothesis: 待补齐的假说（P07 对象）。
    :param nulls: 显式给出的零假说条目；``None`` 表示按类型反推。
        条目可以是字符串，也可以是含 ``null_id`` / ``statement`` / ``rationale``
        的非空映射。
    :param max_nulls: 条数上界。
    :return: 含零假说的假说对象；内容未变时返回原对象。
    :raises GenerateError: 条目为空或形态非法。
    :raises HypothesisValidationError: 不是 P07 的假说对象，或假说已归档。
    """
    if not isinstance(hypothesis, Hypothesis):
        raise HypothesisValidationError(
            f"attach_nulls 只接受 P07 的 Hypothesis 对象，"
            f"实际得到：{type(hypothesis).__name__}"
        )
    if isinstance(max_nulls, bool) or not isinstance(max_nulls, int) or max_nulls < 1:
        raise GenerateError(f"max_nulls 必须是正整数，实际得到：{max_nulls!r}")

    if nulls is None:
        pair = _NULL_BY_TYPE.get(hypothesis.type, _GENERIC_NULL)
        derived: list[Any] = [
            {
                "null_id": null_id,
                "statement": statement,
                "rationale": (
                    "由假说自身已声明的结构反推得到；不引入新的经验断言，"
                    "不据此授予任何证据等级。"
                ),
            }
            for null_id, statement in pair
        ]
    else:
        items = _as_entry_list(nulls, "nulls")
        for index, item in enumerate(items):
            if isinstance(item, str):
                if not item.strip():
                    raise GenerateError(f"nulls[{index}] 不能是空白字符串")
            elif isinstance(item, Mapping):
                if not item:
                    raise GenerateError(f"nulls[{index}] 不能是空映射")
                if "statement" not in item:
                    raise GenerateError(
                        f"nulls[{index}] 必须含 'statement' 键（零假说必须可陈述）"
                    )
            else:
                raise GenerateError(
                    f"nulls[{index}] 必须是字符串或映射，"
                    f"实际得到：{type(item).__name__}"
                )
        derived = items

    return _attach(
        hypothesis,
        field_name="null_hypotheses",
        entries=derived[:max_nulls],
        derived=derived[:max_nulls],
        mode="exact",
    )


def attach_alternatives(
    hypothesis: Hypothesis,
    alternatives: Sequence[Any] | None = None,
    *,
    max_alternatives: int = DEFAULT_MAX_ALTERNATIVES,
) -> Hypothesis:
    """为假说补齐（或替换）竞争解释，返回**新版本**。

    ``alternatives`` 缺省时按假说 ``type`` 给出通用竞争解释
    （见 :data:`_ALTERNATIVE_BY_TYPE`）。每条竞争解释都应尽量带
    ``distinguishing_observation``（区分性观测），以对齐 M7 的主动取证需求：
    竞争解释若无法区分，对取证没有指导价值。

    :param hypothesis: 待补齐的假说（P07 对象）。
    :param alternatives: 显式给出的竞争解释条目；``None`` 表示按类型给出。
    :param max_alternatives: 条数上界。
    :return: 含竞争解释的假说对象；内容未变时返回原对象。
    :raises GenerateError: 条目为空或形态非法。
    :raises HypothesisValidationError: 不是 P07 的假说对象，或假说已归档。
    """
    if not isinstance(hypothesis, Hypothesis):
        raise HypothesisValidationError(
            f"attach_alternatives 只接受 P07 的 Hypothesis 对象，"
            f"实际得到：{type(hypothesis).__name__}"
        )
    if (
        isinstance(max_alternatives, bool)
        or not isinstance(max_alternatives, int)
        or max_alternatives < 1
    ):
        raise GenerateError(
            f"max_alternatives 必须是正整数，实际得到：{max_alternatives!r}"
        )

    if alternatives is None:
        pairs = _ALTERNATIVE_BY_TYPE.get(hypothesis.type, _GENERIC_ALTERNATIVE)
        derived: list[Any] = [dict(item) for item in pairs]
    else:
        items = _as_entry_list(alternatives, "alternatives")
        for index, item in enumerate(items):
            if isinstance(item, str):
                if not item.strip():
                    raise GenerateError(f"alternatives[{index}] 不能是空白字符串")
            elif isinstance(item, Mapping):
                if not item:
                    raise GenerateError(f"alternatives[{index}] 不能是空映射")
                if "statement" not in item:
                    raise GenerateError(
                        f"alternatives[{index}] 必须含 'statement' 键"
                        "（竞争解释必须可陈述）"
                    )
            else:
                raise GenerateError(
                    f"alternatives[{index}] 必须是字符串或映射，"
                    f"实际得到：{type(item).__name__}"
                )
        derived = items

    return _attach(
        hypothesis,
        field_name="alternatives",
        entries=derived[:max_alternatives],
        derived=derived[:max_alternatives],
        mode="exact",
    )


# 说明：``HYPOTHESIS_TYPES`` 在本模块内被引用以明确「模式种类必须落在假说类型
# 词表内」这一不变量；若 P07 的词表发生变更，本模块会在导入期即暴露不一致。
# 该断言刻意写成模块级检查而非运行时断言，以保证它在任何调用路径下都被执行。
_MISSING_TYPES = sorted(set(KIND_TO_HYPOTHESIS_TYPE.values()) - set(HYPOTHESIS_TYPES))
if _MISSING_TYPES:  # pragma: no cover - 仅在跨模块契约不一致时触发
    raise GenerateError(
        "模式种类到假说类型的映射与 P07 的词表不一致："
        + ", ".join(_MISSING_TYPES)
    )
