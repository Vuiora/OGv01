"""M3 结构模式搜索（P05 交付物）。

本模块在探索分区 E 的样本之上搜索**结构模式**：子群（聚类）、阈值（变点）与
近似守恒量（不变量）。它是框架说明 §2「结构模式」行的可调用骨架：

    按任务调用聚类、变点、周期或不变量检测；检查重采样下的结构稳定性

本阶段只做**检测本身**。重采样稳定性评估属 P06（``sdl_m03/stability.py``）：
因此本模块输出的 :class:`StructurePattern` 的 ``stability`` 字段**恒为 ``None``**，
不计算、不猜测、不用任何代理量冒充稳定性分数。

公开接口
--------

- :func:`find_clusters`：子群检测（标准化空间中的简化 k-means + 简化轮廓系数选 k）。
- :func:`find_changepoints`：阈值／变点检测（按序序列上的二分分割 + BIC 式惩罚）。
- :func:`check_invariant`：近似守恒量检测（一致行占比判据 + 反例清单）。

另有支撑公开接口：:func:`cluster_labels`、:func:`select_cluster_count`、
:func:`silhouette_score`、:func:`changepoint_positions`，用于测试与人工复核。

三条验收标准与实现对应关系
--------------------------

① **固定 seed 下可复现**：三个检测函数都是纯函数，不使用任何模块级可变状态；
   唯一涉及随机性的聚类初始化使用**局部** ``random.Random(seed)`` 实例
   （不触碰全局随机状态），且聚类中心在返回前按中心坐标排序、
   分量按簇大小与成员标识排序，使输出与「运行顺序」「字典/集合迭代顺序」无关。
   同一 seed、同一输入恒得逐字节相同的 ``to_dict()``。

② **纯标准库简化实现**：仅使用 ``math`` / ``random`` / ``json`` / ``hashlib``
   与标准库数据结构，**不引入 numpy、sklearn 或任何第三方依赖**。
   算法刻意选用可读、可核验的简化版本（Lloyd 迭代、二分分割、极差判据），
   而不是追求与成熟库等价的实现——本阶段的目标是「提供可调用骨架」。

③ **参数不适用时返回空结果**：数据量不足、所选特征无变异、簇数不可行、
   结构分离度低于阈值、无变点通过惩罚、覆盖率不足、不变量判据不成立等
   情形一律返回**空列表**，而不是抛出未捕获异常或返回伪造的结论。
   与之相对，**契约违规**（传入非样本对象、字段名不存在、参数类型或取值非法）
   会抛出 :class:`StructureError`——这两类情形被严格区分，见「错误约定」。

错误约定（重要）
----------------

本模块把「无法给出结论」与「调用方用错了」分开处理：

- **统计意义上的不适用** → 返回 ``[]``：样本太少、无变异、k 超界、
  分离度不足、无显著变点、容差过严、覆盖率不足。
- **契约层面的违规** → 抛 :class:`StructureError`：
  传入的对象不是 :class:`~sdl_m03.equations.ExplorationSample`、
  要求的字段不在样本变量中、``order_by`` / ``features`` 不是字符串序列、
  ``seed`` 不是整数、容量类参数不是正整数、``tolerance`` 为负等。
- **策略层面的违规** → 抛 :class:`StructurePolicyError`（:class:`StructureError` 子类）：
  样本的用途不是探索分区 ``E``。

设计边界（严格遵守 P05 增量卡）
-------------------------------

- 本模块**不做稳定性与重采样评估**（属 P06）：没有 bootstrap、没有交叉验证、
  没有子采样、没有「用不同 seed 跑多次再比较」的稳定性分数。
  ``seed`` 只用于聚类初始化的**可复现性**，不用于任何稳定性估计。
- 本模块**不做关系式拟合与基线对照**（属 P04）：不计算 ``y ≈ a + b·Z`` 的
  系数，也不产出基线结果；需要关系式时请调用 :mod:`sdl_m03.equations`。
- 本模块**不做假说构造**（属 M4）、**不做多维评估与筛选**（属 M5）、
  **不做确证检验**（属 M6）：不计算 p 值、不控制错误率、不授予证据等级、
  不把「结构存在」表述为因果或确证结论。
- 本模块**不修改** ``sdl_m01/`` 与任何前序交付物，只读取其公开接口。
- 本模块**不做周期检测**：框架说明把「周期」列为可选结构之一，
  但 P05 增量卡只要求聚类 / 变点 / 不变量三个入口，因此本阶段不额外扩张范围。

已知限制
--------

1. **聚类是欧氏距离下的等权 k-means。** 特征先按样本做 z 标准化，
   因此结果依赖样本自身的均值与标准差；该样本是探索分区 E 的开发期数据，
   标准化参数**不得**直接搬到确证分区使用。簇数是启发式选择而非渐近理论结果。
2. **不处理「推断单位」。** 与 P04 一致，本模块按「读数」逐行计算，
   不把同一批次的多条读数聚合或加权；把读数当独立单元会高估结构证据强度。
   按推断单位聚合属 M5／M6 的职责。
3. **选簇判据是简化轮廓系数**，在簇很小时不稳定；``min_silhouette`` 是
   一个刻意保守的接受门槛，它降低假阳性，也可能漏掉弱结构。
4. **变点检测是一维、单序列、均值漂移模型。** 二分分割只找**均值**突变，
   对变方差、变趋势、周期性漂移不敏感；惩罚项是 BIC 式近似，
   不是严格的信息准则，也不提供显著性保证。
5. **不变量判据是「一致行占比」判据。** 一致行指
   ``|Z − 中位数| ≤ 容差 × 尺度``。当估计量中位数接近零时尺度退化为 ``1.0``
   （即改为绝对容差），这一点已随结果一起输出，以免被误读成无条件的相对误差。
   容差与一致度门槛均由调用方给定，本模块不自动选容差。
   判据刻意**允许少量反例**（这正是「近似」的含义），并把反例逐条列出，
   而不是靠收紧判据把反例藏起来。
6. **反例只列出样本内违规行**，样本外的反例需要新数据，属 P14 主动取证范围。
7. 所有结果都是**描述性的**：不构成统计检验，不给出置信区间与错误率控制。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sdl_m02.expressions import (
    ExpressionError,
    node_count as expression_node_count,
    parse as parse_expression,
)
from sdl_m03.equations import (
    EXPLORATION_PURPOSE,
    ExplorationSample,
    evaluate_expression,
    render_expression,
    restrict_to_domain,
)

__all__ = [
    "StructureError",
    "StructurePolicyError",
    "STRUCTURE_VERSION",
    "STRUCTURE_COMPLEXITY_NOTE",
    "PATTERN_KINDS",
    "DEFAULT_MAX_CLUSTERS",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "DEFAULT_SEED",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MIN_SILHOUETTE",
    "DEFAULT_MIN_SEGMENT",
    "DEFAULT_MAX_CHANGEPOINTS",
    "DEFAULT_PENALTY",
    "DEFAULT_TOLERANCE",
    "DEFAULT_MIN_CONFORMITY",
    "DEFAULT_MIN_COVERAGE",
    "StructurePattern",
    "cluster_labels",
    "select_cluster_count",
    "silhouette_score",
    "find_clusters",
    "changepoint_positions",
    "find_changepoints",
    "check_invariant",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本模块（结构模式搜索）的版本号。字段或行为变化时递增。
STRUCTURE_VERSION = "1"

#: 本阶段产出的模式种类。``relation`` 由 P04 产出，此处仅保留为合法取值以对齐契约。
PATTERN_KINDS: tuple[str, ...] = ("cluster", "changepoint", "invariant", "relation")

#: 复杂度的口径说明，随每个结果一起输出，避免被误读为严格 MDL。
STRUCTURE_COMPLEXITY_NOTE = (
    "复杂度为结构规模近似（簇数×维度 / 分段数 / 表达式节点数），不是严格 MDL；"
    "它只用于让不同规模的结构在同一尺度上可比。"
)

#: 聚类的默认规模上界。
DEFAULT_MAX_CLUSTERS = 6

#: 单个簇的默认最小成员数。
DEFAULT_MIN_CLUSTER_SIZE = 2

#: 默认随机种子。刻意固定，使「不传 seed」也满足可复现要求。
DEFAULT_SEED = 0

#: Lloyd 迭代的默认上限。
DEFAULT_MAX_ITERATIONS = 100

#: 接受聚类结构所需的最小简化轮廓系数。
DEFAULT_MIN_SILHOUETTE = 0.1

#: 单个分段的默认最小长度。
DEFAULT_MIN_SEGMENT = 3

#: 变点的默认数量上限。
DEFAULT_MAX_CHANGEPOINTS = 3

#: BIC 式惩罚系数（1.0 对应「惩罚 = 段内方差 × log(n)」）。
DEFAULT_PENALTY = 1.0

#: 不变量的默认相对容差。
DEFAULT_TOLERANCE = 1e-6

#: 接受不变量所需的最小一致行占比。
DEFAULT_MIN_CONFORMITY = 0.9

#: 不变量所需的最小覆盖率（定义域内有效行占比）。
DEFAULT_MIN_COVERAGE = 0.5

#: 反例清单的默认长度上限。
DEFAULT_MAX_COUNTEREXAMPLES = 10

#: 数值比较容差。
_EPS = 1e-12

#: 模式标识的固定前缀，便于在报告中一眼识别来源模块。
_PATTERN_ID_PREFIX = "pat-"


class StructureError(ValueError):
    """结构模式搜索的输入非法（契约层面的违规）。"""


class StructurePolicyError(StructureError):
    """数据访问策略违规：样本用途不是探索分区 E。"""


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def _validate_seed(seed: Any) -> int:
    """校验随机种子必须是整数（``bool`` 不算）。"""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise StructureError(f"seed 必须是整数，实际得到：{seed!r}")
    return seed


def _validate_positive_int(value: Any, label: str) -> int:
    """校验容量类参数必须是正整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StructureError(f"{label} 必须是正整数，实际得到：{value!r}")
    return value


def _validate_nonnegative_number(value: Any, label: str) -> float:
    """校验容差／惩罚类参数必须是非负有限实数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructureError(f"{label} 必须是实数，实际得到：{value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise StructureError(f"{label} 必须是非负有限实数，实际得到：{value!r}")
    return number


def _validate_unit_interval(value: Any, label: str) -> float:
    """校验比例类参数必须落在 ``(0, 1]`` 内。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructureError(f"{label} 必须是实数，实际得到：{value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 1:
        raise StructureError(f"{label} 必须落在 (0, 1] 内，实际得到：{value!r}")
    return number


def _validate_names(names: Any, label: str) -> tuple[str, ...]:
    """校验字段名序列：非空、唯一、非空字符串。"""
    if isinstance(names, str) or not isinstance(names, Sequence):
        raise StructureError(f"{label} 必须是字段名序列（例如 ['X1', 'X2']）")
    out: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise StructureError(f"字段名必须是非空字符串，实际得到：{name!r}")
        if name in out:
            raise StructureError(f"{label} 中存在重复字段名：{name!r}")
        out.append(name)
    if not out:
        raise StructureError(f"{label} 不能为空")
    return tuple(out)


def _assert_exploration_sample(sample: Any) -> ExplorationSample:
    """确认传入的是探索分区样本。

    两道检查：对象类型必须是 :class:`ExplorationSample`（该对象本身已由
    :func:`sdl_m03.equations.prepare_sample` 完成「只经 explorer 角色读 E」的
    来源约束），且用途字段必须仍是探索分区 ``E``。
    """
    if not isinstance(sample, ExplorationSample):
        raise StructureError(
            "sample 必须是 ExplorationSample（由 sdl_m03.equations.prepare_sample 构造）；"
            f"实际得到：{type(sample).__name__}"
        )
    if sample.purpose != EXPLORATION_PURPOSE:
        raise StructurePolicyError(
            f"本模块只处理探索分区 {EXPLORATION_PURPOSE} 的样本；"
            f"实际用途：{sample.purpose!r}"
        )
    return sample


def _round(value: float, digits: int = 12) -> float:
    """按固定位数取整，使输出与序列化跨运行稳定。"""
    return round(float(value), digits)


# ---------------------------------------------------------------------------
# 模式对象
# ---------------------------------------------------------------------------


def _pattern_id(kind: str, payload: Mapping[str, Any]) -> str:
    """由模式种类与载荷派生稳定标识。

    标识只依赖语义内容，不依赖发现顺序、运行时间或随机状态，
    因此「同一份数据、同一参数」恒得同一 ``pattern_id``。
    """
    text = json.dumps(
        {"kind": kind, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _PATTERN_ID_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class StructurePattern:
    """一个结构模式，字段与 ``INTERFACES.md`` §2.2 的 Pattern 对齐。

    :param pattern_id: 稳定标识，由 :func:`_pattern_id` 派生。
    :param kind: ``cluster`` / ``changepoint`` / ``invariant``。
    :param payload: 结构描述（簇心与成员、变点位置与阈值、不变量估计与反例）。
    :param metrics: **误差、残差、复杂度三项恒存在**（构造即校验）。
    :param provenance: 来源信息：模块版本、所用样本指纹、参数与随机种子。
        **不含任何令牌、不含受限质量报告**。
    :param stability: 重采样稳定性，落在 ``[0, 1]``。本阶段（P05）恒为 ``None``，
        因为稳定性评估属 P06；此处保留字段是为了不改动契约形状。
    """

    pattern_id: str
    kind: str
    payload: Mapping[str, Any]
    metrics: Mapping[str, Any]
    provenance: Mapping[str, Any]
    stability: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in PATTERN_KINDS:
            raise StructureError(
                f"未知的模式种类：{self.kind!r}；仅支持 {', '.join(PATTERN_KINDS)}"
            )
        if not self.pattern_id:
            raise StructureError("pattern_id 不能为空")
        if not self.payload:
            raise StructureError("模式必须包含载荷（结构描述）")
        if not self.metrics:
            raise StructureError("模式必须包含度量")
        for key in ("error", "residual", "complexity"):
            if key not in self.metrics:
                raise StructureError(f"度量缺少必需项 {key!r}（误差 / 残差 / 复杂度三项恒存在）")
        if not self.provenance:
            raise StructureError("模式必须包含来源信息")
        if self.stability is not None:
            if not isinstance(self.stability, (int, float)) or isinstance(self.stability, bool):
                raise StructureError("stability 必须是实数或 None")
            if not (0.0 <= float(self.stability) <= 1.0):
                raise StructureError("stability 必须落在 [0, 1] 内")

    @property
    def error(self) -> Any:
        """误差分量。"""
        return self.metrics["error"]

    @property
    def residual(self) -> Any:
        """残差分量。"""
        return self.metrics["residual"]

    @property
    def complexity(self) -> Any:
        """复杂度分量（结构规模近似，不是严格 MDL）。"""
        return self.metrics["complexity"]

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（形状对齐 ``INTERFACES.md`` §2.2）。"""
        return {
            "pattern_id": self.pattern_id,
            "kind": self.kind,
            "payload": _jsonable(self.payload),
            "metrics": _jsonable(self.metrics),
            "stability": self.stability,
            "provenance": _jsonable(self.provenance),
        }


def _jsonable(value: Any) -> Any:
    """把嵌套结构递归转换为 JSON 兼容对象（元组转列表，映射按键排序）。"""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _provenance(
    sample: ExplorationSample,
    *,
    detector: str,
    parameters: Mapping[str, Any],
) -> dict:
    """构造模式来源信息。

    只记录**可核验的抽象信息**：模块与版本、所用样本的指纹与行数、
    样本来源引用、检测器名与参数。不记录任何令牌，也不记录记录正文。
    """
    return {
        "origin": "structure",
        "detector": detector,
        "module": "sdl_m03/structure.py",
        "module_version": STRUCTURE_VERSION,
        "sample_fingerprint": sample.fingerprint,
        "sample_rows": len(sample.rows),
        "source_ref": sample.source_ref,
        "purpose": sample.purpose,
        "parameters": _jsonable(parameters),
    }


def _make_pattern(
    *,
    kind: str,
    payload: Mapping[str, Any],
    error: Any,
    residual: Any,
    complexity: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> StructurePattern:
    """统一构造模式，保证三项度量与复杂度口径说明恒存在。"""
    body = _jsonable(payload)
    metrics = {
        "error": error,
        "residual": residual,
        "complexity": dict(complexity),
        "complexity_note": STRUCTURE_COMPLEXITY_NOTE,
    }
    return StructurePattern(
        pattern_id=_pattern_id(kind, body),
        kind=kind,
        payload=body,
        metrics=metrics,
        provenance=dict(provenance),
        stability=None,
    )


# ---------------------------------------------------------------------------
# 数值辅助
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    """算术平均。"""
    return sum(values) / len(values)


def _std(values: Sequence[float], mean: float | None = None) -> float:
    """总体标准差（除以 n，非 n-1）。"""
    center = _mean(values) if mean is None else mean
    variance = sum((value - center) ** 2 for value in values) / len(values)
    return math.sqrt(max(variance, 0.0))


def _median(values: Sequence[float]) -> float:
    """中位数（偶数个取中间两个的均值）。"""
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def _has_variation(values: Sequence[float], *, eps: float = _EPS) -> bool:
    """序列是否存在可分辨的变异。"""
    if len(values) < 2:
        return False
    low = min(values)
    high = max(values)
    scale = max(abs(low), abs(high), 1.0)
    return (high - low) > eps * scale


def _squared_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """欧氏距离的平方。"""
    total = 0.0
    for a, b in zip(left, right):
        diff = a - b
        total += diff * diff
    return total


def _resolve_features(sample: ExplorationSample, features: Any) -> tuple[str, ...]:
    """把 ``features=None`` 解析为样本全部自变量，并校验字段确实存在。"""
    if features is None:
        names = sample.variables
    else:
        names = _validate_names(features, "features")
    missing = [name for name in names if name not in sample.variables]
    if missing:
        raise StructureError(
            f"样本中不存在字段：{', '.join(missing)}；样本变量为 {', '.join(sample.variables)}"
        )
    return tuple(names)


def _render_relationship(relationship: Any) -> str:
    """把候选表示渲染为可读文本；数值列返回占位说明。"""
    if isinstance(relationship, str):
        try:
            return render_expression(parse_expression(relationship))
        except ExpressionError as exc:
            raise StructureError(f"表达式文本无法解析：{relationship!r}（{exc}）") from exc
    if isinstance(relationship, tuple):
        return render_expression(relationship)
    ast = getattr(relationship, "ast", None)
    if isinstance(ast, tuple):
        return render_expression(ast)
    return "<预置数值列>"


def _relationship_cost(relationship: Any) -> int:
    """候选表示的规模近似（表达式节点数）；数值列记 0。"""
    if isinstance(relationship, str):
        try:
            return expression_node_count(parse_expression(relationship))
        except ExpressionError:
            return 0
    if isinstance(relationship, tuple):
        return expression_node_count(relationship)
    ast = getattr(relationship, "ast", None)
    if isinstance(ast, tuple):
        return expression_node_count(ast)
    return 0


# ---------------------------------------------------------------------------
# 聚类
# ---------------------------------------------------------------------------


def _cell(row: Any, name: str) -> float:
    """从一行样本中取出某字段的取值。

    同时接受 :class:`~sdl_m03.equations.SampleRow`（取值在 ``values`` 映射里）
    与普通 ``Mapping``，使内部辅助函数可被单元测试直接复用。
    """
    values = getattr(row, "values", None)
    if values is None:
        if not isinstance(row, Mapping):
            raise StructureError(f"样本行必须是 SampleRow 或映射，实际得到：{type(row).__name__}")
        values = row
    if name not in values:
        raise StructureError(f"样本行中不存在字段 {name!r}")
    return float(values[name])


def _zscore_columns(
    rows: Sequence[Any],
    features: Sequence[str],
) -> tuple[list[list[float]], tuple[str, ...], dict[str, dict[str, float]]]:
    """对特征做 z 标准化，并**剔除无变异特征**。

    :return: ``(标准化矩阵, 保留的特征名, 统计量字典)``。
        无变异特征（标准差为 0）在所有点上取值相同，不携带任何可分信息，
        强行保留会让距离退化为常数并制造「假维度」，故剔除并如实记录。
    """
    kept: list[str] = []
    stats: dict[str, dict[str, float]] = {}
    raw_columns: dict[str, list[float]] = {}
    for name in features:
        column = [_cell(row, name) for row in rows]
        center = _mean(column)
        spread = _std(column, center)
        if spread <= _EPS:
            # 无变异特征：在所有点上取值相同，不携带可分信息，剔除。
            continue
        kept.append(name)
        raw_columns[name] = column
        stats[name] = {"mean": _round(center), "std": _round(spread)}

    if not kept:
        return [], (), {}

    matrix: list[list[float]] = []
    for index in range(len(rows)):
        vector: list[float] = []
        for name in kept:
            center = stats[name]["mean"]
            spread = max(stats[name]["std"], _EPS)
            vector.append((raw_columns[name][index] - center) / spread)
        matrix.append(vector)
    return matrix, tuple(kept), stats


def cluster_labels(
    sample: ExplorationSample,
    features: Sequence[str] | None = None,
    k: int = 2,
    *,
    seed: int = DEFAULT_SEED,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> list[int]:
    """在标准化特征空间上做简化 k-means，返回每行的簇编号。

    纯函数：同一 ``(样本, 特征, k, seed)`` 恒得同一结果。簇编号按**中心坐标排序**
    后重新编号，因此编号本身与初始化顺序无关，可跨运行直接比对。

    :raises StructureError: 样本类型、特征字段、``k`` 或 ``seed`` 非法。
    :raises StructurePolicyError: 样本用途不是探索分区 E。
    """
    checked = _assert_exploration_sample(sample)
    names = _resolve_features(checked, features)
    cluster_count = _validate_positive_int(k, "k")
    random_seed = _validate_seed(seed)
    iterations = _validate_positive_int(max_iterations, "max_iterations")

    if not checked.rows:
        raise StructureError("样本为空，无法聚类")
    if cluster_count > len(checked.rows):
        raise StructureError(
            f"k={cluster_count} 超过样本行数 {len(checked.rows)}，无法聚类"
        )

    matrix, kept, _stats = _zscore_columns(checked.rows, names)
    if not kept:
        raise StructureError("全部所选特征在样本上均无变异，无法聚类")

    labels, _centers = _kmeans(matrix, cluster_count, random_seed, iterations)
    return labels


def _kmeans(
    matrix: Sequence[Sequence[float]],
    k: int,
    seed: int,
    max_iterations: int,
) -> tuple[list[int], list[list[float]]]:
    """Lloyd 迭代的简化 k-means。

    确定性来源：

    1. 初始化用**局部** ``random.Random(seed)`` 做 k-means++，不触碰全局随机状态；
    2. 赋值阶段并列时取**编号最小**的簇；
    3. 空簇处理固定：把「离自身中心最远」的点移入空簇（并列取下标最小者）；
    4. 返回前把簇按中心坐标字典序排序并重新编号。
    """
    rng = random.Random(seed)
    n = len(matrix)
    dimension = len(matrix[0])

    # -- k-means++ 初始化 -------------------------------------------------
    centers: list[list[float]] = [list(matrix[0])]
    while len(centers) < k:
        distances: list[float] = []
        for point in matrix:
            distances.append(min(_squared_distance(point, center) for center in centers))
        total = sum(distances)
        if total <= _EPS:
            # 所有点与已有中心重合：按下标顺序补足，保持确定性。
            centers.append(list(matrix[len(centers) % n]))
            continue
        target = rng.random() * total
        running = 0.0
        pick = n - 1
        for index, weight in enumerate(distances):
            running += weight
            if running >= target:
                pick = index
                break
        centers.append(list(matrix[pick]))

    labels = [0] * n
    for _iteration in range(max_iterations):
        changed = False
        for index, point in enumerate(matrix):
            best = 0
            best_distance = _squared_distance(point, centers[0])
            for cluster in range(1, k):
                distance = _squared_distance(point, centers[cluster])
                if distance < best_distance - _EPS:
                    best = cluster
                    best_distance = distance
            if labels[index] != best:
                labels[index] = best
                changed = True

        # 空簇处理（确定性：移入离自身中心最远的点）
        for cluster in range(k):
            members = [index for index, label in enumerate(labels) if label == cluster]
            if members:
                continue
            far_index = -1
            far_distance = -1.0
            for index, label in enumerate(labels):
                distance = _squared_distance(matrix[index], centers[label])
                if distance > far_distance + _EPS or (
                    abs(distance - far_distance) <= _EPS and (far_index == -1 or index < far_index)
                ):
                    far_distance = distance
                    far_index = index
            labels[far_index] = cluster
            changed = True

        for cluster in range(k):
            members = [matrix[index] for index, label in enumerate(labels) if label == cluster]
            if not members:
                continue
            centers[cluster] = [
                sum(point[axis] for point in members) / len(members) for axis in range(dimension)
            ]

        if not changed:
            break

    # -- 按中心坐标排序并重新编号，使输出与初始化顺序无关 ----------------
    order = sorted(range(k), key=lambda cluster: tuple(_round(value) for value in centers[cluster]))
    remap = {old: new for new, old in enumerate(order)}
    sorted_centers = [centers[old] for old in order]
    return [remap[label] for label in labels], sorted_centers


def silhouette_score(
    matrix: Sequence[Sequence[float]],
    labels: Sequence[int],
) -> float:
    """简化轮廓系数：所有点 ``(b - a) / max(a, b)`` 的均值。

    与经典定义一致；所有点重合（``a = b = 0``）或只有单个簇时约定为 ``0.0``，
    以免出现除以零的伪结论。
    """
    if len(matrix) != len(labels):
        raise StructureError("矩阵行数与标签数不一致")
    distinct = sorted(set(labels))
    if len(distinct) < 2:
        return 0.0
    scores: list[float] = []
    for index, point in enumerate(matrix):
        own = labels[index]
        own_members = [
            other for other, label in enumerate(labels) if label == own and other != index
        ]
        if own_members:
            inner = sum(math.sqrt(_squared_distance(point, matrix[other])) for other in own_members)
            a = inner / len(own_members)
        else:
            a = 0.0
        b = float("inf")
        for cluster in distinct:
            if cluster == own:
                continue
            members = [other for other, label in enumerate(labels) if label == cluster]
            if not members:
                continue
            outer = sum(math.sqrt(_squared_distance(point, matrix[other])) for other in members)
            b = min(b, outer / len(members))
        if not math.isfinite(b):
            return 0.0
        denominator = max(a, b)
        scores.append(0.0 if denominator <= _EPS else (b - a) / denominator)
    return sum(scores) / len(scores)


def select_cluster_count(
    sample: ExplorationSample,
    features: Sequence[str] | None = None,
    *,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    seed: int = DEFAULT_SEED,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> dict:
    """在 ``k = 2 … min(max_clusters, n // min_cluster_size)`` 中选簇数。

    选择规则（全确定，无随机搜索）：取简化轮廓系数**最大**的 ``k``；
    分数并列（相差不超过 ``1e-12``）时取**较小**的 ``k``。
    所有簇都满足 ``min_cluster_size`` 的 ``k`` 才参与比较。

    :return: ``{"k", "score", "candidates": [...], "dropped_features": [...],
        "kept_features": [...], "search_space": [...]}``；
        没有可行 ``k`` 时 ``k`` 为 ``None``、``candidates`` 为空。
    """
    checked = _assert_exploration_sample(sample)
    names = _resolve_features(checked, features)
    upper = _validate_positive_int(max_clusters, "max_clusters")
    floor_size = _validate_positive_int(min_cluster_size, "min_cluster_size")
    random_seed = _validate_seed(seed)
    iterations = _validate_positive_int(max_iterations, "max_iterations")

    result: dict[str, Any] = {
        "k": None,
        "score": None,
        "candidates": [],
        "dropped_features": [],
        "kept_features": [],
        "search_space": [],
    }
    if len(checked.rows) < 2 * floor_size:
        # 统计意义上的不适用：无法构成两个满足最小规模的簇。
        result["dropped_features"] = list(names)
        return result

    matrix, kept, _stats = _zscore_columns(checked.rows, names)
    result["kept_features"] = list(kept)
    result["dropped_features"] = [name for name in names if name not in kept]
    if not kept:
        return result

    feasible = range(2, min(upper, len(checked.rows) // floor_size) + 1)
    result["search_space"] = list(feasible)
    best_key: tuple[float, int] | None = None
    for candidate_k in feasible:
        labels, _centers = _kmeans(matrix, candidate_k, random_seed, iterations)
        sizes = [labels.count(cluster) for cluster in range(candidate_k)]
        score = silhouette_score(matrix, labels)
        record = {"k": candidate_k, "score": _round(score), "sizes": sizes}
        result["candidates"].append(record)
        if min(sizes) < floor_size:
            continue
        key = (_round(score), -candidate_k)
        if best_key is None or key > best_key:
            best_key = key
            result["k"] = candidate_k
            result["score"] = _round(score)
    return result


def find_clusters(
    sample: ExplorationSample,
    features: Sequence[str] | None = None,
    *,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    seed: int = DEFAULT_SEED,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    min_silhouette: float = DEFAULT_MIN_SILHOUETTE,
) -> list[StructurePattern]:
    """子群检测：返回**每个簇一条**模式，无法给出结论时返回空列表。

    :param sample: 探索分区样本。
    :param features: 参与聚类的字段；``None`` 表示样本全部自变量。
    :param max_clusters: 簇数搜索上界。
    :param min_cluster_size: 单个簇的最小成员数。
    :param seed: 聚类初始化种子（只影响可复现性，不用于稳定性估计）。
    :param max_iterations: Lloyd 迭代上限。
    :param min_silhouette: 接受聚类结构所需的最小简化轮廓系数。
    :return: 模式列表，按「簇中心坐标」排序，保证顺序确定；
        数据不足 / 特征无变异 / 无可行簇数 / 分离度不足时返回 ``[]``。
    :raises StructureError: 类型或参数非法、字段不存在。
    :raises StructurePolicyError: 样本用途不是 E。
    """
    checked = _assert_exploration_sample(sample)
    names = _resolve_features(checked, features)
    upper = _validate_positive_int(max_clusters, "max_clusters")
    floor_size = _validate_positive_int(min_cluster_size, "min_cluster_size")
    iterations = _validate_positive_int(max_iterations, "max_iterations")
    random_seed = _validate_seed(seed)
    threshold = _validate_nonnegative_number(min_silhouette, "min_silhouette")

    if len(checked.rows) < 2 * floor_size:
        return []

    selection = select_cluster_count(
        checked,
        names,
        max_clusters=upper,
        min_cluster_size=floor_size,
        seed=random_seed,
        max_iterations=iterations,
    )
    chosen = selection["k"]
    if chosen is None or selection["score"] is None:
        return []
    if float(selection["score"]) < threshold:
        # 结构分离度不足：不给出「发现」，避免把噪声当成子群。
        return []

    matrix, kept, stats = _zscore_columns(checked.rows, names)
    if not kept:
        return []
    labels, centers = _kmeans(matrix, int(chosen), random_seed, iterations)

    partition_text = json.dumps(
        [[row.record_id, label] for row, label in zip(checked.rows, labels)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    partition_signature = hashlib.sha256(partition_text.encode("utf-8")).hexdigest()[:16]

    provenance = _provenance(
        checked,
        detector="find_clusters",
        parameters={
            "features": list(names),
            "kept_features": list(kept),
            "requested_max_clusters": int(
                _validate_positive_int(max_clusters, "max_clusters")
            ),
            "min_cluster_size": floor_size,
            "seed": _validate_seed(seed),
            "min_silhouette": threshold,
            "selected_k": int(chosen),
            "silhouette": selection["score"],
            "partition_signature": partition_signature,
            "note": "seed 仅用于聚类初始化的可复现性，未用于任何稳定性估计（属 P06）",
        },
    )

    patterns: list[StructurePattern] = []
    for cluster in range(int(chosen)):
        members = [index for index, label in enumerate(labels) if label == cluster]
        member_ids = sorted(checked.rows[index].record_id for index in members)
        vector = centers[cluster]
        center_original = {
            name: _round(vector[axis] * max(stats[name]["std"], _EPS) + stats[name]["mean"])
            for axis, name in enumerate(kept)
        }
        center_z = {name: _round(vector[axis]) for axis, name in enumerate(kept)}
        inner_sse = sum(_squared_distance(matrix[index], vector) for index in members)
        mean_distance = (
            math.sqrt(inner_sse / len(members)) if members else 0.0
        )
        payload = {
            "cluster_index": cluster,
            "k": int(chosen),
            "features": list(kept),
            "center": center_original,
            "center_zscore": center_z,
            "size": len(members),
            "members": member_ids,
            "partition_signature": partition_signature,
            "selection": {
                "criterion": "简化轮廓系数（最大者胜出，并列取较小 k）",
                "silhouette": selection["score"],
                "sizes": [labels.count(index) for index in range(int(chosen))],
            },
        }
        patterns.append(
            _make_pattern(
                kind="cluster",
                payload=payload,
                error=_round(inner_sse),
                residual=_round(mean_distance),
                complexity={
                    "clusters": int(chosen),
                    "dimensions": len(kept),
                    "rows": len(checked.rows),
                    "total": int(chosen) * len(kept),
                },
                provenance=provenance,
            )
        )
    return patterns


# ---------------------------------------------------------------------------
# 变点
# ---------------------------------------------------------------------------


def _series_column(
    sample: ExplorationSample,
    series: Any,
) -> tuple[list[float], int]:
    """把 ``series`` 解析为逐行取值列。

    :param series: ``None``（取目标列）、表达式（文本 / AST 元组 / Candidate）
        或预先算好的数值列。
    :return: ``(取值列, 被剔除的非有限行数)``；非有限位置记为 ``nan``，
        由调用方决定是丢弃还是拒绝。
    """
    if series is None:
        return [row.target for row in sample.rows], 0
    if isinstance(series, (str, tuple)) or hasattr(series, "ast"):
        if isinstance(series, str):
            try:
                ast = parse_expression(series)
            except ExpressionError as exc:
                raise StructureError(f"表达式文本无法解析：{series!r}（{exc}）") from exc
        elif isinstance(series, tuple):
            ast = series
        else:
            ast = series.ast
        # 表达式可引用样本自变量，也可引用目标字段本身（目标不在 variables 中，
        # 但确实逐行可取值，故一并放行）。
        allowed = set(sample.variables) | {sample.target}
        missing = sorted(set(_walk_variables(ast)) - allowed)
        if missing:
            raise StructureError(
                f"样本缺少表达式所需变量：{', '.join(missing)}；"
                f"样本自变量为 {', '.join(sample.variables)}，目标为 {sample.target}"
            )
        values: list[float] = []
        invalid = 0
        for row in sample.rows:
            # 求值点 = 自变量取值 + 目标取值（目标字段可用于表达式，如 "Y*1.0"）。
            point = dict(row.values)
            point.setdefault(sample.target, row.target)
            try:
                values.append(evaluate_expression(ast, point))
            except (ArithmeticError, ValueError):
                values.append(float("nan"))
                invalid += 1
        return values, invalid
    if isinstance(series, Sequence) and not isinstance(series, (bytes, bytearray)):
        column = [float(value) for value in series]
        if len(column) != len(sample.rows):
            raise StructureError(
                f"数值列长度 {len(column)} 与样本行数 {len(sample.rows)} 不一致"
            )
        return column, 0
    raise StructureError(
        "series 必须是 None、表达式（文本 / AST / Candidate）或数值列；"
        f"实际得到：{type(series).__name__}"
    )


def _walk_variables(ast: Any) -> list[str]:
    """收集 AST 中的变量名（供 ``series`` 解析使用）。"""
    out: list[str] = []
    if not isinstance(ast, tuple) or not ast:
        return out
    kind = ast[0]
    if kind == "var":
        out.append(ast[1])
        return out
    if kind == "num":
        return out
    for child in ast[1:]:
        if isinstance(child, tuple):
            out.extend(_walk_variables(child))
        elif isinstance(child, str) and kind == "func":
            continue
    return out


def _ordered_series(
    sample: ExplorationSample,
    order_by: Any,
    series: Any,
) -> tuple[list[dict], int]:
    """按指定字段排序后给出序列。

    :return: ``(有序行列表, 被剔除的非有限行数)``；有序行元素为
        ``{"record_id", "key", "value"}``。排序键为 ``(key, record_id)``，
        因此并列键下顺序仍唯一确定。
    """
    values, invalid = _series_column(sample, series)
    if order_by is None:
        keys = [float(index) for index in range(len(sample.rows))]
    else:
        if not isinstance(order_by, str) or not order_by.strip():
            raise StructureError("order_by 必须是非空字符串或 None")
        if order_by not in sample.variables:
            raise StructureError(
                f"order_by 字段 {order_by!r} 不在样本变量 {', '.join(sample.variables)} 中"
            )
        keys = [row.values[order_by] for row in sample.rows]

    ordered: list[dict] = []
    for row, key, value in zip(sample.rows, keys, values):
        if not math.isfinite(key) or not math.isfinite(value):
            invalid += 1
            continue
        ordered.append({"record_id": row.record_id, "key": float(key), "value": float(value)})
    ordered.sort(key=lambda item: (item["key"], item["record_id"]))
    return ordered, invalid


def _segment_sse(
    prefix: Sequence[float],
    prefix_squared: Sequence[float],
    low: int,
    high: int,
) -> float:
    """区间 ``[low, high)`` 的残差平方和（相对区间均值）。

    用前缀和算出，使一次分裂扫描是 ``O(n)`` 而不是 ``O(n²)``。
    """
    count = high - low
    if count <= 0:
        return 0.0
    total = prefix[high] - prefix[low]
    total_squared = prefix_squared[high] - prefix_squared[low]
    return max(total_squared - total * total / count, 0.0)


def _best_split(
    prefix: Sequence[float],
    prefix_squared: Sequence[float],
    low: int,
    high: int,
    min_segment: int,
) -> tuple[int | None, float]:
    """在区间内找出使 SSE 下降最多的分裂位置。"""
    count = high - low
    if count < 2 * min_segment:
        return None, 0.0
    parent = _segment_sse(prefix, prefix_squared, low, high)
    best_index: int | None = None
    best_gain = 0.0
    for index in range(low + min_segment, high - min_segment + 1):
        gain = parent - _segment_sse(prefix, prefix_squared, low, index) - _segment_sse(
            prefix, prefix_squared, index, high
        )
        # 严格大于：并列时保留**更靠前**的分裂位置，保证确定性。
        if gain > best_gain + _EPS:
            best_gain = gain
            best_index = index
    return best_index, max(best_gain, 0.0)


def changepoint_positions(
    sample: ExplorationSample,
    *,
    order_by: str | None = None,
    series: Any = None,
    min_segment: int = DEFAULT_MIN_SEGMENT,
    max_changepoints: int = DEFAULT_MAX_CHANGEPOINTS,
    penalty: float = DEFAULT_PENALTY,
) -> list[int]:
    """返回变点位置的**下标列表**（在排序后的序列上，分裂点右侧为第一个下标）。

    纯函数：同一输入恒得同一结果。没有变点通过惩罚时返回 ``[]``。

    :raises StructureError: 类型或参数非法、字段不存在。
    :raises StructurePolicyError: 样本用途不是 E。
    """
    checked = _assert_exploration_sample(sample)
    floor_size = _validate_positive_int(min_segment, "min_segment")
    limit = _validate_positive_int(max_changepoints, "max_changepoints")
    weight = _validate_nonnegative_number(penalty, "penalty")

    ordered, _invalid = _ordered_series(checked, order_by, series)
    count = len(ordered)
    if count < 2 * floor_size:
        return []
    values = [item["value"] for item in ordered]
    if not _has_variation(values):
        return []

    prefix = [0.0]
    prefix_squared = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
        prefix_squared.append(prefix_squared[-1] + value * value)

    segments: list[tuple[int, int]] = [(0, count)]
    accepted: list[int] = []
    while len(accepted) < limit:
        best: tuple[float, int, int] | None = None
        for low, high in segments:
            index, gain = _best_split(prefix, prefix_squared, low, high, floor_size)
            if index is None:
                continue
            span = high - low
            local_variance = _segment_sse(prefix, prefix_squared, low, high) / span
            threshold = weight * local_variance * math.log(span)
            if gain <= threshold + _EPS:
                continue
            key = (gain, -low, index)
            if best is None or key > best:
                best = key
        if best is None:
            break
        gain, _neg_low, index = best
        accepted.append(index)
        parent = next(segment for segment in segments if segment[0] < index < segment[1])
        segments.remove(parent)
        segments.append((parent[0], index))
        segments.append((index, parent[1]))
        segments.sort()
    accepted.sort()
    return accepted


def find_changepoints(
    sample: ExplorationSample,
    *,
    order_by: str | None = None,
    series: Any = None,
    min_segment: int = DEFAULT_MIN_SEGMENT,
    max_changepoints: int = DEFAULT_MAX_CHANGEPOINTS,
    penalty: float = DEFAULT_PENALTY,
) -> list[StructurePattern]:
    """阈值／变点检测：返回**每个变点一条**模式，无结论时返回空列表。

    算法为「二分分割 + BIC 式惩罚」：反复选出使段内 SSE 下降最多且
    下降量超过 ``penalty × 段内方差 × log(段长)`` 的分裂位置，直到达到
    ``max_changepoints`` 或没有分裂通过惩罚。该判据是**近似**，不是严格信息准则。

    :param sample: 探索分区样本。
    :param order_by: 排序字段；``None`` 表示沿用样本行序（以行下标为键）。
    :param series: 被检测的序列；``None`` 表示目标列，亦可给表达式或数值列。
    :param min_segment: 单个分段的最小长度。
    :param max_changepoints: 变点数量上限。
    :param penalty: 惩罚系数，越大越保守（``0`` 表示只看下降量是否为正）。
    :return: 模式列表，按变点位置升序；数据不足 / 无变异 / 无变点通过惩罚时返回 ``[]``。
    :raises StructureError: 类型或参数非法、字段不存在。
    :raises StructurePolicyError: 样本用途不是 E。
    """
    checked = _assert_exploration_sample(sample)
    floor_size = _validate_positive_int(min_segment, "min_segment")
    limit = _validate_positive_int(max_changepoints, "max_changepoints")
    weight = _validate_nonnegative_number(penalty, "penalty")
    if order_by is not None and (
        not isinstance(order_by, str) or not order_by.strip()
    ):
        raise StructureError("order_by 必须是非空字符串或 None")

    ordered, invalid = _ordered_series(checked, order_by, series)
    count = len(ordered)
    if count < 2 * floor_size:
        return []
    values = [item["value"] for item in ordered]
    if not _has_variation(values):
        return []

    positions = changepoint_positions(
        checked,
        order_by=order_by,
        series=series,
        min_segment=floor_size,
        max_changepoints=limit,
        penalty=weight,
    )
    if not positions:
        return []

    prefix = [0.0]
    prefix_squared = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
        prefix_squared.append(prefix_squared[-1] + value * value)

    boundaries = [0, *positions, count]
    segment_stats: list[dict] = []
    total_residual = 0.0
    total_abs = 0.0
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        segment_values = values[start:end]
        center = _mean(segment_values)
        sse = _segment_sse(prefix, prefix_squared, start, end)
        total_residual += sse
        total_abs += sum(abs(value - center) for value in segment_values)
    total_residual = math.sqrt(total_residual / count)
    mean_abs = total_abs / count

    provenance_parameters = {
        "order_by": order_by,
        "series": _render_relationship(series) if series is not None else f"目标列 {checked.target}",
        "min_segment": floor_size,
        "max_changepoints": limit,
        "penalty": weight,
        "ordered_rows": count,
        "dropped_nonfinite": invalid,
        "positions": positions,
        "note": "惩罚项为 BIC 式近似，不是严格信息准则；不提供显著性保证",
    }
    provenance = _provenance(checked, detector="find_changepoints", parameters=provenance_parameters)

    patterns: list[StructurePattern] = []
    for order, position in enumerate(positions):
        left_low, left_high = boundaries[order], position
        right_low, right_high = position, boundaries[order + 2]
        left_values = values[left_low:left_high]
        right_values = values[right_low:right_high]
        parent_sse = _segment_sse(prefix, prefix_squared, left_low, right_high)
        after_sse = _segment_sse(prefix, prefix_squared, left_low, left_high) + _segment_sse(
            prefix, prefix_squared, right_low, right_high
        )
        gain = max(parent_sse - after_sse, 0.0)
        left_key = ordered[position - 1]["key"]
        right_key = ordered[position]["key"]
        threshold_value = 0.5 * (left_key + right_key)
        payload = {
            "position": position,
            "threshold": _round(threshold_value),
            "order_by": order_by,
            "before": {
                "count": len(left_values),
                "mean": _round(_mean(left_values)),
                "record_first": ordered[left_low]["record_id"],
                "record_last": ordered[position - 1]["record_id"],
            },
            "after": {
                "count": len(right_values),
                "mean": _round(_mean(right_values)),
                "record_first": ordered[position]["record_id"],
                "record_last": ordered[right_high - 1]["record_id"],
            },
            "mean_shift": _round(_mean(right_values) - _mean(left_values)),
            "segment_index": order + 1,
            "segments_total": len(positions) + 1,
        }
        patterns.append(
            _make_pattern(
                kind="changepoint",
                payload=payload,
                error=_round(gain),
                residual=_round(mean_abs),
                complexity={
                    "changepoints": len(positions),
                    "segments": len(positions) + 1,
                    "expression_nodes": 0,
                    "total": len(positions) + len(positions) + 1,
                },
                provenance=provenance,
            )
        )
    # 全局残差随每条模式一起给出，便于人工复核分段质量。
    for pattern in patterns:
        pattern.metrics["segment_rmse"] = _round(total_residual)
    return patterns


# ---------------------------------------------------------------------------
# 不变量
# ---------------------------------------------------------------------------


def check_invariant(
    sample: ExplorationSample,
    relationship: Any,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    min_conformity: float = DEFAULT_MIN_CONFORMITY,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_counterexamples: int = DEFAULT_MAX_COUNTEREXAMPLES,
) -> list[StructurePattern]:
    """近似守恒量检测：最多返回一条模式，判据不成立时返回空列表。

    判据（全确定，两步）：

    1. 令候选表达式在样本上取值为 ``Z``，中位数为 ``m``，尺度为
       ``scale = |m|``（当 ``|m|`` 不超过机器精度时退化为 ``1.0``，
       即改为绝对容差），容差上限为 ``allowed = tolerance × scale``；
    2. 称满足 ``|Z − m| ≤ allowed`` 的行为**一致行**。当一致行占有效行的比例
       （``conformity``）不低于 ``min_conformity`` 时，记为近似守恒量。

    一致行之外的**反例**（超出容差的行）被逐条列出，按偏离度降序，
    上限 ``max_counterexamples`` 条。这是「近似」二字的落点：不变量允许少量
    反例存在，但必须把反例摆出来，而不是靠放宽判据把它们藏起来。

    另设覆盖率门槛：候选定义域限制后保留的行占比不足 ``min_coverage``
    时不给出结论（样本被定义域剪得太狠，守恒证据不可信）。

    :param sample: 探索分区样本。
    :param relationship: 候选表示（:class:`~sdl_m02.domain.Candidate`、
        AST 元组、表达式文本或预先算好的数值列）。
    :param tolerance: 相对容差（非负；``0`` 表示要求严格相等）。
    :param min_conformity: 一致行占比下限，取值落在 ``(0, 1]``。
    :param min_coverage: 最小覆盖率，取值落在 ``(0, 1]``。
    :param max_counterexamples: 反例清单长度上限（按偏离度降序）。
    :returns: 判据成立时返回长度为 1 的模式列表，否则 ``[]``。
    :raises StructureError: 类型或参数非法、表达式无法解析、变量缺失。
    :raises StructurePolicyError: 样本用途不是 E。
    """
    checked = _assert_exploration_sample(sample)
    epsilon = _validate_nonnegative_number(tolerance, "tolerance")
    conformity_floor = _validate_unit_interval(min_conformity, "min_conformity")
    coverage_floor = _validate_unit_interval(min_coverage, "min_coverage")
    limit = _validate_positive_int(max_counterexamples, "max_counterexamples")

    if not checked.rows:
        return []

    try:
        restricted = restrict_to_domain(checked, relationship)
    except ValueError:
        # 表达式非法 / 变量缺失等契约问题由 P04 的入口判定。此处统一转为
        # 本模块的错误类型，避免调用方需要同时捕获两套异常。
        rendered = _render_relationship(relationship)
        if rendered == "<预置数值列>":
            raise StructureError("不变量检测的候选表示非法：数值列长度与样本不一致") from None
        missing = sorted(set(_walk_variables(_ast_of(relationship))) - set(checked.variables))
        if missing:
            raise StructureError(
                f"样本缺少表达式所需变量：{', '.join(missing)}；"
                f"样本变量为 {', '.join(checked.variables)}"
            ) from None
        raise StructureError(f"候选表示无法求值：{rendered}") from None

    used = len(restricted.values)
    coverage = used / len(checked.rows)
    if used < 2 or coverage < coverage_floor:
        return []

    values = list(restricted.values)
    ordered = sorted(values)
    center = _median(ordered)
    spread = ordered[-1] - ordered[0]
    scale = abs(center) if abs(center) > _EPS else 1.0
    allowed = epsilon * scale

    violations: list[dict] = []
    conforming: list[float] = []
    for row, value in zip(restricted.sample.rows, values):
        deviation = abs(value - center)
        if deviation > allowed + _EPS:
            violations.append(
                {
                    "record_id": row.record_id,
                    "value": _round(value),
                    "deviation": _round(deviation),
                }
            )
        else:
            conforming.append(value)

    if not conforming:
        return []
    conformity = len(conforming) / used
    if conformity < conformity_floor:
        # 一致行不足：不给出「发现」，避免把噪声当成守恒量。
        return []

    violations.sort(key=lambda item: (-item["deviation"], item["record_id"]))
    counterexample_count = len(violations)
    rendered = _render_relationship(relationship)

    payload = {
        "expression": rendered,
        "estimate": _round(center),
        "interval": [_round(ordered[0]), _round(ordered[-1])],
        "core_interval": [_round(min(conforming)), _round(max(conforming))],
        "spread": _round(spread),
        "scale": _round(scale),
        "allowed_deviation": _round(allowed),
        "tolerance": epsilon,
        "basis": "relative" if abs(center) > _EPS else "absolute",
        "criterion": (
            "一致行占比 ≥ 一致度门槛，其中一致行指 |Z − 中位数| ≤ 容差 × 尺度"
            "（中位数接近零时尺度退化为 1.0）"
        ),
        "conformity": _round(conformity),
        "min_conformity": conformity_floor,
        "coverage": _round(coverage),
        "n_used": used,
        "n_rows": len(checked.rows),
        "n_excluded": len(checked.rows) - used,
        "counterexample_count": counterexample_count,
        "counterexamples": violations[:limit],
        "note": (
            "近似守恒量是描述性结果，允许存在少量反例；不构成因果或确证结论；"
            "样本外反例需新数据（属 P14）"
        ),
    }
    provenance = _provenance(
        checked,
        detector="check_invariant",
        parameters={
            "expression": rendered,
            "tolerance": epsilon,
            "min_conformity": conformity_floor,
            "min_coverage": coverage_floor,
            "conformity": _round(conformity),
            "counterexample_count": counterexample_count,
            "domain": restricted.detail.get("domain"),
            "kept_rows": used,
            "note": "容差由调用方给定；本模块不自动选容差，也不做重采样稳定性评估",
        },
    )
    pattern = _make_pattern(
        kind="invariant",
        payload=payload,
        error=_round(max(abs(value - center) for value in values)),
        residual=_round(_std(values, center)),
        complexity={
            "expression_nodes": _relationship_cost(relationship),
            "domain_conditions": len(
                getattr(getattr(relationship, "domain", None), "conditions", ())
            ),
            "counterexamples": counterexample_count,
            "total": _relationship_cost(relationship) + counterexample_count,
        },
        provenance=provenance,
    )
    return [pattern]


def _ast_of(relationship: Any) -> Any:
    """尽力取出候选表示的 AST；数值列返回 ``None``。"""
    if isinstance(relationship, str):
        try:
            return parse_expression(relationship)
        except ExpressionError:
            return None
    if isinstance(relationship, tuple):
        return relationship
    return getattr(relationship, "ast", None)
