"""M2 表示构造管线与去重剪枝（P03 交付物）。

本模块把 P01 的**规范化**与 P02 的**有界枚举**串成一条可复现的构造管线，
并对候选池做**去重**与**被支配剪枝**：

1. :func:`build_representation` —— 管线入口。读取探索侧记录，从中提取
   「可作为叶子出现的变量」，据此枚举候选，再依次做去重与被支配剪枝，
   最终返回受预算约束的 :class:`RepresentationPool`。
2. :func:`dedup` —— 剔除**语义重复**与**参数微变体**，保留结构不同的候选。
3. :func:`prune_dominated` —— 剔除在规模与定义域代价两方面均不优于
   同结构参照候选的**被支配**候选。

设计边界（严格遵守 P03 增量卡）：

- **数据访问只经 explorer 角色，且只读 E 分区**。本模块不直接打开数据库、
  不构造连接、不派生令牌，只接受调用方传入的 M1 客户端（:class:`Module01`
  实例），并以**位置参数**调用其 ``read_dataset(ref)``。
  M1 的读方法对 explorer 同时放行 E 与 V（二者皆为开发视图），因此
  **「严禁访问 V」不能依赖 M1 拒绝**：本模块**强制要求**调用方提供协议描述
  （含 ``spec["resources"]`` 分区映射），据此**主动**校验引用归属——只有
  等于 ``E`` 的引用才被放行，其余（V、C1、H_* 等）一律以
  :class:`RepresentError` 拒绝。引用不在映射中时同样拒绝——不猜测、不放行。
  缺少映射时**整个读取被拒绝**：分区引用形如 ``data_<hex>``，无法自证归属，
  无法校验即不读取。
- 本模块**不接触任何令牌正文**：不读取、不打印、不序列化令牌。
- 本模块**不做模式搜索**（属 M3）：不拟合、不残差、不评分、不做重采样；
  唯一被读取的数据用途是「确定有哪些变量可供枚举」。
- 本模块**不生成假说**（属 M4），**不做筛选排序打分**（属 M5）。
- 仅使用 Python 3.11+ 标准库。

「参数微变体」的判定口径
------------------------

P01 的规范化会把**常数因子折入系数**（``2 * X1`` 保持原样），因此
``X1 / 2``、``X1 / 3`` 这类「同结构、仅常数不同」的表达式在规范式层面
并不相等。本模块按 **skeleton（骨架）** 把它们归为一类：

- skeleton 是把规范式中所有 ``num`` 叶子替换为占位符 ``"_"`` 后得到的树；
- 同一 skeleton 下的候选构成一族 **结构等价类**；
- 族内按 (节点数, 深度, 规范式 JSON) 排序，**只保留首个**作为代表，
  其余即为「参数微变体」而被合并。

由此「结构多样性保留」体现为：每个 skeleton 恰好留下一个代表，
不同 skeleton（如 ``X1 + X2`` 与 ``X1 * X2``、``X1 / X2`` 与 ``X1 / X3``——
后者骨架相同故合并）不会被误删。

「被支配」的口径与实测效果
--------------------------

**为何「被支配」只能按结构代价判定**：本阶段尚无误差指标——拟合与残差
属 M3、Pareto 支配（误差 vs 复杂度）属 M5。故 P03 的「被支配」只能是
**结构 / 定义域代价**层面的：同一 skeleton 族内，规模与定义域条件数
均不劣且至少一项严格更优者支配其余。

**实测（3 变量、容量 5000）**：不合法候选（定义域可证为空，如
``1 / 0``、``sqrt(-1)``）有 505 个，是最主要的剔除来源；家族合并把
3103 个合法候选压到 478 个；而**同族内严格被支配者仅 2 个**——
因为同一 skeleton 的成员规模相同，定义域代价差异很小。
这不是实现缺陷，而是 M2 阶段信息受限的必然结果：真实的支配筛选要等
M5 拿到误差指标后才能充分展开。本模块如实保留该规则作为安全网，
不夸大其作用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .domain import (
    DEFAULT_EPS,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_NODES,
    Candidate,
    DomainError,
    DomainSpec,
    enumerate_candidates,
)
from .expressions import (
    DEFAULT_MAX_DEPTH,
    SUPPORTED_FUNCTIONS,
    ExpressionError,
    depth,
    node_count,
    normalize,
    to_canonical_json,
)

__all__ = [
    "RepresentError",
    "Budget",
    "RepresentationPool",
    "DEFAULT_TARGET_FIELD",
    "DEFAULT_MAX_LEAVES",
    "DEFAULT_ENUMERATION_CAPACITY",
    "build_representation",
    "dedup",
    "prune_dominated",
    "skeleton",
    "domain_cost",
]


class RepresentError(ValueError):
    """表示构造、去重或剪枝的输入非法。"""


# ---------------------------------------------------------------------------
# 常量与默认配置
# ---------------------------------------------------------------------------

#: 默认的「目标变量」字段名。从记录中提取的叶子**排除**该字段——
#: 目标是待预测的量，不应作为构造表示的输入变量。
DEFAULT_TARGET_FIELD = "Y"

#: 从记录中提取叶子变量的数量上界，避免记录字段过多导致枚举爆炸。
DEFAULT_MAX_LEAVES = 8

#: **枚举阶段的内部容量**，与输出的 ``Budget.max_candidates`` 解耦。
#:
#: 为什么需要：P02 的枚举按节点数分层递推，层内候选数呈组合增长。若直接拿
#: 紧凑的输出上界去截断枚举，靠前的浅层表达式会把容量占满，深层结构根本
#: 没有机会出现，结构多样性被预算压扁。故枚举阶段用本容量先铺开候选，
#: 再经剪枝 / 去重后截到输出上界。
#:
#: 取值依据（实测，3 变量）：容量 1000 → 最大节点数 4、约 0.04s；
#: 5000 → 6 节点、约 0.8s；20000 → 7 节点、约 6.2s。选 5000 作为
#: 「结构深度」与「耗时」的平衡点。
DEFAULT_ENUMERATION_CAPACITY = 5000

#: 骨架中常数叶子的占位符。选一个非法变量名，确保不会与真实变量冲突。
_CONSTANT_PLACEHOLDER = "_"


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """表示构造的资源预算。

    两个维度：

    - ``max_candidates``：候选池的**数量**上界（硬约束，返回值长度不超过它）；
    - ``max_nodes`` / ``max_depth``：单个候选的**规模**上界。

    另设 ``max_attempts`` 兜住「数量未达上界但组合爆炸」的情形。

    注意：``max_candidates`` 只约束**最终输出**。管线内部会先按更宽的容量
    充分枚举，再经剪枝 / 去重后截断到该上界——否则紧凑的上界会让浅层表达式
    占满枚举空间，反而压扁结构多样性。
    """

    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_nodes: int = DEFAULT_MAX_NODES
    max_depth: int = DEFAULT_MAX_DEPTH
    max_attempts: int = 200_000

    def __post_init__(self) -> None:
        for label, value in (
            ("max_candidates", self.max_candidates),
            ("max_nodes", self.max_nodes),
            ("max_depth", self.max_depth),
            ("max_attempts", self.max_attempts),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RepresentError(f"{label} 必须是正整数，实际得到：{value!r}")


# ---------------------------------------------------------------------------
# 骨架与定义域代价
# ---------------------------------------------------------------------------


def skeleton(ast: tuple) -> tuple:
    """把规范式中的常数叶子折叠为占位符，得到**结构骨架**。

    参数微变体（仅常数不同的同结构表达式）拥有相同骨架，
    因而可被 :func:`dedup` 归入同一等价类。

    :raises RepresentError: 输入不是可识别的 AST。
    """
    if not isinstance(ast, tuple) or not ast:
        raise RepresentError(f"AST 必须是非空元组，实际得到：{ast!r}")
    kind = ast[0]
    if kind == "num":
        return ("num", _CONSTANT_PLACEHOLDER)
    if kind == "var":
        return ("var", ast[1])
    if kind in ("neg", "inv"):
        return (kind, skeleton(ast[1]))
    if kind == "pow":
        # 指数也是常数：X1^2 与 X1^3 结构相同，属参数微变体。
        return ("pow", skeleton(ast[1]), _CONSTANT_PLACEHOLDER)
    if kind == "func":
        return ("func", ast[1], skeleton(ast[2]))
    if kind in ("add", "mul"):
        # 规范式已按交换律排序，此处保持顺序以确保骨架可复现。
        return (kind, *[skeleton(child) for child in ast[1:]])
    raise RepresentError(f"无法提取骨架的 AST 节点类型：{kind!r}")


def domain_cost(candidate: Candidate) -> int:
    """定义域代价：候选携带的条件条数。

    条件越多，表示越「贵」——越多的分母非零 / 开方非负 / 取对数为正约束，
    意味着越小的可用取值域。剪枝用它与规模一同判定被支配。
    """
    return len(candidate.domain.conditions)


# ---------------------------------------------------------------------------
# 候选池
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepresentationPool:
    """表示构造的产出：受预算约束的候选池 + 可追溯的来源说明。

    :param candidates: 去重与被支配剪枝后的候选，按
        ``(节点数, 深度, 规范式)`` 升序，顺序完全确定。
    :param provenance: 构造来源；**只记录 E 引用的标识与数量**，
        不含任何令牌、不含记录正文。
    """

    candidates: tuple[Candidate, ...]
    provenance: dict

    def __len__(self) -> int:
        return len(self.candidates)

    def __iter__(self):
        return iter(self.candidates)

    @property
    def size(self) -> int:
        """候选池大小。"""
        return len(self.candidates)

    def variables(self) -> tuple[str, ...]:
        """池中候选涉及的变量名（升序）。"""
        names: set[str] = set()
        for candidate in self.candidates:
            names.update(_variables_of(candidate.ast))
        return tuple(sorted(names))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "size": len(self.candidates),
            "budget": dict(self.provenance.get("budget", {})),
            "exploration_refs": list(self.provenance.get("exploration_refs", [])),
            "records_read": self.provenance.get("records_read", 0),
            "candidates": [c.to_dict() for c in self.candidates],
            "provenance": dict(self.provenance),
        }


def _variables_of(ast: tuple) -> set[str]:
    """收集 AST 中出现的变量名。"""
    kind = ast[0]
    if kind == "var":
        return {ast[1]}
    found: set[str] = set()
    for child in ast[1:]:
        if isinstance(child, tuple):
            found |= _variables_of(child)
    return found


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


def dedup(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """去重：保留**结构不同**的候选，合并参数微变体。

    两级去重：

    1. **语义重复**——规范式完全相同者只留一个（P02 已在构造层消除，
       此处作为对任意输入的兜底）。
    2. **参数微变体**——骨架完全相同者只留一个，取排序最前的那个：
       规模小者优先（节点数 → 深度），再按规范式 JSON 定序。
       例如 ``X1 / 2`` 与 ``X1 / 3`` 同骨架，保留前者。

    :return: 去重后的候选元组，顺序确定。
    :raises RepresentError: 输入元素不是 :class:`Candidate`。
    """
    exact: dict[str, Candidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, Candidate):
            raise RepresentError(f"候选必须是 Candidate 实例，实际得到：{type(candidate).__name__}")
        key = candidate.expression_key()
        if key not in exact:
            exact[key] = candidate

    families: dict[str, Candidate] = {}
    for candidate in exact.values():
        key = to_canonical_json(skeleton(candidate.ast))
        current = families.get(key)
        if current is None or _variant_rank(candidate) < _variant_rank(current):
            families[key] = candidate

    return tuple(sorted(families.values(), key=_variant_rank))


def _variant_rank(candidate: Candidate) -> tuple:
    """参数微变体的代表选取顺序：先比规模，再比规范式。"""
    return (candidate.node_count, candidate.depth, candidate.expression_key())


# ---------------------------------------------------------------------------
# 被支配剪枝
# ---------------------------------------------------------------------------


def prune_dominated(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """剪枝：剔除**不合法**候选与被**同结构**参照候选支配的候选。

    两级剪枝：

    1. **不合法**——定义域可被证明为空（:meth:`DomainSpec.symbolically_ok`
       为 ``False``）的候选一律移除。例如 ``1 / 0``、``sqrt(-1)``：
       这类候选在任何取值点上都无法代入，不构成可用的表示。
       注意含变量的条件在此视为「可能满足」，不会被误判为空定义域。
    2. **被支配**——同骨架族内，候选 A 支配 B 当且仅当 A 的**规模**与
       **定义域代价**均不劣于 B，且至少一项严格更优。规模按
       ``(节点数, 深度)`` 比较，定义域代价见 :func:`domain_cost`。

    为什么只在同骨架族内比较：跨骨架没有统一的「更简」口径——
    ``X1 + X2`` 与 ``X1 * X2`` 是两种结构，不应互相判为冗余。

    :return: 剪枝后的候选元组，顺序确定。
    :raises RepresentError: 输入元素不是 :class:`Candidate`。
    """
    families: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Candidate):
            raise RepresentError(f"候选必须是 Candidate 实例，实际得到：{type(candidate).__name__}")
        # 第 1 级：不合法候选（定义域可证为空）先行剔除。
        if not candidate.domain.symbolically_ok():
            continue
        families.setdefault(to_canonical_json(skeleton(candidate.ast)), []).append(candidate)

    kept: list[Candidate] = []
    for members in families.values():
        ordered = sorted(members, key=lambda c: (c.node_count, c.depth, domain_cost(c)))
        survivors: list[Candidate] = []
        for candidate in ordered:
            # 第 2 级：被支配者不进入存活集。
            if _is_dominated(candidate, survivors):
                continue
            survivors.append(candidate)
        kept.extend(survivors)

    return tuple(sorted(_unique(kept), key=_variant_rank))


def _is_dominated(candidate: Candidate, survivors: Sequence[Candidate]) -> bool:
    """``candidate`` 是否被 ``survivors`` 中的某个候选支配。"""
    size = (candidate.node_count, candidate.depth)
    cost = domain_cost(candidate)
    for other in survivors:
        other_size = (other.node_count, other.depth)
        other_cost = domain_cost(other)
        if other_size <= size and other_cost <= cost and (other_size, other_cost) < (size, cost):
            return True
    return False


def _unique(candidates: Sequence[Candidate]) -> list[Candidate]:
    """按语义标识去重，保持首次出现顺序。"""
    seen: dict[str, Candidate] = {}
    for candidate in candidates:
        seen.setdefault(candidate.expression_key(), candidate)
    return list(seen.values())


# ---------------------------------------------------------------------------
# 管线入口
# ---------------------------------------------------------------------------


def build_representation(
    exploration_records: Any,
    spec: Any = None,
    budget: Budget | Mapping[str, Any] | None = None,
    *,
    clients: Sequence[Any] | None = None,
    target_field: str = DEFAULT_TARGET_FIELD,
    max_leaves: int = DEFAULT_MAX_LEAVES,
    constants: Iterable[float] | None = None,
    functions: Iterable[str] = SUPPORTED_FUNCTIONS,
    powers: Iterable[int] = (2, 3),
    eps: float = DEFAULT_EPS,
) -> RepresentationPool:
    """表示构造管线主入口。

    :param exploration_records: 探索侧输入，三种形态皆可：

        - **M1 客户端**（:class:`~sdl_m01.Module01` 实例）：本模块以
          ``client.read_dataset(ref)`` 读取记录，``ref`` 默认取
          ``spec["resources"]["E"]``。
        - **E 引用字符串**（如 ``"data_2799ec…"``）：必须同时经 ``clients``
          传入客户端，且 ``spec`` 必须给出分区映射以供校验归属。
        - **记录序列**（已读取的 :class:`list`）：直接使用，不再触发任何读取，
          也不需要 ``spec``。这条路径供调用方自行完成读取与审计，本模块不代劳。

    :param spec: 协议描述字典（:meth:`Module01.describe` 的产物或等价字典），
        **必须含 ``resources`` 分区映射**。由于分区引用形如 ``data_<hex>``、
        无法自证归属，缺少映射时本模块拒绝读取（宁可报错也不越界）。
        仅「记录序列」路径可省略 ``spec``。
    :param budget: :class:`Budget`，或可映射为 :class:`Budget` 字段的字典。
    :param clients: 当 ``exploration_records`` 为引用字符串时使用的客户端序列；
        取首个。仅用于读取**探索侧**分区。
    :param target_field: 从记录中提取叶子时要排除的目标字段名。
    :param max_leaves: 提取的叶子变量数量上界。
    :param constants: 参与枚举的常数集合；``None`` 表示沿用 P02 默认值。
    :param functions: 参与枚举的一元变换。
    :param powers: 参与枚举的正整数幂指数。
    :param eps: 定义域容差 ε。
    :return: 受预算约束的 :class:`RepresentationPool`。
    :raises RepresentError: 参数形态非法。
    :raises sdl_m01.AccessDenied: 引用不可被探索侧读取（M1 拒绝，如实传递）。
    """
    # 延迟导入：本模块只借 M1 的**类型**做判定，不引入额外运行时依赖，
    # 也不构造任何连接。M1 缺失时按「非客户端」处理，不影响记录序列路径。
    client_type = _module01_type()

    box = _as_budget(budget)
    refs: list[str] = []
    records: list[dict]
    resources: dict | None = None

    if isinstance(exploration_records, str):
        client = _pick_client(clients)
        ref, resources = _resolve_ref(spec, explicit=exploration_records)
        records = _read_exploration(client, ref)
        refs.append(ref)
    elif client_type is not None and isinstance(exploration_records, client_type):
        ref, resources = _resolve_ref(spec)
        records = _read_exploration(exploration_records, ref)
        refs.append(ref)
    elif isinstance(exploration_records, Sequence):
        records = _validate_records(exploration_records)
    else:
        raise RepresentError(
            "exploration_records 必须是 M1 客户端、E 引用字符串或记录序列；"
            f"实际得到：{type(exploration_records).__name__}"
        )

    leaves = _extract_leaves(records, target_field=target_field, max_leaves=max_leaves)
    if not leaves:
        raise RepresentError(
            f"未能从记录中提取任何候选变量（已排除目标字段 {target_field!r}）"
        )

    # 枚举阶段用**内部容量**而非直接使用输出预算：若一开始就按紧凑的
    # 输出上界截断枚举，候选池会被浅层表达式占满，反而损害结构多样性。
    # 故先按「输出预算与默认探索容量中的较大者」充分枚举，再经剪枝 / 去重，
    # 最后才截到 `max_candidates`。这样输出上界始终被遵守，同时多样性不被
    # 预算过早压扁。
    internal_cap = max(box.max_candidates, DEFAULT_ENUMERATION_CAPACITY)
    kwargs: dict[str, Any] = {
        "functions": tuple(functions),
        "powers": tuple(powers),
        "max_depth": box.max_depth,
        "max_nodes": box.max_nodes,
        "max_candidates": internal_cap,
        "max_attempts": box.max_attempts,
        "eps": eps,
    }
    if constants is not None:
        kwargs["constants"] = tuple(constants)

    enumerated = enumerate_candidates(leaves, **kwargs)
    # 先剔除不合法候选，再作结构去重：否则「不合法但规模更小」的变体
    # 可能被 dedup 选为骨架族代表，把整族合法候选挤掉。
    pruned = prune_dominated(enumerated)
    deduped = dedup(pruned)
    final = deduped[: box.max_candidates]

    provenance = {
        "origin": "build_representation",
        "builder": "sdl_m02/represent.py",
        "exploration_refs": list(refs),  # 只记 E 引用标识，不含令牌
        "records_read": len(records),
        "leaves": list(leaves),
        "target_field": target_field,
        "budget": {
            "max_candidates": box.max_candidates,
            "max_nodes": box.max_nodes,
            "max_depth": box.max_depth,
            "max_attempts": box.max_attempts,
            "enumeration_capacity": internal_cap,
        },
        "counts": {
            "enumerated": len(enumerated),
            "pruned": len(pruned),
            "deduped": len(deduped),
            "final": len(final),
        },
        "access_role": "explorer",
        "data_refs": list(refs),
        "allowed_purposes": ["E"],
    }
    if resources is not None:
        # 只登记分区**用途键**，不复制引用正文之外的任何协议内容。
        provenance["protocol_partitions"] = sorted(str(k) for k in resources)
    pool = RepresentationPool(candidates=tuple(final), provenance=provenance)
    return _seal(pool)


def _seal(pool: RepresentationPool) -> RepresentationPool:
    """为候选池附上内容指纹，便于跨轮比对与审计。"""
    digest = hashlib.sha256(
        to_canonical_json([c.expression for c in pool.candidates]).encode("utf-8")
    ).hexdigest()[:16]
    pool.provenance["pool_digest"] = digest
    return pool


# ---------------------------------------------------------------------------
# 数据访问：只经 explorer 角色的公开接口
# ---------------------------------------------------------------------------


def _read_exploration(client: Any, ref: Any) -> list[dict]:
    """经 M1 客户端读取**探索侧**记录。

    本函数是模块内**唯一**的数据读取入口，只调用 ``client.read_dataset``：

    - 不打开数据库、不拼接 SQL、不派生令牌；
    - 角色合法性由 M1 判定——非 explorer / custodian 的令牌会在 M1 内
      触发 ``AccessDenied``；确认侧分区（``C*``）同样被 M1 拒绝；
    - 不使用可变默认参数，不缓存返回结果。

    调用方若持有 explorer 之外的客户端，错误会由 M1 抛出并在此如实传递。
    """
    if client is None:
        raise RepresentError("读取探索分区需要传入 M1 客户端")
    reader = getattr(client, "read_dataset", None)
    if not callable(reader):
        raise RepresentError(
            "客户端必须提供 read_dataset 方法（M1 的公开读取接口），"
            f"实际得到：{type(client).__name__}"
        )
    _require_ref(ref)
    data = reader(ref)
    return _validate_records(data)


def _require_ref(ref: Any) -> None:
    """校验分区引用形态。"""
    if not isinstance(ref, str) or not ref.strip():
        raise RepresentError(
            "分区引用必须是非空字符串（如 data_<hex>）；"
            f"实际得到：{ref!r}"
        )


def _validate_records(data: Any) -> list[dict]:
    """校验读取结果是记录序列。"""
    if isinstance(data, (str, bytes)) or not isinstance(data, Sequence):
        raise RepresentError(
            "读取结果必须是记录序列；"
            f"实际得到：{type(data).__name__}"
        )
    records: list[dict] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise RepresentError(
                f"每条记录必须是映射，实际得到：{type(item).__name__}"
            )
        records.append(dict(item))
    return records


def _pick_client(clients: Sequence[Any] | None) -> Any:
    """从客户端序列中取出用于读取探索侧的那一个。"""
    if not clients:
        raise RepresentError("传入 E 引用字符串时必须同时提供 clients")
    return clients[0]


def _resolve_ref(spec: Any, *, explicit: str | None = None) -> tuple[str, dict]:
    """解析出要读取的引用，并**强制**取得协议分区映射以校验归属。

    设计要点：分区引用形如 ``data_<hex>``，**无法从引用本身**看出它属于
    E 还是 V/C1。因此「只读 E」这条约束只有在拿到协议的分区映射时才能
    被真正执行。基于「严禁访问 V、不猜测、不放行」的原则，本函数**要求**
    调用方提供映射：

    - 若 ``spec`` 是含 ``resources`` 的字典（:meth:`Module01.describe` 的产物），
      取出映射并校验引用归属；
    - 若未提供映射（``spec`` 为 ``None``，或字典中无 ``resources``），
      则**拒绝执行**——宁可报错，也不在无法校验归属的情况下读取数据。

    只有「记录序列」这条路径完全不涉及引用，因而不受此要求约束。

    :return: ``(ref, resources)``，其中 ``resources`` 必非空。
    :raises RepresentError: 缺少分区映射，或引用不属于 E 分区。
    """
    if not isinstance(spec, Mapping):
        raise RepresentError(
            "只读 E 分区这一约束需要协议的分区映射才能校验：请把"
            " Module01.describe(...) 的产物（含 resources）作为 spec 传入。"
            f"当前 spec 类型为 {type(spec).__name__}，无法确认引用归属，故拒绝读取。"
        )

    raw = spec.get("resources")
    if not isinstance(raw, Mapping) or not raw:
        raise RepresentError(
            "spec.resources 缺失或为空：无法确认引用属于 E 分区，故拒绝读取。"
        )
    resources = {str(k): v for k, v in raw.items()}

    e_ref = resources.get("E")
    if not isinstance(e_ref, str) or not e_ref.strip():
        raise RepresentError("spec.resources 中缺少探索分区引用 'E'")

    ref = explicit if explicit is not None else e_ref
    _require_ref(ref)
    _guard_exploration_ref(ref, resources)
    return ref, resources


def _guard_exploration_ref(ref: str, resources: Mapping[str, Any]) -> None:
    """守卫：**只允许**读取探索分区 E 的引用。

    M1 的 ``read_dataset`` 放行 explorer 读 E 与 V（二者都是开发视图），
    但 P03 增量卡**严禁**访问 V 与确认侧 ``C*`` 引用。故此处不等 M1 拒绝，
    而是按协议描述中的分区映射**主动**判定归属：

    - 引用等于 ``resources["E"]`` → 放行；
    - 引用等于其他任何已登记分区（V / C1 / H_* 等）→ 拒绝；
    - 引用不在映射中 → 视为未知引用，同样拒绝（不猜测、不放行）。

    :raises RepresentError: 引用不属于探索分区。
    """
    e_ref = resources.get("E")
    if ref == e_ref:
        return
    for purpose in sorted(str(k) for k in resources):
        if purpose != "E" and resources.get(purpose) == ref:
            raise RepresentError(
                f"本阶段严禁读取 {purpose} 分区引用（只允许 E）；ref={ref!r}"
            )
    raise RepresentError(
        f"引用 {ref!r} 不在协议的分区映射中，视为越界引用；只允许读取 E 分区"
    )


def _module01_type() -> type | None:
    """取得 M1 客户端类型；M1 不可用时返回 ``None``。"""
    try:
        from sdl_m01 import Module01
    except Exception:  # noqa: BLE001 - M1 缺失不应阻断记录序列路径
        return None
    return Module01


# ---------------------------------------------------------------------------
# 叶子提取
# ---------------------------------------------------------------------------


def _extract_leaves(
    records: Sequence[Mapping[str, Any]],
    *,
    target_field: str,
    max_leaves: int,
) -> list[str]:
    """从记录中提取可作为叶子出现的变量名。

    规则：

    - 只取 ``values`` 字段下**数值型**的键；
    - 排除 ``target_field``（目标是待预测量，不作为构造输入）；
    - 只保留合法标识符形态的名字（避免表达式解析失败）；
    - 升序排序后截取前 ``max_leaves`` 个，保证顺序确定、可复现。

    :raises RepresentError: ``max_leaves`` 非法。
    """
    if isinstance(max_leaves, bool) or not isinstance(max_leaves, int) or max_leaves < 1:
        raise RepresentError(f"max_leaves 必须是正整数，实际得到：{max_leaves!r}")

    names: set[str] = set()
    for record in records:
        values = record.get("values")
        if not isinstance(values, Mapping):
            continue
        for name, value in values.items():
            if name == target_field:
                continue
            if not isinstance(name, str) or not name.isidentifier():
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            names.add(name)

    return sorted(names)[:max_leaves]


def _as_budget(budget: Budget | Mapping[str, Any] | None) -> Budget:
    """把多种形态的预算统一为 :class:`Budget`。"""
    if budget is None:
        return Budget()
    if isinstance(budget, Budget):
        return budget
    if isinstance(budget, Mapping):
        allowed = {"max_candidates", "max_nodes", "max_depth", "max_attempts"}
        unknown = set(budget) - allowed
        if unknown:
            raise RepresentError(f"预算中出现未知字段：{', '.join(sorted(unknown))}")
        try:
            return Budget(**{k: v for k, v in budget.items()})
        except TypeError as exc:
            raise RepresentError(f"预算字段非法：{exc}") from exc
    raise RepresentError(
        f"budget 必须是 Budget 或字段字典；实际得到：{type(budget).__name__}"
    )
