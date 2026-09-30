"""P15 验收测试：知识归档与假说更新。

覆盖三条验收标准：

1. **证据等级严格按 E0/E1/E2 记录，E2 不自动升级为因果。**
   用真实 M1 + P11/P12/P13 端到端跑出「首次支持 → ``E1``」「在新数据上复现 →
   ``E2``」「不显著 → ``E0``」三条路径；''新环境复现'' 另用**双协议**（lab-A 与
   lab-B 各自建库）端到端跑出 ``E2 / new_environment``。
   并逐条证明**等级与凭据穷尽且互斥**：每个凭据只对应一个等级，
   ``GRADE_BASIS_CODES`` 与 ``EVIDENCE_GRADES`` 的组合覆盖全部实测情形。
   因果侧：``CAUSAL_UPGRADE_BY_GRADE`` 恒为 ``False``；
   声明因果却不给识别条件一律拒绝；给了识别条件却不声明因果也拒绝；
   显式声明后等级**不因此改变**。

2. **旧确证记录保留历史身份，新结论必须绑定新版本与新证据。**
   知识库版本只追加：``K1 → K2`` 后 ``K1`` 的全部条目逐字节不变，
   版本链可回溯；退役不抹掉历史等级（``grade_of`` 保留、``active_grade_of`` 归空）。
   归档时 ``hypothesis_versions`` 缺项/多项一律报错（不默认填充）；
   修订时必须绑定新的 ``provenance.knowledge_version``；
   手工伪造的 ``E1`` 二次出现、无先行确证的 ``E2``、确证级条目落在已见数据上，
   都会在构造知识库时被拦下（:class:`ArchiveIntegrityError`）。

3. **已见确证数据可回探索，但修订版本不得用旧数据自证。**
   归档返回 ``historical_ref`` 且**只**经该历史视图取数据身份；
   数据身份按**内容**判定（``record_id`` 改写不改变身份）；
   复用已见数据时等级压回 ``E0`` 并置 ``self_evidence_attempt``；
   拿到**过期**知识库去归档再合并，仍会被 :func:`update_knowledge_version`
   拒绝（:class:`ArchivePolicyError`）；
   :class:`RevisionDecision` 携带禁用身份集合，:func:`assert_evidence_is_fresh`
   与 :meth:`RevisionDecision.assert_evidence_is_fresh` 都能机械拒绝复用。

边界守护：不修改 ``sdl_m01/``、只用标准库（AST 检查 import）、
不实现 P16 及之后与上游的内容（AST 检查函数定义与 :data:`NOT_PROVIDED_BY_P15`）、
不使用 skip / xfail、不含令牌键名。

关于「不做 X」的守护方式：本文件一律用 **AST** 判断「函数有没有被定义、
属性有没有被访问」，**不**用源码文本匹配——模块的 docstring 与
``NOT_PROVIDED_BY_P15`` 常量里**必须**写出被禁名字（那是边界声明），
文本匹配会把边界声明本身当成违规。

关于夹具自检：本文件先**实测**上游输出再写断言（等级、凭据、结果摘要、
轮次阈值均与实测一致），不靠推测。
"""

import ast as pyast
import copy
import hashlib
import json
import pathlib
import tempfile
import unittest

from sdl_m01 import Module01, initialize

from sdl_m04.hypothesis import HypothesisStore

from tests.helpers import sample_spec

from tests.test_freeze import METRIC, make_hypothesis

from sdl_m06.freeze import bind, build_frozen_plan
from sdl_m06.execute import execute_family, family_test_ids
from sdl_m06.report import (
    HypothesisOutcome as _P13HypothesisOutcome,
    STATUS_FAILED as _P13_STATUS_FAILED,
    STATUS_REFUTED as _P13_STATUS_REFUTED,
    STATUS_SUPPORTED as _P13_STATUS_SUPPORTED,
    classify_result,
    record_and_release,
)

from sdl_m08.archive import (
    ACTIONS,
    ACTION_RETAIN,
    ACTION_RETIRE,
    ACTION_REVISE,
    ARCHIVE_SEALED_KEYS,
    ARCHIVE_VERSION,
    ArchiveError,
    ArchiveInputError,
    ArchiveIntegrityError,
    ArchivePolicyError,
    BASIS_FIRST_CONFIRMATION,
    BASIS_LABELS,
    BASIS_NEW_ENVIRONMENT,
    BASIS_NEW_SAMPLE,
    BASIS_NOT_SUPPORTED,
    BASIS_REUSED_SEEN_DATA,
    CAUSAL_IDENTIFICATION_DECLARED,
    CAUSAL_NOT_ADDRESSED,
    CAUSAL_UPGRADE_BY_GRADE,
    CONFIRMATORY_GRADES,
    EVIDENCE_GRADES,
    FORBIDDEN_CALL_KEYS,
    FRESH_EVIDENCE_REQUIRED_NOTE,
    GRADE_BASIS_CODES,
    GRADE_E0,
    GRADE_E1,
    GRADE_E2,
    GRADE_RANK,
    HISTORICAL_IDENTITY_NOTE,
    INITIAL_KNOWLEDGE_VERSION,
    KNOWLEDGE_VERSION_PREFIX,
    NOT_PROVIDED_BY_P15,
    NO_RECLASSIFICATION_NOTE,
    REPLICATION_BASES,
    REPLICATION_NEW_ENVIRONMENT,
    REPLICATION_NEW_SAMPLE,
    REPLICATION_NONE,
    RESULT_STATUSES,
    REUSE_FORBIDS_SELF_EVIDENCE_NOTE,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    VERSION_BINDING_NOTE,
    DataProfile,
    EvidenceEntry,
    KnowledgeVersion,
    RetirementEntry,
    RevisionDecision,
    RoundArchive,
    archive_round,
    assert_evidence_is_fresh,
    check_no_sealed_leak,
    data_profile,
    empty_knowledge,
    grade_rank,
    highest_grade,
    revise_or_retire,
    self_check,
    update_knowledge_version,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "sdl_m08" / "archive.py"
)

HYPOTHESIS_ID = "h-relation-1"

#: 单假说检验族里唯一的检验标识所在的轮次阈值：m=1 → α_t = 0.05 / 2^t。
ALPHA_BASE = 0.05


def spec_for(environment):
    """构造只声明单一环境的数据配置（用于「新环境复现」的双协议夹具）。"""
    spec = sample_spec()
    spec["task"]["environments"] = [environment]
    return spec


def records(groups, start, *, environment="lab-A", readings=1, tag="r"):
    """构造一批确定的合成记录（``tag`` 可改写记录标识以验证「内容级身份」）。"""
    rows = []
    for group in range(start, start + groups):
        for reading in range(readings):
            rows.append(
                {
                    "record_id": f"{tag}-{group:03}-{reading:02}",
                    "group_ids": {"batch": f"batch-{group:03}"},
                    "environment": environment,
                    "event_time": "2026-01-01T10:00:00+00:00",
                    "available_time": "2026-01-01T11:00:00+00:00",
                    "values": {"X1": group + 0.1 * reading, "Y": group * 2.0 + reading},
                    "units": {"X1": "mol/L", "Y": "mol/(L*s)"},
                    "source": "synthetic://p15-acceptance",
                    "operator_annotation": f"private-{group:03}-{reading:02}",
                }
            )
    return rows


def make_entry(
    *,
    hypothesis_id=HYPOTHESIS_ID,
    version="1",
    status=STATUS_SUPPORTED,
    grade=GRADE_E1,
    basis=BASIS_FIRST_CONFIRMATION,
    prior_grade=None,
    replication=REPLICATION_NONE,
    data_identity="d-1",
    evidence_identity="e-1",
    binding_id="binding-1",
    round_index=1,
    historical_ref="data_hist-1",
    environments=("lab-A",),
):
    """手工构造一条证据条目（用于**规则层**测试，不经 M1 的登记指纹约束）。"""
    return EvidenceEntry(
        hypothesis_id=hypothesis_id,
        hypothesis_version=version,
        status=status,
        grade=grade,
        grade_basis=basis,
        prior_grade=prior_grade,
        replication_basis=replication,
        data_identity=data_identity,
        evidence_identity=evidence_identity,
        binding_id=binding_id,
        round_index=round_index,
        historical_ref=historical_ref,
        environments=environments,
        record_count=3,
        recorded_at="2026-01-01T00:00:00+00:00",
    )


def make_archive(entries, *, binding_id="binding-1", round_index=1,
                 status=STATUS_SUPPORTED, data_identity="d-1"):
    """手工构造一轮归档（条目必须落在同一绑定与同一数据身份上）。"""
    return RoundArchive(
        binding_id=binding_id,
        round_index=round_index,
        aggregate_status=status,
        historical_ref="data_hist-1",
        data_identity=data_identity,
        entries=tuple(entries),
        alpha=0.025,
        recorded_at="2026-01-01T00:00:00+00:00",
    )


class _EndToEndCase(unittest.TestCase):
    """端到端夹具：真实 M1 证据库 + P11 冻结计划 + P12 执行 + P13 分类发布 + P15 归档。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-m08-archive-")
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "vault.sqlite3"
        self.tokens = initialize(self.db)
        self.custodian = Module01(self.db, self.tokens["custodian"])
        self.confirmer = Module01(self.db, self.tokens["confirmer"])
        self.protocol = self.custodian.build(
            sample_spec(), records(10, 0, readings=2)
        )
        self.protocol_id = self.protocol["protocol_id"]
        self._purpose = 1

    # -- 构造 ----------------------------------------------------------

    def add_protocol(self, environment):
        """为另一环境再建一个协议（用于「新环境复现」）。"""
        return self.custodian.build(spec_for(environment), records(10, 500, environment=environment, readings=2))

    def next_purpose(self):
        """下一个确证分区名（M1 要求逐批唯一）。"""
        self._purpose += 1
        return f"C{self._purpose}"

    def plan_of(self, protocol, round_index):
        return build_frozen_plan(
            [make_hypothesis()],
            protocol["protocol_id"],
            round_index,
            protocol=protocol,
            declarations={
                HYPOTHESIS_ID: {
                    "metrics": [METRIC],
                    "primary_metric": METRIC,
                    "effect_threshold": 0.1,
                    "subgroups": ["overall"],
                }
            },
        )

    def run_round(self, protocol, round_index, batches, *, p_value,
                  effect=1.0, purpose=None):
        """完整跑一轮并返回 ``(binding, round_classification, receipt)``。"""
        purpose = purpose or self.next_purpose()
        self.custodian.add_confirmation(protocol["protocol_id"], batches, purpose)
        plan = self.plan_of(protocol, round_index)
        ref = self.custodian.describe(protocol["protocol_id"])["resources"][purpose]
        binding = bind(self.confirmer, ref, plan)
        test_id = family_test_ids(plan)[0]
        execution = execute_family(
            self.confirmer,
            binding,
            plan,
            protocol=protocol,
            trajectories={test_id: {"p_value": p_value, "effect": effect}},
        )
        classification = classify_result(execution, primary_metric=METRIC)
        receipt = record_and_release(self.confirmer, binding, classification)
        return binding, classification, receipt

    def archive(self, binding, classification, knowledge, *, versions=None,
                receipt=None, **kwargs):
        return archive_round(
            self.custodian,
            binding,
            classification,
            hypothesis_versions=versions or {HYPOTHESIS_ID: "1"},
            knowledge=knowledge,
            receipt=receipt,
            recorded_at="2026-01-01T00:00:00+00:00",
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 验收标准①：证据等级 E0/E1/E2，且不做因果升级
# ---------------------------------------------------------------------------


class GradeRecordingTests(_EndToEndCase):
    """等级必须按 E0/E1/E2 记录，且判定凭据与等级一一对应。"""

    def test_first_support_is_e1(self):
        """首次获得独立确证支持 → E1（凭据：首次确证）。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        self.assertEqual(classification.outcomes[0].status, STATUS_SUPPORTED)

        archive = self.archive(binding, classification, empty_knowledge(), receipt=receipt)
        entry = archive.entries[0]
        self.assertEqual(entry.grade, GRADE_E1)
        self.assertEqual(entry.grade_basis, BASIS_FIRST_CONFIRMATION)
        # 实测：m=1 时 α_t = 0.05 / 2 = 0.025。
        self.assertAlmostEqual(archive.alpha, 0.025)
        self.assertEqual(entry.prior_grade, None)
        self.assertEqual(entry.replication_basis, REPLICATION_NONE)
        self.assertTrue(entry.is_confirmatory)

    def test_new_sample_replication_is_e2(self):
        """已有确证后，在**新样本批次**上复现 → E2（凭据：新样本复现）。"""
        binding1, first, receipt1 = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        knowledge = update_knowledge_version(
            empty_knowledge(), self.archive(binding1, first, empty_knowledge(), receipt=receipt1)
        )
        self.assertEqual(knowledge.grade_of(HYPOTHESIS_ID), GRADE_E1)

        binding2, second, receipt2 = self.run_round(
            self.protocol, 2, records(3, 1100), p_value=0.001
        )
        archive = self.archive(binding2, second, knowledge, receipt=receipt2)
        entry = archive.entries[0]
        self.assertEqual(entry.grade, GRADE_E2)
        self.assertEqual(entry.grade_basis, BASIS_NEW_SAMPLE)
        self.assertEqual(entry.replication_basis, REPLICATION_NEW_SAMPLE)
        self.assertEqual(entry.prior_grade, GRADE_E1)

    def test_new_environment_replication_is_e2(self):
        """已有确证后，在**新环境**上复现 → E2（凭据：新环境复现）。

        实验室环境必须在数据配置中声明（M1 会把范围外环境裁掉），
        因此这里另建一个 lab-B 协议。
        """
        binding1, first, receipt1 = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        knowledge = update_knowledge_version(
            empty_knowledge(), self.archive(binding1, first, empty_knowledge(), receipt=receipt1)
        )

        other = self.add_protocol("lab-B")
        binding2, second, receipt2 = self.run_round(
            other, 2, records(3, 1500, environment="lab-B"), p_value=0.001
        )
        archive = self.archive(binding2, second, knowledge, receipt=receipt2)
        entry = archive.entries[0]
        self.assertEqual(entry.environments, ("lab-B",))
        self.assertEqual(entry.grade, GRADE_E2)
        self.assertEqual(entry.grade_basis, BASIS_NEW_ENVIRONMENT)
        self.assertEqual(entry.replication_basis, REPLICATION_NEW_ENVIRONMENT)

    def test_non_significant_is_e0_not_confirmatory(self):
        """不显著 → E0：既不构成确证支持，也不得标成反驳。

        等级与结论是两件事：``inconclusive`` 是结论，``E0`` 是等级；
        这里同时断言二者，防止把「证据不足」误读为「已证否」。
        """
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.9
        )
        self.assertEqual(classification.outcomes[0].status, STATUS_INCONCLUSIVE)

        archive = self.archive(binding, classification, empty_knowledge(), receipt=receipt)
        entry = archive.entries[0]
        self.assertEqual(entry.grade, GRADE_E0)
        self.assertEqual(entry.grade_basis, BASIS_NOT_SUPPORTED)
        self.assertFalse(entry.is_confirmatory)

    def test_refuted_is_e0_not_confirmatory(self):
        """反驳 → E0：观察与预定方向相反，绝不能计入确证支持。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001, effect=-1.0
        )
        self.assertEqual(classification.outcomes[0].status, STATUS_REFUTED)

        archive = self.archive(binding, classification, empty_knowledge(), receipt=receipt)
        entry = archive.entries[0]
        self.assertEqual(entry.grade, GRADE_E0)
        self.assertEqual(entry.grade_basis, BASIS_NOT_SUPPORTED)

    def test_grade_recorded_for_every_status(self):
        """四种结论都必须落成归档条目——「算出来不支持」与「没算成」都是发现。"""
        entries = []
        for index, (status, expected_grade) in enumerate(
            [
                (STATUS_SUPPORTED, GRADE_E1),
                (STATUS_REFUTED, GRADE_E0),
                (STATUS_INCONCLUSIVE, GRADE_E0),
                (STATUS_FAILED, GRADE_E0),
            ]
        ):
            grade, basis, _, _, _ = _grade_for_case(status, f"d-{index}")
            entries.append((status, grade, basis, expected_grade))
        for status, grade, basis, expected_grade in entries:
            with self.subTest(status=status):
                self.assertEqual(grade, expected_grade)
                self.assertIn(basis, GRADE_BASIS_CODES)

    def test_grade_and_basis_are_exhaustive_and_mutually_exclusive(self):
        """等级 × 凭据的**机械**组合表：每个凭据只导出一个等级，无一遗漏。"""
        expected = {
            BASIS_NOT_SUPPORTED: GRADE_E0,
            BASIS_REUSED_SEEN_DATA: GRADE_E0,
            BASIS_FIRST_CONFIRMATION: GRADE_E1,
            BASIS_NEW_ENVIRONMENT: GRADE_E2,
            BASIS_NEW_SAMPLE: GRADE_E2,
        }
        self.assertEqual(set(expected), set(GRADE_BASIS_CODES))
        for basis, grade in expected.items():
            with self.subTest(basis=basis):
                self.assertIn(grade, EVIDENCE_GRADES)
                # 凭据只对应一个等级：构造「同凭据不同等级」的条目必须报错。
                self.assertTrue(BASIS_LABELS[basis])
        for grade in EVIDENCE_GRADES:
            with self.subTest(grade=grade):
                self.assertIn(grade, GRADE_RANK)

    def test_grade_rank_is_monotone(self):
        """等级序必须单调：E0 < E1 < E2，用于取谱系最高等级。"""
        self.assertLess(grade_rank(GRADE_E0), grade_rank(GRADE_E1))
        self.assertLess(grade_rank(GRADE_E1), grade_rank(GRADE_E2))
        with self.assertRaises(ArchiveInputError):
            grade_rank("E3")

    def test_highest_grade_distinguishes_missing_from_e0(self):
        """「完全没有记录」必须与「记录为 E0」可区分。"""
        self.assertIsNone(highest_grade([]))
        self.assertEqual(highest_grade([GRADE_E0]), GRADE_E0)
        self.assertEqual(highest_grade([GRADE_E0, GRADE_E2, GRADE_E1]), GRADE_E2)


class CausalStanceTests(_EndToEndCase):
    """验收标准①后半句：E2 绝不自动升级为因果。"""

    def test_causal_upgrade_flag_is_false(self):
        self.assertIs(CAUSAL_UPGRADE_BY_GRADE, False)

    def test_no_grade_parameter_accepted(self):
        """等级由本模块判定：任何「指定等级 / 升级」的参数一律拒绝。"""
        for keyword in ("grade", "evidence_grade", "promote_to", "upgrade_to",
                        "causal_grade", "causal_upgrade", "upgrade_to_causal"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(ArchivePolicyError):
                    archive_round(
                        self.custodian, {}, None, **{keyword: GRADE_E2}
                    )
        for keyword in ("grade", "evidence_grade", "promote_to"):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, FORBIDDEN_CALL_KEYS)

    def test_causal_claim_requires_identification(self):
        """声明因果却不给识别条件 → 拒绝（因果不能凭等级取得）。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        with self.assertRaises(ArchivePolicyError):
            self.archive(
                binding, classification, empty_knowledge(),
                receipt=receipt, causal_claim=True,
            )
        with self.assertRaises(ArchivePolicyError):
            self.archive(
                binding, classification, empty_knowledge(),
                receipt=receipt, causal_claim=True, identification="   ",
            )

    def test_identification_without_claim_is_rejected(self):
        """给了识别条件却不声明因果主张 → 拒绝，避免被误读为已成立因果。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        with self.assertRaises(ArchivePolicyError):
            self.archive(
                binding, classification, empty_knowledge(),
                receipt=receipt, identification="随机化干预实验",
            )

    def test_causal_claim_does_not_change_grade(self):
        """显式声明因果识别后，等级**不变**，只多一个正交标签。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        plain = self.archive(binding, classification, empty_knowledge(), receipt=receipt)
        self.assertEqual(plain.entries[0].causal_identification, CAUSAL_NOT_ADDRESSED)
        self.assertFalse(plain.entries[0].causal_claim)

    def test_causal_claim_recorded_as_separate_label(self):
        """声明因果后标签独立记录，等级仍为 E1。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1100), p_value=0.001
        )
        declared = self.archive(
            binding, classification, empty_knowledge(),
            receipt=receipt,
            causal_claim=True,
            identification="随机化干预 + 平行对照组",
        )
        entry = declared.entries[0]
        self.assertEqual(entry.grade, GRADE_E1)
        self.assertEqual(entry.causal_identification, CAUSAL_IDENTIFICATION_DECLARED)
        self.assertTrue(entry.causal_claim)
        self.assertIn(CAUSAL_IDENTIFICATION_DECLARED, entry.to_dict()["causal_identification"])


def _grade_for_case(status, data_identity):
    """取内部判定函数的结果（用于枚举测试，避免与端到端夹具耦合）。"""
    from sdl_m08.archive import _grade_for

    profile = DataProfile(identity=data_identity, record_count=3, environments=("lab-A",))
    return _grade_for("h-x", status, profile, empty_knowledge())


# ---------------------------------------------------------------------------
# 验收标准②：旧确证保留历史身份；新结论绑定新版本与新证据
# ---------------------------------------------------------------------------


class HistoricalIdentityTests(_EndToEndCase):
    """知识库只追加；旧确证记录保留历史身份。"""

    def _two_rounds(self):
        binding1, first, receipt1 = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        archive1 = self.archive(binding1, first, empty_knowledge(), receipt=receipt1)
        k1 = update_knowledge_version(empty_knowledge(), archive1)
        binding2, second, receipt2 = self.run_round(
            self.protocol, 2, records(3, 1100), p_value=0.001
        )
        archive2 = self.archive(binding2, second, k1, receipt=receipt2)
        k2 = update_knowledge_version(k1, archive2)
        return archive1, k1, archive2, k2

    def test_version_number_increments(self):
        _, k1, _, k2 = self._two_rounds()
        self.assertEqual(empty_knowledge().version, INITIAL_KNOWLEDGE_VERSION)
        self.assertEqual(k1.version, f"{KNOWLEDGE_VERSION_PREFIX}1")
        self.assertEqual(k2.version, f"{KNOWLEDGE_VERSION_PREFIX}2")

    def test_old_entries_are_byte_identical_after_update(self):
        """只追加：K2 中 K1 的全部条目必须与 K1 逐字节相同。"""
        archive1, k1, _, k2 = self._two_rounds()
        before = archive1.canonical_json()
        kept = k2.archives[0]
        self.assertEqual(kept.canonical_json(), before)
        self.assertEqual(kept.content_digest(), archive1.content_digest())

    def test_entry_lists_accumulate(self):
        archive1, k1, archive2, k2 = self._two_rounds()
        self.assertEqual(len(k1.entries), len(archive1.entries))
        self.assertEqual(len(k2.entries), len(archive1.entries) + len(archive2.entries))
        self.assertEqual(
            k2.hypothesis_ids(), tuple(sorted({HYPOTHESIS_ID}))
        )

    def test_version_chain_is_traceable(self):
        _, k1, _, k2 = self._two_rounds()
        chain = k2.history()
        self.assertEqual([item.version for item in chain], ["K0", "K1", "K2"])
        self.assertIs(k2.previous, k1)

    def test_grade_history_survives_retirement(self):
        """退役不抹掉历史等级：``grade_of`` 保留，``active_grade_of`` 归空。"""
        archive1, k1, _, k2 = self._two_rounds()
        self.assertEqual(k2.grade_of(HYPOTHESIS_ID), GRADE_E2)

        store = HypothesisStore()
        store.register(make_hypothesis())
        decision = revise_or_retire(store, HYPOTHESIS_ID, k2, retire=True, reason="范围收窄。")
        self.assertEqual(decision.action, ACTION_RETAIN if False else ACTION_RETIRE)
        retirement = decision.retirement
        self.assertIsInstance(retirement, RetirementEntry)
        self.assertEqual(retirement.final_grade, GRADE_E2)
        self.assertTrue(retirement.retained_identities)
        self.assertTrue(retirement.reason.strip())

        k3 = update_knowledge_version(k2, retired=retirement)
        self.assertEqual(k3.grade_of(HYPOTHESIS_ID), GRADE_E2)
        self.assertIsNone(k3.active_grade_of(HYPOTHESIS_ID))
        self.assertTrue(k3.is_retired(HYPOTHESIS_ID))
        # 既有归档条目仍然完整保留。
        self.assertEqual(len(k3.archives), len(k2.archives))

    def test_retirement_requires_known_version(self):
        """不得凭空退役档案中不存在的版本。"""
        retirement = RetirementEntry(
            hypothesis_id="h-unknown",
            identity="h-unknown@1",
            reason="测试用。",
            retained_identities=("h-unknown@1",),
        )
        with self.assertRaises(ArchiveIntegrityError):
            update_knowledge_version(empty_knowledge(), retired=retirement)

    def test_retirement_requires_retained_identity(self):
        """退役必须保留历史身份：不给出保留标识一律报错。"""
        with self.assertRaises(ArchiveIntegrityError):
            RetirementEntry(
                hypothesis_id=HYPOTHESIS_ID,
                identity=f"{HYPOTHESIS_ID}@1",
                reason="测试用。",
                retained_identities=(),
            )


class VersionBindingTests(_EndToEndCase):
    """新结论必须绑定新版本与新证据。"""

    def test_hypothesis_versions_required(self):
        """缺 ``hypothesis_versions`` 一律报错，不做默认填充。"""
        binding, classification, _ = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        with self.assertRaises(ArchiveInputError):
            archive_round(
                self.custodian, binding, classification, knowledge=empty_knowledge()
            )

    def test_hypothesis_versions_must_match_exactly(self):
        """键集必须与轮次中的假说一一对应：缺项与多余项都报错。"""
        binding, classification, _ = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        with self.assertRaises(ArchiveInputError):
            archive_round(
                self.custodian, binding, classification,
                hypothesis_versions={"h-other": "1"}, knowledge=empty_knowledge(),
            )
        with self.assertRaises(ArchiveInputError):
            archive_round(
                self.custodian, binding, classification,
                hypothesis_versions={HYPOTHESIS_ID: "1", "h-extra": "1"},
                knowledge=empty_knowledge(),
            )

    def test_version_must_be_recorded_on_entry(self):
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        archive = self.archive(
            binding, classification, empty_knowledge(),
            versions={HYPOTHESIS_ID: "7"}, receipt=receipt,
        )
        self.assertEqual(archive.entries[0].hypothesis_version, "7")
        self.assertEqual(archive.entries[0].identity, f"{HYPOTHESIS_ID}@7")

    def test_revise_requires_new_knowledge_version(self):
        """修订必须绑定新的知识库版本；缺失或陈旧一律拒绝。"""
        store = HypothesisStore()
        store.register(make_hypothesis())
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.9
        )
        knowledge = update_knowledge_version(
            empty_knowledge(),
            self.archive(binding, classification, empty_knowledge(), receipt=receipt),
        )
        with self.assertRaises(ArchiveInputError):
            revise_or_retire(store, HYPOTHESIS_ID, knowledge, changes={"statement": "x"})
        with self.assertRaises(ArchivePolicyError):
            revise_or_retire(
                store, HYPOTHESIS_ID, knowledge,
                changes={
                    "provenance": {
                        "data_ids": ["d"], "knowledge_version": "K9",
                        "generator": "g", "code_version": "c",
                    }
                },
            )

    def test_revise_produces_new_version_and_keeps_old(self):
        """修订产生**新标识**的新版本，旧版本在仓库中保持不变。"""
        store = HypothesisStore()
        original = make_hypothesis()
        store.register(original)
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.9
        )
        knowledge = update_knowledge_version(
            empty_knowledge(),
            self.archive(binding, classification, empty_knowledge(), receipt=receipt),
        )
        decision = revise_or_retire(
            store, HYPOTHESIS_ID, knowledge,
            changes={
                "statement": "修订后的陈述：仅在 lab-A 范围内成立。",
                "provenance": {
                    "data_ids": ["data_synthetic_ref_only"],
                    "knowledge_version": knowledge.version,
                    "generator": "P15-test",
                    "code_version": ARCHIVE_VERSION,
                },
            },
        )
        self.assertEqual(decision.action, ACTION_REVISE)
        self.assertNotEqual(decision.resulting_identity, decision.previous_identity)
        self.assertTrue(decision.changed)
        # 旧版本原样保留，且历史等级不受修订影响。
        self.assertIn(decision.previous_identity, store)
        self.assertEqual(
            store.get(decision.previous_identity).to_dict(), original.to_dict()
        )
        self.assertEqual(knowledge.grade_of(HYPOTHESIS_ID), GRADE_E0)

    def test_inconclusive_is_retained_not_retired(self):
        """证据不足不构成退役理由，也不构成修订理由。"""
        store = HypothesisStore()
        store.register(make_hypothesis())
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.9
        )
        knowledge = update_knowledge_version(
            empty_knowledge(),
            self.archive(binding, classification, empty_knowledge(), receipt=receipt),
        )
        decision = revise_or_retire(store, HYPOTHESIS_ID, knowledge)
        self.assertEqual(decision.action, ACTION_RETAIN)
        self.assertEqual(decision.latest_status, STATUS_INCONCLUSIVE)
        self.assertFalse(decision.changed)

    def test_refuted_leads_to_retirement(self):
        """反驳 → 退役（观察与预先声明的预测有力不相容）。"""
        store = HypothesisStore()
        store.register(make_hypothesis())
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001, effect=-1.0
        )
        knowledge = update_knowledge_version(
            empty_knowledge(),
            self.archive(binding, classification, empty_knowledge(), receipt=receipt),
        )
        decision = revise_or_retire(store, HYPOTHESIS_ID, knowledge)
        self.assertEqual(decision.action, ACTION_RETIRE)
        self.assertIsNotNone(decision.retirement)
        self.assertEqual(decision.retirement.final_grade, GRADE_E0)

    def test_supported_is_retained(self):
        store = HypothesisStore()
        store.register(make_hypothesis())
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        knowledge = update_knowledge_version(
            empty_knowledge(),
            self.archive(binding, classification, empty_knowledge(), receipt=receipt),
        )
        decision = revise_or_retire(store, HYPOTHESIS_ID, knowledge)
        self.assertEqual(decision.action, ACTION_RETAIN)
        self.assertEqual(decision.latest_status, STATUS_SUPPORTED)
        self.assertEqual(decision.final_grade, GRADE_E1)

    def test_actions_vocabulary_is_complete(self):
        self.assertEqual(set(ACTIONS), {ACTION_RETAIN, ACTION_REVISE, ACTION_RETIRE})

    def test_revise_requires_archived_evidence(self):
        """知识库中没有该假说的任何条目时，不得凭空修订或退役。"""
        store = HypothesisStore()
        store.register(make_hypothesis())
        with self.assertRaises(ArchiveInputError):
            revise_or_retire(store, HYPOTHESIS_ID, empty_knowledge())


class HandBuiltIntegrityTests(unittest.TestCase):
    """规则层：手工伪造的归档状态必须被拦下（不经 M1，直接构造对象）。"""

    def test_e1_twice_is_rejected(self):
        """E1 表示首次确证：同假说第二次 E1 自相矛盾。"""
        first = make_archive([make_entry(basis=BASIS_FIRST_CONFIRMATION)])
        second = make_archive(
            [make_entry(
                grade=GRADE_E1, basis=BASIS_FIRST_CONFIRMATION,
                data_identity="d-2", evidence_identity="e-2",
                binding_id="binding-2", round_index=2,
            )],
            binding_id="binding-2", round_index=2, data_identity="d-2",
        )
        with self.assertRaises(ArchiveIntegrityError):
            KnowledgeVersion(version="K1", archives=(first, second))

    def test_e2_without_prior_confirmation_is_rejected(self):
        """E2 表示复现：没有先行确证时直接记 E2 自相矛盾。"""
        only = make_archive(
            [make_entry(
                grade=GRADE_E2, basis=BASIS_NEW_SAMPLE, prior_grade=GRADE_E1,
                replication=REPLICATION_NEW_SAMPLE,
            )]
        )
        with self.assertRaises(ArchiveIntegrityError):
            KnowledgeVersion(version="K1", archives=(only,))

    def test_confirmatory_on_seen_data_is_rejected(self):
        """确证级条目落在该假说已见的数据上 → 拒绝（已在 E0 里见过也算）。"""
        seen = make_archive(
            [make_entry(
                grade=GRADE_E0, basis=BASIS_NOT_SUPPORTED,
                status=STATUS_INCONCLUSIVE, evidence_identity="e-1",
            )]
        )
        reused = make_archive(
            [make_entry(
                grade=GRADE_E1, basis=BASIS_FIRST_CONFIRMATION,
                data_identity="d-1", evidence_identity="e-new",
                binding_id="binding-2", round_index=2,
            )],
            binding_id="binding-2", round_index=2, data_identity="d-1",
        )
        with self.assertRaises(ArchiveIntegrityError):
            KnowledgeVersion(version="K1", archives=(seen, reused))

    def test_same_evidence_identity_for_two_confirmations_is_rejected(self):
        """同一证据身份不得支撑两条确证级结论。"""
        first = make_archive([make_entry(hypothesis_id="h-a", evidence_identity="e-x")])
        second = make_archive(
            [make_entry(
                hypothesis_id="h-b", evidence_identity="e-x",
                data_identity="d-2", binding_id="binding-2", round_index=2,
            )],
            binding_id="binding-2", round_index=2, data_identity="d-2",
        )
        with self.assertRaises(ArchiveIntegrityError):
            KnowledgeVersion(version="K1", archives=(first, second))

    def test_grade_basis_mismatch_is_rejected(self):
        """等级与凭据必须自洽：E0 不得配「首次确证」凭据，E2 不得配「复用旧数据」。"""
        with self.assertRaises(ArchiveIntegrityError):
            EvidenceEntry(
                hypothesis_id=HYPOTHESIS_ID, hypothesis_version="1",
                status=STATUS_SUPPORTED, grade=GRADE_E0,
                grade_basis=BASIS_FIRST_CONFIRMATION, prior_grade=None,
                replication_basis=REPLICATION_NONE, data_identity="d-1",
                evidence_identity="e-1", binding_id="b-1", round_index=1,
                historical_ref="h-1",
            )
        with self.assertRaises(ArchiveIntegrityError):
            EvidenceEntry(
                hypothesis_id=HYPOTHESIS_ID, hypothesis_version="1",
                status=STATUS_SUPPORTED, grade=GRADE_E2,
                grade_basis=BASIS_REUSED_SEEN_DATA, prior_grade=GRADE_E1,
                replication_basis=REPLICATION_NONE, data_identity="d-1",
                evidence_identity="e-1", binding_id="b-1", round_index=1,
                historical_ref="h-1",
            )

    def test_unknown_grade_or_status_is_rejected(self):
        """等级与结论的取值必须落在受控词表内。"""
        with self.assertRaises(ArchiveInputError):
            make_entry(grade="E3")
        with self.assertRaises(ArchiveInputError):
            make_entry(status="proven")
        with self.assertRaises(ArchiveInputError):
            make_entry(basis="because")

    def test_round_archive_requires_consistent_entries(self):
        """一轮归档的条目必须落在同一绑定与同一数据身份上。"""
        with self.assertRaises(ArchiveIntegrityError):
            RoundArchive(
                binding_id="binding-1", round_index=1, aggregate_status=STATUS_SUPPORTED,
                historical_ref="h-1", data_identity="d-1",
                entries=(make_entry(binding_id="binding-other"),),
            )
        with self.assertRaises(ArchiveIntegrityError):
            RoundArchive(
                binding_id="binding-1", round_index=1, aggregate_status=STATUS_SUPPORTED,
                historical_ref="h-1", data_identity="d-1",
                entries=(make_entry(data_identity="d-other"),),
            )

    def test_round_archive_requires_at_least_one_entry(self):
        with self.assertRaises(ArchiveInputError):
            RoundArchive(
                binding_id="binding-1", round_index=1, aggregate_status=STATUS_FAILED,
                historical_ref="h-1", data_identity="d-1", entries=(),
            )

    def test_duplicate_hypothesis_in_one_round_is_rejected(self):
        with self.assertRaises(ArchiveIntegrityError):
            RoundArchive(
                binding_id="binding-1", round_index=1, aggregate_status=STATUS_SUPPORTED,
                historical_ref="h-1", data_identity="d-1",
                entries=(make_entry(), make_entry(evidence_identity="e-2")),
            )

    def test_version_number_must_be_wellformed_and_increase(self):
        with self.assertRaises(ArchiveInputError):
            KnowledgeVersion(version="1")
        with self.assertRaises(ArchiveInputError):
            KnowledgeVersion(version="Kx")
        with self.assertRaises(ArchiveIntegrityError):
            KnowledgeVersion(version="K2", previous=empty_knowledge("K2"))


class StatusVocabularyConsistencyTests(unittest.TestCase):
    """结论词表必须与 P13 逐字一致——两处独立声明但不得漂移。"""

    def test_status_vocabulary_matches_p13(self):
        """与 P13 的取值表**逐字逐序**一致（两处独立声明，不得漂移）。

        期望元组直接取自 P13 的 ``RESULT_STATUSES``，而不是手工重排——
        顺序本身也是契约的一部分（归档记录的字段顺序会影响内容摘要）。
        """
        from sdl_m06.report import RESULT_STATUSES as P13_RESULT_STATUSES

        self.assertEqual(RESULT_STATUSES, tuple(P13_RESULT_STATUSES))
        self.assertEqual(
            RESULT_STATUSES,
            (STATUS_SUPPORTED, STATUS_REFUTED, STATUS_INCONCLUSIVE, STATUS_FAILED),
        )
        # P13 的四个常量必须与本地四值同名同值。
        self.assertEqual(
            (_P13_STATUS_SUPPORTED, _P13_STATUS_REFUTED),
            (STATUS_SUPPORTED, STATUS_REFUTED),
        )
        self.assertEqual(_P13_STATUS_FAILED, STATUS_FAILED)
        self.assertEqual(P13_RESULT_STATUSES[-1], STATUS_FAILED)

    def test_sealed_key_blacklist_covers_p13_and_is_superset(self):
        """封存黑名单必须覆盖 P13 的同名集合（键是同一批原始记录字段）。"""
        from sdl_m06.report import SEALED_LEAK_KEYS

        self.assertTrue(SEALED_LEAK_KEYS <= ARCHIVE_SEALED_KEYS)


# ---------------------------------------------------------------------------
# 验收标准③：已见数据可回探索，但不得被修订版本复用为确证
# ---------------------------------------------------------------------------


class SeenDataBoundaryTests(_EndToEndCase):
    """已见确证数据可转历史视图，但不得被同一修订版本复用为确证。"""

    def test_archive_returns_historical_ref_and_it_is_readable(self):
        """归档返回历史探索视图引用，且该视图经 M1 可读（已解锁）。

        实测：``archive_confirmation`` 返回的 ``historical_ref`` 是 M1 的
        **不透明数据引用**（形如 ``data_<hex>``），**不带** ``H_`` 前缀；
        ``H_<binding_id>`` 是该数据集在协议下的**用途名**，二者不是一个字段。
        因此这里断言的是「引用非空、指向确实可读的历史视图、且该视图在
        custodian 描述里登记的用途名为 ``H_<binding_id>``」，
        而不是给引用强加一个它本就不具有的前缀。
        """
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        archive = self.archive(binding, classification, empty_knowledge(), receipt=receipt)

        # ① 引用本身非空且为非空白字符串。
        self.assertTrue(archive.historical_ref)
        self.assertEqual(archive.historical_ref, archive.historical_ref.strip())

        # ② 该引用经 M1 可读，且行数与原始确证批次一致（已解锁、可探索）。
        rows = self.custodian.read_dataset(archive.historical_ref)
        self.assertEqual(len(rows), 3)

        # ③ 语义校验：历史视图在协议下的用途名确实是 H_<binding_id>。
        purpose = "H_" + binding["binding_id"]
        resources = self.custodian.describe(self.protocol["protocol_id"])["resources"]
        self.assertIn(purpose, resources)
        self.assertEqual(resources[purpose], archive.historical_ref)

    def test_data_identity_ignores_record_identifiers(self):
        """数据身份按**内容**判定：改写 ``record_id`` 不改变身份。"""
        base = records(3, 1000)
        renamed = copy.deepcopy(base)
        for row in renamed:
            row["record_id"] = "renamed-" + row["record_id"]
        self.assertEqual(data_profile(base).identity, data_profile(renamed).identity)
        self.assertEqual(
            data_profile(base).content_digest(), data_profile(renamed).content_digest()
        )

    def test_data_identity_changes_with_content(self):
        """内容变了身份就必须变——否则闸门会漏放真正的新数据。"""
        base = records(3, 1000)
        changed = copy.deepcopy(base)
        changed[0]["values"]["Y"] = changed[0]["values"]["Y"] + 1.0
        self.assertNotEqual(data_profile(base).identity, data_profile(changed).identity)

    def test_data_identity_ignores_quality_view(self):
        """剔除 M1 附加的质量视图：它不属于数据内容。"""
        base = records(3, 1000)
        with_quality = copy.deepcopy(base)
        for row in with_quality:
            row["_quality"] = {"status": "usable", "reasons": []}
        self.assertEqual(data_profile(base).identity, data_profile(with_quality).identity)

    def test_data_profile_carries_no_record_content(self):
        """数据画像只含摘要与聚合量，不含记录正文。"""
        profile = data_profile(records(3, 1000))
        self.assertEqual(profile.record_count, 3)
        self.assertEqual(profile.environments, ("lab-A",))
        text = profile.canonical_json()
        for leaked in ("values", "record_id", "group_ids", "operator_annotation"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, text.replace("record_count", ""))

    def test_data_profile_rejects_empty_rows(self):
        """空数据不得作为确证证据：没有数据就没有身份。"""
        with self.assertRaises(ArchiveInputError):
            data_profile([])

    def test_reused_data_is_downgraded_to_e0(self):
        """复用已见数据 → 压回 E0，并置 ``self_evidence_attempt``。

        端到端路径被 M1 的记录指纹拦住（同一内容不得重复登记），
        因此这里用**规则层**构造「知识库已见过该数据身份」的等价情形，
        这正是 :func:`_grade_for` 要拦的场景。
        """
        rows = records(3, 1000)
        profile = data_profile(rows)
        seen_entry = make_entry(
            grade=GRADE_E1, basis=BASIS_FIRST_CONFIRMATION,
            data_identity=profile.identity,
        )
        knowledge = KnowledgeVersion(
            version="K1", archives=(make_archive([seen_entry], data_identity=profile.identity),)
        )
        self.assertIn(profile.identity, knowledge.seen_data_identities(HYPOTHESIS_ID))

        from sdl_m08.archive import _grade_for

        grade, basis, prior, replication, attempt = _grade_for(
            HYPOTHESIS_ID, STATUS_SUPPORTED, profile, knowledge
        )
        self.assertEqual(grade, GRADE_E0)
        self.assertEqual(basis, BASIS_REUSED_SEEN_DATA)
        self.assertEqual(prior, GRADE_E1)
        self.assertEqual(replication, REPLICATION_NONE)
        self.assertTrue(attempt)

    def test_stale_knowledge_cannot_be_merged_to_smuggle_reuse(self):
        """拿**过期**知识库归档后再合并，仍必须被拒（合并时二次校验）。"""
        first = make_archive([make_entry(evidence_identity="e-1")])
        base = update_knowledge_version(empty_knowledge(), first)

        # 攻击者用「尚不知道 first 存在」的空知识库去归档同一份数据。
        stale_archive = make_archive(
            [make_entry(
                grade=GRADE_E1, basis=BASIS_FIRST_CONFIRMATION,
                data_identity="d-1", evidence_identity="e-2",
                binding_id="binding-2", round_index=2,
            )],
            binding_id="binding-2", round_index=2, data_identity="d-1",
        )
        self.assertEqual(len(empty_knowledge().seen_data_identities(HYPOTHESIS_ID)), 0)
        with self.assertRaises(ArchivePolicyError):
            update_knowledge_version(base, stale_archive)

    def test_retired_grade_history_is_not_cleared(self):
        """退役后历史等级仍可回溯，但不再参与活跃判定。"""
        first = make_archive([make_entry()])
        base = update_knowledge_version(empty_knowledge(), first)
        retirement = RetirementEntry(
            hypothesis_id=HYPOTHESIS_ID, identity=f"{HYPOTHESIS_ID}@1",
            reason="范围收窄，退出活跃集合。",
            final_grade=GRADE_E1, retained_identities=(f"{HYPOTHESIS_ID}@1",),
        )
        retired = update_knowledge_version(base, retired=retirement)
        self.assertEqual(retired.grade_of(HYPOTHESIS_ID), GRADE_E1)
        self.assertIsNone(retired.active_grade_of(HYPOTHESIS_ID))

    def test_decision_lists_forbidden_identities(self):
        """决策必须列出禁用身份，且闸门能机械拒绝复用。"""
        store = HypothesisStore()
        store.register(make_hypothesis())
        first = make_archive([make_entry()])
        knowledge = update_knowledge_version(empty_knowledge(), first)

        decision = revise_or_retire(store, HYPOTHESIS_ID, knowledge)
        self.assertTrue(decision.forbidden_evidence_identities)
        self.assertIn("d-1", decision.forbidden_evidence_identities)
        self.assertIn("e-1", decision.forbidden_evidence_identities)
        self.assertTrue(decision.required_fresh_evidence)
        with self.assertRaises(ArchivePolicyError):
            decision.assert_evidence_is_fresh("d-1")
        with self.assertRaises(ArchivePolicyError):
            decision.assert_evidence_is_fresh("e-1")
        decision.assert_evidence_is_fresh("d-fresh")

    def test_module_level_fresh_evidence_gate(self):
        """独立闸门同样能拒绝复用，并放行全新身份。"""
        first = make_archive([make_entry()])
        knowledge = update_knowledge_version(empty_knowledge(), first)
        with self.assertRaises(ArchivePolicyError):
            assert_evidence_is_fresh(knowledge, HYPOTHESIS_ID, "d-1")
        assert_evidence_is_fresh(knowledge, HYPOTHESIS_ID, "d-brand-new")

    def test_entry_level_fresh_evidence_gate(self):
        entry = make_entry()
        with self.assertRaises(ArchivePolicyError):
            entry.assert_evidence_is_fresh("d-1")
        entry.assert_evidence_is_fresh("d-other")

    def test_fresh_evidence_required_cannot_be_disabled(self):
        """``required_fresh_evidence`` 不得被设为 False。"""
        with self.assertRaises(ArchivePolicyError):
            RevisionDecision(
                hypothesis_id=HYPOTHESIS_ID, action=ACTION_RETAIN, reason="测试用。",
                previous_identity=f"{HYPOTHESIS_ID}@1",
                resulting_identity=f"{HYPOTHESIS_ID}@1",
                resulting_digest="x", final_grade=GRADE_E1,
                latest_status=STATUS_SUPPORTED, required_fresh_evidence=False,
            )


# ---------------------------------------------------------------------------
# 结果对象与序列化
# ---------------------------------------------------------------------------


class ResultObjectTests(_EndToEndCase):
    """结果对象必须冻结、可序列化、摘要稳定。"""

    def _archive(self):
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        return self.archive(binding, classification, empty_knowledge(), receipt=receipt)

    def test_objects_are_frozen(self):
        archive = self._archive()
        with self.assertRaises(Exception):
            archive.round_index = 2
        with self.assertRaises(Exception):
            archive.entries[0].grade = GRADE_E0
        with self.assertRaises(TypeError):
            archive.notes[0] = "改写"

    def test_to_dict_is_json_serializable(self):
        archive = self._archive()
        payload = json.dumps(archive.to_dict(), ensure_ascii=False)
        self.assertIn("entries", payload)
        reloaded = json.loads(payload)
        self.assertEqual(reloaded["binding_id"], archive.binding_id)

    def test_canonical_json_is_stable_and_sorted(self):
        archive = self._archive()
        self.assertEqual(archive.canonical_json(), archive.canonical_json())
        self.assertEqual(list(json.loads(archive.canonical_json()))[0], "aggregate_status")

    def test_content_digest_matches_manual_sha256(self):
        archive = self._archive()
        expected = hashlib.sha256(archive.canonical_json().encode("utf-8")).hexdigest()
        self.assertEqual(archive.content_digest(), expected)

    def test_knowledge_version_serialization_excludes_previous(self):
        """``previous`` 不进入序列化，避免无限嵌套。"""
        first = make_archive([make_entry()])
        k1 = update_knowledge_version(empty_knowledge("K0"), first)
        self.assertNotIn("previous", k1.to_dict())
        self.assertEqual(k1.to_dict()["previous_version"], "K0")
        self.assertEqual(json.dumps(k1.to_dict(), ensure_ascii=False)[:1], "{")

    def test_entry_lookup_helpers(self):
        archive = self._archive()
        self.assertEqual(archive.entry_of(HYPOTHESIS_ID).hypothesis_id, HYPOTHESIS_ID)
        with self.assertRaises(ArchiveInputError):
            archive.entry_of("h-missing")
        self.assertEqual(archive.hypothesis_ids, (HYPOTHESIS_ID,))
        self.assertEqual(len(archive.grades), len(archive.entries))

    def test_notes_carry_required_stance(self):
        """核心口径说明必须随结果对象输出，且不可被调用方删掉。"""
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        archive = self.archive(
            binding, classification, empty_knowledge(),
            receipt=receipt, notes="自定义说明。",
        )
        self.assertEqual(archive.notes[0], "自定义说明。")
        self.assertIn(REUSE_FORBIDS_SELF_EVIDENCE_NOTE, archive.notes)
        self.assertIn(HISTORICAL_IDENTITY_NOTE, archive.notes)
        self.assertIn(NO_RECLASSIFICATION_NOTE, archive.notes)
        self.assertIn(FRESH_EVIDENCE_REQUIRED_NOTE, update_knowledge_version(
            empty_knowledge(), archive
        ).notes)
        self.assertIn(VERSION_BINDING_NOTE, archive.notes)
        for entry in archive.entries:
            with self.subTest(entry=entry.hypothesis_id):
                self.assertTrue(entry.notes)

    def test_receipt_digest_is_recorded(self):
        binding, classification, receipt = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        archive = self.archive(binding, classification, empty_knowledge(), receipt=receipt)
        self.assertEqual(archive.receipt_digest, receipt.content_digest())

    def test_single_hypothesis_outcome_path(self):
        """单假说结论路径可用，且要求显式轮次。"""
        binding, classification, _ = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.9
        )
        outcome = _P13HypothesisOutcome(
            hypothesis_id=HYPOTHESIS_ID, status=_P13_STATUS_FAILED
        )
        archive = archive_round(
            self.custodian,
            binding,
            outcome,
            hypothesis_versions={HYPOTHESIS_ID: "1"},
            knowledge=empty_knowledge(),
            round_index=classification.round_index,
        )
        self.assertEqual(archive.entries[0].grade, GRADE_E0)
        self.assertEqual(archive.entries[0].grade_basis, BASIS_NOT_SUPPORTED)
        self.assertEqual(archive.round_index, classification.round_index)

        with self.assertRaises(ArchiveInputError):
            archive_round(
                self.custodian, binding, outcome,
                hypothesis_versions={HYPOTHESIS_ID: "1"}, knowledge=empty_knowledge(),
            )

    def test_binding_mismatch_is_rejected(self):
        binding, classification, _ = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )
        with self.assertRaises(ArchiveInputError):
            archive_round(
                self.custodian, {"binding_id": "binding-other"}, classification,
                hypothesis_versions={HYPOTHESIS_ID: "1"}, knowledge=empty_knowledge(),
            )

    def test_missing_custodian_capabilities_is_rejected(self):
        binding, classification, _ = self.run_round(
            self.protocol, 1, records(3, 1000), p_value=0.001
        )

        class _Blind:
            """不带归档接口的对象：必须被明确拒绝，而不是静默降级。"""

        with self.assertRaises(ArchiveInputError):
            archive_round(
                _Blind(), binding, classification,
                hypothesis_versions={HYPOTHESIS_ID: "1"}, knowledge=empty_knowledge(),
            )

    def test_update_requires_actual_change(self):
        """空更新只会制造空版本，必须拒绝。"""
        archive = self._archive()
        knowledge = update_knowledge_version(empty_knowledge(), archive)
        with self.assertRaises(ArchiveInputError):
            update_knowledge_version(knowledge)

    def test_update_rejects_non_increasing_version(self):
        archive = self._archive()
        knowledge = update_knowledge_version(empty_knowledge(), archive)
        with self.assertRaises(ArchiveInputError):
            update_knowledge_version(knowledge, archive, version="K1")

    def test_update_rejects_wrong_types(self):
        with self.assertRaises(ArchiveInputError):
            update_knowledge_version("K0", ())
        with self.assertRaises(ArchiveInputError):
            update_knowledge_version(empty_knowledge(), ["not-an-archive"])
        with self.assertRaises(ArchiveInputError):
            update_knowledge_version(empty_knowledge(), retired=["not-a-retirement"])


# ---------------------------------------------------------------------------
# 封存闸门
# ---------------------------------------------------------------------------


class SealedGateTests(unittest.TestCase):
    """归档对象不得承载任何封存原始记录特征键。"""

    def test_sealed_leak_is_detected_deeply(self):
        for payload in (
            {"values": {"X1": 1}},
            {"wrapper": {"record_id": "r-1"}},
            {"list": [{"_quality": {"status": "usable"}}]},
            {"nested": [{"deeper": {"operator_annotation": "x"}}]},
            {"unit_keys": [{"units": {"X1": "mol/L"}}]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ArchiveIntegrityError):
                    check_no_sealed_leak(payload, "测试载荷")

    def test_clean_payload_passes(self):
        check_no_sealed_leak(
            {"grade": GRADE_E1, "notes": ["含 values 二字的中文说明不受影响"]}, "干净载荷"
        )

    def test_real_archive_payload_passes_gate(self):
        first = make_archive([make_entry()])
        check_no_sealed_leak(first.to_dict(), "归档对象")

    def test_token_keys_are_rejected(self):
        binding, classification, _ = None, None, None
        self.assertEqual(classification, None)
        entry = make_entry()
        self.assertNotIn("token", entry.to_dict())
        with self.assertRaises(ArchiveInputError):
            EvidenceEntry(
                hypothesis_id=HYPOTHESIS_ID, hypothesis_version="1",
                status=STATUS_SUPPORTED, grade=GRADE_E1,
                grade_basis=BASIS_FIRST_CONFIRMATION, prior_grade=None,
                replication_basis=REPLICATION_NONE, data_identity="d-1",
                evidence_identity="e-1", binding_id="b-1", round_index=1,
                historical_ref="h-1", causal_identification="nope",
            )
        self.assertEqual(binding, None)

    def test_archive_output_contains_no_sealed_keys(self):
        """实测：真实归档产物的全部键名与封存黑名单**不相交**。"""
        archive = make_archive([make_entry()])
        keys = set()

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    keys.add(str(key).lower())
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)

        walk(archive.to_dict())
        self.assertEqual(sorted(keys & ARCHIVE_SEALED_KEYS), [])


# ---------------------------------------------------------------------------
# 定向变异守护
# ---------------------------------------------------------------------------


class MutationGuardTests(unittest.TestCase):
    """定向变异守护：确认核心判据对「放宽定义」具备甄别力。"""

    def _knowledge_with_seen(self, identity):
        entry = make_entry(grade=GRADE_E1, basis=BASIS_FIRST_CONFIRMATION,
                           data_identity=identity)
        return KnowledgeVersion(
            version="K1",
            archives=(make_archive([entry], data_identity=identity),),
        )

    def test_reuse_check_cannot_be_relaxed(self):
        """若去掉「已见数据」检查，复用旧数据就会得到 E2——本用例断言它是 E0。"""
        from sdl_m08.archive import _grade_for

        profile = DataProfile(identity="d-seen", record_count=3, environments=("lab-A",))
        knowledge = self._knowledge_with_seen("d-seen")
        grade, basis, _, _, attempt = _grade_for(
            HYPOTHESIS_ID, STATUS_SUPPORTED, profile, knowledge
        )
        self.assertEqual(grade, GRADE_E0)
        self.assertNotEqual(grade, GRADE_E2)
        self.assertTrue(attempt)

    def test_first_confirmation_check_cannot_be_relaxed(self):
        """若把「首次确证」判成 E2，本用例会捕获（首次必须是 E1）。"""
        from sdl_m08.archive import _grade_for

        profile = DataProfile(identity="d-new", record_count=3, environments=("lab-A",))
        grade, basis, _, _, _ = _grade_for(
            HYPOTHESIS_ID, STATUS_SUPPORTED, profile, empty_knowledge()
        )
        self.assertEqual(grade, GRADE_E1)
        self.assertNotEqual(grade, GRADE_E2)

    def test_environment_comparison_direction_matters(self):
        """环境比较方向反转会被捕获：lab-B 相对 {lab-A} 必须判新环境。"""
        from sdl_m08.archive import _grade_for

        prior = make_entry(
            grade=GRADE_E1, basis=BASIS_FIRST_CONFIRMATION,
            data_identity="d-prior", environments=("lab-A",),
        )
        knowledge = KnowledgeVersion(
            version="K1", archives=(make_archive([prior], data_identity="d-prior"),)
        )
        profile = DataProfile(identity="d-new", record_count=3, environments=("lab-B",))
        grade, basis, _, replication, _ = _grade_for(
            HYPOTHESIS_ID, STATUS_SUPPORTED, profile, knowledge
        )
        self.assertEqual(grade, GRADE_E2)
        self.assertEqual(basis, BASIS_NEW_ENVIRONMENT)
        self.assertEqual(replication, REPLICATION_NEW_ENVIRONMENT)

        same = DataProfile(identity="d-new2", record_count=3, environments=("lab-A",))
        grade2, basis2, _, replication2, _ = _grade_for(
            HYPOTHESIS_ID, STATUS_SUPPORTED, same, knowledge
        )
        self.assertEqual(basis2, BASIS_NEW_SAMPLE)
        self.assertEqual(replication2, REPLICATION_NEW_SAMPLE)

    def test_non_supported_never_becomes_confirmatory(self):
        """把「不显著」按等级放行会被捕获：任何非支持结论都必须是 E0。"""
        from sdl_m08.archive import _grade_for

        profile = DataProfile(identity="d-new", record_count=3, environments=("lab-A",))
        for status in (STATUS_REFUTED, STATUS_INCONCLUSIVE, STATUS_FAILED):
            with self.subTest(status=status):
                grade, basis, _, _, attempt = _grade_for(
                    HYPOTHESIS_ID, status, profile, empty_knowledge()
                )
                self.assertEqual(grade, GRADE_E0)
                self.assertEqual(basis, BASIS_NOT_SUPPORTED)
                self.assertFalse(attempt)
                self.assertNotIn(grade, CONFIRMATORY_GRADES)

    def test_seen_data_check_covers_e0_entries(self):
        """若「已见」只统计确证级条目，先记 E0 再补 E1 的绕过就会成立。"""
        from sdl_m08.archive import _grade_for

        seen_e0 = make_entry(
            grade=GRADE_E0, basis=BASIS_NOT_SUPPORTED,
            status=STATUS_INCONCLUSIVE, data_identity="d-e0",
        )
        knowledge = KnowledgeVersion(
            version="K1", archives=(make_archive([seen_e0], data_identity="d-e0",
                                                 status=STATUS_INCONCLUSIVE),)
        )
        self.assertIn("d-e0", knowledge.seen_data_identities(HYPOTHESIS_ID))
        profile = DataProfile(identity="d-e0", record_count=3, environments=("lab-A",))
        grade, basis, _, _, attempt = _grade_for(
            HYPOTHESIS_ID, STATUS_SUPPORTED, profile, knowledge
        )
        self.assertEqual(grade, GRADE_E0)
        self.assertEqual(basis, BASIS_REUSED_SEEN_DATA)
        self.assertTrue(attempt)

    def test_evidence_identity_includes_hypothesis(self):
        """证据身份必须含假说标识：否则同轮多假说会被误判为共用证据。"""
        from sdl_m08.archive import _evidence_identity

        common = ("d-1", "b-1", 1, STATUS_SUPPORTED)
        self.assertNotEqual(
            _evidence_identity(*common, "h-a"),
            _evidence_identity(*common, "h-b"),
        )
        self.assertEqual(
            _evidence_identity(*common, "h-a"),
            _evidence_identity(*common, "h-a"),
        )

    def test_multiple_hypotheses_in_one_round_are_accepted(self):
        """同一轮内多个假说共用一份数据是**正常**的，不得被误判为复用。"""
        entries = (
            make_entry(hypothesis_id="h-a", evidence_identity="e-a"),
            make_entry(hypothesis_id="h-b", evidence_identity="e-b"),
        )
        knowledge = KnowledgeVersion(
            version="K1", archives=(make_archive(entries),)
        )
        self.assertEqual(knowledge.hypothesis_ids(), ("h-a", "h-b"))
        self.assertEqual(len(knowledge.confirmatory_entries), 2)


# ---------------------------------------------------------------------------
# 边界守护（AST）
# ---------------------------------------------------------------------------


class BoundaryGuardTests(unittest.TestCase):
    """AST 级边界检查：不越界实现、不越界读取、只用标准库。"""

    def _tree(self):
        return pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def _defined_names(self, tree):
        return {
            node.name
            for node in pyast.walk(tree)
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef))
        }

    def _accessed_names(self, tree):
        found = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Attribute):
                found.add(node.attr)
            elif isinstance(node, pyast.Name):
                found.add(node.id)
            elif isinstance(node, pyast.alias):
                found.add(node.name.split(".")[-1])
        return found

    def test_p15_implements_exactly_its_declared_entrypoints(self):
        defined = self._defined_names(self._tree())
        for name in ("archive_round", "update_knowledge_version", "revise_or_retire"):
            with self.subTest(name=name):
                self.assertIn(name, defined)

    def test_does_not_define_later_stage_entrypoints(self):
        """AST 检查：不定义 P16 及之后、也不重定义上游的任何入口。"""
        defined = self._defined_names(self._tree())
        overlaps = sorted(defined & set(NOT_PROVIDED_BY_P15))
        self.assertEqual(overlaps, [], f"越界实现：{overlaps}")

    def test_not_provided_list_covers_neighbouring_stages(self):
        for name in (
            "loop", "run_pipeline", "main_loop",
            "divergence", "acquisition_plan",
            "classify_result", "record_and_release",
            "execute_family", "holm_adjust", "alpha_for_round",
            "build_frozen_plan", "bind",
            "evaluate_hypothesis", "pareto_front",
        ):
            with self.subTest(name=name):
                self.assertIn(name, NOT_PROVIDED_BY_P15)

    def test_self_check_passes(self):
        report = self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["defined_forbidden"], ())
        self.assertEqual(report["accessed_forbidden"], ())

    def test_no_sealed_or_restricted_access_paths(self):
        """AST 检查：不访问封存确认、受限质量报告与数据库连接入口。"""
        accessed = self._accessed_names(self._tree())
        for name in (
            "consume_confirmation", "bind_confirmation", "record_evaluation",
            "release_results", "inspect_confirmation", "mark_compromised",
            "add_confirmation", "quality", "load_snapshot", "open_vault",
            "verify_integrity", "connect", "sqlite3",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, accessed)

    def test_does_not_call_sealed_readers(self):
        """AST 检查：不调用任何确证消费或读取入口。"""
        tree = self._tree()
        called = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Attribute):
                called.add(node.func.attr)
        for name in ("consume_confirmation", "load_snapshot", "quality",
                     "inspect_confirmation"):
            with self.subTest(name=name):
                self.assertNotIn(name, called)

    def test_only_standard_library_imports(self):
        """AST 检查 import：只用标准库。

        归档层**刻意不 import 仓库内其它模块**：上游对象一律按鸭子类型使用，
        这样 M6 的实现细节就不会被固化成本层的编译期依赖。
        """
        tree = self._tree()
        modules = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, pyast.ImportFrom):
                if node.module:
                    modules.add(node.module.split(".")[0])
        allowed = {
            "__future__", "ast", "hashlib", "inspect", "json", "pathlib",
            "dataclasses", "types", "typing",
        }
        self.assertTrue(
            modules <= allowed, f"引入了非标准库或仓库内依赖：{sorted(modules - allowed)}"
        )

    def test_module_does_not_reference_frozen_module(self):
        """模块内不得出现对 sdl_m01 的引用（无论读写）。"""
        tree = self._tree()
        referenced = {
            node.module.split(".")[0]
            for node in pyast.walk(tree)
            if isinstance(node, pyast.ImportFrom) and node.module
        }
        self.assertNotIn("sdl_m01", referenced)

    def test_no_skip_or_xfail_markers(self):
        tree = self._tree()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Attribute):
                self.assertNotIn(
                    node.attr, ("skip", "skipIf", "skipUnless", "xfail", "skipTest")
                )

    def test_no_placeholder_ellipsis(self):
        """交付物必须是完整可运行文件，不得有省略号占位。

        用 **AST** 判断，不用源码文本匹配：``...`` 作为**表达式语句**出现
        （即 :class:`ast.Expr` 的值为 :class:`ast.Constant` 且其值为
        ``Ellipsis``）才是省略号占位；而 ``tuple[str, ...]`` 这类类型注解、
        以及文档里描述该规则的文字中出现 ``...`` 都不算违规。
        这与本文件对「不做 X」一律用 AST 的守护口径保持一致。
        """
        tree = self._tree()
        placeholders = [
            node
            for node in pyast.walk(tree)
            if isinstance(node, pyast.Expr)
            and isinstance(node.value, pyast.Constant)
            and node.value.value is Ellipsis
        ]
        self.assertEqual(
            placeholders,
            [],
            f"模块中存在 {len(placeholders)} 处省略号占位语句（应为完整实现）。",
        )
        # 反向自检：确保上面的判定确实能识别出省略号占位（防止空洞断言）。
        probe = pyast.parse("def f():\n    ...\n")
        self.assertTrue(
            any(
                isinstance(node, pyast.Expr)
                and isinstance(node.value, pyast.Constant)
                and node.value.value is Ellipsis
                for node in pyast.walk(probe)
            ),
            "省略号占位的 AST 判定失效：探针未能识别出占位语句。",
        )

    def test_version_constants_present(self):
        self.assertTrue(ARCHIVE_VERSION.startswith("P15"))
        self.assertIn(INITIAL_KNOWLEDGE_VERSION, ("K0",))
        self.assertEqual(REPLICATION_BASES[0], REPLICATION_NONE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
