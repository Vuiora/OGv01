"""P18 交付物：分析决策层 / 工具派发器（模块 M10）。

本模块回答的问题是：**《SDL算法框架说明》§7 说「LLM 辅助提出表示、候选解释、
竞争假说、可执行代码草案以及取证建议」——这句话由谁落地？**

在本模块之前，M2–M9 是一批**确定性算法**，但它们只被 ``sdl_pipeline.loop``
以**硬编码顺序**串起来（§5 伪代码写死在函数体里）。LLM 唯一的位置是 M4 的
``text_enricher``——而且只能填三个文本字段。换言之，LLM 是**文字润色器**，
不是**分析主体**：它不参与「先算什么、再算什么、拿哪个候选去拟合」这类决策。

M10 把这一层补上：

    算法层（M2–M9）──> 工具箱（M10 登记与派发）──> LLM 决定调用顺序

M10 **不新增任何统计能力**（:data:`TOOLBOX_ADDS_ALGORITHMS` 恒为 ``False``）：
它只把 M2–M9 的既有公开接口登记成**可被 LLM 调用的工具**，由 LLM 自主决定
编排顺序，算法层负责执行与校验。这既贴合 §7 的原意，又不破坏任何已有闸门。

三条设计纪律
------------

① **只登记与派发，不替 LLM 决策。**

   M10 不含任何「搜索策略」「启发式排序」「默认调用顺序」——
   :data:`TOOLBOX_DECIDES_NOT_LLM` 恒为 ``False``。工具是**被动**的：
   给什么参数就调什么，然后把结果原样交回。编排智能属于 LLM。

② **富对象只能经「会话句柄」引用，不能由 LLM 伪造。**

   算法层多数函数接收富对象（``Candidate`` / ``ExplorationSample`` /
   ``KnowledgeBase`` / ``Evaluation``），无法经 JSON 直接传参。M10 为每个
   产出对象分配**不透明会话句柄**（形如 ``h_<hex>``）。这带来两个保障：

   - LLM **不必也无法**构造算法层内部对象，只能凭句柄复用真实产出；
   - 句柄仅在本会话内有效，形如 M1 的 ``historical_ref``，是**不透明数据引用**，
     **不带类型前缀语义**，不可解析、不可跨会话传递。

③ **不授予任何证据等级，也不做统计裁决。**

   等级由 M8 机械判定，p 值由 M6 的外部执行器计算。
   :data:`TOOLBOX_GRANTS_EVIDENCE_GRADE` 与 :data:`TOOLBOX_RUNS_STATISTICS`
   恒为 ``False``。任何试图把「等级 / 结论 / p 值」当作**入参**塞进工具的调用，
   都会被 :data:`FORBIDDEN_TOOL_ARGUMENTS` 守卫直接拒绝。

关于依赖方向与 P18 的范围
--------------------------

M10 站在**算法层之上**：它导入并包装 M2/M3/M5/M8/M9 的公开函数，但**不导入
``sdl_m01``**——对探索分区 E 的读取只能经**注入的** ``explorer`` 客户端完成
（令牌不进 LLM 上下文，也不进本模块的成员变量之外的地方）。

本阶段（P18）**只登记探索侧工具**（M2 表示、M3 拟合/结构、M5 指标、M9 规律
提取）。确证侧编排（冻结 → 消费 → 执行 → 归档）仍由 P16 的 ``run_loop`` 承担：
那条路径受冻结协议与跨轮误差预算约束，不适合交给 LLM 自由调度。这是**有意的
范围切分**，不是遗漏——符合本项目「一轮一个最小增量」的施工纪律。

主要入口
--------

================================  ==========================================
:class:`HandleStore`               会话句柄表（富对象 → 不透明引用）。
:class:`ToolSpec`                  单个工具的描述（含与真实签名一致的参数 schema）。
:class:`ToolResult`                一次工具调用的结果（含状态与原因码）。
:class:`Toolbox`                   工具注册表：登记、列出、描述、派发。
:func:`build_default_toolbox`      构造登记了全部探索侧工具的默认工具箱。
:func:`self_check`                 对自身源码做 AST 级边界自检。
================================  ==========================================

边界与已知限制
--------------

- 本模块**不**实现 P19 及以后的内容；不修改 ``sdl_m01/``；只依赖标准库。
- M10 **不做**任何统计判断：结果里的 ``summary`` 只是**结构化转录**，不含
  「是否成立」的结论。判定权在 M6/M8。
- 会话句柄是本模块的**运行期**概念，不落盘、不可序列化传递；跨进程编排应重新
  调用工具产出新句柄。
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from sdl_m02.domain import Candidate, enumerate_candidates
from sdl_m02.expressions import (
    depth as expression_depth,
    node_count as expression_node_count,
    parse as parse_expression,
    variables as expression_variables,
)
from sdl_m03.equations import (
    baseline_constant,
    baseline_linear,
    fit_relation,
    gain_against,
    prepare_sample,
    render_expression,
)
from sdl_m05.metrics import complexity as complexity_metric
from sdl_m05.metrics import novelty as novelty_metric
from sdl_m05.metrics import KnowledgeBase as MetricsKnowledgeBase
from sdl_m05.metrics import KnowledgeEntry
from sdl_m08.archive import (
    ARCHIVE_SEALED_KEYS,
    empty_knowledge,
    grade_rank,
    highest_grade,
)
from sdl_m09.collection import extract_discovered_regularities

# ---------------------------------------------------------------------------
# 版本与边界声明
# ---------------------------------------------------------------------------

#: 本模块的口径版本。
TOOLBOX_VERSION = "P18-v1.0"

#: 本模块是否**替 LLM 决策**。恒为 ``False``——决策权属于 LLM。
#:
#: 工具是纯被动的：给什么参数调什么，不含默认编排顺序、不含搜索策略。
#: 与之互补的是算法层的确定性：M10 不新增任何统计能力
#: （见 :data:`TOOLBOX_ADDS_ALGORITHMS`）。
TOOLBOX_DECIDES_NOT_LLM = False

#: 本模块是否**新增统计能力**。恒为 ``False``——它只登记与派发既有接口。
TOOLBOX_ADDS_ALGORITHMS = False

#: 本模块是否**授予证据等级**。恒为 ``False``——等级由 M8 机械判定。
TOOLBOX_GRANTS_EVIDENCE_GRADE = False

#: 本模块是否**执行统计检验 / 计算 p 值**。恒为 ``False``——由 M6 外部执行器负责。
TOOLBOX_RUNS_STATISTICS = False

#: 本阶段登记的工具是否**仅限探索侧**。恒为 ``True``。
#:
#: 确证侧编排（冻结 → 消费 → 执行 → 归档）仍由 P16 的 ``run_loop`` 承担，
#: 它受冻结协议与跨轮误差预算约束，不交给 LLM 自由调度。
TOOLBOX_EXPLORATION_ONLY = True

#: 本层的边界声明（随结果对象输出，供复核）。
TOOLBOX_BOUNDARY_NOTE = (
    "M10 是分析决策层：它把 M2–M9 的既有公开接口登记为 LLM 可调用的工具，"
    "由 LLM 决定调用顺序，算法层负责执行与校验。M10 不新增统计能力、"
    "不授予证据等级、不执行统计检验，也不含任何默认编排顺序。"
)

#: 会话句柄的语义说明（类比 M1 的 ``historical_ref``）。
HANDLE_SEMANTICS_NOTE = (
    "会话句柄是不透明数据引用（形如 h_<hex>）：LLM 只能凭句柄复用真实产出，"
    "既不必也无法构造算法层内部对象。句柄仅在本会话内有效，不带类型前缀语义，"
    "不可解析、不可跨会话传递。"
)

#: 被禁止的**工具入参名**。任何试图把「等级 / 结论 / p 值」当作入参塞进工具的
#: 调用都会被直接拒绝——文本或参数都不得用来授予证据等级。
FORBIDDEN_TOOL_ARGUMENTS: frozenset[str] = frozenset({
    "grade",
    "evidence_grade",
    "evidence_level",
    "promote_to",
    "causal",
    "causal_grade",
    "conclusion",
    "verdict",
    "p_value",
    "pvalue",
    "is_true",
    "truth",
    "supported",
    "refuted",
})

#: 本模块**不得定义**的名字（那些属算法层与裁决层职责）。
#:
#: 注意：本模块会**调用**其中部分能力所对应的公开接口（如 ``prepare_sample``），
#: 但绝**不重新实现**它们——因此自检用 AST 比对**定义名**，而非源码文本。
NOT_PROVIDED_BY_P18: tuple[str, ...] = (
    "compute_p_value",
    "holm_adjust",
    "classify_result",
    "grade_for",
    "bind_confirmation",
    "consume_confirmation",
    "record_evaluation",
    "archive_round",
    "update_knowledge_version",
    "revise_or_retire",
    "run_loop",
)

#: 本模块**不得**访问或导入的名字（绕过注入客户端直连存储或确证侧）。
FORBIDDEN_ACCESS_NAMES: frozenset[str] = frozenset({
    "sqlite3",
    "connect",
    "consume_confirmation",
    "bind_confirmation",
    "record_evaluation",
    "archive_confirmation",
    "release_results",
    "mark_compromised",
})

#: 工具调用结果的三种状态。
STATUS_OK = "ok"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

TOOL_RESULT_STATUSES: tuple[str, ...] = (STATUS_OK, STATUS_REJECTED, STATUS_FAILED)

#: 原因码。
REASON_OK = "ok"
REASON_UNKNOWN_TOOL = "unknown_tool"
REASON_FORBIDDEN_ARGUMENT = "forbidden_argument"
REASON_BAD_HANDLE = "bad_handle"
REASON_MISSING_ARGUMENT = "missing_argument"
REASON_UNEXPECTED_ARGUMENT = "unexpected_argument"
REASON_SEALED_LEAK = "sealed_leak"
REASON_TOOL_RAISED = "tool_raised"

TOOLBOX_REASONS: tuple[str, ...] = (
    REASON_OK,
    REASON_UNKNOWN_TOOL,
    REASON_FORBIDDEN_ARGUMENT,
    REASON_BAD_HANDLE,
    REASON_MISSING_ARGUMENT,
    REASON_UNEXPECTED_ARGUMENT,
    REASON_SEALED_LEAK,
    REASON_TOOL_RAISED,
)

#: 句柄前缀。刻意与 M1 的 ``data_`` / ``H_`` 均不同，避免语义混淆。
HANDLE_PREFIX = "h_"

#: 注入的 explorer 客户端在句柄表中登记的保留句柄。
#:
#: 刻意用一个**不占用普通句柄序号**的固定名，使句柄表既能被 LLM 查询，
#: 又不会把客户端本身当作可复用产物暴露出去。
_EXPLORER_HANDLE = "client:explorer"

#: 结果摘要中**允许**出现的键名——即便它们命中 M8 的封存键名黑名单。
#:
#: 这是 M10 相对 M8 的**必要校准**。M8 的 :data:`ARCHIVE_SEALED_KEYS` 是为
#: **归档对象**设计的：在归档语境下 ``source`` / ``source_ref`` 指「原始资料的
#: 定位信息」，必须拦下。但算法层结果的 ``source`` 是**另一个意思**——
#: 例如 M5 ``complexity`` 的 ``source`` 是「复杂度近似的依据说明」，
#: 属正常的口径标注。直接复用 M8 的黑名单会把这类合法输出误判为泄漏。
#:
#: 因此 M10 只拦截**确定的**封存正文特征（记录正文、快照、质量报告），
#: 对本表中列出的键名放行。**校准方向是「宁窄勿宽」**：漏报由 M8 自己的
#: 出闸闸门兜底（本层产出的知识库对象最终仍要经 M8 归档），误报则会直接
#: 使合法工具不可用。
LEAK_GUARD_EXEMPT_KEYS: frozenset[str] = frozenset({
    "source",  # 算法层常以 source 标注口径依据，非原始资料定位
    "source_ref",  # 同上：M3 的 sample.source_ref 指向 E 分区，非封存内容
})

#: M10 实际用于出闸检查的封存键名 = M8 黑名单 − 本层豁免。
M10_SEALED_KEYS: frozenset[str] = frozenset(
    key for key in ARCHIVE_SEALED_KEYS if key.lower() not in LEAK_GUARD_EXEMPT_KEYS
)



# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ToolboxError(ValueError):
    """本模块所有异常的基类。"""


class ToolboxInputError(ToolboxError):
    """调用方传入的输入不合法（缺参、类型错、句柄无效等）。"""


class ToolboxPolicyError(ToolboxError):
    """调用触碰了本层的策略边界（例如试图授予证据等级）。"""


class UnknownToolError(ToolboxInputError):
    """请求的工具未登记。"""


class HandleError(ToolboxInputError):
    """会话句柄无效、已过期或类型不符。"""


# ---------------------------------------------------------------------------
# 私有工具
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """把值转成 JSON 安全的普通结构（供摘要输出）。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _plain(value.to_dict())
        except Exception:  # pragma: no cover - 防御性
            return repr(value)
    return repr(value)


def _digest(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# 会话句柄
# ---------------------------------------------------------------------------


class HandleStore:
    """会话句柄表：把算法层的富对象映射为不透明引用。

    句柄形如 ``h_<hex>``，由「类型名 + 序号 + 实例盐」派生，因此**可复现**
    （同一会话、同一顺序必得同一句柄），但不可从外部反推对象内容。
    """

    def __init__(self, *, limit: int = 512, salt: str = "") -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ToolboxInputError("limit 必须是正整数")
        self._limit = int(limit)
        self._salt = str(salt)
        self._objects: dict[str, Any] = {}
        self._kinds: dict[str, str] = {}
        self._reserved: set[str] = set()
        self._counter = 0

    # -- 写入 ---------------------------------------------------------------

    def put(self, kind: str, value: Any) -> str:
        """登记一个富对象，返回其不透明句柄。

        :raises ToolboxInputError: ``kind`` 为空，或已超出容量上限。
        """
        if not isinstance(kind, str) or not kind.strip():
            raise ToolboxInputError("kind 必须是非空字符串")
        if len(self._objects) >= self._limit:
            raise ToolboxInputError(
                f"会话句柄已达上限 {self._limit}；请收敛分析规模或开新会话"
            )
        self._counter += 1
        handle = HANDLE_PREFIX + _digest(f"{kind}:{self._counter}:{self._salt}", 16)
        self._objects[handle] = value
        self._kinds[handle] = kind
        return handle

    # -- 读写（保留名） -----------------------------------------------------

    def put_kind(self, kind: str, fixed_handle: str, value: Any) -> str:
        """以**指定句柄名**登记对象（供注入客户端等保留资源使用）。

        与 :meth:`put` 的区别：句柄名由调用方给出，且**不占用**序号——
        因此这类句柄不会被计入 :meth:`kinds` 的普通产物统计，也不会出现在
        :meth:`to_dict` 的摘要里（避免把客户端当作可复用产物暴露给 LLM）。
        """
        if not isinstance(fixed_handle, str) or not fixed_handle:
            raise ToolboxInputError("fixed_handle 必须是非空字符串")
        if fixed_handle in self._reserved:
            raise ToolboxInputError(f"保留句柄已存在：{fixed_handle}")
        self._objects[fixed_handle] = value
        self._kinds[fixed_handle] = kind
        self._reserved.add(fixed_handle)
        return fixed_handle

    # -- 读取 ---------------------------------------------------------------

    def get(self, handle: Any, *, expect: str | None = None) -> Any:
        """按句柄取回对象。

        :param expect: 期望的类型名；不符即拒绝（防止把句柄用错工具）。
        :raises HandleError: 句柄无效或类型不符。
        """
        if not isinstance(handle, str) or not handle:
            raise HandleError(f"不是有效的会话句柄：{handle!r}")
        if handle not in self._objects:
            raise HandleError(f"句柄不存在或已过期：{handle!r}")
        kind = self._kinds[handle]
        if expect is not None and kind != expect:
            raise HandleError(
                f"句柄类型不符：期望 {expect}，实为 {kind}（{handle}）"
            )
        return self._objects[handle]

    def kind_of(self, handle: Any) -> str:
        """返回句柄对应的类型名。"""
        self.get(handle)
        return self._kinds[str(handle)]

    def has(self, handle: Any) -> bool:
        """该句柄是否有效。"""
        return isinstance(handle, str) and handle in self._objects

    # -- 元信息 -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._objects)

    def kinds(self) -> dict[str, int]:
        """各类型名下的句柄数量（不含保留资源）。"""
        counts: dict[str, int] = {}
        for handle, kind in self._kinds.items():
            if handle in self._reserved:
                continue
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """句柄表的**摘要**（不暴露任何对象内容）。"""
        return {
            "count": len(self._objects) - len(self._reserved),
            "kinds": self.kinds(),
            "limit": self._limit,
            "note": HANDLE_SEMANTICS_NOTE,
        }


# ---------------------------------------------------------------------------
# 工具描述
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """一个工具参数的描述。"""

    name: str
    type: str
    required: bool
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": _plain(self.default) if self.required is False else None,
        }


@dataclass(frozen=True)
class ToolSpec:
    """一个可被 LLM 调用的工具的描述。"""

    name: str
    module: str
    summary: str
    parameters: tuple[ParamSpec, ...]
    returns: str
    requires_handles: tuple[str, ...] = ()
    produces_handle: bool = False
    wrapped: str = ""
    note: str = ""

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "module": self.module,
            "summary": self.summary,
            "parameters": [p.to_dict() for p in self.parameters],
            "returns": self.returns,
            "requires_handles": list(self.requires_handles),
            "produces_handle": self.produces_handle,
            "wrapped": self.wrapped,
            "note": self.note,
        }


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。

    ``summary`` **只是结构化转录**，不含「是否成立」的结论——判定权在 M6/M8。
    """

    tool: str
    status: str
    handle: str | None = None
    summary: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in TOOL_RESULT_STATUSES:
            raise ToolboxInputError(f"未知的结果状态：{self.status!r}")
        if self.reason is not None and self.reason not in TOOLBOX_REASONS:
            raise ToolboxInputError(f"未知的原因码：{self.reason!r}")
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "handle": self.handle,
            "summary": _plain(self.summary),
            "reason": self.reason,
        }

    def canonical_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def content_digest(self) -> str:
        return _digest(self.canonical_json())


# ---------------------------------------------------------------------------
# 工具箱
# ---------------------------------------------------------------------------


class Toolbox:
    """工具注册表：登记、列出、描述、派发。

    **本类不含任何默认调用顺序**——它不替 LLM 决策，只被动派发。
    """

    def __init__(self, *, context: Mapping[str, Any] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._impls: dict[str, Callable[[Mapping[str, Any], HandleStore], ToolResult]] = {}
        self._context: dict[str, Any] = dict(context or {})
        self._handles = HandleStore()
        self._journal: list[dict[str, Any]] = []

    # -- 注册 ---------------------------------------------------------------

    def register(
        self,
        spec: ToolSpec,
        impl: Callable[[Mapping[str, Any], HandleStore], ToolResult],
    ) -> None:
        """登记一个工具。重名即拒绝（避免静默覆盖）。"""
        if not isinstance(spec, ToolSpec):
            raise ToolboxInputError("spec 必须是 ToolSpec 实例")
        if not callable(impl):
            raise ToolboxInputError("impl 必须可调用")
        if spec.name in self._specs:
            raise ToolboxInputError(f"工具名重复：{spec.name}")
        self._specs[spec.name] = spec
        self._impls[spec.name] = impl

    # -- 查询 ---------------------------------------------------------------

    @property
    def handles(self) -> HandleStore:
        """会话句柄表。"""
        return self._handles

    def list_tools(self) -> tuple[ToolSpec, ...]:
        """列出全部工具（按名称排序，保证输出确定）。"""
        return tuple(self._specs[name] for name in sorted(self._specs))

    def names(self) -> tuple[str, ...]:
        """全部工具名。"""
        return tuple(sorted(self._specs))

    def describe(self, name: str) -> ToolSpec:
        """取单个工具的描述。

        :raises UnknownToolError: 工具未登记。
        """
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownToolError(f"未登记的工具：{name!r}")
        return spec

    def catalogue(self) -> dict[str, Any]:
        """工具的完整目录（供 LLM 选择工具）。"""
        return {
            "version": TOOLBOX_VERSION,
            "exploration_only": bool(TOOLBOX_EXPLORATION_ONLY),
            "decides_not_llm": bool(TOOLBOX_DECIDES_NOT_LLM),
            "adds_algorithms": bool(TOOLBOX_ADDS_ALGORITHMS),
            "tools": [spec.to_dict() for spec in self.list_tools()],
            "boundary": TOOLBOX_BOUNDARY_NOTE,
            "handles": HANDLE_SEMANTICS_NOTE,
        }

    # -- 派发 ---------------------------------------------------------------

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        """调用一个工具。

        派发前依次做四项守卫：

        1. 工具是否存在；
        2. 入参名是否命中 :data:`FORBIDDEN_TOOL_ARGUMENTS`（越权即拒）；
        3. 必填参数是否齐备、是否有未声明的多余参数；
        4. 结果是否夹带确证分区内容（:func:`check_no_sealed_leak`）。

        :return: :class:`ToolResult`；**任何失败都返回对象而非抛出**，
            使 LLM 能读到原因码并自行改道。
        """
        spec = self._specs.get(name)
        if spec is None:
            return self._record(
                ToolResult(name, STATUS_REJECTED, None, {"known": self.names()}, REASON_UNKNOWN_TOOL)
            )

        # -- 守卫 1：禁止把等级 / 结论 / p 值当作入参 --------------------
        forbidden = sorted(set(kwargs) & FORBIDDEN_TOOL_ARGUMENTS)
        if forbidden:
            return self._record(
                ToolResult(
                    name,
                    STATUS_REJECTED,
                    None,
                    {"forbidden_arguments": forbidden, "boundary": TOOLBOX_BOUNDARY_NOTE},
                    REASON_FORBIDDEN_ARGUMENT,
                )
            )

        # -- 守卫 2：参数齐备性与多寡 ------------------------------------
        allowed = set(spec.param_names)
        unexpected = sorted(set(kwargs) - allowed)
        if unexpected:
            return self._record(
                ToolResult(
                    name,
                    STATUS_REJECTED,
                    None,
                    {"unexpected_arguments": unexpected, "accepted": sorted(allowed)},
                    REASON_UNEXPECTED_ARGUMENT,
                )
            )
        missing = sorted(
            p.name for p in spec.parameters if p.required and p.name not in kwargs
        )
        if missing:
            return self._record(
                ToolResult(
                    name,
                    STATUS_REJECTED,
                    None,
                    {"missing_arguments": missing, "accepted": sorted(allowed)},
                    REASON_MISSING_ARGUMENT,
                )
            )

        # -- 执行 ---------------------------------------------------------
        impl = self._impls[name]
        try:
            result = impl(dict(kwargs), self._handles)
        except HandleError as exc:
            return self._record(
                ToolResult(name, STATUS_REJECTED, None, {"error": str(exc)}, REASON_BAD_HANDLE)
            )
        except ToolboxPolicyError as exc:
            return self._record(
                ToolResult(name, STATUS_REJECTED, None, {"error": str(exc)}, REASON_FORBIDDEN_ARGUMENT)
            )
        except Exception as exc:  # 算法层自身报错 -> 如实降级
            return self._record(
                ToolResult(
                    name,
                    STATUS_FAILED,
                    None,
                    {"error": f"{type(exc).__name__}: {exc}"},
                    REASON_TOOL_RAISED,
                )
            )

        # -- 守卫 3：结果不得夹带确证分区内容 ----------------------------
        leaked = _find_leaked_key(dict(result.summary))
        if leaked is not None:
            return self._record(
                ToolResult(
                    name,
                    STATUS_REJECTED,
                    None,
                    {
                        "leaked_key": leaked,
                        "error": f"结果含封存特征键名 {leaked!r}，已拦截",
                    },
                    REASON_SEALED_LEAK,
                )
            )

        return self._record(result)

    # -- 审计 ---------------------------------------------------------------

    def _record(self, result: ToolResult) -> ToolResult:
        self._journal.append(result.to_dict())
        return result

    def trace(self) -> tuple[dict[str, Any], ...]:
        """本会话的工具调用轨迹（按时间顺序）。"""
        return tuple(self._journal)

    def to_dict(self) -> dict[str, Any]:
        """工具箱的状态摘要。"""
        return {
            "version": TOOLBOX_VERSION,
            "tool_count": len(self._specs),
            "tools": list(self.names()),
            "handles": self._handles.to_dict(),
            "calls": len(self._journal),
            "exploration_only": bool(TOOLBOX_EXPLORATION_ONLY),
        }

    def canonical_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def content_digest(self) -> str:
        return _digest(self.canonical_json())


# ---------------------------------------------------------------------------
# 工具实现（模块级，便于 AST 自检与单测）
# ---------------------------------------------------------------------------


def _spec_params(func: Callable[..., Any]) -> tuple[ParamSpec, ...]:
    """从真实函数签名派生参数 schema。

    这是「schema 与真实签名一致」的**唯一来源**——不手工抄写，避免漂移。
    """
    sig = inspect.signature(func)
    out: list[ParamSpec] = []
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        required = p.default is p.empty
        default = None if required else p.default
        ann = p.annotation
        type_name = ann if isinstance(ann, str) else getattr(ann, "__name__", "any")
        out.append(ParamSpec(p.name, type_name, required, default))
    return tuple(out)


def _require_handle(store: HandleStore, value: Any, expect: str, label: str) -> Any:
    """把句柄解析成对象；非句柄值直接拒绝（防止伪造内部对象）。

    只接受形如 ``h_<hex>`` 的**LLM 可见句柄**——保留资源句柄（如注入的
    explorer 客户端）**不能**经此路径取用，避免 LLM 越权触达客户端本身。
    """
    if not isinstance(value, str) or not value.startswith(HANDLE_PREFIX):
        raise HandleError(
            f"{label} 必须是会话句柄（{HANDLE_PREFIX}<hex>），"
            f"实际得到 {type(value).__name__}；不得直接构造算法层对象"
        )
    return store.get(value, expect=expect)


def _find_leaked_key(value: Any, *, found: list[str] | None = None) -> str | None:
    """深度扫描工具结果，返回首个命中的封存键名（无则 ``None``）。

    与 M8 的 ``check_no_sealed_leak`` 的区别在于**黑名单已校准**：
    见 :data:`LEAK_GUARD_EXEMPT_KEYS`。只扫键名不扫字符串值——键名是可穷举的
    结构边界，而中文说明里出现「values」之类的词属正常。
    """
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if isinstance(key, str) and key.lower() in M10_SEALED_KEYS:
                    return key
                stack.append(item)
        elif isinstance(current, (list, tuple, set, frozenset)):
            stack.extend(current)
    return None


def _impl_parse_expression(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    ast = parse_expression(
        str(args["text"]),
        max_depth=int(args.get("max_depth", 3)),
        max_nodes=int(args.get("max_nodes", 25)),
    )
    handle = store.put("Ast", ast)
    return ToolResult(
        "m02.parse_expression",
        STATUS_OK,
        handle,
        {
            "rendered": render_expression(ast),
            "variables": sorted(expression_variables(ast)),
            "node_count": expression_node_count(ast),
            "depth": expression_depth(ast),
        },
    )


def _impl_enumerate_candidates(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    variables = args["variables"]
    if isinstance(variables, (str, bytes)) or not isinstance(variables, Sequence):
        raise ToolboxInputError("variables 必须是变量名序列")
    candidates = enumerate_candidates(
        list(variables),
        max_depth=int(args.get("max_depth", 3)),
        max_nodes=int(args.get("max_nodes", 25)),
        max_candidates=int(args.get("max_candidates", 1000)),
    )
    handle = store.put("CandidateList", candidates)
    rendered = [render_expression(c.ast) for c in candidates[:20]]
    return ToolResult(
        "m02.enumerate_candidates",
        STATUS_OK,
        handle,
        {
            "count": len(candidates),
            "sample_expressions": rendered,
            "max_depth": max((c.depth for c in candidates), default=0),
        },
    )


def _impl_candidate_summary(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    candidates = _require_handle(store, args["candidates"], "CandidateList", "candidates")
    depths: dict[str, int] = {}
    for c in candidates:
        depths[str(c.depth)] = depths.get(str(c.depth), 0) + 1
    return ToolResult(
        "m02.candidate_summary",
        STATUS_OK,
        None,
        {
            "count": len(candidates),
            "depth_histogram": depths,
            "sample_expressions": [render_expression(c.ast) for c in candidates[:20]],
        },
    )


def _impl_prepare_sample(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    """经**注入的** explorer 读取 E 分区，制备 M3 的探索样本。

    这是 M3 系列工具的前提：``fit_relation`` / ``baseline_*`` 都要一个
    ``ExplorationSample``。数据访问只经注入客户端，令牌不进 LLM 上下文。
    """
    explorer = store.get(_EXPLORER_HANDLE, expect="ExplorerClient")
    variables = args["variables"]
    if isinstance(variables, (str, bytes)) or not isinstance(variables, Sequence):
        raise ToolboxInputError("variables 必须是变量名序列")
    target = str(args["target"])
    purpose = str(args.get("purpose", "E"))
    resources = explorer.describe(str(args["protocol_id"])).get("resources", {})
    ref = resources.get(purpose)
    if not ref:
        raise ToolboxPolicyError(
            f"协议中没有可读分区 {purpose!r}；可用分区：{sorted(resources)}"
        )
    rows = explorer.read_dataset(ref)
    sample = prepare_sample(
        list(rows), list(variables), target, purpose=purpose, source_ref=str(ref)
    )
    handle = store.put("ExplorationSample", sample)
    return ToolResult(
        "m03.prepare_sample",
        STATUS_OK,
        handle,
        {
            "row_count": len(sample.rows),
            "variables": list(sample.variables),
            "target": sample.target,
            "purpose": sample.purpose,
            "partition_ref": str(ref),
        },
    )


def _impl_fit_relation(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    sample = _require_handle(store, args["sample"], "ExplorationSample", "sample")
    relationship_arg = args["relationship"]
    #: M3 的 ``fit_relation`` 接受 ``Candidate`` / AST 元组 / 表达式文本。
    #: 本层据此接受两类句柄（Candidate 或 Ast），或直接给表达式文本。
    if isinstance(relationship_arg, str) and relationship_arg.startswith(HANDLE_PREFIX):
        kind = store.kind_of(relationship_arg)
        if kind == "Candidate":
            relationship: Any = store.get(relationship_arg, expect="Candidate")
        elif kind == "Ast":
            relationship = store.get(relationship_arg, expect="Ast")
        else:
            raise HandleError(
                f"relationship 句柄类型应为 Candidate 或 Ast，实为 {kind}"
            )
    elif isinstance(relationship_arg, str):
        relationship = relationship_arg
    else:
        raise HandleError("relationship 必须是会话句柄或表达式文本")
    fit = fit_relation(sample, relationship)
    handle = store.put("RelationFit", fit)
    return ToolResult(
        "m03.fit_relation",
        STATUS_OK,
        handle,
        {
            "kind": getattr(fit.candidate, "kind", None),
            "expression": render_expression(getattr(fit.candidate, "expression", None))
            if getattr(fit.candidate, "expression", None) is not None
            else None,
            "metrics": _plain(getattr(fit.candidate, "metrics", {})),
        },
    )


def _impl_baseline_constant(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    sample = _require_handle(store, args["sample"], "ExplorationSample", "sample")
    fit = baseline_constant(sample)
    handle = store.put("FitResult", fit)
    return ToolResult(
        "m03.baseline_constant",
        STATUS_OK,
        handle,
        {"name": fit.name, "kind": fit.kind, "metrics": _plain(fit.metrics)},
    )


def _impl_baseline_linear(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    sample = _require_handle(store, args["sample"], "ExplorationSample", "sample")
    fit = baseline_linear(sample, str(args["feature"]))
    handle = store.put("FitResult", fit)
    return ToolResult(
        "m03.baseline_linear",
        STATUS_OK,
        handle,
        {"name": fit.name, "kind": fit.kind, "metrics": _plain(fit.metrics)},
    )


def _as_fit_result(store: HandleStore, value: Any, label: str) -> Any:
    """把句柄解析成 ``FitResult``；``RelationFit`` 自动取其 ``candidate``。"""
    kind = store.kind_of(value) if isinstance(value, str) and value.startswith(HANDLE_PREFIX) else None
    if kind == "FitResult":
        return store.get(value, expect="FitResult")
    if kind == "RelationFit":
        relation = store.get(value, expect="RelationFit")
        return relation.candidate
    raise HandleError(
        f"{label} 应为 FitResult 或 RelationFit 句柄，实为 {kind or type(value).__name__}"
    )


def _impl_gain_against(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    candidate = _as_fit_result(store, args["candidate"], "candidate")
    baseline = _as_fit_result(store, args["baseline"], "baseline")
    gain = gain_against(candidate, baseline, loss=str(args.get("loss", "mse")))
    return ToolResult("m03.gain_against", STATUS_OK, None, _plain(gain))


def _impl_complexity(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    ast = _require_handle(store, args["ast"], "Ast", "ast")
    result = complexity_metric(ast=ast)
    return ToolResult("m05.complexity", STATUS_OK, None, _plain(result))


def _impl_novelty(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    knowledge = _require_handle(store, args["knowledge_base"], "MetricsKnowledgeBase", "knowledge_base")
    #: 假说的最小形态要求 id / version / statement（见 M5 ``_hypothesis_fields``）。
    hypothesis = {
        "id": str(args["hypothesis_id"]),
        "version": str(args["hypothesis_version"]),
        "statement": str(args["statement"]),
    }
    #: ``expression`` 接受 M2 的 AST 句柄，解包成**元组**再交给 M5。
    expression_arg = args.get("expression")
    expression: Any = None
    if isinstance(expression_arg, str) and expression_arg.startswith(HANDLE_PREFIX):
        expression = store.get(expression_arg, expect="Ast")
    elif isinstance(expression_arg, str):
        expression = expression_arg
    elif expression_arg is not None:
        raise HandleError("expression 必须是 AST 会话句柄、表达式文本或省略")
    result = novelty_metric(hypothesis, knowledge, expression=expression)
    return ToolResult("m05.novelty", STATUS_OK, None, _plain(result))


def _impl_knowledge_base(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    """构造 M5 口径的知识库（``KnowledgeBase``），供新颖性使用。

    这是**转录**：只把「陈述」作为已知项，不携带任何等级信息（M5 不接触等级）。
    """
    version = str(args["version"])
    raw_entries = args.get("entries") or ()
    if isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Sequence):
        raise ToolboxInputError("entries 必须是「陈述字符串」序列")
    entries = tuple(
        KnowledgeEntry(
            entry_id=f"K-{version}-{index:04d}",
            statement=str(item),
            kind="known",
        )
        for index, item in enumerate(raw_entries)
    )
    knowledge = MetricsKnowledgeBase(version=version, entries=entries)
    handle = store.put("MetricsKnowledgeBase", knowledge)
    return ToolResult(
        "m05.knowledge_base",
        STATUS_OK,
        handle,
        {
            "version": knowledge.version,
            "entry_count": len(knowledge),
            "is_empty": knowledge.is_empty,
        },
    )


def _impl_empty_knowledge(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    version = str(args.get("version", "K0"))
    knowledge = empty_knowledge(version)
    handle = store.put("KnowledgeVersion", knowledge)
    return ToolResult(
        "m08.empty_knowledge",
        STATUS_OK,
        handle,
        {"version": knowledge.version, "entry_count": len(knowledge.entries)},
    )


def _impl_knowledge_entries(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    knowledge = _require_handle(store, args["knowledge"], "KnowledgeVersion", "knowledge")
    entries = list(getattr(knowledge, "entries", ()) or ())
    grades = [
        getattr(e, "grade", None)
        for e in entries
        if getattr(e, "grade", None) is not None
    ]
    return ToolResult(
        "m08.knowledge_summary",
        STATUS_OK,
        None,
        {
            "version": knowledge.version,
            "entry_count": len(entries),
            "highest_grade": highest_grade(grades) if grades else None,
            "grade_rank_of_highest": grade_rank(highest_grade(grades)) if grades else None,
            "note": "等级为原样转录，本层不判定任何等级。",
        },
    )


def _impl_extract_regularities(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    knowledge = _require_handle(store, args["knowledge"], "KnowledgeVersion", "knowledge")
    report = extract_discovered_regularities(knowledge)
    return ToolResult(
        "m09.extract_regularities",
        STATUS_OK,
        None,
        {
            "supported": len(getattr(report, "supported", ()) or ()),
            "refuted": len(getattr(report, "refuted", ()) or ()),
            "new_supported": len(getattr(report, "new_supported", ()) or ()),
            "note": "本层只做转录；是否成立由 M8 机械判定。",
        },
    )


def _impl_render_expression(args: Mapping[str, Any], store: HandleStore) -> ToolResult:
    ast = _require_handle(store, args["ast"], "Ast", "ast")
    return ToolResult(
        "m02.render_expression",
        STATUS_OK,
        None,
        {"rendered": render_expression(ast)},
    )


# ---------------------------------------------------------------------------
# 默认工具箱
# ---------------------------------------------------------------------------


def build_default_toolbox(*, explorer: Any = None, seed: int = 0) -> Toolbox:
    """构造登记了全部探索侧工具的默认工具箱。

    :param explorer: 以 explorer 角色构造的 M1 客户端（**可选**）。它只被
        用于读取探索分区 E 与开发评估分区 V；令牌不进 LLM 上下文。
    :param seed: 会话盐，用于派生可复现的会话句柄。
    :return: 配置完毕的 :class:`Toolbox`。
    """
    box = Toolbox(context={"explorer": explorer})
    box._handles = HandleStore(salt=str(seed))
    if explorer is not None:
        # 把客户端本身登记进句柄表（保留名不占普通序号），
        # 使 prepare_sample 能经 store 取回它——但 LLM 拿不到它的句柄。
        box._handles.put_kind("ExplorerClient", _EXPLORER_HANDLE, explorer)

    specs: list[tuple[ToolSpec, Callable[[Mapping[str, Any], HandleStore], ToolResult]]] = [
        (
            ToolSpec(
                name="m02.parse_expression",
                module="M2",
                summary="把表达式文本解析为规范化 AST（受限语法，深度与节点数受限）。",
                parameters=_spec_params(parse_expression),
                returns="Ast",
                produces_handle=True,
                wrapped="sdl_m02.expressions.parse",
            ),
            _impl_parse_expression,
        ),
        (
            ToolSpec(
                name="m02.render_expression",
                module="M2",
                summary="把 AST 句柄渲染回表达式文本，便于人工核对。",
                parameters=(ParamSpec("ast", "Ast", True),),
                returns="none",
                requires_handles=("Ast",),
                wrapped="sdl_m03.equations.render_expression",
            ),
            _impl_render_expression,
        ),
        (
            ToolSpec(
                name="m02.enumerate_candidates",
                module="M2",
                summary="在受限语法内枚举候选变量/表示，产出候选池句柄。",
                parameters=_spec_params(enumerate_candidates),
                returns="CandidateList",
                produces_handle=True,
                wrapped="sdl_m02.domain.enumerate_candidates",
            ),
            _impl_enumerate_candidates,
        ),
        (
            ToolSpec(
                name="m02.candidate_summary",
                module="M2",
                summary="汇总候选池：数量、深度分布与样例表达式（不重排、不筛选）。",
                parameters=(ParamSpec("candidates", "CandidateList", True),),
                returns="none",
                requires_handles=("CandidateList",),
                wrapped="sdl_m02.domain.Candidate",
            ),
            _impl_candidate_summary,
        ),
        (
            ToolSpec(
                name="m03.prepare_sample",
                module="M3",
                summary="经注入的 explorer 读取探索分区 E，制备 M3 拟合所需的探索样本。",
                parameters=(
                    ParamSpec("protocol_id", "str", True),
                    ParamSpec("variables", "Sequence[str]", True),
                    ParamSpec("target", "str", True),
                    ParamSpec("purpose", "str", False, "E"),
                ),
                returns="ExplorationSample",
                produces_handle=True,
                wrapped="sdl_m03.equations.prepare_sample",
                note="需要构造时注入 explorer 客户端；未注入即拒绝。",
            ),
            _impl_prepare_sample,
        ),
        (
            ToolSpec(
                name="m03.fit_relation",
                module="M3",
                summary="在探索样本上拟合一个候选关系式，产出拟合结果句柄。",
                parameters=(
                    ParamSpec("sample", "ExplorationSample", True),
                    ParamSpec("relationship", "Ast|Candidate|str", True),
                ),
                returns="RelationFit",
                requires_handles=("ExplorationSample",),
                produces_handle=True,
                wrapped="sdl_m03.equations.fit_relation",
            ),
            _impl_fit_relation,
        ),
        (
            ToolSpec(
                name="m03.baseline_constant",
                module="M3",
                summary="拟合常数基线（零模型），作为比较基准。",
                parameters=(ParamSpec("sample", "ExplorationSample", True),),
                returns="FitResult",
                requires_handles=("ExplorationSample",),
                produces_handle=True,
                wrapped="sdl_m03.equations.baseline_constant",
            ),
            _impl_baseline_constant,
        ),
        (
            ToolSpec(
                name="m03.baseline_linear",
                module="M3",
                summary="拟合单变量线性基线，作为比较基准。",
                parameters=(
                    ParamSpec("sample", "ExplorationSample", True),
                    ParamSpec("feature", "str", True),
                ),
                returns="FitResult",
                requires_handles=("ExplorationSample",),
                produces_handle=True,
                wrapped="sdl_m03.equations.baseline_linear",
            ),
            _impl_baseline_linear,
        ),
        (
            ToolSpec(
                name="m03.gain_against",
                module="M3",
                summary="计算候选相对基线的预测增益（转录，不含显著性判定）。",
                parameters=(
                    ParamSpec("candidate", "FitResult|RelationFit", True),
                    ParamSpec("baseline", "FitResult|RelationFit", True),
                    ParamSpec("loss", "str", False, "mse"),
                ),
                returns="none",
                requires_handles=("FitResult",),
                wrapped="sdl_m03.equations.gain_against",
                note="RelationFit 句柄会自动取其 candidate 拟合结果。",
            ),
            _impl_gain_against,
        ),
        (
            ToolSpec(
                name="m05.complexity",
                module="M5",
                summary="计算复杂度 C（公式长度近似，非严格 MDL）。",
                parameters=(ParamSpec("ast", "Ast", True),),
                returns="none",
                requires_handles=("Ast",),
                wrapped="sdl_m05.metrics.complexity",
            ),
            _impl_complexity,
        ),
        (
            ToolSpec(
                name="m05.knowledge_base",
                module="M5",
                summary="构造 M5 口径的知识库（版本 + 已知陈述列表），供新颖性使用。",
                parameters=(
                    ParamSpec("version", "str", True),
                    ParamSpec("entries", "Sequence[str]", False, ()),
                ),
                returns="MetricsKnowledgeBase",
                produces_handle=True,
                wrapped="sdl_m05.metrics.KnowledgeBase",
            ),
            _impl_knowledge_base,
        ),
        (
            ToolSpec(
                name="m05.novelty",
                module="M5",
                summary="相对指定知识库版本计算新颖性 N（须绑定 K 版本，本层只转录）。",
                parameters=(
                    ParamSpec("hypothesis_id", "str", True),
                    ParamSpec("hypothesis_version", "str", True),
                    ParamSpec("statement", "str", True),
                    ParamSpec("knowledge_base", "MetricsKnowledgeBase", True),
                    ParamSpec("expression", "Ast", False, None),
                ),
                returns="none",
                requires_handles=("MetricsKnowledgeBase",),
                wrapped="sdl_m05.metrics.novelty",
            ),
            _impl_novelty,
        ),
        (
            ToolSpec(
                name="m08.empty_knowledge",
                module="M8",
                summary="构造一个空的知识库版本（M8 口径，探索起点）。",
                parameters=(ParamSpec("version", "str", False, "K0"),),
                returns="KnowledgeVersion",
                produces_handle=True,
                wrapped="sdl_m08.archive.empty_knowledge",
            ),
            _impl_empty_knowledge,
        ),
        (
            ToolSpec(
                name="m08.knowledge_summary",
                module="M8",
                summary="转录知识库的条目数与已有等级（原样转录，不判定）。",
                parameters=(ParamSpec("knowledge", "KnowledgeVersion", True),),
                returns="none",
                requires_handles=("KnowledgeVersion",),
                wrapped="sdl_m08.archive.KnowledgeVersion",
            ),
            _impl_knowledge_entries,
        ),
        (
            ToolSpec(
                name="m09.extract_regularities",
                module="M9",
                summary="从知识库版本中提取确定结论（支持 / 反驳），供 LLM 汇总新规律。",
                parameters=(ParamSpec("knowledge", "KnowledgeVersion", True),),
                returns="none",
                requires_handles=("KnowledgeVersion",),
                wrapped="sdl_m09.collection.extract_discovered_regularities",
            ),
            _impl_extract_regularities,
        ),
    ]

    for spec, impl in specs:
        box.register(spec, impl)
    return box


# ---------------------------------------------------------------------------
# 边界自检
# ---------------------------------------------------------------------------


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查四项：

    1. 模块是否**定义**了 :data:`NOT_PROVIDED_BY_P18` 中的任何名字
       （越界重实现算法层或裁决层职责）；
    2. 模块是否出现 :data:`FORBIDDEN_ACCESS_NAMES` 中的导入或属性访问
       （绕过注入客户端直连存储 / 触碰确证侧）；
    3. 是否声明了会授予等级或结论的常量；
    4. 是否声明了「替 LLM 决策」的常量。

    刻意用 AST 而非源码文本匹配：本模块的 docstring 与常量里**必须**写出
    ``compute_p_value`` / ``bind_confirmation`` 等被禁名字（那是边界声明），
    文本匹配会把边界声明本身判成违规。同时，本模块**会调用**算法层接口
    （如 ``prepare_sample`` 的公开函数），但那属于**调用**而非**定义**，
    AST 比对定义名与属性访问即可区分。

    :return: 检查结果字典，``ok`` 为 ``True`` 表示全部通过。
    """
    import ast as _ast
    import pathlib

    source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
    tree = _ast.parse(source_path.read_text(encoding="utf-8"))

    # -- 收集本模块自身的定义名（函数/类/赋值目标） -------------------------
    defined: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name):
                    defined.add(target.id)

    # -- 收集本模块自身的**本地绑定名**（参数 / 赋值 / 定义），用于剔除
    #    与黑名单同名的合法本地变量，避免假阳性 ---------------------------
    local_bindings: set[str] = set(defined)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.arg):
            local_bindings.add(node.arg)

    # -- 收集访问名（属性访问 + 名字引用 + 导入别名） ----------------------
    accessed: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, _ast.Name):
            accessed.add(node.id)
        elif isinstance(node, _ast.alias):
            accessed.add(node.name.split(".")[-1])

    defined_other_stages = tuple(sorted(defined & set(NOT_PROVIDED_BY_P18)))
    #: 访问检查剔除本地绑定：``kwargs.get("p_value")`` 之类是守卫逻辑，
    #: 不是越权访问；只有**真正的**属性访问/导入才计入。
    accessed_forbidden = tuple(
        sorted((accessed - local_bindings) & set(FORBIDDEN_ACCESS_NAMES))
    )

    grants_grade = bool(TOOLBOX_GRANTS_EVIDENCE_GRADE)
    runs_stats = bool(TOOLBOX_RUNS_STATISTICS)
    adds_algorithms = bool(TOOLBOX_ADDS_ALGORITHMS)
    decides_for_llm = bool(TOOLBOX_DECIDES_NOT_LLM)

    return {
        "defined_other_stages": defined_other_stages,
        "accessed_forbidden": accessed_forbidden,
        "grants_evidence_grade": grants_grade,
        "runs_statistics": runs_stats,
        "adds_algorithms": adds_algorithms,
        "decides_for_llm": decides_for_llm,
        "ok": not (
            defined_other_stages
            or accessed_forbidden
            or grants_grade
            or runs_stats
            or adds_algorithms
            or decides_for_llm
        ),
        "checked_names": len(NOT_PROVIDED_BY_P18) + len(FORBIDDEN_ACCESS_NAMES),
    }


__all__ = [
    "TOOLBOX_VERSION",
    "TOOLBOX_DECIDES_NOT_LLM",
    "TOOLBOX_ADDS_ALGORITHMS",
    "TOOLBOX_GRANTS_EVIDENCE_GRADE",
    "TOOLBOX_RUNS_STATISTICS",
    "TOOLBOX_EXPLORATION_ONLY",
    "TOOLBOX_BOUNDARY_NOTE",
    "HANDLE_SEMANTICS_NOTE",
    "FORBIDDEN_TOOL_ARGUMENTS",
    "LEAK_GUARD_EXEMPT_KEYS",
    "M10_SEALED_KEYS",
    "NOT_PROVIDED_BY_P18",
    "FORBIDDEN_ACCESS_NAMES",
    "STATUS_OK",
    "STATUS_REJECTED",
    "STATUS_FAILED",
    "TOOL_RESULT_STATUSES",
    "TOOLBOX_REASONS",
    "REASON_OK",
    "REASON_UNKNOWN_TOOL",
    "REASON_FORBIDDEN_ARGUMENT",
    "REASON_BAD_HANDLE",
    "REASON_MISSING_ARGUMENT",
    "REASON_UNEXPECTED_ARGUMENT",
    "REASON_SEALED_LEAK",
    "REASON_TOOL_RAISED",
    "HANDLE_PREFIX",
    "ToolboxError",
    "ToolboxInputError",
    "ToolboxPolicyError",
    "UnknownToolError",
    "HandleError",
    "ParamSpec",
    "ToolSpec",
    "ToolResult",
    "HandleStore",
    "Toolbox",
    "build_default_toolbox",
    "self_check",
]
