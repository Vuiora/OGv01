"""M3 关系与方程搜索（P04 交付物）。

本模块在 P03 的候选池之上做**关系拟合**：把每个候选表达式当作一个新的
合成变量 ``Z = f(X)``，用探索分区 E 的数据拟合「截距 + 斜率 × Z」形式的关系式，
并与**两类基线**在同一份数据上比较。

公开接口
--------

- :func:`fit_relation`：拟合候选关系，**同时**计算基线并给出对照结果。
- :func:`baseline_constant`：常数基线 ``y ≈ ȳ``（不可省略的对照之一）。
- :func:`baseline_linear`：单变量线性基线 ``y ≈ a + b·X_j``（对照之二）。
- :func:`residual_summary`：由真值与预测值给出误差、残差与诊断量。

另有支撑公开接口：:func:`prepare_sample`、:func:`restrict_to_domain`、
:func:`evaluate_expression`、:func:`render_expression`、:func:`formula_length`、
:func:`compare_on_same_data`、:func:`gain_against`。

三条验收标准与实现对应关系
--------------------------

① **只用探索数据拟合**：本模块自身不打开数据库、不读文件；数据只能经
   :func:`prepare_sample` 进入。该函数要求数据来源被显式标注为探索分区
   （``purpose == "E"``），拒绝 ``V`` / ``C*`` / ``Q`` / ``H_*``；
   若提供了分区映射（``spec["resources"]``），引用必须**恰好等于**映射中的 ``E``；
   若传入带 ``role`` 属性的客户端，则**强制角色为 explorer**。
   所有拟合结果都带有所用数据的指纹，使「用了哪份数据」可核验。

② **结果必含误差、残差与复杂度**：:class:`FitResult` 的 ``metrics``（误差与诊断量）、
   ``residual_summary``（残差摘要 + 逐点残差）、``complexity``（复杂度）三项恒存在，
   且在 ``__post_init__`` 中被强制校验，无法构造缺项的结果。

③ **基线不可省略，且与候选在同一数据上比较**：:func:`fit_relation` 的
   ``baselines`` 参数若为空即报错；两类基线**默认全部开启**。
   基线所用的数据是候选经定义域限制之后**逐行相同**的子样本，
   并由 :func:`compare_on_same_data` 依数据指纹与行数复核；
   指纹或行数不一致即报错，从而不存在「基线用全量、候选用子集」这类不可比对照。

复杂度的口径（重要）
--------------------

``complexity`` 是**公式长度近似**，取「表达式节点数 + 可拟合参数个数」，
并附上渲染后的字符长度。这**不是严格 MDL**：

- 严格 MDL 需要一致的编码方式、噪声模型与数据表示，才能把「描述长度」
  与「数据压缩收益」放在同一尺度上比较；
- 本阶段只提供可比较的、单调于公式规模的近似量，供 M5 的复杂度轴使用。
  任何把该近似当作严格 MDL 的表述都是错的。

设计边界（严格遵守 P04 增量卡）
-------------------------------

- 本模块**不做结构模式搜索**（属 P05）：不涉及聚类、变点、不变量。
- 本模块**不做稳定性与重采样评估**（属 P06）：不做 bootstrap、交叉验证、
  重采样稳定性；所有指标都在单份 E 数据上一次算出。
- 本模块**不做假说构造**（属 M4）、**不做多维评估与筛选**（属 M5）、
  **不做确证检验**（属 M6）：不计算 p 值、不控制错误率、不授予证据等级。
- 本模块**不修改 P03 及更早的交付物**，只读取其公开接口。
- 所有公开函数均为**纯函数**：不写入任何模块级可变状态，同一输入恒得同一输出。
- 仅使用 Python 3.11+ 标准库，不引入第三方依赖。

已知限制
--------

1. **拟合粒度是「读数」而非「推断单位」。** 正式推断单位由数据配置声明
   （通常为批次）。本模块只做点估计与残差描述，因此按记录做等权最小二乘；
   把读数当独立样本会低估不确定性。按推断单位聚合 / 加权属 M5/M6 的职责，
   本次刻意不实现，以免在无误差控制的情况下暗示精度。
2. **模型形式固定为「截距 + 斜率 × Z」。** 这是框架说明 §4 的候选形式
   （``Y = a + bZ``）。更一般的非线性参数拟合不在本阶段范围内。
3. **不做多重比较校正。** 同时拟合很多候选必然产生「看起来最好」的候选，
   本模块只如实报告每个候选与基线的对照，不据此宣称发现。
4. **残差诊断量是描述性的**（R²、残差标准差等），不构成任何推断结论。
5. **线性基线只使用原始字段**，不引入构造变量；若某字段在样本上没有变异，
   该字段的线性基线无法定义，函数会如实报错（在 :func:`fit_relation` 中
   表现为该字段被跳过，若全部字段都无变异则整体报错）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from sdl_m02.domain import (
    DEFAULT_EPS,
    Candidate,
    DomainSpec,
    domain_ok,
)
from sdl_m02.expressions import (
    ExpressionError,
    from_ast,
    node_count as expression_node_count,
    parse as parse_expression,
    to_ast,
    variables as expression_variables,
)

__all__ = [
    "EquationError",
    "EquationPolicyError",
    "EQUATION_VERSION",
    "COMPLEXITY_NOTE",
    "EXPLORER_ROLE",
    "EXPLORATION_PURPOSE",
    "BASELINE_KINDS",
    "DEFAULT_BASELINES",
    "SampleRow",
    "ExplorationSample",
    "RestrictedSample",
    "ResidualSummary",
    "FitResult",
    "RelationFit",
    "prepare_sample",
    "evaluate_expression",
    "render_expression",
    "formula_length",
    "restrict_to_domain",
    "residual_summary",
    "baseline_constant",
    "baseline_linear",
    "fit_relation",
    "compare_on_same_data",
    "gain_against",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本模块（关系搜索）的版本号。字段或行为变化时递增。
EQUATION_VERSION = "1"

#: 探索侧唯一允许的角色名。
EXPLORER_ROLE = "explorer"

#: 唯一允许的数据用途。
EXPLORATION_PURPOSE = "E"

#: 两类基线的种类名。
BASELINE_KINDS: tuple[str, ...] = ("constant", "linear")

#: 基线的默认开启集合。**刻意包含两类**，使基线在默认路径上不可省略。
DEFAULT_BASELINES: tuple[str, ...] = BASELINE_KINDS

#: 复杂度的口径说明，随每个结果一起输出，避免被误读为严格 MDL。
COMPLEXITY_NOTE = (
    "复杂度为公式长度近似（表达式节点数 + 可拟合参数个数），不是严格 MDL；"
    "严格 MDL 需要一致的编码方式、噪声模型与数据表示。"
)

#: 非探索用途的引用前缀；出现即拒绝。
_RESTRICTED_PURPOSES = ("V", "C", "Q", "H")

#: 用于识别引用用途的引用前缀正则。
_REF_PREFIX_RE = re.compile(r"^[A-Za-z_]+")

#: 数值比较容差。
_EPS = 1e-12

#: 被视为缺失的字符串标记（大小写无关）。
_MISSING_MARKERS = ("", "NA", "NAN", "NULL")


class EquationError(ValueError):
    """关系拟合或对照计算的输入非法。"""


class EquationPolicyError(EquationError):
    """数据访问策略违规：非探索角色，或指向了非 E 用途的数据。"""


# ---------------------------------------------------------------------------
# 表达式渲染与求值
# ---------------------------------------------------------------------------


def _format_number(value: float) -> str:
    """把常数格式化为紧凑十进制文本。"""
    if abs(value - round(value)) <= _EPS and abs(value) < 1e15:
        return f"{int(round(value))}"
    return f"{value:g}"


def render_expression(ast: tuple) -> str:
    """把规范式（或原始式）AST 渲染为中缀文本，用于复杂度与报告。"""
    kind = ast[0]
    if kind == "num":
        return _format_number(ast[1])
    if kind == "var":
        return ast[1]
    if kind == "neg":
        inner = render_expression(ast[1])
        return f"-{inner}" if ast[1][0] in ("num", "var") else f"-({inner})"
    if kind == "inv":
        return f"1/({render_expression(ast[1])})"
    if kind == "pow":
        return f"({render_expression(ast[1])})^{ast[2]}"
    if kind == "func":
        return f"{ast[1]}({render_expression(ast[2])})"
    if kind in ("add", "mul"):
        separator = " + " if kind == "add" else " * "
        return "(" + separator.join(render_expression(child) for child in ast[1:]) + ")"
    if kind in ("sub", "div"):
        separator = " - " if kind == "sub" else " / "
        return f"({render_expression(ast[1])}{separator}{render_expression(ast[2])})"
    raise EquationError(f"无法渲染的 AST 节点类型：{kind!r}")


def evaluate_expression(ast: tuple, point: Mapping[str, float]) -> float:
    """在给定取值点上对表达式求值。

    这是 M3 搜索阶段的求值入口——P02 刻意不做候选表达式的数据求值，
    该职责在本阶段落地，与框架说明的分工一致。

    :param ast: 规范式或原始式 AST（原始式的 ``sub`` / ``div`` 亦被支持）。
    :param point: 变量名 → 数值 的映射。
    :raises EquationError: 表达式为空、缺少变量取值，或取值不是有限实数。
    """
    if not isinstance(ast, tuple) or not ast:
        raise EquationError("AST 必须是非空元组")
    kind = ast[0]

    if kind == "num":
        return float(ast[1])

    if kind == "var":
        name = ast[1]
        if name not in point:
            raise EquationError(f"求值缺少变量取值：{name!r}")
        raw = point[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise EquationError(f"变量 {name!r} 的取值必须是实数，实际得到：{raw!r}")
        value = float(raw)
        if not math.isfinite(value):
            raise EquationError(f"变量 {name!r} 的取值必须是有限实数，实际得到：{raw!r}")
        return value

    if kind == "neg":
        return -evaluate_expression(ast[1], point)

    if kind == "inv":
        return 1.0 / evaluate_expression(ast[1], point)

    if kind == "add":
        total = 0.0
        for child in ast[1:]:
            total += evaluate_expression(child, point)
        return total

    if kind == "mul":
        product = 1.0
        for child in ast[1:]:
            product *= evaluate_expression(child, point)
        return product

    if kind == "sub":
        return evaluate_expression(ast[1], point) - evaluate_expression(ast[2], point)

    if kind == "div":
        return evaluate_expression(ast[1], point) / evaluate_expression(ast[2], point)

    if kind == "pow":
        return evaluate_expression(ast[1], point) ** ast[2]

    if kind == "func":
        name = ast[1]
        value = evaluate_expression(ast[2], point)
        if name == "abs":
            return abs(value)
        if name == "sqrt":
            return math.sqrt(value)
        if name == "log":
            return math.log(value)
        if name == "exp":
            return math.exp(value)
        raise EquationError(f"不支持的一元函数 {name!r}")

    raise EquationError(f"无法求值的 AST 节点类型：{kind!r}")


def formula_length(ast: tuple) -> dict:
    """返回表达式的**公式长度近似**复杂度分量。

    :return: ``{"expression_nodes": n, "rendered_length": k}``。
        这只是长度近似；**不是严格 MDL**（理由见模块说明）。
    """
    nodes = expression_node_count(ast)
    return {"expression_nodes": nodes, "rendered_length": len(render_expression(ast))}


# ---------------------------------------------------------------------------
# 数据样本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleRow:
    """一行对齐后的观测：原始记录标识、自变量取值、目标取值。"""

    record_id: str
    values: Mapping[str, float]
    target: float

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（变量按键名排序，便于比对）。"""
        return {
            "record_id": self.record_id,
            "values": {name: self.values[name] for name in sorted(self.values)},
            "target": self.target,
        }


@dataclass(frozen=True)
class ExplorationSample:
    """对齐后的探索样本。

    只包含「全部声明变量与目标均为有限实数」的行；被排除的行数与原因
    如实记录在 ``excluded`` 中，**绝不填补**缺失取值。

    :param variables: 参与拟合的自变量名（输入顺序保留）。
    :param target: 目标字段名。
    :param rows: 对齐后的行（保持输入顺序，便于复现）。
    :param excluded: 被排除行的计数与原因。
    :param purpose: 数据用途，恒为 ``"E"``。
    :param source_ref: 数据引用（若调用方提供）。
    :param provenance: 访问来源与版本信息。
    """

    variables: tuple[str, ...]
    target: str
    rows: tuple[SampleRow, ...]
    excluded: Mapping[str, int] = field(default_factory=dict)
    purpose: str = EXPLORATION_PURPOSE
    source_ref: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def fingerprint(self) -> str:
        """样本指纹：对对齐后的实际数据做 sha256，前 16 位。

        指纹覆盖**逐行的记录标识、自变量取值与目标**，因此「候选用的子样本」
        与「基线用的样本」是否为同一份数据，可由指纹直接判定。
        指纹与 :data:`_EPS` 无关，取值按 12 位小数量化后再入摘要，
        以便跨进程稳定复现。
        """
        payload = {
            "variables": list(self.variables),
            "target": self.target,
            "purpose": self.purpose,
            "rows": [
                [
                    row.record_id,
                    [round(row.values[name], 12) for name in self.variables],
                    round(row.target, 12),
                ]
                for row in self.rows
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def y(self) -> list[float]:
        """目标取值序列。"""
        return [row.target for row in self.rows]

    def column(self, name: str) -> list[float]:
        """某一自变量的取值序列。"""
        if name not in self.variables:
            raise EquationError(f"样本中不存在自变量 {name!r}")
        return [row.values[name] for row in self.rows]

    def subset(self, indices: Sequence[int]) -> "ExplorationSample":
        """按行下标取子样本，并把被剔除的行计入 ``restricted_out``。"""
        index_list = list(indices)
        rows = tuple(self.rows[i] for i in index_list)
        excluded = dict(self.excluded)
        removed = len(self.rows) - len(rows)
        if removed:
            excluded["restricted_out"] = excluded.get("restricted_out", 0) + removed
        return ExplorationSample(
            variables=self.variables,
            target=self.target,
            rows=rows,
            excluded=excluded,
            purpose=self.purpose,
            source_ref=self.source_ref,
            provenance=dict(self.provenance),
        )

    def to_dict(self, *, with_rows: bool = True) -> dict:
        """序列化为 JSON 兼容字典。"""
        payload = {
            "variables": list(self.variables),
            "target": self.target,
            "n_rows": len(self.rows),
            "fingerprint": self.fingerprint,
            "excluded": {key: self.excluded[key] for key in sorted(self.excluded)},
            "purpose": self.purpose,
            "source_ref": self.source_ref,
            "provenance": dict(self.provenance),
        }
        if with_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


def _purpose_of_ref(ref: Any) -> str | None:
    """从数据引用中解析用途字母；无法识别返回 ``None``。"""
    if not isinstance(ref, str) or not ref.strip():
        return None
    match = _REF_PREFIX_RE.match(ref.strip())
    if match is None:
        return None
    token = match.group(0).upper()
    if token.startswith("E"):
        return "E"
    for prefix in _RESTRICTED_PURPOSES:
        if token.startswith(prefix):
            return prefix
    return None


def _assert_exploration_ref(ref: Any, resources: Any = None) -> None:
    """校验数据引用指向探索分区 E。

    两级校验：

    1. 若提供了分区映射（``spec["resources"]`` 形态），引用必须**恰好等于**
       映射中的 ``E`` 项；映射缺少 E、或引用不等于它，一律拒绝
       （不猜测归属、不放行未登记引用）；
    2. 否则按引用前缀判定：非 E 前缀（V/C/Q/H）拒绝；无法识别前缀时不臆造
       结论（引用格式由上游决定），但真正的权限始终由 M1 的 ``read_dataset``
       执行，本模块不做替代。
    """
    if isinstance(resources, Mapping) and resources:
        expected = resources.get("E")
        if not isinstance(ref, str) or not ref.strip():
            raise EquationPolicyError("提供了分区映射时，必须给出字符串形式的数据引用")
        if expected is None:
            raise EquationPolicyError("分区映射中缺少 E 分区引用，无法确认引用归属")
        if ref != expected:
            purpose = _purpose_of_ref(ref) or "未知"
            raise EquationPolicyError(
                f"本模块只拟合探索分区 E；拒绝引用 {ref!r}（用途：{purpose}）"
            )
        return
    purpose = _purpose_of_ref(ref)
    if purpose is not None and purpose != EXPLORATION_PURPOSE:
        raise EquationPolicyError(
            f"本模块只拟合探索分区 E；拒绝用途为 {purpose} 的数据引用：{ref!r}"
        )


def _check_rows(records: Any) -> list[Mapping[str, Any]]:
    """把记录来源归一为字典序列。"""
    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        raise EquationError("记录来源必须是记录序列")
    rows = list(records)
    for row in rows:
        if not isinstance(row, Mapping):
            raise EquationError("每条记录必须是字典")
    return rows


def prepare_sample(
    records: Any,
    variables: Sequence[str],
    target: str,
    *,
    purpose: str = EXPLORATION_PURPOSE,
    source_ref: str | None = None,
    resources: Mapping[str, Any] | None = None,
    client: Any | None = None,
) -> ExplorationSample:
    """把探索记录整理为可拟合的对齐样本。

    :param records: 数据来源，二选一：

        - **记录序列**（``list[dict]``）；
        - **探索侧客户端**（带 ``read_dataset`` 与 ``role``）：此时必须同时给出
          ``source_ref``；角色不是 ``explorer`` 一律拒绝。
    :param variables: 自变量字段名序列（非空、无重复）。
    :param target: 目标字段名，不得与自变量重名。
    :param purpose: 数据用途，固定为 ``"E"``；其它取值一律拒绝。
    :param source_ref: 数据引用；给定时按 :func:`_assert_exploration_ref` 校验。
    :param resources: 分区映射（``{"E": "...", "V": "...", ...}``）；
        提供时启用严格归属校验（引用必须等于其中的 E 项）。
    :param client: 备选的探索侧客户端，用于 ``records=None`` 的调用形式；
        与 ``records`` 至多提供其一。
    :return: :class:`ExplorationSample`。
    """
    if purpose != EXPLORATION_PURPOSE:
        raise EquationPolicyError(
            f"本模块的用途固定为探索分区 {EXPLORATION_PURPOSE}；拒绝用途 {purpose!r}"
        )

    source = records
    if client is not None:
        if records is not None:
            raise EquationError("records 与 client 不得同时提供")
        source = client
    if source is None:
        raise EquationError("必须提供记录序列或探索侧客户端")

    if source_ref is not None and not isinstance(source_ref, str):
        raise EquationError("source_ref 必须是字符串或 None")

    reader = getattr(source, "read_dataset", None)
    if callable(reader):
        role = getattr(source, "role", None)
        if role is not None and role != EXPLORER_ROLE:
            raise EquationPolicyError(
                f"本模块只接受探索侧客户端（role == {EXPLORER_ROLE!r}）；实际角色：{role!r}"
            )
        if not source_ref:
            raise EquationPolicyError("通过客户端读取数据时必须给出探索分区 E 的数据引用")
        _assert_exploration_ref(source_ref, resources)
        rows = _check_rows(reader(source_ref))
        access_mode = "client"
    else:
        if isinstance(source, (str, bytes, Mapping)):
            raise EquationError(
                "记录来源必须是记录序列或探索侧客户端；不接受数据库路径或配置字典"
            )
        _assert_exploration_ref(source_ref, resources)
        rows = _check_rows(source)
        access_mode = "records"

    names = _validate_names(variables, "variables")
    if not isinstance(target, str) or not target.strip():
        raise EquationError("target 必须是非空字符串")
    if target in names:
        raise EquationError(f"目标字段 {target!r} 不得同时出现在自变量中")

    needed = list(names) + [target]
    aligned: list[SampleRow] = []
    excluded = {"quality": 0, "missing": 0, "nonnumeric": 0}

    for index, record in enumerate(rows):
        quality = record.get("_quality")
        status = quality.get("status") if isinstance(quality, Mapping) else None
        if status in ("quarantined", "excluded"):
            excluded["quality"] += 1
            continue

        values = record.get("values")
        if not isinstance(values, Mapping):
            values = {}

        point: dict[str, float] = {}
        broken = False
        for name in needed:
            raw = values.get(name)
            if raw is None or (isinstance(raw, str) and raw.strip().upper() in _MISSING_MARKERS):
                excluded["missing"] += 1
                broken = True
                break
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                excluded["nonnumeric"] += 1
                broken = True
                break
            number = float(raw)
            if not math.isfinite(number):
                excluded["nonnumeric"] += 1
                broken = True
                break
            point[name] = number
        if broken:
            # 该行至少有一个必需字段不可用：整行排除，绝不部分填补。
            continue

        record_id = record.get("record_id")
        identity = record_id if isinstance(record_id, str) and record_id.strip() else f"#{index}"
        aligned.append(
            SampleRow(
                record_id=identity,
                values={name: point[name] for name in names},
                target=point[target],
            )
        )

    provenance = {
        "origin": "prepare_sample",
        "module": "sdl_m03/equations.py",
        "module_version": EQUATION_VERSION,
        "access_mode": access_mode,
        "purpose": purpose,
        "role": getattr(source, "role", None) if access_mode == "client" else None,
    }
    return ExplorationSample(
        variables=names,
        target=target,
        rows=tuple(aligned),
        excluded=excluded,
        purpose=purpose,
        source_ref=source_ref,
        provenance=provenance,
    )


def _validate_names(names: Any, label: str) -> tuple[str, ...]:
    """校验字段名序列：非空、唯一、非空字符串。"""
    if isinstance(names, str) or not isinstance(names, Sequence):
        raise EquationError(f"{label} 必须是字段名序列（例如 ['X1', 'X2']）")
    out: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise EquationError(f"字段名必须是非空字符串，实际得到：{name!r}")
        if name in out:
            raise EquationError(f"{label} 中存在重复字段名：{name!r}")
        out.append(name)
    if not out:
        raise EquationError(f"{label} 不能为空")
    return tuple(out)


# ---------------------------------------------------------------------------
# 候选表示的归一化
# ---------------------------------------------------------------------------


def _is_value_column(relationship: Any) -> bool:
    """判断候选表示是否为「预先算好的数值列」。"""
    if isinstance(relationship, (str, bytes, tuple)):
        return False
    if isinstance(relationship, Candidate):
        return False
    return isinstance(relationship, Sequence)


def _to_ast(relationship: Candidate | tuple | str) -> tuple:
    """由候选表示得到规范式 AST。"""
    if isinstance(relationship, Candidate):
        return relationship.ast
    if isinstance(relationship, str):
        try:
            return parse_expression(relationship)
        except ExpressionError as exc:
            raise EquationError(f"表达式文本无法解析：{relationship!r}（{exc}）") from exc
    if isinstance(relationship, tuple):
        try:
            return from_ast(to_ast(relationship))
        except ExpressionError as exc:
            raise EquationError(f"AST 无法规范化为规范式：{exc}") from exc
    raise EquationError(
        "候选表达式必须是 Candidate、AST 元组或表达式文本；"
        f"实际得到：{type(relationship).__name__}"
    )


def _as_domain_spec(relationship: Any, *, eps: float) -> DomainSpec | None:
    """由候选表示推导定义域规格；数值列没有定义域概念，返回 ``None``。"""
    if isinstance(relationship, Candidate):
        return relationship.domain
    if _is_value_column(relationship):
        return None
    if isinstance(relationship, (int, float)) and not isinstance(relationship, bool):
        raise EquationError("候选表达式不能是裸数字")
    ast = _to_ast(relationship)
    return DomainSpec.for_expression(ast, eps=eps)


def _value_column(
    relationship: Candidate | Sequence[float] | tuple | str,
    sample: ExplorationSample,
) -> list[float]:
    """把候选表示统一为「Z 列」：在样本各行上求值，或直接使用给定序列。

    表达式在个别行上落在自身定义域之外（如 ``1/0``、``log(-1)``）时，
    对应位置记为 ``nan``，由 :func:`restrict_to_domain` 统一按「非有限」排除，
    **绝不填空值或截断**。
    """
    if _is_value_column(relationship):
        column = [float(value) for value in relationship]
        if len(column) != len(sample.rows):
            raise EquationError(
                f"数值列长度 {len(column)} 与样本行数 {len(sample.rows)} 不一致"
            )
        return column

    ast = _to_ast(relationship)
    needed = expression_variables(ast)
    missing = sorted(needed - set(sample.variables))
    if missing:
        raise EquationError(
            f"样本缺少候选所需的变量：{', '.join(missing)}；"
            f"样本变量为 {', '.join(sample.variables)}"
        )
    column: list[float] = []
    for row in sample.rows:
        try:
            column.append(evaluate_expression(ast, row.values))
        except (ArithmeticError, ValueError):
            column.append(float("nan"))
    return column


# ---------------------------------------------------------------------------
# 定义域限制
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestrictedSample:
    """按候选定义域限制后的样本，以及在其上算好的 Z 列。

    :param sample: 受限样本（只含定义域内且 Z 有限的行）。
    :param values: 与 ``sample.rows`` 等长的 Z 列。
    :param detail: 限制统计：输入行数、保留行数、被定义域排除数、非有限数。
    """

    sample: ExplorationSample
    values: tuple[float, ...]
    detail: Mapping[str, Any]

    def __len__(self) -> int:
        return len(self.sample.rows)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（不展开 Z 列取值）。"""
        return {
            "n_rows": len(self.sample.rows),
            "fingerprint": self.sample.fingerprint,
            "detail": dict(self.detail),
        }


def restrict_to_domain(
    sample: ExplorationSample,
    relationship: Candidate | Sequence[float] | tuple | str,
    *,
    eps: float = DEFAULT_EPS,
) -> RestrictedSample:
    """把样本限制在候选表达式的定义域内，并剔除求值非有限的行。

    纯函数：不写入任何模块级状态，同一输入恒得同一输出。

    :param sample: 待限制的样本。
    :param relationship: 候选表示：

        - :class:`~sdl_m02.domain.Candidate`：直接使用其携带的定义域；
        - AST 元组或表达式文本：按 P02 的定义域追踪规则推导条件；
        - **数值序列**：视为已算好的 Z 列，不做定义域限制，只剔除非有限值。
    :param eps: 定义域容差 ε。
    :return: :class:`RestrictedSample`。
    """
    if not isinstance(sample, ExplorationSample):
        raise EquationError("sample 必须是 ExplorationSample")
    spec = _as_domain_spec(relationship, eps=eps)
    values = _value_column(relationship, sample)

    kept: list[int] = []
    domain_out = 0
    nonfinite = 0
    for index, row in enumerate(sample.rows):
        if spec is not None and not domain_ok(spec, row.values, eps=eps):
            domain_out += 1
            continue
        if not math.isfinite(values[index]):
            nonfinite += 1
            continue
        kept.append(index)

    restricted = sample.subset(kept)
    detail = {
        "input": len(sample.rows),
        "kept": len(kept),
        "domain_out": domain_out,
        "nonfinite": nonfinite,
        "domain": spec.describe() if spec is not None else "未检查（数值列）",
    }
    return RestrictedSample(
        sample=restricted,
        values=tuple(values[index] for index in kept),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 残差与误差
# ---------------------------------------------------------------------------


def _finite_sequence(values: Any, label: str) -> list[float]:
    """把序列转为有限浮点列表。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise EquationError(f"{label} 必须是数值序列")
    out: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EquationError(f"{label} 必须只包含实数，实际得到：{value!r}")
        number = float(value)
        if not math.isfinite(number):
            raise EquationError(f"{label} 必须只包含有限实数，实际得到：{value!r}")
        out.append(number)
    return out


@dataclass(frozen=True)
class ResidualSummary:
    """残差摘要：误差指标与描述性诊断量。

    约定：残差 = 真值 − 预测值。``r2`` 在真值无变异时按「残差为零记 1.0，
    否则记 0.0」的约定处理，并随 :attr:`r2_defined` 一起输出，避免被误读。
    """

    n: int
    sse: float
    mse: float
    rmse: float
    mae: float
    max_abs_error: float
    mean_residual: float
    residual_std: float
    r2: float
    r2_defined: bool
    residuals: tuple[float, ...]

    def to_dict(self, *, with_values: bool = True) -> dict:
        """序列化为 JSON 兼容字典；``with_values=False`` 时略去逐点残差。"""
        payload = {
            "n": self.n,
            "sse": self.sse,
            "mse": self.mse,
            "rmse": self.rmse,
            "mae": self.mae,
            "max_abs_error": self.max_abs_error,
            "mean_residual": self.mean_residual,
            "residual_std": self.residual_std,
            "r2": self.r2,
            "r2_defined": self.r2_defined,
        }
        if with_values:
            payload["residuals"] = list(self.residuals)
        return payload


def residual_summary(y_true: Sequence[float], y_pred: Sequence[float]) -> ResidualSummary:
    """由真值与预测值计算误差与残差摘要。

    :param y_true: 真值序列。
    :param y_pred: 预测值序列，长度必须与 ``y_true`` 一致。
    :raises EquationError: 长度不一致、为空，或含非有限数值。
    """
    truth = _finite_sequence(y_true, "y_true")
    pred = _finite_sequence(y_pred, "y_pred")
    if len(truth) != len(pred):
        raise EquationError(f"真值与预测值长度不一致：{len(truth)} 与 {len(pred)}")
    if not truth:
        raise EquationError("真值与预测值不能为空")

    n = len(truth)
    residuals = [truth[i] - pred[i] for i in range(n)]
    sse = sum(value * value for value in residuals)
    mse = sse / n
    mae = sum(abs(value) for value in residuals) / n
    max_abs = max(abs(value) for value in residuals)
    mean_residual = sum(residuals) / n
    variance = sum((value - mean_residual) ** 2 for value in residuals) / n

    mean_truth = sum(truth) / n
    total = sum((value - mean_truth) ** 2 for value in truth)
    r2_defined = total > _EPS
    if r2_defined:
        r2 = 1.0 - sse / total
    else:
        r2 = 1.0 if sse <= _EPS else 0.0

    return ResidualSummary(
        n=n,
        sse=sse,
        mse=mse,
        rmse=math.sqrt(mse),
        mae=mae,
        max_abs_error=max_abs,
        mean_residual=mean_residual,
        residual_std=math.sqrt(variance),
        r2=r2,
        r2_defined=r2_defined,
        residuals=tuple(residuals),
    )


# ---------------------------------------------------------------------------
# 拟合结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FitResult:
    """一次拟合的结果。

    **误差、残差、复杂度三项恒存在**（构造时强制），对应验收标准②：

    - ``metrics``：误差与描述性诊断量；
    - ``residual_summary``：残差摘要对象（含逐点残差）；
    - ``complexity``：公式长度近似（**不是严格 MDL**）。
    """

    kind: str
    name: str
    parameters: Mapping[str, float]
    metrics: Mapping[str, Any]
    residual_summary: ResidualSummary
    complexity: Mapping[str, Any]
    data_fingerprint: str
    n_used: int
    expression: list | None = None
    feature: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    degenerate: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("candidate", "constant", "linear"):
            raise EquationError(f"未知的拟合种类：{self.kind!r}")
        if not self.parameters:
            raise EquationError("拟合结果必须包含参数")
        if not self.metrics:
            raise EquationError("拟合结果必须包含误差指标")
        if self.residual_summary is None:
            raise EquationError("拟合结果必须包含残差")
        if not self.complexity:
            raise EquationError("拟合结果必须包含复杂度")
        if self.n_used != self.residual_summary.n:
            raise EquationError("n_used 必须与残差摘要的 n 一致")
        for key in ("sse", "mse", "rmse", "mae"):
            if key not in self.metrics:
                raise EquationError(f"误差指标缺少必需项 {key!r}")

    @property
    def mse(self) -> float:
        """均方误差（损失对照直接取用）。"""
        return float(self.metrics["mse"])

    @property
    def sse(self) -> float:
        """残差平方和。"""
        return float(self.metrics["sse"])

    @property
    def residuals(self) -> tuple[float, ...]:
        """逐点残差（真值 − 预测值）。"""
        return self.residual_summary.residuals

    def to_dict(self, *, with_residuals: bool = True) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "kind": self.kind,
            "name": self.name,
            "parameters": {key: self.parameters[key] for key in sorted(self.parameters)},
            "metrics": dict(self.metrics),
            "residuals": self.residual_summary.to_dict(with_values=with_residuals),
            "complexity": dict(self.complexity),
            "complexity_note": COMPLEXITY_NOTE,
            "data_fingerprint": self.data_fingerprint,
            "n_used": self.n_used,
            "expression": self.expression,
            "feature": self.feature,
            "degenerate": self.degenerate,
            "provenance": dict(self.provenance),
        }


def _assemble(
    *,
    kind: str,
    name: str,
    parameters: Mapping[str, float],
    y_true: Sequence[float],
    y_pred: Sequence[float],
    complexity: Mapping[str, Any],
    sample: ExplorationSample,
    provenance: Mapping[str, Any],
    expression: list | None = None,
    feature: str | None = None,
    degenerate: bool = False,
) -> FitResult:
    """组装一个 :class:`FitResult`（内部统一入口，保证三项恒存在）。"""
    summary = residual_summary(y_true, y_pred)
    metrics = {
        "sse": summary.sse,
        "mse": summary.mse,
        "rmse": summary.rmse,
        "mae": summary.mae,
        "max_abs_error": summary.max_abs_error,
        "mean_residual": summary.mean_residual,
        "residual_std": summary.residual_std,
        "r2": summary.r2,
        "r2_defined": summary.r2_defined,
    }
    return FitResult(
        kind=kind,
        name=name,
        parameters=dict(parameters),
        metrics=metrics,
        residual_summary=summary,
        complexity=dict(complexity),
        data_fingerprint=sample.fingerprint,
        n_used=len(sample.rows),
        expression=expression,
        feature=feature,
        provenance=dict(provenance),
        degenerate=degenerate,
    )


def _ols(y: Sequence[float], x: Sequence[float]) -> tuple[float, float, bool]:
    """一元线性最小二乘：返回 ``(intercept, slope, degenerate)``。

    ``degenerate`` 为 ``True`` 表示自变量没有变异，斜率无定义：此时斜率为 0、
    截距取 ``ȳ``（等价于常数基线），并如实标记，不把无效斜率当作拟合结果。
    """
    n = len(y)
    if n < 2:
        raise EquationError(f"线性拟合至少需要 2 行数据，实际只有 {n} 行")
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum((value - mean_x) ** 2 for value in x)
    scale = max(1.0, max(abs(value) for value in x))
    if sxx <= _EPS * scale * scale:
        return mean_y, 0.0, True
    sxy = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    slope = sxy / sxx
    return mean_y - slope * mean_x, slope, False


def _has_variation(values: Sequence[float]) -> bool:
    """判断取值是否有变异（用于筛选可用的线性基线字段）。"""
    low = min(values)
    high = max(values)
    scale = max(1.0, abs(low), abs(high))
    return (high - low) > _EPS * scale


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------


def baseline_constant(sample: ExplorationSample) -> FitResult:
    """常数基线：``y ≈ ȳ``。

    这是「不引入任何自变量」的对照，任何候选关系都必须先超过它才有意义。

    :param sample: 已对齐的探索样本（至少 1 行）。
    """
    if not isinstance(sample, ExplorationSample):
        raise EquationError("sample 必须是 ExplorationSample")
    if not sample.rows:
        raise EquationError("常数基线至少需要 1 行数据")
    y = sample.y()
    mean_y = sum(y) / len(y)
    prediction = [mean_y for _ in y]
    return _assemble(
        kind="constant",
        name=f"常数基线：{sample.target} ≈ {_format_number(mean_y)}",
        parameters={"intercept": mean_y, "slope": 0.0},
        y_true=y,
        y_pred=prediction,
        complexity={
            "expression_nodes": 0,
            "rendered_length": len(_format_number(mean_y)),
            "parameter_count": 1,
            "total": 1,
            "note": COMPLEXITY_NOTE,
        },
        sample=sample,
        provenance={"origin": "baseline_constant", "module_version": EQUATION_VERSION},
    )


def baseline_linear(sample: ExplorationSample, feature: str) -> FitResult:
    """单变量线性基线：``y ≈ a + b·X_j``。

    对应框架说明所称「使用原始变量的预定基线」。**只用原始变量**，
    不引入任何构造出来的合成变量。

    :param sample: 已对齐的探索样本（至少 2 行）。
    :param feature: 作为自变量的原始字段名。
    :raises EquationError: 字段不在样本中，或该字段在样本上没有变异。
    """
    if not isinstance(sample, ExplorationSample):
        raise EquationError("sample 必须是 ExplorationSample")
    if feature not in sample.variables:
        raise EquationError(
            f"线性基线的自变量 {feature!r} 不在样本变量 "
            f"{', '.join(sample.variables)} 中"
        )
    y = sample.y()
    x = sample.column(feature)
    if not _has_variation(x):
        raise EquationError(
            f"自变量 {feature!r} 在样本上没有变异，无法定义线性基线"
        )
    intercept, slope, degenerate = _ols(y, x)
    if degenerate:  # pragma: no cover - _has_variation 已先行拦截
        raise EquationError(f"自变量 {feature!r} 在样本上没有变异，无法定义线性基线")
    prediction = [intercept + slope * value for value in x]
    return _assemble(
        kind="linear",
        name=(
            f"线性基线：{sample.target} ≈ {_format_number(intercept)}"
            f" + {_format_number(slope)} * {feature}"
        ),
        parameters={"intercept": intercept, "slope": slope},
        y_true=y,
        y_pred=prediction,
        complexity={
            "expression_nodes": 1,
            "rendered_length": len(feature),
            "parameter_count": 2,
            "total": 3,
            "note": COMPLEXITY_NOTE,
        },
        sample=sample,
        feature=feature,
        provenance={"origin": "baseline_linear", "module_version": EQUATION_VERSION},
    )


# ---------------------------------------------------------------------------
# 对照
# ---------------------------------------------------------------------------


def _pairwise(candidate: FitResult, baseline: FitResult) -> dict:
    """候选相对单个基线的损失差（``loss_difference = 基线 − 候选``，正值表示候选更好）。"""
    base = baseline.mse
    difference = base - candidate.mse
    if abs(base) > _EPS:
        relative: float = difference / base
    else:
        relative = 0.0 if difference <= 0 else float("inf")
    return {
        "kind": baseline.kind,
        "name": baseline.name,
        "feature": baseline.feature,
        "baseline_mse": base,
        "candidate_mse": candidate.mse,
        "mse_difference": difference,
        "relative_mse_reduction": relative,
        "beats_baseline": difference > 0.0,
        "baseline_n_used": baseline.n_used,
        "data_fingerprint": baseline.data_fingerprint,
    }


def _flatten_baselines(baselines: Mapping[str, Any]) -> list[FitResult]:
    """把基线容器展平为 :class:`FitResult` 列表。"""
    out: list[FitResult] = []
    for payload in baselines.values():
        if isinstance(payload, FitResult):
            out.append(payload)
        elif isinstance(payload, Mapping):
            out.extend(payload.get("per_feature", {}).values())
    return out


def _baseline_block(candidate: FitResult, payload: Any) -> dict:
    """给出候选相对某类基线的对照。"""
    if isinstance(payload, FitResult):
        return {"single": _pairwise(candidate, payload)}
    per_feature = {
        feature: _pairwise(candidate, fit)
        for feature, fit in payload["per_feature"].items()
    }
    best = None
    for item in per_feature.values():
        if best is None or item["mse_difference"] > best["mse_difference"]:
            best = item
    return {"per_feature": per_feature, "best": best}


def _best_baseline(candidate: FitResult, baselines: Mapping[str, Any]) -> dict | None:
    """在全部基线中挑出损失最小者（最难超越的基线），并给出其对候选的对照。"""
    fits = _flatten_baselines(baselines)
    if not fits:
        return None
    hardest = min(fits, key=lambda fit: (fit.mse, fit.name))
    item = _pairwise(candidate, hardest)
    item["is_hardest"] = True
    return item


def compare_on_same_data(*fits: FitResult, loss: str = "mse") -> dict:
    """核验若干拟合结果是否建立在同一份数据上，并给出损失对照。

    :param fits: 至少两个 :class:`FitResult`。
    :param loss: 用于对照的损失名，必须是 ``metrics`` 中存在的键。
    :return: ``{"data_fingerprint", "n_used", "loss", "table", "deltas", "note"}``。
    :raises EquationError: 数量不足、数据指纹或行数不一致、损失名不存在。
    """
    items = list(fits)
    if len(items) < 2:
        raise EquationError("同数据核验至少需要两个拟合结果")
    for item in items:
        if not isinstance(item, FitResult):
            raise EquationError("同数据核验的输入必须是 FitResult")

    fingerprints = {item.data_fingerprint for item in items}
    if len(fingerprints) != 1:
        detail = ", ".join(f"{item.name}={item.data_fingerprint}" for item in items)
        raise EquationError(
            "候选与基线必须建立在同一份数据上，实测数据指纹不一致：" + detail
        )
    sizes = {item.n_used for item in items}
    if len(sizes) != 1:
        raise EquationError(
            "候选与基线必须使用相同的行集，实测行数不一致："
            + ", ".join(str(size) for size in sorted(sizes))
        )
    for item in items:
        if loss not in item.metrics:
            raise EquationError(
                f"拟合结果的 metrics 中不存在损失 {loss!r}；"
                f"可用键：{', '.join(sorted(item.metrics))}"
            )

    first = items[0]
    table = [
        {
            "kind": item.kind,
            "name": item.name,
            "feature": item.feature,
            "n_used": item.n_used,
            "loss": float(item.metrics[loss]),
            "complexity_total": item.complexity.get("total"),
        }
        for item in items
    ]
    deltas = [
        {
            "name": item.name,
            "loss_difference_vs_first": float(first.metrics[loss]) - float(item.metrics[loss]),
        }
        for item in items[1:]
    ]
    return {
        "data_fingerprint": next(iter(fingerprints)),
        "n_used": next(iter(sizes)),
        "loss": loss,
        "table": table,
        "deltas": deltas,
        "note": "同一数据指纹表示候选与基线建立在逐行相同的数据上。",
    }


def gain_against(candidate: FitResult, baseline: FitResult, *, loss: str = "mse") -> dict:
    """给出候选相对单条基线的损失差与相对下降。

    约定：``loss_difference = 基线损失 − 候选损失``，**正值表示候选更好**
    （损失更低）。适用于 MSE/SSE/MAE 等「越低越好」的损失；
    不适用于 ``r2`` 这类「越高越好」的量。
    """
    if candidate.data_fingerprint != baseline.data_fingerprint:
        raise EquationError(
            "损失对照要求候选与基线使用同一份数据："
            f"{candidate.data_fingerprint} != {baseline.data_fingerprint}"
        )
    for item in (candidate, baseline):
        if loss not in item.metrics:
            raise EquationError(f"拟合结果的 metrics 中不存在损失 {loss!r}")
    base = float(baseline.metrics[loss])
    current = float(candidate.metrics[loss])
    difference = base - current
    relative = difference / base if abs(base) > _EPS else (
        0.0 if difference <= 0 else float("inf")
    )
    return {
        "loss": loss,
        "baseline_loss": base,
        "candidate_loss": current,
        "loss_difference": difference,
        "relative_reduction": relative,
        "candidate_better": difference > 0.0,
    }


# ---------------------------------------------------------------------------
# 关系拟合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationFit:
    """候选关系与其基线的完整对照结果。

    :param candidate: 候选关系的拟合结果（含误差、残差、复杂度）。
    :param baselines: 基线结果容器。
    :param comparison: 对照汇总（损失差、相对下降、最难超越的基线、口径说明）。
    :param restriction: 定义域限制统计（输入行数、保留行数、排除原因）。
    :param sample: 实际用于拟合与基线的受限样本。
    :param sample_input: 限制前的样本行数。
    """

    candidate: FitResult
    baselines: Mapping[str, Any]
    comparison: Mapping[str, Any]
    restriction: Mapping[str, Any]
    sample: ExplorationSample
    sample_input: int

    def __post_init__(self) -> None:
        if self.candidate.kind != "candidate":
            raise EquationError("RelationFit.candidate 必须是候选取向的拟合结果")
        if not self.baselines:
            raise EquationError("RelationFit 必须包含基线，基线不可省略")
        if not self.comparison:
            raise EquationError("RelationFit 必须包含对照结果")

    @property
    def fingerprint(self) -> str:
        """实际使用数据的指纹。"""
        return self.candidate.data_fingerprint

    def to_dict(self, *, with_residuals: bool = True) -> dict:
        """序列化为 JSON 兼容字典。"""

        def dump(payload: Any) -> Any:
            if isinstance(payload, FitResult):
                return payload.to_dict(with_residuals=with_residuals)
            if isinstance(payload, Mapping):
                return {key: dump(value) for key, value in payload.items()}
            return payload

        return {
            "version": EQUATION_VERSION,
            "candidate": self.candidate.to_dict(with_residuals=with_residuals),
            "baselines": dump(self.baselines),
            "comparison": dump(self.comparison),
            "restriction": dict(self.restriction),
            "sample": {
                "n_rows": len(self.sample.rows),
                "n_input": self.sample_input,
                "fingerprint": self.sample.fingerprint,
                "excluded": dict(self.sample.excluded),
            },
        }


def _validate_baselines(baselines: Any) -> tuple[str, ...]:
    """校验基线种类集合：非空且取值合法。"""
    if isinstance(baselines, str) or not isinstance(baselines, Sequence):
        raise EquationError("baselines 必须是种类序列，例如 ('constant', 'linear')")
    kinds: list[str] = []
    for kind in baselines:
        if kind not in BASELINE_KINDS:
            raise EquationError(
                f"未知的基线种类 {kind!r}；仅支持 {', '.join(BASELINE_KINDS)}"
            )
        if kind not in kinds:
            kinds.append(kind)
    if not kinds:
        raise EquationError(
            "基线不可省略：baselines 不能为空，至少需要 "
            f"{' 与 '.join(BASELINE_KINDS)} 之一"
        )
    return tuple(kinds)


def _linear_feature_pool(
    sample: ExplorationSample,
    features: Sequence[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """决定线性基线可用的原始字段。

    :return: ``(可用字段, 因无变异而不可用字段)``。
        无变异的字段无法定义线性基线（``a + b·X`` 中的 ``X`` 全为常数），
        因此被排除并如实记录，而不是强行拟合出一个虚假的斜率。
    """
    if features is None:
        candidates = sample.variables
    else:
        candidates = _validate_names(features, "linear_features")
        for name in candidates:
            if name not in sample.variables:
                raise EquationError(
                    f"线性基线字段 {name!r} 不在样本变量 {', '.join(sample.variables)} 中"
                )
    usable: list[str] = []
    unusable: list[str] = []
    for name in candidates:
        (usable if _has_variation(sample.column(name)) else unusable).append(name)
    return tuple(usable), tuple(unusable)


def fit_relation(
    sample: ExplorationSample,
    relationship: Candidate | Sequence[float] | tuple | str,
    *,
    baselines: Sequence[str] = DEFAULT_BASELINES,
    linear_features: Sequence[str] | None = None,
    eps: float = DEFAULT_EPS,
    restrict_domain: bool = True,
) -> RelationFit:
    """拟合候选关系 ``y ≈ a + b·Z``，并在**同一数据**上计算基线。

    :param sample: 已对齐的探索样本。
    :param relationship: 候选表示（:class:`~sdl_m02.domain.Candidate`、AST 元组、
        表达式文本，或预先算好的 ``Z`` 数值列）。
    :param baselines: 需要计算的基线种类，取值须落在 :data:`BASELINE_KINDS` 内。
        **不得为空**——空序列即报错，因为本阶段要求基线不可省略。
    :param linear_features: 线性基线使用的原始字段；``None`` 表示对样本中
        每个有变异的自变量各算一个线性基线。
    :param eps: 定义域容差 ε。
    :param restrict_domain: 是否按候选定义域限制样本（默认 ``True``）。
        无论是否限制，基线都在**限制之后**的同一份样本上计算。
    :return: :class:`RelationFit`。
    :raises EquationError: 基线为空或种类非法、数据不足、候选无变异等。
    """
    if not isinstance(sample, ExplorationSample):
        raise EquationError("sample 必须是 ExplorationSample")
    if not sample.rows:
        raise EquationError("拟合至少需要 1 行数据")

    kinds = _validate_baselines(baselines)

    # -- 步骤 1：按定义域限制样本（候选与基线共用同一子样本） -----------
    if restrict_domain:
        restricted = restrict_to_domain(sample, relationship, eps=eps)
        work_sample = restricted.sample
        values = list(restricted.values)
        restriction: Mapping[str, Any] = dict(restricted.detail)
    else:
        work_sample = sample
        values = _value_column(relationship, sample)
        if any(not math.isfinite(value) for value in values):
            raise EquationError("未启用定义域限制时，候选取值必须全部为有限实数")
        restriction = {
            "input": len(sample.rows),
            "kept": len(sample.rows),
            "domain_out": 0,
            "nonfinite": 0,
            "domain": "未检查（restrict_domain=False）",
        }

    if len(work_sample.rows) < 2:
        raise EquationError(
            f"候选在其定义域内只剩 {len(work_sample.rows)} 行有效数据，不足以拟合"
        )

    # -- 步骤 2：拟合候选 ------------------------------------------------
    y = work_sample.y()
    if not _has_variation(values):
        raise EquationError(
            "候选在受限样本上没有变异（Z 为常量），无法拟合斜率；"
            "该候选等价于常数基线"
        )
    intercept, slope, degenerate = _ols(y, values)
    prediction = [intercept + slope * value for value in values]

    if _is_value_column(relationship):
        complexity_ast = None
        expression_payload = None
        rendered = "<预置数值列>"
        expression_nodes = 0
        rendered_length = 0
    else:
        complexity_ast = _to_ast(relationship)
        expression_payload = to_ast(complexity_ast)
        rendered = render_expression(complexity_ast)
        length = formula_length(complexity_ast)
        expression_nodes = length["expression_nodes"]
        rendered_length = length["rendered_length"]

    candidate_fit = _assemble(
        kind="candidate",
        name=(
            f"候选关系：{work_sample.target} ≈ {_format_number(intercept)}"
            f" + {_format_number(slope)} * Z，其中 Z = {rendered}"
        ),
        parameters={"intercept": intercept, "slope": slope},
        y_true=y,
        y_pred=prediction,
        complexity={
            "expression_nodes": expression_nodes,
            "rendered_length": rendered_length,
            "parameter_count": 2,
            "total": expression_nodes + 2,
            "note": COMPLEXITY_NOTE,
        },
        sample=work_sample,
        expression=expression_payload,
        provenance={
            "origin": "fit_relation",
            "module_version": EQUATION_VERSION,
            "model_form": "intercept + slope * Z",
            "domain_restricted": bool(restrict_domain),
            "candidate_source": "value_column" if complexity_ast is None else "expression",
        },
        degenerate=degenerate,
    )

    # -- 步骤 3：在同一份受限样本上算基线 -------------------------------
    baseline_fits: dict[str, Any] = {}
    for kind in kinds:
        if kind == "constant":
            baseline_fits["constant"] = baseline_constant(work_sample)
            continue
        usable, unusable = _linear_feature_pool(work_sample, linear_features)
        if not usable:
            raise EquationError(
                "线性基线不可省略但无法定义：全部候选自变量在受限样本上均无变异"
                f"（无变异字段：{', '.join(unusable)}）"
            )
        per_feature = {feature: baseline_linear(work_sample, feature) for feature in usable}
        baseline_fits["linear"] = {
            "per_feature": per_feature,
            "unusable_no_variation": list(unusable),
            "best_feature": min(per_feature, key=lambda name: (per_feature[name].mse, name)),
        }

    # -- 步骤 4：同数据核验 + 对照 --------------------------------------
    all_fits = _flatten_baselines(baseline_fits)
    same_data = compare_on_same_data(candidate_fit, *all_fits)

    comparison: dict[str, Any] = {
        "loss": "mse",
        "same_data": same_data,
        "baselines": {
            kind: _baseline_block(candidate_fit, payload)
            for kind, payload in baseline_fits.items()
        },
        "hardest_baseline": _best_baseline(candidate_fit, baseline_fits),
    }
    hardest = comparison["hardest_baseline"]
    if hardest is not None:
        comparison["gain_over_hardest_baseline"] = {
            "baseline_kind": hardest["kind"],
            "baseline_name": hardest["name"],
            "mse_difference": hardest["mse_difference"],
            "relative_mse_reduction": hardest["relative_mse_reduction"],
            "beats_baseline": hardest["beats_baseline"],
        }
    comparison["note"] = (
        "对照只在探索分区 E 上进行，属开发期比较；"
        "不构成确证检验，不计算 p 值，不授予证据等级。"
    )

    return RelationFit(
        candidate=candidate_fit,
        baselines=baseline_fits,
        comparison=comparison,
        restriction=restriction,
        sample=work_sample,
        sample_input=len(sample.rows),
    )
