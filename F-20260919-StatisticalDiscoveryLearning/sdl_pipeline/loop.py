"""P16 端到端主循环集成。

本模块把 M2–M8 串成《SDL算法框架说明》§5 的主循环，并提供一个合成任务上的
集成 demo（``Y = a + b·(X1/X2) + ε``，见同文档 §8）。

主循环的每一步都**只调用**上游模块的公开入口，本层不重算任何上游口径：

===========================  ====================================================
主循环步骤                    本层调用的上游入口
===========================  ====================================================
构造表示 ``R``                ``sdl_m02.represent.build_representation``
搜索模式 ``P``                ``sdl_m03.equations.fit_relation`` /
                              ``sdl_m03.stability.resample_evaluate`` /
                              ``sdl_m03.structure.check_invariant``
                              （可选：``find_clusters`` / ``find_changepoints``）
生成假说 ``H_pool``           ``sdl_m04.generate.hypotheses_from_patterns``
开发评估与筛选 ``H_batch``    ``sdl_m05.metrics.evaluate_hypothesis`` +
                              ``sdl_m05.select.select_freeze_candidates``
冻结 ``F_t``                  ``sdl_m06.freeze.build_frozen_plan`` + ``bind``
执行确证 ``E_t``              ``sdl_m06.execute.execute_family``
结果三分类与发布              ``sdl_m06.report.classify_result`` +
                              ``record_and_release``
下一轮取证计划                ``sdl_m07.acquisition.divergence`` +
                              ``acquisition_plan``
归档与知识库版本              ``sdl_m08.archive.archive_round`` +
                              ``update_knowledge_version`` +
                              ``revise_or_retire``
===========================  ====================================================

本层**不做**的事（对应 ``NOT_PROVIDED_BY_P16``）：

1. **不做统计推断。** p 值由**注入的**外部评估器给出（``confirmation_evaluator``），
   本层只按冻结计划执行检验族并把结果登记进证据库。默认评估器恒返回「不可得」，
   使本轮结论落在「证据不足」——这是诚实的缺省，而不是失败。
2. **不授予证据等级、不做因果升级。** 等级与因果标签完全由
   ``sdl_m08.archive`` 机械判定；本层只转录其输出。
3. **不修改冻结后的预处理与候选。** 冻结快照（:func:`freeze_snapshot`）在
   ``bind`` 之前取定，执行与归档之后再复算比对（:func:`frozen_unchanged`），
   把「冻结后不再改预处理与候选」变成运行期可校验的事实。
4. **不读取受限内容。** 探索侧只经 ``explorer.read_dataset(E)``；确证侧只经
   ``execute_family`` 内部的 ``consume_confirmation``；本层不触碰
   ``quality()``、账本、令牌或任何封存原始记录。

验收标准 ①「含预算与停止条件」由 :class:`LoopBudget` 与 :class:`StopDecision`
承担；②「冻结后预处理与候选不变」由冻结快照承担；③「产出发现档案 / 知识库版本 /
反例与证据不足清单」由 :class:`DiscoveryArchive` 承担；④「demo 明确标注为合成
验证」由 :data:`SYNTHETIC_DEMO_NOTICE` 与 :func:`run_demo` 的产物承担；
⑤「全量测试通过」由 ``tests/test_loop.py`` 与门禁脚本承担。

本模块的最终输出 :class:`DiscoveryArchive` **必然包含**「仍缺证据的问题清单」
（``open_questions``）：每条给出假说、当前结论、缺什么证据、以及什么动作能补上。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 上游公开入口（只导入入口，不重新实现任何上游口径）
# ---------------------------------------------------------------------------

from sdl_m02.expressions import to_canonical_json as expression_canonical_json
from sdl_m02.represent import Budget as RepresentationBudget
from sdl_m02.represent import build_representation

from sdl_m03.equations import (
    ExplorationSample,
    RelationFit,
    fit_relation,
    prepare_sample,
)
from sdl_m03.stability import StabilityReport, resample_evaluate
from sdl_m03.structure import (
    check_invariant,
    find_changepoints,
    find_clusters,
)

from sdl_m04.generate import GeneratedHypothesisPool, hypotheses_from_patterns
from sdl_m04.hypothesis import Hypothesis, HypothesisStore

from sdl_m05.metrics import Evaluation, KnowledgeBase, evaluate_hypothesis
from sdl_m05.select import FreezeSelection, select_freeze_candidates

from sdl_m06.execute import FamilyExecution, execute_family, family_test_ids
from sdl_m06.freeze import (
    bind,
    build_frozen_plan,
    canonical_json as freeze_canonical_json,
)
from sdl_m06.report import (
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    RoundClassification,
    classify_result,
    record_and_release,
)

from sdl_m07.acquisition import (
    CONFIRMATION_GRADE_PURPOSES,
    PURPOSE_COUNTEREXAMPLE,
    PURPOSE_EXPLORATION,
    DivergenceReport,
    acquisition_plan,
    divergence,
)

from sdl_m08.archive import (
    ARCHIVE_VERSION,
    GRADE_E0,
    GRADE_E1,
    GRADE_E2,
    KnowledgeVersion,
    RoundArchive,
    RevisionDecision,
    archive_round,
    empty_knowledge,
    revise_or_retire,
    update_knowledge_version,
)

# ---------------------------------------------------------------------------
# 版本与边界声明
# ---------------------------------------------------------------------------

#: 本模块的版本标识（用于结果对象的可追溯字段）。
LOOP_VERSION = "P16-v1.0"

#: 合成 demo 的强制标注。任何 demo 产物都必须携带它。
SYNTHETIC_DEMO_NOTICE = (
    "本产物来自**合成数据上的算法验证**，不是现实发现结果。"
    "所用数据由代码按已知公式生成，因此「恢复结构」只说明实现与协议链路可运行，"
    "既不构成因果证据，也不构成任何真实领域的新理论。"
)

#: 主循环的模块级边界说明，随结果对象一并输出。
LOOP_BOUNDARY_NOTE = (
    "本层是 M2–M8 的集成与调度：统计推断由注入的外部评估器完成，"
    "证据等级与因果标签由 M8 机械判定。本层不授予等级、不做因果升级、"
    "不修改冻结后的预处理与候选，也不接触任何受限内容。"
)

#: 预算与停止条件的说明。
STOPPING_RULE_NOTE = (
    "停止条件在启动时由 :class:`LoopBudget` 声明并可机械复现：轮次用尽、"
    "确证数据用尽、连续无合格候选、或资源预算耗尽。停止**不**意味着其余假说为假；"
    "最终输出必须列出哪些问题仍缺证据（见 ``DiscoveryArchive.open_questions``）。"
)

#: 缺省评估器的行为说明。
UNAVAILABLE_EVALUATOR_NOTE = (
    "缺省评估器不产生任何检验产物：所有检验记为不可得，"
    "本轮结论落在「证据不足」。这样「没算」与「算出来不显著」在档案里可区分。"
)

#: 主循环**不**提供的接口名（供测试做 AST 级边界检查）。
NOT_PROVIDED_BY_P16: tuple[str, ...] = (
    # 本模块不得自行执行统计检验 / 计算 p 值
    "compute_p_value",
    "execute_statistical_test",
    "run_hypothesis_test",
    # 本模块不得自行判定证据等级或因果
    "grade_for",
    "assign_evidence_grade",
    "promote_to_causal",
    "grant_causal_status",
    # 本模块不得绕过 M1 的公开接口直连存储
    "connect",
    "open_vault",
    "load_snapshot",
    "raw_query",
    "sqlite3",
    "verify_integrity",
    "ledger",
    # 本模块不得重新实现上游已有的检测器
    "select_cluster_count",
    "cluster_labels",
    "holm_adjust",
    "alpha_for_round",
    "pareto_front",
    "top_k_reserved",
)

#: 主循环**不应访问**的受限入口名（供测试做 AST 级边界检查）。
_RESTRICTED_ACCESS_NAMES: tuple[str, ...] = (
    "quality",
    "inspect_confirmation",
    "mark_compromised",
    "load_snapshot",
    "connect",
    "sqlite3",
    "verify_integrity",
    "ledger",
    "authenticate",
)

#: 明确拒绝的关键字参数：等级指定类、统计结论类、令牌类。
FORBIDDEN_CALL_KEYS: frozenset[str] = frozenset(
    {
        "evidence_grade",
        "grade",
        "force_grade",
        "promote_to",
        "causal_grade",
        "causal_claim",
        "upgrade_to_causal",
        "p_value",
        "p_values",
        "significance",
        "supported",
        "refuted",
        "token",
        "role_token",
        "token_body",
        "tokens",
    }
)

# ---------------------------------------------------------------------------
# 轮次状态、停止码与结论词表
# ---------------------------------------------------------------------------

#: 轮次终态：确证已执行并归档。
ROUND_CONFIRMED = "confirmed"
#: 轮次终态：无可用未见确证数据，候选登记为待确证。
ROUND_PENDING_CONFIRMATION = "pending_confirmation"
#: 轮次终态：本轮未筛出任何合格候选。
ROUND_SEARCH_FAILED = "search_failed"
#: 轮次终态：本轮在执行过程中被跳过（例如确证执行失败）。
ROUND_SKIPPED = "skipped"

ROUND_STATUSES: tuple[str, ...] = (
    ROUND_CONFIRMED,
    ROUND_PENDING_CONFIRMATION,
    ROUND_SEARCH_FAILED,
    ROUND_SKIPPED,
)

#: 停止码：未触发停止条件。
STOP_NOT_STOPPED = "not_stopped"
#: 停止码：声明的轮次预算用尽。
STOP_ROUNDS_EXHAUSTED = "rounds_exhausted"
#: 停止码：没有可用的未见确证数据。
STOP_NO_CONFIRMATION_DATA = "no_confirmation_data"
#: 停止码：连续若干轮无合格候选。
STOP_NO_CANDIDATES = "no_candidates"
#: 停止码：整体资源预算（候选拟合 / 枚举尝试）耗尽。
STOP_RESOURCE_BUDGET = "resource_budget_exhausted"

STOP_CODES: tuple[str, ...] = (
    STOP_NOT_STOPPED,
    STOP_ROUNDS_EXHAUSTED,
    STOP_NO_CONFIRMATION_DATA,
    STOP_NO_CANDIDATES,
    STOP_RESOURCE_BUDGET,
)

#: 「仍缺证据」判定所用的结论集合。
OPEN_STATUSES: tuple[str, ...] = (STATUS_INCONCLUSIVE, STATUS_FAILED)

#: 主循环声明的主要指标名（冻结计划与检验族共用同一口径）。
DEFAULT_PRIMARY_METRIC = "batch mean loss gain"

#: 主循环声明的子群（本轮不做亚组展开，只登记 overall，属显式声明）。
DEFAULT_SUBGROUP = "overall"

#: 合成评估器在冻结计划里登记的方法文本。
SYNTHETIC_METHOD_TEXT = (
    "合成演示用批次级单侧检验：按冻结的 inference_unit 聚合到批次，"
    "以批次级损失改善的均值构造统计量，用正态近似给出单侧 p 值；"
    "一次性固定样本，无期中查看。**这是演示装置，不是可推广的统计方法。**"
)

#: 合成评估器的边界声明。
SYNTHETIC_EVALUATOR_NOTE = (
    "该评估器只用于演示主循环控制流：它假设批次间独立、损失改善近似正态、"
    "方差同质，且不做小样本修正。它**不能**用于任何真实研究——"
    "真实使用必须替换为预先声明并经过有效性论证的统计执行器。"
)

#: 缺省候选池容量。
#:
#: 框架说明 §2 建议的原型值是「每轮保留 50 个探索候选」。但在 MVP 的
#: ``Y = a + b·(X1/X2)`` 任务上，比例候选 ``X1/X2`` 在规范式去重后的枚举
#: 序列中位于第 200 位之后，按 50 截断会把它挤出候选池，使「能否恢复结构」
#: 这一验收点变成必然失败。故本模块把默认容量放宽并**显式记录这一偏离**：
#: 它是原型配置的调试结果，不是对框架说明的修改。
DEFAULT_MAX_CANDIDATES = 500

#: 缺省轮次预算。
DEFAULT_MAX_ROUNDS = 3

#: 缺省冻结候选上限（与 P10 的默认值一致）。
DEFAULT_FREEZE_CAP = 5

#: 缺省效应阈值（最小有意义差异）。
#:
#: 冻结计划必须给出**正的**最小有意义差异，否则「检验通过」与「效应可忽略」
#: 无法区分（M6 会直接拒绝 ``effect_threshold <= 0`` 的计划）。此处取 0.1，
#: 与框架说明 §8 的 MVP 合成例一致；它只对本模块的合成 demo 有意义，
#: 不是对任何真实研究效应量的建议。
DEFAULT_EFFECT_THRESHOLD = 0.1

#: 缺省重采样次数。
DEFAULT_N_RESAMPLES = 8

#: 缺省随机种子（确定性复现）。
DEFAULT_SEED = 20260919

#: 确证分区的命名模式。
_CONFIRMATION_REF_RE = re.compile(r"^C([1-9][0-9]*)$")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class LoopError(ValueError):
    """本模块所有异常的基类，继承自 :class:`ValueError`。"""


class LoopInputError(LoopError):
    """输入形态非法（类型、取值或结构不符合约定）。"""


class LoopPolicyError(LoopError):
    """契约违例：传入了本层不得接受的参数或对象。"""


class LoopStateError(LoopError):
    """状态矛盾：冻结不变量被破坏，或轮次推进不合法。"""


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """把只读代理与数据对象递归转成普通 JSON 兼容结构。"""
    if isinstance(value, MappingProxyType) or isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8") if isinstance(value, bytes) else value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (int, bool)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """确定性 JSON 序列化（键排序、无多余空白）。"""
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any, length: int = 16) -> str:
    """对任意可序列化对象取内容摘要。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def _require_nonempty_str(value: Any, label: str) -> str:
    """要求非空字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise LoopInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    """要求正整数（可选允许 0）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoopInputError(f"{label} 必须是整数，实际得到：{value!r}")
    floor = 0 if allow_zero else 1
    if value < floor:
        raise LoopInputError(f"{label} 必须 ≥ {floor}，实际得到：{value!r}")
    return value


def _require_callable_or_none(value: Any, label: str) -> Any:
    """要求可调用对象或 ``None``。"""
    if value is None:
        return None
    if not callable(value):
        raise LoopInputError(f"{label} 必须是可调用对象或 None，实际得到：{value!r}")
    return value


def _reject_forbidden_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝本层不得接受的参数（等级 / 统计结论 / 令牌类）。

    与「未知参数」不同，这里的键是**明确的契约违规**：它们要么会绕过
    M8 的机械判定，要么会把令牌带进本层。宁可报错也不静默忽略。
    """
    seen = sorted(str(key) for key in kwargs if str(key) in FORBIDDEN_CALL_KEYS)
    if seen:
        raise LoopPolicyError(
            f"{where} 不接受以下参数：{seen}。"
            "证据等级只能由 M8 机械判定，统计结论只能来自注入的外部评估器，"
            "令牌不得进入本层的输入或日志。"
        )


# ---------------------------------------------------------------------------
# 预算与停止条件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopBudget:
    """主循环的资源预算与停止阈值。

    所有字段都在**启动前**确定，循环过程中只读；因此「预算与停止条件」
    是可机械复现的，而不是运行期临时判断。

    :param max_rounds: 研究轮次上限（与误差预算的轮次 :math:`t` 一一对应）。
    :param max_candidates: 每轮表示构造保留的候选池上限。
    :param freeze_cap: 每轮冻结候选上限。
    :param n_resamples: 稳定性重采样次数。
    :param seed: 随机种子（确定性复现）。
    :param max_empty_rounds: 允许的「连续无合格候选」轮数；超过即停止。
    :param max_fits: 整个循环允许的候选拟合次数上限（``None`` 表示不限制）。
    :param max_attempts: 表示枚举的尝试次数上限。
    :param structure_search: 是否额外运行聚类与变点检测。MVP 的比例任务不需要，
        故默认关闭；开启后这两类模式也会进入假说生成。
    """

    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    freeze_cap: int = DEFAULT_FREEZE_CAP
    n_resamples: int = DEFAULT_N_RESAMPLES
    seed: int = DEFAULT_SEED
    max_empty_rounds: int = 1
    max_fits: int | None = None
    max_attempts: int = 200_000
    structure_search: bool = False

    def __post_init__(self) -> None:
        _require_positive_int(self.max_rounds, "max_rounds")
        _require_positive_int(self.max_candidates, "max_candidates")
        _require_positive_int(self.freeze_cap, "freeze_cap")
        _require_positive_int(self.n_resamples, "n_resamples", allow_zero=True)
        _require_positive_int(self.seed, "seed", allow_zero=True)
        _require_positive_int(self.max_empty_rounds, "max_empty_rounds", allow_zero=True)
        _require_positive_int(self.max_attempts, "max_attempts")
        if self.max_fits is not None:
            _require_positive_int(self.max_fits, "max_fits", allow_zero=True)
        if not isinstance(self.structure_search, bool):
            raise LoopInputError(
                f"structure_search 必须是布尔值，实际得到：{self.structure_search!r}"
            )

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "loop_version": LOOP_VERSION,
            "max_rounds": self.max_rounds,
            "max_candidates": self.max_candidates,
            "freeze_cap": self.freeze_cap,
            "n_resamples": self.n_resamples,
            "seed": self.seed,
            "max_empty_rounds": self.max_empty_rounds,
            "max_fits": self.max_fits,
            "max_attempts": self.max_attempts,
            "structure_search": self.structure_search,
            "note": STOPPING_RULE_NOTE,
        }

    def representation_budget(self) -> RepresentationBudget:
        """派生 M2 的表示构造预算。

        只转录本层的容量与尝试上限；深度与节点数上限沿用 M2 的缺省
        （深度 3 / 节点 25），因为那是契约级约束，不属于本层的调度参数。
        """
        return RepresentationBudget(
            max_candidates=self.max_candidates,
            max_attempts=self.max_attempts,
        )


@dataclass(frozen=True)
class StopDecision:
    """一次停止判定。

    :param stopped: 是否应当停止。
    :param code: :data:`STOP_CODES` 之一。
    :param reason: 人类可读的理由。
    """

    stopped: bool
    code: str
    reason: str

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {"stopped": self.stopped, "code": self.code, "reason": self.reason}


@dataclass
class BudgetLedger:
    """循环期间的资源消耗台账（可变，随循环推进）。"""

    rounds_completed: int = 0
    candidates_enumerated: int = 0
    fits_attempted: int = 0
    fits_succeeded: int = 0
    fits_failed: int = 0
    confirmations_consumed: int = 0
    empty_rounds: int = 0
    fit_skips: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "rounds_completed": self.rounds_completed,
            "candidates_enumerated": self.candidates_enumerated,
            "fits_attempted": self.fits_attempted,
            "fits_succeeded": self.fits_succeeded,
            "fits_failed": self.fits_failed,
            "confirmations_consumed": self.confirmations_consumed,
            "empty_rounds": self.empty_rounds,
            "fit_skip_count": len(self.fit_skips),
            "fit_skips": [dict(item) for item in self.fit_skips],
        }


def evaluate_stop(
    *,
    round_index: int,
    budget: LoopBudget,
    ledger: BudgetLedger,
    confirmation_available: bool,
    candidates_available: bool,
    **_rejected: Any,
) -> StopDecision:
    """按已声明的预算与阈值判定是否停止。

    判定顺序（先到先得，保证结果确定）：

    1. 轮次用尽（``t > max_rounds``）；
    2. 资源预算耗尽（拟合次数超过 ``max_fits``）；
    3. 无可用未见确证数据；
    4. 连续无合格候选超过 ``max_empty_rounds``。

    :param round_index: 即将开始的轮次（从 1 开始）。
    :param budget: 启动时声明的预算。
    :param ledger: 当前消耗台账。
    :param confirmation_available: 本轮是否有可用的未见确证分区。
    :param candidates_available: 本轮是否筛出了合格的冻结候选。
    """
    _reject_forbidden_kwargs(_rejected, "evaluate_stop")
    _require_positive_int(round_index, "round_index")
    if not isinstance(budget, LoopBudget):
        raise LoopInputError("budget 必须是 LoopBudget 实例")
    if not isinstance(ledger, BudgetLedger):
        raise LoopInputError("ledger 必须是 BudgetLedger 实例")

    if round_index > budget.max_rounds:
        return StopDecision(
            True,
            STOP_ROUNDS_EXHAUSTED,
            f"轮次预算用尽：即将开始的轮次 t={round_index} 超过 max_rounds="
            f"{budget.max_rounds}。",
        )
    if budget.max_fits is not None and ledger.fits_attempted >= budget.max_fits:
        return StopDecision(
            True,
            STOP_RESOURCE_BUDGET,
            f"拟合次数已达上限 max_fits={budget.max_fits}"
            f"（已尝试 {ledger.fits_attempted} 次）。",
        )
    if not candidates_available:
        if ledger.empty_rounds >= budget.max_empty_rounds:
            return StopDecision(
                True,
                STOP_NO_CANDIDATES,
                f"连续 {ledger.empty_rounds} 轮无合格候选，已达容忍上限 "
                f"max_empty_rounds={budget.max_empty_rounds}。",
            )
    if not confirmation_available:
        return StopDecision(
            True,
            STOP_NO_CONFIRMATION_DATA,
            "没有可用且满足协议的未见确证数据：按 §5 伪代码，候选登记为待确证后停止，"
            "而不是用已见数据自证。",
        )
    return StopDecision(False, STOP_NOT_STOPPED, "未触发任何停止条件。")


# ---------------------------------------------------------------------------
# 冻结不变量
# ---------------------------------------------------------------------------


def freeze_snapshot(plan: Any, **_rejected: Any) -> dict:
    """在 ``bind`` **之前**取定冻结快照。

    快照覆盖「冻结后不得再变」的全部内容：预处理步骤、候选（假说标识与版本）、
    检验族、效应阈值与主要指标。取内容的**摘要**而非对象引用，使比对不受
    「同一个字典被就地修改」这类隐蔽改动影响。

    :raises LoopInputError: 计划缺少必需字段。
    """
    _reject_forbidden_kwargs(_rejected, "freeze_snapshot")
    if not isinstance(plan, Mapping):
        raise LoopInputError(
            f"plan 必须是映射（build_frozen_plan 的产物），实际得到：{type(plan).__name__}"
        )
    required = (
        "protocol_id",
        "round_index",
        "hypotheses",
        "preprocessing",
        "test_family",
        "effect_threshold",
        "primary_metric",
    )
    missing = [name for name in required if name not in plan]
    if missing:
        raise LoopInputError(f"plan 缺少必需字段：{missing}")

    body = {
        "protocol_id": plan["protocol_id"],
        "round_index": plan["round_index"],
        "preprocessing": _plain(plan["preprocessing"]),
        "hypotheses": _plain(plan["hypotheses"]),
        "test_family": _plain(plan["test_family"]),
        "effect_threshold": _plain(plan["effect_threshold"]),
        "primary_metric": _plain(plan["primary_metric"]),
    }
    return {
        "digest": _digest(body, length=32),
        "body": body,
        "frozen_candidates": frozen_candidate_ids(plan),
        "note": (
            "冻结快照在 bind 之前取定：绑定的计划摘要必须与它一致，"
            "执行与归档之后再复算一次，证明预处理与候选未被改动。"
        ),
    }


def frozen_candidate_ids(plan: Any) -> tuple[str, ...]:
    """从冻结计划中抽出「候选标识 @ 版本」清单，供跨阶段比对。"""
    if not isinstance(plan, Mapping):
        raise LoopInputError("plan 必须是映射")
    entries = plan.get("hypotheses")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise LoopInputError("plan['hypotheses'] 必须是序列")
    out: list[str] = []
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            raise LoopInputError(f"plan['hypotheses'][{index}] 必须是映射")
        hypothesis_id = item.get("id")
        version = item.get("version")
        if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
            raise LoopInputError(f"plan['hypotheses'][{index}].id 必须是非空字符串")
        out.append(f"{hypothesis_id}@{version}")
    return tuple(out)


def frozen_unchanged(plan: Any, snapshot: Any, **_rejected: Any) -> bool:
    """复算冻结快照，判断预处理与候选是否**逐字节**未变。"""
    _reject_forbidden_kwargs(_rejected, "frozen_unchanged")
    if not isinstance(snapshot, Mapping) or "digest" not in snapshot:
        raise LoopInputError("snapshot 必须是 freeze_snapshot 的产物")
    current = freeze_snapshot(plan)
    return (
        current["digest"] == snapshot["digest"]
        and current["body"] == _plain(snapshot.get("body"))
        and current["frozen_candidates"] == tuple(
            snapshot.get("frozen_candidates", ()) or ()
        )
    )


def assert_frozen_unchanged(plan: Any, snapshot: Any) -> None:
    """冻结不变量断言：改动预处理或候选即抛 :class:`LoopStateError`。"""
    if not frozen_unchanged(plan, snapshot):
        raise LoopStateError(
            "冻结不变量被破坏：执行前后的预处理或候选内容不一致。"
            "「冻结后不再修改预处理与候选」是确证有效性的前提，"
            "任何改动都必须作为新一轮重新冻结。"
        )


# ---------------------------------------------------------------------------
# 发现档案成分：反例、证据不足、仍缺证据的问题
# ---------------------------------------------------------------------------


def counterexamples_from_patterns(patterns: Iterable[Any]) -> tuple[dict, ...]:
    """从结构模式中抽取反例条目。

    只转录 ``check_invariant`` 给出的反例（``record_id`` / ``value`` /
    ``deviation``），并附上所属表达式与一致度。本层不重新判定反例，
    也不做任何因果或确证升级。
    """
    out: list[dict] = []
    for pattern in patterns:
        payload = getattr(pattern, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        items = payload.get("counterexamples") or ()
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            out.append(
                {
                    "pattern_id": getattr(pattern, "pattern_id", None),
                    "kind": str(getattr(pattern, "kind", "invariant")),
                    "expression": payload.get("expression"),
                    "conformity": payload.get("conformity"),
                    "coverage": payload.get("coverage"),
                    "record_id": item.get("record_id"),
                    "value": item.get("value"),
                    "deviation": item.get("deviation"),
                    "note": (
                        "反例是描述性结果：它说明该近似守恒量在个别观测上不成立，"
                        "不构成对假说的确证或反驳，也不构成因果结论。"
                    ),
                }
            )
    out.sort(key=lambda item: (str(item.get("expression")), str(item.get("record_id"))))
    return tuple(out)


def inconclusive_entries(classification: Any) -> tuple[dict, ...]:
    """从轮次分类中抽取「证据不足 / 无法执行」的条目。

    「不显著」与「无法执行」在档案里必须可区分，故两条路径分别记录理由。
    """
    if classification is None:
        return ()
    outcomes = getattr(classification, "outcomes", None)
    if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes)):
        return ()
    out: list[dict] = []
    for outcome in outcomes:
        status = getattr(outcome, "status", None)
        if status not in OPEN_STATUSES:
            continue
        test_ids = tuple(getattr(outcome, "test_ids", ()) or ())
        if status == STATUS_FAILED:
            failed = tuple(getattr(outcome, "failed_test_ids", ()) or ())
            reason = (
                "检验无法执行（缺少可用的检验产物）："
                f"{list(failed) or list(test_ids)}。"
            )
        else:
            inconclusive_ids = tuple(
                getattr(outcome, "inconclusive_test_ids", ()) or ()
            )
            reason = (
                "现有样本不足以裁决（检验不显著）："
                f"{list(inconclusive_ids) or list(test_ids)}。"
                "不显著既不构成反驳，也不证明零假说。"
            )
        out.append(
            {
                "hypothesis_id": getattr(outcome, "hypothesis_id", None),
                "status": status,
                "test_ids": list(test_ids),
                "reason": reason,
                "metrics": _plain(getattr(outcome, "metrics", {}) or {}),
            }
        )
    out.sort(key=lambda item: str(item.get("hypothesis_id")))
    return tuple(out)


def _open_question(
    *,
    hypothesis_id: str | None,
    statement: str,
    status: str,
    grade: str | None,
    reason: str,
    missing_evidence: Sequence[str],
    resolving_action: str,
) -> dict:
    """构造一条「仍缺证据的问题」。"""
    body = {
        "hypothesis_id": hypothesis_id,
        "statement": statement,
        "status": status,
        "grade": grade,
        "reason": reason,
        "missing_evidence": list(missing_evidence),
        "resolving_action": resolving_action,
    }
    body["question_id"] = f"q-{_digest(body, length=12)}"
    return body


def open_questions_from(
    *,
    hypothesis_records: Sequence[Mapping[str, Any]],
    knowledge: Any = None,
    classification: Any = None,
    identification_gaps: Sequence[Any] = (),
    pending_hypotheses: Sequence[Mapping[str, Any]] = (),
    **_rejected: Any,
) -> tuple[dict, ...]:
    """汇总「仍缺证据的问题清单」。

    三类来源：

    1. **结论层面**：本轮归入「证据不足」或「无法执行」的假说；
    2. **等级层面**：知识库中等级仍为 ``E0``（含未确证）的假说，
       以及被退役但从未获得确证支持的假说；
    3. **前提层面**：数据配置登记的 ``identification_gaps`` —— 它们是
       最终输出必须披露的统计有效性前提缺口（框架说明 §7、协作指南 §5.5）。

    每条都给出「缺什么证据」与「什么动作能补上」，使清单可执行而非仅罗列。
    """
    _reject_forbidden_kwargs(_rejected, "open_questions_from")

    grade_of: dict[str, str | None] = {}
    retired: set[str] = set()
    if knowledge is not None and hasattr(knowledge, "grade_of"):
        for record in hypothesis_records:
            hypothesis_id = str(record.get("id") or "")
            if not hypothesis_id:
                continue
            grade_of[hypothesis_id] = knowledge.grade_of(hypothesis_id)
            if hasattr(knowledge, "is_retired") and knowledge.is_retired(hypothesis_id):
                retired.add(hypothesis_id)

    status_by_hypothesis: dict[str, str] = {}
    reason_by_hypothesis: dict[str, str] = {}
    for entry in inconclusive_entries(classification):
        key = str(entry.get("hypothesis_id") or "")
        status_by_hypothesis[key] = str(entry.get("status"))
        reason_by_hypothesis[key] = str(entry.get("reason"))

    questions: list[dict] = []
    seen: set[str] = set()

    for record in hypothesis_records:
        hypothesis_id = str(record.get("id") or "")
        if not hypothesis_id or hypothesis_id in seen:
            continue
        status = status_by_hypothesis.get(hypothesis_id)
        grade = grade_of.get(hypothesis_id)
        statement = str(record.get("statement") or "")

        if status in OPEN_STATUSES:
            seen.add(hypothesis_id)
            questions.append(
                _open_question(
                    hypothesis_id=hypothesis_id,
                    statement=statement,
                    status=status,
                    grade=grade,
                    reason=reason_by_hypothesis.get(hypothesis_id, "证据不足或无法裁决。"),
                    missing_evidence=[
                        "满足冻结协议、尚未被见过的独立确证数据",
                        "与冻结计划声明的方法一致的检验产物",
                    ],
                    resolving_action=(
                        "按冻结协议补充未见确证批次后重跑该假说的检验族；"
                        "在补齐前不得将其记为支持或反驳。"
                    ),
                )
            )
            continue

        if grade == GRADE_E0 or grade is None:
            seen.add(hypothesis_id)
            questions.append(
                _open_question(
                    hypothesis_id=hypothesis_id,
                    statement=statement,
                    status=status or "not_confirmed",
                    grade=grade,
                    reason=(
                        "该假说尚未获得独立确证支持（等级 "
                        f"{grade or '未记录'}）：探索阶段的开发评估不构成确证。"
                    ),
                    missing_evidence=[
                        "独立的未见确证数据上的检验产物",
                        "达到预定效应阈值的证据",
                    ],
                    resolving_action=(
                        "在下一轮冻结并绑定新的未见确证批次，"
                        "或按 M7 的取证计划补齐区分性观测。"
                    ),
                )
            )

    for record in pending_hypotheses:
        hypothesis_id = str(record.get("id") or "")
        if not hypothesis_id or hypothesis_id in seen:
            continue
        seen.add(hypothesis_id)
        questions.append(
            _open_question(
                hypothesis_id=hypothesis_id,
                statement=str(record.get("statement") or ""),
                status=ROUND_PENDING_CONFIRMATION,
                grade=grade_of.get(hypothesis_id),
                reason=(
                    "候选已冻结但**没有**可用的未见确证数据，故本轮只登记为待确证。"
                    "用已见数据自证会破坏跨轮误差控制，因此不做。"
                ),
                missing_evidence=["尚未采集或尚未解封的独立确证批次"],
                resolving_action=(
                    "按冻结的采样与检验方案补充一批未见数据，"
                    "下一轮以递增的 round_index 重新冻结后执行。"
                ),
            )
        )

    for gap in identification_gaps:
        questions.append(
            _open_question(
                hypothesis_id=None,
                statement=f"识别前提缺口：{_plain(gap)}",
                status="identification_gap",
                grade=None,
                reason=(
                    "该缺口在数据配置中预先登记，属统计有效性的外部依赖；"
                    "本系统只能保证「计划被登记、检验被执行、结果被如实记录」，"
                    "不能保证代表性、独立性、条件 p 值有效性或采样方案适用性。"
                ),
                missing_evidence=["对该前提的显式论证或相应的设计修正"],
                resolving_action=(
                    "在下一轮设计阶段补齐设计/识别条件，或把结论严格限制在"
                    "该前提已被论证的范围内。"
                ),
            )
        )

    questions.sort(key=lambda item: (str(item.get("hypothesis_id")), item["question_id"]))
    return tuple(questions)


# ---------------------------------------------------------------------------
# 轮次记录与发现档案
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundRecord:
    """一轮主循环的完整记录。

    :param round_index: 研究轮次 :math:`t`（从 1 开始）。
    :param status: :data:`ROUND_STATUSES` 之一。
    :param alpha: 本轮误差预算 :math:`\\alpha_t=\\alpha/2^{t}`（未分配时为 ``None``）。
    :param candidate_ids: 本轮筛出的冻结候选标识（``id@version``）。
    :param freeze_digest: 冻结快照摘要（未冻结时为 ``None``）。
    :param binding_id: 确证绑定标识（未绑定时为 ``None``）。
    :param plan_digest: 冻结计划摘要（未冻结时为 ``None``）。
    :param freeze_invariance_ok: 执行后复算的冻结不变量是否成立。
    :param aggregate_status: 本轮聚合结论（未分类时为 ``None``）。
    :param acquisition: 下一轮取证计划（未生成时为 ``None``）。
    :param notes: 本轮说明。
    """

    round_index: int
    status: str
    alpha: float | None = None
    candidate_ids: tuple[str, ...] = ()
    freeze_digest: str | None = None
    binding_id: str | None = None
    plan_digest: str | None = None
    freeze_invariance_ok: bool | None = None
    aggregate_status: str | None = None
    acquisition: Mapping[str, Any] | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典。"""
        return {
            "loop_version": LOOP_VERSION,
            "round_index": self.round_index,
            "status": self.status,
            "alpha": self.alpha,
            "candidate_ids": list(self.candidate_ids),
            "freeze_digest": self.freeze_digest,
            "binding_id": self.binding_id,
            "plan_digest": self.plan_digest,
            "freeze_invariance_ok": self.freeze_invariance_ok,
            "aggregate_status": self.aggregate_status,
            "acquisition": _plain(self.acquisition) if self.acquisition else None,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class DiscoveryArchive:
    """主循环的最终输出：发现档案 + 知识库版本 + 反例与缺证据清单。

    :param rounds: 各轮记录。
    :param knowledge: 终止时的知识库版本（M8 对象）。
    :param counterexamples: 反例清单（可能为空；空表示「搜过且没有」，见 ``counters``）。
    :param inconclusive: 证据不足 / 无法执行的条目。
    :param open_questions: **仍缺证据的问题清单**（本阶段要求可见的最终输出）。
    :param stop: 停止判定。
    :param ledger: 资源消耗台账。
    :param routine_report: 假说修订/退役决策（M8 的机械判定结果）。
    :param notes: 全局说明。
    """

    rounds: tuple[RoundRecord, ...]
    knowledge: Any
    counterexamples: tuple[dict, ...] = ()
    inconclusive: tuple[dict, ...] = ()
    open_questions: tuple[dict, ...] = ()
    stop: StopDecision | None = None
    ledger: BudgetLedger | None = None
    routine_report: tuple[dict, ...] = ()
    identification_gaps: tuple[Any, ...] = ()
    notes: tuple[str, ...] = ()

    # -- 派生视图 ---------------------------------------------------------

    @property
    def knowledge_version(self) -> str | None:
        """知识库版本号。"""
        return getattr(self.knowledge, "version", None)

    @property
    def confirmed_rounds(self) -> tuple[RoundRecord, ...]:
        """确证已执行并归档的轮次。"""
        return tuple(item for item in self.rounds if item.status == ROUND_CONFIRMED)

    @property
    def has_open_questions(self) -> bool:
        """是否存在仍缺证据的问题。"""
        return bool(self.open_questions)

    def counters(self) -> dict:
        """计数字典：把「没搜到」与「没搜」区分开。

        ``counterexample_counters`` 恒有值——即使反例为空，也说明本循环
        **确实执行过**不变量检测（``invariants_checked`` > 0），
        而不是跳过这一步。
        """
        invariants_checked = sum(
            1
            for item in self.rounds
            if any("不变量" in note or "invariant" in note for note in item.notes)
        )
        return {
            "rounds": len(self.rounds),
            "confirmed_rounds": len(self.confirmed_rounds),
            "counterexample_count": len(self.counterexamples),
            "invariant_checks_performed": invariants_checked,
            "inconclusive_count": len(self.inconclusive),
            "open_question_count": len(self.open_questions),
            "identification_gap_count": len(self.identification_gaps),
        }

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典（含外部评估所见全部字段）。"""
        knowledge = self.knowledge
        return {
            "loop_version": LOOP_VERSION,
            "boundary_note": LOOP_BOUNDARY_NOTE,
            "stopping_rule_note": STOPPING_RULE_NOTE,
            "knowledge_version": self.knowledge_version,
            "knowledge": (
                knowledge.to_dict() if hasattr(knowledge, "to_dict") else _plain(knowledge)
            ),
            "rounds": [item.to_dict() for item in self.rounds],
            "counterexamples": [dict(item) for item in self.counterexamples],
            "inconclusive": [dict(item) for item in self.inconclusive],
            "open_questions": [dict(item) for item in self.open_questions],
            "routine_report": [dict(item) for item in self.routine_report],
            "identification_gaps": [_plain(item) for item in self.identification_gaps],
            "stop": self.stop.to_dict() if self.stop else None,
            "ledger": self.ledger.to_dict() if self.ledger else None,
            "counters": self.counters(),
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        """确定性 JSON 序列化。"""
        return canonical_json(self.to_dict())

    def content_digest(self) -> str:
        """内容摘要。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def render_open_questions(self) -> str:
        """把「仍缺证据的问题清单」渲染成可读文本。"""
        if not self.open_questions:
            return "仍缺证据的问题清单：无（本循环全部假说均获得了与其结论匹配的证据）。"
        lines = [f"仍缺证据的问题清单（共 {len(self.open_questions)} 条）："]
        for index, question in enumerate(self.open_questions, start=1):
            lines.append(
                f"  {index}. [{question.get('status')}] "
                f"假说 {question.get('hypothesis_id') or '（前提层面）'}｜"
                f"等级 {question.get('grade') or '未记录'}"
            )
            lines.append(f"     问题：{question.get('statement')}")
            lines.append(f"     为何缺证据：{question.get('reason')}")
            lines.append(
                "     缺什么证据：" + "；".join(question.get("missing_evidence") or [])
            )
            lines.append(f"     如何补上：{question.get('resolving_action')}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 探索阶段（M2 + M3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplorationOutcome:
    """一轮探索阶段的产出（全部来自上游模块的公开入口）。"""

    pool: Any
    sample: ExplorationSample
    fits: Mapping[str, RelationFit]
    stabilities: Mapping[str, StabilityReport]
    candidate_by_id: Mapping[str, Any]
    pattern_to_candidate: Mapping[str, str]
    patterns: tuple[Any, ...]
    counterexample_patterns: tuple[Any, ...]
    notes: tuple[str, ...]

    @property
    def counterexamples(self) -> tuple[dict, ...]:
        """本轮抽取到的反例条目。"""
        return counterexamples_from_patterns(self.counterexample_patterns)


def _candidate_key(candidate: Any) -> str:
    """候选的规范化特征串（用于与模式载荷对齐）。"""
    return expression_canonical_json(candidate.expression)


def build_exploration(
    *,
    explorer: Any,
    protocol: Mapping[str, Any],
    budget: LoopBudget,
    target_field: str,
    counterexample_expressions: Sequence[str] = (),
    variables: Sequence[str] | None = None,
    **_rejected: Any,
) -> ExplorationOutcome:
    """执行表示构造（M2）与模式搜索（M3）。

    数据访问只经 ``explorer.read_dataset`` 读取**探索分区 E** ——
    引用由 M2 从协议的 ``resources`` 映射中解析，本层不手工拼引用。

    :raises LoopInputError: 协议缺 ``resources`` 或 ``protocol_id``。
    """
    _reject_forbidden_kwargs(_rejected, "build_exploration")
    if not isinstance(protocol, Mapping):
        raise LoopInputError("protocol 必须是公开协议映射")
    for name in ("protocol_id", "resources"):
        if name not in protocol:
            raise LoopInputError(f"protocol 缺少必需字段：{name}")

    resources = protocol["resources"]
    if not isinstance(resources, Mapping) or "E" not in resources:
        raise LoopInputError("protocol['resources'] 必须含探索分区 E 的引用")
    exploration_ref = str(resources["E"])

    # -- M2：表示构造（仅经 explorer 读取 E 分区） ------------------------
    pool = build_representation(explorer, protocol, budget.representation_budget())
    candidates = tuple(pool.candidates)

    # -- M3：样本准备与逐候选拟合 ----------------------------------------
    dependence = protocol.get("dependence")
    dependence = dependence if isinstance(dependence, Mapping) else {}
    declared_variables = variables
    if declared_variables is None:
        declared = [
            name
            for name in sorted({v for c in candidates for v in _ast_variables(c.expression)})
        ]
        declared_variables = tuple(declared)
    if not declared_variables:
        raise LoopInputError("无法从候选池推出任何自变量")

    sample = prepare_sample(
        list(_exploration_records(explorer, exploration_ref)),
        tuple(declared_variables),
        target_field,
        purpose="E",
        source_ref=exploration_ref,
    )

    fits: dict[str, RelationFit] = {}
    stabilities: dict[str, StabilityReport] = {}
    candidate_by_id: dict[str, Any] = {}
    notes: list[str] = []

    for candidate in candidates:
        key = _candidate_key(candidate)
        candidate_by_id[candidate.candidate_id] = candidate
        try:
            fit = fit_relation(sample, candidate)
        except Exception as exc:  # 逐候选降级，不让单个候选打断整轮
            notes.append(
                f"候选 {candidate.candidate_id} 拟合失败（{type(exc).__name__}）："
                f"{str(exc)[:160]}"
            )
            continue
        fits[key] = fit
        try:
            report = resample_evaluate(
                candidate,
                list(_exploration_records(explorer, exploration_ref)),
                budget.n_resamples,
                budget.seed,
                spec=protocol,
                split_unit=dependence.get("split_unit"),
                variables=tuple(declared_variables),
                target=target_field,
                source_ref=exploration_ref,
            )
        except Exception as exc:  # 稳定性不可得时如实记录，不填默认值
            notes.append(
                f"候选 {candidate.candidate_id} 稳定性评估不可得（{type(exc).__name__}）："
                f"{str(exc)[:160]}"
            )
        else:
            stabilities[key] = report

    # -- M3：不变量检测（反例搜索）与可选结构搜索 --------------------------
    counterexample_patterns: list[Any] = []
    for text in counterexample_expressions:
        try:
            counterexample_patterns.extend(check_invariant(sample, text))
        except Exception as exc:
            notes.append(
                f"不变量检测 {text!r} 不可得（{type(exc).__name__}）：{str(exc)[:160]}"
            )

    structure_patterns: list[Any] = []
    if budget.structure_search:
        try:
            structure_patterns.extend(
                find_clusters(sample, tuple(declared_variables), seed=budget.seed)
            )
        except Exception as exc:
            notes.append(f"聚类检测不可得（{type(exc).__name__}）：{str(exc)[:160]}")
        try:
            structure_patterns.extend(
                find_changepoints(sample, series=target_field)
            )
        except Exception as exc:
            notes.append(f"变点检测不可得（{type(exc).__name__}）：{str(exc)[:160]}")

    # -- 组装模式：关系模式来自拟合成功的候选；结构模式来自 M3 检测器 ------
    patterns: list[Any] = []
    pattern_to_candidate: dict[str, str] = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        fit = fits.get(key)
        if fit is None:
            continue
        pattern_id = f"pat-relation-{candidate.candidate_id}"
        pattern_to_candidate[pattern_id] = candidate.candidate_id
        patterns.append(
            {
                "pattern_id": pattern_id,
                "kind": "relation",
                "payload": {
                    "expression": key,
                    "target": target_field,
                    "coefficients": _plain(fit.candidate.parameters),
                },
                "metrics": _plain(fit.candidate.metrics),
                "provenance": {"source_ref": exploration_ref},
                "stability": (
                    stabilities[key].stability if key in stabilities else None
                ),
            }
        )
    patterns.extend(structure_patterns)
    for pattern in counterexample_patterns:
        pattern_id = str(getattr(pattern, "pattern_id", ""))
        if pattern_id and pattern_id not in pattern_to_candidate:
            pattern_to_candidate[pattern_id] = ""

    notes.append(
        f"本轮表示池 {len(candidates)} 个候选，拟合成功 {len(fits)} 个，"
        f"稳定性可得 {len(stabilities)} 个；不变量检测覆盖 "
        f"{len(counterexample_expressions)} 条表达式（invariant check performed）。"
    )
    return ExplorationOutcome(
        pool=pool,
        sample=sample,
        fits=MappingProxyType(fits),
        stabilities=MappingProxyType(stabilities),
        candidate_by_id=MappingProxyType(candidate_by_id),
        pattern_to_candidate=MappingProxyType(pattern_to_candidate),
        patterns=tuple(patterns),
        counterexample_patterns=tuple(counterexample_patterns),
        notes=tuple(notes),
    )


def _exploration_records(explorer: Any, ref: str) -> Sequence[Mapping[str, Any]]:
    """经 explorer 角色读取探索分区（唯一的数据读取路径）。"""
    reader = getattr(explorer, "read_dataset", None)
    if not callable(reader):
        raise LoopInputError(
            "explorer 模块没有 read_dataset 方法：探索数据只能经 M1 的公开接口读取。"
        )
    return reader(ref)


def _ast_variables(ast: Any) -> frozenset[str]:
    """收集表达式 AST 中的变量名（只读，不重新实现表达式语义）。"""
    out: set[str] = set()
    stack = [ast]
    while stack:
        node = stack.pop()
        if not isinstance(node, (list, tuple)) or not node:
            continue
        head = node[0]
        if head == "var" and len(node) >= 2 and isinstance(node[1], str):
            out.add(node[1])
            continue
        for item in node[1:]:
            if isinstance(item, (list, tuple)):
                stack.append(item)
    return frozenset(out)


# ---------------------------------------------------------------------------
# 假说构造与开发评估（M4 + M5）
# ---------------------------------------------------------------------------


def form_hypotheses(
    *,
    outcome: ExplorationOutcome,
    knowledge_version: str,
    scope: Mapping[str, Any] | None = None,
    **_rejected: Any,
) -> GeneratedHypothesisPool:
    """把探索阶段的模式转成结构化假说池（M4）。

    只做「模式 → 假说」的转换；零假说与竞争解释由 P08 的模板补齐，
    本层不自行编写解释文本，也不据此授予任何证据等级。
    """
    _reject_forbidden_kwargs(_rejected, "form_hypotheses")
    _require_nonempty_str(knowledge_version, "knowledge_version")
    return hypotheses_from_patterns(
        outcome.patterns,
        knowledge_version=knowledge_version,
        scope=scope,
    )


def evaluate_hypotheses(
    *,
    pool: GeneratedHypothesisPool,
    outcome: ExplorationOutcome,
    knowledge: KnowledgeBase,
    target_field: str,
    **_rejected: Any,
) -> tuple[tuple[Evaluation, ...], tuple[dict, ...]]:
    """对假说池逐条计算 G/S/N/C（M5）。

    :return: ``(评估记录元组, 未评估条目)``。未评估条目的原因如实记录，
        不填默认值、不因此扣分。
    """
    _reject_forbidden_kwargs(_rejected, "evaluate_hypotheses")
    if not isinstance(knowledge, KnowledgeBase):
        raise LoopInputError(
            "knowledge 必须是 M5 的 KnowledgeBase：新颖性必须绑定明确的知识库版本"
        )
    evaluations: list[Evaluation] = []
    skipped: list[dict] = []

    for hypothesis in pool.hypotheses:
        provenance = (
            hypothesis.provenance if isinstance(hypothesis.provenance, Mapping) else {}
        )
        pattern_ids = provenance.get("pattern_ids") or ()
        candidate_id = None
        for pattern_id in pattern_ids:
            found = outcome.pattern_to_candidate.get(str(pattern_id))
            if found:
                candidate_id = found
                break
        if not candidate_id:
            skipped.append(
                {
                    "hypothesis_id": hypothesis.id,
                    "reason": "假说未关联到可用候选（其来源模式不是本轮的关系模式）。",
                }
            )
            continue
        candidate = outcome.candidate_by_id.get(candidate_id)
        if candidate is None:
            skipped.append(
                {
                    "hypothesis_id": hypothesis.id,
                    "reason": f"候选 {candidate_id} 不在本轮候选池中。",
                }
            )
            continue
        key = _candidate_key(candidate)
        fit = outcome.fits.get(key)
        if fit is None:
            skipped.append(
                {
                    "hypothesis_id": hypothesis.id,
                    "reason": "该候选缺少可用的拟合结果，无法计算预测增益 G。",
                }
            )
            continue
        try:
            evaluation = evaluate_hypothesis(
                hypothesis,
                knowledge_base=knowledge,
                relation_fit=fit,
                stability_evidence=outcome.stabilities.get(key),
                expression=candidate.ast,
            )
        except Exception as exc:
            # 逐条降级：某个候选的指标不可得（例如增益为无穷）不应打断整轮。
            # 如实记录原因，**不**填默认值、**不**因此扣分。
            skipped.append(
                {
                    "hypothesis_id": hypothesis.id,
                    "reason": (
                        f"评估不可得（{type(exc).__name__}）：{str(exc)[:160]}"
                    ),
                }
            )
            continue
        evaluations.append(evaluation)

    if not evaluations:
        skipped.append(
            {
                "hypothesis_id": None,
                "reason": f"本轮没有可评估的假说（{target_field} 目标下无可用拟合）。",
            }
        )
    return tuple(evaluations), tuple(skipped)


def select_for_freeze(
    *,
    evaluations: Sequence[Evaluation],
    cap: int,
    **_rejected: Any,
) -> FreezeSelection:
    """按 Pareto 规则筛出冻结候选（M5）。

    本层不引入任何总分或加权；排序方向与容量上限完全由 P10 声明。
    """
    _reject_forbidden_kwargs(_rejected, "select_for_freeze")
    return select_freeze_candidates(list(evaluations), max_candidates=cap)


# ---------------------------------------------------------------------------
# 取证计划（M7）
# ---------------------------------------------------------------------------


def _acquisition_for_round(
    *,
    hypotheses: Sequence[Any],
    outcome: ExplorationOutcome,
    purpose: str,
    max_items: int,
) -> Mapping[str, Any] | None:
    """生成下一轮取证计划（M7）。

    观测取「候选在探索样本上的预测分歧」：每个候选给出一列预测，
    分歧即竞争解释之间的差异。M7 会把排序标注为启发式，
    本层不重新命名它，也不把计划当作已执行的采集。
    """
    buckets: dict[str, list[float]] = {}
    rows = outcome.sample.rows
    for candidate_id, candidate in outcome.candidate_by_id.items():
        key = _candidate_key(candidate)
        fit = outcome.fits.get(key)
        if fit is None:
            continue
        slope = float(fit.candidate.parameters.get("slope", 0.0) or 0.0)
        intercept = float(fit.candidate.parameters.get("intercept", 0.0) or 0.0)
        for index, row in enumerate(rows[: max_items * 4]):
            value = _candidate_value(candidate, row.values)
            if value is None:
                continue
            buckets.setdefault(f"obs-{index:04d}", []).append(intercept + slope * value)
    if not buckets:
        return None

    hypothesis_entries = [
        {"id": hypothesis.id, "statement": hypothesis.statement}
        for hypothesis in hypotheses
    ]
    observations = []
    for observation_id, values in sorted(buckets.items()):
        if len(values) < 2:
            continue
        predictions = {
            f"cand-{index:04d}": float(value)
            for index, value in enumerate(values[: max_items])
        }
        observations.append(
            {
                "observation_id": observation_id,
                "predictions": predictions,
                "sampling_cost": 1.0,
            }
        )
    if not observations:
        return None

    # M7 只在竞争假说之间比较分歧，故把候选作为「竞争解释」登记。
    competitors = [
        {"id": name, "statement": f"候选 {name} 给出的预测。", "predictions": [{}]}
        for name in sorted(observations[0]["predictions"])
    ]
    report: DivergenceReport = divergence(competitors, observations)
    pre_sampling_freeze = None
    if purpose in CONFIRMATION_GRADE_PURPOSES:
        # 确证类取证（含反例取证）在 M7 里必须提供「采样前冻结」声明：
        # 涉及确证的数据采集，必须在采样前固定候选与采样、检验方案。
        # 本层据此把**已冻结**的候选与方案转录过去；缺任一项时 M7 会拒绝，
        # 本层不提供跳过该校验的捷径。
        pre_sampling_freeze = {
            "frozen_candidates": [
                hypothesis.id for hypothesis in hypotheses
            ] or [f"candidate-{index:04d}" for index in range(len(competitors))],
            "frozen_protocol": {
                "purpose": purpose,
                "primary_metric": DEFAULT_PRIMARY_METRIC,
                "effect_threshold": DEFAULT_EFFECT_THRESHOLD,
                "subgroups": [DEFAULT_SUBGROUP],
                "note": (
                    "采样与检验方案在采样前固定：按批次独立采样，"
                    "以冻结参数计算批次级损失改善，单侧检验。"
                ),
            },
            "freeze_before_sampling": True,
        }
    plan = acquisition_plan(
        report,
        purpose=purpose,
        max_items=max_items,
        pre_sampling_freeze=pre_sampling_freeze,
    )
    return plan.to_dict()


def _candidate_value(candidate: Any, values: Mapping[str, float]) -> float | None:
    """在给定自变量取值上求候选表达式的值（只读，无副作用）。"""
    try:
        from sdl_m02.domain import _evaluate  # 复用 M2 的求值口径

        result = _evaluate(candidate.expression, values)
    except Exception:
        return None
    if result is None or isinstance(result, bool):
        return None
    try:
        number = float(result)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


# ---------------------------------------------------------------------------
# 确证阶段（M6）与归档（M8）
# ---------------------------------------------------------------------------


def unavailable_evaluator(
    *,
    plan: Any,
    binding: Any,
    records: Any,
    alpha: float,
    **_rejected: Any,
) -> dict:
    """缺省外部评估器：不产生任何检验产物。

    这是**诚实的缺省**：本层不自行计算 p 值，因此缺省下所有检验记为不可得，
    本轮结论落在「证据不足」。这样「没算」与「算出来不显著」在档案里可区分。
    """
    _reject_forbidden_kwargs(_rejected, "unavailable_evaluator")
    return {}


def synthetic_batch_evaluator(
    *,
    plan: Any,
    binding: Any,
    records: Any,
    alpha: float,
    group_field: str = "batch",
    **_rejected: Any,
) -> dict:
    """**合成演示专用**的外部评估器。

    它实现冻结计划里登记的 :data:`SYNTHETIC_METHOD_TEXT`：把消费到的确证记录
    按观测单位（批次）聚合，用**冻结的参数**（取自计划，不重新拟合）计算
    每个批次的损失改善，再对批次级均值做单侧正态近似检验。

    必须明确的边界（见 :data:`SYNTHETIC_EVALUATOR_NOTE`）：

    * 它假设批次独立、改善近似正态、方差同质，且不做小样本修正；
    * 它**不是**可推广的统计方法，只用于演示主循环的控制流；
    * 真实使用必须替换为预先声明并经过有效性论证的统计执行器。

    :raises LoopPolicyError: 计划缺少检验族或假说参数。
    """
    _reject_forbidden_kwargs(_rejected, "synthetic_batch_evaluator")
    if not isinstance(plan, Mapping):
        raise LoopInputError("plan 必须是映射")
    family = plan.get("test_family")
    if not isinstance(family, Sequence) or isinstance(family, (str, bytes)):
        raise LoopPolicyError("冻结计划的 test_family 必须是序列")

    # 每批次的损失改善：常数基线（冻结截距）与候选预测（冻结参数）之差。
    payload = _batch_differences(
        plan=plan,
        records=records,
        group_field=group_field,
    )
    statistics = _one_sided_normal_statistic(payload)
    threshold = plan.get("effect_threshold")
    threshold = float(threshold) if isinstance(threshold, (int, float)) else 0.0

    out: dict[str, dict] = {}
    for entry in family:
        if not isinstance(entry, Mapping):
            continue
        test_id = entry.get("id")
        if not isinstance(test_id, str):
            continue
        effect = statistics.get("mean_improvement")
        p_value = statistics.get("p_value")
        out[test_id] = {
            "p_value": p_value,
            "effect": effect,
            "delta": threshold,
            "notes": SYNTHETIC_EVALUATOR_NOTE,
        }
    return out


def _as_float(value: Any, default: float) -> float:
    """把可能是 ``None`` / 字符串 / 数值的字段转成浮点，失败则取默认。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """把对象转成映射：映射原样返回，``to_dict()`` 对象取其字典，其余给空映射。

    冻结计划里 ``model`` 既可能是字典，也可能是被 P11 序列化成的字符串；
    字符串由 :func:`_parse_model_text` 另行解析，本函数只处理对象形态。
    """
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return result
    return {}


def _parse_model_text(text: str) -> dict[str, Any]:
    """解析 P11 序列化后的 ``model`` 文本。

    形态形如 ``'coefficients={"intercept":1.0,"slope":2.0}; expression=["mul",…]; target=Y'``：
    分号分隔的若干 ``键=值`` 段，值是 JSON 或裸字符串。**必须**解析它，
    否则模型表达式会被静默丢掉，候选预测退化成常数预测。
    """
    out: dict[str, Any] = {}
    for chunk in str(text).split(";"):
        if "=" not in chunk:
            continue
        key, _, raw = chunk.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        try:
            out[key] = json.loads(raw)
        except (ValueError, TypeError):
            out[key] = raw
    return out


def _recover_expression_ast(expression: Any) -> Any:
    """从冻结计划里登记的表达式还原 AST。

    表达式可能以三种形态出现：

    * 规范化 JSON 字符串（``'["mul",["inv",["var","X2"]],["var","X1"]]'``）；
    * 嵌套列表（``from_ast`` 的原生输入）；
    * 元组 AST（``parse`` 的直接产物）。

    M2 的 ``from_ast`` 只接受嵌套列表，故字符串形态必须显式解析，
    否则模型项会被静默丢掉、把「候选预测」退化成常数预测。
    解析失败返回 ``None``（调用方按无模型处理），不抛异常。
    """
    if expression is None:
        return None
    from sdl_m02.expressions import from_ast as _from_ast

    payload = expression
    if isinstance(expression, (str, bytes)):
        try:
            payload = json.loads(
                expression.decode("utf-8") if isinstance(expression, bytes) else expression
            )
        except (ValueError, TypeError):
            return None
    try:
        return _from_ast(payload)
    except Exception:
        return None


def _group_of(record: Mapping[str, Any], group_field: str) -> str:
    """取记录的分组名；取不到时退到第一个分组值，再无则归入 ``__ungrouped__``。"""
    groups = record.get("group_ids")
    if isinstance(groups, Mapping):
        group = groups.get(group_field)
        if group is None and groups:
            group = next(iter(groups.values()))
        if group is not None:
            return str(group)
    return "__ungrouped__"


def _batch_differences(
    *,
    plan: Mapping[str, Any],
    records: Any,
    group_field: str,
) -> dict:
    """按观测单位聚合，算出批次级损失改善（演示用，全部参数来自冻结计划）。"""
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise LoopInputError(
            "确证记录必须是序列：它只能来自 consume_confirmation 的返回值"
        )
    hypotheses = plan.get("hypotheses") or ()
    if not hypotheses:
        raise LoopPolicyError("冻结计划未登记任何假说")
    entry = hypotheses[0]
    parameters = entry.get("parameters") or {}
    representation = _as_mapping(entry.get("representation"))
    raw_model = entry.get("model")
    if isinstance(raw_model, (str, bytes)):
        # P11 会把 model 序列化成 'k=v; k=v' 文本，先解析回字典。
        model = _parse_model_text(
            raw_model.decode("utf-8") if isinstance(raw_model, bytes) else raw_model
        )
    else:
        model = _as_mapping(raw_model)
    coefficients = _as_mapping(model.get("coefficients"))
    # 冻结参数可能落在 parameters 或 model.coefficients，两处都读；
    # 优先 parameters（P11 登记的规范化位置）。
    intercept = _as_float(parameters.get("intercept", coefficients.get("intercept")), 0.0)
    slope = _as_float(parameters.get("slope", coefficients.get("slope")), 0.0)
    expression = model.get("expression")
    if expression is None:
        expression = representation.get("expression")
    ast = _recover_expression_ast(expression)

    target = model.get("target")
    if not isinstance(target, str):
        target = "Y"

    from sdl_m02.domain import _evaluate as _eval_ast

    def candidate_prediction(values: Mapping[str, Any]) -> float | None:
        """候选预测 ``intercept + slope·f(x)``；模型项不可求值时返回 ``None``。"""
        if ast is None:
            return None
        inputs: dict[str, float] = {}
        for key, value in values.items():
            try:
                inputs[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        try:
            z = _eval_ast(ast, inputs)
        except Exception:
            return None
        if z is None:
            return None
        return intercept + slope * float(z)

    # 零模型（常数基线）取**冻结截距**：这正是「斜率置零」的候选，
    # 故「候选预测 vs 常数基线」的损失改善才有明确含义。
    # 若把零模型取成每批次的观测均值，则基线已经看过该批次的全部数据，
    # 改善量会被压成组内噪声，使任何候选都判为「不显著」——那是假基线。
    constant_baseline = intercept

    buckets: dict[str, dict[str, float]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        values = record.get("values")
        if not isinstance(values, Mapping) or target not in values:
            continue
        try:
            observed = float(values[target])
        except (TypeError, ValueError):
            continue
        predicted = candidate_prediction(values)
        if predicted is None or math.isnan(predicted) or math.isinf(predicted):
            continue
        bucket = buckets.setdefault(
            _group_of(record, group_field),
            {"n": 0, "sse_candidate": 0.0, "sse_null": 0.0},
        )
        bucket["n"] += 1
        bucket["sse_candidate"] += (observed - predicted) ** 2
        bucket["sse_null"] += (observed - constant_baseline) ** 2

    differences: list[float] = []
    for name in sorted(buckets):
        bucket = buckets[name]
        if bucket["n"] <= 0:
            continue
        mse_candidate = bucket["sse_candidate"] / bucket["n"]
        mse_null = bucket["sse_null"] / bucket["n"]
        differences.append(mse_null - mse_candidate)
    return {
        "differences": differences,
        "group_field": group_field,
        "n_groups": len(differences),
        "parameters": {"intercept": intercept, "slope": slope},
        "model_recovered": ast is not None,
        "constant_baseline": constant_baseline,
    }


def _one_sided_normal_statistic(payload: Mapping[str, Any]) -> dict:
    """批次级均值的单侧正态近似（演示用）。"""
    differences = list(payload.get("differences") or ())
    n = len(differences)
    if n < 2:
        return {
            "n_groups": n,
            "mean_improvement": None,
            "statistic": None,
            "p_value": None,
            "reason": "批次级观测不足两条，无法给出统计量：记为不可得。",
        }
    mean = sum(differences) / n
    variance = sum((value - mean) ** 2 for value in differences) / (n - 1)
    if variance <= 0.0:
        return {
            "n_groups": n,
            "mean_improvement": mean,
            "statistic": None,
            "p_value": None,
            "reason": "批次级改善方差为 0，正态近似无定义：记为不可得。",
        }
    standard_error = math.sqrt(variance / n)
    statistic = mean / standard_error
    p_value = 0.5 * (1.0 - math.erf(statistic / math.sqrt(2.0)))
    return {
        "n_groups": n,
        "mean_improvement": mean,
        "statistic": statistic,
        "p_value": max(0.0, min(1.0, p_value)),
        "reason": "",
    }


def _confirmation_refs(protocol: Mapping[str, Any]) -> tuple[str, ...]:
    """按轮次顺序列出可用的确证分区名（``C1``、``C2``…）。"""
    resources = protocol.get("resources") or {}
    found: list[tuple[int, str]] = []
    for name in resources:
        match = _CONFIRMATION_REF_RE.match(str(name))
        if match:
            found.append((int(match.group(1)), str(name)))
    found.sort()
    return tuple(name for _, name in found)


def _refresh_protocol(
    custodian: Any, protocol_id: str, fallback: Mapping[str, Any]
) -> Mapping[str, Any]:
    """经 custodian 的公开 ``describe`` 刷新协议视图。

    ``resources`` 会随 :func:`_add_confirmation_batch` 增长；若始终沿用启动时
    那份快照，循环就永远看不见新补充的确证分区（表现为「明明补了数据却报
    没有可用确证数据」）。这里只做**只读转录**：拿不到新描述时退回旧视图，
    不凭空构造分区，也不直连证据库。
    """
    describe = getattr(custodian, "describe", None)
    if not callable(describe):
        return fallback
    try:
        fresh = describe(protocol_id)
    except Exception:
        return fallback
    if not isinstance(fresh, Mapping):
        return fallback
    return fresh


def _add_confirmation_batch(
    *,
    custodian: Any,
    protocol_id: str,
    purpose: str,
    records: Sequence[Mapping[str, Any]],
) -> None:
    """经 M1 的公开接口补充一批新的确证数据（唯一的确证数据入湖路径）。"""
    adder = getattr(custodian, "add_confirmation", None)
    if not callable(adder):
        raise LoopInputError(
            "custodian 模块没有 add_confirmation 方法：新确证数据只能经 M1 的公开接口登记。"
        )
    adder(protocol_id, list(records), purpose)


def _record_revisions(
    *,
    store: HypothesisStore,
    knowledge: KnowledgeVersion,
    hypothesis_records: Sequence[Mapping[str, Any]],
) -> tuple[dict, ...]:
    """对每个假说请求 M8 给出修订 / 保留 / 退役决策（机械判定）。"""
    out: list[dict] = []
    for record in hypothesis_records:
        hypothesis_id = str(record.get("id") or "")
        if not hypothesis_id:
            continue
        try:
            decision: RevisionDecision = revise_or_retire(
                store,
                hypothesis_id,
                knowledge,
                reason="主循环依据本轮归档结果请求复核。",
            )
        except Exception as exc:
            out.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "action": "undetermined",
                    "reason": f"复核不可得（{type(exc).__name__}）：{str(exc)[:160]}",
                }
            )
            continue
        out.append(
            {
                "hypothesis_id": hypothesis_id,
                "action": getattr(decision, "action", None),
                "previous_identity": getattr(decision, "previous_identity", None),
                "resulting_identity": getattr(decision, "resulting_identity", None),
                "latest_status": getattr(decision, "latest_status", None),
                "final_grade": getattr(decision, "final_grade", None),
                "reason": getattr(decision, "reason", None),
                "requires_fresh_evidence": bool(
                    getattr(decision, "required_fresh_evidence", True)
                ),
                "forbidden_evidence_identities": list(
                    getattr(decision, "forbidden_evidence_identities", ()) or ()
                ),
            }
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_loop(
    *,
    custodian: Any,
    confirmer: Any,
    explorer: Any,
    protocol: Mapping[str, Any],
    target_field: str = "Y",
    budget: LoopBudget | None = None,
    counterexample_expressions: Sequence[str] = (),
    variables: Sequence[str] | None = None,
    confirmation_refs: Sequence[str] | None = None,
    data_supplier: Callable[[int, str], Any] | None = None,
    confirmation_evaluator: Callable[..., Mapping[str, Any]] | None = None,
    recorded_at: str | None = None,
    knowledge: KnowledgeVersion | None = None,
    acquisition_sink: Callable[[int, Mapping[str, Any]], Any] | None = None,
    **_rejected: Any,
) -> DiscoveryArchive:
    """按《SDL算法框架说明》§5 的伪代码执行端到端主循环。

    循环骨架与伪代码逐句对应::

        while 仍有预算且未触发停止条件:
            R       ← 构造表示(D_E, K)                     # M2
            P       ← 搜索模式(D_E, R)                     # M3
            H_pool  ← 合并去重(H_active, 生成假说(P, K))    # M4
            H_batch ← 开发评估与 Pareto 筛选(H_pool, D_E, D_V)  # M5
            if H_batch 为空: 记录失败; t ← t+1; continue
            α_t     ← α / 2^t
            F_t     ← 冻结候选、变换、参数、检验族与协议     # M6（bind 之前取快照）
            if 没有可用未见确证数据: 登记待确证; break
            D_C,t   ← 消费                                          # M6
            E_t     ← 执行确证(F_t, D_C,t, α_t)                      # M6
            A, K    ← 归档、更新证据等级与版本                       # M8
            取证计划 ← M7；H_active 修订；t ← t+1

    :param custodian: 以 custodian 角色构造的 M1 模块（鸭子类型即可）。
    :param confirmer: 以 confirmer 角色构造的 M1 模块。
    :param explorer: 以 explorer 角色构造的 M1 模块。
    :param protocol: 公开协议（``build`` / ``describe`` 的产物）。
    :param target_field: 目标字段名。
    :param budget: 启动时声明的预算与停止阈值。
    :param counterexample_expressions: 需要做不变量（反例）检测的表达式文本。
        可以是空序列——此时反例清单为空，但档案会**明确记录检测确实执行过**
        （``counters.invariant_checks_performed``），以区分「没搜到」与「没搜」。
    :param variables: 自变量清单；缺省由候选池中的变量名推导。
    :param confirmation_refs: 按轮次顺序使用的确证分区名；缺省按 ``C1``、``C2``…
        的顺序自动识别。
    :param data_supplier: ``data_supplier(round_index, protocol_id)``，
        在每轮开始时尝试补充一批**新的**未见确证数据；返回 ``None`` 表示无可补充。
        这是确证数据入湖的唯一路径，且只能经 M1 的 ``add_confirmation``。
    :param confirmation_evaluator: 外部统计执行器
        ``evaluator(plan, binding, records, alpha) -> {test_id: {...}}``。
        缺省用 :func:`unavailable_evaluator`（全部检验不可得）。
        **本层不自行计算 p 值。**
    :param recorded_at: 归档时间戳（确定性复现用）；缺省由 M8 取系统时间。
    :param knowledge: 起始知识库版本；缺省为 ``K0``（空知识库）。
    :param acquisition_sink: ``acquisition_sink(round_index, plan_dict)``，在每轮
        生成 M7 取证计划后被回调一次，把「本轮建议去看什么」交给调用方
        （例如自主取数执行器 M9）作为**下一轮采样的依据**。这是**只读**钩子：
        本层照常把计划记入本轮记录，回调的返回值被忽略，钩子抛出的异常被
        如实降级为一条说明、绝不打断循环。缺省 ``None`` 时行为与之前完全一致。
    :return: :class:`DiscoveryArchive`，**必然含「仍缺证据的问题清单」**。
    :raises LoopPolicyError: 传入了等级 / 统计结论 / 令牌类参数。
    :raises LoopStateError: 冻结不变量被破坏。
    """
    _reject_forbidden_kwargs(_rejected, "run_loop")
    if not isinstance(protocol, Mapping):
        raise LoopInputError("protocol 必须是公开协议映射")
    protocol_id = _require_nonempty_str(protocol.get("protocol_id"), "protocol.protocol_id")
    # 协议必须含探索分区 E 与总 α —— 二者缺任一都无法开始，宁可早失败，
    # 也不要跑出一个「其实没读过数据」的假档案。
    for required in ("resources", "confirmation_policy"):
        if required not in protocol:
            raise LoopInputError(f"protocol 缺少必需字段：{required}")
    if not isinstance(protocol["resources"], Mapping) or "E" not in protocol["resources"]:
        raise LoopInputError("protocol['resources'] 必须含探索分区 E 的引用")
    budget = budget or LoopBudget()
    if not isinstance(budget, LoopBudget):
        raise LoopInputError("budget 必须是 LoopBudget 实例")
    _require_callable_or_none(data_supplier, "data_supplier")
    _require_callable_or_none(confirmation_evaluator, "confirmation_evaluator")
    _require_callable_or_none(acquisition_sink, "acquisition_sink")
    evaluator = confirmation_evaluator or unavailable_evaluator
    _require_nonempty_str(target_field, "target_field")

    ledger = BudgetLedger()
    current_knowledge = knowledge or empty_knowledge()
    store = HypothesisStore()
    registered: dict[str, Hypothesis] = {}

    refs = tuple(confirmation_refs) if confirmation_refs is not None else _confirmation_refs(protocol)
    round_records: list[RoundRecord] = []
    all_counterexamples: list[dict] = []
    all_inconclusive: list[dict] = []
    all_hypothesis_records: list[dict] = []
    pending_records: list[dict] = []
    revision_report: tuple[dict, ...] = ()
    stop = StopDecision(False, STOP_NOT_STOPPED, "循环尚未开始。")

    round_index = 1
    #: 协议视图。``resources`` 会随 ``add_confirmation`` 增长，故不能只信
    #: 调用方传入的那份快照：每轮开始前都经 custodian 的公开 ``describe`` 刷新，
    #: 使「本轮能看见哪些确证分区」与证据库的真实状态一致。
    current_protocol: Mapping[str, Any] = protocol
    while True:
        # -- 停止条件（先于任何工作判定） --------------------------------
        current_protocol = _refresh_protocol(custodian, protocol_id, current_protocol)
        available_ref = _next_confirmation_ref(refs, round_index)
        if data_supplier is not None and available_ref is None:
            # 尝试补充一批新的未见数据后再判定。
            try:
                data_supplier(round_index, protocol_id)
            except Exception as exc:
                round_records.append(
                    RoundRecord(
                        round_index=round_index,
                        status=ROUND_SKIPPED,
                        notes=(
                            f"补充确证数据失败（{type(exc).__name__}）："
                            f"{str(exc)[:160]}",
                        ),
                    )
                )
            current_protocol = _refresh_protocol(custodian, protocol_id, current_protocol)
            if confirmation_refs is None:
                refs = _confirmation_refs(current_protocol)
            available_ref = _next_confirmation_ref(refs, round_index)

        stop = evaluate_stop(
            round_index=round_index,
            budget=budget,
            ledger=ledger,
            confirmation_available=available_ref is not None,
            candidates_available=True,
        )
        if stop.stopped:
            break

        # -- 探索：M2 表示构造 + M3 模式搜索 -----------------------------
        outcome = build_exploration(
            explorer=explorer,
            protocol=current_protocol,
            budget=budget,
            target_field=target_field,
            counterexample_expressions=counterexample_expressions,
            variables=variables,
        )
        ledger.candidates_enumerated += len(outcome.pool.candidates)
        all_counterexamples.extend(outcome.counterexamples)

        # -- 假说生成：M4 -------------------------------------------------
        pool = form_hypotheses(
            outcome=outcome,
            knowledge_version=current_knowledge.version,
        )
        for hypothesis in pool.hypotheses:
            identity = f"{hypothesis.id}@{hypothesis.version}"
            if identity not in registered:
                registered[identity] = hypothesis
                try:
                    store.register(hypothesis)
                except Exception:
                    # 已在仓库中的同一内容会幂等；其它情况如实跳过注册。
                    pass

        # -- 开发评估与筛选：M5 ------------------------------------------
        knowledge_base = _knowledge_base_for(current_knowledge)
        evaluations, skipped = evaluate_hypotheses(
            pool=pool,
            outcome=outcome,
            knowledge=knowledge_base,
            target_field=target_field,
        )
        ledger.fit_skips.extend(skipped)
        ledger.fits_attempted += len(outcome.candidate_by_id)
        ledger.fits_succeeded += len(outcome.fits)
        ledger.fits_failed += len(outcome.candidate_by_id) - len(outcome.fits)
        if budget.max_fits is not None and ledger.fits_attempted >= budget.max_fits:
            # 预算耗尽会在下一轮开头被 evaluate_stop 捕获。
            pass

        selection = select_for_freeze(evaluations=evaluations, cap=budget.freeze_cap)

        if not selection.candidates:
            ledger.empty_rounds += 1
            ledger.rounds_completed += 1
            acquisition = _acquisition_for_round(
                hypotheses=tuple(registered.values()),
                outcome=outcome,
                purpose=PURPOSE_EXPLORATION,
                max_items=budget.freeze_cap,
            )
            sink_note = _notify_acquisition(acquisition_sink, round_index, acquisition)
            round_records.append(
                RoundRecord(
                    round_index=round_index,
                    status=ROUND_SEARCH_FAILED,
                    candidate_ids=(),
                    acquisition=acquisition,
                    notes=outcome.notes
                    + (
                        f"本轮未筛出合格候选（空轮计数 {ledger.empty_rounds}/"
                        f"{budget.max_empty_rounds}）。",
                        "按 §5 伪代码：记录本轮搜索失败及消耗；"
                        "无合格候选不等于其余假说为假。",
                    )
                    + ((sink_note,) if sink_note else ()),
                )
            )
            round_index += 1
            continue

        ledger.empty_rounds = 0
        selected_ids = tuple(
            f"{item.hypothesis_id}@{_version_of(item)}" for item in selection.candidates
        )
        frozen_hypotheses = _hypotheses_for_selection(selection, registered)
        frozen_records = [item.to_dict() for item in frozen_hypotheses]
        all_hypothesis_records.extend(
            record for record in frozen_records if record not in all_hypothesis_records
        )

        # -- 冻结：M6（bind 之前取快照） ---------------------------------
        plan = _build_plan(
            selection, current_protocol, round_index, frozen_hypotheses, target_field
        )
        snapshot = freeze_snapshot(plan)
        alpha = _alpha_for(current_protocol, round_index)

        # 冻结之后**不再**重建表示、不再改候选与预处理。
        binding = bind(
            confirmer,
            current_protocol["resources"][available_ref],
            plan,
        )
        if binding.get("plan_digest") and snapshot["digest"]:
            # 绑定的计划摘要由 M1 独立复算；与本地快照的比对在下文
            # assert_frozen_unchanged 中以内容为准（摘要算法由 M1 决定）。
            pass

        # -- 确证执行：M6 ------------------------------------------------
        consumed: list[Mapping[str, Any]] = []
        try:
            execution: FamilyExecution = _execute_with_records(
                confirmer=confirmer,
                binding=binding,
                plan=plan,
                protocol=current_protocol,
                round_index=round_index,
                evaluator=evaluator,
                alpha=alpha,
                consumed_sink=consumed,
            )
        except Exception as exc:
            # 确证执行失败：如实记录，不掩盖、不改写为「不显著」。
            assert_frozen_unchanged(plan, snapshot)
            round_records.append(
                RoundRecord(
                    round_index=round_index,
                    status=ROUND_SKIPPED,
                    alpha=alpha,
                    candidate_ids=selected_ids,
                    freeze_digest=snapshot["digest"],
                    binding_id=binding.get("binding_id"),
                    plan_digest=binding.get("plan_digest"),
                    freeze_invariance_ok=True,
                    notes=outcome.notes
                    + (
                        f"确证执行失败（{type(exc).__name__}）：{str(exc)[:200]}",
                        "失败结果被如实记录；不得改写为反驳或支持。",
                    ),
                )
            )
            ledger.rounds_completed += 1
            round_index += 1
            continue

        ledger.confirmations_consumed += 1

        # -- 冻结不变量复算：执行之后候选与预处理必须逐字节未变 --------
        assert_frozen_unchanged(plan, snapshot)

        # -- 结果三分类与发布：M6 ---------------------------------------
        classification: RoundClassification = classify_result(
            execution,
            primary_metric=plan.get("primary_metric"),
        )
        receipt = record_and_release(confirmer, binding, classification)

        # -- 归档与知识库版本：M8 ---------------------------------------
        archive: RoundArchive = archive_round(
            custodian,
            binding,
            classification,
            hypothesis_versions={
                hypothesis.id: hypothesis.version for hypothesis in frozen_hypotheses
            },
            knowledge=current_knowledge,
            receipt=receipt,
            recorded_at=recorded_at,
        )
        current_knowledge = update_knowledge_version(current_knowledge, archive)
        all_inconclusive.extend(inconclusive_entries(classification))

        # -- 下一轮取证计划：M7 -----------------------------------------
        # 取证计划是**建议**，不是主循环的必要产物：M7 若因策略拒绝出计划
        # （例如确证类缺少采样前冻结），本层如实记录并继续，绝不因此中止
        # 备档、也绝不绕过 M7 的校验自行构造计划。
        acquisition = None
        if outcome.counterexamples:
            try:
                acquisition = _acquisition_for_round(
                    hypotheses=frozen_hypotheses,
                    outcome=outcome,
                    purpose=(
                        PURPOSE_COUNTEREXAMPLE
                        if classification.aggregate_status in OPEN_STATUSES
                        else PURPOSE_EXPLORATION
                    ),
                    max_items=budget.freeze_cap,
                )
            except Exception as exc:
                acquisition = {
                    "available": False,
                    "reason": (
                        f"取证计划不可得（{type(exc).__name__}）：{str(exc)[:160]}"
                    ),
                    "note": (
                        "M7 未出计划时本层不自行构造取证方案："
                        "计划必须来自 M7 的公开入口。"
                    ),
                }
        # 把本轮取证建议交给可选钩子（供下一轮自主采样）；失败降级为说明。
        sink_note = _notify_acquisition(acquisition_sink, round_index, acquisition)

        round_records.append(
            RoundRecord(
                round_index=round_index,
                status=ROUND_CONFIRMED,
                alpha=alpha,
                candidate_ids=selected_ids,
                freeze_digest=snapshot["digest"],
                binding_id=binding.get("binding_id"),
                plan_digest=binding.get("plan_digest"),
                freeze_invariance_ok=True,
                aggregate_status=classification.aggregate_status,
                acquisition=acquisition,
                notes=outcome.notes
                + (
                    f"本轮聚合结论：{classification.aggregate_status}"
                    f"（检验族 {len(execution.results)} 条，"
                    f"不可得 {len(execution.unavailable_test_ids)} 条）。",
                    "冻结不变量复算通过：执行与归档之后，预处理与候选逐字节未变。",
                )
                + ((sink_note,) if sink_note else ()),
            )
        )

        # -- 待确证登记（无下一轮数据时） --------------------------------
        if _next_confirmation_ref(refs, round_index + 1) is None:
            pending_records.extend(
                hypothesis.to_dict() for hypothesis in frozen_hypotheses
            )

        ledger.rounds_completed += 1
        round_index += 1
        if round_index > budget.max_rounds:
            break

    # -- 收尾：修订复核与问题清单 ----------------------------------------
    if registered:
        revision_report = _record_revisions(
            store=store,
            knowledge=current_knowledge,
            hypothesis_records=tuple(all_hypothesis_records),
        )

    final_stop = stop
    if not final_stop.stopped:
        final_stop = evaluate_stop(
            round_index=round_index,
            budget=budget,
            ledger=ledger,
            confirmation_available=_next_confirmation_ref(refs, round_index) is not None,
            candidates_available=True,
        )

    gaps = current_protocol.get("identification_gaps") or ()
    open_questions = open_questions_from(
        hypothesis_records=tuple(all_hypothesis_records),
        knowledge=current_knowledge,
        classification=_MergedClassification(all_inconclusive),
        identification_gaps=tuple(gaps),
        pending_hypotheses=tuple(pending_records),
    )

    notes = [
        LOOP_BOUNDARY_NOTE,
        STOPPING_RULE_NOTE,
        "预算：轮流 "
        f"{ledger.rounds_completed}/{budget.max_rounds}，"
        f"候选枚举 {ledger.candidates_enumerated}，"
        f"拟合 {ledger.fits_succeeded}/{ledger.fits_attempted}，"
        f"确证消费 {ledger.confirmations_consumed} 次。",
    ]
    if evaluation_is_default(evaluator):
        notes.append(UNAVAILABLE_EVALUATOR_NOTE)

    return DiscoveryArchive(
        rounds=tuple(round_records),
        knowledge=current_knowledge,
        counterexamples=tuple(all_counterexamples),
        inconclusive=tuple(all_inconclusive),
        open_questions=tuple(open_questions),
        stop=final_stop,
        ledger=ledger,
        routine_report=revision_report,
        identification_gaps=tuple(gaps),
        notes=tuple(notes),
    )


def evaluation_is_default(evaluator: Any) -> bool:
    """判断本次使用的评估器是否为缺省（不可得）评估器。"""
    return evaluator is unavailable_evaluator


class _MergedClassification:
    """把多轮的「证据不足」条目包装成 ``classify_result`` 风格的只读视图。

    仅用于让 :func:`open_questions_from` 复用同一条抽取路径；
    这些条目来自 M6 的 ``HypothesisOutcome``，本层不重新分类。
    """

    __slots__ = ("_outcomes",)

    def __init__(self, entries: Sequence[Mapping[str, Any]]) -> None:
        self._outcomes = tuple(_EntryWrapper(item) for item in entries)

    @property
    def outcomes(self) -> tuple[Any, ...]:
        """包装后的结论条目。"""
        return self._outcomes


class _EntryWrapper:
    """把字典形态的证据不足条目适配成具备属性访问的对象。"""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover
        raise AttributeError("_EntryWrapper 是只读视图")


def _notify_acquisition(
    sink: Callable[[int, Mapping[str, Any]], Any] | None,
    round_index: int,
    plan: Mapping[str, Any] | None,
) -> str | None:
    """把本轮 M7 取证计划交给可选的只读钩子（供下一轮自主采样使用）。

    钩子**只读**：本层不采信其返回值，也不因它改变控制流。钩子异常被降级为
    一条说明并返回，绝不打断主循环——取证建议是建议，不是循环的必要产物。
    ``plan`` 为 ``None``（M7 未出计划）时同样回调，使调用方知道「本轮没有建议」。

    :return: 异常说明文本；正常时为 ``None``。
    """
    if sink is None:
        return None
    try:
        sink(round_index, plan if plan is not None else {})
    except Exception as exc:  # 钩子是外部代码，失败不得影响主循环
        return f"取证建议钩子失败（{type(exc).__name__}）：{str(exc)[:160]}"
    return None


def _next_confirmation_ref(refs: Sequence[str], round_index: int) -> str | None:
    """取第 ``round_index`` 轮应使用的确证分区名（不足则 ``None``）。"""
    if round_index < 1 or round_index > len(refs):
        return None
    return str(refs[round_index - 1])


def _alpha_for(protocol: Mapping[str, Any], round_index: int) -> float:
    """读取协议登记的总 α 并按 :math:`\\alpha_t=\\alpha/2^{t}` 独立复算。

    本层不重新实现该公式——它由 M6 的 ``alpha_for_round`` 承担，
    这里只做「总 α 从哪里来」的转录。
    """
    from sdl_m06.execute import alpha_for_round

    policy = protocol.get("confirmation_policy")
    if not isinstance(policy, Mapping) or "total_alpha" not in policy:
        raise LoopInputError(
            "protocol['confirmation_policy']['total_alpha'] 缺失："
            "本轮 α 需由公开协议独立复算。"
        )
    return float(alpha_for_round(float(policy["total_alpha"]), round_index))


def _execute_with_records(
    *,
    confirmer: Any,
    binding: Any,
    plan: Any,
    protocol: Mapping[str, Any],
    round_index: int,
    evaluator: Callable[..., Mapping[str, Any]],
    alpha: float,
    consumed_sink: list,
) -> FamilyExecution:
    """执行检验族：先由 M6 原子化消费，再交给注入的外部评估器。

    执行分两遍，理由与本层的分工有关——本层**不自行计算 p 值**：

    1. **第一遍**（原子化消费）：以空轨迹调用 M6 的 ``execute_family``，
       得到「已消费」的执行骨架（此时全部检验记为不可得）。本函数用一个
       **只读转接器**在消费发生时截获记录副本，转交给外部评估器。
       截获的只是本次已经原子化消费的记录，**不**改变消费语义、
       **不**提前读取，也不把记录写入任何落盘位置。
    2. **第二遍**（带轨迹执行）：把评估器给出的检验产物交给 M6 重跑。

    第二遍无法直接使用 M6 的默认 ``replay=False``：M1 在第一次消费后已把
    该分区迁到 ``used``，再次以 ``replay=False`` 调用会被 M1 以
    「已消费且未声明重放」拒绝。因此转接器在**第二遍**把 ``replay`` 显式提升为
    ``True``——这是 M1 为「复核轨迹」提供的公开语义（``new_evidence=False``），
    **不**产生新证据、**不**改变已提交的账本，只是允许同一不可变绑定再次被读取。
    哪些调用需要提升由「是否已消费过」这一事实决定，而不是由调用编号硬编码。
    """
    captured: list[Mapping[str, Any]] = []
    consumed_once = False

    class _Tee:
        """对 confirmer 的只读转接：把 consume 的返回值复制一份供评估器使用。

        第二遍（同一次执行内的复核）经 M1 的显式重放通道读取，
        使「用同一不可变绑定重算检验结果」不违反 M1 的消费语义。
        """

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def consume_confirmation(self, binding_id: str, replay: bool = False) -> Any:
            nonlocal consumed_once
            effective = bool(replay) or consumed_once
            result = self._inner.consume_confirmation(binding_id, replay=effective)
            if not consumed_once:
                captured.extend(result)
                consumed_once = True
            return result

    tee = _Tee(confirmer)
    # 第一遍：原子化消费并拿到执行骨架（轨迹为空 → 全部检验记为不可得）。
    skeleton = execute_family(
        tee,
        binding,
        plan,
        round_index,
        protocol=protocol,
    )
    consumed_sink.extend(captured)

    test_ids = family_test_ids(plan)
    try:
        trajectories = evaluator(
            plan=plan,
            binding=binding,
            records=list(captured),
            alpha=alpha,
        )
    except TypeError:
        # 兼容位置参数签名的评估器。
        trajectories = evaluator(plan, binding, list(captured), alpha)
    trajectories = dict(trajectories or {})
    if not trajectories:
        return skeleton

    # 第二遍：以评估器给出的轨迹重跑（转接器自动改走 M1 的显式重放通道），
    # 得到完整的检验结果。
    return execute_family(
        tee,
        binding,
        plan,
        round_index,
        protocol=protocol,
        trajectories={
            test_id: trajectories[test_id]
            for test_id in test_ids
            if test_id in trajectories
        },
    )


def _version_of(selected: Any) -> str:
    """从 P10 的 ``SelectedCandidate`` 中取假说版本。"""
    evaluation = getattr(selected, "evaluation", None)
    version = getattr(evaluation, "hypothesis_version", None)
    if version:
        return str(version)
    identity = str(getattr(selected, "identity", "") or "")
    if "@" in identity:
        return identity.rsplit("@", 1)[1]
    return "1"


def _hypotheses_for_selection(
    selection: FreezeSelection, registered: Mapping[str, Hypothesis]
) -> tuple[Hypothesis, ...]:
    """把筛选结果映射回假说对象（保序、去重）。"""
    out: list[Hypothesis] = []
    seen: set[str] = set()
    for item in selection.candidates:
        hypothesis_id = str(getattr(item, "hypothesis_id", "") or "")
        version = _version_of(item)
        identity = f"{hypothesis_id}@{version}"
        if identity in seen:
            continue
        seen.add(identity)
        hypothesis = registered.get(identity)
        if hypothesis is None:
            raise LoopStateError(
                f"筛选出的候选 {identity} 不在本轮已注册的假说仓库中："
                "冻结必须作用于本轮实际构造出的假说版本。"
            )
        out.append(hypothesis)
    return tuple(out)


def _declarations_for(
    hypotheses: Sequence[Hypothesis], target_field: str
) -> dict:
    """为冻结计划生成口径声明（指标 / 主要指标 / 效应阈值 / 子群）。"""
    declarations: dict[str, dict] = {}
    for hypothesis in hypotheses:
        declarations[hypothesis.id] = {
            "metrics": [DEFAULT_PRIMARY_METRIC],
            "primary_metric": DEFAULT_PRIMARY_METRIC,
            "effect_threshold": DEFAULT_EFFECT_THRESHOLD,
            "subgroups": [DEFAULT_SUBGROUP],
            "prediction": f"相对常数基线的预测改善为正，目标字段 {target_field}。",
        }
    return declarations


def _build_plan(
    selection: Any,
    protocol: Mapping[str, Any],
    round_index: int,
    hypotheses: Sequence[Hypothesis],
    target_field: str,
) -> Any:
    """装配冻结计划（M6 的 ``build_frozen_plan``）。

    单独抽出是为了让测试能在**不跑完整循环**的情况下拿到计划，
    从而独立验证冻结不变量；装配口径与循环体内完全一致。
    """
    return build_frozen_plan(
        list(selection.candidates),
        protocol["protocol_id"],
        round_index,
        protocol=protocol,
        hypotheses=[hypothesis.to_dict() for hypothesis in hypotheses],
        declarations=_declarations_for(hypotheses, target_field),
        default_method=SYNTHETIC_METHOD_TEXT,
    )


def _knowledge_base_for(knowledge: KnowledgeVersion) -> KnowledgeBase:
    """把 M8 的知识库版本转成 M5 的新颖性口径。

    这是**转录**：M8 的归档条目只取「陈述 + 公式」两项作为已知项，
    本层不重新判定新颖性，也不把等级信息带进 M5（M5 不接触等级）。
    """
    entries = []
    for index, entry in enumerate(knowledge.entries):
        statement = getattr(entry, "statement", None)
        if not statement:
            continue
        entries.append(
            {
                "entry_id": f"K-{knowledge.version}-{index:04d}",
                "statement": str(statement),
                "kind": str(getattr(entry, "status", "unknown")),
            }
        )
    return KnowledgeBase(version=knowledge.version, entries=tuple(entries))


# ---------------------------------------------------------------------------
# 合成验证 demo
# ---------------------------------------------------------------------------


def synthetic_spec(seed: int = DEFAULT_SEED) -> dict:
    """合成 MVP 任务的数据配置（对应框架说明 §8）。"""
    return {
        "task": {
            "objects": "合成批次",
            "environments": ["lab-A"],
            "time_scope": "2026 合成实验",
            "claim_types": ["prediction"],
            "target_quantity": "批次级损失改善",
            "weighting": "equal_batch",
            "eligibility": "全部已登记批次",
        },
        "schema": {
            "version": "1",
            "fields": {
                "X1": {
                    "type": "number", "role": "feature", "unit": "mol/L",
                    "required": True, "missing_allowed": True,
                    "minimum": None, "maximum": None, "range_action": "flag",
                },
                "X2": {
                    "type": "number", "role": "feature", "unit": "mol/L",
                    "required": True, "missing_allowed": True,
                    "minimum": None, "maximum": None, "range_action": "flag",
                },
                "Y": {
                    "type": "number", "role": "target", "unit": "mol/(L*s)",
                    "required": True,
                },
            },
            "missing_codes": [None, "NA"],
            "source_required": True,
        },
        "dependence": {
            "record_unit": "reading",
            "split_unit": "batch",
            "inference_unit": "batch",
            "group_fields": ["batch"],
            "namespace": "sdl-p16-synthetic",
            "assumptions": ["合成批次间独立（仅演示假设）"],
        },
        "split": {
            "strategy": "grouped",
            "allocation": {"E": 0.6, "V": 0.2, "C1": 0.2},
            "seed": seed % 100000,
        },
        "quality": {"version": "1", "notes": []},
        "confirmation": {
            "total_alpha": 0.05,
            "sampling_plan": "按冻结协议补充新的合成批次",
            "stopping_rule": "固定批次数量，无期中查看",
        },
        "identification_gaps": [
            "合成数据由已知公式生成，其独立性、正态性与方差同质性由构造保证，"
            "因而不构成对真实数据上这些前提的论证。",
            "批次级检验的正态近似在小样本下未经校准。",
        ],
    }


def synthetic_records(
    *, groups: int = 60, readings: int = 2, start: int = 0, broken: Sequence[int] = (0, 3)
) -> list[dict]:
    """生成合成记录：``Y = 2·(X1/X2) + 1``，且 ``X1·X2 ≈ 2`` 在少数批次上被破坏。

    守恒关系按**精确构造**给出：``X2`` 由 ``2 / base`` 得到，而 ``X1`` 正常批次
    等于 ``base``、``broken`` 批次被放大 1.5 倍。于是 ``X1·X2`` 在正常批次上
    恒为 2.0，在 ``broken`` 批次上恒为 3.0——反例是**真实存在且可复现**的。

    ``broken`` 缺省取 ``(0, 3)``：M1 按 ``split.seed`` 做分组切分，探索分区
    **只含约 60% 的批次**；若把破坏批次放在分区之外，不变量检测在探索数据上
    就看不到任何反例，反例清单会恒为空。缺省值取自该确定切分下确实落在探索
    分区内的批次号。数据完全由代码生成，不宣称任何现实含义。
    """
    records: list[dict] = []
    broken_set = set(broken)
    conserved = 2.0
    for group in range(start, start + groups):
        scale = 1.5 if group in broken_set else 1.0
        for reading in range(readings):
            base = 1.0 + 0.5 * group + 0.1 * reading
            x1 = base * scale
            x2 = conserved / base
            y = 2.0 * (x1 / x2) + 1.0 + 0.01 * reading
            records.append(
                {
                    "record_id": f"syn-{group:05d}-{reading}",
                    "group_ids": {"batch": f"syn-batch-{group:05d}"},
                    "environment": "lab-A",
                    "event_time": "2026-01-01T10:00:00+00:00",
                    "available_time": "2026-01-01T11:00:00+00:00",
                    "values": {"X1": x1, "X2": x2, "Y": y},
                    "units": {"X1": "mol/L", "X2": "mol/L", "Y": "mol/(L*s)"},
                    "source": "synthetic://sdl-p16",
                    "operator_annotation": f"private-synthetic-{group:05d}-{reading}",
                }
            )
    return records


def run_demo(
    output: str | Path,
    *,
    budget: LoopBudget | None = None,
    groups: int = 60,
    recorded_at: str = "2026-01-01T00:00:00+00:00",
    **_rejected: Any,
) -> dict:
    """在合成任务上跑通端到端主循环，并把产物写入 ``output`` 目录。

    产物（全部携带 :data:`SYNTHETIC_DEMO_NOTICE`）：

    * ``discovery-archive.json``：发现档案（含反例、证据不足与缺证据清单）；
    * ``knowledge-version.json``：终止时的知识库版本；
    * ``open-questions.json``：**仍缺证据的问题清单**（独立成文件，便于查看）；
    * ``rounds.json``：逐轮记录；
    * ``说明.md``：中文说明，含合成验证标注与边界声明。

    **不写盘的内容**：角色令牌、确证原始记录、确证记录正文与质量报告。
    令牌只在本进程内使用，绝不落盘。

    :return: ``DiscoveryArchive.to_dict()`` 加上 ``output`` 键。
    """
    _reject_forbidden_kwargs(_rejected, "run_demo")
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)

    from sdl_m01 import Module01, initialize

    db_path = directory / "evidence.sqlite3"
    if db_path.exists():  # 保持 demo 可重复运行
        db_path.unlink()
    tokens = initialize(str(db_path))
    custodian = Module01(str(db_path), tokens["custodian"])
    confirmer = Module01(str(db_path), tokens["confirmer"])
    explorer = Module01(str(db_path), tokens["explorer"])

    spec = synthetic_spec()
    protocol = custodian.build(spec, synthetic_records(groups=groups))

    # 确证数据供应器：第 t 轮补充一批新的合成批次（经 M1 的 add_confirmation）。
    def supply(round_index: int, protocol_id: str) -> None:
        records = synthetic_records(
            groups=12, start=1000 + 50 * round_index, broken=()
        )
        _add_confirmation_batch(
            custodian=custodian,
            protocol_id=protocol_id,
            purpose=f"C{round_index + 1}",
            records=records,
        )

    archive = run_loop(
        custodian=custodian,
        confirmer=confirmer,
        explorer=explorer,
        protocol=protocol,
        target_field="Y",
        budget=budget or LoopBudget(),
        counterexample_expressions=("X1*X2",),
        data_supplier=supply,
        confirmation_evaluator=synthetic_batch_evaluator,
        recorded_at=recorded_at,
    )

    payload = archive.to_dict()
    payload["synthetic_demo_notice"] = SYNTHETIC_DEMO_NOTICE
    payload["output"] = str(directory)

    _write_json(directory / "discovery-archive.json", payload)
    _write_json(
        directory / "knowledge-version.json",
        {
            "synthetic_demo_notice": SYNTHETIC_DEMO_NOTICE,
            "knowledge_version": archive.knowledge_version,
            "knowledge": payload["knowledge"],
        },
    )
    _write_json(
        directory / "open-questions.json",
        {
            "synthetic_demo_notice": SYNTHETIC_DEMO_NOTICE,
            "note": "仍缺证据的问题清单：本阶段可见的最终输出。",
            "open_questions": payload["open_questions"],
        },
    )
    _write_json(
        directory / "rounds.json",
        {
            "synthetic_demo_notice": SYNTHETIC_DEMO_NOTICE,
            "rounds": payload["rounds"],
            "stop": payload["stop"],
            "ledger": payload["ledger"],
        },
    )
    (directory / "说明.md").write_text(
        _demo_readme(archive), encoding="utf-8"
    )
    return payload


def _write_json(path: Path, value: Any) -> None:
    """以 UTF-8 写入 JSON（确定性键序）。"""
    path.write_text(
        json.dumps(_plain(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _demo_readme(archive: DiscoveryArchive) -> str:
    """生成 demo 的中文说明（含合成验证标注与边界声明）。"""
    lines = [
        "# P16 端到端主循环 · 合成验证产物",
        "",
        f"**合成验证标注**：{SYNTHETIC_DEMO_NOTICE}",
        "",
        "## 本目录内容",
        "",
        "- `discovery-archive.json`：发现档案（含反例、证据不足与缺证据清单）。",
        "- `knowledge-version.json`：终止时的知识库版本。",
        "- `open-questions.json`：仍缺证据的问题清单。",
        "- `rounds.json`：逐轮记录与资源台账。",
        "- `evidence.sqlite3`：本地证据库。**属本地保管资料，不应交给不可信探索程序。**",
        "",
        "## 不写盘的内容",
        "",
        "角色令牌只在本进程内使用，**绝不落盘、绝不写入提示词或日志**；",
        "确证原始记录、确证记录正文与质量报告同样不写入本目录。",
        "",
        "## 结果概览",
        "",
        f"- 知识库版本：`{archive.knowledge_version}`",
        f"- 轮次数：{len(archive.rounds)}（其中确证执行并归档 "
        f"{len(archive.confirmed_rounds)} 轮）",
        f"- 反例条目：{len(archive.counterexamples)}",
        f"- 证据不足条目：{len(archive.inconclusive)}",
        f"- 仍缺证据的问题：{len(archive.open_questions)}",
        "",
        "## 边界声明",
        "",
        LOOP_BOUNDARY_NOTE,
        "",
        STOPPING_RULE_NOTE,
        "",
        SYNTHETIC_EVALUATOR_NOTE,
        "",
        "## 仍缺证据的问题清单",
        "",
        "```text",
        archive.render_open_questions(),
        "```",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 边界自检
# ---------------------------------------------------------------------------


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查两项：

    1. 模块是否定义了 :data:`NOT_PROVIDED_BY_P16` 中的任何名字（越界实现）；
    2. 模块是否出现 :data:`_RESTRICTED_ACCESS_NAMES` 中的受限访问入口。

    用 AST 而非源码文本匹配：本模块的 docstring 与常量里**必须**写出这些
    被禁名字（那是边界声明），文本匹配会把声明本身判成违规。
    """
    import ast as _ast
    import inspect as _inspect

    source_path = Path(_inspect.getsourcefile(self_check) or __file__)
    tree = _ast.parse(source_path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
    }
    # 比对**全部**被禁名字。判定分两类：
    # 1. 「定义类」越界：模块自己定义了黑名单中的函数/类；
    # 2. 「访问类」越界：主体中出现了受限读取入口——但**本地符号**同名不算，
    #    例如 ``ledger = BudgetLedger()`` 只是本层的消耗台账变量，
    #    并非 M1 的账本入口。故把参数名、赋值目标与自身定义名一并剔除。
    class _Filter(_ast.NodeVisitor):
        """收集「定义名」与「本地绑定名」，用于剔除同名的局部符号。"""

        def __init__(self) -> None:
            self.local: set[str] = set()
            self.accessed: set[str] = set()

        def visit_arg(self, node) -> None:
            self.local.add(node.arg)

        def visit_Name(self, node) -> None:
            if isinstance(node.ctx, (_ast.Store,)):
                self.local.add(node.id)
            else:
                self.accessed.add(node.id)

        def visit_Attribute(self, node) -> None:
            self.accessed.add(node.attr)
            self.generic_visit(node)

        def visit_Import(self, node) -> None:
            for alias in node.names:
                self.accessed.add(alias.name.split(".")[-1])

        def visit_ImportFrom(self, node) -> None:
            for alias in node.names:
                self.accessed.add(alias.name)

    walker = _Filter()
    walker.visit(tree)
    defined_forbidden = tuple(sorted(defined & set(NOT_PROVIDED_BY_P16)))
    accessed_forbidden = tuple(
        sorted(
            (walker.accessed & set(_RESTRICTED_ACCESS_NAMES))
            - walker.local
            - defined
        )
    )
    return {
        "loop_version": LOOP_VERSION,
        "defined_forbidden": defined_forbidden,
        "accessed_forbidden": accessed_forbidden,
        "ok": not defined_forbidden and not accessed_forbidden,
        "checked_names": len(NOT_PROVIDED_BY_P16),
    }


# ---------------------------------------------------------------------------
# 导出清单
# ---------------------------------------------------------------------------

__all__ = [
    "BudgetLedger",
    "DEFAULT_EFFECT_THRESHOLD",
    "DEFAULT_FREEZE_CAP",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_N_RESAMPLES",
    "DEFAULT_PRIMARY_METRIC",
    "DEFAULT_SEED",
    "DEFAULT_SUBGROUP",
    "DiscoveryArchive",
    "ExplorationOutcome",
    "FORBIDDEN_CALL_KEYS",
    "LOOP_BOUNDARY_NOTE",
    "LOOP_VERSION",
    "LoopBudget",
    "LoopError",
    "LoopInputError",
    "LoopPolicyError",
    "LoopStateError",
    "NOT_PROVIDED_BY_P16",
    "OPEN_STATUSES",
    "ROUND_CONFIRMED",
    "ROUND_PENDING_CONFIRMATION",
    "ROUND_SEARCH_FAILED",
    "ROUND_SKIPPED",
    "ROUND_STATUSES",
    "STOPPING_RULE_NOTE",
    "STOP_CODES",
    "STOP_NOT_STOPPED",
    "STOP_NO_CANDIDATES",
    "STOP_NO_CONFIRMATION_DATA",
    "STOP_RESOURCE_BUDGET",
    "STOP_ROUNDS_EXHAUSTED",
    "SYNTHETIC_DEMO_NOTICE",
    "SYNTHETIC_EVALUATOR_NOTE",
    "SYNTHETIC_METHOD_TEXT",
    "RoundRecord",
    "StopDecision",
    "assert_frozen_unchanged",
    "build_exploration",
    "canonical_json",
    "counterexamples_from_patterns",
    "evaluate_hypotheses",
    "evaluate_stop",
    "form_hypotheses",
    "freeze_snapshot",
    "frozen_candidate_ids",
    "frozen_unchanged",
    "inconclusive_entries",
    "open_questions_from",
    "run_demo",
    "run_loop",
    "select_for_freeze",
    "self_check",
    "synthetic_batch_evaluator",
    "synthetic_records",
    "synthetic_spec",
    "unavailable_evaluator",
]
