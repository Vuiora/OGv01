"""M3 稳定性与重采样评估（P06 交付物）。

本模块回答一个问题：**在按观测单位重抽样之后，P04 的关系候选与 P05 的结构模式
还站得住吗？** 框架说明 §4 把「重采样稳定性 S(H)」列为探索阶段 Pareto 筛选的三个
最大化目标之一，本模块就是把它变成可计算、可复现的数值：

    ``S(H) = 重采样中结构被复现的次数 / 有效重采样次数``

公开接口
--------

- :func:`resample_evaluate`：对「关系候选」或「结构模式」做分组级重采样评估，
  返回 :class:`StabilityReport`。
- :func:`stability_score`：把重采样证据折算为落在 ``[0, 1]`` 的稳定性分数 ``S(H)``。
- :func:`resolve_split_unit`：从 M1 的 spec 解析重采样单位（``split_unit``）与分组字段。
- :func:`group_key_of`：按 M1 的 ``unit_keys`` 口径构造单个记录的分组标识。

三条验收标准与实现对应关系
--------------------------

① **重采样单位与 spec 的 ``split_unit`` 一致（分组级，不是记录级）**：
   重采样以 ``spec["dependence"]["split_unit"]`` 所指的**组**为最小单位，
   同一组的全部记录**整组进入或整组不进入**某次重采样，绝不按行独立抽取。
   ``split_unit`` 必须确实出现在 ``spec["dependence"]["group_fields"]`` 中，
   否则抛 :class:`StabilityError`；**未提供 spec 又未显式给出单位时一律报错**，
   因为「默认按记录级重采样」恰恰是这条验收标准要禁止的行为。
   每次抽样的组序列都记录在报告的 ``details["draws"]`` 中，便于逐条复核。

② **分数落在 ``[0, 1]``**：:func:`stability_score` 对任何合法证据都返回
   ``[0, 1]`` 内的浮点数；分子分母都经过校验（``0 <= 复现次数 <= 有效次数``），
   无有效重采样时返回 ``0.0`` 而不是 ``None`` 或 ``nan``。

③ **同一 seed 结果可复现**：整条流程只用**局部** ``random.Random(seed)``
   实例，不触碰全局随机状态；检测器本身（P05）也是固定 seed 下确定的。
   同一 ``(候选, 数据, n_resamples, seed)`` 恒得逐字节相同的 ``to_dict()``。

稳定性的判据（逐类明确写出，避免含糊）
--------------------------------------

「复现」不是模糊的「看着差不多」，而是每一类结构各自可核验的判据：

- **关系候选**（P04 产出）：在重采样子样本上重新拟合候选与全部基线，认为复现
  当且仅当 ①候选的损失仍**严格低于**所有基线中最难超越者（MSE 最小者），
  且 ②候选斜率的**符号**与全样本一致。第二条防止「换了子样本就翻转方向」的
  伪稳定：一个方向都不稳的关系式，其预测增益没有意义。
- **聚类**（P05 ``find_clusters``）：重采样后重新选簇并聚类，认为复现当且仅当
  ①簇数与全样本一致，且 ②在两次都出现过的记录集合上，**成对共簇一致率**
  （Rand 指数）不低于 ``min_agreement``。成对比较与簇编号无关，因此不需要
  也不依赖簇的重命名。
- **变点**（P05 ``find_changepoints``）：重采样后重新检测，认为复现当且仅当
  ①变点数量一致，且 ②各变点的**相对位置**偏差不超过 ``position_tolerance``。
  用相对位置而非绝对行号，是因为重采样后的序列长度会变化，绝对行号不可比。
- **不变量**（P05 ``check_invariant``）：重采样后重新判定，认为复现当且仅当
  ①判据仍然成立（返回了模式），且 ②估计量的**相对偏差**不超过
  ``estimate_tolerance``。

重采样设置
----------

默认是**按组的自助法**（bootstrap：有放回地抽 ``n_groups`` 个组）；
``replace=False`` 时改为不放回的子抽样，抽 ``ceil(subsample_fraction × n_groups)``
个组，且至少 2 个。两种方式都以组为单位。

设计边界（严格遵守 P06 增量卡）
-------------------------------

- 本模块**只使用探索分区 E**：样本用途必须是 ``"E"``（否则抛
  :class:`StabilityPolicyError`），数据引用沿用 P04 :func:`~sdl_m03.equations.prepare_sample`
  的 E 归属校验。**不访问 V 分区，不引入确证数据**，也不存在绕过角色令牌的读取路径。
- 本模块**不做假说构造**（属 M4）、**不做多维评估与加权筛选**（属 M5）、
  **不做确证检验**（属 M6）：不计算 p 值、不控制错误率、不授予证据等级、
  不把稳定性分数当作证据强度或因果结论。
- 本模块**不做关系拟合与结构检测本身**（属 P04 / P05），而是**调用**它们；
  重采样只负责「换一批观测单位再跑一遍」。
- 本模块**不修改** ``sdl_m01/`` 与任何前序交付物，只读取其公开接口。
- 本模块**不读、不写、不打印任何角色令牌或受限质量报告**：报告只记录
  组数、行数、判据与分数等抽象信息。

已知限制
--------

1. **组数偏少时重采样没有分辨力。** 组数少于 ``min_groups``（默认 3）时
   直接判为「不足以给出结论」：``sufficient=False``，并在 ``reasons`` 里说明。
   此时 ``stability`` 仍是一个 ``[0, 1]`` 的数（复现比例），但**不应被采用**——
   报告用 ``sufficient`` 字段把这件事显式区分开，而不是把低分辨力的数字
   伪装成结论。
2. **若 spec 中所有组都只含一条记录，分组级重采样在效果上退化回记录级。**
   这是 spec 本身决定的，不是本模块擅自降级；报告在
   ``unit_summary["unit_level"]`` 中如实标注为 ``"record"`` 并给出说明。
3. **重采样只覆盖「抽取观测单位」这一种不确定性来源**，不覆盖测量误差、
   缺失机制、定义域边界误判或平台漂移。它衡量的是「换一批同类单位，
   结论还在不在」，不是「结论有多真」。
4. **聚类判据依赖簇数一致。** 若重采样选出的最优簇数本身就摇摆，
   一致率会同时下降——这是有意的保守：簇数不稳的划分不值得当成结构。
5. **不变量与变点的重放复用 P05 的简化算法**，因此 P05 的全部已知限制
   （一维均值漂移模型、BIC 式近似惩罚、一致行占比判据等）照旧适用。
6. **不提供 p 值、置信区间意义上的推断**。报告中的 ``wilson_lower_95``
   只是对「复现比例」这一比例量的描述性下界，**不是**错误率控制，
   也不构成显著性结论。
7. **重采样方案的选择会直接影响分数，本模块不做替代判断。** 有放回自助法
   会把同一组重复抽出，于是同一组的数据在子样本中出现多份精确重复；对某些
   检测器（尤其基于轮廓系数的聚类）而言，这种重复会形成「看起来更分离」的
   人为结构，从而抬高分数。实测中曾出现某聚类候选在自助法下得分接近 1.0、
   而在整群子抽样下明显更低的情况。本模块如实报告所用方案与其结果，
   **不替调用方论证哪种方案更合适**——这属于研究设计决策，应在冻结协议中写明。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from sdl_m02.expressions import ExpressionError, parse as parse_expression

from sdl_m03 import structure as structure_module
from sdl_m03.equations import (
    BASELINE_KINDS,
    DEFAULT_BASELINES,
    EXPLORATION_PURPOSE,
    EXPLORER_ROLE,
    EquationPolicyError,
    ExplorationSample,
    FitResult,
    fit_relation,
    prepare_sample,
)
from sdl_m03.structure import (
    StructurePattern,
    changepoint_positions,
    check_invariant,
    find_clusters,
)

__all__ = [
    "StabilityError",
    "StabilityPolicyError",
    "STABILITY_VERSION",
    "STABILITY_CRITERION_NOTE",
    "SUBJECT_KINDS",
    "DEFAULT_N_RESAMPLES",
    "DEFAULT_SEED",
    "DEFAULT_MIN_GROUPS",
    "DEFAULT_MIN_AGREEMENT",
    "DEFAULT_POSITION_TOLERANCE",
    "DEFAULT_ESTIMATE_TOLERANCE",
    "DEFAULT_SUBSAMPLE_FRACTION",
    "DEFAULT_DRAW_SAMPLE",
    "StabilityReport",
    "resolve_split_unit",
    "group_key_of",
    "stability_score",
    "resample_evaluate",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本模块（稳定性与重采样评估）的版本号。字段或行为变化时递增。
STABILITY_VERSION = "1"

#: 可评估的被试种类。
SUBJECT_KINDS: tuple[str, ...] = ("relation", "cluster", "changepoint", "invariant")

#: 稳定性口径说明，随每个结果一起输出，避免被误读为证据强度或显著性结论。
STABILITY_CRITERION_NOTE = (
    "稳定性 S(H) 是「按观测单位重采样后结构被复现的比例」，落在 [0, 1]；"
    "它只衡量结构对观测单位抽样的敏感程度，不是错误率控制，"
    "也不构成证据等级或因果结论。"
)

#: 默认重采样次数。
DEFAULT_N_RESAMPLES = 200

#: 默认随机种子。
DEFAULT_SEED = 0

#: 给出结论所需的最少组数（少于该值时判为不足以给出结论）。
DEFAULT_MIN_GROUPS = 3

#: 聚类复现所需的成对共簇一致率下限。
DEFAULT_MIN_AGREEMENT = 0.8

#: 变点复现允许的相对位置偏差。
DEFAULT_POSITION_TOLERANCE = 0.1

#: 不变量复现允许的估计量相对偏差。
DEFAULT_ESTIMATE_TOLERANCE = 0.1

#: ``replace=False`` 时抽样的组数比例。
DEFAULT_SUBSAMPLE_FRACTION = 0.8

#: 报告中保留的抽样明细条数（供人工复核分组级抽样）。
DEFAULT_DRAW_SAMPLE = 8

#: 浮点比较容差。
_EPS = 1e-12

#: 组内记录数不足 1 时的兜底（正常路径不会用到）。
_MIN_GROUP_SIZE = 1


class StabilityError(ValueError):
    """契约违例：参数类型、取值或输入对象不符合本模块的约定。"""


class StabilityPolicyError(StabilityError):
    """策略违例：试图把非探索分区（E）的数据带进稳定性评估。"""


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def _validate_seed(seed: Any) -> int:
    """校验随机种子：必须是整数（``bool`` 不算）。"""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise StabilityError(f"seed 必须是整数，实际得到：{seed!r}")
    return seed


def _validate_positive_int(value: Any, label: str) -> int:
    """校验正整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StabilityError(f"{label} 必须是整数，实际得到：{value!r}")
    if value <= 0:
        raise StabilityError(f"{label} 必须是正整数，实际得到：{value!r}")
    return value


def _validate_unit_interval(value: Any, label: str) -> float:
    """校验取值落在 ``(0, 1]`` 的数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StabilityError(f"{label} 必须是实数，实际得到：{value!r}")
    number = float(value)
    if not math.isfinite(number) or not (0.0 < number <= 1.0):
        raise StabilityError(f"{label} 必须落在 (0, 1] 内，实际得到：{value!r}")
    return number


def _validate_nonnegative(value: Any, label: str) -> float:
    """校验非负实数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StabilityError(f"{label} 必须是实数，实际得到：{value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise StabilityError(f"{label} 必须是非负实数，实际得到：{value!r}")
    return number


def _round(value: float, digits: int = 12) -> float:
    """按固定位数取整，保证跨进程、跨平台的字面量稳定。"""
    if not math.isfinite(value):
        return value
    return round(value + 0.0, digits)


def _jsonable(value: Any) -> Any:
    """把嵌套结构递归转换为 JSON 兼容对象（元组转列表，映射按键名排序）。"""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 重采样单位解析
# ---------------------------------------------------------------------------


def resolve_split_unit(
    spec: Mapping[str, Any] | None,
    *,
    split_unit: str | None = None,
    group_field: str | None = None,
) -> dict:
    """确定重采样单位：从 M1 的 spec 读取 ``dependence.split_unit``。

    :param spec: M1 数据配置（``IMPLEMENTATION_CONTRACT.md`` 的 spec 结构）。
    :param split_unit: 显式指定的重采样单位；``None`` 表示取 spec 中的值。
    :param group_field: 显式指定的分组字段；``None`` 时取 ``split_unit`` 本身。
    :return: ``{"split_unit", "group_field", "namespace", "group_fields",
        "record_unit", "inference_unit"}``。
    :raises StabilityError: 未提供 spec、缺字段、单位不在 ``group_fields`` 中，
        或单位名为空。
    """
    dependence: Mapping[str, Any] | None = None
    if spec is not None:
        if not isinstance(spec, Mapping):
            raise StabilityError("spec 必须是字典（M1 数据配置）或 None")
        raw = spec.get("dependence")
        if not isinstance(raw, Mapping):
            raise StabilityError("spec 缺少 dependence 段，无法确定重采样单位")
        dependence = raw

    resolved_unit: str | None = split_unit
    if resolved_unit is None and dependence is not None:
        candidate = dependence.get("split_unit")
        if isinstance(candidate, str) and candidate.strip():
            resolved_unit = candidate

    if not isinstance(resolved_unit, str) or not resolved_unit.strip():
        raise StabilityError(
            "无法确定重采样单位：必须提供 spec['dependence']['split_unit']，"
            "或显式给出 split_unit。本模块不会默认按记录级重采样——"
            "那正是 P06 验收标准明令禁止的做法。"
        )
    resolved_unit = resolved_unit.strip()

    group_fields: tuple[str, ...] = ()
    if dependence is not None:
        raw_fields = dependence.get("group_fields")
        if isinstance(raw_fields, (list, tuple)):
            names: list[str] = []
            for name in raw_fields:
                if not isinstance(name, str) or not name.strip():
                    raise StabilityError(
                        f"spec['dependence']['group_fields'] 只能包含非空字符串，"
                        f"实际得到：{name!r}"
                    )
                names.append(name.strip())
            group_fields = tuple(names)
        if group_fields and resolved_unit not in group_fields:
            raise StabilityError(
                f"重采样单位 {resolved_unit!r} 不在 spec 声明的 group_fields "
                f"（{', '.join(group_fields)}）中：分组标识由 group_fields 派生，"
                "单位必须与之一致，否则就不是「与 split_unit 保持一致」。"
            )

    resolved_field: str | None = group_field
    if resolved_field is None:
        resolved_field = resolved_unit
    if not isinstance(resolved_field, str) or not resolved_field.strip():
        raise StabilityError(f"group_field 必须是非空字符串，实际得到：{group_field!r}")

    namespace: str | None = None
    if dependence is not None:
        raw_namespace = dependence.get("namespace")
        if isinstance(raw_namespace, str) and raw_namespace.strip():
            namespace = raw_namespace
    return {
        "split_unit": resolved_unit,
        "group_field": resolved_field.strip(),
        "namespace": namespace,
        "group_fields": list(group_fields),
        "record_unit": (dependence or {}).get("record_unit"),
        "inference_unit": (dependence or {}).get("inference_unit"),
    }


def group_key_of(
    record: Mapping[str, Any],
    group_field: str,
    *,
    namespace: str | None = None,
) -> str:
    """按 M1 ``unit_keys`` 的口径构造一条记录的分组标识。

    M1 用 ``canonical_json([namespace, 字段名, 取值])`` 派生 ``unit_keys``。
    本模块用等价的稳定 JSON 编码自行派生，不导入 M1 内部实现，也不修改它。

    :raises StabilityError: 记录不是字典、缺 ``group_ids``、字段缺失或取值非法
        （空字符串、``bool``、非标量）。
    """
    if not isinstance(record, Mapping):
        raise StabilityError("每条记录必须是字典")
    groups = record.get("group_ids")
    if not isinstance(groups, Mapping):
        raise StabilityError(
            f"记录 {record.get('record_id')!r} 缺少 group_ids，无法确定其观测单位"
        )
    if group_field not in groups:
        raise StabilityError(
            f"记录 {record.get('record_id')!r} 的 group_ids 中缺少字段 "
            f"{group_field!r}，无法确定其观测单位"
        )
    value = groups[group_field]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise StabilityError(
            f"记录 {record.get('record_id')!r} 的分组取值必须是字符串或整数，"
            f"实际得到：{value!r}"
        )
    if isinstance(value, str) and not value.strip():
        raise StabilityError(
            f"记录 {record.get('record_id')!r} 的分组取值为空字符串，无法确定观测单位"
        )
    payload = [namespace, group_field, value]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# 分组样本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GroupedSample:
    """按观测单位编组后的探索样本。"""

    sample: ExplorationSample
    group_of_row: tuple[str, ...]
    groups: tuple[str, ...]
    rows_by_group: Mapping[str, tuple[int, ...]]
    unit_info: Mapping[str, Any]

    def unit_summary(self) -> dict:
        """分组规模的描述性统计（不展开任何记录正文）。"""
        sizes = sorted(len(self.rows_by_group[key]) for key in self.groups)
        count = len(sizes)
        middle = count // 2
        if count == 0:
            median = 0.0
        elif count % 2 == 1:
            median = float(sizes[middle])
        else:
            median = 0.5 * (sizes[middle - 1] + sizes[middle])
        level = "group" if sizes and sizes[-1] > 1 else "record"
        note = (
            "组级重采样：同组记录整组进出。"
            if level == "group"
            else
            "spec 声明的分组下每组仅含一条记录，分组级重采样在效果上等同于记录级；"
            "这是 spec 本身决定的口径，本模块未擅自降级。"
        )
        return {
            "split_unit": self.unit_info.get("split_unit"),
            "group_field": self.unit_info.get("group_field"),
            "namespace": self.unit_info.get("namespace"),
            "record_unit": self.unit_info.get("record_unit"),
            "inference_unit": self.unit_info.get("inference_unit"),
            "n_rows": len(self.sample.rows),
            "n_groups": count,
            "rows_per_group": {
                "min": sizes[0] if sizes else 0,
                "max": sizes[-1] if sizes else 0,
                "median": _round(median),
            },
            "singleton_groups": sum(1 for size in sizes if size <= 1),
            "unit_level": level,
            "note": note,
        }


def _extract_rows(records: Any) -> list[Mapping[str, Any]]:
    """把记录来源归一为字典列表（类型错误在此拦下，便于给出清晰报错）。"""
    if isinstance(records, (str, bytes, Mapping)):
        raise StabilityError(
            "records 必须是记录序列或 ExplorationSample；不接受路径或配置字典"
        )
    if not isinstance(records, Iterable):
        raise StabilityError("records 必须是记录序列或 ExplorationSample")
    rows = list(records)
    for row in rows:
        if not isinstance(row, Mapping):
            raise StabilityError("每条记录必须是字典")
    return rows


def _require_exploration_sample(sample: Any) -> ExplorationSample:
    """校验对象是探索分区 E 的样本。"""
    if not isinstance(sample, ExplorationSample):
        raise StabilityError(
            "必须提供 ExplorationSample；实际得到：" + type(sample).__name__
        )
    if sample.purpose != EXPLORATION_PURPOSE:
        raise StabilityPolicyError(
            f"本模块只评估探索分区 {EXPLORATION_PURPOSE}；"
            f"拒绝作用途为 {sample.purpose!r} 的样本"
        )
    return sample


def _build_grouped(
    sample: ExplorationSample,
    raw_rows: Sequence[Mapping[str, Any]] | None,
    group_keys: Mapping[str, str] | Sequence[str] | None,
    unit_info: Mapping[str, Any],
) -> _GroupedSample:
    """把样本的每一行映射到其观测单位（分组）标识。"""
    group_field = str(unit_info["group_field"])
    namespace = unit_info.get("namespace")

    mapping: dict[str, str] = {}
    ordered_keys: Sequence[str] | None = None
    if group_keys is not None:
        if isinstance(group_keys, Mapping):
            for key, value in group_keys.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise StabilityError("group_keys 映射的键与值都必须是字符串")
                mapping[key] = value
        elif isinstance(group_keys, Sequence) and not isinstance(group_keys, (str, bytes)):
            if len(group_keys) != len(sample.rows):
                raise StabilityError(
                    f"group_keys 序列长度 {len(group_keys)} 与样本行数 "
                    f"{len(sample.rows)} 不一致"
                )
            ordered_keys = list(group_keys)
        else:
            raise StabilityError("group_keys 必须是「记录标识 → 分组」映射或字符串序列")
    else:
        if raw_rows is None:
            raise StabilityError(
                "传入 ExplorationSample 时必须另给 group_keys："
                "样本对象只保留对齐后的取值，已不含 group_ids，"
                "无法据此判断观测单位。"
            )
        for record in raw_rows:
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                raise StabilityError(
                    "原始记录缺少可用的 record_id，无法与对齐后的样本匹配分组"
                )
            key = group_key_of(record, group_field, namespace=namespace)
            previous = mapping.get(record_id)
            if previous is not None and previous != key:
                raise StabilityError(
                    f"记录标识 {record_id!r} 重复且分组不一致（{previous!r} vs {key!r}）："
                    "无法确定其观测单位"
                )
            mapping[record_id] = key

    group_of_row: list[str] = []
    for index, row in enumerate(sample.rows):
        if ordered_keys is not None:
            key = ordered_keys[index]
            if not isinstance(key, str) or not key:
                raise StabilityError(f"group_keys[{index}] 必须是非空字符串")
        else:
            key = mapping.get(row.record_id) or ""
        if not key:
            raise StabilityError(
                f"样本第 {index} 行（记录 {row.record_id!r}）找不到分组标识；"
                "分组必须覆盖样本中的每一行。"
            )
        group_of_row.append(key)

    rows_by_group: dict[str, list[int]] = {}
    for index, key in enumerate(group_of_row):
        rows_by_group.setdefault(key, []).append(index)
    groups = tuple(sorted(rows_by_group))
    return _GroupedSample(
        sample=sample,
        group_of_row=tuple(group_of_row),
        groups=groups,
        rows_by_group={key: tuple(value) for key, value in rows_by_group.items()},
        unit_info=dict(unit_info),
    )


# ---------------------------------------------------------------------------
# 组合工具
# ---------------------------------------------------------------------------


def _wilson_lower(success: int, total: int, z: float = 1.96) -> float:
    """Wilson 比例区间下界（描述性，**不是**错误率控制）。"""
    if total <= 0:
        return 0.0
    proportion = success / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
    ) / denominator
    return max(0.0, min(1.0, center - half))


def _iter_fit_results(payload: Any) -> Iterable[FitResult]:
    """递归收集嵌套结构中的全部 :class:`FitResult`（顺序稳定）。"""
    if isinstance(payload, FitResult):
        yield payload
    elif isinstance(payload, Mapping):
        for key in sorted(payload, key=str):
            yield from _iter_fit_results(payload[key])
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _iter_fit_results(item)


def _best_baseline_loss(fit: Any, loss: str) -> float | None:
    """取全部基线中「最难超越」者的损失（MSE 等越低越好的损失）。"""
    values: list[float] = []
    for result in _iter_fit_results(fit.baselines):
        if loss in result.metrics:
            number = float(result.metrics[loss])
            if math.isfinite(number):
                values.append(number)
    return min(values) if values else None


def _pairwise_agreement(
    origin: Sequence[int],
    candidate: Sequence[int],
) -> float:
    """成对共簇一致率（Rand 指数），与簇编号无关。

    用计数表算，复杂度 ``O(n)``；样本量再大也不会退化成 ``O(n²)``。
    """
    if len(origin) != len(candidate):
        raise StabilityError("成对一致率要求两个划分长度一致")
    count = len(origin)
    if count < 2:
        return 0.0
    cells: dict[tuple[int, int], int] = {}
    left: dict[int, int] = {}
    right: dict[int, int] = {}
    for a, b in zip(origin, candidate):
        cells[(a, b)] = cells.get((a, b), 0) + 1
        left[a] = left.get(a, 0) + 1
        right[b] = right.get(b, 0) + 1

    def pairs(size: int) -> int:
        return size * (size - 1) // 2

    total_pairs = pairs(count)
    if total_pairs == 0:
        return 0.0
    same_both = sum(pairs(size) for size in cells.values())
    same_left = sum(pairs(size) for size in left.values())
    same_right = sum(pairs(size) for size in right.values())
    concordant = same_both + (total_pairs - same_left - same_right + same_both)
    return concordant / total_pairs


def _subjects_of(candidate: Any) -> tuple[str, Any]:
    """区分「关系候选」与「结构模式」，返回 ``(路径, 载荷)``。"""
    if isinstance(candidate, StructurePattern):
        return "pattern", [candidate]
    if isinstance(candidate, (list, tuple)) and candidate and all(
        isinstance(item, StructurePattern) for item in candidate
    ):
        return "pattern", list(candidate)
    return "relation", candidate


def _pattern_kind(patterns: Sequence[StructurePattern]) -> str:
    """取一组模式的种类；混杂种类或未知种类一律报错。"""
    kinds = {pattern.kind for pattern in patterns}
    if len(kinds) != 1:
        raise StabilityError(
            "一次稳定性评估只能处理同一检测器产出的模式；"
            f"实测种类：{', '.join(sorted(kinds))}"
        )
    kind = next(iter(kinds))
    if kind not in SUBJECT_KINDS:
        raise StabilityError(
            f"不支持评估的模式种类：{kind!r}；仅支持 "
            f"{', '.join(name for name in SUBJECT_KINDS if name != 'relation')}"
        )
    return kind


def _series_from_text(text: Any) -> Any:
    """把 P05 记录的序列描述还原为可供重放的输入。"""
    if text is None:
        return None
    if not isinstance(text, str):
        raise StabilityError("series 的还原目标必须是字符串或 None")
    stripped = text.strip()
    if stripped.startswith("目标列"):
        return None
    try:
        return parse_expression(stripped)
    except ExpressionError as exc:
        raise StabilityError(
            f"无法从 P05 的来源信息还原序列表达式：{text!r}（{exc}）；"
            "请通过 params 显式给出 series。"
        ) from exc


def _merge_params(
    pattern: StructurePattern,
    override: Mapping[str, Any] | None,
) -> dict:
    """从模式来源信息还原重放参数，并用调用方给出的覆盖项合并。"""
    provenance = pattern.provenance
    recorded = provenance.get("parameters") if isinstance(provenance, Mapping) else None
    if not isinstance(recorded, Mapping):
        recorded = {}
    merged = dict(recorded)
    if override:
        if not isinstance(override, Mapping):
            raise StabilityError("params 必须是字典或 None")
        merged.update(override)
    return merged


# ---------------------------------------------------------------------------
# 稳定性分数
# ---------------------------------------------------------------------------


def stability_score(evidence: Any) -> float:
    """把重采样证据折算为落在 ``[0, 1]`` 的稳定性分数 ``S(H)``。

    接受三种输入：

    - :class:`StabilityReport`：用其 ``n_effective`` 与 ``n_reproduced``；
    - 映射：键 ``("n_effective", "n_reproduced")``、``("n_resamples",
      "n_reproduced")``、``("total", "success")`` 三选一，或直接给
      ``"reproduced"`` 布尔序列；
    - 布尔序列：真值个数除以长度。

    :return: ``复现次数 / 有效次数``，恒落在 ``[0, 1]``；有效次数为 0 时返回 ``0.0``
        （**而不是** ``None`` 或 ``nan``，以便下游统一按数值处理）。
    :raises StabilityError: 输入形态非法、次数不是非负整数，或复现次数大于总次数。
    """
    total: int
    success: int

    if isinstance(evidence, StabilityReport):
        total, success = evidence.n_effective, evidence.n_reproduced
    elif isinstance(evidence, Mapping):
        if "reproduced" in evidence:
            flags = evidence["reproduced"]
            if isinstance(flags, (str, bytes)) or not isinstance(flags, Sequence):
                raise StabilityError("映射中的 reproduced 必须是布尔序列")
            total, success = len(flags), sum(1 for flag in flags if flag)
        else:
            pairs = (("n_effective", "n_reproduced"), ("n_resamples", "n_reproduced"), ("total", "success"))
            selected: tuple[Any, Any] | None = None
            for left, right in pairs:
                if left in evidence and right in evidence:
                    selected = (evidence[left], evidence[right])
                    break
            if selected is None:
                raise StabilityError(
                    "映射必须包含 (n_effective, n_reproduced)、"
                    "(n_resamples, n_reproduced)、(total, success) 之一，"
                    "或直接给出 reproduced 布尔序列"
                )
            total, success = selected  # type: ignore[assignment]
    elif isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        total, success = len(evidence), sum(1 for flag in evidence if flag)
    else:
        raise StabilityError(
            "stability_score 的输入必须是 StabilityReport、含计数的映射或布尔序列；"
            f"实际得到：{type(evidence).__name__}"
        )

    for value, label in ((total, "总次数"), (success, "复现次数")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise StabilityError(f"{label} 必须是整数，实际得到：{value!r}")
        if value < 0:
            raise StabilityError(f"{label} 必须非负，实际得到：{value!r}")
    if success > total:
        raise StabilityError(
            f"复现次数不得大于总次数：{success} > {total}"
        )
    if total == 0:
        return 0.0
    return max(0.0, min(1.0, success / total))


# ---------------------------------------------------------------------------
# 评估报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StabilityReport:
    """一次稳定性与重采样评估的完整结果。

    :param subject_kind: 被试种类：``relation`` / ``cluster`` / ``changepoint`` /
        ``invariant``。
    :param subject_id: 被试标识（候选 ``candidate_id``、模式 ``pattern_id``
        或聚类分区的签名）。
    :param stability: 稳定性分数 ``S(H)``，**恒落在 ``[0, 1]``**；
        等于 ``n_reproduced / n_effective``，无有效重采样时为 ``0.0``。
    :param sufficient: 证据是否足以支撑采用该分数。为 ``False`` 时
        ``stability`` 仍是一个 ``[0, 1]`` 的数，但**不应被采用**，
        原因见 ``reasons``。
    :param reasons: 判为「不足以给出结论」的具体原因；``sufficient`` 为
        ``True`` 时为空元组。
    :param criteria: 本次采用的复现判据与阈值（逐条文字化）。
    :param unit_summary: 重采样单位与分组规模摘要，不含任何记录正文。
    :param details: 重采样聚合量（含抽样明细 ``draws``，供人工复核分组级抽样）。
    :param provenance: 模块版本、样本指纹、种子与用到的参数。
    """

    subject_kind: str
    subject_id: str
    split_unit: str
    group_field: str
    namespace: str | None
    n_rows: int
    n_groups: int
    n_resamples: int
    n_effective: int
    n_reproduced: int
    stability: float
    sufficient: bool
    reasons: tuple[str, ...]
    seed: int
    replace: bool
    criteria: Mapping[str, Any]
    unit_summary: Mapping[str, Any]
    details: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.subject_kind not in SUBJECT_KINDS:
            raise StabilityError(
                f"未知的被试种类：{self.subject_kind!r}；仅支持 {', '.join(SUBJECT_KINDS)}"
            )
        if not (0.0 <= float(self.stability) <= 1.0):
            raise StabilityError(
                f"稳定性分数必须落在 [0, 1] 内，实际得到：{self.stability!r}"
            )
        if self.n_effective < 0 or self.n_reproduced < 0:
            raise StabilityError("有效次数与复现次数都必须非负")
        if self.n_reproduced > self.n_effective:
            raise StabilityError(
                f"复现次数不得大于有效次数：{self.n_reproduced} > {self.n_effective}"
            )
        if not self.criteria:
            raise StabilityError("稳定性报告必须写明复现判据")
        if not self.unit_summary:
            raise StabilityError("稳定性报告必须写明重采样单位摘要")

    @property
    def effective_rate(self) -> float:
        """有效重采样占比（拟合/检测能够完成的次数比例）。"""
        if self.n_resamples <= 0:
            return 0.0
        return self.n_effective / self.n_resamples

    @property
    def reproduced_rate(self) -> float:
        """复现比例，即稳定性分数本身。"""
        if self.n_effective <= 0:
            return 0.0
        return self.n_reproduced / self.n_effective

    @property
    def score(self) -> float:
        """可直接交给 M5 的稳定性分数（落在 ``[0, 1]``）。"""
        return stability_score(self)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（形状稳定、可逐字节比对）。"""
        return {
            "version": STABILITY_VERSION,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "split_unit": self.split_unit,
            "group_field": self.group_field,
            "namespace": self.namespace,
            "n_rows": self.n_rows,
            "n_groups": self.n_groups,
            "n_resamples": self.n_resamples,
            "n_effective": self.n_effective,
            "n_reproduced": self.n_reproduced,
            "stability": self.stability,
            "effective_rate": self.effective_rate,
            "sufficient": self.sufficient,
            "reasons": list(self.reasons),
            "seed": self.seed,
            "replace": self.replace,
            "criteria": _jsonable(self.criteria),
            "unit_summary": _jsonable(self.unit_summary),
            "details": _jsonable(self.details),
            "provenance": _jsonable(self.provenance),
            "criterion_note": STABILITY_CRITERION_NOTE,
        }


# ---------------------------------------------------------------------------
# 被试标识
# ---------------------------------------------------------------------------


def _relation_subject_id(candidate: Any) -> str:
    """关系候选的稳定标识。"""
    identifier = getattr(candidate, "candidate_id", None)
    if isinstance(identifier, str) and identifier:
        return identifier
    if isinstance(candidate, str):
        return f"expr:{candidate}"
    if isinstance(candidate, tuple):
        return "ast:" + json.dumps(_jsonable(candidate), ensure_ascii=False, separators=(",", ":"))
    return f"candidate:{type(candidate).__name__}"


def _pattern_subject_id(
    kind: str,
    patterns: Sequence[StructurePattern],
) -> str:
    """结构被试的稳定标识：聚类用分区签名，其余用模式标识的有序串联。"""
    if kind == "cluster":
        signatures = {
            str(pattern.payload.get("partition_signature")) for pattern in patterns
        }
        if len(signatures) != 1:
            raise StabilityError(
                "聚类稳定性要求传入同一分区的全部簇模式（partition_signature 必须一致）"
            )
        return "partition:" + next(iter(signatures))
    return "patterns:" + ",".join(sorted(pattern.pattern_id for pattern in patterns))


# ---------------------------------------------------------------------------
# 重采样
# ---------------------------------------------------------------------------


def _draw_groups(
    grouped: _GroupedSample,
    rng: random.Random,
    *,
    replace: bool,
    subsample_fraction: float,
) -> list[str]:
    """按观测单位抽取一次重采样所用的组序列。"""
    groups = list(grouped.groups)
    if replace:
        return list(rng.choices(groups, k=len(groups)))
    size = max(2, int(math.ceil(subsample_fraction * len(groups))))
    size = min(size, len(groups))
    return list(rng.sample(groups, k=size))


def _resample_indices(grouped: _GroupedSample, drawn: Sequence[str]) -> list[int]:
    """把抽到的组展开为行下标（同组的行整组一起进入）。"""
    indices: list[int] = []
    for key in drawn:
        indices.extend(grouped.rows_by_group[key])
    return indices


# ---------------------------------------------------------------------------
# 关系候选路径
# ---------------------------------------------------------------------------


def _evaluate_relation(
    candidate: Any,
    grouped: _GroupedSample,
    rng: random.Random,
    *,
    n_resamples: int,
    seed: int,
    replace: bool,
    subsample_fraction: float,
    baselines: Sequence[str],
    loss: str,
    min_groups: int,
    min_effective: int,
    draw_sample: int,
) -> StabilityReport:
    """关系候选的分组级重采样评估。"""
    if not isinstance(candidate, (str, tuple)) and not hasattr(candidate, "ast"):
        raise StabilityError(
            "关系候选必须是 Candidate、AST 元组或表达式文本；"
            "**不接受预先算好的数值列**——重采样会改变行集，数值列无法与子样本对齐。"
        )

    sample = grouped.sample
    unit = grouped.unit_info
    reasons: list[str] = []
    criteria = {
        "rule": (
            "重采样子样本上重新拟合候选与全部基线，复现当且仅当："
            "①候选损失严格低于所有基线中最难超越者；②候选斜率符号与全样本一致"
        ),
        "baseline_rule": "以全部基线中损失最小者为最难超越基线",
        "loss": loss,
        "hardest_baseline_is_min": True,
        "note": "斜率符号一致性用于排除「换一批单位就翻转方向」的伪稳定",
    }

    full_fit: Any = None
    full_failure: str | None = None
    try:
        full_fit = fit_relation(sample, candidate, baselines=baselines)
    except ValueError as exc:
        full_failure = f"{type(exc).__name__}: {exc}"

    if full_fit is None:
        reasons.append(f"全样本无法拟合候选：{full_failure}")
        details = {
            "full_sample_baseline_loss": None,
            "full_sample_candidate_loss": None,
            "sign_agreement_rate": 0.0,
            "median_loss_difference": None,
            "failure_reasons": {"全样本不可拟合": 1},
            "draws": [],
        }
        return StabilityReport(
            subject_kind="relation",
            subject_id=_relation_subject_id(candidate),
            split_unit=str(unit["split_unit"]),
            group_field=str(unit["group_field"]),
            namespace=unit.get("namespace"),
            n_rows=len(sample.rows),
            n_groups=len(grouped.groups),
            n_resamples=n_resamples,
            n_effective=0,
            n_reproduced=0,
            stability=0.0,
            sufficient=False,
            reasons=tuple(reasons),
            seed=seed,
            replace=replace,
            criteria=criteria,
            unit_summary=grouped.unit_summary(),
            details=details,
            provenance=_provenance(sample, seed, n_resamples, "resample_evaluate/relation"),
        )

    full_baseline = _best_baseline_loss(full_fit, loss)
    full_candidate = float(full_fit.candidate.metrics[loss])
    full_slope = float(full_fit.candidate.parameters["slope"])
    full_sign = 0 if abs(full_slope) <= _EPS else (1 if full_slope > 0 else -1)
    criteria = dict(criteria)
    criteria["full_sample_candidate_loss"] = _round(full_candidate)
    criteria["full_sample_baseline_loss"] = (
        None if full_baseline is None else _round(full_baseline)
    )
    criteria["full_sample_slope"] = _round(full_slope)

    if full_baseline is None:
        # 没有任何基线可用：无法判断「候选是否优于基线」，不伪造结论。
        reasons.append("全样本上没有任何可用基线，无法判断候选是否优于基线")
        reasons.append("基线不可省略：resample_evaluate 需要 baselines 非空且可算")
        return StabilityReport(
            subject_kind="relation",
            subject_id=_relation_subject_id(candidate),
            split_unit=str(unit["split_unit"]),
            group_field=str(unit["group_field"]),
            namespace=unit.get("namespace"),
            n_rows=len(sample.rows),
            n_groups=len(grouped.groups),
            n_resamples=n_resamples,
            n_effective=0,
            n_reproduced=0,
            stability=0.0,
            sufficient=False,
            reasons=tuple(reasons),
            seed=seed,
            replace=replace,
            criteria=criteria,
            unit_summary=grouped.unit_summary(),
            details={
                "full_sample_baseline_loss": None,
                "full_sample_candidate_loss": _round(full_candidate),
                "sign_agreement_rate": 0.0,
                "median_loss_difference": None,
                "failure_reasons": {"无可用基线": 1},
                "draws": [],
            },
            provenance=_provenance(sample, seed, n_resamples, "resample_evaluate/relation"),
        )

    reproduced_flags: list[bool] = []
    sign_flags: list[bool] = []
    differences: list[float] = []
    failures: dict[str, int] = {}
    draws: list[dict] = []

    for index in range(n_resamples):
        drawn = _draw_groups(
            grouped, rng, replace=replace, subsample_fraction=subsample_fraction
        )
        indices = _resample_indices(grouped, drawn)
        if len(draws) < draw_sample:
            draws.append(
                {
                    "index": index,
                    "groups": list(drawn),
                    "n_groups_drawn": len(set(drawn)),
                    "n_rows": len(indices),
                }
            )
        distinct = len(set(drawn))
        if len(indices) < 2 or distinct < 2:
            failures["子样本组数或行数不足"] = failures.get("子样本组数或行数不足", 0) + 1
            continue
        subset = sample.subset(indices)
        try:
            fit = fit_relation(subset, candidate, baselines=baselines)
        except ValueError as exc:
            label = f"{type(exc).__name__}"
            failures[label] = failures.get(label, 0) + 1
            continue
        best = _best_baseline_loss(fit, loss)
        if best is None:
            failures["子样本无可用基线"] = failures.get("子样本无可用基线", 0) + 1
            continue
        current = float(fit.candidate.metrics[loss])
        slope = float(fit.candidate.parameters["slope"])
        current_sign = 0 if abs(slope) <= _EPS else (1 if slope > 0 else -1)
        same_sign = current_sign == full_sign
        sign_flags.append(same_sign)
        difference = best - current
        differences.append(difference)
        reproduced_flags.append(bool(difference > 0.0 and same_sign))

    effective = len(reproduced_flags)
    reproduced = sum(1 for flag in reproduced_flags if flag)
    score = stability_score({"n_effective": effective, "n_reproduced": reproduced})
    if len(grouped.groups) < min_groups:
        reasons.append(
            f"组数 {len(grouped.groups)} 少于 min_groups={min_groups}："
            "重采样缺乏分辨力，不足以给出结论"
        )
    if effective < min_effective:
        reasons.append(
            f"有效重采样 {effective} 次少于 min_effective={min_effective}："
            "多数子样本无法完成拟合，不足以给出结论"
        )

    details = {
        "full_sample_baseline_loss": _round(full_baseline),
        "full_sample_candidate_loss": _round(full_candidate),
        "sign_agreement_rate": _round(
            (sum(1 for flag in sign_flags if flag) / len(sign_flags)) if sign_flags else 0.0
        ),
        "median_loss_difference": (
            _round(sorted(differences)[len(differences) // 2]) if differences else None
        ),
        "min_loss_difference": _round(min(differences)) if differences else None,
        "failure_reasons": {key: failures[key] for key in sorted(failures)},
        "draws": draws,
        "wilson_lower_95": _round(_wilson_lower(reproduced, effective)),
    }
    return StabilityReport(
        subject_kind="relation",
        subject_id=_relation_subject_id(candidate),
        split_unit=str(unit["split_unit"]),
        group_field=str(unit["group_field"]),
        namespace=unit.get("namespace"),
        n_rows=len(sample.rows),
        n_groups=len(grouped.groups),
        n_resamples=n_resamples,
        n_effective=effective,
        n_reproduced=reproduced,
        stability=score,
        sufficient=not reasons,
        reasons=tuple(reasons),
        seed=seed,
        replace=replace,
        criteria=criteria,
        unit_summary=grouped.unit_summary(),
        details=details,
        provenance=_provenance(sample, seed, n_resamples, "resample_evaluate/relation"),
    )


# ---------------------------------------------------------------------------
# 结构模式路径
# ---------------------------------------------------------------------------


def _cluster_partition(
    patterns: Sequence[StructurePattern],
) -> tuple[dict[str, int], int]:
    """从同一分区的簇模式还原「记录 → 簇编号」与簇数。"""
    assignment: dict[str, int] = {}
    indexes: set[int] = set()
    for pattern in patterns:
        payload = pattern.payload
        try:
            cluster_index = int(payload["cluster_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StabilityError(
                f"聚类模式 {pattern.pattern_id!r} 缺少可用的 cluster_index"
            ) from exc
        indexes.add(cluster_index)
        members = payload.get("members")
        if not isinstance(members, (list, tuple)) or not members:
            raise StabilityError(
                f"聚类模式 {pattern.pattern_id!r} 的 members 必须是非空序列"
            )
        for member in members:
            if not isinstance(member, str):
                raise StabilityError("聚类模式的成员必须记录标识字符串")
            previous = assignment.get(member)
            if previous is not None and previous != cluster_index:
                raise StabilityError(
                    f"记录 {member!r} 同时属于多个簇，分区不一致"
                )
            assignment[member] = cluster_index
    if not assignment:
        raise StabilityError("聚类模式未给出任何成员，无法评估稳定性")
    return assignment, len(indexes)


def _evaluate_patterns(
    patterns: Sequence[StructurePattern],
    grouped: _GroupedSample,
    rng: random.Random,
    *,
    n_resamples: int,
    seed: int,
    replace: bool,
    subsample_fraction: float,
    per_kind: Mapping[str, Any],
    min_groups: int,
    min_effective: int,
    draw_sample: int,
) -> StabilityReport:
    """P05 结构模式的分组级重采样评估。"""
    kind = _pattern_kind(patterns)
    sample = grouped.sample
    unit = grouped.unit_info
    reasons: list[str] = []

    if kind == "cluster":
        origin, origin_k = _cluster_partition(patterns)
        agreement_floor = per_kind["min_agreement"]
        criteria = {
            "rule": (
                "重采样后重新选簇并聚类，复现当且仅当：①簇数与全样本一致；"
                "②两次都出现的记录集合上成对共簇一致率（Rand 指数）不低于阈值"
            ),
            "min_agreement": agreement_floor,
            "k": origin_k,
            "note": "成对比较与簇编号无关，因此不依赖簇的重命名",
        }
        parameters = _merge_params(patterns[0], per_kind.get("params"))
    elif kind == "changepoint":
        origin_positions = sorted(
            int(pattern.payload["position"]) for pattern in patterns
        )
        position_tolerance = per_kind["position_tolerance"]
        criteria = {
            "rule": (
                "重采样后重新检测变点，复现当且仅当：①变点数量一致；"
                "②各变点的相对位置偏差不超过阈值"
            ),
            "position_tolerance": position_tolerance,
            "n_changepoints": len(origin_positions),
            "note": "用相对位置而非绝对行号，因为重采样后序列长度会变化",
        }
        parameters = _merge_params(patterns[0], per_kind.get("params"))
    else:
        origin_estimate = float(patterns[0].payload.get("estimate", 0.0))
        estimate_tolerance = per_kind["estimate_tolerance"]
        criteria = {
            "rule": (
                "重采样后重新判定不变量，复现当且仅当：①判据仍然成立（返回了模式）；"
                "②估计量的相对偏差不超过阈值"
            ),
            "estimate_tolerance": estimate_tolerance,
            "estimate": _round(origin_estimate),
            "note": "判据本身沿用 P05 的一致行占比规则与调用方给定的容差",
        }
        parameters = _merge_params(patterns[0], per_kind.get("params"))

    reproduced_flags: list[bool] = []
    agreements: list[float] = []
    position_deviations: list[float] = []
    estimate_deviations: list[float] = []
    failures: dict[str, int] = {}
    draws: list[dict] = []

    for index in range(n_resamples):
        drawn = _draw_groups(
            grouped, rng, replace=replace, subsample_fraction=subsample_fraction
        )
        indices = _resample_indices(grouped, drawn)
        if len(draws) < draw_sample:
            draws.append(
                {
                    "index": index,
                    "groups": list(drawn),
                    "n_groups_drawn": len(set(drawn)),
                    "n_rows": len(indices),
                }
            )
        distinct = len(set(drawn))
        if len(indices) < 2 or distinct < 2:
            failures["子样本组数或行数不足"] = failures.get("子样本组数或行数不足", 0) + 1
            continue
        subset = sample.subset(indices)

        try:
            if kind == "cluster":
                found = find_clusters(
                    subset,
                    parameters.get("features"),
                    max_clusters=parameters.get(
                        "max_clusters", structure_module.DEFAULT_MAX_CLUSTERS
                    ),
                    min_cluster_size=parameters.get(
                        "min_cluster_size", structure_module.DEFAULT_MIN_CLUSTER_SIZE
                    ),
                    seed=int(parameters.get("seed", structure_module.DEFAULT_SEED)),
                    max_iterations=parameters.get(
                        "max_iterations", structure_module.DEFAULT_MAX_ITERATIONS
                    ),
                    min_silhouette=parameters.get(
                        "min_silhouette", structure_module.DEFAULT_MIN_SILHOUETTE
                    ),
                )
                if not found:
                    failures["重采样未检出聚类结构"] = (
                        failures.get("重采样未检出聚类结构", 0) + 1
                    )
                    continue
                new_assignment, new_k = _cluster_partition(found)
                shared = sorted(set(origin) & set(new_assignment))
                if len(shared) < 2:
                    failures["共有记录不足"] = failures.get("共有记录不足", 0) + 1
                    continue
                agreement = _pairwise_agreement(
                    [origin[key] for key in shared],
                    [new_assignment[key] for key in shared],
                )
                agreements.append(agreement)
                reproduced_flags.append(
                    bool(new_k == origin_k and agreement >= agreement_floor - _EPS)
                )
            elif kind == "changepoint":
                # P05 在来源信息里把序列记成可读文本（表达式或「目标列 …」），
                # 此处还原为可供重放的输入；调用方可用 params["series"] 直接覆盖。
                series = parameters.get("series")
                if isinstance(series, str):
                    series = _series_from_text(series)
                new_positions = changepoint_positions(
                    subset,
                    order_by=parameters.get("order_by"),
                    series=series,
                    min_segment=int(
                        parameters.get("min_segment", structure_module.DEFAULT_MIN_SEGMENT)
                    ),
                    max_changepoints=int(
                        parameters.get(
                            "max_changepoints", structure_module.DEFAULT_MAX_CHANGEPOINTS
                        )
                    ),
                    penalty=float(
                        parameters.get("penalty", structure_module.DEFAULT_PENALTY)
                    ),
                )
                if not new_positions:
                    failures["重采样未检出变点"] = failures.get("重采样未检出变点", 0) + 1
                    continue
                # 相对位置：各自除以**自身序列长度**——重采样后长度会变，
                # 绝对行号不可比，只有相对位置才有意义。
                origin_length = len(sample.rows)
                new_length = len(subset.rows)
                rel_origin = sorted(
                    position / max(origin_length, 1) for position in origin_positions
                )
                rel_new = sorted(
                    position / max(new_length, 1) for position in new_positions
                )
                if len(rel_new) != len(rel_origin):
                    position_deviations.append(1.0)
                    reproduced_flags.append(False)
                    continue
                deviation = max(abs(a - b) for a, b in zip(rel_origin, rel_new))
                position_deviations.append(deviation)
                reproduced_flags.append(bool(deviation <= position_tolerance + _EPS))
            else:
                expression = parameters.get("expression")
                if not isinstance(expression, str) or not expression.strip():
                    raise StabilityError(
                        "不变量重放需要 expression；请通过 params 显式给出。"
                    )
                if expression.strip() == "<预置数值列>":
                    raise StabilityError(
                        "不变量原始输入是预置数值列，无法在重采样样本上重放；"
                        "请改传表达式候选。"
                    )
                found = check_invariant(
                    subset,
                    expression,
                    tolerance=float(
                        parameters.get("tolerance", structure_module.DEFAULT_TOLERANCE)
                    ),
                    min_conformity=float(
                        parameters.get(
                            "min_conformity", structure_module.DEFAULT_MIN_CONFORMITY
                        )
                    ),
                    min_coverage=float(
                        parameters.get("min_coverage", structure_module.DEFAULT_MIN_COVERAGE)
                    ),
                )
                if not found:
                    failures["重采样判据不成立"] = failures.get("重采样判据不成立", 0) + 1
                    continue
                estimate = float(found[0].payload.get("estimate", 0.0))
                scale = max(abs(origin_estimate), 1.0)
                relative = abs(estimate - origin_estimate) / scale
                estimate_deviations.append(relative)
                reproduced_flags.append(bool(relative <= estimate_tolerance + _EPS))
        except StabilityError:
            # 配置类问题（缺 expression 等）必须向上抛出，不能被当作「本子样本失败」
            # 静默吞掉——那会把契约错误伪装成统计结论。
            raise
        except (ValueError, ZeroDivisionError) as exc:
            label = f"{type(exc).__name__}"
            failures[label] = failures.get(label, 0) + 1
            continue

    effective = len(reproduced_flags)
    reproduced = sum(1 for flag in reproduced_flags if flag)
    score = stability_score({"n_effective": effective, "n_reproduced": reproduced})
    if len(grouped.groups) < min_groups:
        reasons.append(
            f"组数 {len(grouped.groups)} 少于 min_groups={min_groups}："
            "重采样缺乏分辨力，不足以给出结论"
        )
    if effective < min_effective:
        reasons.append(
            f"有效重采样 {effective} 次少于 min_effective={min_effective}："
            "多数子样本无法完成检测，不足以给出结论"
        )

    details: dict[str, Any] = {
        "failure_reasons": {key: failures[key] for key in sorted(failures)},
        "draws": draws,
        "wilson_lower_95": _round(_wilson_lower(reproduced, effective)),
    }
    if kind == "cluster":
        details["median_agreement"] = (
            _round(sorted(agreements)[len(agreements) // 2]) if agreements else None
        )
        details["min_agreement_observed"] = _round(min(agreements)) if agreements else None
    elif kind == "changepoint":
        details["median_position_deviation"] = (
            _round(sorted(position_deviations)[len(position_deviations) // 2])
            if position_deviations
            else None
        )
        details["max_position_deviation"] = (
            _round(max(position_deviations)) if position_deviations else None
        )
    else:
        details["median_estimate_deviation"] = (
            _round(sorted(estimate_deviations)[len(estimate_deviations) // 2])
            if estimate_deviations
            else None
        )
        details["max_estimate_deviation"] = (
            _round(max(estimate_deviations)) if estimate_deviations else None
        )

    return StabilityReport(
        subject_kind=kind,
        subject_id=_pattern_subject_id(kind, patterns),
        split_unit=str(unit["split_unit"]),
        group_field=str(unit["group_field"]),
        namespace=unit.get("namespace"),
        n_rows=len(sample.rows),
        n_groups=len(grouped.groups),
        n_resamples=n_resamples,
        n_effective=effective,
        n_reproduced=reproduced,
        stability=score,
        sufficient=not reasons,
        reasons=tuple(reasons),
        seed=seed,
        replace=replace,
        criteria=criteria,
        unit_summary=grouped.unit_summary(),
        details=details,
        provenance=_provenance(
            sample, seed, n_resamples, f"resample_evaluate/{kind}"
        ),
    )


def _prepare_guarded(
    records: Any,
    variables: Any,
    target: Any,
    *,
    source_ref: Any,
    resources: Any,
) -> ExplorationSample:
    """调用 P04 的对齐入口，并把其**策略**错误转为本模块的策略错误。

    P04 的 :func:`~sdl_m03.equations.prepare_sample` 用
    :class:`~sdl_m03.equations.EquationPolicyError` 表达「只接受探索分区 E」。
    该异常不是 :class:`StabilityError` 的子类，若直接透传，调用方按本模块契约
    捕获异常时会漏掉它。故此处做一次显式转换：**策略含义不变，类型统一**。
    """
    try:
        return prepare_sample(
            records,
            variables,
            target,
            purpose=EXPLORATION_PURPOSE,
            source_ref=source_ref,
            resources=resources,
            client=None,
        )
    except EquationPolicyError as exc:
        raise StabilityPolicyError(str(exc)) from exc


def _provenance(
    sample: ExplorationSample,
    seed: int,
    n_resamples: int,
    detector: str,
) -> dict:
    """构造报告来源信息：只记录可核验的抽象信息。"""
    return {
        "origin": "stability",
        "detector": detector,
        "module": "sdl_m03/stability.py",
        "module_version": STABILITY_VERSION,
        "structure_module_version": getattr(structure_module, "STRUCTURE_VERSION", None),
        "sample_fingerprint": sample.fingerprint,
        "sample_rows": len(sample.rows),
        "source_ref": sample.source_ref,
        "purpose": sample.purpose,
        "seed": seed,
        "n_resamples": n_resamples,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def resample_evaluate(
    candidate: Any,
    records: Any,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    seed: int = DEFAULT_SEED,
    *,
    spec: Mapping[str, Any] | None = None,
    split_unit: str | None = None,
    group_field: str | None = None,
    variables: Sequence[str] | None = None,
    target: str | None = None,
    group_keys: Mapping[str, str] | Sequence[str] | None = None,
    baselines: Sequence[str] = DEFAULT_BASELINES,
    loss: str = "mse",
    params: Mapping[str, Any] | None = None,
    min_agreement: float = DEFAULT_MIN_AGREEMENT,
    position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
    estimate_tolerance: float = DEFAULT_ESTIMATE_TOLERANCE,
    min_groups: int = DEFAULT_MIN_GROUPS,
    min_effective: int | None = None,
    replace: bool = True,
    subsample_fraction: float = DEFAULT_SUBSAMPLE_FRACTION,
    draw_sample: int = DEFAULT_DRAW_SAMPLE,
    client: Any | None = None,
    source_ref: str | None = None,
    resources: Mapping[str, Any] | None = None,
) -> StabilityReport:
    """按观测单位重采样，评估关系候选或结构模式的稳定性。

    :param candidate: 被评估的对象，两类：

        - **关系候选**（P04）：:class:`~sdl_m02.domain.Candidate`、AST 元组或
          表达式文本；不接受预先算好的数值列（重采样会改变行集）；
        - **结构模式**（P05）：一个 :class:`~sdl_m03.structure.StructurePattern`，
          或**同一检测器一次调用的完整返回值**。聚类与变点必须传完整列表
          （``find_clusters`` / ``find_changepoints`` 的整份结果），
          因为分区与分段只有看全才能比对。
    :param records: 数据来源，二选一：记录序列（``list[dict]``），或已对齐的
        :class:`~sdl_m03.equations.ExplorationSample`（此时必须另给
        ``group_keys``，因为样本对象已不含 ``group_ids``）。
    :param n_resamples: 重采样次数（正整数）。
    :param seed: 随机种子（整数）。同一 ``(候选, 数据, n_resamples, seed)``
        恒得同一结果。
    :param spec: M1 数据配置。提供时从 ``dependence`` 读取 ``split_unit``、
        ``group_fields``、``namespace``、``record_unit``、``inference_unit``。
    :param split_unit: 显式覆盖重采样单位；``None`` 时取 spec 中的值。
        **既无 spec 又无本参数时抛错**——不默认按记录级重采样。
    :param group_field: 显式覆盖分组字段；``None`` 时取 ``split_unit`` 本身。
    :param variables: 自变量字段名序列（``records`` 为记录序列时必填）。
    :param target: 目标字段名（``records`` 为记录序列时必填）。
    :param group_keys: 分组来源，仅在传入 :class:`ExplorationSample` 时使用：
        记录标识到分组标识的映射，或与样本行一一对应的字符串序列。
    :param baselines: 关系候选的基线种类（默认常数与线性各一）；不可为空。
    :param loss: 用于对照的损失名（默认 ``mse``，越低越好）。
    :param params: 覆盖从模式来源信息还原的重放参数。
    :param min_agreement: 聚类复现所需的成对共簇一致率下限，落在 ``(0, 1]``。
    :param position_tolerance: 变点复现允许的相对位置偏差，非负。
    :param estimate_tolerance: 不变量复现允许的估计量相对偏差，非负。
    :param min_groups: 给出结论所需的最少组数；不足时 ``sufficient=False``。
    :param min_effective: 给出结论所需的最少有效重采样次数；``None`` 表示
        取 ``max(1, n_resamples // 2)``。
    :param replace: ``True`` 为按组自助法（有放回）；``False`` 为按组子抽样。
    :param subsample_fraction: ``replace=False`` 时抽样的组数比例。
    :param draw_sample: 报告中保留的抽样明细条数（便于人工复核）。
    :param client: 备选的探索侧客户端（带 ``read_dataset`` 与 ``role``）；
        与 ``records`` 至多提供其一。角色必须是 ``explorer``。
    :param source_ref: 数据引用；提供时按 E 归属规则校验（拒绝 V/C/Q/H）。
    :param resources: 分区映射；提供时启用严格归属校验。
    :return: :class:`StabilityReport`。``stability`` 恒落在 ``[0, 1]``；
        证据不足时 ``sufficient=False`` 并在 ``reasons`` 中说明原因。
    :raises StabilityError: 参数类型/取值非法、单位无法确定、分组缺覆盖、
        候选形态不可重采样等**契约**问题。
    :raises StabilityPolicyError: 样本用途不是探索分区 ``E``，或客户端角色不是
        ``explorer``。
    """
    rounds = _validate_positive_int(n_resamples, "n_resamples")
    random_seed = _validate_seed(seed)
    floor_groups = _validate_positive_int(min_groups, "min_groups")
    floor_effective = (
        max(1, rounds // 2)
        if min_effective is None
        else _validate_positive_int(min_effective, "min_effective")
    )
    agreement = _validate_unit_interval(min_agreement, "min_agreement")
    pos_tolerance = _validate_nonnegative(position_tolerance, "position_tolerance")
    est_tolerance = _validate_nonnegative(estimate_tolerance, "estimate_tolerance")
    fraction = _validate_unit_interval(subsample_fraction, "subsample_fraction")
    sample_limit = _validate_positive_int(draw_sample, "draw_sample")
    if isinstance(baselines, str) or not isinstance(baselines, Sequence):
        raise StabilityError("baselines 必须是种类序列，例如 ('constant', 'linear')")
    kinds: list[str] = []
    for kind in baselines:
        if kind not in BASELINE_KINDS:
            raise StabilityError(
                f"未知的基线种类 {kind!r}；仅支持 {', '.join(BASELINE_KINDS)}"
            )
        if kind not in kinds:
            kinds.append(kind)
    if not kinds:
        raise StabilityError(
            "基线不可省略：baselines 不能为空，至少需要 "
            f"{' 与 '.join(BASELINE_KINDS)} 之一"
        )
    baselines = tuple(kinds)
    if not isinstance(loss, str) or not loss.strip():
        raise StabilityError("loss 必须是非空字符串")

    unit_info = resolve_split_unit(spec, split_unit=split_unit, group_field=group_field)

    # -- 取样本（并保留原始记录用于还原分组） -----------------------------
    raw_rows: list[Mapping[str, Any]] | None
    if isinstance(records, ExplorationSample):
        sample = _require_exploration_sample(records)
        raw_rows = None
    else:
        if client is not None:
            if records is not None:
                raise StabilityError("records 与 client 不得同时提供")
            reader = getattr(client, "read_dataset", None)
            if not callable(reader):
                raise StabilityError("client 必须具备 read_dataset 方法")
            role = getattr(client, "role", None)
            if role is not None and role != EXPLORER_ROLE:
                raise StabilityPolicyError(
                    f"本模块只接受探索侧客户端（role == {EXPLORER_ROLE!r}）；"
                    f"实际角色：{role!r}"
                )
            if not source_ref:
                raise StabilityPolicyError("通过客户端读取数据时必须给出探索分区 E 的数据引用")
            raw_rows = _extract_rows(reader(source_ref))
            source = raw_rows
        else:
            if records is None:
                raise StabilityError("必须提供记录序列或已对齐的探索样本")
            raw_rows = _extract_rows(records)
            source = raw_rows
        if variables is None or target is None:
            raise StabilityError(
                "records 为记录序列时必须给出 variables 与 target，"
                "以对齐为可拟合样本"
            )
        sample = _require_exploration_sample(
            _prepare_guarded(
                source,
                variables,
                target,
                source_ref=source_ref,
                resources=resources,
            )
        )

    grouped = _build_grouped(sample, raw_rows, group_keys, unit_info)
    if not grouped.groups:
        raise StabilityError("样本中没有任何可用的观测单位，无法重采样")

    rng = random.Random(random_seed)
    path, payload = _subjects_of(candidate)
    if path == "relation":
        return _evaluate_relation(
            payload,
            grouped,
            rng,
            n_resamples=rounds,
            seed=random_seed,
            replace=bool(replace),
            subsample_fraction=fraction,
            baselines=baselines,
            loss=loss,
            min_groups=floor_groups,
            min_effective=floor_effective,
            draw_sample=sample_limit,
        )
    return _evaluate_patterns(
        payload,
        grouped,
        rng,
        n_resamples=rounds,
        seed=random_seed,
        replace=bool(replace),
        subsample_fraction=fraction,
        per_kind={
            "min_agreement": agreement,
            "position_tolerance": pos_tolerance,
            "estimate_tolerance": est_tolerance,
            "params": params,
        },
        min_groups=floor_groups,
        min_effective=floor_effective,
        draw_sample=sample_limit,
    )
