"""P17 交付物：自主取数执行器（模块 M9）。

本模块回答一个新的问题：**当循环说「下一轮该去看什么」时，谁真的去把数据取回来？**

在 M9 之前，这条链是断的：M7 产出的取证计划是**纯建议对象**
（``ACQUISITION_PLAN_EXECUTES_COLLECTION = False``，不执行采集），而端到端
主循环里的数据供应器是**硬编码**的——无论 M7 建议看什么，都机械地补一批
同样的数据。也就是说，「建议 → 采样」这一环没有闭环，系统并不会**自主**
决定去哪里取数。

M9 正是补上这一环的执行侧模块：

    建议（M7）──> 采样设计（M9）──> 取数（数据源）──> 入湖（M1 add_confirmation）

它的产物是**真的被取回来的数据**，而不是又一个建议。这与 M7 的分工是互补的：
M7 说「哪里最值得看」，M9 说「那就去取这些」。两层的边界因此可机检：
:data:`ACQUISITION_EXECUTES_COLLECTION` 恒为 ``False``（M7 不执行采集），
:data:`COLLECTION_EXECUTES_COLLECTION` 恒为 ``True``（M9 执行采集）。

三条设计纪律
------------

① **不替 M7 重排，也不替 M7 命名。**

   M7 的排序值是启发式比值（分歧 ÷ 成本），并已按该口径排好序。M9 **原样
   采信**这个顺序——只把计划里排在前面的建议翻译成采样请求，条目仍按 M7 的
   ``rank`` 排列。M9 既不重算分歧，也不引入假说概率或观测模型，因此**不会**
   把启发式升格成任何信息论结论。计划对象里的 ``ranking_basis`` 被转录到
   采样请求中，口径随结果对象一并输出。

② **「没采」与「采了为空」必须可区分。**

   沿用 M5/M6 的纪律（「没算」与「算出来是 0」不同）：本模块把二者记成不同
   的原因码，绝不用 0 条记录去冒充「取过了」：

   - :data:`REASON_NO_MAPPING`——该观测在采样目录里没有对应设计，**没采**；
   - :data:`REASON_NO_PLAN`——上游根本没有给出建议，**没采**；
   - :data:`REASON_BUDGET_EXHAUSTED`——预算用尽，**没采**；
   - :data:`REASON_SOURCE_EMPTY`——设计齐备、确实去取了，但数据源返回空，
     **采了为空**（这是一个有信息量的结论，不是失败）。

   每一类都单列在 ``skipped`` / ``collected`` 中如实上报，计数见
   ``reason_counts``。

③ **取数不授予任何证据等级。**

   本模块把数据交给 M1 登记，但**不判断**这些数据支持或反驳了什么——
   等级由 M8 机械判定（:data:`COLLECTION_GRANTS_EVIDENCE_GRADE` 恒为
   ``False``）。M9 也不自行执行统计检验（不计算 p 值），不是裁决者。

关于依赖方向
------------

M9 站在 M7 与 M8 之间：它**消费** M7 的计划（只读其输出结构，鸭子类型），
**交给** M1 登记（通过注入的 ``registrar``，同样是鸭子类型）。本模块在
**模块层不导入** ``sdl_m01``，也不直连任何存储——数据源的注入点与登记入口
都由调用方提供，因此 M9 的绝大部分既可单测，也不破坏既有的隔离纪律。
允许的模块层导入仅是与口径一致性相关的常量（来自 M7/M8，用于原样转录排序
依据与证据等级词表）。

唯一例外是端到端编排 :func:`run_autonomous_discovery`：它在**函数体内**惰性
导入 ``sdl_m01`` 与 ``sdl_pipeline.loop``（构造真实金库、复用 P16 的公开
主循环）。这是为了让本模块在导入期不拉入整个管道、保持可独立单测；即便
在该编排里，M9 也只是**调用**这两层的公开接口，自身的取数路径仍只经注入的
``source`` 与 ``registrar``。

主要入口
--------

===============================  ============================================
:class:`SamplingCatalogue`        观测标识 → 采样设计的映射表（可含缺省设计）。
:func:`plan_to_requests`          把 M7 计划翻译成采样请求（保持其排序）。
:func:`collect`                   驱动数据源取一条请求，产出带溯源的结果。
:class:`AutonomousCollector`      同时充当主循环的 sink 与 supplier，实现闭环。
:func:`extract_discovered_regularities`  从知识库版本中提取新发掘的规律。
:func:`run_autonomous_discovery`  端到端编排：自主取数 → 确证 → 规律清单。
===============================  ============================================

边界与已知限制
--------------

- 本模块**不**实现 P18 及以后的内容；不修改 ``sdl_m01/``；只依赖标准库。
- **采样目录由调用方提供**：M9 无法凭空知道某个观测标识对应现实里该怎么采样。
  没有目录时，本模块如实报 :data:`REASON_NO_MAPPING`，绝不臆造设计。
- :class:`SyntheticSource` 只是「可插拔数据源」的一个**合成实现**，用于让链路
  可运行、可验证；它生成的数据由已知公式给出，**不宣称任何现实含义**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from sdl_m07.acquisition import (
    ACQUISITION_VERSION,
    ACQUISITION_RANKING_IS_HEURISTIC,
    RANKING_BASIS_HEURISTIC,
)
from sdl_m08.archive import (
    GRADE_E0,
    GRADE_E1,
    GRADE_E2,
)

# ---------------------------------------------------------------------------
# 版本与边界声明
# ---------------------------------------------------------------------------

#: 本模块的口径版本。
COLLECTION_VERSION = "P17-v1.0"

#: 本模块是否**执行**数据采集。恒为 ``True``——这正是它相对 M7 的定位。
#:
#: M7 的 ``ACQUISITION_PLAN_EXECUTES_COLLECTION`` 恒为 ``False``（计划只是建议）；
#: 本层把它变成事实。两个常量互为补集，是「建议层 / 执行层」分工的机检锚点。
COLLECTION_EXECUTES_COLLECTION = True

#: 本模块是否授予证据等级。恒为 ``False``——取数不等于判定。
COLLECTION_GRANTS_EVIDENCE_GRADE = False

#: 本模块是否自行执行统计检验。恒为 ``False``——它不是裁决者。
COLLECTION_RUNS_STATISTICS = False

#: 本模块的边界说明（随结果对象一并输出）。
COLLECTION_BOUNDARY_NOTE = (
    "本层是 M7 取证建议的**执行侧**：把建议翻译成采样设计、驱动数据源取数、"
    "并交 M1 登记。本层不重排 M7 的启发式顺序、不授予证据等级、"
    "不执行统计检验、不判定任何结论的真假；数据是否构成证据由 M8 机械判定。"
)

#: 采样目录缺失时的口径说明。
CATALOGUE_REQUIRED_NOTE = (
    "采样目录由调用方提供：观测标识对应的现实采样方式（环境、批次、读数）"
    "不是本层能够推断的。目录中查不到、又没有缺省设计时，本层如实记为"
    "「没采」（no_mapping），绝不臆造一份设计去凑数。"
)

#: 「没采」与「采了为空」的区分口径。
EMPTY_VS_ABSENT_NOTE = (
    "「没采」与「采了为空」是两回事：前者是设计缺失/预算用尽/上游无建议，"
    "后者是设计齐备、确实驱动数据源取过、但对方返回了空。二者原因码不同、"
    "分列不同清单，绝不用 0 条记录冒充「取过了」。"
)

# ---------------------------------------------------------------------------
# 原因码（「没采」与「采了为空」的区分基石）
# ---------------------------------------------------------------------------

#: 设计齐备、已驱动数据源、且确实取回非空数据。
REASON_COLLECTED = "collected"
#: 设计齐备、已驱动数据源，但返回空——**采了为空**。
REASON_SOURCE_EMPTY = "source_empty"
#: 该观测在采样目录中没有对应设计——**没采**。
REASON_NO_MAPPING = "no_mapping"
#: 上游未给出任何建议计划——**没采**。
REASON_NO_PLAN = "no_plan"
#: 采集预算已用尽——**没采**。
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
#: 数据源取数过程中抛错，如实降级——**没采**。
REASON_SOURCE_FAILED = "source_failed"

#: 全部原因码（保序，供机检与展示）。
COLLECTION_REASONS: tuple[str, ...] = (
    REASON_COLLECTED,
    REASON_SOURCE_EMPTY,
    REASON_NO_MAPPING,
    REASON_NO_PLAN,
    REASON_BUDGET_EXHAUSTED,
    REASON_SOURCE_FAILED,
)

#: 「没采」的原因码集合（与「采了为空」互补）。
NOT_COLLECTED_REASONS: frozenset[str] = frozenset(
    {
        REASON_NO_MAPPING,
        REASON_NO_PLAN,
        REASON_BUDGET_EXHAUSTED,
        REASON_SOURCE_FAILED,
    }
)

#: 合成数据源的强制标注。
SYNTHETIC_SOURCE_NOTICE = (
    "本数据源是**合成实现**，用于让「自主取数 → 确证」链路可运行、可验证。"
    "它按已知公式生成数据，因此「取回了数据」只说明链路通畅，"
    "既不构成对任何现实规律的支持，也不构成因果关系。"
)

#: 本模块**不**提供的接口名（供测试做 AST 级边界检查）。
NOT_PROVIDED_BY_P17: tuple[str, ...] = (
    # 本模块不由自己生成取证建议（那是 M7 的职责）
    "divergence",
    "acquisition_plan",
    # 本模块不授予证据等级 / 不判定因果（那是 M8 的职责）
    "archive_round",
    "update_knowledge_version",
    "revise_or_retire",
    "grade_for",
    "assign_evidence_grade",
    # 本模块不执行统计检验 / 不计算 p 值
    "compute_p_value",
    "execute_family",
    "execute_statistical_test",
    # 本模块不自行实现端到端主循环（复用 P16 的公开入口）
    "run_loop",
    "main_loop",
)

#: 本模块**不应存在**的数据访问入口（验收纪律：无直连存储）。
#: 取数经注入的 ``source``，登记经注入的 ``registrar``——本模块自身不碰存储。
FORBIDDEN_ACCESS_NAMES: tuple[str, ...] = (
    "sqlite3",
    "connect",
    "read_dataset",
    "consume_confirmation",
    "bind_confirmation",
    "record_evaluation",
    "release_results",
    "archive_confirmation",
    "load_snapshot",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class CollectionError(ValueError):
    """本模块所有异常的基类（进而 ``ValueError``）。"""


class CollectionInputError(CollectionError):
    """输入形态非法：字段缺失、类型不对、数值非有限等。"""


class CollectionPolicyError(CollectionError):
    """违反本模块的策略约束（例如试图授予证据等级）。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """把冻结结构递归还原为可 JSON 序列化的普通结构。"""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8") if isinstance(value, bytes) else value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical(value: Any) -> str:
    """规范式 JSON 文本（排序键、紧凑分隔），用于稳定摘要。"""
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any, length: int = 16) -> str:
    """对任意结构取规范式摘要（默认前 16 位）。"""
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:length]


def _require_text(value: Any, label: str) -> str:
    """要求一个非空字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise CollectionInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    """要求一个整数（可选是否允许 0）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollectionInputError(f"{label} 必须是整数，实际得到：{value!r}")
    if allow_zero:
        if value < 0:
            raise CollectionInputError(f"{label} 不能为负，实际得到：{value}")
    elif value <= 0:
        raise CollectionInputError(f"{label} 必须为正，实际得到：{value}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """要求一个映射。"""
    if not isinstance(value, Mapping):
        raise CollectionInputError(f"{label} 必须是映射，实际得到：{type(value).__name__}")
    return value


def _require_callable(value: Any, label: str) -> Any:
    """要求一个可调用对象。"""
    if not callable(value):
        raise CollectionInputError(f"{label} 必须可调用，实际得到：{type(value).__name__}")
    return value


def _reject_level_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝任何试图让本层授予等级 / 判定结论的入参。"""
    banned = {
        "grade",
        "evidence_grade",
        "promote_to",
        "causal",
        "causal_grade",
        "status",
        "conclusion",
        "p_value",
        "verdict",
    }
    hit = sorted(key for key in kwargs if key in banned)
    if hit:
        raise CollectionPolicyError(
            f"{where} 不接受 {hit} 一类入参：本层只负责取数与登记，"
            "证据等级与结论真假由 M8 机械判定。" + COLLECTION_BOUNDARY_NOTE
        )


# ---------------------------------------------------------------------------
# 采样设计
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingRequest:
    """一条可执行的采样请求（由 M7 建议翻译而来）。

    :param observation_id: 对应的 M7 观测标识（原样保留）。
    :param rank: 该建议在 M7 计划中的序号（**原样转录，不重排**）。
    :param observation_rank: M7 计划里的启发式排序值（原样转录，可能为 ``None``）。
    :param design: 具体采样设计（环境、批次数、读数等），由采样目录给出。
    :param purpose: 本轮取证目的（原样转录）。
    :param near_boundary: 该观测是否接近适用边界（原样转录）。
    :param rationale: 入选依据（转录自 M7，附本层说明）。
    :param provenance: 溯源信息（计划摘要、目录来源等）。
    """

    observation_id: str
    rank: int
    observation_rank: float | None
    design: Mapping[str, Any]
    purpose: str
    near_boundary: bool
    rationale: str
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.observation_id, "SamplingRequest.observation_id")
        _require_int(self.rank, "SamplingRequest.rank", allow_zero=True)
        _require_mapping(self.design, "SamplingRequest.design")
        _require_text(self.purpose, "SamplingRequest.purpose")
        object.__setattr__(self, "design", MappingProxyType(dict(self.design)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    @property
    def batches(self) -> int:
        """设计中的批次数（缺省 0 表示未指定）。"""
        value = self.design.get("batches", 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    @property
    def readings(self) -> int:
        """设计中的每批读数（缺省 0 表示未指定）。"""
        value = self.design.get("readings", 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（含排序依据转录）。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "observation_id": self.observation_id,
            "rank": self.rank,
            "observation_rank": self.observation_rank,
            "design": _plain(self.design),
            "purpose": self.purpose,
            "near_boundary": self.near_boundary,
            "rationale": self.rationale,
            "provenance": _plain(self.provenance),
            "ranking_basis": RANKING_BASIS_HEURISTIC,
            "is_heuristic": ACQUISITION_RANKING_IS_HEURISTIC,
            "ranking_transcribed": True,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class SamplingCatalogue:
    """观测标识 → 采样设计的映射表（验收纪律：设计由调用方提供）。

    解析顺序（先具体后一般）：

    1. ``by_observation[observation_id]``——最具体，按观测标识精确匹配；
    2. ``by_purpose[item_purpose]``——按取证目的匹配（如所有 ``exploration`` 共用）；
    3. ``default_design``——兜底设计；
    4. 都没有 → 返回 ``None``，调用方据此记为**没采**（:data:`REASON_NO_MAPPING`）。

    :param by_observation: 精确映射。
    :param by_purpose: 按目的映射。
    :param default_design: 兜底设计（``None`` 表示没有兜底）。
    """

    def __init__(
        self,
        by_observation: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        by_purpose: Mapping[str, Mapping[str, Any]] | None = None,
        default_design: Mapping[str, Any] | None = None,
    ) -> None:
        self._by_observation = {
            str(key): MappingProxyType(dict(value))
            for key, value in (by_observation or {}).items()
        }
        self._by_purpose = {
            str(key): MappingProxyType(dict(value))
            for key, value in (by_purpose or {}).items()
        }
        self._default = (
            MappingProxyType(dict(default_design)) if default_design is not None else None
        )

    def resolve(self, observation_id: str, purpose: str = "") -> Mapping[str, Any] | None:
        """按解析顺序取设计；取不到返回 ``None``。"""
        found = self._by_observation.get(str(observation_id))
        if found is not None:
            return found
        found = self._by_purpose.get(str(purpose))
        if found is not None:
            return found
        return self._default

    @property
    def has_default(self) -> bool:
        """是否存在兜底设计。"""
        return self._default is not None

    @property
    def size(self) -> int:
        """精确映射的条目数。"""
        return len(self._by_observation)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（仅含**键名**，设计正文可能较大）。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "observation_ids": sorted(self._by_observation),
            "purposes": sorted(self._by_purpose),
            "has_default": self.has_default,
            "size": self.size,
            "note": CATALOGUE_REQUIRED_NOTE,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 数据源协议与合成实现
# ---------------------------------------------------------------------------


def plan_to_requests(
    plan: Mapping[str, Any] | None,
    catalogue: SamplingCatalogue | None = None,
    *,
    max_requests: int | None = None,
    **_rejected: Any,
) -> tuple[tuple[SamplingRequest, ...], tuple[dict, ...]]:
    """把 M7 的取证计划翻译成采样请求（**保持其启发式排序**）。

    :param plan: M7 的计划字典（``AcquisitionPlan.to_dict()``）。``None`` 或空
        计划表示上游没有给出建议。
    :param catalogue: 采样目录；``None`` 时全部观测记为**没采**。
    :param max_requests: 采样请求条数上限（``None`` 表示不设限）。超出部分
        记为 :data:`REASON_BUDGET_EXHAUSTED`。
    :return: ``(requests, skipped)``。``requests`` 按 M7 的 ``rank`` 保序；
        ``skipped`` 逐条给出没采/无法翻译的原因。
    :raises CollectionInputError: 计划形态非法。
    """
    _reject_level_kwargs(_rejected, "plan_to_requests")
    if max_requests is not None:
        _require_int(max_requests, "max_requests", allow_zero=True)

    if plan is None:
        return (), (
            {
                "observation_id": None,
                "reason": REASON_NO_PLAN,
                "detail": "上游未给出任何取证建议（plan 为 None）。",
            },
        )
    if not isinstance(plan, Mapping):
        raise CollectionInputError(f"plan 必须是映射或 None，实际得到：{type(plan).__name__}")

    items = plan.get("items")
    if items is None:
        # M7 在无法出计划时会给出 {"available": False, "reason": ...}。
        return (), (
            {
                "observation_id": None,
                "reason": REASON_NO_PLAN,
                "detail": str(plan.get("reason") or "计划不含 items，视为上游无建议。")[:200],
            },
        )
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise CollectionInputError(f"plan['items'] 必须是序列，实际得到：{type(items).__name__}")

    if catalogue is None:
        skipped_all = tuple(
            {
                "observation_id": str((item or {}).get("observation_id") or "") or None,
                "reason": REASON_NO_MAPPING,
                "detail": "未提供采样目录，无法把观测标识翻译成采样设计。",
            }
            for item in items
        )
        return (), skipped_all

    requests: list[SamplingRequest] = []
    skipped: list[dict] = []
    # 严格按 items 的现有顺序遍历——这就是「不替 M7 重排」的落点。
    for position, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            skipped.append(
                {
                    "observation_id": None,
                    "reason": REASON_NO_MAPPING,
                    "detail": f"计划第 {position} 项不是映射，无法翻译。",
                }
            )
            continue
        observation_id = str(raw.get("observation_id") or "")
        purpose = str(raw.get("purpose") or plan.get("purpose") or "")
        design = catalogue.resolve(observation_id, purpose)
        if design is None:
            skipped.append(
                {
                    "observation_id": observation_id or None,
                    "reason": REASON_NO_MAPPING,
                    "detail": "该观测在采样目录中没有匹配设计，且无兜底设计。",
                }
            )
            continue
        if max_requests is not None and len(requests) >= max_requests:
            skipped.append(
                {
                    "observation_id": observation_id or None,
                    "reason": REASON_BUDGET_EXHAUSTED,
                    "detail": f"采样请求数已达上限 {max_requests}。",
                }
            )
            continue
        rank_value = raw.get("rank")
        requests.append(
            SamplingRequest(
                observation_id=observation_id or f"obs-{position:04d}",
                rank=int(rank_value) if isinstance(rank_value, int) and not isinstance(rank_value, bool) else position,
                observation_rank=(
                    float(raw["heuristic_value"])
                    if isinstance(raw.get("heuristic_value"), (int, float))
                    and not isinstance(raw.get("heuristic_value"), bool)
                    else None
                ),
                design=design,
                purpose=purpose,
                near_boundary=bool(raw.get("near_boundary", False)),
                rationale=(
                    f"依 M7 第 {position} 位建议采样；"
                    f"M7 依据：{str(raw.get('rationale') or '（未附依据）')[:120]}"
                ),
                provenance={
                    "plan_digest": _digest(plan),
                    "plan_purpose": plan.get("purpose"),
                    "source": "M7 acquisition plan（原样转录，未重排）",
                },
            )
        )
    return tuple(requests), tuple(skipped)


#: 数据源协议：``source(request, round_index) -> Sequence[Mapping]``。
#:
#: 返回的记录序列必须是 M1 可登记的记录结构（含 ``record_id`` / ``group_ids`` /
#: ``values`` 等）。返回空序列表示「设计齐备但对方没有数据」——这会被记为
#: :data:`REASON_SOURCE_EMPTY`（**采了为空**），而不是失败。
SourceProtocol = Callable[..., Sequence[Mapping[str, Any]]]


class SyntheticSource:
    """一个**合成**数据源：按已知公式生成数据，用于让链路可运行、可验证。

    生成机制与 P16 合成任务一致（``Y = 2·(X1/X2) + 1``，且 ``X1·X2 = 2`` 守恒），
    但**每批使用新的批次号段**，以保证 M1 的「不得重复登记已见内容」闸门不被触发
    （重放同一批内容会被 M1 以内容级数据身份拒绝）。

    它只是一个**替身**：真实使用应替换为真正去采集数据的实现。
    """

    def __init__(
        self,
        *,
        environment: str = "lab-A",
        conserved: float = 2.0,
        slope: float = 2.0,
        intercept: float = 1.0,
        start_batch: int = 10_000,
        notice: str = SYNTHETIC_SOURCE_NOTICE,
    ) -> None:
        self.environment = _require_text(environment, "environment")
        self.conserved = float(conserved)
        self.slope = float(slope)
        self.intercept = float(intercept)
        self.start_batch = _require_int(start_batch, "start_batch", allow_zero=True)
        self.notice = notice
        self._cursor = self.start_batch

    def _allocate_start(self, batches: int) -> int:
        start = self._cursor
        self._cursor += max(batches, 1) + 1
        return start

    def __call__(self, request: SamplingRequest, round_index: int = 0) -> list[dict]:
        """按请求的采样设计生成一批**新的**合成记录。

        :param request: 采样请求（读取其中的 ``batches`` / ``readings`` /
            ``environment``）。
        :param round_index: 研究轮次，仅写入溯源字段。
        :return: 记录列表；设计未指定批次或读数时返回空列表（**采了为空**）。
        """
        if not isinstance(request, SamplingRequest):
            raise CollectionInputError("source 收到的必须是 SamplingRequest。")
        batches = request.batches
        readings = request.readings
        if batches <= 0 or readings <= 0:
            # 设计不完整 → 不臆造数量，如实返回空（采了为空）。
            return []
        environment = str(request.design.get("environment") or self.environment)
        start = self._allocate_start(batches)

        records: list[dict] = []
        for offset in range(batches):
            group = start + offset
            for reading in range(readings):
                base = 1.0 + 0.5 * group + 0.1 * reading
                x1 = base
                x2 = self.conserved / base
                y = self.slope * (x1 / x2) + self.intercept + 0.01 * reading
                records.append(
                    {
                        "record_id": f"m09-{round_index:02d}-{group:05d}-{reading}",
                        "group_ids": {"batch": f"m09-batch-{group:05d}"},
                        "environment": environment,
                        "event_time": "2026-01-01T10:00:00+00:00",
                        "available_time": "2026-01-01T11:00:00+00:00",
                        "values": {"X1": x1, "X2": x2, "Y": y},
                        "units": {"X1": "mol/L", "X2": "mol/L", "Y": "mol/(L*s)"},
                        "source": "synthetic://m09-autonomous-source",
                        "operator_annotation": f"private-m09-{group:05d}-{reading}",
                    }
                )
        return records


# ---------------------------------------------------------------------------
# 采集结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectedBatch:
    """一条采样请求的采集结果（无论取到与否，都逐条留痕）。

    :param observation_id: 对应观测标识。
    :param reason: 结果原因码（:data:`COLLECTION_REASONS` 之一）。
    :param collected: 是否确实取到了**非空**数据。
    :param record_count: 取回的记录条数。
    :param batches: 设计中的批次数。
    :param readings: 设计中的读数。
    :param environment: 采样环境。
    :param records_digest: 取回记录的内容摘要（未取到为 ``None``）。
    :param registered_ref: 登记后 M1 返回的数据引用（未登记为 ``None``）。
    :param error: 失败说明（仅 :data:`REASON_SOURCE_FAILED` 时非空）。
    """

    observation_id: str
    reason: str
    collected: bool
    record_count: int
    batches: int = 0
    readings: int = 0
    environment: str | None = None
    records_digest: str | None = None
    registered_ref: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.observation_id, "CollectedBatch.observation_id")
        if self.reason not in COLLECTION_REASONS:
            raise CollectionInputError(
                f"reason 取值 {self.reason!r} 不在合法原因码内（{list(COLLECTION_REASONS)}）。"
            )
        _require_int(self.record_count, "CollectedBatch.record_count", allow_zero=True)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "observation_id": self.observation_id,
            "reason": self.reason,
            "collected": self.collected,
            "record_count": self.record_count,
            "batches": self.batches,
            "readings": self.readings,
            "environment": self.environment,
            "records_digest": self.records_digest,
            "registered_ref": self.registered_ref,
            "error": self.error,
            "is_empty_collection": self.reason == REASON_SOURCE_EMPTY,
            "is_not_collected": self.reason in NOT_COLLECTED_REASONS,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CollectionOutcome:
    """一轮自主取数的完整产出。

    :param round_index: 研究轮次 :math:`t`。
    :param purpose: 本轮取证目的（转录自 M7 计划）。
    :param requests: 翻译得到的采样请求（按 M7 排序）。
    :param results: 逐请求的采集结果。
    :param skipped: 未进入采样阶段的条目（没采）及其原因。
    :param reason_counts: 各原因码的计数（把「没采」与「采了为空」分开）。
    :param source_notice: 数据源的自我声明（合成源会带强制标注）。
    :param notes: 口径说明元组。
    """

    round_index: int
    purpose: str
    requests: tuple[SamplingRequest, ...]
    results: tuple[CollectedBatch, ...]
    skipped: tuple[dict, ...] = ()
    source_notice: str | None = None
    notes: tuple[str, ...] = ()
    reason_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_int(self.round_index, "CollectionOutcome.round_index")
        counts = {reason: 0 for reason in COLLECTION_REASONS}
        for item in self.results:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        for item in self.skipped:
            reason = str(item.get("reason") or REASON_NO_MAPPING)
            counts[reason] = counts.get(reason, 0) + 1
        object.__setattr__(self, "reason_counts", MappingProxyType(counts))

    @property
    def collected_batches(self) -> tuple[CollectedBatch, ...]:
        """确实取到非空数据的结果。"""
        return tuple(item for item in self.results if item.collected)

    @property
    def empty_batches(self) -> tuple[CollectedBatch, ...]:
        """设计齐备但取回为空的结果（**采了为空**）。"""
        return tuple(item for item in self.results if item.reason == REASON_SOURCE_EMPTY)

    @property
    def not_collected(self) -> tuple[CollectedBatch, ...]:
        """未取到数据的结果（**没采**）。"""
        return tuple(item for item in self.results if item.reason in NOT_COLLECTED_REASONS)

    @property
    def registered_refs(self) -> tuple[str, ...]:
        """已登记的数据引用（保序）。"""
        return tuple(item.registered_ref for item in self.results if item.registered_ref)

    @property
    def total_records(self) -> int:
        """本轮取回的记录总条数。"""
        return sum(item.record_count for item in self.results)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（含「没采 vs 采了为空」的分列视图）。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "boundary_note": COLLECTION_BOUNDARY_NOTE,
            "empty_vs_absent_note": EMPTY_VS_ABSENT_NOTE,
            "round_index": self.round_index,
            "purpose": self.purpose,
            "requests": [item.to_dict() for item in self.requests],
            "results": [item.to_dict() for item in self.results],
            "skipped": [_plain(item) for item in self.skipped],
            "collected_batch_count": len(self.collected_batches),
            "empty_batch_count": len(self.empty_batches),
            "not_collected_count": len(self.not_collected),
            "total_records": self.total_records,
            "registered_refs": list(self.registered_refs),
            "reason_counts": dict(self.reason_counts),
            "executes_collection": COLLECTION_EXECUTES_COLLECTION,
            "grants_evidence_grade": COLLECTION_GRANTS_EVIDENCE_GRADE,
            "runs_statistics": COLLECTION_RUNS_STATISTICS,
            "source_notice": self.source_notice,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def render(self) -> str:
        """把本轮取数渲染成可读文本。"""
        lines = [
            f"第 {self.round_index} 轮自主取数：目的 {self.purpose}",
            f"  采样请求 {len(self.requests)} 条｜取到 {len(self.collected_batches)} 批｜"
            f"采了为空 {len(self.empty_batches)} 批｜没采 {len(self.not_collected)} 条",
            f"  取回记录合计 {self.total_records} 条，登记引用 {len(self.registered_refs)} 个",
        ]
        for item in self.results:
            lines.append(
                f"    [{item.reason}] {item.observation_id}｜"
                f"{item.record_count} 条｜环境 {item.environment}"
            )
        for item in self.skipped:
            lines.append(f"    [{item.get('reason')}] {item.get('observation_id')}｜{item.get('detail')}")
        return "\n".join(lines)


def collect(
    request: SamplingRequest,
    source: SourceProtocol,
    *,
    round_index: int = 1,
    **_rejected: Any,
) -> list[dict]:
    """驱动数据源取一条采样请求，返回原始记录（**不做登记**）。

    :param request: 采样请求。
    :param source: 数据源（可调用对象）。
    :param round_index: 研究轮次，透传给数据源做溯源。
    :return: 记录列表（可能为空——那表示**采了为空**）。
    :raises CollectionInputError: 请求或数据源形态非法。
    :raises CollectionError: 数据源抛错时向上冒泡，由调用方决定如何降级。
    """
    _reject_level_kwargs(_rejected, "collect")
    if not isinstance(request, SamplingRequest):
        raise CollectionInputError("request 必须是 SamplingRequest。")
    _require_callable(source, "source")
    try:
        result = source(request, round_index)
    except TypeError:
        # 兼容只接受单个参数的数据源实现。
        result = source(request)
    if result is None:
        return []
    if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
        raise CollectionInputError(
            f"数据源必须返回记录序列，实际得到：{type(result).__name__}"
        )
    return [dict(item) for item in result if isinstance(item, Mapping)]


# ---------------------------------------------------------------------------
# 自主取数执行器：同时充当主循环的 sink 与 supplier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionBudget:
    """自主取数的预算（启动前确定，过程中只读）。

    :param max_batches: 整个流程允许取回的批次数上限。
    :param max_requests_per_round: 每轮最多翻译成多少条采样请求。
    :param purpose_template: 登记的用途名模板（须产生 ``C<正整数>``）。
    """

    #: 缺省预算：足够让「自主取数」在多个轮次上发生（而非首轮即耗尽），
    #: 从而体现「建议 → 采样 → 再建议」的逐轮闭环。这只是原型取值，
    #: 不对任何真实研究的采样规模构成建议。
    max_batches: int = 32
    max_requests_per_round: int = 1
    purpose_template: str = "C{round_index}"

    def __post_init__(self) -> None:
        _require_int(self.max_batches, "max_batches")
        _require_int(self.max_requests_per_round, "max_requests_per_round")
        _require_text(self.purpose_template, "purpose_template")


class AutonomousCollector:
    """把 M7 的取证建议变成**真的取回来的数据**，并交给 M1 登记。

    这个对象同时扮演两个角色，从而闭合「建议 → 采样」的环：

    - **sink**：``observe(round_index, plan)`` 接收主循环每轮算出的 M7 计划；
    - **supplier**：``__call__(round_index, protocol_id)`` 在主循环需要新数据时，
      依据**上一轮**的计划取数、并经商定的登记入口入湖。

    于是「主循环说下一轮该看什么 → 下一轮真的去看那些」这一因果链成立。

    :param source: 数据源（``source(request, round_index) -> records``）。
    :param registrar: 登记入口（鸭子类型：``registrar.add_confirmation(protocol_id,
        records, purpose)``，例如以 custodian 角色构造的 M1 模块）。
    :param catalogue: 采样目录；``None`` 时全部观测记为**没采**。
    :param budget: 采集预算。
    :param fallback_design: 当上一轮没有可用建议时使用的兜底采样设计
        （``None`` 表示不兜底，如实记为 :data:`REASON_NO_PLAN`）。
    :param source_notice: 数据源的自我声明。
    """

    def __init__(
        self,
        *,
        source: SourceProtocol,
        registrar: Any,
        catalogue: SamplingCatalogue | None = None,
        budget: CollectionBudget | None = None,
        fallback_design: Mapping[str, Any] | None = None,
        source_notice: str | None = None,
    ) -> None:
        _require_callable(source, "source")
        if not hasattr(registrar, "add_confirmation") or not callable(
            getattr(registrar, "add_confirmation")
        ):
            raise CollectionInputError(
                "registrar 必须提供 add_confirmation(protocol_id, records, purpose)："
                "自主取回的数据只能经 M1 的公开接口登记。"
            )
        self.source = source
        self.registrar = registrar
        self.catalogue = catalogue
        self.budget = budget or CollectionBudget()
        self.fallback_design = (
            MappingProxyType(dict(fallback_design)) if fallback_design is not None else None
        )
        self.source_notice = source_notice or getattr(source, "notice", None)

        self._plans: dict[int, Mapping[str, Any]] = {}
        self._outcomes: list[CollectionOutcome] = []
        self._batches_used = 0
        self._purpose_counter = 0
        self._registered_purposes: set[str] = set()

    # -- 角色一：sink -----------------------------------------------------

    def observe(self, round_index: int, plan: Mapping[str, Any]) -> None:
        """接收主循环本轮算出的 M7 计划（只读转录，不改变任何状态判断）。"""
        self._plans[int(round_index)] = plan if isinstance(plan, Mapping) else {}

    # -- 角色二：supplier --------------------------------------------------

    def _next_purpose(self, round_index: int) -> str:
        """生成一个尚未使用的确认用途名（形如 ``C3``）。"""
        candidate_index = max(int(round_index), self._purpose_counter + 1)
        while True:
            name = self.budget.purpose_template.format(round_index=candidate_index)
            if name not in self._registered_purposes:
                self._purpose_counter = candidate_index
                return name
            candidate_index += 1

    def _register(self, protocol_id: str, records: Sequence[Mapping[str, Any]], purpose: str) -> str:
        """经登记入口入湖，返回数据引用。"""
        outcome = self.registrar.add_confirmation(protocol_id, list(records), purpose)
        self._registered_purposes.add(purpose)
        if isinstance(outcome, Mapping):
            return str(outcome.get("ref") or "")
        return ""

    def _plan_for_round(self, round_index: int) -> Mapping[str, Any] | None:
        """取驱动本轮采样的计划——即**上一轮**算出的建议。"""
        previous = int(round_index) - 1
        if previous in self._plans:
            return self._plans[previous]
        # 首轮之前没有「上一轮」：退化为无建议。
        return None

    def __call__(self, round_index: int, protocol_id: str) -> None:
        """主循环的 ``data_supplier`` 入口：按上一轮建议自主取数并登记。"""
        plan = self._plan_for_round(round_index)
        outcome = self.collect_round(round_index, protocol_id, plan=plan)
        self._outcomes.append(outcome)

    def collect_round(
        self,
        round_index: int,
        protocol_id: str,
        *,
        plan: Mapping[str, Any] | None,
    ) -> CollectionOutcome:
        """执行一轮取数（供 ``__call__`` 与测试直接调用）。

        :param round_index: 研究轮次。
        :param protocol_id: 协议标识（登记时使用）。
        :param plan: 驱动本轮的 M7 计划（``None`` 表示无建议）。
        :return: :class:`CollectionOutcome`。
        """
        _require_text(protocol_id, "protocol_id")
        purpose = "exploration"
        if isinstance(plan, Mapping) and plan.get("purpose"):
            purpose = str(plan["purpose"])

        remaining = max(self.budget.max_batches - self._batches_used, 0)
        per_round_cap = min(self.budget.max_requests_per_round, remaining)

        requests, skipped = plan_to_requests(
            plan, self.catalogue, max_requests=per_round_cap
        )

        # 无建议且提供兜底设计时，用兜底设计补一条（明确记为 plan 缺失后的兜底）。
        used_fallback = False
        if not requests and self.fallback_design is not None and per_round_cap > 0:
            requests = (
                SamplingRequest(
                    observation_id="fallback",
                    rank=0,
                    observation_rank=None,
                    design=self.fallback_design,
                    purpose=purpose,
                    near_boundary=False,
                    rationale="上游无可用建议，使用调用方声明的兜底采样设计。",
                    provenance={
                        "plan_digest": _digest(plan) if plan is not None else None,
                        "source": "fallback design（上游无建议时的兜底）",
                    },
                ),
            )
            used_fallback = True

        results: list[CollectedBatch] = []
        notes: list[str] = []
        for request in requests:
            batches = request.batches
            if self._batches_used + max(batches, 1) > self.budget.max_batches:
                results.append(
                    CollectedBatch(
                        observation_id=request.observation_id,
                        reason=REASON_BUDGET_EXHAUSTED,
                        collected=False,
                        record_count=0,
                        batches=batches,
                        readings=request.readings,
                        environment=str(request.design.get("environment") or "") or None,
                        error=f"批次预算已用尽（{self._batches_used}/{self.budget.max_batches}）。",
                    )
                )
                continue
            try:
                records = collect(request, self.source, round_index=round_index)
            except Exception as exc:  # 数据源失败必须降级，不得中断主循环
                results.append(
                    CollectedBatch(
                        observation_id=request.observation_id,
                        reason=REASON_SOURCE_FAILED,
                        collected=False,
                        record_count=0,
                        batches=batches,
                        readings=request.readings,
                        environment=str(request.design.get("environment") or "") or None,
                        error=f"数据源取数失败（{type(exc).__name__}）：{str(exc)[:160]}",
                    )
                )
                continue

            if not records:
                # 设计齐备、确实去取了、但对方返回空——**采了为空**，不是失败。
                results.append(
                    CollectedBatch(
                        observation_id=request.observation_id,
                        reason=REASON_SOURCE_EMPTY,
                        collected=False,
                        record_count=0,
                        batches=batches,
                        readings=request.readings,
                        environment=str(request.design.get("environment") or "") or None,
                    )
                )
                continue

            environment = str(request.design.get("environment") or "") or None
            try:
                ref = self._register(protocol_id, records, self._next_purpose(round_index))
            except Exception as exc:
                results.append(
                    CollectedBatch(
                        observation_id=request.observation_id,
                        reason=REASON_SOURCE_FAILED,
                        collected=False,
                        record_count=0,
                        batches=batches,
                        readings=request.readings,
                        environment=environment,
                        error=f"登记失败（{type(exc).__name__}）：{str(exc)[:160]}",
                    )
                )
                continue

            self._batches_used += max(batches, 1)
            results.append(
                CollectedBatch(
                    observation_id=request.observation_id,
                    reason=REASON_COLLECTED,
                    collected=True,
                    record_count=len(records),
                    batches=batches,
                    readings=request.readings,
                    environment=environment,
                    records_digest=_digest(records),
                    registered_ref=ref or None,
                )
            )

        if used_fallback:
            notes.append("上游无可用建议，本轮改用调用方声明的兜底采样设计。")
        if not requests and not used_fallback:
            notes.append("本轮无可执行的采样请求（上游无建议或目录缺设计），如实记为没采。")
        notes.append(EMPTY_VS_ABSENT_NOTE)

        return CollectionOutcome(
            round_index=int(round_index),
            purpose=purpose,
            requests=tuple(requests),
            results=tuple(results),
            skipped=tuple(skipped),
            source_notice=self.source_notice,
            notes=tuple(notes),
        )

    # -- 视图 -------------------------------------------------------------

    @property
    def outcomes(self) -> tuple[CollectionOutcome, ...]:
        """历次取数结果（按轮次）。"""
        return tuple(self._outcomes)

    @property
    def batches_used(self) -> int:
        """已消耗的批次预算。"""
        return self._batches_used

    def observed_plans(self) -> Mapping[int, Mapping[str, Any]]:
        """已收到的 M7 计划（只读视图）。"""
        return MappingProxyType(dict(self._plans))

    def trace(self) -> dict:
        """取数决策溯源：把「建议 → 采样」的因果链留痕。"""
        rounds: list[dict] = []
        for outcome in self._outcomes:
            plan = self._plans.get(outcome.round_index - 1)
            rounds.append(
                {
                    "round_index": outcome.round_index,
                    "driven_by_plan_round": outcome.round_index - 1,
                    "plan_digest": _digest(plan) if plan is not None else None,
                    "plan_item_ids": [
                        str(item.get("observation_id"))
                        for item in (plan or {}).get("items", [])
                        if isinstance(item, Mapping)
                    ],
                    "requests": [item.observation_id for item in outcome.requests],
                    "reason_counts": dict(outcome.reason_counts),
                    "registered_refs": list(outcome.registered_refs),
                }
            )
        return {
            "collection_version": COLLECTION_VERSION,
            "boundary_note": COLLECTION_BOUNDARY_NOTE,
            "rounds": rounds,
            "batches_used": self._batches_used,
            "budget": {
                "max_batches": self.budget.max_batches,
                "max_requests_per_round": self.budget.max_requests_per_round,
            },
            "registered_purposes": sorted(self._registered_purposes),
        }

    def to_dict(self) -> dict:
        """把历次取数结果序列化为 JSON 兼容字典。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "outcomes": [item.to_dict() for item in self._outcomes],
            "round_count": len(self._outcomes),
            "batches_used": self._batches_used,
            "catalogue": self.catalogue.to_dict() if self.catalogue else None,
            "source_notice": self.source_notice,
            "boundary_note": COLLECTION_BOUNDARY_NOTE,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 规律提取
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Regularity:
    """一条**被发掘的规律**：达到确证级或给出明确反驳的假说条目。

    :param hypothesis_id: 假说标识。
    :param hypothesis_version: 被确证的版本号。
    :param grade: 证据等级（``E0`` / ``E1`` / ``E2``）。
    :param status: 结论（``supported`` / ``refuted`` / ``inconclusive`` / ``failed``）。
    :param grade_basis: 等级判定凭据（转录自 M8，原样采信）。
    :param round_index: 首次达到该结论的研究轮次。
    :param environments: 本轮数据出现过的环境。
    :param record_count: 本轮记录条数。
    :param is_new: 相对起始知识库，该条目是否为本轮**新增**。
    """

    hypothesis_id: str
    hypothesis_version: str
    grade: str
    status: str
    grade_basis: str
    round_index: int
    environments: tuple[str, ...] = ()
    record_count: int = 0
    is_new: bool = True

    @property
    def is_confirmatory(self) -> bool:
        """是否确证级（``E1`` / ``E2``）。"""
        return self.grade in (GRADE_E1, GRADE_E2)

    @property
    def relation(self) -> str:
        """规律的类型标签（支持 / 反驳 / 未定）。"""
        if self.status == "supported" and self.is_confirmatory:
            return "supported_regularity"
        if self.status == "refuted":
            return "refuted_regularity"
        return "undetermined"

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_version": self.hypothesis_version,
            "grade": self.grade,
            "status": self.status,
            "grade_basis": self.grade_basis,
            "round_index": self.round_index,
            "environments": list(self.environments),
            "record_count": self.record_count,
            "is_new": self.is_new,
            "is_confirmatory": self.is_confirmatory,
            "relation": self.relation,
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegularityReport:
    """从知识库版本中提取的规律清单。

    :param regularities: 全部规律条目。
    :param prior_identities: 起始知识库中已存在的条目身份（用于判定 ``is_new``）。
    :param knowledge_version: 产出该清单的知识库版本号。
    :param boundary_note: 边界说明。
    """

    regularities: tuple[Regularity, ...]
    prior_identities: tuple[str, ...] = ()
    knowledge_version: str | None = None
    boundary_note: str = COLLECTION_BOUNDARY_NOTE

    @property
    def supported(self) -> tuple[Regularity, ...]:
        """被支持的确证级规律（真正的「新规律」）。"""
        return tuple(item for item in self.regularities if item.relation == "supported_regularity")

    @property
    def refuted(self) -> tuple[Regularity, ...]:
        """被反驳的规律（同样是信息量）。"""
        return tuple(item for item in self.regularities if item.relation == "refuted_regularity")

    @property
    def new_supported(self) -> tuple[Regularity, ...]:
        """本轮**新增**的、被支持的确证级规律。"""
        return tuple(item for item in self.supported if item.is_new)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "collection_version": COLLECTION_VERSION,
            "boundary_note": self.boundary_note,
            "knowledge_version": self.knowledge_version,
            "count": len(self.regularities),
            "supported_count": len(self.supported),
            "refuted_count": len(self.refuted),
            "new_supported_count": len(self.new_supported),
            "regularities": [item.to_dict() for item in self.regularities],
            "prior_identities": list(self.prior_identities),
        }

    def canonical_json(self) -> str:
        """规范式 JSON 文本。"""
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def render(self) -> str:
        """把规律清单渲染成可读文本。"""
        if not self.regularities:
            return "未发掘到任何达到确证级的规律（这不等于「规律不存在」）。"
        lines = [
            f"发掘到的规律清单（知识库版本 {self.knowledge_version}）：",
            f"  支持 {len(self.supported)} 条（其中本轮新增 {len(self.new_supported)} 条）｜"
            f"反驳 {len(self.refuted)} 条",
        ]
        for index, item in enumerate(self.regularities, start=1):
            tag = "新增" if item.is_new else "既有"
            lines.append(
                f"  {index}. [{item.relation}｜{item.grade}｜{tag}] "
                f"{item.hypothesis_id}@{item.hypothesis_version}（第 {item.round_index} 轮）"
            )
        return "\n".join(lines)


def _entry_identity(entry: Any) -> str:
    """证据条目的稳定身份（用于判定「相对起始知识库是否新增」）。"""
    return "|".join(
        [
            str(getattr(entry, "hypothesis_id", "")),
            str(getattr(entry, "hypothesis_version", "")),
            str(getattr(entry, "round_index", "")),
            str(getattr(entry, "evidence_identity", "")),
        ]
    )


def extract_discovered_regularities(
    knowledge: Any,
    *,
    prior_knowledge: Any | None = None,
    **_rejected: Any,
) -> RegularityReport:
    """从知识库版本中提取「发掘到的规律」。

    只把**结论明确**的条目算作规律：``supported`` 且确证级（``E1``/``E2``），
    或 ``refuted``。``inconclusive`` / ``E0`` 的条目不算规律——它们恰恰是
    「还没看清」的部分，会出现在主循环的「仍缺证据的问题清单」里。

    :param knowledge: 终止时的知识库版本（M8 的 ``KnowledgeVersion``）。
    :param prior_knowledge: 起始知识库版本；给出时用于判定哪些规律是**新增**的。
    :return: :class:`RegularityReport`。
    """
    _reject_level_kwargs(_rejected, "extract_discovered_regularities")
    entries = list(getattr(knowledge, "entries", ()) or ())
    prior_identities: set[str] = set()
    if prior_knowledge is not None:
        prior_identities = {_entry_identity(item) for item in getattr(prior_knowledge, "entries", ()) or ()}
    prior_tuple = tuple(sorted(prior_identities))

    found: list[Regularity] = []
    for entry in entries:
        grade = str(getattr(entry, "grade", GRADE_E0))
        status = str(getattr(entry, "status", ""))
        is_confirmatory = grade in (GRADE_E1, GRADE_E2)
        if not (status == "refuted" or (status == "supported" and is_confirmatory)):
            continue
        identity = _entry_identity(entry)
        found.append(
            Regularity(
                hypothesis_id=str(getattr(entry, "hypothesis_id", "")),
                hypothesis_version=str(getattr(entry, "hypothesis_version", "")),
                grade=grade,
                status=status,
                grade_basis=str(getattr(entry, "grade_basis", "")),
                round_index=int(getattr(entry, "round_index", 0) or 0),
                environments=tuple(getattr(entry, "environments", ()) or ()),
                record_count=int(getattr(entry, "record_count", 0) or 0),
                is_new=identity not in prior_identities,
            )
        )
    return RegularityReport(
        regularities=tuple(found),
        prior_identities=prior_tuple,
        knowledge_version=getattr(knowledge, "version", None),
    )


# ---------------------------------------------------------------------------
# 端到端编排
# ---------------------------------------------------------------------------


def run_autonomous_discovery(
    output: str | None = None,
    *,
    budget: Any | None = None,
    collection_budget: CollectionBudget | None = None,
    groups: int = 60,
    source: SourceProtocol | None = None,
    catalogue: SamplingCatalogue | None = None,
    recorded_at: str = "2026-01-01T00:00:00+00:00",
    **_rejected: Any,
) -> dict:
    """端到端跑通「自主取数 → 确证 → 规律清单」。

    与 P16 的 ``run_demo`` 的区别在于**数据供应器不再是硬编码的**：

    - P16：无论 M7 建议什么都不变，机械补一批同样的数据；
    - P17：**M7 建议看什么，下一轮就去取什么**——由 :class:`AutonomousCollector`
      同时充当主循环的 sink（收建议）与 supplier（按建议取数）实现闭环。

    :param output: 可选的落盘目录；``None`` 表示只在内存中返回结果。
    :param budget: 主循环预算（``sdl_pipeline.loop.LoopBudget``）；``None`` 用缺省。
    :param collection_budget: 自主取数预算。
    :param groups: 初始登记的合成批次数。
    :param source: 数据源；``None`` 使用 :class:`SyntheticSource`。
    :param catalogue: 采样目录；``None`` 时用「所有观测共用一份兜底设计」。
    :param recorded_at: 归档时间戳（确定性复现用）。
    :return: 结果字典，含 ``regularities`` / ``collection`` / ``archive`` 等键。
    """
    _reject_level_kwargs(_rejected, "run_autonomous_discovery")

    # 延迟导入，避免 M9 在导入期就拉入整个管道（也让本模块可独立单测）。
    from sdl_m01 import Module01, initialize
    from sdl_pipeline.loop import (
        LoopBudget,
        run_loop,
        synthetic_batch_evaluator,
        synthetic_records,
        synthetic_spec,
    )

    if catalogue is None:
        # 缺省目录：所有观测共用一份兜底设计（每轮取 8 个新批次 × 2 读数）。
        catalogue = SamplingCatalogue(
            default_design={
                "environment": "lab-A",
                "batches": 8,
                "readings": 2,
            }
        )
    if source is None:
        source = SyntheticSource()

    from pathlib import Path

    directory = Path(output) if output else None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        db_path = directory / "evidence.sqlite3"
        if db_path.exists():
            db_path.unlink()
    else:
        import tempfile

        _tmp = tempfile.mkdtemp(prefix="m09-")
        db_path = Path(_tmp) / "evidence.sqlite3"

    tokens = initialize(str(db_path))
    custodian = Module01(str(db_path), tokens["custodian"])
    confirmer = Module01(str(db_path), tokens["confirmer"])
    explorer = Module01(str(db_path), tokens["explorer"])

    protocol = custodian.build(synthetic_spec(), synthetic_records(groups=groups))

    collector = AutonomousCollector(
        source=source,
        registrar=custodian,
        catalogue=catalogue,
        budget=collection_budget or CollectionBudget(),
        fallback_design={"environment": "lab-A", "batches": 8, "readings": 2},
    )

    from sdl_m08.archive import empty_knowledge

    starting_knowledge = empty_knowledge()

    archive = run_loop(
        custodian=custodian,
        confirmer=confirmer,
        explorer=explorer,
        protocol=protocol,
        target_field="Y",
        budget=budget or LoopBudget(),
        counterexample_expressions=("X1*X2",),
        data_supplier=collector,
        acquisition_sink=collector.observe,
        confirmation_evaluator=synthetic_batch_evaluator,
        recorded_at=recorded_at,
        knowledge=starting_knowledge,
    )

    report = extract_discovered_regularities(
        archive.knowledge, prior_knowledge=starting_knowledge
    )

    payload = {
        "collection_version": COLLECTION_VERSION,
        "boundary_note": COLLECTION_BOUNDARY_NOTE,
        "regularities": report.to_dict(),
        "collection": collector.to_dict(),
        "collection_trace": collector.trace(),
        "archive": archive.to_dict(),
        "output": str(directory) if directory else None,
    }

    if directory is not None:
        _write_json(directory / "regularities.json", payload["regularities"])
        _write_json(directory / "collection.json", payload["collection"])
        _write_json(directory / "collection-trace.json", payload["collection_trace"])
        _write_json(directory / "discovery-archive.json", payload["archive"])
        (directory / "说明.md").write_text(_autonomous_readme(report, collector), encoding="utf-8")

    return payload


def _write_json(path: Any, value: Any) -> None:
    """以 UTF-8 写入 JSON（确定性键序）。"""
    path.write_text(
        json.dumps(_plain(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _autonomous_readme(report: RegularityReport, collector: AutonomousCollector) -> str:
    """生成自主发现的中文说明（含合成验证标注与边界声明）。"""
    lines = [
        "# P17 自主取数 · 合成验证产物",
        "",
        f"**合成验证标注**：{SYNTHETIC_SOURCE_NOTICE}",
        "",
        "## 本目录内容",
        "",
        "- `regularities.json`：从知识库版本中提取的**规律清单**。",
        "- `collection.json`：逐轮自主取数结果（含「没采」与「采了为空」的分列）。",
        "- `collection-trace.json`：取数决策溯源（建议 → 采样的因果链）。",
        "- `discovery-archive.json`：主循环的完整发现档案。",
        "",
        "## 规律清单",
        "",
        report.render(),
        "",
        "## 取数概况",
        "",
        f"- 取数轮次：{len(collector.outcomes)}",
        f"- 已用批次预算：{collector.batches_used}",
        "",
        "## 边界声明",
        "",
        COLLECTION_BOUNDARY_NOTE,
        "",
        EMPTY_VS_ABSENT_NOTE,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 边界自检
# ---------------------------------------------------------------------------


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查三项：

    1. 模块是否定义了 :data:`NOT_PROVIDED_BY_P17` 中的任何名字（越界实现他层职责）；
    2. 模块是否出现 :data:`FORBIDDEN_ACCESS_NAMES` 中的属性访问或导入
       （绕过注入的 source/registrar 直连存储）；
    3. 模块是否声明了会授予等级或结论的常量（防「取数即判定」）。

    刻意用 AST 而不是源码文本匹配：本模块的 docstring 与常量里**必须**写出
    ``read_dataset`` / ``grade_for`` 等被禁名字（那是边界声明），
    文本匹配会把边界声明本身判成违规。

    :return: ``{"defined_other_stages", "accessed_forbidden", "ok", "checked_names"}``。
    """
    import ast as _ast
    import inspect
    import pathlib

    source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
    tree = _ast.parse(source_path.read_text(encoding="utf-8"))

    defined = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
    }
    defined |= {
        target.id
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Assign)
        for target in node.targets
        if isinstance(target, _ast.Name)
    }

    accessed: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, _ast.Name):
            accessed.add(node.id)
        elif isinstance(node, _ast.alias):
            accessed.add(node.name.split(".")[-1])

    defined_other_stages = tuple(sorted(defined & set(NOT_PROVIDED_BY_P17)))
    accessed_forbidden = tuple(sorted(accessed & set(FORBIDDEN_ACCESS_NAMES)))
    grants_grade = bool(COLLECTION_GRANTS_EVIDENCE_GRADE)
    runs_stats = bool(COLLECTION_RUNS_STATISTICS)
    return {
        "defined_other_stages": defined_other_stages,
        "accessed_forbidden": accessed_forbidden,
        "grants_evidence_grade": grants_grade,
        "runs_statistics": runs_stats,
        "executes_collection": bool(COLLECTION_EXECUTES_COLLECTION),
        "ok": not (
            defined_other_stages or accessed_forbidden or grants_grade or runs_stats
        ),
        "checked_names": len(NOT_PROVIDED_BY_P17) + len(FORBIDDEN_ACCESS_NAMES),
    }


__all__ = [
    "COLLECTION_VERSION",
    "COLLECTION_EXECUTES_COLLECTION",
    "COLLECTION_GRANTS_EVIDENCE_GRADE",
    "COLLECTION_RUNS_STATISTICS",
    "COLLECTION_BOUNDARY_NOTE",
    "CATALOGUE_REQUIRED_NOTE",
    "EMPTY_VS_ABSENT_NOTE",
    "SYNTHETIC_SOURCE_NOTICE",
    "COLLECTION_REASONS",
    "NOT_COLLECTED_REASONS",
    "REASON_COLLECTED",
    "REASON_SOURCE_EMPTY",
    "REASON_NO_MAPPING",
    "REASON_NO_PLAN",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_SOURCE_FAILED",
    "NOT_PROVIDED_BY_P17",
    "FORBIDDEN_ACCESS_NAMES",
    "CollectionError",
    "CollectionInputError",
    "CollectionPolicyError",
    "CollectionBudget",
    "SamplingRequest",
    "SamplingCatalogue",
    "SyntheticSource",
    "CollectedBatch",
    "CollectionOutcome",
    "AutonomousCollector",
    "Regularity",
    "RegularityReport",
    "plan_to_requests",
    "collect",
    "extract_discovered_regularities",
    "run_autonomous_discovery",
    "self_check",
]
