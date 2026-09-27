"""M2 候选变量枚举与定义域追踪（P02 交付物）。

本模块在 P01 的受限语法与规范化之上，完成两件事：

1. **候选变量的有界枚举**：给定若干变量名，在受限语法内生成一批候选表达式，
   并以 P01 的**规范式**作为「语义唯一标识」——因此语义等价的不同写法
   （如 ``X1 + X2`` 与 ``X2 + X1``）只会出现一次。
2. **定义域的显式追踪**：为每个候选收集它成立所需的定义域条件，
   例如除法要求分母非零（记为 ``|X2| > ε``）、开方要求被开方数非负、
   取对数要求真数为正；并提供一个**纯函数** ``domain_ok`` 来判断某个取值点
   是否落在该定义域内。

设计边界（严格遵守 P02 增量卡）：

- 本模块**不做数据访问**：不导入 ``sdl_m01``，不接触任何分区引用、令牌、
  证据数据库或质量报告。定义域检查只依赖调用方传入的**一个取值点**（字典），
  与该点来自哪个数据集无关。
- 本模块**不实现去重剪枝**（属 P03）：不提供 ``dedup`` / ``prune_dominated``
  之类的候选池级去重或被支配剪枝接口，也**不剔除**「不合法」候选
  （「剔除不合法、重复与被支配的候选」是 P03 的职责）。
  这里只在**构造层面**保证同一语义形式上不会重复生成——这属于「枚举本身
  无重复语义」的构造性质，而非对既有候选池的再加工。
- 本模块**不执行候选表达式的数据求值**（属 M3 的搜索阶段）。唯一被求值的对象
  是**定义域条件所引用的小子树**，且只在调用方给出的单点上求值。
- 仅使用 Python 3.11+ 标准库。

枚举策略
--------

按**节点数分层**递推（size = 1, 2, 3, …），层内按规范式的紧凑 JSON 串排序，
保证枚举顺序完全确定、可复现：

- size = 1：叶子，即声明的常数与给定的变量；
- size = s：由更小的层组合而成——
  - 一元构造：``neg``、``inv``（表示除法因子）、``func``（abs/log/sqrt/exp）、
    ``pow``（指数取声明的正整数）；
  - 二元构造：``add`` 与 ``mul``（规范式会自行展平为 n 元并按交换律排序，
    因此无需另外生成参数置换）。
- 每个新节点都先经 P01 的 :func:`~sdl_m02.expressions.normalize` 归一，
  再以规范 JSON 串查重，最后校验深度与节点数上限。

这样产生的结果满足：

- **有上界**：``max_candidates``（候选数量）与 ``max_nodes`` / ``max_depth``
  （单个候选的规模）三重上界；另设 ``max_attempts`` 防止构造过程失控。
- **无重复语义**：任意两个候选的规范式不同，故语义不同。

已知限制：常数折叠（如 ``exp(3)`` 折为常数约 20.0855）可能产生「规模型层」
变化的新常数叶子。这类节点会被如实收录为候选，但**不参与更深层的组合**，
以免因折叠引起的层级漂移造成同一语义被反复枚举。因此枚举空间是
「声明的容量上界内的确定性前缀」，而非全域闭包。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .expressions import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    SUPPORTED_FUNCTIONS,
    ExpressionError,
    depth,
    node_count,
    normalize,
    to_ast,
    to_canonical_json,
    variables as expression_variables,
)

__all__ = [
    "DomainError",
    "DomainCondition",
    "DomainSpec",
    "Candidate",
    "DOMAIN_SPEC_VERSION",
    "DEFAULT_EPS",
    "DEFAULT_CONSTANTS",
    "DEFAULT_POWERS",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "collect_domain_conditions",
    "enumerate_candidates",
    "domain_ok",
]

# ---------------------------------------------------------------------------
# 常量与默认配置
# ---------------------------------------------------------------------------

#: DomainSpec 序列化版本号。字段含义变化时递增。
DOMAIN_SPEC_VERSION = "1"

#: 定义域判定的默认容差 ε。除法候选据此记为 ``|分母| > ε``。
DEFAULT_EPS = 1e-9

#: 默认参与枚举的常数集合。刻意保持精简，使候选池可解释、可复现。
DEFAULT_CONSTANTS: tuple[float, ...] = (-1.0, 0.0, 0.5, 1.0, 2.0, 3.0)

#: 默认参与枚举的幂指数（正整数；0 与 1 会被规范化消去，故不列出）。
DEFAULT_POWERS: tuple[int, ...] = (2, 3)

#: 默认候选数量上界。
DEFAULT_MAX_CANDIDATES = 1000

#: 默认构造尝试次数上界，用于兜住「候选尚未达到数量上界但组合爆炸」的情形。
DEFAULT_MAX_ATTEMPTS = 200_000

#: ``candidate_id`` 的前缀。
_CANDIDATE_ID_PREFIX = "cand-"

#: 定义域条件的种类及其含义。
#: - ``nonzero``     要求子树取值绝对值大于 ε（除法/倒数）；
#: - ``nonnegative`` 要求子树取值不小于 0（开方）；
#: - ``positive``    要求子树取值大于 0（取对数）。
CONDITION_KINDS: tuple[str, ...] = ("nonzero", "nonnegative", "positive")


class DomainError(ValueError):
    """候选枚举或定义域处理的输入非法。"""


# ---------------------------------------------------------------------------
# 表达式渲染（仅用于生成人类可读的定义域说明文本）
# ---------------------------------------------------------------------------


def _format_number(value: float) -> str:
    """把常数格式化为紧凑的十进制文本。"""
    return f"{value:g}"


def _render(ast: tuple) -> str:
    """把规范式 AST 渲染为中缀表达式文本（仅供说明文本使用）。"""
    kind = ast[0]
    if kind == "num":
        return _format_number(ast[1])
    if kind == "var":
        return ast[1]
    if kind == "neg":
        inner = _render(ast[1])
        return f"-{inner}" if ast[1][0] in ("num", "var") else f"-({inner})"
    if kind == "inv":
        return f"1/({_render(ast[1])})"
    if kind == "pow":
        return f"({_render(ast[1])})^{ast[2]}"
    if kind == "func":
        return f"{ast[1]}({_render(ast[2])})"
    if kind == "add":
        return "(" + " + ".join(_render(child) for child in ast[1:]) + ")"
    if kind == "mul":
        return "(" + " * ".join(_render(child) for child in ast[1:]) + ")"
    raise DomainError(f"无法渲染的 AST 节点类型：{kind!r}")


# ---------------------------------------------------------------------------
# 规范式与原始式之间的桥接
# ---------------------------------------------------------------------------


def _to_raw(ast: tuple) -> tuple:
    """把（可能已规范的）AST 转成 P01 的 ``normalize`` 可接受的原始形式。

    为什么需要：P01 的规范化入口把 ``inv`` 视为「产物」而非「输入」——
    规范式中的 ``('inv', x)`` 需要还原为 ``1 / x`` 才能再次送入 ``normalize``。
    本函数做这个无损还原，使规范化对已规范 AST 也保持幂等，
    从而可以在枚举过程中反复组合与归一。
    """
    if not isinstance(ast, tuple) or len(ast) < 2:
        raise DomainError(f"AST 必须是非空元组，实际得到：{ast!r}")
    kind = ast[0]

    if kind in ("num", "var"):
        return ast
    if kind == "inv":
        return ("div", ("num", 1.0), _to_raw(ast[1]))
    if kind in ("neg",):
        return ("neg", _to_raw(ast[1]))
    if kind == "func":
        return ("func", ast[1], _to_raw(ast[2]))
    if kind == "pow":
        return ("pow", _to_raw(ast[1]), ("num", float(ast[2])))
    if kind in ("sub", "div"):
        return (kind, _to_raw(ast[1]), _to_raw(ast[2]))
    if kind in ("add", "mul"):
        node = _to_raw(ast[1])
        for child in ast[2:]:
            node = (kind, node, _to_raw(child))
        return node
    raise DomainError(f"无法转换的 AST 节点类型：{kind!r}")


def _ensure_canonical(ast: tuple) -> tuple:
    """把可能是原始式或规范式的 AST 统一为规范式。"""
    if not isinstance(ast, tuple) or not ast:
        raise DomainError("AST 必须是非空元组")
    try:
        return normalize(_to_raw(ast))
    except ExpressionError as exc:  # pragma: no cover - 交由调用方感知
        raise DomainError(f"AST 无法规范化为规范式：{exc}") from exc


# ---------------------------------------------------------------------------
# 定义域条件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainCondition:
    """定义域中的**单条**约束。

    :param kind: 约束种类，取值见 :data:`CONDITION_KINDS`。
    :param operand: 被约束的子树（规范式 AST）。
    :param variables: 该子树涉及的变量名（升序元组，便于比较与序列化）。

    条件本身**不携带数据**，只是一段可计算的语法对象；
    是否满足由调用方在给定取值点上判定。
    """

    kind: str
    operand: tuple
    variables: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.kind not in CONDITION_KINDS:
            raise DomainError(
                f"未知的定义域条件种类 {self.kind!r}；仅支持 {', '.join(CONDITION_KINDS)}"
            )
        if not isinstance(self.operand, tuple) or not self.operand:
            raise DomainError("定义域条件的被约束子树必须是非空元组 AST")
        # 变量集合由子树推导，调用方无需（也不应）手工传入。
        derived = tuple(sorted(expression_variables(self.operand)))
        object.__setattr__(self, "variables", derived)

    # -- 判定 -------------------------------------------------------------

    def evaluate(self, point: Mapping[str, float]) -> float:
        """在被约束子树上求值，返回该点的取值。

        :raises DomainError: 取值点缺少子树所需的变量，或取值不是有限实数。
        """
        return _evaluate(self.operand, point)

    def holds(self, point: Mapping[str, float], eps: float) -> bool:
        """判断给定取值点是否满足本条件。

        取值落在被约束子树自身的定义域之外（如 ``1/0``、``log(-1)``、
        ``sqrt(-1)`` 或数值溢出）时返回 ``False``，因为该点本就无法代入。
        缺少变量取值属于**调用错误**，向上抛出 :class:`DomainError`。
        """
        try:
            value = _evaluate(self.operand, point)
        except DomainError:
            # 必须单独前置拦截：DomainError 继承自 ValueError，
            # 若被下面的 except 捕获会被误判为「定义域外」，从而掩盖调用错误。
            raise
        except (ArithmeticError, ValueError):
            # 取值点落在被约束子树自身的定义域之外（``1/0``、``log(-1)``、
            # ``sqrt(-1)``、``exp`` 溢出等），该点本就无法代入，判为不满足。
            return False
        if not math.isfinite(value):
            return False
        if self.kind == "nonzero":
            return abs(value) > eps
        if self.kind == "nonnegative":
            return value >= 0.0
        return value > 0.0  # positive

    def render(self, eps: float) -> str:
        """返回该条件的人类可读文本，例如 ``|X2| > 1e-09``。"""
        text = _render(self.operand)
        if self.kind == "nonzero":
            return f"|{text}| > {eps:g}"
        if self.kind == "nonnegative":
            return f"{text} >= 0"
        return f"{text} > 0"

    def to_dict(self, eps: float) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "kind": self.kind,
            "operand": to_ast(self.operand),
            "variables": list(self.variables),
            "text": self.render(eps),
        }


def collect_domain_conditions(ast: tuple) -> tuple[DomainCondition, ...]:
    """遍历规范式 AST，收集使其有定义所需的全部条件。

    识别规则（与 P01 的规范化改写一一对应）：

    - ``('inv', sub)``        → ``nonzero(sub)``，即 ``|sub| > ε``；
      P01 把 ``a / b`` 改写成 ``mul(a, inv(b))``，故除法的定义域条件由此承载。
    - ``('func', 'sqrt', sub)`` → ``nonnegative(sub)``；
    - ``('func', 'log', sub)``  → ``positive(sub)``；
    - ``abs`` / ``exp`` 在全实数上有定义，不产生条件。

    返回的条件已按 ``(kind, 规范式)`` 去重并排序，顺序确定。
    """
    canonical = _ensure_canonical(ast)
    found: list[DomainCondition] = []
    _walk_conditions(canonical, found)

    unique: dict[tuple[str, str], DomainCondition] = {}
    for condition in found:
        key = (condition.kind, to_canonical_json(condition.operand))
        if key not in unique:
            unique[key] = condition
    return tuple(sorted(unique.values(), key=lambda c: (c.kind, to_canonical_json(c.operand))))


def _walk_conditions(ast: tuple, sink: list[DomainCondition]) -> None:
    """递归收集条件（内部实现，直接向 ``sink`` 追加）。"""
    kind = ast[0]
    if kind in ("num", "var"):
        return
    if kind == "inv":
        sink.append(DomainCondition("nonzero", ast[1]))
        _walk_conditions(ast[1], sink)
        return
    if kind == "neg":
        _walk_conditions(ast[1], sink)
        return
    if kind == "pow":
        _walk_conditions(ast[1], sink)
        return
    if kind == "func":
        name, arg = ast[1], ast[2]
        if name == "sqrt":
            sink.append(DomainCondition("nonnegative", arg))
        elif name == "log":
            sink.append(DomainCondition("positive", arg))
        _walk_conditions(arg, sink)
        return
    if kind in ("add", "mul"):
        for child in ast[1:]:
            _walk_conditions(child, sink)
        return
    raise DomainError(f"无法处理的条件收集节点类型：{kind!r}")


# ---------------------------------------------------------------------------
# 定义域规格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainSpec:
    """一个候选的显式定义域：若干条件 + 一个容差 ε。

    空条件集表示「在全实数上有定义」（例如 ``X1 + X2``）。

    :param conditions: 定义域条件，构造时会自动去重与排序。
    :param epsilon: 除法等条件使用的容差，必须为正的有限实数。
    """

    conditions: tuple[DomainCondition, ...] = field(default=())
    epsilon: float = field(default=DEFAULT_EPS)

    def __post_init__(self) -> None:
        if not isinstance(self.epsilon, (int, float)) or isinstance(self.epsilon, bool):
            raise DomainError("定义域容差 ε 必须是实数")
        if not math.isfinite(self.epsilon) or self.epsilon <= 0:
            raise DomainError("定义域容差 ε 必须是正的有限实数")

        unique: dict[tuple[str, str], DomainCondition] = {}
        for condition in self.conditions:
            if not isinstance(condition, DomainCondition):
                raise DomainError("定义域条件必须是 DomainCondition 实例")
            key = (condition.kind, to_canonical_json(condition.operand))
            if key not in unique:
                unique[key] = condition
        ordered = tuple(
            sorted(unique.values(), key=lambda c: (c.kind, to_canonical_json(c.operand)))
        )
        object.__setattr__(self, "conditions", ordered)

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def for_expression(cls, ast: tuple, eps: float = DEFAULT_EPS) -> "DomainSpec":
        """由表达式 AST 构造其定义域规格。"""
        return cls(conditions=collect_domain_conditions(ast), epsilon=eps)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DomainSpec":
        """由 :meth:`to_dict` 的产物还原。"""
        if not isinstance(payload, Mapping):
            raise DomainError("DomainSpec 序列化必须是字典")
        eps = float(payload.get("epsilon", DEFAULT_EPS))
        conditions = []
        for item in payload.get("conditions", ()):
            if not isinstance(item, Mapping):
                raise DomainError("定义域条件的序列化必须是字典")
            conditions.append(
                DomainCondition(kind=str(item["kind"]), operand=_list_to_tuple(item["operand"]))
            )
        return cls(conditions=tuple(conditions), epsilon=eps)

    # -- 性质 -------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """是否不含任何条件（即在全实数上有定义）。"""
        return not self.conditions

    def required_variables(self) -> frozenset[str]:
        """返回定义域条件涉及的全部变量名。"""
        names: set[str] = set()
        for condition in self.conditions:
            names.update(condition.variables)
        return frozenset(names)

    # -- 判定 -------------------------------------------------------------

    def satisfied_by(self, point: Mapping[str, float]) -> bool:
        """判断给定取值点是否落在定义域内。

        纯函数：仅依赖 ``self`` 与 ``point``，不读取任何数据集。
        """
        for condition in self.conditions:
            if not condition.holds(point, self.epsilon):
                return False
        return True

    def symbolically_ok(self) -> bool:
        """在不给定取值点时的**结构性**检查。

        只校验「被约束子树为常量」的条件——这类条件的真假与取值点无关，
        若其本身不成立，则定义域为空（例如 ``sqrt(-1)``）。
        含变量的条件在此视为「可能满足」，不在本方法内判定。
        """
        for condition in self.conditions:
            if condition.variables:
                continue
            if not condition.holds({}, self.epsilon):
                return False
        return True

    # -- 序列化 -----------------------------------------------------------

    def describe(self) -> str:
        """返回定义域的可读说明；无条件时返回「全实数」。"""
        if self.is_empty:
            return "全实数"
        return "；".join(condition.render(self.epsilon) for condition in self.conditions)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（INTERFACES.md §2.1 的 ``domain`` 字段）。"""
        return {
            "version": DOMAIN_SPEC_VERSION,
            "epsilon": self.epsilon,
            "empty": self.is_empty,
            "text": self.describe(),
            "conditions": [condition.to_dict(self.epsilon) for condition in self.conditions],
        }


def _list_to_tuple(payload: Any) -> tuple:
    """把 :func:`~sdl_m02.expressions.to_ast` 的嵌套列表还原为元组 AST。"""
    if not isinstance(payload, list) or not payload:
        raise DomainError("AST 序列化必须是形如 ['kind', ...] 的非空列表")
    kind = payload[0]
    if kind == "num":
        return ("num", float(payload[1]))
    if kind == "var":
        return ("var", str(payload[1]))
    if kind in ("neg", "inv"):
        return (kind, _list_to_tuple(payload[1]))
    if kind == "pow":
        return ("pow", _list_to_tuple(payload[1]), int(payload[2]))
    if kind == "func":
        return ("func", str(payload[1]), _list_to_tuple(payload[2]))
    if kind in ("add", "mul"):
        return (kind, *[_list_to_tuple(item) for item in payload[1:]])
    raise DomainError(f"未知的 AST 节点类型：{kind!r}")


# ---------------------------------------------------------------------------
# 单点求值（仅服务于定义域条件）
# ---------------------------------------------------------------------------


def _evaluate(ast: tuple, point: Mapping[str, float]) -> float:
    """在给定取值点上对（条件所引用的）子树求值。

    这是本模块唯一的求值入口，且**只应在定义域条件上使用**——
    候选表达式本身的数据求值属 M3 的搜索阶段。
    """
    kind = ast[0]
    if kind == "num":
        return ast[1]

    if kind == "var":
        name = ast[1]
        if name not in point:
            raise DomainError(f"定义域检查缺少变量取值：{name!r}")
        raw = point[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise DomainError(f"变量 {name!r} 的取值必须是实数，实际得到：{raw!r}")
        value = float(raw)
        if not math.isfinite(value):
            raise DomainError(f"变量 {name!r} 的取值必须是有限实数，实际得到：{raw!r}")
        return value

    if kind == "neg":
        return -_evaluate(ast[1], point)

    if kind == "inv":
        return 1.0 / _evaluate(ast[1], point)

    if kind == "add":
        total = 0.0
        for child in ast[1:]:
            total += _evaluate(child, point)
        return total

    if kind == "mul":
        product = 1.0
        for child in ast[1:]:
            product *= _evaluate(child, point)
        return product

    if kind == "pow":
        return _evaluate(ast[1], point) ** ast[2]

    if kind == "func":
        name = ast[1]
        value = _evaluate(ast[2], point)
        if name == "abs":
            return abs(value)
        if name == "sqrt":
            return math.sqrt(value)
        if name == "log":
            return math.log(value)
        if name == "exp":
            return math.exp(value)
        raise DomainError(f"不支持的一元函数 {name!r}")

    raise DomainError(f"无法求值的 AST 节点类型：{kind!r}")


# ---------------------------------------------------------------------------
# 候选对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """一个候选变量／表示。

    字段与 ``INTERFACES.md`` §2.1 的 Candidate 对齐：

    - ``candidate_id``：稳定标识，由规范式哈希派生，同一语义恒得同一 id；
    - ``expression``（属性）：规范化 AST 的 JSON 兼容编码；
    - ``depth`` / ``node_count``：规模度量，用于预算约束；
    - ``domain``：显式定义域；
    - ``unit``：量纲签名，本阶段恒为 ``None``（未定，留待后续阶段）；
    - ``provenance``：构造来源与版本；本阶段不含任何数据引用。
    """

    candidate_id: str
    ast: tuple
    domain: DomainSpec
    depth: int
    node_count: int
    provenance: dict
    unit: str | None = field(default=None)

    @classmethod
    def create(
        cls,
        ast: tuple,
        domain: DomainSpec,
        provenance: Mapping[str, Any] | None = None,
        unit: str | None = None,
    ) -> "Candidate":
        """由规范式 AST 构造候选，并派生 ``candidate_id`` 与规模度量。"""
        canonical = _ensure_canonical(ast)
        key = to_canonical_json(canonical)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return cls(
            candidate_id=f"{_CANDIDATE_ID_PREFIX}{digest}",
            ast=canonical,
            domain=domain,
            depth=depth(canonical),
            node_count=node_count(canonical),
            provenance=dict(provenance) if provenance else {},
            unit=unit,
        )

    @property
    def expression(self) -> list:
        """规范化 AST 的 JSON 兼容编码（可由 ``expressions.from_ast`` 还原）。"""
        return to_ast(self.ast)

    def expression_key(self) -> str:
        """候选的语义唯一标识（规范式紧凑 JSON 串）。"""
        return to_canonical_json(self.ast)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "candidate_id": self.candidate_id,
            "expression": self.expression,
            "depth": self.depth,
            "node_count": self.node_count,
            "domain": self.domain.to_dict(),
            "unit": self.unit,
            "provenance": dict(self.provenance),
        }


# ---------------------------------------------------------------------------
# 定义域检查（纯函数入口）
# ---------------------------------------------------------------------------


def domain_ok(
    target: Candidate | DomainSpec | tuple,
    point: Mapping[str, float] | None = None,
    *,
    eps: float | None = None,
) -> bool:
    """判断定义域是否成立。**纯函数**，不依赖任何数据集。

    :param target: :class:`Candidate`、:class:`DomainSpec`，或一条规范式 AST。
    :param point: 取值点（变量名 → 数值）的字典。

        - 给定时：在该点上判定全部条件，任一不成立即返回 ``False``；
        - 为 ``None`` 时：只判定与取值无关的常量条件，
          若存在被常量违反的条件（定义域为空，如 ``sqrt(-1)``）则返回 ``False``，
          含变量的条件视为「可能满足」。
    :param eps: 覆盖定义域容差；``None`` 表示沿用对象自身的 ε。
    :raises DomainError: 取值点缺少必需变量，或取值不是有限实数。
    """
    spec = _as_domain_spec(target, eps=eps)
    if point is None:
        return spec.symbolically_ok()
    if not isinstance(point, Mapping):
        raise DomainError("取值点必须是「变量名 → 数值」的映射")
    return spec.satisfied_by(point)


def _as_domain_spec(target: Candidate | DomainSpec | tuple, *, eps: float | None) -> DomainSpec:
    """把 ``domain_ok`` 接受的多种输入统一为 :class:`DomainSpec`。"""
    if isinstance(target, Candidate):
        spec = target.domain
    elif isinstance(target, DomainSpec):
        spec = target
    elif isinstance(target, tuple):
        spec = DomainSpec.for_expression(target, eps if eps is not None else DEFAULT_EPS)
    else:
        raise DomainError(
            "定义域检查的输入必须是 Candidate、DomainSpec 或规范式 AST 元组；"
            f"实际得到：{type(target).__name__}"
        )
    if eps is not None:
        spec = DomainSpec(conditions=spec.conditions, epsilon=eps)
    return spec


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


def enumerate_candidates(
    variables: Sequence[str],
    *,
    constants: Iterable[float] = DEFAULT_CONSTANTS,
    functions: Iterable[str] = SUPPORTED_FUNCTIONS,
    powers: Iterable[int] = DEFAULT_POWERS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    eps: float = DEFAULT_EPS,
) -> list[Candidate]:
    """在受限语法内枚举候选变量。

    :param variables: 参与枚举的变量名序列（例如 ``["X1", "X2", "X3"]``）。
    :param constants: 作为叶子的常数集合。
    :param functions: 参与枚举的一元变换，必须是 :data:`SUPPORTED_FUNCTIONS` 的子集。
    :param powers: 参与枚举的正整数幂指数。
    :param max_depth: 单个候选的深度上限。
    :param max_nodes: 单个候选的节点数上限。
    :param max_candidates: 候选数量上限（**声明上界**，返回值长度不会超过它）。
    :param max_attempts: 构造尝试次数上限，防止组合爆炸。
    :param eps: 定义域容差 ε。
    :return: 候选列表，按 ``(节点数, 深度, 规范式)`` 升序排列，顺序完全确定。

    保证：

    1. 结果有上界（``max_candidates``）且**无重复语义**——任意两个候选的规范式不同；
    2. 除法（由 ``inv`` 承载）等候选**显式携带**定义域条件；
    3. 全过程不读取任何数据。

    :raises DomainError: 参数非法。
    """
    names = _validate_variables(variables)
    const_values = _validate_constants(constants)
    func_names = _validate_functions(functions)
    power_values = _validate_powers(powers)
    _validate_bounds(max_depth, max_nodes, max_candidates, max_attempts, eps)

    provenance = {
        "origin": "enumerate_candidates",
        "builder": "sdl_m02/domain.py",
        "builder_version": DOMAIN_SPEC_VERSION,
        "variables": list(names),
        "data_refs": [],  # 显式声明：本阶段不接触任何数据分区
    }

    # -- 第 1 层：叶子 ---------------------------------------------------
    seen: dict[str, tuple] = {}
    buckets: dict[int, list[tuple]] = {}

    leaves: list[tuple] = []
    for value in const_values:
        leaves.append(_ensure_canonical(("num", float(value))))
    for name in names:
        leaves.append(("var", name))

    for leaf in sorted(leaves, key=to_canonical_json):
        key = to_canonical_json(leaf)
        if key in seen:
            continue
        seen[key] = leaf
        buckets.setdefault(node_count(leaf), []).append(leaf)

    for size in buckets:
        buckets[size].sort(key=to_canonical_json)

    # -- 第 2..max_nodes 层：组合 ---------------------------------------
    attempts = 0
    truncated = False

    for size in range(2, max_nodes + 1):
        if len(seen) >= max_candidates:
            truncated = True
            break

        produced: list[tuple] = []

        # 一元构造：作用在「恰好小一层」的规范式上
        for child in buckets.get(size - 1, ()):
            produced.append(("neg", child))
            produced.append(("inv", child))  # 除法因子
            for func_name in func_names:
                produced.append(("func", func_name, child))
            for exponent in power_values:
                produced.append(("pow", child, exponent))

        # 二元构造：两侧节点数之和为 size - 1（算子自身占 1 个节点）
        for left_size in range(1, (size - 1) // 2 + 1):
            right_size = size - 1 - left_size
            left_bucket = buckets.get(left_size)
            right_bucket = buckets.get(right_size)
            if not left_bucket or not right_bucket:
                continue
            for left in left_bucket:
                for right in right_bucket:
                    produced.append(("add", left, right))
                    produced.append(("mul", left, right))

        layer: list[tuple] = []
        for raw in produced:
            attempts += 1
            if attempts > max_attempts:
                truncated = True
                break
            node = _ensure_canonical(raw)
            if depth(node) > max_depth or node_count(node) > max_nodes:
                continue
            key = to_canonical_json(node)
            if key in seen:
                continue  # 构造层面就地消除同语义形式
            seen[key] = node
            if node_count(node) == size:
                # 规模恰好等于本层者，参与更深层的组合；
                # 常数折叠导致规模变小者仅作为候选收录（见模块说明的限制）。
                layer.append(node)
            if len(seen) >= max_candidates:
                truncated = True
                break

        layer.sort(key=to_canonical_json)
        buckets[size] = layer
        if truncated:
            break

    # -- 组装候选 -------------------------------------------------------
    ordered = sorted(seen.values(), key=lambda ast: (node_count(ast), depth(ast), to_canonical_json(ast)))
    candidates: list[Candidate] = []
    for ast in ordered:
        spec = DomainSpec.for_expression(ast, eps=eps)
        candidates.append(Candidate.create(ast, spec, provenance))

    _ = truncated  # 截断只影响数量，不影响本函数的对外承诺
    return candidates


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def _validate_variables(variables: Sequence[str]) -> tuple[str, ...]:
    """校验变量名序列：非空、唯一、非空字符串。"""
    if isinstance(variables, str) or not isinstance(variables, Sequence):
        raise DomainError("variables 必须是变量名序列（例如 ['X1', 'X2', 'X3']）")
    names = tuple(variables)
    if not names:
        raise DomainError("variables 不能为空：至少需要一个变量")
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name:
            raise DomainError(f"变量名必须是非空字符串，实际得到：{name!r}")
        if name in seen:
            raise DomainError(f"变量名重复：{name!r}")
        seen.add(name)
    return names


def _validate_constants(constants: Iterable[float]) -> tuple[float, ...]:
    """校验常数集合：全部是有限的实数。"""
    values: list[float] = []
    try:
        iterator = iter(constants)
    except TypeError as exc:
        raise DomainError("constants 必须是可迭代的实数集合") from exc
    for value in iterator:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DomainError(f"常数必须是实数，实际得到：{value!r}")
        if not math.isfinite(float(value)):
            raise DomainError(f"常数必须是有限实数，实际得到：{value!r}")
        values.append(float(value))
    return tuple(values)


def _validate_functions(functions: Iterable[str]) -> tuple[str, ...]:
    """校验一元变换集合：必须是受支持函数的子集，且无重复。"""
    names: list[str] = []
    for name in functions:
        if name not in SUPPORTED_FUNCTIONS:
            raise DomainError(
                f"不支持的一元变换 {name!r}；仅支持 {', '.join(SUPPORTED_FUNCTIONS)}"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def _validate_powers(powers: Iterable[int]) -> tuple[int, ...]:
    """校验幂指数集合：正整数且不小于 2（0 与 1 会被规范化消去）。"""
    values: list[int] = []
    for exponent in powers:
        if isinstance(exponent, bool) or not isinstance(exponent, int):
            raise DomainError(f"幂指数必须是整数，实际得到：{exponent!r}")
        if exponent < 2:
            raise DomainError(f"幂指数必须不小于 2（0 与 1 会被规范化消去），实际得到：{exponent}")
        if exponent not in values:
            values.append(exponent)
    return tuple(values)


def _validate_bounds(
    max_depth: int,
    max_nodes: int,
    max_candidates: int,
    max_attempts: int,
    eps: float,
) -> None:
    """校验全部上界与容差。"""
    for label, value in (
        ("max_depth", max_depth),
        ("max_nodes", max_nodes),
        ("max_candidates", max_candidates),
        ("max_attempts", max_attempts),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise DomainError(f"{label} 必须是正整数，实际得到：{value!r}")
    if isinstance(eps, bool) or not isinstance(eps, (int, float)):
        raise DomainError("eps 必须是实数")
    if not math.isfinite(float(eps)) or float(eps) <= 0:
        raise DomainError("eps 必须是正的有限实数")
