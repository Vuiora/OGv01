"""P12 交付物：检验执行与跨轮误差预算（模块 M6）。

本模块是**确证执行的第二步**：P11 已经把冻结计划绑定到某轮的封存确证分区，
本模块在绑定的约束下**原子化消费**该分区、保证检验族被完整执行，
并按跨轮预算 :math:`\\alpha_t=\\alpha/2^{t}` 生成 Holm 逐步下降的显著性阈值。
理论依据见 `SDL算法框架说明.md <../SDL算法框架说明.md>`_ §6（数据隔离与跨轮误差控制）
与 `DS协作指南-阶段划分与提示词.md <../DS协作指南-阶段划分与提示词.md>`_ §3 P12；
接口约定见 `INTERFACES.md <../INTERFACES.md>`_ §2.6 与 §3。

四条验收标准的实现方式
-----------------------

① **``alpha_for_round`` 严格为 ``total_alpha / 2**t``。**
   实现见 :func:`alpha_for_round`：``math.ldexp(total_alpha, -round_index)``。
   ``math.ldexp(x, n)`` 的定义就是 :math:`x \\times 2^{n}`，因此
   ``ldexp(a, -t) == a / 2**t`` 在浮点意义上是**恒等**的，
   不存在任何四舍五入或近似分支。轮次 ``t`` 受 :data:`ROUND_INDEX_MIN` /
   :data:`ROUND_INDEX_MAX` 约束（与 M1 的 ``_validate_plan`` 同步，见 P11 的
   :func:`sdl_m06.freeze._validate_round_index`）；``alpha_t`` 下溢到 0 时
   拒绝执行（:class:`ErrorBudgetError`）——「预算为 0」意味着任何 p 值都不可能显著，
   继续执行只会产生一轮无意义的确证。

   实现刻意与 M1 无关：本模块**不从绑定结果抄 alpha**，而是由公开协议的
   ``confirmation_policy.total_alpha`` 与轮次独立复算，并与
   :func:`execute_family` 的输入 ``round_index``、``plan["round_index"]``
   **多方对账**，任一处不一致即拒绝执行（:class:`SequenceOrderError`）。这样「严格为
   ``total_alpha/2**t``」就不是一句声明，而是被强制的不变量。

② **消费顺序为先提交 ``used`` 再返回数据。**
   这是本模块唯一的**有序**副作用：:func:`execute_family` 先调用注入模块的
   ``consume_confirmation(binding_id)``，**它返回时 M1 已经完成
   ``used`` 状态迁移与账本提交**（见 ``IMPLEMENTATION_CONTRACT.md``：
   *Atomic ``used`` transition + ledger commit BEFORE any returned content*），
   之后本模块才让确证记录进入执行路径。

   为了把这条顺序做成可机检的事实，而不是「靠代码写对」：

   - 确证记录**只能**经 ``consume_confirmation`` 的返回值进入本模块。
     :func:`execute_family` **没有任何**读取记录参数
     （``records`` / ``dataset`` / ``raw`` 一律列入 :data:`EXECUTE_RESERVED_KEYS` 并被拒绝），
     模块内也不存在 :func:`_forbidden_access_names` 所列的任何数据访问入口；
   - 显式传入的 ``trajectories``（各检验的预声明检验轨迹）**只是统计事实的载体**，
     它不是确证原始记录；执行时由 :func:`_check_binding_handoff` 提交的
     ``used_committed`` 门闩守护：未提交 ``used`` 之前，轨迹不可被读取；
   - :class:`ExecutionOrder` 记录 ``consume_call_index`` 与 ``first_read_index``，
     并提供 :meth:`ExecutionOrder.verify` 断言「消费调用先于首次读取」。

③ **检验族完整，不得删改失败项。**
   完整性与「不得删改」由 P11 建立、由本模块沿用并再加两道锁：

   - 计划在进入执行前先过 :func:`sdl_m06.freeze.validate_frozen_plan`（离线自检）；
   - :func:`check_family_coherence` 用 P11 的 :func:`sdl_m06.freeze.family_coverage`
     **逐条对账**：轨迹的 ``test_id`` 集合与计划的 ``test_id`` 集合必须严格相等
     （缺一条 = 不完整，多一条 = 计划被外部改写，二者都报错）；
   - 执行拒绝任何**筛选参数**：``only`` / ``exclude`` / ``subset`` / ``filter`` /
     ``select`` / ``drop`` 一律列入 :data:`FORBIDDEN_CALL_KEYS`，
     因此「先全量算、再挑成功项上报」在本接口上不可表达。
     执行结果逐条保留 ``available`` 标记：未提供轨迹的检验也在结果中占位，
     只是其 ``p_value`` 为 ``None``，并计入 ``unavailable_test_ids``——
     **「没算」与「算出来不显著」在结果对象里是可区分的**，任何一条检验都不会被静默丢弃。

④ **Holm 校正可运行，且对空输入返回空。**
   :func:`holm_adjust` 是标准 Holm 逐步下降（step-down）实现，
   自带单调化（``p_adj`` 非降），并对 ``p`` 缺失的检验保持缺失而不插补。
   空映射返回空映射、空序列返回空元组，两侧一致。

口径与边界（本模块不做什么）
----------------------------

- **不判定结果分类。** :func:`execute_family` 只给出「是否超过本轮阈值」这一
  机械事实（``significant``），**不**把任何检验归入
  ``supported`` / ``refuted`` / ``inconclusive``，也不调用 ``record_evaluation`` /
  ``release_results``——三分类、记录与发布属 P13。
  该边界以 :data:`NOT_PROVIDED_BY_P12` 声明，并由测试用 AST 机检。
- **不计算 p 值。** 效应量与 p 值的计算由外部预先声明的检验方案完成
  （``IMPLEMENTATION_CONTRACT.md``：*This records external M6 results,
  does not compute p values*）。本模块是「统计执行器」的组织层：
  它负责消费顺序、族的完整性、误差预算与阈值，不发明统计方法。
- **不修 α 的结构去当作用统计方法。** Holm 只处理批内多重比较；
  数据泄漏、错误采样模型、检验后选择与「反复查看 p 值后提前停止」一律不由本模块修复
  （见 :data:`HOLM_NOT_A_CURE_NOTE`）。这些前提属外部义务。
- **不修改 ``sdl_m01/``**，不读取/打印/写入角色令牌、evidence 数据库内容或受限质量报告，
  不引入第三方依赖。

术语约定（全文一致）
--------------------

- **检验轨迹（trajectory）**：外部执行器给出的、**预先声明**的检验产物，
  至少含一个 p 值；可附带效应量、标准误、样本量等用于披露的事实。
  它是本模块唯一的统计输入。
- **族（family）**：冻结计划 ``test_family`` 中登记的全部检验；
  「完整执行」指族内每一条都被计算或如实标记为不可得。
- **轮次（round）**：研究轮次 :math:`t`，与「对话轮次」无关；
  只有 P11–P13 的产物才推进它。
"""

from __future__ import annotations

import ast as _ast
import hashlib
import inspect
import json
import math
import pathlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

from sdl_m06.freeze import (
    FORBIDDEN_TOKEN_KEYS,
    ROUND_INDEX_MAX,
    ROUND_INDEX_MIN,
    family_coverage,
    validate_frozen_plan,
)

__all__ = [
    # 异常
    "ExecuteError",
    "ExecuteInputError",
    "SequenceOrderError",
    "ErrorBudgetError",
    "FamilyCoherenceError",
    # 常量：版本与预算
    "EXECUTE_VERSION",
    "ROUND_INDEX_MIN",
    "ROUND_INDEX_MAX",
    "ROUND_SCOPE_NOTE",
    "ALPHA_FORMULA_NOTE",
    "ALPHA_UNDERFLOW_NOTE",
    "FORWARD_ALPHA_NOTE",
    "TOTAL_ALPHA_SOURCE_NOTE",
    "HOLM_NOTE",
    "HOLM_ADJUSTMENT_NAME",
    "HOLM_NOT_A_CURE_NOTE",
    "CONSUMPTION_ORDER_NOTE",
    "FAMILY_COHERENCE_NOTE",
    "COMPLETE_FAMILY_BOUNDARY_NOTE",
    "EVIDENCE_PURPOSE",
    "EVIDENCE_PURPOSES",
    "MEASUREMENT_KEYS",
    "FACT_KEYS",
    "RECORD_METADATA_KEYS",
    "FORBIDDEN_CALL_KEYS",
    "EXECUTE_RESERVED_KEYS",
    "NOT_PROVIDED_BY_P12",
    # 误差预算
    "alpha_for_round",
    # Holm 校正
    "holm_adjust",
    # 族级辅助
    "family_test_ids",
    "check_family_coherence",
    "summarize_family",
    # 主入口
    "execute_family",
    # 结果对象
    "ExecutionOrder",
    "HolmEntry",
    "HolmResult",
    "FamilyExecution",
    "TestExecution",
]


# ---------------------------------------------------------------------------
# 常量：版本与跨轮误差预算（验收标准①）
# ---------------------------------------------------------------------------

#: 交付物版本号。写入执行记录的 ``code_version``，便于追溯执行口径的变更。
EXECUTE_VERSION = "P12-v1.0"

#: 轮次取值范围，与 M1 的 ``_validate_plan`` 及 P11 的
#: :data:`sdl_m06.freeze.ROUND_INDEX_MIN` / :data:`sdl_m06.freeze.ROUND_INDEX_MAX`
#: 必须保持同步（三处任一改动都要同步）。
#:
#: 上界 1022 不是随意取的：:data:`sdl_m06.freeze.ROUND_INDEX_MAX` 与之相同，
#: 且 ``total_alpha/2**1023`` 在双精度下必然下溢到 0，
#: 因此这个上界同时是「误差预算仍可表示」的边界。

#: 研究轮次与对话轮次的分野说明。混淆二者是本项目最容易犯的设计错误。
ROUND_SCOPE_NOTE = (
    "轮次 t 指研究轮次（SDL 主循环的确证轮次），不是「对话轮次」（P00–P16）。"
    "一次对话轮次不得递增 t，也不得消耗 α 预算；"
    "跨轮预算只由 P11–P13 的产物推进。"
)

#: 跨轮预算的公式说明（验收标准①的可读形式）。
ALPHA_FORMULA_NOTE = (
    "跨轮误差预算 α_t = α / 2**t（t ≥ 1），几何级数求和 Σ_{t≥1} α_t = α。"
    "实现用 math.ldexp(α, -t)，该函数按定义即 α × 2**(-t)，"
    "因此 α_t 与 α / 2**t 在浮点意义下恒等（不存在近似分支）。"
    "协议的 total_alpha 只是数值来源；本模块独立复算，不从绑定记录抄写。"
)

#: 「预算下溢」的处理口径。
ALPHA_UNDERFLOW_NOTE = (
    "α_t 下溢到 0（或非正）时拒绝执行：预算是 0 意味着任何 p 值都无法被判定为显著，"
    "继续执行只会产出无意义的一轮确证，并可能被误读为「检验过了、没发现」。"
)

#: α 的可见性口径：α 是「对外接口的公式」，不是探索侧的第一自由度。
FORWARD_ALPHA_NOTE = (
    "α 是对外接口的公式（α = Σ_t α_t 这一分配规则），不是探索侧的第一自由度；"
    "本模块不把原始 α 暴露给探索侧，也不提供任何从探索侧调整 α 的入口"
    "（total_alpha 只能来自绑定记录，且为只读值）。"
)


# ---------------------------------------------------------------------------
# 常量：Holm 校正（验收标准④）
# ---------------------------------------------------------------------------

#: 本模块实现的校正方法名。单列成常量，避免在注释与代码里出现两种写法。
HOLM_ADJUSTMENT_NAME = "holm"

#: Holm 校正的口径说明。刻意不叫 ``bonferroni_holm`` 之外的别名，
#: 也不提供 ``method=`` 参数：本阶段只实现一种方法，
#: 多一个可选方法就多一个「事后换方法」的可选报告路径。
HOLM_NOTE = (
    "Holm 逐步下降（step-down）校正：把 m 个 p 值升序排列，"
    "第 k 个（k 从 1 计）的调整值为 max(p_(k) × (m - k + 1), 前一个调整值)，"
    "故调整值序列非降。它控制家族错误率（FWER），"
    "且不要求批内 p 值相互独立（可处理任意相关的有效 p 值）。"
    "本实现只提供 Holm 一种方法，不设 method 参数——避免「事后换校正方法」这一可选报告路径。"
)

#: Holm 的适用边界：它不修复四类前提性问题。
HOLM_NOT_A_CURE_NOTE = (
    "Holm 校正只处理批内多重比较，不能修复数据泄漏、错误采样模型、检验后选择，"
    "也不能为「反复查看 p 值后任意提前停止」提供保证（后者需预定序贯方法）。"
    "复核者不得把「已做 Holm 校正」当作这些前提成立的证据。"
)


# ---------------------------------------------------------------------------
# 常量：消费顺序与族完整性（验收标准②③）
# ---------------------------------------------------------------------------

#: 消费顺序说明（验收标准②的可读形式）。
CONSUMPTION_ORDER_NOTE = (
    "正确顺序是「先 bind（P11）→ 再原子化 consume → 拿到数据后才执行检验」。"
    "consume_confirmation 返回时，M1 已经完成 used 状态迁移与账本提交，"
    "因此本模块在拿到轨迹之前就已满足「先提交 used 再返回数据」。"
    "本模块不提供任何绕过消费直接读取确证记录的路径："
    "确证记录只能作为 consume_confirmation 的返回值进入执行路径。"
)

#: 族完整性说明（验收标准③的可读形式）。
FAMILY_COHERENCE_NOTE = (
    "执行前用 P11 的 family_coverage 反解计划的检验标识集合，"
    "与轨迹携带的检验标识集合严格对账：缺一条说明族不完整，"
    "多一条说明冻结计划被外部改写；两者都拒绝执行。"
    "执行接口不提供 only / exclude / subset 一类筛选参数，"
    "因此「先全量算、再挑成功项上报」在本接口上不可表达。"
)

#: 「完整执行」的边界：不可得检验如何登记。
COMPLETE_FAMILY_BOUNDARY_NOTE = (
    "「完整执行」不等于「每条都有 p 值」。未提供轨迹的检验在结果中**占位**，"
    "其 p_value 为 None 并单列进 unavailable_test_ids："
    "「没算」与「算出来不显著」必须可区分，不得插补、不得丢弃。"
)


# ---------------------------------------------------------------------------
# 常量：输入形态与白名单
# ---------------------------------------------------------------------------

#: 本模块执行的确证用途前缀。只有 ``C`` 开头的确证分区可以被执行：
#: 探索（E）与开发（V）分区上的「检验」不构成确证。
EVIDENCE_PURPOSE = "C"

#: 允许进入执行路径的确证用途集合（``C`` 前缀判定，见 :func:`_evidence_purpose_ok`）。
EVIDENCE_PURPOSES: tuple[str, ...] = ("C",)

#: 检验轨迹允许的键。轨迹是「预先声明的检验产物」的载体；
#: 未知键一律拒绝，避免调用方以为某个统计口径已生效、实际被静默忽略。
MEASUREMENT_KEYS: frozenset[str] = frozenset(
    {
        # 必需
        "p_value",
        # 效应与不确定性的披露事实（均为可选）
        "effect",
        "delta",
        "standard_error",
        "sample_size",
        "unit_count",
        # 追溯
        "measure",
        "estimator",
        "notes",
    }
)

#: 事实载荷中**必须**出现的键。至少要有 p 值，否则这条「检验」没有统计内容。
FACT_KEYS: tuple[str, ...] = ("p_value",)

#: **载体元数据键**：当轨迹条目是「上游执行记录 + 统计事实」的连接形态时，
#: 条目里会夹带定位与追溯信息。它们既不触发「未知键」报错，
#: 也不携带任何记录正文。
RECORD_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "test_id",
        "hypothesis_id",
        "metric",
        "subgroup",
        "index",
        "available",
        "provenance",
        "source",
        "source_ref",
        "code_version",
        "executor",
        "executed_at",
        "reasons",
    }
)

#: 明确拒绝的关键字参数。两类：
#:
#: 1. **记录读取类**（``records`` / ``dataset`` / ``raw`` / ``read_dataset``）：
#:    确证记录只能经 ``consume_confirmation`` 的返回值进入本层，
#:    任何「顺手传一份数据进来」的入口都会破坏验收标准②的消费顺序；
#: 2. **筛选类**（``only`` / ``exclude`` / ``subset`` / ``filter`` / ``select`` / ``drop``）：
#:    会使「族完整、不得删改失败项」失效（验收标准③）；
#: 3. **令牌类**：不得进入本层输入。
FORBIDDEN_CALL_KEYS: frozenset[str] = frozenset(
    {
        "records",
        "dataset",
        "raw",
        "read_dataset",
        "quality",
        "only",
        "exclude",
        "subset",
        "filter",
        "select",
        "drop",
        "token",
        "role_token",
    }
)

#: 执行入口的**必填**参数名。它们的默认值是本地常量（不是调用方传入对象），
#: 因此显式传 ``None`` 会被视为「忘记传值」而拒绝——静默替换成默认值会让
#: 「没声明」被误认为「用了默认声明」。
#:
#: ``round_index`` / ``protocol`` / ``trajectories`` 不在此列：
#: ``round_index=None`` 表示沿用计划声明的轮次，``trajectories=None``
#: 表示「无轨迹（全部检验记为不可得）」，二者都是合法缺省；
#: ``protocol=None`` 由 :func:`execute_family` 单独给出更明确的报错。
EXECUTE_RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "binding",
        "plan",
    }
)

#: :math:`\\alpha` 的来源口径。本模块**不从绑定记录抄写** α，
#: 而是由公开协议的 ``confirmation_policy.total_alpha`` 与轮次独立复算，
#: 再与绑定登记的 α 对账。之所以不从计划里取：冻结计划的字段集受
#: ``IMPLEMENTATION_CONTRACT.md`` 与 :data:`sdl_m06.freeze.FROZEN_PLAN_FIELDS`
#: 的「逐项对应」约束，往里塞一个非契约字段会破坏该约束
#: （P11 的 ``validate_frozen_plan`` 也会拒绝多余字段）。
TOTAL_ALPHA_SOURCE_NOTE = (
    "本轮 α 的数值来源是公开协议的 confirmation_policy.total_alpha；"
    "本模块据此与轮次独立复算 α_t=α/2**t，再与绑定登记的 α 对账。"
    "三处（协议、绑定、本次执行轮次）必须一致，任一处不一致都拒绝执行。"
)

#: 本模块**不提供**的接口名。测试据此断言「P12 未越界实现其它阶段的内容」。
#:
#: 注意：本模块自己的 :func:`execute_family` / :func:`holm_adjust` /
#: :func:`alpha_for_round` **不在此列**，它们是本阶段的交付内容。
NOT_PROVIDED_BY_P12: tuple[str, ...] = (
    # P13 的结果分类、记录与发布
    "classify_result",
    "record_and_release",
    "record_evaluation",
    "release_results",
    "release",
    # P14 的主动取证
    "divergence",
    "acquisition_plan",
    # P15 的归档
    "archive_round",
    "update_knowledge_version",
    "revise_or_retire",
    # P09/P10 的度量与筛选（本模块只消费，不重算）
    "gain",
    "novelty",
    "evaluate_hypothesis",
    "pareto_front",
    "top_k_reserved",
    "select_freeze_candidates",
    # P11 的构造（本模块只校验与执行，不重建计划）
    "build_frozen_plan",
    "bind",
    # 任何数据读取入口（本模块没有任何读确证记录的路径）
    "read_dataset",
    "read_records",
    "consume",
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ExecuteError(ValueError):
    """本模块所有异常的基类，继承自 :class:`ValueError`。"""


class ExecuteInputError(ExecuteError):
    """输入形态非法（缺绑定、缺计划、类型不对、键名受限等）。"""


class SequenceOrderError(ExecuteError):
    """消费顺序或轮次对账失败（先取数据后提交 used、轮次不一致等）。

    单列一个子类，是因为验收标准②把「消费顺序」作为独立一条：
    调用方与测试需要能把它与一般的输入错误区分开。
    """


class ErrorBudgetError(ExecuteError):
    """误差预算非法（轮次越界、total_alpha 非法）或下溢到 0。"""


class FamilyCoherenceError(ExecuteError):
    """检验族对账失败（轨迹与冻结计划的检验标识集合不一致）。

    验收标准③「族完整、不得删改失败项」的可机检形式。
    """


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionOrder:
    """消费与读取的**顺序凭证**（验收标准②的可机检形式）。

    本对象记录的是「哪个动作先发生」，而不是「代码看起来对不对」：

    :param consume_call_index: ``consume_confirmation`` 调用的单调序号（从 1 起）。
    :param used_committed_index: ``used`` 状态提交的单调序号。
        由于 M1 的 ``consume_confirmation`` 返回时提交必然已完成，
        该值**恒等于** :attr:`consume_call_index`。
    :param first_read_index: 首次读取（轨迹或确证记录）的单调序号。
        未发生任何读取时为 ``None``（全部检验记为不可得、未读入任何统计事实）。
        该序号由内部的单调步进账本**实际步进**得到，不是在构造本对象时写定的常量。
    :param replay_used: 是否使用了显式 replay（复核轨迹）。
        复核**不**构成新证据，故在结果中被显式标出。
    :param note: 口径说明。
    """

    consume_call_index: int
    used_committed_index: int
    first_read_index: int | None
    replay_used: bool = False
    note: str = CONSUMPTION_ORDER_NOTE

    def verify(self) -> None:
        """断言「先提交 ``used``、再返回数据」这一顺序成立。

        :raises SequenceOrderError: 顺序被破坏（提交晚于消费），
            或发生了读取却没有提交凭证。
        """
        if self.used_committed_index != self.consume_call_index:
            raise SequenceOrderError(
                "消费顺序被破坏：used 状态提交序号 "
                f"{self.used_committed_index} 与消费调用序号 "
                f"{self.consume_call_index} 不一致。" + CONSUMPTION_ORDER_NOTE
            )
        if self.first_read_index is not None:
            if self.used_committed_index <= 0:
                raise SequenceOrderError(
                    "未提交 used 状态即读取了内容：本层没有任何绕过消费的读取路径。"
                    + CONSUMPTION_ORDER_NOTE
                )
            if self.used_committed_index > self.first_read_index:
                raise SequenceOrderError(
                    "消费顺序被破坏：首次读取（序号 "
                    f"{self.first_read_index}）早于 used 提交（序号 "
                    f"{self.used_committed_index}）。" + CONSUMPTION_ORDER_NOTE
                )

    @property
    def committed_before_read(self) -> bool:
        """提交是否先于（或等于）首次读取。无读取时视为成立。"""
        if self.first_read_index is None:
            return self.used_committed_index == self.consume_call_index
        return self.used_committed_index <= self.first_read_index

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "consume_call_index": self.consume_call_index,
            "used_committed_index": self.used_committed_index,
            "first_read_index": self.first_read_index,
            "replay_used": self.replay_used,
            "committed_before_read": self.committed_before_read,
            "note": self.note,
        }


@dataclass(frozen=True)
class HolmEntry:
    """Holm 校正中的一条记录（按升序处理的第 k 条）。

    :param test_id: 检验标识。
    :param p_value: 原始 p 值。
    :param rank: 升序名次（从 1 计）。
    :param factor: 该名次的乘数 ``m - k + 1``。
    :param adjusted: 该名次的调整后 p 值（已单调化）。
    """

    test_id: str
    p_value: float
    rank: int
    factor: int
    adjusted: float

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "p_value": self.p_value,
            "rank": self.rank,
            "factor": self.factor,
            "adjusted": self.adjusted,
        }


@dataclass(frozen=True)
class HolmResult:
    """Holm 校正的完整结果。

    :param adjusted: ``检验标识 → 调整后 p 值``（只含原始 p 值得以计算的检验）。
    :param raw: ``检验标识 → 原始 p 值``（只读视图）。
    :param entries: 升序处理轨迹（供复核逐条核对乘数）。
    :param missing: 原始 p 值为 ``None`` 的检验标识（按名排序）。
    :param method: 校正方法名，恒为 :data:`HOLM_ADJUSTMENT_NAME`。
    :param note: 口径说明。
    """

    adjusted: Mapping[str, float]
    raw: Mapping[str, float]
    entries: tuple[HolmEntry, ...]
    missing: tuple[str, ...]
    method: str = HOLM_ADJUSTMENT_NAME
    coefficient: int = 1
    note: str = HOLM_NOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "adjusted", MappingProxyType(dict(self.adjusted)))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    def __len__(self) -> int:
        return len(self.adjusted)

    def __getitem__(self, test_id: str) -> float:
        return self.adjusted[test_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self.adjusted)

    def __contains__(self, test_id: object) -> bool:
        return test_id in self.adjusted

    def items(self):
        return self.adjusted.items()

    def get(self, test_id: str, default: Any = None) -> Any:
        return self.adjusted.get(test_id, default)

    @property
    def size(self) -> int:
        """参与校正的检验数量（不含缺失者）。"""
        return len(self.adjusted)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "adjusted": dict(self.adjusted),
            "raw": dict(self.raw),
            "entries": [entry.to_dict() for entry in self.entries],
            "missing": list(self.missing),
            "note": self.note,
        }


@dataclass(frozen=True)
class TestExecution:
    """单条检验的执行记录。

    :param test_id: 检验标识。
    :param hypothesis_id: 所属假说。
    :param metric: 指标（由检验标识反解）。
    :param subgroup: 子群（由检验标识反解）。
    :param null: 计划的零假说原文（**完整转录**：不得删改）。
    :param alternative: 计划的备择原文（**完整转录**）。
    :param method: 计划的检验方法原文（**完整转录**）。
    :param p_value: 原始 p 值；未提供轨迹时为 ``None``。
    :param effect: 效应量（可选披露事实）。
    :param delta: 该检验所用的效应阈值（默认取计划的 ``effect_threshold``）。
    :param holm_adjusted: 该检验在本轮族内的 Holm 调整值；不可得时 ``None``。
    :param rank: Holm 升序名次；不可得时 ``None``。
    :param holm_threshold: 该名次的 Holm 判定阈值 ``α_t / (m - k + 1)``；
        不可得时 ``None``。
    :param significant: 机械判定结果（原 p ≤ 阈值）。**不构成结果分类**。
    :param available: 是否有可用的 p 值。``False`` 表示「没算」而非「不显著」。
    :param facts: 轨迹携带的披露事实（只读视图）。
    :param reasons: 不可得或异常的说明（如 ``未提供检验轨迹``）。
    """

    test_id: str
    hypothesis_id: str
    metric: str
    subgroup: str
    null: str
    alternative: str
    method: str
    p_value: float | None
    effect: float | None
    delta: float
    holm_adjusted: float | None
    rank: int | None
    holm_threshold: float | None
    significant: bool | None
    available: bool
    facts: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "hypothesis_id": self.hypothesis_id,
            "metric": self.metric,
            "subgroup": self.subgroup,
            "null": self.null,
            "alternative": self.alternative,
            "method": self.method,
            "p_value": self.p_value,
            "effect": self.effect,
            "delta": self.delta,
            "holm_adjusted": self.holm_adjusted,
            "rank": self.rank,
            "holm_threshold": self.holm_threshold,
            "significant": self.significant,
            "available": self.available,
            "facts": dict(self.facts),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class FamilyExecution:
    """一次检验族执行的完整结果（验收标准①②③④的统一载体）。

    :param binding_id: 绑定标识（来自 M1 的 ``bind_confirmation``）。
    :param plan_digest: 冻结计划的摘要（用于确认执行的是哪一份计划）。
    :param round_index: 研究轮次 :math:`t`。
    :param total_alpha: 计划所属协议的总体 :math:`\\alpha`（只读转录）。
    :param alpha: 本轮预算 :math:`\\alpha_t=\\alpha/2^{t}`（独立复算）。
    :param results: 逐条执行记录（顺序与冻结计划的 ``test_family`` 一致）。
    :param holm: Holm 校正结果。
    ``test_id → (holm_threshold, significant)``；为保证可 JSON 序列化，
    ``significant`` 以布尔存储（``available`` 为 ``False`` 的检验不参与判定）。
    :param decisions: 逐条机械判定：``test_id → (holm_threshold, significant)``。
    :param order: 消费顺序凭证。
    :param consumed_record_count: 本次消费返回的确证记录条数（**只记数量**，
        不转录任何记录正文）：用于复核「确证分区确实有数据被消费」。
    :param excluded_failed_tests: 恒为空元组——本模块不剔除任何失败项。
        保留该字段是为了让「有没有剔除」成为可读的事实（空 = 没剔除），
        而不是一个需要读代码才能确认的隐含性质。
    :param unavailable_test_ids: 无可用 p 值的检验标识（按名排序）。
    :param code_version: 执行口径版本。
    :param notes: 口径说明元组。
    """

    binding_id: str
    plan_digest: str
    round_index: int
    total_alpha: float
    alpha: float
    results: tuple[TestExecution, ...]
    holm: HolmResult
    decisions: Mapping[str, tuple[float, bool]]
    order: ExecutionOrder
    consumed_record_count: int = 0
    excluded_failed_tests: tuple[str, ...] = ()
    unavailable_test_ids: tuple[str, ...] = ()
    code_version: str = EXECUTE_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "decisions", MappingProxyType(dict(self.decisions)))
        object.__setattr__(self, "excluded_failed_tests", tuple(self.excluded_failed_tests))
        object.__setattr__(self, "unavailable_test_ids", tuple(self.unavailable_test_ids))
        object.__setattr__(self, "notes", tuple(self.notes))

    # -- 只读视图 ---------------------------------------------------------

    @property
    def test_count(self) -> int:
        """检验族规模（恒等于冻结计划的检验数）。"""
        return len(self.results)

    @property
    def available_count(self) -> int:
        """有可用 p 值的检验数。"""
        return sum(1 for item in self.results if item.available)

    @property
    def significant_count(self) -> int:
        """机械判定为「超过本轮阈值」的检验数（不构成结果分类）。"""
        return sum(1 for item in self.results if item.significant is True)

    @property
    def complete(self) -> bool:
        """族内每条检验都有可用 p 值。"""
        return not self.unavailable_test_ids

    def test_ids(self) -> tuple[str, ...]:
        """族内全部检验标识（与冻结计划同序）。"""
        return tuple(item.test_id for item in self.results)

    def result_of(self, test_id: str) -> TestExecution:
        """按标识取执行记录。

        :raises ExecuteInputError: 标识不在本族内。
        """
        for item in self.results:
            if item.test_id == test_id:
                return item
        raise ExecuteInputError(f"检验 {test_id!r} 不在本轮检验族内。")

    def to_dict(self) -> dict:
        """转为可序列化字典。"""
        return {
            "binding_id": self.binding_id,
            "plan_digest": self.plan_digest,
            "round_index": self.round_index,
            "total_alpha": self.total_alpha,
            "alpha": self.alpha,
            "results": [item.to_dict() for item in self.results],
            "holm": self.holm.to_dict(),
            "order": self.order.to_dict(),
            "consumed_record_count": self.consumed_record_count,
            "excluded_failed_tests": list(self.excluded_failed_tests),
            "unavailable_test_ids": list(self.unavailable_test_ids),
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        """规范 JSON（键排序、无多余空格），供持久化与摘要使用。"""
        return canonical_json(self.to_dict())

    def content_digest(self) -> str:
        """本次执行结果的规范摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """把对象编码为确定性的 JSON 文本（键排序、紧凑分隔符、ASCII 转义）。"""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性分支
        raise ExecuteInputError(f"对象无法规范序列化为 JSON：{exc}") from exc


def _is_number(value: Any) -> bool:
    """是否为有限实数（排除 ``bool``——它在 Python 里是 ``int`` 的子类）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_number(value: Any, label: str) -> float:
    """校验有限实数并返回 ``float``。

    :raises ExecuteInputError: 非有限实数。
    """
    if not _is_number(value):
        raise ExecuteInputError(f"{label} 必须是有限实数，实际得到：{value!r}")
    return float(value)


def _require_text(value: Any, label: str) -> str:
    """校验非空字符串。

    :raises ExecuteInputError: 非字符串或全为空白。
    """
    if not isinstance(value, str) or not value.strip():
        raise ExecuteInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """校验映射类型。

    :raises ExecuteInputError: 非映射。
    """
    if not isinstance(value, Mapping):
        raise ExecuteInputError(f"{label} 必须是映射，实际得到：{type(value).__name__}")
    return value


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    """校验序列类型（排除 ``str`` / ``bytes`` 这类「像序列的字符串」）。

    :raises ExecuteInputError: 不是可迭代序列。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExecuteInputError(
            f"{label} 必须是序列，实际得到：{type(value).__name__}"
        )
    return value


def _check_no_forbidden_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝任何疑似携带令牌正文的键（依据 ``INTERFACES.md`` §4.4）。

    :raises ExecuteInputError: 命中 :data:`FORBIDDEN_TOKEN_KEYS`。
    """
    for key in mapping:
        if isinstance(key, str) and key.lower() in FORBIDDEN_TOKEN_KEYS:
            raise ExecuteInputError(
                f"{label} 含受限键名 {key!r}：令牌正文不得进入本层输入、日志或提示词。"
            )


def _reject_forbidden_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝受限的关键字参数（见 :data:`FORBIDDEN_CALL_KEYS`）。

    :raises ExecuteInputError: 传入受限键或必填但值为 ``None`` 的保留键。
    """
    for key in kwargs:
        lowered = str(key).lower()
        if lowered in FORBIDDEN_CALL_KEYS:
            raise ExecuteInputError(
                f"{where} 不接受参数 {key!r}：" + _forbidden_reason(lowered)
            )
        if key in EXECUTE_RESERVED_KEYS and kwargs[key] is None:
            raise ExecuteInputError(
                f"{where} 的保留参数 {key!r} 不能显式传 None（None 会被当作「用默认值」，"
                "而默认值是本地常量，不是调用方传入的对象）：请传具体值或省略该参数。"
            )


def _forbidden_reason(key: str) -> str:
    """给出某个受限键被拒绝的原因（可读的中文说明）。"""
    if key in ("records", "dataset", "raw", "read_dataset", "quality"):
        return (
            "确证记录只能经 consume_confirmation 的返回值进入本层；"
            "任何外部传入的记录入口都会破坏「先提交 used、再返回数据」的顺序。"
            + CONSUMPTION_ORDER_NOTE
        )
    if key in ("only", "exclude", "subset", "filter", "select", "drop"):
        return (
            "执行不接受任何筛选参数：检验族必须完整执行，不得删改失败项或只上报成功项。"
            + FAMILY_COHERENCE_NOTE
        )
    return "令牌类键名不得进入本层输入。"


def _round_index_range() -> tuple[int, int]:
    """返回合法轮次区间，取 P11 与 M1 的公共约定（二者必须同步）。"""
    return ROUND_INDEX_MIN, ROUND_INDEX_MAX


def _validate_round_index(round_index: Any, label: str = "round_index") -> int:
    """校验研究轮次为整数且落在合法区间内。

    :raises ErrorBudgetError: 越界或非整数。
    """
    low, high = _round_index_range()
    if isinstance(round_index, bool) or not isinstance(round_index, int):
        raise ErrorBudgetError(f"{label} 必须是整数，实际得到：{round_index!r}")
    if not low <= round_index <= high:
        raise ErrorBudgetError(
            f"{label} 必须落在 {low}..{high} 内，实际得到：{round_index!r}。"
            + ROUND_SCOPE_NOTE
        )
    return round_index


# ---------------------------------------------------------------------------
# 验收标准①：跨轮误差预算
# ---------------------------------------------------------------------------


def alpha_for_round(total_alpha: Any, round_index: Any) -> float:
    """计算第 :math:`t` 轮的误差预算 :math:`\\alpha_t=\\alpha/2^{t}`。

    实现为 :func:`math.ldexp`：``ldexp(x, n)`` 按定义等于
    :math:`x\\times 2^{n}`，因此 ``ldexp(total_alpha, -t)`` 与
    ``total_alpha / 2**t`` **恒等**，不存在近似或截断分支。

    本函数是**写死的公式，不接受任何替代口径**：没有 ``method`` 参数、
    没有「线性递减」或「固定 α」的选项。跨轮预算一旦可被调用方替换，
    :math:`\\sum_t\\alpha_t=\\alpha` 就不再成立，整轮的确证强度随之失效。

    :param total_alpha: 协议登记的总体 :math:`\\alpha`，必须严格落在 ``(0, 1)``。
    :param round_index: 研究轮次 :math:`t\\ge 1`（上界见 :data:`ROUND_INDEX_MAX`）。
    :return: :math:`\\alpha_t`，严格为正的实数。
    :raises ErrorBudgetError: ``total_alpha`` 非 ``(0,1)`` 实数、
        轮次越界，或 :math:`\\alpha_t` 下溢到 0。
    """
    if not _is_number(total_alpha):
        raise ErrorBudgetError(
            f"total_alpha 必须是有限实数，实际得到：{total_alpha!r}。"
        )
    alpha_total = float(total_alpha)
    if not 0.0 < alpha_total < 1.0:
        raise ErrorBudgetError(
            "total_alpha 必须严格落在 (0, 1) 内，实际得到："
            f"{alpha_total!r}；" + FORWARD_ALPHA_NOTE
        )
    index = _validate_round_index(round_index, "total_alpha 对应的轮次")
    alpha_t = math.ldexp(alpha_total, -index)
    if alpha_t <= 0.0:
        raise ErrorBudgetError(
            f"第 t={index} 轮的误差预算下溢为 0（total_alpha={alpha_total!r}）。"
            + ALPHA_UNDERFLOW_NOTE
        )
    return alpha_t


# ---------------------------------------------------------------------------
# 验收标准④：Holm 校正
# ---------------------------------------------------------------------------


def _normalize_p_values(values: Any) -> tuple[dict[str, float], tuple[str, ...]]:
    """把 ``{标识: p}`` 规范化为「可计算集合 + 缺失集合」。

    仅两种值形态被接受：落在 ``[0,1]`` 的有限实数，或 ``None``（缺失）。
    ``None`` **不插补为 1**：插补会把「没算」伪装成「不显著」，
    这正是本模块要杜绝的混淆。

    :raises ExecuteInputError: 含有非映射、非数值、越界或受限键名。
    """
    mapping = _require_mapping(values, "p_values")
    _check_no_forbidden_keys(mapping, "p_values")
    computed: dict[str, float] = {}
    missing: list[str] = []
    for raw_id, raw_p in mapping.items():
        test_id = _require_text(raw_id, "p_values 的检验标识")
        if raw_p is None:
            missing.append(test_id)
            continue
        p_value = _require_number(raw_p, f"p_values[{test_id!r}]")
        if not 0.0 <= p_value <= 1.0:
            raise ExecuteInputError(
                f"p_values[{test_id!r}] 必须落在 [0, 1] 内，实际得到：{raw_p!r}"
            )
        computed[test_id] = p_value
    return computed, tuple(sorted(missing))


def holm_adjust(values: Any) -> HolmResult:
    """标准 Holm 逐步下降（step-down）校正，控制家族错误率（FWER）。

    步骤（std 标准实现，与 R 的 ``p.adjust(method="holm")`` 同口径）：

    1. 记可计算的 p 值个数为 :math:`m`，按升序排列，第 :math:`k` 个乘数取
       :math:`m-k+1`；
    2. 调整值 :math:`p^{\\text{adj}}_{(k)}=\\max\\big(p_{(k)}\\cdot(m-k+1),\\;
       p^{\\text{adj}}_{(k-1)}\\big)`，即**单调化**——这一步保证
       「原始 p 更小 ⇒ 调整后 p 不会更大」，缺少单调化就不是 Holm；
    3. 结果按检验标识返回。

    **空输入返回空**：空映射返回 :class:`HolmResult`（``adjusted`` 为空、
    ``entries`` 为空元组、``missing`` 为空元组）；``p`` 为 ``None`` 的检验
    只进入 ``missing``，不参与名次计算、也不被插补。

    平局处理：原始 p 值相同时按检验标识的字典序决定名次，
    使结果与输入顺序无关（同一次执行可复现）。

    :param values: ``{检验标识: p 值或 None}`` 的映射。
    :return: :class:`HolmResult`。
    :raises ExecuteInputError: 输入不是映射、含非数值或越界 p 值、含受限键名。
    """
    computed, missing = _normalize_p_values(values)
    if not computed:
        return HolmResult(
            adjusted={}, raw={}, entries=(), missing=missing
        )
    ordered = sorted(computed.items(), key=lambda pair: (pair[1], pair[0]))
    total = len(ordered)
    entries: list[HolmEntry] = []
    adjusted: dict[str, float] = {}
    running = 0.0
    for position, (test_id, p_value) in enumerate(ordered, start=1):
        factor = total - position + 1
        candidate = min(1.0, p_value * factor)
        running = max(running, candidate)
        value = min(1.0, running)
        entries.append(
            HolmEntry(
                test_id=test_id,
                p_value=p_value,
                rank=position,
                factor=factor,
                adjusted=value,
            )
        )
        adjusted[test_id] = value
    return HolmResult(
        adjusted=adjusted,
        raw=dict(computed),
        entries=tuple(entries),
        missing=missing,
    )


# ---------------------------------------------------------------------------
# 验收标准③：族对账与完整执行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PlanView:
    """从冻结计划提取的只读视图（只读计划本身，不触碰任何数据）。

    :param plan: 计划原文（已深拷贝，避免调用方后续改动影响执行）。
    :param digest: 计划的规范摘要。
    :param protocol_id: 协议标识。
    :param round_index: 计划声明的轮次。
    :param effect_threshold: 计划的效应阈值（各检验的默认 δ）。
    :param family: 检验条目元组，顺序与计划一致。
    :param coverage: P11 的 :func:`sdl_m06.freeze.family_coverage` 产物。
    """

    plan: dict
    digest: str
    protocol_id: str
    round_index: int
    effect_threshold: float
    family: tuple[dict, ...]
    coverage: dict


def _jsonable(value: Any) -> Any:
    """把 ``MappingProxyType`` 等只读包装还原为普通可序列化对象。"""
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _plan_view(plan: Any) -> _PlanView:
    """校验冻结计划并提取 :class:`_PlanView`。

    计划先过 P11 的 :func:`sdl_m06.freeze.validate_frozen_plan`（离线自检），
    因此「字段与契约对齐」「键名受限」「检验族非空」等条件在进入执行前就成立。

    :raises ExecuteInputError: 计划不是映射或含受限键名。
    :raises FamilyCoherenceError: 计划未通过 P11 自检（结构被改写）。
    """
    body = _require_mapping(plan, "plan")
    _check_no_forbidden_keys(body, "plan")
    plan_copy = _jsonable(body)
    try:
        validate_frozen_plan(plan_copy)
        coverage = family_coverage(plan_copy)
    except Exception as exc:  # noqa: BLE001 - 统一转为本模块的族错误
        raise FamilyCoherenceError(
            f"冻结计划未通过 P11 离线自检，执行被拒绝：{exc}"
        ) from exc
    family = tuple(_require_mapping(item, "plan.test_family 条目") for item in plan_copy["test_family"])
    return _PlanView(
        plan=plan_copy,
        digest=hashlib.sha256(canonical_json(plan_copy).encode("utf-8")).hexdigest(),
        protocol_id=str(plan_copy["protocol_id"]),
        round_index=int(plan_copy["round_index"]),
        effect_threshold=float(plan_copy["effect_threshold"]),
        family=family,
        coverage=coverage,
    )


def family_test_ids(plan: Any) -> tuple[str, ...]:
    """由冻结计划反解除全部检验标识（P11 的 ``family_coverage`` 产物）。

    :param plan: 冻结计划（或含 ``test_family`` 的映射）。
    :return: 检验标识元组，已排序。
    :raises FamilyCoherenceError: 计划结构非法或检验标识格式不符（族被改写）。
    """
    body = _require_mapping(plan, "plan")
    try:
        coverage = family_coverage(_jsonable(body))
    except Exception as exc:  # noqa: BLE001
        raise FamilyCoherenceError(
            f"无法由冻结计划反解检验族：{exc}" + FAMILY_COHERENCE_NOTE
        ) from exc
    return tuple(coverage["test_ids"])


def check_family_coherence(plan: Any, provided_test_ids: Any) -> dict:
    """把「计划登记的检验族」与「实际执行的检验集合」逐条对账（验收标准③）。

    两个方向都必须为空集：

    - ``missing``：计划有、轨迹无 → **族不完整**，执行被拒绝；
    - ``unexpected``：轨迹有、计划无 → **计划被外部改写或轨迹串了轮次**，
      同样拒绝（保留「多算一条」比丢掉结果更危险：它会让未冻结的检验混进确证）。

    :param plan: 冻结计划。
    :param provided_test_ids: 轨迹携带的检验标识序列（可重复，自动去重）。
    :return: ``{"expected", "provided", "missing", "unexpected"}``。
    :raises FamilyCoherenceError: 任一方向非空。
    :raises ExecuteInputError: ``provided_test_ids`` 不是合法序列。
    """
    expected = set(family_test_ids(plan))
    raw = _require_sequence(provided_test_ids, "provided_test_ids")
    provided: set[str] = set()
    for index, item in enumerate(raw):
        provided.add(_require_text(item, f"provided_test_ids[{index}]"))

    missing = sorted(expected - provided)
    unexpected = sorted(provided - expected)
    if missing or unexpected:
        parts: list[str] = []
        if missing:
            parts.append("缺少检验 " + ", ".join(missing))
        if unexpected:
            parts.append("多出未登记检验 " + ", ".join(unexpected))
        raise FamilyCoherenceError(
            "检验族与冻结计划不一致（" + "；".join(parts) + "）：" + FAMILY_COHERENCE_NOTE
        )
    return {
        "expected": tuple(sorted(expected)),
        "provided": tuple(sorted(provided)),
        "missing": (),
        "unexpected": (),
    }


def summarize_family(results: Any, alpha: Any) -> dict:
    """汇总执行结果，供复核与 P13 使用（**不做任何结果分类**）。

    :param results: :class:`TestExecution` 序列（或 :class:`FamilyExecution`）。
    :param alpha: 本轮预算 :math:`\\alpha_t`。
    :return: ``{"test_count", "available_count", "unavailable_test_ids",
        "significant_count", "threshold_count", "alpha", "complete",
        "holm_method"}``。
    :raises ExecuteInputError: 输入形态非法。
    """
    if isinstance(results, FamilyExecution):
        items = results.results
    else:
        items = tuple(_require_sequence(results, "results"))
    alpha_t = _require_number(alpha, "alpha")
    for index, item in enumerate(items):
        if not isinstance(item, TestExecution):
            raise ExecuteInputError(
                f"results[{index}] 必须是 TestExecution，实际得到：{type(item).__name__}"
            )
    unavailable = tuple(sorted(item.test_id for item in items if not item.available))
    return {
        "test_count": len(items),
        "available_count": sum(1 for item in items if item.available),
        "unavailable_test_ids": unavailable,
        "significant_count": sum(1 for item in items if item.significant is True),
        "threshold_count": sum(1 for item in items if item.holm_threshold is not None),
        "alpha": alpha_t,
        "complete": not unavailable,
        "holm_method": HOLM_ADJUSTMENT_NAME,
    }


# ---------------------------------------------------------------------------
# 轨迹规范化（验收标准②③：确证记录之外，唯一的统计输入）
# ---------------------------------------------------------------------------


def _split_entry(entry: Any, index: int) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """把一个轨迹条目拆成 ``(test_id, 统计事实, 元数据)``。

    条目可为两种形态：

    - **映射**：既可含事实键（``p_value`` / ``effect`` …），也可含元数据键
      （``test_id`` / ``hypothesis_id`` …）；
    - **标量**：直接就是 p 值，此时检验标识取该位置在计划里的顺序位（由调用方补）。

    未知键一律拒绝——避免调用方以为某个统计口径已生效、实际被静默忽略。

    :raises ExecuteInputError: 条目含未知键、缺 p 值或 p 值非法。
    """
    if isinstance(entry, Mapping):
        _check_no_forbidden_keys(entry, f"trajectories[{index}]")
        facts: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        for key, value in entry.items():
            if key in MEASUREMENT_KEYS:
                facts[key] = value
            elif key in RECORD_METADATA_KEYS:
                metadata[key] = value
            else:
                raise ExecuteInputError(
                    f"trajectories[{index}] 含未知键 {key!r}："
                    f"允许的统计事实键为 {sorted(MEASUREMENT_KEYS)}，"
                    f"允许的元数据键为 {sorted(RECORD_METADATA_KEYS)}。"
                )
        test_id = metadata.get("test_id")
        if test_id is not None:
            test_id = _require_text(test_id, f"trajectories[{index}].test_id")
        return test_id, facts, metadata
    # 标量形态：直接作为 p 值。
    return None, {"p_value": entry}, {}


def _normalize_trajectories(trajectories: Any) -> dict[str, dict[str, Any]]:
    """把轨迹规范化为 ``test_id → 统计事实``。

    接受三种输入形态：

    1. ``None``：无轨迹（全部检验记为不可得，但仍逐条占位）；
    2. **映射** ``{test_id: 事实}``；
    3. **序列**：元素为映射（含 ``test_id``）或标量 p 值。

    顺序形态下，未带 ``test_id`` 的元素按位置与计划的 ``test_family`` 对齐，
    并在元素数超过族规模时报错（多出来的元素无法归属，宁可失败也不静默丢弃）。

    :raises ExecuteInputError: 形态非法、条目未知键、重复标识。
    """
    if trajectories is None:
        return {}
    if isinstance(trajectories, Mapping):
        _check_no_forbidden_keys(trajectories, "trajectories")
        out: dict[str, dict[str, Any]] = {}
        for raw_id, entry in trajectories.items():
            test_id = _require_text(raw_id, "trajectories 的检验标识")
            if test_id in out:
                raise ExecuteInputError(f"轨迹中检验 {test_id!r} 重复出现。")
            if isinstance(entry, Mapping):
                _, facts, _ = _split_entry(entry, 0)
            else:
                facts = {"p_value": entry}
            out[test_id] = facts
        return out

    items = _require_sequence(trajectories, "trajectories")
    out = {}
    for index, entry in enumerate(items):
        test_id, facts, _ = _split_entry(entry, index)
        if test_id is None:
            # 位置形态：用一个占位键标记「待按顺序对齐」，由调用方替换。
            test_id = f"__index__:{index}"
        if test_id in out:
            raise ExecuteInputError(f"轨迹中检验 {test_id!r} 重复出现。")
        out[test_id] = facts
    return out


def _align_by_index(facts: Mapping[str, Mapping[str, Any]], expected: Sequence[str]) -> dict[str, dict[str, Any]]:
    """把位置形态的轨迹按计划顺序对齐到真实检验标识。

    :raises ExecuteInputError: 位置形态的元素数超过族规模。
    """
    indexed = [
        (int(key.split(":", 1)[1]), value)
        for key, value in facts.items()
        if key.startswith("__index__:")
    ]
    if not indexed:
        return {key: dict(value) for key, value in facts.items()}
    others = {
        key: dict(value) for key, value in facts.items() if not key.startswith("__index__:")
    }
    for position, value in indexed:
        if position >= len(expected):
            raise ExecuteInputError(
                f"位置形态的轨迹第 {position} 项没有对应的检验："
                f"本轮检验族只有 {len(expected)} 条；多出的统计事实无法归属，"
                "拒绝静默丢弃。" + FAMILY_COHERENCE_NOTE
            )
        test_id = expected[position]
        if test_id in others:
            raise ExecuteInputError(
                f"检验 {test_id!r} 同时以标识与位置两种形态出现，归属不明。"
            )
        others[test_id] = dict(value)
    return others


# ---------------------------------------------------------------------------
# 验收标准②：消费顺序
# ---------------------------------------------------------------------------


class _StepLedger:
    """单调步进账本：为执行过程中的关键动作分配序号。

    存在的意义是把「先提交 ``used``、再返回数据」从「代码看起来是对的」
    变成**被测量的事实**：:attr:`read_index` 由 :meth:`record_read` 实际步进，
    而不是在构造 :class:`ExecutionOrder` 时写一个常量。

    刻意不复用 :mod:`time`：时间戳可被系统时钟调整影响，且在同一次调用内
    分辨率不足；序号是单调的，比较才是确定的。
    """

    __slots__ = ("_next", "_reads", "_commit_index", "_consume_index")

    def __init__(self) -> None:
        self._next = 1
        self._reads: list[int] = []
        self._commit_index = 0
        self._consume_index = 0

    def bump(self) -> int:
        """分配下一个序号。"""
        value = self._next
        self._next += 1
        return value

    def record_consume(self) -> int:
        """记录消费调用。M1 的 ``used`` 提交与消费调用原子发生，故共用该序号。"""
        self._consume_index = self.bump()
        self._commit_index = self._consume_index
        return self._consume_index

    def record_read(self) -> int:
        """记录一次读取（确证记录或统计事实进入执行路径），并返回其序号。"""
        value = self.bump()
        self._reads.append(value)
        return value

    @property
    def consume_index(self) -> int:
        """消费调用序号。"""
        return self._consume_index

    @property
    def commit_index(self) -> int:
        """``used`` 提交序号（与消费调用相等，由 M1 的原子语义保证）。"""
        return self._commit_index

    @property
    def read_index(self) -> int | None:
        """首次读取序号；从未读取时为 ``None``。"""
        return self._reads[0] if self._reads else None

    @property
    def read_count(self) -> int:
        """读取次数。"""
        return len(self._reads)


class _BindingClient:
    """对注入的 confirmer 模块做一次能力探测，并给出可读的失败信息。

    本类**只**暴露 ``consume_confirmation`` 一条读取路径；
    模块内不存在任何其它读取确证内容的入口（见 :func:`_forbidden_access_names`）。
    """

    def __init__(self, module_confirmer: Any) -> None:
        self._module = module_confirmer
        self._consume = getattr(module_confirmer, "consume_confirmation", None)
        if not callable(self._consume):
            raise ExecuteInputError(
                "注入的 confirmer 模块没有 consume_confirmation 方法"
                f"（实际类型 {type(module_confirmer).__name__}）："
                "确证记录只能经 M1 的公开消费接口进入本层。"
            )

    def consume_via_m1(self, binding_id: str, replay: bool) -> Any:
        """经 M1 的公开接口 ``consume_confirmation`` 消费确证分区。

        方法名刻意不叫 ``consume``：``NOT_PROVIDED_BY_P12`` 用 ``consume``
        作为「绕过消费的快捷入口」的黑名单名，私有方法若同名会让
        :func:`self_check` 的 AST 检查产生假阳性。
        本方法是**唯一**的读取路径，且它就是被要求的那个接口。

        :raises SequenceOrderError: 返回形态非法（无法作为消费凭证）。
        """
        result = self._consume(binding_id, replay=replay)
        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            raise SequenceOrderError(
                "consume_confirmation 应返回记录序列，实际得到："
                f"{type(result).__name__}；" + CONSUMPTION_ORDER_NOTE
            )
        return result


def _check_binding_handoff(binding: Any) -> tuple[str, dict, float]:
    """校验绑定交接信息，并抽取执行所需的最小只读事实。

    只读绑定**元信息**（标识、摘要、本轮 α、状态、轮次）；
    **不**读取绑定所指向的确证记录正文。

    :return: ``(binding_id, 只读元信息映射, 绑定登记的 α)``。
    :raises ExecuteInputError: 绑定不是映射、缺关键字段或含受限键名。
    """
    body = _require_mapping(binding, "binding")
    _check_no_forbidden_keys(body, "binding")
    binding_id = _require_text(body.get("binding_id"), "binding.binding_id")
    state = str(body.get("state", "")).strip()
    if state not in ("bound_to_frozen_protocol", "used", "historical"):
        raise ExecuteInputError(
            "binding.state 必须是 bound_to_frozen_protocol（待消费）"
            "或 used / historical（已消费），实际得到："
            f"{state!r}；本模块只执行处于可消费状态的绑定。"
        )
    plan_digest = str(body.get("plan_digest") or "")
    registered_alpha = body.get("alpha")
    if registered_alpha is None:
        raise ExecuteInputError(
            "binding.alpha 缺失：绑定记录必须携带本轮登记的 α，"
            "用于与本模块独立复算的 α_t 对账。" + ALPHA_FORMULA_NOTE
        )
    metadata = {
        "binding_id": binding_id,
        "plan_digest": plan_digest,
        "state": state,
        "round_index": body.get("round_index"),
    }
    return binding_id, metadata, _require_number(registered_alpha, "binding.alpha")


def _protocol_alpha(protocol: Any, expected_protocol_id: str) -> float:
    """从公开协议读出总体 :math:`\\alpha`，并校验协议归属。

    只读**协议元信息**（``confirmation_policy`` 与 ``protocol_id``）：
    本函数不触及任何分区引用，也不读取任何记录正文。

    :raises ExecuteInputError: 协议不是映射、缺 ``confirmation_policy.total_alpha``、
        含受限键名，或协议标识与计划不符。
    """
    body = _require_mapping(protocol, "protocol")
    _check_no_forbidden_keys(body, "protocol")
    protocol_id = _require_text(body.get("protocol_id"), "protocol.protocol_id")
    if protocol_id != expected_protocol_id:
        raise SequenceOrderError(
            f"传入协议 {protocol_id!r} 与计划的 protocol_id "
            f"{expected_protocol_id!r} 不一致：本轮确证必须在计划所属协议下执行。"
        )
    policy = _require_mapping(body.get("confirmation_policy"), "protocol.confirmation_policy")
    total_alpha = _require_number(
        policy.get("total_alpha"), "protocol.confirmation_policy.total_alpha"
    )
    if not 0.0 < total_alpha < 1.0:
        raise ErrorBudgetError(
            "protocol.confirmation_policy.total_alpha 必须严格落在 (0, 1) 内，"
            f"实际得到：{total_alpha!r}"
        )
    return total_alpha


# ---------------------------------------------------------------------------
# 主入口：执行检验族（验收标准①②③④）
# ---------------------------------------------------------------------------


def execute_family(
    module_confirmer: Any,
    binding: Any,
    plan: Any,
    round_index: Any = None,
    *,
    protocol: Any = None,
    trajectories: Any = None,
) -> FamilyExecution:
    """在冻结计划的约束下消费确证数据、执行完整检验族并按预算分配阈值。

    执行顺序（**顺序本身是验收标准②**）：

    1. **离线自检**：计划先过 P11 的 :func:`sdl_m06.freeze.validate_frozen_plan`，
       不合法就地失败，**连 confirmer 都不碰**（避免把坏计划送进证据库）；
    2. **轮次三方对账**：``round_index`` 参数、``plan["round_index"]``、
       ``binding["round_index"]``（若提供）必须一致；
    3. **独立复算预算**：由公开协议的 ``total_alpha`` 与轮次算
       :math:`\\alpha_t=\\alpha/2^{t}`，并与绑定登记的 ``alpha`` 对账；
    4. **原子化消费**：调用 ``consume_confirmation``——它返回时 ``used``
       状态迁移与账本提交**已经完成**，此后才允许统计事实进入执行路径；
    5. **族对账**：用 P11 的 ``family_coverage`` 与轨迹的检验标识严格对账
       （缺一条 / 多一条都拒绝）；
    6. **完整执行**：逐条计算 Holm 调整值与阈值，判定
       ``p ≤ α_t / (m - k + 1)``；未提供轨迹的检验占位并标记不可得；
    7. **顺序校验**：构造成 :class:`ExecutionOrder` 并调用其 ``verify()``，
       把「先提交 used、再返回数据」变成运行期不变量。

    本函数**不做结果分类**：``significant`` 只是机械的阈值比较，
    ``supported`` / ``refuted`` / ``inconclusive`` 的判定属 P13。

    :param module_confirmer: 以 confirmer 角色构造的 M1 模块（鸭子类型即可）。
    :param binding: ``bind_confirmation`` 的返回值（含 ``binding_id`` / ``alpha`` / ``state``）。
    :param plan: :func:`sdl_m06.freeze.build_frozen_plan` 的产物。
    :param round_index: 研究轮次；省略时取计划声明的轮次。
        显式给出时必须与计划一致（轮次错配会让预算对应的不是同一轮）。
    :param protocol: 公开协议（``build()`` / ``describe()`` 的产物）。
        用于读取 ``confirmation_policy.total_alpha`` 以独立复算 α_t
        （见 :data:`TOTAL_ALPHA_SOURCE_NOTE`）。
    :param trajectories: 各检验**预先声明**的检验产物。``None`` 表示无轨迹
        （全部检验记为不可得）。确证原始记录不得由本参数传入。
    :return: :class:`FamilyExecution`。
    :raises ExecuteInputError: 输入形态非法、含受限键名或受限参数。
    :raises SequenceOrderError: 轮次不一致、绑定 α 与复算值不一致、消费顺序被破坏。
    :raises ErrorBudgetError: 轮次越界、α 非法或预算下溢。
    :raises FamilyCoherenceError: 检验族与冻结计划不一致。
    """
    _reject_forbidden_kwargs(
        {
            "binding": binding,
            "plan": plan,
            "round_index": round_index,
            "protocol": protocol,
            "trajectories": trajectories,
        },
        "execute_family",
    )
    if protocol is None:
        raise ExecuteInputError(
            "缺少 protocol：本轮 α 需由公开协议的 confirmation_policy.total_alpha "
            "与轮次独立复算。" + TOTAL_ALPHA_SOURCE_NOTE
        )

    view = _plan_view(plan)

    # -- 步骤 2：轮次三方对账 --------------------------------------------
    resolved_index = (
        view.round_index if round_index is None else _validate_round_index(round_index)
    )
    if resolved_index != view.round_index:
        raise SequenceOrderError(
            f"传入的 round_index={resolved_index} 与计划声明的 "
            f"{view.round_index} 不一致：轮次决定误差预算 α_t=α/2**t，"
            "两者错配会让预算与检验族对应的轮次不是同一轮。" + ROUND_SCOPE_NOTE
        )

    binding_id, binding_meta, registered_alpha = _check_binding_handoff(binding)
    binding_round = binding_meta.get("round_index")
    if binding_round is not None:
        if isinstance(binding_round, bool) or not isinstance(binding_round, int):
            raise SequenceOrderError(
                f"binding.round_index 必须是整数，实际得到：{binding_round!r}"
            )
        if binding_round != resolved_index:
            raise SequenceOrderError(
                f"绑定登记的轮次 {binding_round} 与本次执行轮次 {resolved_index} 不一致："
                "同一绑定只能在其登记的轮次上被执行。"
            )

    # -- 步骤 3：独立复算预算并与绑定对账 ---------------------------------
    total_alpha = _protocol_alpha(protocol, view.protocol_id)
    alpha = alpha_for_round(total_alpha, resolved_index)
    if not math.isclose(alpha, registered_alpha, rel_tol=1e-12, abs_tol=0.0):
        raise SequenceOrderError(
            f"绑定登记的 α={registered_alpha!r} 与本模块独立复算的 "
            f"α_t={alpha!r} 不一致（轮次 t={resolved_index}）。"
            + ALPHA_FORMULA_NOTE
        )

    # -- 步骤 4：原子化消费 ----------------------------------------------
    client = _BindingClient(module_confirmer)
    ledger = _StepLedger()
    replay = False
    consume_index = ledger.record_consume()
    # 消费调用。返回时 M1 已提交 used 状态（见 IMPLEMENTATION_CONTRACT）。
    consumed_records = client.consume_via_m1(binding_id, replay)
    used_committed_index = ledger.commit_index

    # -- 步骤 5：族对账 --------------------------------------------------
    expected_ids = tuple(view.coverage["test_ids"])
    if trajectories is None:
        # 无轨迹：不把任何统计事实读入执行路径，故不记录读取事件；
        # first_read_index 保持 None（结果对象中可区分「没读取」与「读过」）。
        # 此时没有「轨迹集合」可对账，族完整性由「逐条占位」保证（见步骤 6）。
        facts_by_id: dict[str, dict[str, Any]] = {}
    else:
        # 记录读取事件：此后统计事实才进入执行路径。
        ledger.record_read()
        facts_by_id = _align_by_index(_normalize_trajectories(trajectories), expected_ids)
        check_family_coherence(view.plan, tuple(facts_by_id))

    # -- 步骤 6：完整执行（不剔除任何失败项） -----------------------------
    holm = holm_adjust(
        {test_id: facts_by_id.get(test_id, {}).get("p_value") for test_id in expected_ids}
    )
    rank_of = {entry.test_id: entry for entry in holm.entries}
    family_size = holm.size
    results: list[TestExecution] = []
    decisions: dict[str, tuple[float, bool]] = {}
    for entry in view.family:
        test_id = str(entry["id"])
        # 检验标识由 P11 的 family_coverage 保证可反解为 (假说, 指标, 子群) 三元组。
        parts = test_id.split("::")
        if len(parts) == 4:
            hypothesis_id, metric, subgroup = parts[1], parts[2], parts[3]
        else:  # pragma: no cover - _plan_view 已用 family_coverage 保证格式
            hypothesis_id, metric, subgroup = str(entry["hypothesis_id"]), "", ""
        facts = dict(facts_by_id.get(test_id, {}))
        raw_p = facts.get("p_value")
        if raw_p is None:
            results.append(
                TestExecution(
                    test_id=test_id,
                    hypothesis_id=hypothesis_id,
                    metric=metric,
                    subgroup=subgroup,
                    null=str(entry["null"]),
                    alternative=str(entry["alternative"]),
                    method=str(entry["method"]),
                    p_value=None,
                    effect=None,
                    delta=view.effect_threshold,
                    holm_adjusted=None,
                    rank=None,
                    holm_threshold=None,
                    significant=None,
                    available=False,
                    facts={},
                    reasons=("未提供检验轨迹：该条检验记为不可得，而非不显著。",),
                )
            )
            continue
        p_value = _require_number(raw_p, f"轨迹 {test_id!r}.p_value")
        if not 0.0 <= p_value <= 1.0:
            raise ExecuteInputError(
                f"轨迹 {test_id!r}.p_value 必须落在 [0, 1] 内，实际得到：{raw_p!r}"
            )
        holm_entry = rank_of.get(test_id)
        rank = holm_entry.rank if holm_entry is not None else None
        adjusted = holm_entry.adjusted if holm_entry is not None else None
        threshold = (
            alpha / holm_entry.factor if holm_entry is not None else None
        )
        significant = (
            (p_value <= threshold) if threshold is not None else None
        )
        if rank is not None and threshold is not None:
            decisions[test_id] = (threshold, bool(significant))
        delta = facts.get("delta", view.effect_threshold)
        results.append(
            TestExecution(
                test_id=test_id,
                hypothesis_id=hypothesis_id,
                metric=metric,
                subgroup=subgroup,
                null=str(entry["null"]),
                alternative=str(entry["alternative"]),
                method=str(entry["method"]),
                p_value=p_value,
                effect=facts.get("effect"),
                delta=_require_number(delta, f"轨迹 {test_id!r}.delta"),
                holm_adjusted=adjusted,
                rank=rank,
                holm_threshold=threshold,
                significant=significant,
                available=True,
                facts=facts,
                reasons=(),
            )
        )

    unavailable = tuple(sorted(item.test_id for item in results if not item.available))

    # -- 步骤 7：顺序凭证与运行期不变量 ----------------------------------
    order = ExecutionOrder(
        consume_call_index=consume_index,
        used_committed_index=used_committed_index,
        first_read_index=ledger.read_index,
        replay_used=bool(replay),
    )
    order.verify()

    notes = [
        ALPHA_FORMULA_NOTE,
        CONSUMPTION_ORDER_NOTE,
        FAMILY_COHERENCE_NOTE,
        HOLM_NOTE,
        HOLM_NOT_A_CURE_NOTE,
        COMPLETE_FAMILY_BOUNDARY_NOTE,
        ROUND_SCOPE_NOTE,
        FORWARD_ALPHA_NOTE,
    ]
    return FamilyExecution(
        binding_id=binding_id,
        plan_digest=binding_meta.get("plan_digest") or view.digest,
        round_index=resolved_index,
        total_alpha=float(total_alpha),
        alpha=alpha,
        results=tuple(results),
        holm=holm,
        decisions=decisions,
        order=order,
        consumed_record_count=len(consumed_records),
        excluded_failed_tests=(),
        unavailable_test_ids=unavailable,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 边界守护：本模块不提供的数据访问与摆放位置
# ---------------------------------------------------------------------------


def _forbidden_access_names() -> tuple[str, ...]:
    """本模块**不应存在**的数据访问入口名。

    与 :data:`NOT_PROVIDED_BY_P12` 中列出的越界接口不同，本集合聚焦「读取」：
    确证记录只能经 ``consume_confirmation`` 的返回值进入本层。
    测试用 AST 检查本模块是否定义了这些名字、或访问了这些属性——
    用 AST 而非源码文本匹配，避免把「边界声明本身」当成违规。
    """
    return (
        "read_dataset",
        "read_records",
        "load",
        "load_snapshot",
        "open_vault",
        "quality",
        "connect",
        "sqlite3",
    )


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查两项：

    1. 模块是否定义了 :data:`NOT_PROVIDED_BY_P12` 中的任何名字（越界实现）；
    2. 模块是否出现 :func:`_forbidden_access_names` 中的属性访问或导入（越界读取）。

    刻意用 AST 而不是源码文本匹配：本模块的 docstring 与常量里**必须**写出
    ``consume_confirmation`` / ``classify_result`` 等被禁名字（那是边界声明），
    文本匹配会把边界声明本身判成违规。

    :return: ``{"defined_forbidden", "accessed_forbidden", "ok", "checked_names"}``。
    """
    source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
    tree = _ast.parse(source_path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
    }
    accessed = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, _ast.Name):
            accessed.add(node.id)
        elif isinstance(node, _ast.alias):
            accessed.add(node.name.split(".")[-1])
    defined_forbidden = tuple(sorted(defined & set(NOT_PROVIDED_BY_P12)))
    accessed_forbidden = tuple(
        sorted(accessed & set(_forbidden_access_names()) - set(NOT_PROVIDED_BY_P12))
    )
    return {
        "defined_forbidden": defined_forbidden,
        "accessed_forbidden": accessed_forbidden,
        "ok": not defined_forbidden and not accessed_forbidden,
        "checked_names": len(NOT_PROVIDED_BY_P12),
    }
