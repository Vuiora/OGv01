"""P15 交付物：知识归档与假说更新（模块 M8）。

本模块是**「探索—冻结—确证—更新」闭环的收尾**：P13 已经把某轮确证结果做了
语义三分类并登记发布，P14 已经给出下一轮的主动取证建议；本模块负责把确证结果
**归档成可追溯的发现档案**、据此**生成新版知识库**，并对假说作出
「保留 / 修订 / 退役」的决定。理论依据见
`SDL算法框架说明.md <../SDL算法框架说明.md>`_ §3「假说对象与发现档案」、
§5 主循环伪代码（``A, K ← 归档结果、更新证据等级和版本``）、§6「数据隔离与
跨轮误差控制」（已见确证数据及其结果可以启发修订，**但修订版本不能重复使用
旧数据证明自身**）；阶段定义见
`DS协作指南-阶段划分与提示词.md <../DS协作指南-阶段划分与提示词.md>`_ §3 P15；
接口约定见 `INTERFACES.md <../INTERFACES.md>`_ §1 与 §3
（``archive_confirmation(binding_id) -> {historical_ref}``：*custodian after
recorded+released result, preserve original consumed binding; creates historical
exploration view (not new confirmation)*）。

三条验收标准的实现方式
----------------------

① **证据等级严格按 E0/E1/E2 记录，E2 不自动升级为因果。**
   等级由 :func:`archive_round` **机械判定**，规则穷尽且互斥（凭据记在
   :attr:`EvidenceEntry.grade_basis`）：

   1. 本轮结论不是 ``supported``（``refuted`` / ``inconclusive`` / ``failed``）
      → :data:`GRADE_E0`／:data:`BASIS_NOT_SUPPORTED`。
      **反驳与证据不足都不构成「独立确证支持」**，只能作为探索线索记录。
   2. 支持，但本轮所用的确证数据身份**已经见过**（见验收标准③）
      → :data:`GRADE_E0`／:data:`BASIS_REUSED_SEEN_DATA`。
   3. 支持，且该假说**此前没有任何确证级记录**
      → :data:`GRADE_E1`／:data:`BASIS_FIRST_CONFIRMATION`。
   4. 支持，此前已有确证级记录，且本轮数据身份未见过
      → :data:`GRADE_E2`，凭据为 :data:`BASIS_NEW_ENVIRONMENT`
      （出现了既往确证记录中未见过的环境）或 :data:`BASIS_NEW_SAMPLE`
      （环境相同，但样本批次是新的）。

   因果**不是第十个等级、也不由等级派生**：:data:`CAUSAL_UPGRADE_BY_GRADE`
   恒为 ``False``，:func:`archive_round` **不接受**任何 ``evidence_grade`` /
   ``grade`` / ``promote_to`` / ``causal_grade`` 一类参数
   （:func:`_reject_forbidden_kwargs` 直接报错），因果识别只能由调用方以
   ``causal_claim=True`` + 非空 ``identification`` 声明**显式提出**，
   并以 :data:`CAUSAL_IDENTIFICATION_DECLARED` 单独登记。
   相关口径常量：:data:`GRADE_NOT_CAUSAL_NOTE`、:data:`CAUSAL_SCOPE_NOTE`、
   :data:`CAUSAL_REQUIRES_IDENTIFICATION_NOTE`。

② **旧确证记录保留历史身份，新结论必须绑定新版本与新证据。**
   三重落点：

   - **只追加**：:class:`KnowledgeVersion` 与本模块所有结果对象都是冻结对象，
     :func:`update_knowledge_version` 只在旧版本之上**追加**归档轮次与退役条目，
     从不删除、不覆盖、不改写既有条目；:meth:`KnowledgeVersion.history`
     可回溯整条版本链，旧条目的 ``grade`` / ``status`` / ``recorded_at``
     逐字节保持原样（:data:`HISTORICAL_IDENTITY_NOTE`）。
   - **绑定新版本**：:func:`archive_round` **强制要求** ``hypothesis_versions``
     逐假说给出被确证的假说版本号，缺失即报错、**不做默认填充**
     （:data:`VERSION_BINDING_NOTE`）；:func:`revise_or_retire` 走修订路径时
     要求 ``changes["provenance"]["knowledge_version"]`` 必须等于新的知识库版本，
     否则拒绝（:data:`VERSION_BINDING_NOTE`）。
   - **绑定新证据**：确证级条目必须携带未使用过的 ``evidence_identity``；
     :class:`KnowledgeVersion` 在构造时就校验「同一假说 id 不得有两条确证级条目
     落在同一份数据身份上」（:func:`_validate_archives`），且
     ``E1`` 必须首次、``E2`` 必须有先行确证——手写伪造的状态同样会被挡住。

③ **已见确证数据可回探索，但修订版本不得用旧数据自证。**
   两个动作 + 一道闸门：

   - **回探索**：:func:`archive_round` 调用 M1 的 ``archive_confirmation``
     把原绑定转入历史视图（``historical_ref``），并**只**通过
     ``read_dataset(historical_ref)`` 读取这个**已解锁的历史探索视图**来
     计算数据身份——本模块**没有任何**读取封存/未解锁确证分区的路径
     （``consume_confirmation`` / ``bind_confirmation`` / ``record_evaluation`` /
     ``release_results`` / ``quality`` / ``load_snapshot`` 均在
     :func:`_forbidden_access_names` 的黑名单内，由测试用 AST 机检）。
   - **数据身份按内容判定**：:func:`_data_identity` 对历史视图记录**剔除
     ``record_id`` 与 ``_quality`` 后**做规范摘要——换名字、换引用都不能
     伪装成新数据；这直接把「修订版本不得用旧数据自证」变成可机检的比较。
   - **闸门**：只要本轮数据身份在该假说**谱系内**出现过，等级一律压回
     :data:`GRADE_E0`，并置 :attr:`EvidenceEntry.self_evidence_attempt`
     为 ``True``（:data:`REUSE_FORBIDS_SELF_EVIDENCE_NOTE`）。
     :func:`revise_or_retire` 产出的 :class:`RevisionDecision` 进一步携带
     ``forbidden_evidence_identities`` 与 :meth:`RevisionDecision.assert_evidence_is_fresh`，
     使下游（P16 主循环）在**下一次**确证前就能机械拒绝复用。

本模块的定位与边界
------------------

- **不重新分类、不重算统计量。** 本轮结论（``supported`` / ``refuted`` /
  ``inconclusive`` / ``failed``）与计数一概**原样采信** P13 的结果对象；
  本模块只做「结论 + 数据身份 → 证据等级 → 档案/知识库」的登记。
- **不生成确证计划、不执行检验、不登记发布。** 这些属 P11/P12/P13，
  其入口名列入 :data:`NOT_PROVIDED_BY_P15`，由测试用 AST 机检「本层未定义它们」。
- **不做主动取证。** 属 P14（``divergence`` / ``acquisition_plan``）。
- **不做端到端主循环。** 属 P16（``loop`` / ``run_pipeline``）。
- **不实现 M4 的版本仓库。** 本模块只**调用** ``HypothesisStore`` 的公开接口
  （``latest`` / ``revise``）与 ``Hypothesis.retire``，不重新实现版本不可覆盖语义。
- **不引入任何仓库内依赖。** 上游对象（M1 模块、P13 结果对象、M4 假说对象）
  一律按**鸭子类型**使用，因此本模块只依赖 Python 标准库——归档层不应把
  M6 的实现细节固化下来。
- 不修改 ``sdl_m01/``，不读取/打印/写入角色令牌、evidence 数据库内容或受限
  质量报告（``quality()`` 在访问黑名单内），不引入第三方依赖。

术语约定（全文一致）
--------------------

- **结论（status）**：P13 给出的契约四值，本模块**只读不造**。
- **证据等级（grade）**：本模块给出的记录约定，取值 :data:`EVIDENCE_GRADES`。
  它是**本系统的记录约定，不是统计量，也不是因果断言**。
- **确证级（confirmatory）**：等级为 ``E1`` 或 ``E2``，即 :data:`CONFIRMATORY_GRADES`。
- **数据身份（data identity）**：一批确证数据**按内容**派生的摘要，
  与引用名、绑定名无关（见验收标准③）。
- **证据身份（evidence identity）**：由数据身份、绑定标识、轮次与结论共同派生的
  摘要，用于知识库级别的「同一份证据不得被两条确证级结论共同引用」。
- **已见（seen）**：某假说谱系内**任何**归档条目引用过的数据身份；
  已见数据可以启发修订，但不得作为新的确证。
- **因果识别（identification）**：达到因果解释所需的额外识别条件或干预证据。
  本模块只登记「是否由调用方声明」，**绝不由证据等级推断**。
"""

from __future__ import annotations

import ast as _ast
import hashlib
import inspect
import json
import pathlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

__all__ = [
    # 异常
    "ArchiveError",
    "ArchiveInputError",
    "ArchivePolicyError",
    "ArchiveIntegrityError",
    # 常量：版本
    "ARCHIVE_VERSION",
    "KNOWLEDGE_VERSION_PREFIX",
    "INITIAL_KNOWLEDGE_VERSION",
    # 常量：结论四值（与 P13 逐字一致）
    "STATUS_SUPPORTED",
    "STATUS_REFUTED",
    "STATUS_INCONCLUSIVE",
    "STATUS_FAILED",
    "RESULT_STATUSES",
    # 常量：证据等级
    "GRADE_E0",
    "GRADE_E1",
    "GRADE_E2",
    "EVIDENCE_GRADES",
    "CONFIRMATORY_GRADES",
    "GRADE_RANK",
    "GRADE_LABELS",
    "GRADE_NOT_CAUSAL_NOTE",
    # 常量：等级凭据
    "BASIS_NOT_SUPPORTED",
    "BASIS_REUSED_SEEN_DATA",
    "BASIS_FIRST_CONFIRMATION",
    "BASIS_NEW_ENVIRONMENT",
    "BASIS_NEW_SAMPLE",
    "GRADE_BASIS_CODES",
    "BASIS_LABELS",
    # 常量：复现依据
    "REPLICATION_NONE",
    "REPLICATION_NEW_ENVIRONMENT",
    "REPLICATION_NEW_SAMPLE",
    "REPLICATION_BASES",
    # 常量：因果识别
    "CAUSAL_NOT_ADDRESSED",
    "CAUSAL_IDENTIFICATION_DECLARED",
    "CAUSAL_LEVELS",
    "CAUSAL_UPGRADE_BY_GRADE",
    "CAUSAL_SCOPE_NOTE",
    "CAUSAL_REQUIRES_IDENTIFICATION_NOTE",
    # 常量：动作
    "ACTION_RETAIN",
    "ACTION_REVISE",
    "ACTION_RETIRE",
    "ACTIONS",
    # 常量：口径说明
    "REUSE_FORBIDS_SELF_EVIDENCE_NOTE",
    "HISTORICAL_IDENTITY_NOTE",
    "VERSION_BINDING_NOTE",
    "FRESH_EVIDENCE_REQUIRED_NOTE",
    "ARCHIVE_SCOPE_NOTE",
    "HISTORICAL_VIEW_IS_UNLOCKED_NOTE",
    "E0_SCOPE_NOTE",
    "NO_RECLASSIFICATION_NOTE",
    "NOT_PROVIDED_BY_P15",
    "ARCHIVE_SEALED_KEYS",
    "FORBIDDEN_CALL_KEYS",
    # 入口
    "archive_round",
    "update_knowledge_version",
    "revise_or_retire",
    # 辅助
    "empty_knowledge",
    "grade_rank",
    "highest_grade",
    "data_profile",
    "assert_evidence_is_fresh",
    "check_no_sealed_leak",
    "self_check",
    # 结果对象
    "DataProfile",
    "EvidenceEntry",
    "RoundArchive",
    "RetirementEntry",
    "KnowledgeVersion",
    "RevisionDecision",
]


# ---------------------------------------------------------------------------
# 常量：版本
# ---------------------------------------------------------------------------

#: 交付物版本号。写入全部归档对象的 ``code_version``，便于追溯口径变更。
ARCHIVE_VERSION = "P15-v1.0"

#: 知识库版本号前缀。``K`` 之后是十进制序号，从 0 开始。
KNOWLEDGE_VERSION_PREFIX = "K"

#: 初始（空）知识库版本号。空知识库有版本号，表示「已初始化但尚无任何发现」，
#: 与「没有知识库」严格区分——前者可以参与新颖性比较，后者不能。
INITIAL_KNOWLEDGE_VERSION = "K0"


# ---------------------------------------------------------------------------
# 常量：契约四值结论（与 P13 逐字一致，本模块只读不造）
# ---------------------------------------------------------------------------

STATUS_SUPPORTED = "supported"
STATUS_REFUTED = "refuted"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_FAILED = "failed"

#: 合法结论取值集合。**刻意在本模块内独立声明**，并用测试与
#: ``sdl_m06.report.RESULT_STATUSES`` 逐字比对：
#: 归档层若把 M6 的取值表直接 import 进来，就把 M6 的实现细节固化成了
#: M8 的编译期依赖；而若两处静默漂移，归档记录就会与确证记录对不上。
#: 「独立声明 + 测试比对」同时满足解耦与一致性两个要求。
RESULT_STATUSES: tuple[str, ...] = (
    STATUS_SUPPORTED,
    STATUS_REFUTED,
    STATUS_INCONCLUSIVE,
    STATUS_FAILED,
)


# ---------------------------------------------------------------------------
# 常量：证据等级（验收标准①）
# ---------------------------------------------------------------------------

#: ``E0`` 探索线索：尚未获得独立确证支持（含反驳、证据不足、失败与复用旧数据）。
GRADE_E0 = "E0"

#: ``E1`` 独立确证支持：该假说**首次**获得独立确证支持。
GRADE_E1 = "E1"

#: ``E2`` 新环境或新样本复现：在既往确证之外的**新数据**上复现。
GRADE_E2 = "E2"

#: **合法等级取值集合**，由低到高。
EVIDENCE_GRADES: tuple[str, ...] = (GRADE_E0, GRADE_E1, GRADE_E2)

#: 确证级等级。只有这两个等级才构成「独立确证支持」。
CONFIRMATORY_GRADES: tuple[str, ...] = (GRADE_E1, GRADE_E2)

#: 等级序（用于取「最高等级」）。数值只用于比较，不参与任何加权。
GRADE_RANK: Mapping[str, int] = MappingProxyType(
    {GRADE_E0: 0, GRADE_E1: 1, GRADE_E2: 2}
)

#: 等级的中文标签。**只用于展示与 notes 文本**，结构化字段一律用 ``E0``/``E1``/``E2``。
GRADE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        GRADE_E0: "E0 探索线索（尚无独立确证支持）",
        GRADE_E1: "E1 独立确证支持（首次）",
        GRADE_E2: "E2 新环境或新样本复现",
    }
)

#: **验收标准①的核心口径**：等级是记录约定，不是因果断言。
GRADE_NOT_CAUSAL_NOTE = (
    "证据等级 E0/E1/E2 是本系统的**记录约定**，不是统计量，也不是因果断言。"
    "E2 表示该假说在既往确证之外的新环境或新样本上复现，"
    "**不表示**已经识别出因果机制；统计关联本身不构成因果证明。"
    "等级永远不派生因果标签——因果只能由显式的识别条件或干预证据支撑。"
)


# ---------------------------------------------------------------------------
# 常量：等级凭据（可机检的判定理由）
# ---------------------------------------------------------------------------

BASIS_NOT_SUPPORTED = "not_supported"
BASIS_REUSED_SEEN_DATA = "reused_seen_confirmation_data"
BASIS_FIRST_CONFIRMATION = "first_independent_confirmation"
BASIS_NEW_ENVIRONMENT = "new_environment_replication"
BASIS_NEW_SAMPLE = "new_sample_replication"

#: 全部判定凭据。与 :data:`EVIDENCE_GRADES` 一起构成「判定穷尽且互斥」的机检形式。
GRADE_BASIS_CODES: tuple[str, ...] = (
    BASIS_NOT_SUPPORTED,
    BASIS_REUSED_SEEN_DATA,
    BASIS_FIRST_CONFIRMATION,
    BASIS_NEW_ENVIRONMENT,
    BASIS_NEW_SAMPLE,
)

BASIS_LABELS: Mapping[str, str] = MappingProxyType(
    {
        BASIS_NOT_SUPPORTED: "本轮结论不是 supported，只可作为探索线索",
        BASIS_REUSED_SEEN_DATA: "本轮所用确证数据在该假说谱系内已经见过，禁止自证",
        BASIS_FIRST_CONFIRMATION: "该假说首次获得独立确证支持",
        BASIS_NEW_ENVIRONMENT: "在既往确证未覆盖的环境中复现",
        BASIS_NEW_SAMPLE: "环境相同，但在新的样本批次上复现",
    }
)


# ---------------------------------------------------------------------------
# 常量：复现依据
# ---------------------------------------------------------------------------

REPLICATION_NONE = "none"
REPLICATION_NEW_ENVIRONMENT = "new_environment"
REPLICATION_NEW_SAMPLE = "new_sample"

#: 复现依据取值。``none`` 表示本轮不构成复现（首次确证或未达确证级）。
REPLICATION_BASES: tuple[str, ...] = (
    REPLICATION_NONE,
    REPLICATION_NEW_ENVIRONMENT,
    REPLICATION_NEW_SAMPLE,
)


# ---------------------------------------------------------------------------
# 常量：因果识别（验收标准①的后半句）
# ---------------------------------------------------------------------------

#: 未提出因果主张（默认）。
CAUSAL_NOT_ADDRESSED = "not_addressed"

#: 调用方显式声明了识别条件或干预证据。
CAUSAL_IDENTIFICATION_DECLARED = "identification_declared"

CAUSAL_LEVELS: tuple[str, ...] = (
    CAUSAL_NOT_ADDRESSED,
    CAUSAL_IDENTIFICATION_DECLARED,
)

#: **恒为 False**：证据等级绝不升级为因果。这是验收标准①的可机检形式。
CAUSAL_UPGRADE_BY_GRADE = False

CAUSAL_SCOPE_NOTE = (
    "因果识别是与证据等级**正交**的独立标签，取值 "
    f"{CAUSAL_NOT_ADDRESSED!r} / {CAUSAL_IDENTIFICATION_DECLARED!r}。"
    "本模块只登记调用方是否显式声明识别条件，绝不由 E1/E2 推断因果，"
    "也不提供任何『升级为因果』的入口。"
)

CAUSAL_REQUIRES_IDENTIFICATION_NOTE = (
    "声明因果主张却未给出识别条件（identification）一律拒绝："
    "因果识别需要额外的识别条件或干预证据，不能仅凭统计关联或复现等级取得。"
)


# ---------------------------------------------------------------------------
# 常量：假说更新动作（验收标准②）
# ---------------------------------------------------------------------------

ACTION_RETAIN = "retain"
ACTION_REVISE = "revise"
ACTION_RETIRE = "retire"

ACTIONS: tuple[str, ...] = (ACTION_RETAIN, ACTION_REVISE, ACTION_RETIRE)


# ---------------------------------------------------------------------------
# 常量：口径说明（随结果对象输出，不与结构化字段混用）
# ---------------------------------------------------------------------------

REUSE_FORBIDS_SELF_EVIDENCE_NOTE = (
    "已见确证数据可以回探索、可以启发修订，**但不得被同一修订版本复用为确证**。"
    "本模块以**内容级数据身份**（剔除记录标识后的规范摘要）识别「已见」，"
    "凡在该假说谱系内出现过的数据身份，其复现一律压回 E0 并置 "
    "self_evidence_attempt=True，绝不因「换了引用名」而放行。"
)

HISTORICAL_IDENTITY_NOTE = (
    "旧确证记录保留历史身份：知识库只追加、不删除、不覆盖。"
    "假说退役只是在档案上追加一条退役条目，既有条目的等级、结论与记录时间"
    "逐字节保持原样，仍可按版本链回溯。"
)

VERSION_BINDING_NOTE = (
    "新结论必须绑定新版本与新证据：归档时逐假说显式给出假说版本号"
    "（缺失即报错，不做默认填充）；修订时修订版本的 provenance.knowledge_version "
    "必须等于新的知识库版本。"
)

FRESH_EVIDENCE_REQUIRED_NOTE = (
    "任何后续确证都必须使用**未见过**的证据："
    "本模块给出该假说谱系已用过的全部数据身份与证据身份，"
    "下游在冻结前即可机械拒绝复用。"
)

ARCHIVE_SCOPE_NOTE = (
    "本模块只做归档、知识库版本更新与假说更新决策；"
    "不重新分类、不重算统计量、不生成确证计划、不执行检验、不登记发布、"
    "不设计取证方案、不实现端到端主循环。"
)

HISTORICAL_VIEW_IS_UNLOCKED_NOTE = (
    "本模块读取的**只有** M1 在归档后返回的历史探索视图（H_ 引用）："
    "它已从确证分区解锁、可用于探索。本模块没有任何读取封存或未解锁确证分区的路径，"
    "也不触碰受限质量报告。"
)

E0_SCOPE_NOTE = (
    "E0 是**兜底等级**而非丢弃：反驳、证据不足、失败与复用旧数据的轮次都会留下"
    "归档条目（含判定凭据），因为「算出来不支持」与「没算成」本身就是要保留的发现。"
)

NO_RECLASSIFICATION_NOTE = (
    "本轮结论（supported / refuted / inconclusive / failed）与计数一概原样采信"
    "P13 的结果对象，本模块不重新分类、不重算任何统计量，也不授予统计结论。"
)


# ---------------------------------------------------------------------------
# 常量：边界声明（由测试用 AST 机检）
# ---------------------------------------------------------------------------

#: 本模块**不应定义**的名字：后续阶段的入口、上游的构造与执行口径、
#: 以及 M1 的接口名（本层只调用、不重新实现）。
NOT_PROVIDED_BY_P15: tuple[str, ...] = (
    # P16 的端到端主循环
    "loop",
    "run_pipeline",
    "main_loop",
    "run_loop",
    # P14 的主动取证
    "divergence",
    "acquisition_plan",
    # P13 的结果分类与登记发布
    "classify_result",
    "record_and_release",
    # P12 的检验执行
    "execute_family",
    "execute_tests",
    "holm_adjust",
    "alpha_for_round",
    # P11 的冻结计划生成与绑定
    "build_frozen_plan",
    "bind",
    # P10 / P09 的筛选与度量
    "evaluate_hypothesis",
    "pareto_front",
    # M1 的数据与证据接口（本层只通过鸭子类型调用，不重新实现）
    "initialize",
    "build",
    "read_dataset",
    "quality",
    "bind_confirmation",
    "consume_confirmation",
    "record_evaluation",
    "release_results",
    "archive_confirmation",
    "add_confirmation",
    "ledger",
    "verify_integrity",
    # M4 的假说版本仓库（本层只调用其公开接口）
    "register",
    "publish",
    "revise",
    "retire",
)

#: 归档对象在结构上必须拒绝的「封存原始记录特征键」（小写比较）。
#: 与 P13 的同名常量语义一致：键名是结构性的、可穷举的边界；按文本猜疑字符串值
#: 会产生大量假阳性。测试会把它与 ``sdl_m06.report.SEALED_LEAK_KEYS`` 对齐核对，
#: 并断言本模块产出的键集与该黑名单**不相交**。
ARCHIVE_SEALED_KEYS: frozenset[str] = frozenset(
    {
        # 记录正文与标识
        "values",
        "records",
        "records_body",
        "raw",
        "raw_records",
        "dataset",
        "dataset_ref",
        "record_id",
        "group_ids",
        "operator_annotation",
        # 封存与质量
        "snapshot",
        "snapshot_hash",
        "sealed_counts",
        "sealed_report",
        "quality_report",
        "_quality",
        # 来源与时间戳（原始资料的定位信息）
        "source",
        "source_ref",
        "available_time",
        "event_time",
        "units",
        # 令牌
        "token",
        "role_token",
        "token_body",
    }
)

#: 明确拒绝的关键字参数。三类：
#:
#: 1. **等级指定类**：等级必须由本模块机械判定，不接受调用方指定，
#:    更不接受「升级到因果」；
#: 2. **数据读取类**：本层只经归档后的历史探索视图取数据身份，不接受数据入口；
#: 3. **令牌类**：不得进入本层输入。
FORBIDDEN_CALL_KEYS: frozenset[str] = frozenset(
    {
        "evidence_grade",
        "grade",
        "force_grade",
        "promote_to",
        "upgrade_to",
        "causal_grade",
        "causal_upgrade",
        "upgrade_to_causal",
        "records",
        "raw_records",
        "rows",
        "dataset",
        "dataset_ref",
        "token",
        "role_token",
        "token_body",
    }
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ArchiveError(ValueError):
    """本模块所有异常的基类，继承自 :class:`ValueError`。"""


class ArchiveInputError(ArchiveError):
    """输入形态非法：类型不对、缺字段、键名受限、参数越界、版本号非法等。"""


class ArchivePolicyError(ArchiveError):
    """口径违规：等级被指定、因果无识别条件、确证级条目复用已见数据等。

    单列一个子类，让「输入的形态错了」与「输入触犯了归档口径」在异常类型上
    就可区分：前者是使用者写错了，后者是「这件事本身不允许」。
    """


class ArchiveIntegrityError(ArchiveError):
    """知识库状态自相矛盾：E1 非首次、E2 无先行确证、同一证据被两条确证级引用等。"""


# ---------------------------------------------------------------------------
# 基础校验与序列化工具
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """把任意嵌套结构转换为 JSON 兼容形态（只读视图、元组、集合都会被摊平）。"""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical(value: Any) -> str:
    """规范 JSON：键排序、紧凑分隔、保留非 ASCII。

    本模块**不复用** ``sdl_m06.execute.canonical_json``，因为规范串是摘要的
    唯一输入，一旦上游改了分隔符口径，历史归档的摘要就会整体漂移。
    一致性由测试按「同一输入 → 同一摘要」自校验，而不是靠共享实现。
    """
    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _plain(value: Any) -> Any:
    """把只读视图还原为可 JSON 序列化的普通对象。"""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(item) for item in value), key=lambda item: repr(item))
    return value


def _freeze(value: Any) -> Any:
    """深度冻结：映射转只读视图，序列转元组。"""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=lambda item: repr(item)))
    return value


def _require_text(value: Any, label: str) -> str:
    """校验非空字符串。

    :raises ArchiveInputError: 非字符串或全为空白。
    """
    if not isinstance(value, str) or not value.strip():
        raise ArchiveInputError(f"{label} 必须是非空字符串，实际得到：{value!r}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """校验映射类型。

    :raises ArchiveInputError: 非映射。
    """
    if not isinstance(value, Mapping):
        raise ArchiveInputError(f"{label} 必须是映射，实际得到：{type(value).__name__}")
    return value


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    """校验序列类型（排除字符串这类「像序列的字符串」）。

    :raises ArchiveInputError: 不是序列。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ArchiveInputError(f"{label} 必须是序列，实际得到：{type(value).__name__}")
    return value


def _require_int(value: Any, label: str) -> int:
    """校验正整数（``bool`` 不算）。

    :raises ArchiveInputError: 非正整数。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArchiveInputError(f"{label} 必须是正整数，实际得到：{value!r}")
    return value


def _require_grade(value: Any, label: str) -> str:
    """校验证据等级取值。

    :raises ArchiveInputError: 取值不在 :data:`EVIDENCE_GRADES` 内。
    """
    if value not in EVIDENCE_GRADES:
        raise ArchiveInputError(
            f"{label} 取值 {value!r} 不在合法等级内（{list(EVIDENCE_GRADES)}）。"
            + GRADE_NOT_CAUSAL_NOTE
        )
    return value


def _require_status(value: Any, label: str) -> str:
    """校验结论取值严格限于契约四值（本模块只读不造）。

    :raises ArchiveInputError: 取值越界。
    """
    if value not in RESULT_STATUSES:
        raise ArchiveInputError(
            f"{label} 取值 {value!r} 不在契约四值内（{list(RESULT_STATUSES)}）；"
            "本轮结论由 P13 给出，本模块只原样采信。" + NO_RECLASSIFICATION_NOTE
        )
    return value


def _reject_forbidden_kwargs(kwargs: Mapping[str, Any], where: str) -> None:
    """拒绝受控关键字参数（等级指定、数据读取、令牌类）。

    :raises ArchivePolicyError: 提供了等级指定类参数。
    :raises ArchiveInputError: 提供了其它受控参数。
    """
    if not isinstance(kwargs, Mapping):
        return None
    hits = sorted(key for key in kwargs if isinstance(key, str) and key in FORBIDDEN_CALL_KEYS)
    if not hits:
        return None
    grade_like = sorted(
        key for key in hits
        if key in {"evidence_grade", "grade", "force_grade", "promote_to",
                   "upgrade_to", "causal_grade", "causal_upgrade", "upgrade_to_causal"}
    )
    if grade_like:
        raise ArchivePolicyError(
            f"{where} 不接受 {grade_like} 一类参数：证据等级由本模块**机械判定**，"
            "因果主张必须走 causal_claim + identification 显式声明。" + CAUSAL_SCOPE_NOTE
        )
    raise ArchiveInputError(
        f"{where} 不接受的参数 {hits}：本层只经归档后的历史探索视图取数据身份，"
        "令牌与原始记录正文不得进入本层输入。"
    )


def _reject_token_keys(mapping: Mapping[str, Any], label: str) -> None:
    """拒绝任何疑似携带令牌正文的键。

    :raises ArchiveInputError: 命中令牌类键名。
    """
    for key in mapping:
        if isinstance(key, str) and key.lower() in (
            "token",
            "tokens",
            "role_token",
            "raw_token",
            "token_body",
            "secret",
            "password",
        ):
            raise ArchiveInputError(
                f"{label} 含受限键名 {key!r}：令牌正文不得进入本层输入、日志或版本库。"
            )


def check_no_sealed_leak(value: Any, label: str) -> None:
    """深度扫描对象，拒绝任何封存原始记录的特征键（验收标准③的出闸闸门）。

    递归遍历映射与序列；键名（小写后）命中 :data:`ARCHIVE_SEALED_KEYS` 即报错。
    只扫**键名**不扫字符串值：键名是可穷举的结构边界，而中文说明里出现
    「values」之类的词属正常，按文本猜疑会把闸门变成噪声。

    :param value: 待检查的对象（任意嵌套结构）。
    :param label: 出错信息中的位置说明。
    :raises ArchiveIntegrityError: 命中封存键名。
    """
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if isinstance(key, str) and key.lower() in ARCHIVE_SEALED_KEYS:
                    raise ArchiveIntegrityError(
                        f"{label} 含封存字段名 {key!r}：归档对象只承载结论、等级与摘要，"
                        "绝不承载封存原始记录正文。"
                    )
                stack.append(item)
        elif isinstance(current, (list, tuple, set, frozenset)):
            for item in current:
                stack.append(item)
    return None


# ---------------------------------------------------------------------------
# 数据身份与数据画像（验收标准③的判定基础）
# ---------------------------------------------------------------------------


def _content_item(row: Mapping[str, Any]) -> Any:
    """取一条记录中**与身份相关**的内容：剔除记录标识与质量标记。

    剔除 ``record_id``：记录标识是可以改写的引用名，而 M1 自己的记录指纹
    （``assess_records`` 的 ``fingerprint``）也刻意排除 ``record_id``。
    若把 ``record_id`` 计入身份，一份数据只要换一批编号就能伪装成「新数据」，
    「修订版本不得用旧数据自证」这条边界就会被绕过。
    剔除 ``_quality``：它是 M1 附加的质量视图，不属于数据内容本身。
    """
    return {
        str(key): _jsonable(item)
        for key, item in row.items()
        if key not in ("record_id", "_quality")
    }


@dataclass(frozen=True)
class DataProfile:
    """一批确证数据的内容画像（只含摘要与聚合元信息，不含记录正文）。

    :param identity: **内容级数据身份**：对剔除记录标识后的记录内容做规范摘要。
        换引用名、换绑定名都不改变它。
    :param record_count: 记录条数（聚合量，不是记录正文）。
    :param environments: 出现过的环境（升序去重），用于判定「新环境复现」。
    :param code_version: 口径版本。
    """

    identity: str
    record_count: int
    environments: tuple[str, ...] = ()
    code_version: str = ARCHIVE_VERSION

    def __post_init__(self) -> None:
        _require_text(self.identity, "DataProfile.identity")
        _require_int(self.record_count, "DataProfile.record_count")
        object.__setattr__(self, "environments", tuple(str(item) for item in self.environments))

    def to_dict(self) -> dict:
        return {
            "identity": self.identity,
            "record_count": self.record_count,
            "environments": list(self.environments),
            "code_version": self.code_version,
        }

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def data_profile(rows: Any) -> DataProfile:
    """由一批记录（通常是归档后的历史探索视图）计算 :class:`DataProfile`。

    **只返回摘要与聚合量**，记录正文在函数返回时即被丢弃，绝不离开本函数。

    :param rows: 记录序列，每项为映射。
    :raises ArchiveInputError: 不是非空序列，或某项不是映射。
    """
    items = _require_sequence(rows, "历史视图记录")
    if len(items) == 0:
        raise ArchiveInputError(
            "历史探索视图为空，无法计算数据身份："
            "空数据不能作为确证证据，也不能凭它授予任何证据等级。"
        )
    payload: list[Any] = []
    environments: set[str] = set()
    for index, row in enumerate(items):
        if not isinstance(row, Mapping):
            raise ArchiveInputError(
                f"历史视图第 {index} 条不是映射（{type(row).__name__}）："
                "记录视图必须是映射序列。"
            )
        payload.append(_content_item(row))
        environment = row.get("environment")
        if isinstance(environment, str) and environment.strip():
            environments.add(environment)
    payload.sort(key=lambda item: _canonical(item))
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return DataProfile(
        identity=digest,
        record_count=len(items),
        environments=tuple(sorted(environments)),
    )


def _evidence_identity(
    data_identity: str,
    binding_id: str,
    round_index: int,
    status: str,
    hypothesis_id: str,
) -> str:
    """派生证据身份：假说 + 数据身份 + 绑定 + 轮次 + 结论。

    **必须含假说标识**：一轮确证里多个假说共用同一份数据、同一个绑定与同一个
    轮次，若身份里不含假说，「同一份数据支撑两条确证结论」的闸门就会把同轮内
    正常的多个假说误判成违规；反之，含了假说标识之后，闸门才能真正只拦截
    「同一假说、同一份数据反复充当确证」——这才是那条边界要防的事。
    """
    body = _canonical(
        {
            "hypothesis_id": hypothesis_id,
            "data_identity": data_identity,
            "binding_id": binding_id,
            "round_index": round_index,
            "status": status,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 等级工具
# ---------------------------------------------------------------------------


def grade_rank(grade: str) -> int:
    """等级的序（``E0`` < ``E1`` < ``E2``）。

    :raises ArchiveInputError: 取值越界。
    """
    _require_grade(grade, "grade")
    return GRADE_RANK[grade]


def highest_grade(grades: Any) -> str | None:
    """取若干等级中的最高者；空输入返回 ``None``（**不是** ``E0``）。

    「没有任何记录」与「记录为 E0」必须可区分：前者说明该假说尚未进入确证，
    后者说明确证过但未获支持。把二者混为一谈会让「没算」被读成「算了不支持」。
    """
    items = [item for item in _require_sequence(grades, "grades")]
    for item in items:
        _require_grade(item, "grades 元素")
    if not items:
        return None
    return max(items, key=lambda item: GRADE_RANK[item])


# ---------------------------------------------------------------------------
# 归档结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceEntry:
    """一条逐假说的归档证据条目（验收标准①②的载体）。

    :param hypothesis_id: 假说稳定标识。
    :param hypothesis_version: 被确证的假说**版本号**（必填，不默认填充）。
    :param status: 本轮结论，限契约四值（原样采信 P13）。
    :param grade: 本模块机械判定的证据等级。
    :param grade_basis: 判定凭据，取值 :data:`GRADE_BASIS_CODES`。
    :param prior_grade: 归档前该假说谱系的最高等级；``None`` 表示尚无任何记录。
    :param replication_basis: 复现依据，取值 :data:`REPLICATION_BASES`。
    :param data_identity: 本轮确证数据的内容级身份。
    :param evidence_identity: 本轮证据身份。
    :param binding_id: 绑定标识。
    :param round_index: 研究轮次 :math:`t`（**不是**对话轮次）。
    :param historical_ref: 归档后返回的历史探索视图引用。
    :param environments: 本轮数据出现过的环境。
    :param record_count: 本轮记录条数（聚合量）。
    :param causal_identification: 因果识别标签，取值 :data:`CAUSAL_LEVELS`。
    :param causal_claim: 调用方是否显式提出了因果主张。
    :param self_evidence_attempt: 是否因复用已见数据而被压回 ``E0``。
    :param recorded_at: 归档时间（调用方提供；``None`` 表示未提供，不自动生成）。
    :param code_version: 归档口径版本。
    :param notes: 口径说明元组。
    """

    hypothesis_id: str
    hypothesis_version: str
    status: str
    grade: str
    grade_basis: str
    prior_grade: str | None
    replication_basis: str
    data_identity: str
    evidence_identity: str
    binding_id: str
    round_index: int
    historical_ref: str
    environments: tuple[str, ...] = ()
    record_count: int = 0
    causal_identification: str = CAUSAL_NOT_ADDRESSED
    causal_claim: bool = False
    self_evidence_attempt: bool = False
    recorded_at: str | None = None
    code_version: str = ARCHIVE_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.hypothesis_id, "EvidenceEntry.hypothesis_id")
        _require_text(self.hypothesis_version, "EvidenceEntry.hypothesis_version")
        _require_status(self.status, "EvidenceEntry.status")
        _require_grade(self.grade, "EvidenceEntry.grade")
        if self.grade_basis not in GRADE_BASIS_CODES:
            raise ArchiveInputError(
                f"grade_basis 取值 {self.grade_basis!r} 不在合法凭据内"
                f"（{list(GRADE_BASIS_CODES)}）。"
            )
        if self.prior_grade is not None:
            _require_grade(self.prior_grade, "EvidenceEntry.prior_grade")
        if self.replication_basis not in REPLICATION_BASES:
            raise ArchiveInputError(
                f"replication_basis 取值 {self.replication_basis!r} 不在"
                f"（{list(REPLICATION_BASES)}）内。"
            )
        if self.causal_identification not in CAUSAL_LEVELS:
            raise ArchiveInputError(
                f"causal_identification 取值 {self.causal_identification!r} 不在"
                f"（{list(CAUSAL_LEVELS)}）内。" + CAUSAL_SCOPE_NOTE
            )
        if not isinstance(self.causal_claim, bool):
            raise ArchiveInputError(
                f"causal_claim 必须是布尔值，实际得到：{self.causal_claim!r}"
            )
        if not isinstance(self.self_evidence_attempt, bool):
            raise ArchiveInputError(
                f"self_evidence_attempt 必须是布尔值，实际得到：{self.self_evidence_attempt!r}"
            )
        if self.recorded_at is not None:
            _require_text(self.recorded_at, "EvidenceEntry.recorded_at")
        # -- 判定与凭据必须自洽（防止手写伪造的条目混进知识库） -------------
        if self.grade == GRADE_E0 and self.grade_basis in (
            BASIS_FIRST_CONFIRMATION,
            BASIS_NEW_ENVIRONMENT,
            BASIS_NEW_SAMPLE,
        ):
            raise ArchiveIntegrityError(
                f"等级 {GRADE_E0} 与凭据 {self.grade_basis!r} 自相矛盾："
                "确证类凭据只能对应确证级等级。"
            )
        if self.grade in CONFIRMATORY_GRADES and self.grade_basis in (
            BASIS_NOT_SUPPORTED,
            BASIS_REUSED_SEEN_DATA,
        ):
            raise ArchiveIntegrityError(
                f"等级 {self.grade} 与凭据 {self.grade_basis!r} 自相矛盾："
                "非支持或复用旧数据不得取得确证级等级。" + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
            )
        object.__setattr__(self, "environments", tuple(str(item) for item in self.environments))
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    # -- 只读性质 ---------------------------------------------------------

    @property
    def is_confirmatory(self) -> bool:
        """是否确证级（``E1`` / ``E2``）。"""
        return self.grade in CONFIRMATORY_GRADES

    @property
    def grade_label(self) -> str:
        """等级的中文标签（只用于展示）。"""
        return GRADE_LABELS[self.grade]

    @property
    def identity(self) -> str:
        """版本标识 ``hypothesis_id@hypothesis_version``。"""
        return f"{self.hypothesis_id}@{self.hypothesis_version}"

    def assert_evidence_is_fresh(self, identity: str) -> None:
        """核对给定身份不是本条目已用过的证据。

        :raises ArchivePolicyError: 身份与该条目的数据/证据身份相同。
        """
        _require_text(identity, "identity")
        if identity in (self.data_identity, self.evidence_identity):
            raise ArchivePolicyError(
                f"身份 {identity!r} 已被 {self.identity} 的归档条目录用，"
                "不得再次作为确证证据。" + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
            )

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_version": self.hypothesis_version,
            "status": self.status,
            "grade": self.grade,
            "grade_basis": self.grade_basis,
            "prior_grade": self.prior_grade,
            "replication_basis": self.replication_basis,
            "data_identity": self.data_identity,
            "evidence_identity": self.evidence_identity,
            "binding_id": self.binding_id,
            "round_index": self.round_index,
            "historical_ref": self.historical_ref,
            "environments": list(self.environments),
            "record_count": self.record_count,
            "causal_identification": self.causal_identification,
            "causal_claim": self.causal_claim,
            "self_evidence_attempt": self.self_evidence_attempt,
            "recorded_at": self.recorded_at,
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoundArchive:
    """一次确证轮次的归档记录（发现档案 A 的单轮条目）。

    :param binding_id: 绑定标识。
    :param round_index: 研究轮次 :math:`t`。
    :param alpha: 本轮误差预算；``None`` 表示调用方未提供（不推断）。
    :param aggregate_status: 整轮结论（原样采信 P13）。
    :param historical_ref: 归档返回的历史探索视图引用。
    :param data_identity: 本轮确证数据的内容级身份。
    :param entries: 逐假说的证据条目（顺序与 P13 的 out口顺序一致）。
    :param receipt_digest: P13 登记发布凭证的摘要（可选）；用于追溯已登记证据。
    :param recorded_at: 归档时间（调用方提供）。
    :param code_version: 归档口径版本。
    :param notes: 口径说明元组。
    """

    binding_id: str
    round_index: int
    aggregate_status: str
    historical_ref: str
    data_identity: str
    entries: tuple[EvidenceEntry, ...]
    alpha: float | None = None
    receipt_digest: str | None = None
    recorded_at: str | None = None
    code_version: str = ARCHIVE_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.binding_id, "RoundArchive.binding_id")
        _require_int(self.round_index, "RoundArchive.round_index")
        _require_status(self.aggregate_status, "RoundArchive.aggregate_status")
        _require_text(self.historical_ref, "RoundArchive.historical_ref")
        _require_text(self.data_identity, "RoundArchive.data_identity")
        entries = tuple(self.entries)
        if not entries:
            raise ArchiveInputError(
                "RoundArchive 至少要有一条证据条目："
                "一轮确证即使全部失败也应留下条目（E0），否则该轮消耗将无从追溯。"
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, EvidenceEntry):
                raise ArchiveInputError(
                    f"RoundArchive 第 {index} 条不是 EvidenceEntry"
                    f"（{type(entry).__name__}）。"
                )
            if entry.binding_id != self.binding_id:
                raise ArchiveIntegrityError(
                    f"第 {index} 条证据归属绑定 {entry.binding_id!r}，"
                    f"与归档轮次的 {self.binding_id!r} 不一致。"
                )
            if entry.data_identity != self.data_identity:
                raise ArchiveIntegrityError(
                    f"第 {index} 条证据的数据身份与轮次数据身份不一致："
                    "一轮确证的所有条目必须落在同一份数据上。"
                )
        seen_ids = [entry.hypothesis_id for entry in entries]
        if len(set(seen_ids)) != len(seen_ids):
            raise ArchiveIntegrityError(
                f"同一轮次出现重复假说：{seen_ids}。逐假说条目必须唯一。"
            )
        if self.alpha is not None and (
            isinstance(self.alpha, bool) or not isinstance(self.alpha, (int, float))
        ):
            raise ArchiveInputError(
                f"alpha 必须是实数或 None，实际得到：{type(self.alpha).__name__}"
            )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    # -- 只读视图 ---------------------------------------------------------

    @property
    def hypothesis_ids(self) -> tuple[str, ...]:
        """本轮涉及的假说标识（按条目顺序）。"""
        return tuple(entry.hypothesis_id for entry in self.entries)

    @property
    def grades(self) -> tuple[str, ...]:
        """逐条等级（方便一次性核对取值合法性）。"""
        return tuple(entry.grade for entry in self.entries)

    @property
    def confirmatory_entries(self) -> tuple[EvidenceEntry, ...]:
        """确证级条目（``E1`` / ``E2``）。"""
        return tuple(entry for entry in self.entries if entry.is_confirmatory)

    @property
    def evidence_identities(self) -> tuple[str, ...]:
        """本轮全部证据身份。"""
        return tuple(entry.evidence_identity for entry in self.entries)

    def entry_of(self, hypothesis_id: str) -> EvidenceEntry:
        """按假说标识取条目。

        :raises ArchiveInputError: 该假说不在本轮归档内。
        """
        for entry in self.entries:
            if entry.hypothesis_id == hypothesis_id:
                return entry
        raise ArchiveInputError(f"假说 {hypothesis_id!r} 不在本轮归档记录内。")

    def to_dict(self) -> dict:
        return {
            "binding_id": self.binding_id,
            "round_index": self.round_index,
            "alpha": self.alpha,
            "aggregate_status": self.aggregate_status,
            "historical_ref": self.historical_ref,
            "data_identity": self.data_identity,
            "entries": [entry.to_dict() for entry in self.entries],
            "receipt_digest": self.receipt_digest,
            "recorded_at": self.recorded_at,
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RetirementEntry:
    """退役条目：记录某个假说版本退出活跃集合，同时保留其历史身份。

    :param hypothesis_id: 假说标识。
    :param identity: 被退役的版本标识 ``id@version``。
    :param reason: 退役理由（必填，不得空白）。
    :param final_grade: 退役时的最高等级；``None`` 表示谱系内无确证级记录。
    :param retained_identities: 退役后仍保留在档案中的版本标识（历史身份）。
    :param recorded_at: 退役记录时间（调用方提供）。
    :param code_version: 口径版本。
    :param notes: 口径说明元组。
    """

    hypothesis_id: str
    identity: str
    reason: str
    final_grade: str | None = None
    retained_identities: tuple[str, ...] = ()
    recorded_at: str | None = None
    code_version: str = ARCHIVE_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.hypothesis_id, "RetirementEntry.hypothesis_id")
        _require_text(self.identity, "RetirementEntry.identity")
        _require_text(self.reason, "RetirementEntry.reason")
        if self.final_grade is not None:
            _require_grade(self.final_grade, "RetirementEntry.final_grade")
        if not self.retained_identities:
            raise ArchiveIntegrityError(
                "退役必须保留历史身份：retained_identities 不得为空。"
                + HISTORICAL_IDENTITY_NOTE
            )
        object.__setattr__(
            self, "retained_identities", tuple(str(item) for item in self.retained_identities)
        )
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "identity": self.identity,
            "reason": self.reason,
            "final_grade": self.final_grade,
            "retained_identities": list(self.retained_identities),
            "recorded_at": self.recorded_at,
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 知识库版本（验收标准②③）
# ---------------------------------------------------------------------------


def _version_number(version: str) -> int:
    """解析知识库版本号 ``K<n>``。

    :raises ArchiveInputError: 形态非法。
    """
    text = _require_text(version, "知识库版本号")
    prefix = KNOWLEDGE_VERSION_PREFIX
    if not text.startswith(prefix):
        raise ArchiveInputError(
            f"知识库版本号必须以 {prefix!r} 开头，实际得到：{text!r}"
        )
    tail = text[len(prefix):]
    if not tail.isdigit():
        raise ArchiveInputError(
            f"知识库版本号 {text!r} 的后缀必须是十进制序号，实际得到：{tail!r}"
        )
    return int(tail)


def empty_knowledge(version: str = INITIAL_KNOWLEDGE_VERSION) -> "KnowledgeVersion":
    """构造一个空知识库（已初始化、尚无任何发现）。

    空知识库**有版本号**：新颖性只相对于指定知识库版本定义，需要一个明确的
    「尚无所知」起点，而不是 ``None`` 这样的「不知道」。

    :raises ArchiveInputError: 版本号形态非法。
    """
    return KnowledgeVersion(version=version)


def _validate_archives(archives: Sequence[RoundArchive]) -> None:
    """校验归档序列的自洽性（验收标准②③的可机检形式）。

    按时间（序列）顺序遍历，维护：

    - ``seen_evidence``：已经用过的证据身份（**仅确证级**）；
    - ``seen_data``：某假说谱系已见过的数据身份（**任何等级**）；
    - ``confirmatory_hids``：已获得确证级支持的假说集合。

    三条规则：

    1. 确证级条目不得与既有确证级条目共用证据身份；
    2. 确证级条目不得落在该假说谱系**已经见过**的数据身份上（含只在 ``E0``
       条目中出现过的数据）——否则「已见数据不得自证」会被「先记 E0 再补 E1」
       的写法绕过；
    3. ``E1`` 必须首次、``E2`` 必须有先行确证。
    """
    seen_evidence: set[str] = set()
    seen_data: dict[str, set[str]] = {}
    confirmatory_hids: set[str] = set()
    for archive in archives:
        for entry in archive.entries:
            per_hypothesis = seen_data.setdefault(entry.hypothesis_id, set())
            if entry.is_confirmatory:
                if entry.evidence_identity in seen_evidence:
                    raise ArchiveIntegrityError(
                        f"证据身份 {entry.evidence_identity!r} 已被另一条确证级条目引用："
                        "同一份证据不得支撑两条确证结论。" + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
                    )
                if entry.data_identity in per_hypothesis:
                    raise ArchiveIntegrityError(
                        f"假说 {entry.hypothesis_id!r} 的 {entry.grade} 条目落在已见数据"
                        f"（数据身份 {entry.data_identity!r}）上："
                        "已见数据不得作为新的确证。" + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
                    )
                if entry.grade == GRADE_E1 and entry.hypothesis_id in confirmatory_hids:
                    raise ArchiveIntegrityError(
                        f"假说 {entry.hypothesis_id!r} 已有确证级记录，"
                        "不应再出现 E1（E1 表示首次独立确证支持）："
                        "复现应记 E2。"
                    )
                if entry.grade == GRADE_E2 and entry.hypothesis_id not in confirmatory_hids:
                    raise ArchiveIntegrityError(
                        f"假说 {entry.hypothesis_id!r} 尚无先行确证，"
                        "不得直接记 E2（E2 表示在新环境或新样本上复现）。"
                    )
                seen_evidence.add(entry.evidence_identity)
                confirmatory_hids.add(entry.hypothesis_id)
            per_hypothesis.add(entry.data_identity)


@dataclass(frozen=True)
class KnowledgeVersion:
    """一个知识库版本 ``K^(v)``，同时承载发现档案 A 的全部历史。

    **只追加**：构造时接收的是「历史全部归档 + 本次新增」，任何既有条目都不会
    被删除或改写；:attr:`previous` 只是为回溯版本链保留的引用，
    不参与相等性比较，也不进入序列化（避免无限嵌套）。

    :param version: 版本号 ``K<n>``。
    :param archives: 全部归档轮次（按时间顺序）。
    :param retired: 全部退役条目（按时间顺序）。
    :param previous: 上一版本（``None`` 表示这是初始版本）。
    :param code_version: 口径版本。
    :param notes: 口径说明元组。
    """

    version: str
    archives: tuple[RoundArchive, ...] = ()
    retired: tuple[RetirementEntry, ...] = ()
    previous: "KnowledgeVersion | None" = field(default=None, compare=False, repr=False)
    code_version: str = ARCHIVE_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _version_number(self.version)
        archives = tuple(self.archives)
        retired = tuple(self.retired)
        for index, archive in enumerate(archives):
            if not isinstance(archive, RoundArchive):
                raise ArchiveInputError(
                    f"archives 第 {index} 项不是 RoundArchive（{type(archive).__name__}）。"
                )
        _validate_archives(archives)
        known_identities = {entry.identity for entry in _entries_of(archives)}
        for index, item in enumerate(retired):
            if not isinstance(item, RetirementEntry):
                raise ArchiveInputError(
                    f"retired 第 {index} 项不是 RetirementEntry（{type(item).__name__}）。"
                )
            if item.identity not in known_identities:
                raise ArchiveIntegrityError(
                    f"退役条目指向 {item.identity!r}，但档案中没有该版本的任何条目："
                    "不得凭空退役。" + HISTORICAL_IDENTITY_NOTE
                )
        if self.previous is not None:
            if not isinstance(self.previous, KnowledgeVersion):
                raise ArchiveInputError(
                    "previous 必须是 KnowledgeVersion 或 None，"
                    f"实际得到：{type(self.previous).__name__}"
                )
            if _version_number(self.previous.version) >= _version_number(self.version):
                raise ArchiveIntegrityError(
                    f"版本号必须严格递增：previous={self.previous.version!r}，"
                    f"current={self.version!r}。"
                )
        object.__setattr__(self, "archives", archives)
        object.__setattr__(self, "retired", retired)
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    # -- 只读视图 ---------------------------------------------------------

    @property
    def entries(self) -> tuple[EvidenceEntry, ...]:
        """全部证据条目（按归档时间顺序）。"""
        return _entries_of(self.archives)

    @property
    def confirmatory_entries(self) -> tuple[EvidenceEntry, ...]:
        """全部确证级条目。"""
        return tuple(entry for entry in self.entries if entry.is_confirmatory)

    def history(self) -> tuple["KnowledgeVersion", ...]:
        """版本链（由最旧到当前，含自身）。"""
        chain: list[KnowledgeVersion] = []
        cursor: KnowledgeVersion | None = self
        while cursor is not None:
            chain.append(cursor)
            cursor = cursor.previous
        return tuple(reversed(chain))

    def entries_for(self, hypothesis_id: str) -> tuple[EvidenceEntry, ...]:
        """某假说的全部归档条目（谱系级，跨版本）。"""
        return tuple(
            entry for entry in self.entries if entry.hypothesis_id == hypothesis_id
        )

    def latest_entry(self, hypothesis_id: str) -> EvidenceEntry | None:
        """某假说最近一次归档条目；``None`` 表示尚未归档过。"""
        found = self.entries_for(hypothesis_id)
        return found[-1] if found else None

    def grade_of(self, hypothesis_id: str) -> str | None:
        """某假说谱系的**最高**等级；``None`` 表示尚无任何归档记录。

        注意：返回值是「历史曾经达到的最高等级」，不随后续退役而清零——
        旧确证记录保留历史身份。
        """
        return highest_grade([entry.grade for entry in self.entries_for(hypothesis_id)])

    def active_grade_of(self, hypothesis_id: str) -> str | None:
        """某假说当前**活跃**的最高等级；已退役则返回 ``None``。

        与 :meth:`grade_of` 的区别是「最终结论」与「历史事实」的区别：
        退役不抹掉历史等级，但该假说不再作为活跃确证参与后续知识库。
        """
        if self.is_retired(hypothesis_id):
            return None
        return self.grade_of(hypothesis_id)

    def seen_data_identities(self, hypothesis_id: str) -> tuple[str, ...]:
        """该假说谱系**已经见过**的全部数据身份（升序去重，含 E0 条目）。

        这是验收标准③的判定基础：已见数据可以启发修订，但不得再作为确证。
        """
        found = {
            entry.data_identity for entry in self.entries_for(hypothesis_id)
        }
        return tuple(sorted(found))

    def seen_evidence_identities(self, hypothesis_id: str) -> tuple[str, ...]:
        """该假说谱系用过的全部证据身份（升序去重）。"""
        found = {
            entry.evidence_identity for entry in self.entries_for(hypothesis_id)
        }
        return tuple(sorted(found))

    def hypothesis_ids(self) -> tuple[str, ...]:
        """档案中出现过的全部假说标识（升序）。"""
        return tuple(sorted({entry.hypothesis_id for entry in self.entries}))

    def is_retired(self, hypothesis_id: str) -> bool:
        """该假说是否已退役。"""
        return any(item.hypothesis_id == hypothesis_id for item in self.retired)

    def retirement_of(self, hypothesis_id: str) -> RetirementEntry | None:
        """该假说的退役条目；``None`` 表示仍在活跃集合中。"""
        for item in self.retired:
            if item.hypothesis_id == hypothesis_id:
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "previous_version": self.previous.version if self.previous is not None else None,
            "archives": [archive.to_dict() for archive in self.archives],
            "retired": [item.to_dict() for item in self.retired],
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"KnowledgeVersion(version={self.version!r}, "
            f"rounds={len(self.archives)}, entries={len(self.entries)}, "
            f"retired={len(self.retired)})"
        )


def _entries_of(archives: Sequence[RoundArchive]) -> tuple[EvidenceEntry, ...]:
    """把归档序列摊平成证据条目序列。"""
    found: list[EvidenceEntry] = []
    for archive in archives:
        found.extend(archive.entries)
    return tuple(found)


# ---------------------------------------------------------------------------
# 入口一：轮次归档（验收标准①③）
# ---------------------------------------------------------------------------


def _round_facts(
    round_result: Any, round_index: int | None, fallback_binding_id: Any = None
) -> dict:
    """从 P13 的结果对象中提取归档所需的机械事实（鸭子类型，不改写）。

    支持两种来源：

    - 整轮结果（含 ``binding_id`` / ``round_index`` / ``aggregate_status`` /
      ``outcomes``）——归档整轮；
    - 单假说结论（含 ``hypothesis_id`` / ``status``）——归档该假说一条。
      单假说结论**通常不携带 ``binding_id``**（P13 的 :class:`HypothesisOutcome`
      只有假说标识与结论），此时回落到调用方给出的绑定描述
      ``fallback_binding_id``——绑定描述按契约「含 ``binding_id``」。

    本模块**不重新分类**：结论与计数原样采信。
    """
    if round_result is None:
        raise ArchiveInputError(
            "round_result 不能为 None：归档必须建立在 P13 已发表的结果对象之上。"
        )
    binding_id = getattr(round_result, "binding_id", None)
    aggregate = getattr(round_result, "aggregate_status", None)
    outcomes = getattr(round_result, "outcomes", None)

    if outcomes is not None and binding_id is not None:
        _require_sequence(outcomes, "round_result.outcomes")
        if len(outcomes) == 0:
            raise ArchiveInputError(
                "整轮结果不含任何假说结论：没有可归档的对象。"
            )
        index = getattr(round_result, "round_index", None)
        if index is None:
            index = round_index
        if index is None:
            raise ArchiveInputError(
                "未能确定研究轮次 round_index：整轮结果未提供，且调用方也未显式给出。"
                "轮次是跨轮误差预算与去重的关键，不得默认填充。"
            )
        items: list[dict] = []
        for position, outcome in enumerate(outcomes):
            hypothesis_id = getattr(outcome, "hypothesis_id", None)
            status = getattr(outcome, "status", None)
            if hypothesis_id is None or status is None:
                raise ArchiveInputError(
                    f"第 {position} 个假说结论缺少 hypothesis_id 或 status："
                    "归档依赖逐假说的结论与标识。"
                )
            items.append(
                {
                    "hypothesis_id": _require_text(hypothesis_id, "outcome.hypothesis_id"),
                    "status": _require_status(status, "outcome.status"),
                }
            )
        return {
            "binding_id": _require_text(binding_id, "round_result.binding_id"),
            "round_index": _require_int(index, "round_result.round_index"),
            "aggregate_status": _require_status(aggregate, "round_result.aggregate_status"),
            "alpha": getattr(round_result, "alpha", None),
            "outcomes": tuple(items),
        }

    hypothesis_id = getattr(round_result, "hypothesis_id", None)
    status = getattr(round_result, "status", None)
    if hypothesis_id is None or status is None:
        raise ArchiveInputError(
            "round_result 既不是整轮结果（缺 binding_id / outcomes），"
            "也不是单假说结论（缺 hypothesis_id / status）：无法归档。"
        )
    if round_index is None:
        index = getattr(round_result, "round_index", None)
    else:
        index = round_index
        carried = getattr(round_result, "round_index", None)
        if carried is not None and carried != round_index:
            raise ArchiveInputError(
                f"显式给出的 round_index={round_index!r} 与结果对象自带的 "
                f"{carried!r} 不一致：轮次是跨轮误差预算与去重的关键，"
                "不得含糊。"
            )
    if index is None:
        raise ArchiveInputError(
            "归档单假说结论时必须显式给出 round_index：该结果对象不携带轮次。"
        )
    resolved_binding = getattr(round_result, "binding_id", None)
    if resolved_binding is None:
        resolved_binding = binding_id
    if resolved_binding is None:
        resolved_binding = fallback_binding_id
    return {
        "binding_id": _require_text(resolved_binding, "binding_id"),
        "round_index": _require_int(index, "round_index"),
        "aggregate_status": _require_status(status, "round_result.status"),
        "alpha": getattr(round_result, "alpha", None),
        "outcomes": (
            {
                "hypothesis_id": _require_text(hypothesis_id, "round_result.hypothesis_id"),
                "status": _require_status(status, "round_result.status"),
            },
        ),
    }


def _custodian_capabilities(module_custodian: Any) -> tuple[Any, Any]:
    """探测注入的 custodian 模块是否具备归档与历史视图读取能力。

    本模块**只**使用这两条接口。

    :raises ArchiveInputError: 缺少 ``archive_confirmation`` 或 ``read_dataset``。
    """
    archive = getattr(module_custodian, "archive_confirmation", None)
    read = getattr(module_custodian, "read_dataset", None)
    missing = [
        name
        for name, func in (("archive_confirmation", archive), ("read_dataset", read))
        if not callable(func)
    ]
    if missing:
        raise ArchiveInputError(
            f"注入的 custodian 模块缺少接口 {missing}"
            f"（实际类型 {type(module_custodian).__name__}）："
            "归档必须经 M1 的公开接口完成，不得绕过令牌直接访问数据库。"
        )
    return archive, read


def _require_hypothesis_versions(
    value: Any, hypothesis_ids: Sequence[str]
) -> Mapping[str, str]:
    """校验逐假说的版本号声明：键集必须与轮次中的假说**逐一对应**。

    缺失、多余或空白一律报错——「新结论必须绑定新版本」，不得默认填充。

    :raises ArchiveInputError: 键集不对应或版本号非法。
    """
    mapping = _require_mapping(value, "hypothesis_versions")
    _reject_token_keys(mapping, "hypothesis_versions")
    declared = {str(key) for key in mapping}
    expected = set(hypothesis_ids)
    missing = sorted(expected - declared)
    extra = sorted(declared - expected)
    if missing or extra:
        raise ArchiveInputError(
            f"hypothesis_versions 必须与轮次中的假说逐一对应：缺少 {missing}，"
            f"多出 {extra}。新结论必须绑定明确的假说版本，缺失即报错、不做默认填充。"
            + VERSION_BINDING_NOTE
        )
    resolved: dict[str, str] = {}
    for hypothesis_id in hypothesis_ids:
        resolved[hypothesis_id] = _require_text(
            mapping[hypothesis_id], f"hypothesis_versions[{hypothesis_id!r}]"
        )
    return MappingProxyType(resolved)


def _grade_for(
    hypothesis_id: str, status: str, profile: DataProfile, knowledge: KnowledgeVersion
) -> tuple[str, str, str | None, str, bool]:
    """机械判定某假说本轮的等级、凭据、此前等级、复现依据与自证标记。

    规则见模块文档「验收标准①」。**穷尽且互斥**，不依赖任何统计量。
    """
    prior_grade = knowledge.grade_of(hypothesis_id)
    prior_entries = knowledge.entries_for(hypothesis_id)

    if status != STATUS_SUPPORTED:
        return GRADE_E0, BASIS_NOT_SUPPORTED, prior_grade, REPLICATION_NONE, False

    seen = knowledge.seen_data_identities(hypothesis_id)
    if profile.identity in seen:
        return GRADE_E0, BASIS_REUSED_SEEN_DATA, prior_grade, REPLICATION_NONE, True

    prior_confirmatory = [entry for entry in prior_entries if entry.is_confirmatory]
    if not prior_confirmatory:
        return GRADE_E1, BASIS_FIRST_CONFIRMATION, prior_grade, REPLICATION_NONE, False

    prior_environments: set[str] = set()
    for entry in prior_confirmatory:
        prior_environments.update(entry.environments)
    if any(
        environment not in prior_environments for environment in profile.environments
    ):
        return (
            GRADE_E2,
            BASIS_NEW_ENVIRONMENT,
            prior_grade,
            REPLICATION_NEW_ENVIRONMENT,
            False,
        )
    return GRADE_E2, BASIS_NEW_SAMPLE, prior_grade, REPLICATION_NEW_SAMPLE, False


def archive_round(
    module_custodian: Any,
    binding: Any,
    round_result: Any,
    *,
    hypothesis_versions: Any = None,
    knowledge: KnowledgeVersion | None = None,
    round_index: int | None = None,
    identification: str | None = None,
    causal_claim: bool = False,
    receipt: Any = None,
    recorded_at: str | None = None,
    notes: str | None = None,
    code_version: str = ARCHIVE_VERSION,
    **forbidden: Any,
) -> RoundArchive:
    """把一轮确证结果归档进发现档案（验收标准①③）。

    执行顺序（顺序本身可核对）：

    1. **拒绝受控参数**：不接受任何等级指定或数据入口
       （:func:`_reject_forbidden_kwargs`）；
    2. **校验输入**：绑定描述、轮次事实、逐假说版本声明；
       ``binding`` / ``round_result`` 都要过令牌键名与封存键名扫描；
    3. **归档**：调用 ``archive_confirmation(binding_id)``，
       原绑定转入历史探索视图，返回 ``historical_ref``；
    4. **取数据身份**：**只**通过 ``read_dataset(historical_ref)`` 读取这个
       已解锁的历史探索视图，计算内容级数据身份与环境集合，随后丢弃记录正文；
    5. **机械判定等级**：逐假说按 :func:`_grade_for` 判定 E0/E1/E2 与凭据；
    6. **出闸扫描**：对归档对象的序列化形态做 :func:`check_no_sealed_leak`
       深度扫描，确保没有任何记录正文混入。

    :param module_custodian: 以 custodian 角色构造的 M1 模块（鸭子类型）。
    :param binding: 含 ``binding_id`` 的绑定描述。
    :param round_result: P13 的 :class:`RoundClassification` 或 :class:`HypothesisOutcome`。
    :param hypothesis_versions: 逐假说的版本号映射，键必须与轮次中的假说一致。
    :param knowledge: 当前知识库版本；``None`` 表示从空知识库开始判定。
        它决定「是否已见」「是否首次确证」与「此前等级」。
    :param round_index: 单假说结论缺轮次时显式给出。
    :param identification: 因果识别声明；``causal_claim=True`` 时必填。
    :param causal_claim: 是否显式提出因果主张。
    :param receipt: P13 的登记发布凭证（可选）；仅取其摘要用于追溯。
    :param recorded_at: 归档时间（调用方提供；不自动生成，保证可复现）。
    :param notes: 自定义说明文本（可选）；核心口径说明始终追加，不可删除。
    :param code_version: 归档口径版本。
    :param forbidden: 受控参数兜底入口。本层**不**接受任何等级指定或
        数据读取类参数；一旦调用方传入 ``grade`` / ``promote_to`` /
        ``raw_records`` 等名字，会在此处被 :func:`_reject_forbidden_kwargs`
        以 :class:`ArchivePolicyError` / :class:`ArchiveInputError` 明确拒绝，
        而**不是**退化成 Python 的 ``TypeError``——前者是可归档的口径违规，
        后者只是签名不匹配，二者不能混同。该参数本身不可被正常使用。
    :return: :class:`RoundArchive`。
    :raises ArchiveInputError: 输入形态非法。
    :raises ArchivePolicyError: 触犯归档口径（指定等级、因果无识别条件等）。
    :raises ArchiveIntegrityError: 归档对象含封存字段。
    """
    # -- 受控参数闸门：先于一切业务校验 --------------------------------
    _reject_forbidden_kwargs(
        {
            "module_custodian": module_custodian,
            "binding": binding,
            "round_result": round_result,
            "hypothesis_versions": hypothesis_versions,
            "knowledge": knowledge,
            "round_index": round_index,
            "identification": identification,
            "causal_claim": causal_claim,
            "receipt": receipt,
            "recorded_at": recorded_at,
            "notes": notes,
            "code_version": code_version,
        },
        "archive_round",
    )
    _reject_forbidden_kwargs(forbidden, "archive_round")
    if forbidden:
        raise ArchiveInputError(
            f"archive_round 不接受未知参数 {sorted(forbidden)}："
            "请对照接口约定；本层不做静默忽略，以免调用方以为参数已生效。"
        )

    # -- 因果主张与识别声明必须成对（验收标准①） ---------------------------
    if not isinstance(causal_claim, bool):
        raise ArchiveInputError(
            f"causal_claim 必须是布尔值，实际得到：{causal_claim!r}"
        )
    if causal_claim and (identification is None or not str(identification).strip()):
        raise ArchivePolicyError(
            "声明了因果主张（causal_claim=True）却未给出识别条件（identification）。"
            + CAUSAL_REQUIRES_IDENTIFICATION_NOTE
        )
    if identification is not None and not causal_claim:
        raise ArchivePolicyError(
            "提供了识别声明（identification）却未声明因果主张（causal_claim=False）："
            "请明确二者，避免把识别条件误读为已成立的因果结论。"
            + CAUSAL_SCOPE_NOTE
        )
    causal_identification = (
        CAUSAL_IDENTIFICATION_DECLARED if causal_claim else CAUSAL_NOT_ADDRESSED
    )

    # -- 绑定与轮次事实 ----------------------------------------------------
    body = _require_mapping(binding, "binding")
    _reject_token_keys(body, "binding")
    check_no_sealed_leak(body, "binding")
    facts = _round_facts(round_result, round_index, body.get("binding_id"))
    if body.get("binding_id") not in (None, facts["binding_id"]):
        raise ArchiveInputError(
            f"binding.binding_id={body.get('binding_id')!r} 与结果对象的 "
            f"binding_id={facts['binding_id']!r} 不一致：归档必须落在同一绑定上。"
        )
    binding_id = facts["binding_id"]

    hypothesis_ids = [item["hypothesis_id"] for item in facts["outcomes"]]
    versions = _require_hypothesis_versions(hypothesis_versions, hypothesis_ids)

    base = _require_knowledge(knowledge)

    # -- 归档：转入历史探索视图（验收标准③的「回探索」） -------------------
    archive_fn, read_fn = _custodian_capabilities(module_custodian)
    archived = archive_fn(binding_id)
    archived = _require_mapping(archived, "archive_confirmation 的返回值")
    historical_ref = _require_text(
        archived.get("historical_ref"), "archive_confirmation.historical_ref"
    )

    rows = read_fn(historical_ref)
    profile = data_profile(rows)

    # -- 逐假说机械判定 ----------------------------------------------------
    entries: list[EvidenceEntry] = []
    for item in facts["outcomes"]:
        hypothesis_id = item["hypothesis_id"]
        status = item["status"]
        grade, basis, prior_grade, replication, self_attempt = _grade_for(
            hypothesis_id, status, profile, base
        )
        entry_notes = [
            GRADE_NOT_CAUSAL_NOTE,
            BASIS_LABELS[basis],
            E0_SCOPE_NOTE,
            NO_RECLASSIFICATION_NOTE,
            HISTORICAL_VIEW_IS_UNLOCKED_NOTE,
        ]
        if self_attempt:
            entry_notes.append(REUSE_FORBIDS_SELF_EVIDENCE_NOTE)
        if causal_claim:
            entry_notes.append(CAUSAL_SCOPE_NOTE)
        entries.append(
            EvidenceEntry(
                hypothesis_id=hypothesis_id,
                hypothesis_version=versions[hypothesis_id],
                status=status,
                grade=grade,
                grade_basis=basis,
                prior_grade=prior_grade,
                replication_basis=replication,
                data_identity=profile.identity,
                evidence_identity=_evidence_identity(
                    profile.identity,
                    binding_id,
                    facts["round_index"],
                    status,
                    hypothesis_id,
                ),
                binding_id=binding_id,
                round_index=facts["round_index"],
                historical_ref=historical_ref,
                environments=profile.environments,
                record_count=profile.record_count,
                causal_identification=causal_identification,
                causal_claim=causal_claim,
                self_evidence_attempt=self_attempt,
                recorded_at=recorded_at,
                code_version=code_version,
                notes=tuple(entry_notes),
            )
        )

    receipt_digest: str | None = None
    if receipt is not None:
        digest_fn = getattr(receipt, "content_digest", None)
        if callable(digest_fn):
            receipt_digest = _require_text(digest_fn(), "receipt.content_digest()")

    round_notes = [
        ARCHIVE_SCOPE_NOTE,
        VERSION_BINDING_NOTE,
        HISTORICAL_IDENTITY_NOTE,
        REUSE_FORBIDS_SELF_EVIDENCE_NOTE,
        NO_RECLASSIFICATION_NOTE,
    ]
    if notes is not None:
        round_notes.insert(0, _require_text(notes, "notes"))

    archive = RoundArchive(
        binding_id=binding_id,
        round_index=facts["round_index"],
        aggregate_status=facts["aggregate_status"],
        historical_ref=historical_ref,
        data_identity=profile.identity,
        entries=tuple(entries),
        alpha=facts["alpha"],
        receipt_digest=receipt_digest,
        recorded_at=recorded_at,
        code_version=code_version,
        notes=tuple(round_notes),
    )
    check_no_sealed_leak(archive.to_dict(), "归档对象")
    return archive


# ---------------------------------------------------------------------------
# 入口二：知识库版本更新（验收标准②③）
# ---------------------------------------------------------------------------


def _require_knowledge(value: Any) -> KnowledgeVersion:
    """校验知识库参数；``None`` 视为空知识库。

    :raises ArchiveInputError: 不是 :class:`KnowledgeVersion`。
    """
    if value is None:
        return empty_knowledge()
    if not isinstance(value, KnowledgeVersion):
        raise ArchiveInputError(
            f"knowledge 必须是 KnowledgeVersion 或 None，"
            f"实际得到：{type(value).__name__}"
        )
    return value


def _as_archive_sequence(value: Any) -> tuple[RoundArchive, ...]:
    """把单个 :class:`RoundArchive` 或归档序列统一成元组。"""
    if value is None:
        return ()
    if isinstance(value, RoundArchive):
        return (value,)
    items = _require_sequence(value, "round_archives")
    resolved: list[RoundArchive] = []
    for index, item in enumerate(items):
        if not isinstance(item, RoundArchive):
            raise ArchiveInputError(
                f"round_archives 第 {index} 项不是 RoundArchive"
                f"（{type(item).__name__}）。"
            )
        resolved.append(item)
    return tuple(resolved)


def _as_retirement_sequence(value: Any) -> tuple[RetirementEntry, ...]:
    """把单个 :class:`RetirementEntry` 或序列统一成元组。"""
    if value is None:
        return ()
    if isinstance(value, RetirementEntry):
        return (value,)
    items = _require_sequence(value, "retired")
    resolved: list[RetirementEntry] = []
    for index, item in enumerate(items):
        if not isinstance(item, RetirementEntry):
            raise ArchiveInputError(
                f"retired 第 {index} 项不是 RetirementEntry（{type(item).__name__}）。"
            )
        resolved.append(item)
    return tuple(resolved)


def update_knowledge_version(
    knowledge: KnowledgeVersion | None,
    round_archives: Any = (),
    *,
    retired: Any = (),
    version: str | None = None,
    recorded_at: str | None = None,
    notes: str | None = None,
    code_version: str = ARCHIVE_VERSION,
) -> KnowledgeVersion:
    """在旧知识库之上**追加**归档与退役条目，生成新版知识库（验收标准②③）。

    行为要点：

    - **只追加**：新版本携带「旧版本全部归档 + 本次新增」，旧条目逐字节不变；
      :attr:`KnowledgeVersion.previous` 指向旧版本，形成可回溯的版本链。
    - **版本严格递增**：显式给出 ``version`` 时必须大于旧版本；省略则自动 +1。
    - **新结论绑定新证据**：新增的确证级条目会**再次**对整条版本链校验
      （证据身份唯一、数据身份未见过、``E1`` 首次、``E2`` 有先行确证）。
      这一步不可省略——若调用方拿着**过期**的知识库去归档，再合并进来，
      仅靠 :func:`archive_round` 当时的判定是挡不住的。
    - **退役保留历史**：退役只追加 :class:`RetirementEntry`，既有条目不动。

    :param knowledge: 旧知识库；``None`` 表示从空知识库开始。
    :param round_archives: 本次追加的归档轮次（单个或序列）。
    :param retired: 本次追加的退役条目（单个或序列）。
    :param version: 显式指定新版本号；省略则自动递增。
    :param recorded_at: 版本生成时间（调用方提供）。
    :param notes: 自定义说明文本（可选）。
    :param code_version: 口径版本。
    :return: 新的 :class:`KnowledgeVersion`。
    :raises ArchiveInputError: 输入形态非法或版本号非法。
    :raises ArchiveIntegrityError: 版本链或条目自相矛盾。
    :raises ArchivePolicyError: 复用已见证据。
    """
    base = _require_knowledge(knowledge)
    archives = _as_archive_sequence(round_archives)
    retirements = _as_retirement_sequence(retired)

    if version is None:
        new_version = KNOWLEDGE_VERSION_PREFIX + str(_version_number(base.version) + 1)
    else:
        new_version = _require_text(version, "version")
        if _version_number(new_version) <= _version_number(base.version):
            raise ArchiveInputError(
                f"新版本号必须严格大于旧版本：旧={base.version!r}，新={new_version!r}。"
                "知识库版本只增不改。"
            )

    if not archives and not retirements:
        raise ArchiveInputError(
            "本次没有任何归档与退役条目：无内容的版本更新只会制造空版本。"
            "若确实只是重新声明状态，请直接沿用旧版本对象。"
        )

    # -- 显式再校验：防止「拿过期知识库归档后再合并」绕过复用闸门 ----------
    prior_entries = base.entries
    prior_evidence = {
        entry.evidence_identity for entry in prior_entries if entry.is_confirmatory
    }
    prior_confirmatory_hids = {
        entry.hypothesis_id for entry in prior_entries if entry.is_confirmatory
    }
    prior_data: dict[str, set[str]] = {}
    for entry in prior_entries:
        prior_data.setdefault(entry.hypothesis_id, set()).add(entry.data_identity)

    for archive in archives:
        for entry in archive.entries:
            per_hypothesis = prior_data.setdefault(entry.hypothesis_id, set())
            if not entry.is_confirmatory:
                per_hypothesis.add(entry.data_identity)
                continue
            if entry.evidence_identity in prior_evidence:
                raise ArchivePolicyError(
                    f"证据身份 {entry.evidence_identity!r} 已被知识库中另一条确证级条目引用："
                    "同一份证据不得支撑两条确证结论。"
                    + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
                )
            if entry.data_identity in per_hypothesis:
                raise ArchivePolicyError(
                    f"假说 {entry.hypothesis_id!r} 的 {entry.grade} 条目落在已见数据上："
                    "已见确证数据不得被修订版本复用为确证。"
                    + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
                )
            if entry.grade == GRADE_E1 and entry.hypothesis_id in prior_confirmatory_hids:
                raise ArchivePolicyError(
                    f"假说 {entry.hypothesis_id!r} 在知识库中已有确证级记录，"
                    "不得再追加 E1（应记 E2）。"
                )
            if entry.grade == GRADE_E2 and entry.hypothesis_id not in prior_confirmatory_hids:
                raise ArchivePolicyError(
                    f"假说 {entry.hypothesis_id!r} 尚无先行确证，不得追加 E2。"
                )
            prior_evidence.add(entry.evidence_identity)
            prior_confirmatory_hids.add(entry.hypothesis_id)
            per_hypothesis.add(entry.data_identity)

    version_notes = [
        HISTORICAL_IDENTITY_NOTE,
        ARCHIVE_SCOPE_NOTE,
        FRESH_EVIDENCE_REQUIRED_NOTE,
    ]
    if notes is not None:
        version_notes.insert(0, _require_text(notes, "notes"))

    updated = KnowledgeVersion(
        version=new_version,
        archives=base.archives + archives,
        retired=base.retired + retirements,
        previous=base,
        code_version=code_version,
        notes=tuple(version_notes),
    )
    check_no_sealed_leak(updated.to_dict(), "知识库版本")
    return updated


# ---------------------------------------------------------------------------
# 入口三：假说保留 / 修订 / 退役（验收标准②③）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevisionDecision:
    """假说更新决策（保留 / 修订 / 退役）及其对后续取证的要求。

    :param hypothesis_id: 假说标识。
    :param action: 取值 :data:`ACTIONS`。
    :param reason: 决策理由。
    :param previous_identity: 决策前的版本标识。
    :param resulting_identity: 决策后的版本标识（退役时为退役态标识）。
    :param resulting_digest: 决策后假说对象的内容摘要（可核对未被替换）。
    :param final_grade: 决策所依据的谱系最高等级；``None`` 表示无确证级记录。
    :param latest_status: 最近一次归档的结论。
    :param retirement: 退役条目（``action == "retire"`` 时非空）。
    :param forbidden_evidence_identities: 该假说谱系已用过的全部数据/证据身份，
        后续确证**必须**避开它们（验收标准③的下游可机检形式）。
    :param required_fresh_evidence: 恒为 ``True``：任何后续确证都必须使用新证据。
    :param recorded_at: 决策时间（调用方提供）。
    :param code_version: 口径版本。
    :param notes: 口径说明元组。
    """

    hypothesis_id: str
    action: str
    reason: str
    previous_identity: str
    resulting_identity: str
    resulting_digest: str
    final_grade: str | None
    latest_status: str
    retirement: RetirementEntry | None = None
    forbidden_evidence_identities: tuple[str, ...] = ()
    required_fresh_evidence: bool = True
    recorded_at: str | None = None
    code_version: str = ARCHIVE_VERSION
    notes: tuple[str, ...] = ()
    resulting_hypothesis: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _require_text(self.hypothesis_id, "RevisionDecision.hypothesis_id")
        if self.action not in ACTIONS:
            raise ArchiveInputError(
                f"action 取值 {self.action!r} 不在（{list(ACTIONS)}）内。"
            )
        _require_text(self.reason, "RevisionDecision.reason")
        _require_status(self.latest_status, "RevisionDecision.latest_status")
        if self.final_grade is not None:
            _require_grade(self.final_grade, "RevisionDecision.final_grade")
        if self.action == ACTION_RETIRE and not isinstance(self.retirement, RetirementEntry):
            raise ArchiveIntegrityError(
                "action 为 retire 时必须给出退役条目："
                "退役要有理由，也要保留历史身份。"
            )
        if self.action != ACTION_RETIRE and self.retirement is not None:
            raise ArchiveIntegrityError(
                f"action={self.action!r} 不应携带退役条目。"
            )
        if not self.required_fresh_evidence:
            raise ArchivePolicyError(
                "required_fresh_evidence 不得为 False："
                "任何后续确证都必须使用未见过的新证据。"
                + FRESH_EVIDENCE_REQUIRED_NOTE
            )
        object.__setattr__(
            self,
            "forbidden_evidence_identities",
            tuple(sorted(str(item) for item in self.forbidden_evidence_identities)),
        )
        object.__setattr__(self, "notes", tuple(str(item) for item in self.notes))

    def assert_evidence_is_fresh(self, identity: str) -> None:
        """核对给定身份未被该假说谱系用过（验收标准③）。

        :raises ArchivePolicyError: 身份已在禁用集合内。
        """
        _require_text(identity, "identity")
        if identity in self.forbidden_evidence_identities:
            raise ArchivePolicyError(
                f"身份 {identity!r} 已被假说 {self.hypothesis_id!r} 的谱系用过，"
                "不得作为后续确证证据。" + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
            )

    @property
    def changed(self) -> bool:
        """决策是否改变了假说内容（退役与修订都算改变）。"""
        return (
            self.action in (ACTION_REVISE, ACTION_RETIRE)
            or self.resulting_identity != self.previous_identity
        )

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "action": self.action,
            "reason": self.reason,
            "previous_identity": self.previous_identity,
            "resulting_identity": self.resulting_identity,
            "resulting_digest": self.resulting_digest,
            "final_grade": self.final_grade,
            "latest_status": self.latest_status,
            "retirement": self.retirement.to_dict() if self.retirement else None,
            "forbidden_evidence_identities": list(self.forbidden_evidence_identities),
            "required_fresh_evidence": self.required_fresh_evidence,
            "recorded_at": self.recorded_at,
            "code_version": self.code_version,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        return _canonical(self.to_dict())

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _store_capabilities(store: Any) -> tuple[Any, Any]:
    """探测假说版本仓库是否具备 ``latest`` 与 ``revise``。

    :raises ArchiveInputError: 缺少接口。
    """
    latest = getattr(store, "latest", None)
    revise = getattr(store, "revise", None)
    missing = [
        name
        for name, func in (("latest", latest), ("revise", revise))
        if not callable(func)
    ]
    if missing:
        raise ArchiveInputError(
            f"注入的假说仓库缺少接口 {missing}"
            f"（实际类型 {type(store).__name__}）："
            "本模块只调用 M4 仓库的公开接口，不重新实现版本不可覆盖语义。"
        )
    return latest, revise


def _forbidden_identities(
    knowledge: KnowledgeVersion, hypothesis_id: str
) -> tuple[str, ...]:
    """该假说谱系已用过的全部数据身份与证据身份（升序去重）。"""
    found = set(knowledge.seen_data_identities(hypothesis_id))
    found.update(knowledge.seen_evidence_identities(hypothesis_id))
    return tuple(sorted(found))


def assert_evidence_is_fresh(
    knowledge: KnowledgeVersion, hypothesis_id: str, identity: str
) -> None:
    """核对某身份未被该假说谱系用作确证证据（验收标准③的独立闸门）。

    :raises ArchivePolicyError: 身份已在谱系内用过。
    """
    base = _require_knowledge(knowledge)
    _require_text(hypothesis_id, "hypothesis_id")
    _require_text(identity, "identity")
    forbidden = _forbidden_identities(base, hypothesis_id)
    if identity in forbidden:
        raise ArchivePolicyError(
            f"身份 {identity!r} 已被假说 {hypothesis_id!r} 的谱系用过："
            "已见确证数据可以回探索，但不得被同一修订版本复用为确证。"
            + REUSE_FORBIDS_SELF_EVIDENCE_NOTE
        )


def _retention_identities(store: Any, hypothesis_id: str, latest: Any) -> tuple[str, ...]:
    """退役时仍保留在档案中的版本标识。

    取仓库中该假说的全部版本标识（若仓库支持 ``versions``），
    退一步至少保留被退役的这一个版本标识——历史身份绝不为空。
    """
    versions_fn = getattr(store, "versions", None)
    if callable(versions_fn):
        try:
            versions = tuple(versions_fn(hypothesis_id))
        except Exception:  # pragma: no cover - 仓库实现差异时的保守兜底
            versions = ()
        if versions:
            identifier = getattr(latest, "id", hypothesis_id)
            return tuple(f"{identifier}@{item}" for item in versions)
    identity = getattr(latest, "identity", None)
    if isinstance(identity, str) and identity.strip():
        return (identity,)
    return (f"{hypothesis_id}@?",)


def revise_or_retire(
    store: Any,
    hypothesis_id: str,
    knowledge: KnowledgeVersion,
    *,
    changes: Mapping[str, Any] | None = None,
    version: str | None = None,
    retire: bool = False,
    reason: str | None = None,
    recorded_at: str | None = None,
    notes: str | None = None,
    code_version: str = ARCHIVE_VERSION,
) -> RevisionDecision:
    """按归档结果决定假说「保留 / 修订 / 退役」（验收标准②③）。

    决策规则（穷尽且互斥，均为机械规则）：

    1. ``retire=True`` → **退役**：产出退役条目，既有条目与历史等级原样保留。
    2. 最近一次归档结论为 ``refuted`` 且未给出 ``changes`` → **退役**
       （观察与预先声明的预测有力不相容）。
    3. 给出了非空 ``changes`` → **修订**：由当前版本派生**新版本**
       （M4 的 :meth:`HypothesisStore.revise`），并要求
       ``changes["provenance"]["knowledge_version"]`` 等于新知识库版本。
    4. 其余（``supported`` 而无改动、``inconclusive``、``failed``）
       → **保留**。**证据不足不构成退役理由，也不构成修订理由**
       ——「没有证据」不等于「证明没有」，这与 P13 的口径一致。

    无论走哪条路径，决策都会给出该假说谱系已用过的全部数据/证据身份
    （``forbidden_evidence_identities``），使下游在**下一次冻结前**即可机械拒绝
    复用旧数据自证。

    :param store: M4 的假说版本仓库（鸭子类型）。
    :param hypothesis_id: 假说标识。
    :param knowledge: 归档所在的知识库版本（至少含该假说一条条目）。
    :param changes: 修订时覆盖的字段；必须含新的 ``provenance``。
    :param version: 修订时显式指定新版本号；省略则自动递增。
    :param retire: 显式要求退役。
    :param reason: 决策理由（可选；省略时按规则自动生成，始终附加说明）。
    :param recorded_at: 决策时间（调用方提供）。
    :param notes: 自定义说明文本（可选）。
    :param code_version: 口径版本。
    :return: :class:`RevisionDecision`。
    :raises ArchiveInputError: 输入形态非法或缺少归档依据。
    :raises ArchivePolicyError: 修订未绑定新知识库版本。
    """
    base = _require_knowledge(knowledge)
    _require_text(hypothesis_id, "hypothesis_id")
    if not isinstance(retire, bool):
        raise ArchiveInputError(f"retire 必须是布尔值，实际得到：{retire!r}")

    entries = base.entries_for(hypothesis_id)
    if not entries:
        raise ArchiveInputError(
            f"知识库中没有任何关于假说 {hypothesis_id!r} 的归档条目："
            "不得凭空修订或退役。请先归档一轮确证结果。"
        )
    latest_entry = entries[-1]
    latest_status = latest_entry.status
    final_grade = base.grade_of(hypothesis_id)

    latest_fn, revise_fn = _store_capabilities(store)
    try:
        current = latest_fn(hypothesis_id)
    except Exception as exc:  # 把上游异常收敛为可预期的输入错误
        raise ArchiveInputError(
            f"假说仓库中取不到 {hypothesis_id!r} 的最新版本：{exc}"
        ) from exc
    previous_identity = getattr(current, "identity", None)
    if not isinstance(previous_identity, str) or not previous_identity.strip():
        raise ArchiveInputError(
            "仓库返回的假说对象缺少版本标识 identity：无法确定决策对象。"
        )

    forbidden = _forbidden_identities(base, hypothesis_id)
    want_revise = changes is not None

    decision_reason: str
    retirement: RetirementEntry | None = None
    resulting = current
    action: str

    if retire or (latest_status == STATUS_REFUTED and not want_revise):
        action = ACTION_RETIRE
        if retire:
            decision_reason = "调用方显式要求退役。"
        else:
            decision_reason = (
                f"最近一次归档结论为 {STATUS_REFUTED}：观察与预先声明的预测有力不相容，"
                "该版本退出活跃集合（历史条目与旧确证记录保留历史身份）。"
            )
        if reason is not None:
            decision_reason = decision_reason + _require_text(reason, "reason")
        retire_fn = getattr(current, "retire", None)
        if callable(retire_fn):
            resulting = retire_fn()
        resulting_identity = getattr(resulting, "identity", previous_identity)
        retirement = RetirementEntry(
            hypothesis_id=hypothesis_id,
            identity=previous_identity,
            reason=decision_reason,
            final_grade=final_grade,
            retained_identities=_retention_identities(store, hypothesis_id, current),
            recorded_at=recorded_at,
            code_version=code_version,
            notes=(HISTORICAL_IDENTITY_NOTE,),
        )
    elif want_revise:
        mapping = _require_mapping(changes, "changes")
        _reject_token_keys(mapping, "changes")
        check_no_sealed_leak(mapping, "changes")
        if not mapping:
            raise ArchiveInputError(
                "changes 为空映射：修订必须说明改了什么，空修订只会制造无意义的版本。"
            )
        provenance = mapping.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ArchiveInputError(
                "修订必须显式给出完整的 provenance（含 knowledge_version）："
                "新结论必须绑定新版本与新证据。" + VERSION_BINDING_NOTE
            )
        declared_knowledge = provenance.get("knowledge_version")
        if declared_knowledge != base.version:
            raise ArchivePolicyError(
                f"修订版本的 provenance.knowledge_version={declared_knowledge!r} 与"
                f"当前知识库版本 {base.version!r} 不一致："
                "新结论必须绑定新的知识库版本。" + VERSION_BINDING_NOTE
            )
        action = ACTION_REVISE
        decision_reason = (
            f"给出修订内容（最近一次归档结论为 {latest_status}）："
            "由当前版本派生新版本，旧版本原样保留。"
        )
        if reason is not None:
            decision_reason = decision_reason + _require_text(reason, "reason")
        try:
            resulting = revise_fn(previous_identity, changes=dict(mapping), version=version)
        except Exception as exc:
            raise ArchiveInputError(
                f"派生新版本失败：{exc}"
            ) from exc
        resulting_identity = getattr(resulting, "identity", None)
        if not isinstance(resulting_identity, str) or not resulting_identity.strip():
            raise ArchiveIntegrityError("派生的新版本缺少版本标识。")
        if resulting_identity == previous_identity:
            raise ArchiveIntegrityError(
                "派生的新版本与旧版本标识相同：版本不可覆盖，修订必须产生新标识。"
            )
    else:
        action = ACTION_RETAIN
        decision_reason = (
            f"最近一次归档结论为 {latest_status}"
            + (
                "（已获确证级支持）"
                if final_grade in CONFIRMATORY_GRADES
                else "（未获确证级支持）"
            )
            + "，且未给出修订内容：保留当前版本。"
            "证据不足或失败不构成退役理由，也不构成修订理由。"
        )
        if reason is not None:
            decision_reason = decision_reason + _require_text(reason, "reason")
        resulting_identity = previous_identity

    digest_fn = getattr(resulting, "content_digest", None)
    resulting_digest = (
        _require_text(digest_fn(), "resulting.content_digest()")
        if callable(digest_fn)
        else ""
    )

    decision_notes = [
        FRESH_EVIDENCE_REQUIRED_NOTE,
        REUSE_FORBIDS_SELF_EVIDENCE_NOTE,
        HISTORICAL_IDENTITY_NOTE,
        E0_SCOPE_NOTE,
    ]
    if notes is not None:
        decision_notes.insert(0, _require_text(notes, "notes"))

    decision = RevisionDecision(
        hypothesis_id=hypothesis_id,
        action=action,
        reason=decision_reason,
        previous_identity=previous_identity,
        resulting_identity=resulting_identity,
        resulting_digest=resulting_digest,
        final_grade=final_grade,
        latest_status=latest_status,
        retirement=retirement,
        forbidden_evidence_identities=forbidden,
        required_fresh_evidence=True,
        recorded_at=recorded_at,
        code_version=code_version,
        notes=tuple(decision_notes),
        resulting_hypothesis=resulting,
    )
    check_no_sealed_leak(decision.to_dict(), "假说更新决策")
    return decision


# ---------------------------------------------------------------------------
# 边界自检
# ---------------------------------------------------------------------------


def _forbidden_access_names() -> tuple[str, ...]:
    """本模块**不应出现**的受限访问入口名。

    与 :data:`NOT_PROVIDED_BY_P15`（关注「不定义什么」）不同，本集合关注
    「不碰什么」：本模块只调用 ``archive_confirmation`` 与
    ``read_dataset``（且后者只作用于归档返回的历史探索视图），
    其余数据访问、受限质量报告与令牌接口一律不得出现。
    测试用 AST 检查属性访问与导入名，**不用**源码文本匹配——docstring 与常量里
    **必须**写出这些被禁名字（那是边界声明），文本匹配会把声明本身判成违规。
    """
    return (
        "consume_confirmation",
        "bind_confirmation",
        "record_evaluation",
        "release_results",
        "inspect_confirmation",
        "mark_compromised",
        "add_confirmation",
        "quality",
        "load_snapshot",
        "open_vault",
        "verify_integrity",
        "connect",
        "sqlite3",
    )


def self_check() -> dict:
    """对**自身源码**做一次 AST 级边界自检（供测试与复核调用）。

    检查两项：

    1. 模块是否定义了 :data:`NOT_PROVIDED_BY_P15` 中的任何名字（越界实现）；
    2. 模块是否出现 :func:`_forbidden_access_names` 中的访问入口（越界读取）。

    :return: ``{"defined_forbidden", "accessed_forbidden", "ok", "checked_names"}``。
    """
    source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
    tree = _ast.parse(source_path.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))
    }
    accessed: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, _ast.Name):
            accessed.add(node.id)
        elif isinstance(node, _ast.alias):
            accessed.add(node.name.split(".")[-1])
    defined_forbidden = tuple(sorted(defined & set(NOT_PROVIDED_BY_P15)))
    accessed_forbidden = tuple(sorted(accessed & set(_forbidden_access_names())))
    return {
        "defined_forbidden": defined_forbidden,
        "accessed_forbidden": accessed_forbidden,
        "ok": not defined_forbidden and not accessed_forbidden,
        "checked_names": len(NOT_PROVIDED_BY_P15),
    }
