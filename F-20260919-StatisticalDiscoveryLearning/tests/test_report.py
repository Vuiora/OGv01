"""P13 验收测试：结果三分类、记录与发布。

覆盖三条验收标准：

1. **不显著一律归 ``inconclusive``，不得判为反驳。**
   用真实 M1 + P11/P12 端到端跑出「不显著」的检验，断言其结论为
   ``inconclusive``；并**反向**用定向变异清单逐条确认断言具备甄别力
   （把 ``inconclusive`` 改成 ``refuted`` / ``supported`` 会被本文件捕获）。
   另覆盖「显著但方向未披露」「显著且方向一致」「显著且方向相反」
   「未产出统计结果」四条相邻情形，使边界**互斥且穷尽**。

2. **``status`` 取值严格限于契约四值。**
   逐条断言输入输出只在 ``supported/refuted/inconclusive/failed`` 中取值；
   构造越界取值时结果对象与聚合函数都必须报错；
   登记载荷的键集恒等于契约四项（``status`` / ``metrics`` / ``notes`` /
   ``code_version``），不做任何扩张。

3. **发布只含已登记结果，绝不外泄封存原始记录。**
   真实端到端登记并发布后，逐条核对发布内容都是合法结果载荷；
   用**注入式泄漏夹具**（release 返回值里塞记录正文 / 封存计数 / 他轮绑定）
   确认闸门确实会中止；用 AST 确认模块内不存在任何读取确证记录的路径。

边界守护：不修改 ``sdl_m01/``、只用标准库（AST 检查 import）、
不实现 P14 及之后的内容（AST 检查函数定义与 :data:`NOT_PROVIDED_BY_P13`）、
不使用 skip / xfail、不含令牌键名。

关于「不做 X」的守护方式：本文件一律用 **AST** 判断「函数有没有被定义、
属性有没有被访问」，**不**用源码文本匹配——模块的 docstring 与
``NOT_PROVIDED_BY_P13`` 常量里**必须**写出被禁名字（那是边界声明），
文本匹配会把边界声明本身当成违规。

关于夹具自检：本文件先**实测**上游输出再写断言（``p_value`` / ``threshold`` /
``effect`` 的取值均与实测一致），不靠推测。
"""

import ast as pyast
import hashlib
import json
import pathlib
import tempfile
import unittest

from sdl_m01 import Module01, initialize
from sdl_m01.errors import StateError

from tests.helpers import sample_records, sample_spec

from tests.test_freeze import METRIC, make_hypothesis

from sdl_m06.freeze import bind, build_frozen_plan
from sdl_m06.execute import (
    TestExecution,
    canonical_json,
    execute_family,
    family_test_ids,
)

from sdl_m06.report import (
    AGGREGATION_NOTE,
    CONTRACT_STATUS_NOTE,
    DEFAULT_PREDICTED_DIRECTION,
    DIRECTION_NEGATIVE,
    DIRECTION_POSITIVE,
    DIRECTION_VALUES,
    FACT_INPUT_KEYS,
    FORBIDDEN_CALL_KEYS,
    METRICS_KEYS,
    NON_SIGNIFICANT_NOTE,
    NOT_PROVIDED_BY_P13,
    NO_EVIDENCE_GRADE_NOTE,
    REFUTED_REQUIRES_DIRECTION_NOTE,
    REPORT_VERSION,
    RELEASE_STAMP_FIELDS,
    RESULT_PAYLOAD_FIELDS,
    RESULT_STATUSES,
    SEALED_BOUNDARY_NOTE,
    SEALED_LEAK_KEYS,
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    UNAVAILABLE_IS_FAILED_NOTE,
    Classification,
    ClassificationError,
    HypothesisOutcome,
    RecordReleaseReceipt,
    ReleaseError,
    ReportError,
    ReportInputError,
    RoundClassification,
    aggregate_status,
    build_evaluation_payload,
    check_no_sealed_leak,
    classify_result,
    direction_of,
    is_result_payload,
    record_and_release,
    self_check,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "sdl_m06" / "report.py"
)

#: 备用指标与子群，用于构造「假说 × 指标 × 子群」的多条检验族。
ALT_METRIC = "batch mean absolute loss improvement"
SUBGROUPS = ("overall", "lab-A")

#: 封存原始记录中真实存在的字段名（取自 :mod:`tests.helpers.sample_records`）。
#: 它们的名字必须都在封存黑名单里，否则闸门挡不住真正的泄漏。
REAL_RECORD_FIELDS = (
    "record_id",
    "group_ids",
    "values",
    "units",
    "source",
    "operator_annotation",
    "event_time",
    "available_time",
)


def _stub_test_execution(**overrides):
    """构造一条 :class:`TestExecution`，用于伪造内部不一致的执行结果。"""
    base = dict(
        test_id="t::h1::m::overall",
        hypothesis_id="h1",
        metric="m",
        subgroup="overall",
        null="零假说原文",
        alternative="备择原文",
        method="方法原文",
        p_value=0.5,
        effect=None,
        delta=0.1,
        holm_adjusted=0.5,
        rank=1,
        holm_threshold=0.05,
        significant=False,
        available=True,
        facts={},
        reasons=(),
    )
    base.update(overrides)
    return TestExecution(**base)


class _LeakConfirmer:
    """注入式泄漏夹具：在真实 confirmer 之外刻意掺入封存内容。

    用于验证验收标准③的闸门**真的会中止**，而不是「靠代码写对」。
    每次调用都会真实转发给内层 confirmer，因此登记状态仍然一致。
    """

    def __init__(self, inner, *, leak_into_record=False, leak_into_release=False,
                 foreign_binding=None, raw_record=None):
        self._inner = inner
        self._leak_into_record = leak_into_record
        self._leak_into_release = leak_into_release
        self._foreign_binding = foreign_binding
        self._raw_record = raw_record or sample_records(groups=1, readings=1)[0]

    def record_evaluation(self, binding_id, result):
        value = self._inner.record_evaluation(binding_id, result)
        if self._leak_into_record:
            # 把原始记录正文塞进 metrics —— 最常见的泄漏路径。
            value = dict(value)
            value["metrics"] = dict(value["metrics"])
            value["metrics"]["records_body"] = [dict(self._raw_record)]
        return value

    def release_results(self, binding_id):
        payload = self._inner.release_results(binding_id)
        if self._leak_into_release:
            payload = dict(payload)
            first = dict(payload["evaluations"][0])
            first["snapshot"] = {"sealed_counts": 42}
            payload["evaluations"] = [first] + list(payload["evaluations"][1:])
        if self._foreign_binding is not None:
            payload = dict(payload)
            first = dict(payload["evaluations"][0])
            first["binding_id"] = self._foreign_binding
            payload["evaluations"] = [first] + list(payload["evaluations"][1:])
        return payload


class _EndToEndCase(unittest.TestCase):
    """端到端夹具：真实 M1 证据库 + P11 冻结计划 + P12 执行 + P13 分类发布。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-m06-report-")
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "vault.sqlite3"
        self.tokens = initialize(self.db)
        self.custodian = Module01(self.db, self.tokens["custodian"])
        self.confirmer = Module01(self.db, self.tokens["confirmer"])
        self.spec = sample_spec()
        self.protocol = self.custodian.build(self.spec, sample_records())
        self.refs = self.protocol["resources"]
        self._batch = 1

    # -- 构造 ----------------------------------------------------------

    def plan_of(self, hypotheses=None, *, round_index=1, metrics=None,
                subgroups=None, **kwargs):
        """用默认对齐的声明构造冻结计划。"""
        hypotheses = hypotheses or [make_hypothesis()]
        if not isinstance(hypotheses, (list, tuple)):
            hypotheses = [hypotheses]
        if "declarations" not in kwargs:
            kwargs["declarations"] = {
                item.id: {
                    "metrics": list(metrics or [METRIC]),
                    "primary_metric": METRIC,
                    "effect_threshold": 0.1,
                    "subgroups": list(subgroups or ("overall",)),
                }
                for item in hypotheses
            }
        return build_frozen_plan(
            hypotheses,
            self.protocol["protocol_id"],
            round_index,
            protocol=self.protocol,
            **kwargs,
        )

    def fresh_binding(self, plan):
        """为新一批确证数据建立绑定（每批只能被消费一次）。"""
        self._batch += 1
        purpose = f"C{self._batch}"
        self.custodian.add_confirmation(
            self.protocol["protocol_id"],
            sample_records(groups=3, start=1000 + 10 * self._batch),
            purpose,
        )
        described = self.custodian.describe(self.protocol["protocol_id"])
        return bind(self.confirmer, described["resources"][purpose], plan)

    def run_once(self, plan, trajectories, **kwargs):
        """建立新绑定并执行一次，返回 :class:`FamilyExecution`。"""
        binding = self.fresh_binding(plan)
        result = execute_family(
            self.confirmer,
            binding,
            plan,
            protocol=self.protocol,
            trajectories=trajectories,
            **kwargs,
        )
        return binding, result

    def one_test_id(self, plan):
        """单一检验族里唯一的检验标识。"""
        ids = family_test_ids(plan)
        self.assertEqual(len(ids), 1)
        return ids[0]


# ---------------------------------------------------------------------------
# 验收标准①：不显著 → inconclusive（核心）
# ---------------------------------------------------------------------------


class NonSignificantIsInconclusiveTests(_EndToEndCase):
    """不显著必须归为 ``inconclusive``，不得判为反驳，也不得认定零假说成立。"""

    def test_nonsignificant_is_inconclusive_not_refuted(self):
        """端到端：p 明显大于阈值 → inconclusive（**不是** refuted）。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        # 实测：m=1 时 threshold = α_t = 0.05/2 = 0.025。
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.9, "effect": 1.0}}
        )
        item = execution.results[0]
        self.assertFalse(item.significant)
        self.assertAlmostEqual(item.holm_threshold, 0.025)

        round_result = classify_result(execution, primary_metric=METRIC)
        self.assertEqual(round_result.classifications[0].status, STATUS_INCONCLUSIVE)
        self.assertNotEqual(round_result.classifications[0].status, STATUS_REFUTED)
        self.assertEqual(round_result.aggregate_status, STATUS_INCONCLUSIVE)

    def test_nonsignificant_with_large_effect_still_inconclusive(self):
        """效应量再大，只要不显著就仍是证据不足——不得按效应量「感觉」定结论。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.99, "effect": 999.0}}
        )
        result = classify_result(execution)
        self.assertEqual(result.classifications[0].status, STATUS_INCONCLUSIVE)

    def test_nonsignificant_reasons_carry_the_boundary_note(self):
        """分类依据里必须写明「不显著既不构成反驳、也不构成对零假说的证明」。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(plan, {test_id: {"p_value": 0.8}})
        classification = classify_result(execution).classifications[0]
        joined = " ".join(classification.reasons)
        self.assertIn(NON_SIGNIFICANT_NOTE, joined)
        for keyword in ("反驳", "证明", "零假说"):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, NON_SIGNIFICANT_NOTE)

    def test_nonsignificant_note_rejects_both_misreadings(self):
        """口径文本本身必须同时否掉两种误读：判为反驳、认定零假说成立。"""
        self.assertIn("不得判为反驳", NON_SIGNIFICANT_NOTE)
        self.assertIn("也不构成对零假说的证明", NON_SIGNIFICANT_NOTE)

    def test_holm_adjusted_nonsignificant_differs_from_raw(self):
        """Holm 调整后不显著但原始 p 很小 → 仍 inconclusive（判定用校正后阈值）。"""
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC])
        ids = family_test_ids(plan)
        # 实测：m=2；名次 1 的乘数为 2，故其阈值 = α_t/2 = 0.0125；
        # 名次 2 的乘数为 1，其阈值 = α_t = 0.025。
        _, execution = self.run_once(
            plan,
            {ids[0]: {"p_value": 0.02, "effect": 1.0},
             ids[1]: {"p_value": 0.9, "effect": 1.0}},
        )
        # ids 按名排序，故 ids[0] 对应指标 ALT_METRIC（字典序在前）。
        top = execution.result_of(ids[0])
        self.assertAlmostEqual(top.holm_threshold, 0.0125)
        self.assertFalse(top.significant)
        # 原始 p（0.02）小于 α_t（0.025），若不校正就会误判为显著。
        self.assertLess(top.p_value, execution.alpha)
        result = classify_result(execution)
        self.assertEqual(
            result.classifications[[c.test_id for c in result.classifications].index(ids[0])].status,
            STATUS_INCONCLUSIVE,
        )


class AdjacentCasesTests(_EndToEndCase):
    """四条相邻情形：分类边界必须互斥且穷尽。"""

    def test_significant_matching_direction_is_supported(self):
        """显著且方向与备择一致 → supported。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 2.0}}
        )
        result = classify_result(execution)
        self.assertTrue(execution.results[0].significant)
        self.assertEqual(result.classifications[0].status, STATUS_SUPPORTED)
        self.assertEqual(result.aggregate_status, STATUS_SUPPORTED)

    def test_significant_opposite_direction_is_refuted(self):
        """显著且方向与备择相反 → refuted（这才是反驳的唯一充分条件）。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": -2.0}}
        )
        result = classify_result(execution)
        self.assertEqual(result.classifications[0].status, STATUS_REFUTED)
        self.assertEqual(result.aggregate_status, STATUS_REFUTED)

    def test_significant_without_effect_is_inconclusive(self):
        """显著但未披露效应量 → 不猜方向，归 inconclusive。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(plan, {test_id: {"p_value": 0.001}})
        self.assertIsNone(execution.results[0].effect)
        classification = classify_result(execution).classifications[0]
        self.assertEqual(classification.status, STATUS_INCONCLUSIVE)
        self.assertIsNone(classification.direction)
        self.assertIn(REFUTED_REQUIRES_DIRECTION_NOTE, " ".join(classification.reasons))

    def test_significant_with_zero_effect_is_inconclusive(self):
        """效应量为 0 时没有方向可言 → inconclusive，而不是「中性支持」。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 0.0}}
        )
        classification = classify_result(execution).classifications[0]
        self.assertEqual(classification.status, STATUS_INCONCLUSIVE)
        self.assertIsNone(classification.direction)

    def test_unavailable_is_failed_not_inconclusive(self):
        """未产出统计结果 → failed（没算），严格区别于 inconclusive（不显著）。"""
        plan = self.plan_of()
        _, execution = self.run_once(plan, None)
        self.assertFalse(execution.results[0].available)
        result = classify_result(execution)
        self.assertEqual(result.classifications[0].status, STATUS_FAILED)
        self.assertNotEqual(result.classifications[0].status, STATUS_INCONCLUSIVE)
        self.assertEqual(result.aggregate_status, STATUS_FAILED)
        joined = " ".join(result.classifications[0].reasons)
        self.assertIn(UNAVAILABLE_IS_FAILED_NOTE, joined)

    def test_failed_note_distinguishes_not_computed_from_not_significant(self):
        """口径文本必须把「没算」与「不显著」在外部后果上明确分开。"""
        self.assertIn("没算", UNAVAILABLE_IS_FAILED_NOTE)
        self.assertIn("不显著", UNAVAILABLE_IS_FAILED_NOTE)

    def test_predicted_direction_can_be_negative(self):
        """备择声明负向时，显著的负效应为 supported、正效应为 refuted（方向对称）。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": -2.0}}
        )
        result = classify_result(execution, predicted_direction=DIRECTION_NEGATIVE)
        self.assertEqual(result.classifications[0].status, STATUS_SUPPORTED)

        # 换一个研究轮次（M1 约束：同一轮只能有一个冻结批次）。
        plan2 = self.plan_of(round_index=2)
        test_id2 = self.one_test_id(plan2)
        _, execution2 = self.run_once(
            plan2, {test_id2: {"p_value": 0.001, "effect": 2.0}}
        )
        result2 = classify_result(execution2, predicted_direction=DIRECTION_NEGATIVE)
        self.assertEqual(result2.classifications[0].status, STATUS_REFUTED)

    def test_default_predicted_direction_is_positive(self):
        """缺省方向来自 P11 的备择模板（效应「超过」阈值），故为正向。"""
        self.assertEqual(DEFAULT_PREDICTED_DIRECTION, DIRECTION_POSITIVE)


class DirectionAndAggregationTests(unittest.TestCase):
    """``direction_of`` 与 ``aggregate_status`` 的独立单元测试。"""

    def test_direction_of_signs(self):
        self.assertEqual(direction_of(1.5), DIRECTION_POSITIVE)
        self.assertEqual(direction_of(-0.1), DIRECTION_NEGATIVE)
        self.assertIsNone(direction_of(0.0))
        self.assertIsNone(direction_of(None))

    def test_direction_of_rejects_non_numbers(self):
        for bad in ("1", True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ReportInputError):
                    direction_of(bad)

    def test_direction_values_are_two(self):
        self.assertEqual(set(DIRECTION_VALUES), {DIRECTION_POSITIVE, DIRECTION_NEGATIVE})

    def test_aggregate_all_failed_is_failed(self):
        self.assertEqual(aggregate_status([STATUS_FAILED, STATUS_FAILED]), STATUS_FAILED)

    def test_aggregate_empty_is_failed(self):
        """空输入不是「没问题」，而是「没有可判定的执行结果」。"""
        self.assertEqual(aggregate_status([]), STATUS_FAILED)

    def test_aggregate_supported_only(self):
        self.assertEqual(
            aggregate_status([STATUS_SUPPORTED, STATUS_INCONCLUSIVE]),
            STATUS_SUPPORTED,
        )

    def test_aggregate_refuted_only(self):
        self.assertEqual(
            aggregate_status([STATUS_REFUTED, STATUS_FAILED]), STATUS_REFUTED
        )

    def test_aggregate_conflict_is_inconclusive(self):
        """方向冲突时不得择优、也不得多数表决出方向性结论。"""
        self.assertEqual(
            aggregate_status([STATUS_SUPPORTED, STATUS_REFUTED]), STATUS_INCONCLUSIVE
        )

    def test_aggregate_inconclusive_only(self):
        self.assertEqual(
            aggregate_status([STATUS_INCONCLUSIVE, STATUS_INCONCLUSIVE]),
            STATUS_INCONCLUSIVE,
        )

    def test_aggregate_rejects_invalid_status(self):
        for bad in ("SUPPORTED", "支持", "", None, 1):
            with self.subTest(bad=bad):
                with self.assertRaises(ReportError):
                    aggregate_status([bad])

    def test_aggregate_rejects_non_sequence(self):
        with self.assertRaises(ReportInputError):
            aggregate_status("supported")

    def test_aggregation_note_documents_conflict_rule(self):
        self.assertIn("冲突", AGGREGATION_NOTE)
        self.assertIn("多数表决", AGGREGATION_NOTE)


class ClassifyResultInputTests(unittest.TestCase):
    """``classify_result`` 的三种输入形态与输入校验。"""

    def test_fact_mapping_supported(self):
        result = classify_result(
            {"p_value": 0.001, "threshold": 0.05, "effect": 3.0}
        )
        self.assertIsInstance(result, Classification)
        self.assertEqual(result.status, STATUS_SUPPORTED)

    def test_fact_mapping_inconclusive(self):
        result = classify_result({"p_value": 0.5, "threshold": 0.05, "effect": 3.0})
        self.assertEqual(result.status, STATUS_INCONCLUSIVE)

    def test_fact_mapping_unavailable(self):
        result = classify_result({"available": False})
        self.assertEqual(result.status, STATUS_FAILED)

    def test_fact_mapping_rejects_unknown_keys(self):
        with self.assertRaises(ReportInputError):
            classify_result({"p_value": 0.1, "threshold": 0.05, "surprise": 1})

    def test_fact_keys_whitelist_is_small(self):
        self.assertEqual(
            FACT_INPUT_KEYS,
            {"test_id", "hypothesis_id", "metric", "subgroup", "available",
             "p_value", "threshold", "effect"},
        )

    def test_available_false_with_p_value_is_contradiction(self):
        with self.assertRaises(ClassificationError):
            classify_result({"available": False, "p_value": 0.1})

    def test_available_true_without_p_value_raises(self):
        with self.assertRaises(ClassificationError):
            classify_result({"available": True, "threshold": 0.05})

    def test_available_true_without_threshold_raises(self):
        """缺阈值时必须拒绝，而不是用别的数值代替（不猜测、不插补）。"""
        with self.assertRaises(ClassificationError):
            classify_result({"available": True, "p_value": 0.01})

    def test_rejects_out_of_range_p_value(self):
        for bad in (-0.1, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ReportInputError):
                    classify_result({"p_value": bad, "threshold": 0.05})

    def test_rejects_invalid_threshold(self):
        for bad in (0.0, -0.5, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ReportInputError):
                    classify_result({"p_value": 0.01, "threshold": bad})

    def test_rejects_invalid_predicted_direction(self):
        with self.assertRaises(ReportInputError):
            classify_result(
                {"p_value": 0.01, "threshold": 0.05},
                predicted_direction="both",
            )

    def test_rejects_unsupported_subject_type(self):
        with self.assertRaises(ReportInputError):
            classify_result([{"p_value": 0.1}])

    def test_rejects_forbidden_call_keys(self):
        """记录类关键字必须被拒绝：本层不吃原始记录。"""
        for name in ("records", "dataset", "raw", "read_dataset"):
            with self.subTest(name=name):
                self.assertIn(name, FORBIDDEN_CALL_KEYS)

    def test_rejects_token_keys(self):
        with self.assertRaises(ReportInputError):
            classify_result({"p_value": 0.1, "threshold": 0.05, "role_token": "x"})


class TestExecutionConsistencyTests(unittest.TestCase):
    """由 P12 的执行记录分类时，内部不一致必须被拒绝。"""

    def test_significant_flag_mismatch_is_rejected(self):
        """机械判定与「p ≤ 阈值」复算不一致 → 拒绝在其上给出结论。"""
        item = _stub_test_execution(p_value=0.5, holm_threshold=0.05, significant=True)
        with self.assertRaises(ClassificationError):
            classify_result(item)

    def test_missing_threshold_on_available_is_rejected(self):
        item = _stub_test_execution(holm_threshold=None, significant=None)
        with self.assertRaises(ClassificationError):
            classify_result(item)

    def test_unavailable_record_is_failed(self):
        item = _stub_test_execution(
            p_value=None, holm_threshold=None, significant=None, available=False
        )
        self.assertEqual(classify_result(item).status, STATUS_FAILED)

    def test_consistent_record_is_classified(self):
        item = _stub_test_execution(p_value=0.001, holm_threshold=0.05,
                                    significant=True, effect=1.0)
        self.assertEqual(classify_result(item).status, STATUS_SUPPORTED)


# ---------------------------------------------------------------------------
# 验收标准②：status 取值限于契约四值
# ---------------------------------------------------------------------------


class ContractStatusTests(unittest.TestCase):
    """``status`` 只能取契约四值，且载荷形状与契约逐项对应。"""

    def test_result_statuses_exact(self):
        self.assertEqual(
            RESULT_STATUSES, ("supported", "refuted", "inconclusive", "failed")
        )
        self.assertEqual(len(set(RESULT_STATUSES)), 4)

    def test_contract_note_names_all_four(self):
        for value in RESULT_STATUSES:
            with self.subTest(value=value):
                self.assertIn(value, CONTRACT_STATUS_NOTE)

    def test_classification_rejects_out_of_contract_status(self):
        for bad in ("SUPPORTED", "支持", "benefit", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ClassificationError):
                    Classification(
                        test_id="t", hypothesis_id="h", status=bad, available=True
                    )

    def test_classification_rejects_unavailable_with_non_failed(self):
        with self.assertRaises(ClassificationError):
            Classification(
                test_id="t", hypothesis_id="h", status=STATUS_INCONCLUSIVE,
                available=False,
            )

    def test_classification_rejects_available_with_failed(self):
        with self.assertRaises(ClassificationError):
            Classification(
                test_id="t", hypothesis_id="h", status=STATUS_FAILED, available=True
            )

    def test_classification_rejects_bad_direction(self):
        with self.assertRaises(ClassificationError):
            Classification(
                test_id="t", hypothesis_id="h", status=STATUS_SUPPORTED,
                available=True, direction="up",
            )

    def test_hypothesis_outcome_rejects_bad_status(self):
        with self.assertRaises(ClassificationError):
            HypothesisOutcome(hypothesis_id="h", status="maybe")

    def test_round_classification_rejects_bad_aggregate(self):
        with self.assertRaises(ClassificationError):
            RoundClassification(
                binding_id="b", round_index=1, alpha=0.05,
                aggregate_status="great", classifications=(), outcomes=(),
            )

    def test_all_statuses_produced_are_contract_values(self):
        """一次含四种情形的分类，输出取值必须全部落在契约集合内。"""
        plan_supported = {"p_value": 0.001, "threshold": 0.05, "effect": 1.0}
        plan_refuted = {"p_value": 0.001, "threshold": 0.05, "effect": -1.0}
        plan_inconcl = {"p_value": 0.9, "threshold": 0.05, "effect": 1.0}
        plan_failed = {"available": False}
        for facts in (plan_supported, plan_refuted, plan_inconcl, plan_failed):
            with self.subTest(facts=facts):
                status = classify_result(facts).status
                self.assertIn(status, RESULT_STATUSES)


class PayloadShapeTests(_EndToEndCase):
    """登记载荷的键集与契约逐项对应。"""

    def _round_classification(self):
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 2.0}}
        )
        return classify_result(execution, primary_metric=METRIC)

    def test_payload_fields_exact(self):
        payload = build_evaluation_payload(self._round_classification())
        self.assertEqual(set(payload), set(RESULT_PAYLOAD_FIELDS))
        self.assertEqual(set(payload), {"status", "metrics", "notes", "code_version"})

    def test_payload_types_match_m1_requirements(self):
        payload = build_evaluation_payload(self._round_classification())
        self.assertIsInstance(payload["status"], str)
        self.assertIsInstance(payload["metrics"], dict)
        self.assertIsInstance(payload["notes"], str)
        self.assertIsInstance(payload["code_version"], str)
        self.assertEqual(payload["code_version"], REPORT_VERSION)

    def test_payload_metrics_top_level_keys_in_whitelist(self):
        payload = build_evaluation_payload(self._round_classification())
        unknown = set(payload["metrics"]) - METRICS_KEYS
        self.assertEqual(unknown, set())

    def test_payload_notes_keep_mandatory_boundary_text(self):
        """自定义 notes 不能删掉核心口径。"""
        payload = build_evaluation_payload(
            self._round_classification(), notes="复核备注"
        )
        self.assertIn("复核备注", payload["notes"])
        self.assertIn("不显著一律归为 inconclusive", payload["notes"])

    def test_payload_rejects_single_classification(self):
        """单条检验不足以构成一次确证的登记对象。"""
        classification = classify_result({"p_value": 0.1, "threshold": 0.05})
        with self.assertRaises(ReportInputError):
            build_evaluation_payload(classification)

    def test_payload_rejects_blank_code_version(self):
        with self.assertRaises(ReportInputError):
            build_evaluation_payload(self._round_classification(), code_version="  ")

    def test_hypothesis_outcome_payload(self):
        round_result = self._round_classification()
        outcome = round_result.outcomes[0]
        payload = build_evaluation_payload(outcome)
        self.assertEqual(set(payload), set(RESULT_PAYLOAD_FIELDS))
        self.assertEqual(payload["status"], outcome.status)

    def test_no_evidence_grade_note_present(self):
        self.assertIn("E0/E1/E2", NO_EVIDENCE_GRADE_NOTE)
        self.assertIn("因果", NO_EVIDENCE_GRADE_NOTE)

    def test_payload_rejects_sealed_field_nested_under_allowed_key(self):
        """顶层键合法但里层藏了原始记录字段 → 必须被深度扫描拦下。

        这是载荷封存扫描**真正的用武之地**：顶层键白名单（``METRICS_KEYS``）
        只看得见第一层，把 ``values`` 藏在 ``hypotheses`` 里就需要递归扫描。
        若实现里去掉 ``build_evaluation_payload`` 的封存扫描，本用例会失败。
        """
        outcome = HypothesisOutcome(
            hypothesis_id="h1",
            status=STATUS_SUPPORTED,
            metrics={"hypotheses": {"h1": {"values": [1.0, 2.0, 3.0]}}},
        )
        with self.assertRaises(ReleaseError):
            build_evaluation_payload(outcome)

    def test_payload_rejects_record_body_list_nested_deeply(self):
        """更深一层的记录正文同样要被拦下（扫描必须递归到底）。"""
        outcome = HypothesisOutcome(
            hypothesis_id="h1",
            status=STATUS_SUPPORTED,
            metrics={
                "hypotheses": {
                    "h1": {"detail": [{"operator_annotation": "private-observation"}]}
                }
            },
        )
        with self.assertRaises(ReleaseError):
            build_evaluation_payload(outcome)

    def test_payload_rejects_unknown_top_level_metric_key(self):
        outcome = HypothesisOutcome(
            hypothesis_id="h1",
            status=STATUS_SUPPORTED,
            metrics={"raw_records_count": 3},
        )
        with self.assertRaises(ReportInputError):
            build_evaluation_payload(outcome)


# ---------------------------------------------------------------------------
# 验收标准③：发布只含已登记结果，绝不外泄封存原始记录
# ---------------------------------------------------------------------------


class SealedLeakGateTests(unittest.TestCase):
    """封存闸门：深度键名扫描。"""

    def test_real_record_field_names_are_all_blocked(self):
        """夹具里真实存在的原始记录字段名必须全部在黑名单内。"""
        for name in REAL_RECORD_FIELDS:
            with self.subTest(name=name):
                self.assertIn(name, SEALED_LEAK_KEYS)

    def test_clean_payload_passes(self):
        check_no_sealed_leak(
            {"status": "supported", "metrics": {"test_count": 1}, "notes": "ok"},
            "载荷",
        )

    def test_top_level_leak_is_rejected(self):
        with self.assertRaises(ReleaseError):
            check_no_sealed_leak({"values": [1, 2, 3]}, "载荷")

    def test_nested_leak_is_rejected(self):
        """泄漏可能藏在深处，闸门必须递归到底。"""
        with self.assertRaises(ReleaseError):
            check_no_sealed_leak(
                {"metrics": {"hypotheses": {"h1": {"records": [1]}}}}, "载荷"
            )

    def test_leak_inside_sequence_is_rejected(self):
        with self.assertRaises(ReleaseError):
            check_no_sealed_leak(
                {"items": [{"ok": 1}, {"_quality": {"flagged": 2}}]}, "载荷"
            )

    def test_each_blocked_key_is_rejected(self):
        for key in sorted(SEALED_LEAK_KEYS):
            with self.subTest(key=key):
                with self.assertRaises(ReleaseError):
                    check_no_sealed_leak({"outer": {key: 1}}, "载荷")

    def test_values_in_string_are_not_false_positive(self):
        """只扫键名，不按文本猜疑——中文说明里出现同名词不应误报。"""
        check_no_sealed_leak({"notes": "本记录不含 values 与 record_id 等内容"}, "载荷")

    def test_sealed_boundary_note_mentions_raw_records(self):
        self.assertIn("原始记录", SEALED_BOUNDARY_NOTE)
        self.assertIn("发布", SEALED_BOUNDARY_NOTE)


class IsResultPayloadTests(unittest.TestCase):
    """``is_result_payload``：判断一个对象是否为合法结果载荷。"""

    def _payload(self):
        return {
            "status": "supported",
            "metrics": {"test_count": 1},
            "notes": "ok",
            "code_version": "P13-v1.0",
        }

    def test_accepts_contract_payload(self):
        self.assertTrue(is_result_payload(self._payload()))

    def test_accepts_with_release_stamps(self):
        stamped = dict(self._payload())
        stamped["binding_id"] = "b1"
        stamped["recorded_by"] = "external_confirmation_executor"
        self.assertTrue(is_result_payload(stamped))

    def test_rejects_missing_field(self):
        payload = self._payload()
        payload.pop("notes")
        self.assertFalse(is_result_payload(payload))

    def test_rejects_extra_field(self):
        payload = self._payload()
        payload["extra"] = 1
        self.assertFalse(is_result_payload(payload))

    def test_rejects_invalid_status(self):
        payload = self._payload()
        payload["status"] = "great"
        self.assertFalse(is_result_payload(payload))

    def test_rejects_non_mapping(self):
        for bad in ([], "supported", None, 1):
            with self.subTest(bad=bad):
                self.assertFalse(is_result_payload(bad))

    def test_release_stamp_fields_are_exactly_two(self):
        self.assertEqual(RELEASE_STAMP_FIELDS, {"binding_id", "recorded_by"})


class RecordAndReleaseTests(_EndToEndCase):
    """端到端登记与发布。"""

    def _subject_and_binding(self, effect=2.0, p_value=0.001):
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        binding, execution = self.run_once(
            plan, {test_id: {"p_value": p_value, "effect": effect}}
        )
        return binding, classify_result(execution, primary_metric=METRIC)

    def test_record_and_release_end_to_end(self):
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        self.assertIsInstance(receipt, RecordReleaseReceipt)
        self.assertEqual(receipt.binding_id, binding["binding_id"])
        self.assertEqual(receipt.status, STATUS_SUPPORTED)
        self.assertEqual(receipt.code_version, REPORT_VERSION)

    def test_published_content_is_only_result_payloads(self):
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        self.assertEqual(len(receipt.published), 1)
        for index, item in enumerate(receipt.published):
            with self.subTest(index=index):
                self.assertTrue(is_result_payload(item))

    def test_published_contains_this_registration(self):
        """本次登记的结果必须原样出现在发布内容中（键集核对）。"""
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        self.assertEqual(set(receipt.published[0]),
                         set(RESULT_PAYLOAD_FIELDS) | RELEASE_STAMP_FIELDS)

    def test_published_has_no_raw_record_fields(self):
        """发布内容的任意层级都不得出现原始记录字段名。"""
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        for index, item in enumerate(receipt.published):
            with self.subTest(index=index):
                check_no_sealed_leak(item, f"已发布内容第 {index} 条")

    def test_public_view_strips_stamps(self):
        """对外的 public 视图只含契约四字段，不含证据库印记字段。"""
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        public = receipt.public
        self.assertEqual(set(public), {"binding_id", "results"})
        self.assertEqual(set(public["results"][0]), set(RESULT_PAYLOAD_FIELDS))

    def test_explorer_can_read_released_only_after_release(self):
        """发布前探索侧不可读；发布后可读，且读到的仍是结果载荷。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        binding, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 2.0}}
        )
        subject = classify_result(execution, primary_metric=METRIC)
        explorer = Module01(self.db, self.tokens["explorer"])
        with self.assertRaises(Exception):
            explorer.results(binding["binding_id"])
        record_and_release(self.confirmer, binding, subject)
        released = explorer.results(binding["binding_id"])
        self.assertEqual(released["binding_id"], binding["binding_id"])
        for item in released["evaluations"]:
            self.assertTrue(is_result_payload(item))

    def test_receipt_reports_payload_digest(self):
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        # public 视图即契约四字段的普通副本，可直接参与规范序列化。
        plain = receipt.public["results"][0]
        expected = hashlib.sha256(canonical_json(plain).encode("utf-8")).hexdigest()
        self.assertEqual(receipt.payload_digest, expected)

    def test_payload_is_frozen_view(self):
        """载荷以只读视图暴露，调用方改不动已登记内容。"""
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        with self.assertRaises(TypeError):
            receipt.payload["status"] = "refuted"

    def test_cannot_release_without_record(self):
        """未登记即发布：M1 本身就会拒绝（本层不绕过该约束）。"""
        binding, subject = self._subject_and_binding()
        with self.assertRaises(StateError):
            self.confirmer.release_results(binding["binding_id"])

    def test_rejects_forbidden_binding_key(self):
        """绑定描述里夹带原始记录字段 → 封存闸门拦下（ReleaseError）。"""
        binding, subject = self._subject_and_binding()
        with self.assertRaises(ReleaseError):
            record_and_release(
                self.confirmer, {"binding_id": binding["binding_id"], "raw": []}, subject
            )

    def test_rejects_missing_binding_id(self):
        _, subject = self._subject_and_binding()
        with self.assertRaises(ReportInputError):
            record_and_release(self.confirmer, {}, subject)

    def test_rejects_confirmer_without_capabilities(self):
        binding, subject = self._subject_and_binding()
        with self.assertRaises(ReportInputError):
            record_and_release(object(), binding, subject)

    def test_receipt_digest_reproducible(self):
        binding, subject = self._subject_and_binding()
        receipt = record_and_release(self.confirmer, binding, subject)
        self.assertEqual(
            receipt.content_digest(),
            hashlib.sha256(receipt.canonical_json().encode("utf-8")).hexdigest(),
        )


class LeakInjectionTests(_EndToEndCase):
    """注入式泄漏夹具：闸门必须真的中止，而不是「靠代码写对」。"""

    def _binding_and_subject(self):
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        binding, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 2.0}}
        )
        return binding, classify_result(execution, primary_metric=METRIC)

    def test_leak_into_recorded_result_is_rejected(self):
        """证据库返回值被塞入记录正文 → 拒绝继续发布。"""
        binding, subject = self._binding_and_subject()
        leaking = _LeakConfirmer(self.confirmer, leak_into_record=True)
        with self.assertRaises(ReleaseError):
            record_and_release(leaking, binding, subject)

    def test_leak_into_released_content_is_rejected(self):
        """发布内容被塞入封存计数 → 拒绝把它交给调用方。"""
        binding, subject = self._binding_and_subject()
        leaking = _LeakConfirmer(self.confirmer, leak_into_release=True)
        with self.assertRaises(ReleaseError):
            record_and_release(leaking, binding, subject)

    def test_non_result_entry_in_release_is_rejected(self):
        """发布内容里出现非结果载荷形态 → 拒绝（发布只含已登记结果）。"""

        class _NonResultConfirmer:
            def __init__(self, inner):
                self._inner = inner

            def record_evaluation(self, binding_id, result):
                return self._inner.record_evaluation(binding_id, result)

            def release_results(self, binding_id):
                payload = self._inner.release_results(binding_id)
                return {
                    "binding_id": binding_id,
                    "evaluations": list(payload["evaluations"]) + ["裸字符串"],
                }

        binding, subject = self._binding_and_subject()
        with self.assertRaises(ReleaseError):
            record_and_release(
                _NonResultConfirmer(self.confirmer), binding, subject
            )

    def test_foreign_binding_in_release_is_rejected(self):
        """发布内容混入他轮绑定的结果 → 拒绝。"""
        binding, subject = self._binding_and_subject()
        leaking = _LeakConfirmer(self.confirmer, foreign_binding="binding-other")
        with self.assertRaises(ReleaseError):
            record_and_release(leaking, binding, subject)

    def test_missing_registration_in_release_is_rejected(self):
        """发布内容里找不到本次登记结果 → 拒绝。"""

        class _DroppingConfirmer:
            def __init__(self, inner):
                self._inner = inner

            def record_evaluation(self, binding_id, result):
                return self._inner.record_evaluation(binding_id, result)

            def release_results(self, binding_id):
                # 先把真实发布状态置位，再返回空列表——模拟「登记被丢弃」。
                self._inner.release_results(binding_id)
                return {"binding_id": binding_id, "evaluations": []}

        binding, subject = self._binding_and_subject()
        with self.assertRaises(ReleaseError):
            record_and_release(
                _DroppingConfirmer(self.confirmer), binding, subject
            )


class RoundClassificationObjectTests(_EndToEndCase):
    """整轮分类对象的结构与摘要。"""

    def _round(self, metrics=None, trajectories=None):
        plan = self.plan_of(metrics=metrics)
        ids = family_test_ids(plan)
        if trajectories is None:
            trajectories = {
                ids[0]: {"p_value": 0.001, "effect": 2.0}
            }
        _, execution = self.run_once(plan, trajectories)
        return classify_result(execution, primary_metric=METRIC)

    def test_counts_are_consistent(self):
        round_result = self._round()
        self.assertEqual(round_result.test_count, len(round_result.classifications))
        self.assertEqual(
            round_result.supported_count
            + round_result.refuted_count
            + round_result.inconclusive_count
            + round_result.failed_count,
            round_result.test_count,
        )

    def test_statuses_match_aggregate_membership(self):
        round_result = self._round()
        for status in round_result.statuses:
            self.assertIn(status, RESULT_STATUSES)

    def test_canonical_json_and_digest_reproducible(self):
        round_result = self._round()
        again = json.loads(round_result.canonical_json())
        self.assertEqual(again, round_result.to_dict())
        self.assertEqual(
            round_result.content_digest(),
            hashlib.sha256(round_result.canonical_json().encode("utf-8")).hexdigest(),
        )

    def test_outcome_of_unknown_hypothesis_raises(self):
        round_result = self._round()
        with self.assertRaises(ReportInputError):
            round_result.outcome_of("h-nonexistent")

    def test_scope_degrades_when_primary_metric_absent(self):
        """未登记主要指标检验时退化为全部检验，并把 scope 与退化提示标出来。"""
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 2.0}}
        )
        # 计划只登记了 METRIC 的检验，却要求按 ALT_METRIC 聚合 → 必须退化。
        round_result = classify_result(execution, primary_metric=ALT_METRIC)
        outcome = round_result.outcomes[0]
        self.assertEqual(outcome.scope, "all_tests")
        self.assertEqual(outcome.metrics["primary_metric"], ALT_METRIC)
        self.assertTrue(
            any("退化" in note for note in outcome.notes),
            f"退化时必须在 notes 中写明，实际 notes={outcome.notes}",
        )

    def test_primary_metric_scope_selected(self):
        round_result = self._round(metrics=[METRIC])
        outcome = round_result.outcomes[0]
        self.assertEqual(outcome.scope, "primary_metric")

    def test_execution_binding_and_round_are_carried(self):
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        binding, execution = self.run_once(
            plan, {test_id: {"p_value": 0.001, "effect": 2.0}}
        )
        round_result = classify_result(execution)
        self.assertEqual(round_result.binding_id, binding["binding_id"])
        self.assertEqual(round_result.round_index, 1)
        self.assertAlmostEqual(round_result.alpha, execution.alpha)

    def test_alpha_override_is_validated(self):
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(plan, {test_id: {"p_value": 0.001, "effect": 2.0}})
        with self.assertRaises(ReportInputError):
            classify_result(execution, alpha=1.5)

    def test_round_index_must_be_integer(self):
        plan = self.plan_of()
        test_id = self.one_test_id(plan)
        _, execution = self.run_once(plan, {test_id: {"p_value": 0.001, "effect": 2.0}})
        with self.assertRaises(ReportInputError):
            classify_result(execution, round_index=1.0)


class DirectionalMutationGuardTests(unittest.TestCase):
    """定向变异守护：确认核心断言对「把 inconclusive 改成方向性结论」具备甄别力。

    这些用例**不**修改被测模块，而是把「错误分类」显式构造出来并断言它
    **不被**本模块的判据接受——从而证明验收标准①的断言不是空洞断言。
    """

    def test_wrong_refuted_construction_is_rejected_by_invariant(self):
        """若有人把不显著硬写成 refuted，结果对象自身的互斥约束会拦下它。"""
        with self.assertRaises(ClassificationError):
            Classification(
                test_id="t", hypothesis_id="h", status=STATUS_REFUTED,
                available=False,
            )

    def test_nonsignificant_must_not_be_directional(self):
        """不显著的事实经分类后必为 inconclusive，且不是方向性结论。"""
        classification = classify_result(
            {"p_value": 0.9, "threshold": 0.05, "effect": 5.0}
        )
        self.assertFalse(classification.directional)
        self.assertEqual(classification.status, STATUS_INCONCLUSIVE)

    def test_significant_requires_direction_for_directional_verdict(self):
        """仅「显著」不足以产生方向性结论——这是反驳门槛的最低要求。"""
        classification = classify_result(
            {"p_value": 0.001, "threshold": 0.05}
        )
        self.assertFalse(classification.directional)
        self.assertIsNone(classification.direction)

    def test_direction_change_flips_verdict(self):
        """方向符号是 verdict 的唯一决定因素（在显著的前提下）——反向即反转。"""
        positive = classify_result(
            {"p_value": 0.001, "threshold": 0.05, "effect": 0.5}
        )
        negative = classify_result(
            {"p_value": 0.001, "threshold": 0.05, "effect": -0.5}
        )
        self.assertEqual(positive.status, STATUS_SUPPORTED)
        self.assertEqual(negative.status, STATUS_REFUTED)


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

    def test_p13_implements_exactly_its_declared_entrypoints(self):
        defined = self._defined_names(self._tree())
        for name in ("classify_result", "record_and_release"):
            with self.subTest(name=name):
                self.assertIn(name, defined)

    def test_does_not_define_later_stage_entrypoints(self):
        """AST 检查：不定义 P14 及之后的任何入口。"""
        defined = self._defined_names(self._tree())
        overlaps = sorted(defined & set(NOT_PROVIDED_BY_P13))
        self.assertEqual(overlaps, [], f"越界实现：{overlaps}")

    def test_not_provided_list_covers_neighbouring_stages(self):
        for name in (
            "divergence", "acquisition_plan",
            "archive_round", "update_knowledge_version", "revise_or_retire",
            "execute_family", "holm_adjust", "alpha_for_round",
            "build_frozen_plan", "bind",
        ):
            with self.subTest(name=name):
                self.assertIn(name, NOT_PROVIDED_BY_P13)

    def test_self_check_passes(self):
        report = self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["defined_forbidden"], ())
        self.assertEqual(report["accessed_forbidden"], ())

    def test_no_evidence_read_access_paths(self):
        """AST 检查：模块内不存在读取确证记录的入口。"""
        tree = self._tree()
        defined = self._defined_names(tree)
        for name in ("read_dataset", "read_records", "load_snapshot", "open_vault"):
            with self.subTest(name=name):
                self.assertNotIn(name, defined)
        accessed = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Attribute):
                accessed.add(node.attr)
            elif isinstance(node, pyast.Name):
                accessed.add(node.id)
        for name in ("sqlite3", "load_snapshot", "read_dataset", "consume_confirmation"):
            with self.subTest(name=name):
                self.assertNotIn(name, accessed)

    def test_does_not_call_consume_or_read(self):
        """AST 检查：不调用任何数据读取与消费接口。"""
        tree = self._tree()
        called = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Attribute):
                called.add(node.func.attr)
        for name in ("consume_confirmation", "read_dataset", "read_records",
                     "quality", "load_snapshot"):
            with self.subTest(name=name):
                self.assertNotIn(name, called)

    def test_only_standard_library_imports(self):
        """AST 检查 import：只用标准库与同仓库 sdl_m06。"""
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
            "__future__", "ast", "hashlib", "inspect", "json", "math",
            "pathlib", "dataclasses", "types", "typing", "sdl_m06",
        }
        self.assertTrue(
            modules <= allowed, f"引入了非标准库依赖：{sorted(modules - allowed)}"
        )

    def test_module_does_not_modify_frozen_module(self):
        """模块内不得出现对 sdl_m01 的引用（无论读写）。"""
        tree = self._tree()
        self.assertNotIn("sdl_m01", {
            node.module.split(".")[0]
            for node in pyast.walk(tree)
            if isinstance(node, pyast.ImportFrom) and node.module
        })

    def test_no_skip_or_xfail_markers(self):
        tree = self._tree()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Attribute):
                self.assertNotIn(
                    node.attr, ("skip", "skipIf", "skipUnless", "xfail", "skipTest")
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
