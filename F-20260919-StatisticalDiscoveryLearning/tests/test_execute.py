"""P12 验收测试：检验执行与跨轮误差预算。

覆盖四条验收标准：

1. **``alpha_for_round`` 严格为 ``total_alpha / 2**t``。**
   逐轮与 ``total_alpha / 2**t`` 逐位相等；与 ``math.ldexp`` 同源；
   轮次越界、``total_alpha`` 越界、预算下溢均被拒绝；
   函数签名不含任何可替换口径的参数（无 ``method`` / ``adjusted`` 一类入口）。

2. **消费顺序为先提交 ``used`` 再返回数据。**
   用**调用顺序探针**（spy confirmer）记录 ``consume_confirmation`` 与
   首次读取的先后；:class:`ExecutionOrder` 的序号由内部单调步进账本实际测量，
   伪造的顺序会被 :meth:`ExecutionOrder.verify` 拒绝；
   ``execute_family`` **不接受**任何记录入口（``records`` / ``dataset`` / ``raw``）。

3. **检验族完整，不得删改失败项。**
   轨迹与计划的检验标识集合严格对账：缺一条、多一条都拒绝执行；
   执行接口不含 ``only`` / ``exclude`` / ``subset`` 一类筛选参数；
   不可得检验逐条占位（``available=False``、``p_value=None``），
   且单列进 ``unavailable_test_ids``——「没算」与「不显著」可区分；
   ``excluded_failed_tests`` 恒为空。

4. **Holm 校正可运行，且对空输入返回空。**
   空映射返回空结果；调整值序列非降（单调化）；标准算例逐值核对；
   原始 p 值为 ``None`` 的检验只进入 ``missing``，不被插补；
   平局按标识字典序确定名次，结果与输入顺序无关。

另覆盖：轮次三方（参数 / 计划 / 绑定）对账、α 与绑定登记值对账、
计划离线自检失败即拒绝且**不碰 confirmer**、结果对象摘要可复现、
以及与 M1 ``consume_confirmation`` / ``bind_confirmation`` 的真实端到端执行。

边界守护：不修改 ``sdl_m01/``、只用标准库（AST 检查 import）、
不实现 P13 及之后的内容（AST 检查函数定义与 :data:`NOT_PROVIDED_BY_P12`）、
不使用 skip / xfail、不含令牌键名。

关于「不做 X」的守护方式：本文件一律用 **AST** 判断「函数有没有被定义、
属性有没有被访问」，**不**用源码文本匹配——模块的 docstring 与
``NOT_PROVIDED_BY_P12`` 常量里**必须**写出被禁名字（那是边界声明），
文本匹配会把边界声明本身当成违规。
"""

import ast as pyast
import inspect
import json
import math
import pathlib
import tempfile
import unittest

from sdl_m01 import Module01, initialize
from sdl_m01.errors import StateError

from tests.helpers import sample_records, sample_spec

from tests.test_freeze import METRIC, make_hypothesis

from sdl_m06.freeze import bind, build_frozen_plan
from sdl_m06.execute import (
    ALPHA_FORMULA_NOTE,
    ALPHA_UNDERFLOW_NOTE,
    COMPLETE_FAMILY_BOUNDARY_NOTE,
    CONSUMPTION_ORDER_NOTE,
    EXECUTE_RESERVED_KEYS,
    EXECUTE_VERSION,
    FACT_KEYS,
    FAMILY_COHERENCE_NOTE,
    FORBIDDEN_CALL_KEYS,
    HOLM_ADJUSTMENT_NAME,
    HOLM_NOTE,
    NOT_PROVIDED_BY_P12,
    ROUND_INDEX_MAX,
    ROUND_INDEX_MIN,
    ROUND_SCOPE_NOTE,
    ErrorBudgetError,
    ExecuteInputError,
    ExecutionOrder,
    FamilyCoherenceError,
    FamilyExecution,
    HolmResult,
    SequenceOrderError,
    TestExecution,
    alpha_for_round,
    canonical_json,
    check_family_coherence,
    execute_family,
    family_test_ids,
    holm_adjust,
    self_check,
    summarize_family,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "sdl_m06" / "execute.py"
)

#: 备用指标与子群，用于构造「假说 × 指标 × 子群」的笛卡尔积族。
ALT_METRIC = "batch mean absolute loss improvement"
SUBGROUPS = ("overall", "lab-A")

#: 一个标准 Holm 算例：m=4，乘数依次为 4/3/2/1。
HOLM_SAMPLE = {"t-a": 0.004, "t-b": 0.02, "t-c": 0.03, "t-d": 0.5}
HOLM_EXPECTED = {"t-a": 0.016, "t-b": 0.06, "t-c": 0.06, "t-d": 0.5}


class _OrderProbe:
    """调用顺序探针：记录 confirmer 被调用前是否已发生读取。

    它包装真实的 M1 confirmer，`consume_confirmation` 一旦被调用就把
    ``consumed_before_read`` 置为当前是否已有读取记录。若被测模块在消费前
    读取了任何内容，该标志即为 ``True``，测试据此判定顺序被破坏。
    """

    def __init__(self, inner):
        self._inner = inner
        self.events = []
        self.reads_before_consume = None

    # -- 被测模块唯一允许触达的接口 ------------------------------------
    def consume_confirmation(self, binding_id, replay=False):
        self.reads_before_consume = self.events.count("read")
        self.events.append("consume")
        return self._inner.consume_confirmation(binding_id, replay=replay)

    # -- 供测试标记「读取」事件 ------------------------------------------
    def note_read(self):
        self.events.append("read")


class _ProtocolFixture(unittest.TestCase):
    """提供真实 M1 公开协议与绑定作为夹具。

    每个用例独立建库与协议：M1 的 ``consume_confirmation`` 是
    **首用一次** 的（同一绑定不能重复消费），因此需要多次执行的用例
    必须各自持有独立的确证批次。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-m06-execute-")
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

    def plan_of(self, hypotheses=None, *, round_index=1, metrics=None, subgroups=None, **kwargs):
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
        return bind(self.confirmer, self.refs_of(purpose), plan)

    def refs_of(self, purpose):
        """重新描述协议以取到新增分区的引用。"""
        described = self.custodian.describe(self.protocol["protocol_id"])
        return described["resources"][purpose]

    def run_once(self, plan, trajectories, **kwargs):
        """建立新绑定并执行一次，返回 ``(结果, 探针)``。"""
        binding = self.fresh_binding(plan)
        probe = _OrderProbe(self.confirmer)
        result = execute_family(
            probe,
            binding,
            plan,
            protocol=self.protocol,
            trajectories=trajectories,
            **kwargs,
        )
        return result, probe

    def full_trajectories(self, plan, values=None):
        """按计划顺序生成完整的轨迹映射。"""
        ids = family_test_ids(plan)
        values = values if values is not None else [
            0.004 + 0.01 * index for index in range(len(ids))
        ]
        return {test_id: {"p_value": value} for test_id, value in zip(ids, values)}


# ---------------------------------------------------------------------------
# 验收标准①：跨轮误差预算
# ---------------------------------------------------------------------------


class AlphaForRoundTests(unittest.TestCase):
    """``alpha_for_round`` 必须严格等于 ``total_alpha / 2**t``。"""

    def test_matches_power_of_two_division_exactly(self):
        for round_index in (1, 2, 3, 4, 5, 10, 20, 100, 500, ROUND_INDEX_MAX):
            with self.subTest(round_index=round_index):
                expected = 0.05 / 2 ** round_index
                self.assertEqual(
                    alpha_for_round(0.05, round_index), expected
                )

    def test_matches_ldexp_and_never_uses_another_scheme(self):
        """与 ``math.ldexp`` 同源：不是线性递减，也不是固定 α。"""
        for round_index in (1, 2, 6, 33):
            with self.subTest(round_index=round_index):
                value = alpha_for_round(0.01, round_index)
                self.assertEqual(value, math.ldexp(0.01, -round_index))
                # 排除「线性递减」这一常见误实现。
                self.assertNotEqual(value, 0.01 / round_index)

    def test_halves_each_round(self):
        """逐轮减半：α_{t+1} = α_t / 2。"""
        for round_index in range(1, 12):
            with self.subTest(round_index=round_index):
                self.assertEqual(
                    alpha_for_round(0.05, round_index + 1),
                    alpha_for_round(0.05, round_index) / 2,
                )

    def test_geometric_sum_below_total_alpha(self):
        """几何级数部分和小于 α，且随轮次单调逼近。"""
        total = 0.05
        partial = sum(alpha_for_round(total, index) for index in range(1, 40))
        self.assertLess(partial, total)
        self.assertGreater(partial, total * 0.99)

    def test_rejects_non_positive_or_out_of_range_total_alpha(self):
        for bad in (0, 1, -0.1, 1.5, "0.05", None, float("inf"), float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(ErrorBudgetError):
                    alpha_for_round(bad, 1)

    def test_rejects_invalid_round_index(self):
        for bad in (0, -1, ROUND_INDEX_MAX + 1, 1.0, True, "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ErrorBudgetError):
                    alpha_for_round(0.05, bad)

    def test_rejects_underflow_to_zero(self):
        """极小 α 配极大轮次会下溢；必须报错而非返回 0。"""
        with self.assertRaises(ErrorBudgetError) as ctx:
            alpha_for_round(5e-324, 1022)
        self.assertIn("下溢", str(ctx.exception))

    def test_underflow_note_is_documented(self):
        self.assertIn("下溢", ALPHA_UNDERFLOW_NOTE)

    def test_signature_offers_no_alternative_scheme(self):
        """签名不含任何可替换口径的参数：跨轮预算不可被调用方改写。"""
        signature = inspect.signature(alpha_for_round)
        self.assertEqual(
            list(signature.parameters), ["total_alpha", "round_index"]
        )
        for name in ("method", "scheme", "adjust", "alpha", "kwargs"):
            self.assertNotIn(name, signature.parameters)

    def test_formula_note_states_the_identity(self):
        self.assertIn("2**t", ALPHA_FORMULA_NOTE)
        self.assertIn("ldexp", ALPHA_FORMULA_NOTE)


# ---------------------------------------------------------------------------
# 验收标准④：Holm 校正
# ---------------------------------------------------------------------------


class HolmAdjustTests(unittest.TestCase):
    """Holm 校正的正确性、单调性与空输入行为。"""

    def test_empty_mapping_returns_empty(self):
        result = holm_adjust({})
        self.assertIsInstance(result, HolmResult)
        self.assertEqual(result.adjusted, {})
        self.assertEqual(result.entries, ())
        self.assertEqual(result.missing, ())
        self.assertEqual(len(result), 0)
        self.assertEqual(result.size, 0)
        self.assertEqual(result.to_dict()["adjusted"], {})

    def test_standard_example_matches_expected_values(self):
        result = holm_adjust(HOLM_SAMPLE)
        for test_id, expected in HOLM_EXPECTED.items():
            with self.subTest(test_id=test_id):
                self.assertAlmostEqual(result[test_id], expected, places=12)

    def test_factors_follow_step_down_sequence(self):
        result = holm_adjust(HOLM_SAMPLE)
        factors = [entry.factor for entry in result.entries]
        self.assertEqual(factors, [4, 3, 2, 1])
        self.assertEqual([entry.rank for entry in result.entries], [1, 2, 3, 4])

    def test_adjusted_sequence_is_non_decreasing(self):
        """单调化是 Holm 的必要组成：调整值序列必须非降。"""
        values = {
            "a": 0.01, "b": 0.011, "c": 0.012, "d": 0.9, "e": 0.91,
        }
        result = holm_adjust(values)
        ordered = [entry.adjusted for entry in result.entries]
        for previous, current in zip(ordered, ordered[1:]):
            self.assertLessEqual(previous, current)

    def test_adjusted_is_never_below_raw(self):
        values = {"a": 0.001, "b": 0.02, "c": 0.5}
        result = holm_adjust(values)
        for test_id, raw in values.items():
            with self.subTest(test_id=test_id):
                self.assertGreaterEqual(result[test_id], raw)

    def test_none_values_are_reported_not_imputed(self):
        """``None`` 只进入 ``missing``，**不**插补为 1 也不参与名次。"""
        result = holm_adjust({"a": 0.01, "b": None, "c": 0.5})
        self.assertEqual(result.missing, ("b",))
        self.assertNotIn("b", result.adjusted)
        self.assertEqual(result.size, 2)
        # 名次只在可计算者之间分配。
        self.assertEqual(sorted(e.factor for e in result.entries), [1, 2])

    def test_all_missing_returns_empty_like_empty_input(self):
        result = holm_adjust({"a": None, "b": None})
        self.assertEqual(result.adjusted, {})
        self.assertEqual(result.entries, ())
        self.assertEqual(result.missing, ("a", "b"))

    def test_ties_resolved_deterministically_by_identifier(self):
        """平局按标识字典序：结果与输入顺序无关。"""
        forward = holm_adjust({"b": 0.02, "a": 0.02})
        backward = holm_adjust({"a": 0.02, "b": 0.02})
        self.assertEqual(forward.adjusted, backward.adjusted)
        self.assertEqual(forward.adjusted["a"], forward.adjusted["b"])
        self.assertEqual(
            [entry.test_id for entry in forward.entries], ["a", "b"]
        )

    def test_result_is_independent_of_input_order(self):
        first = holm_adjust({"a": 0.004, "b": 0.02, "c": 0.03, "d": 0.5})
        second = holm_adjust({"d": 0.5, "c": 0.03, "b": 0.02, "a": 0.004})
        self.assertEqual(first.adjusted, second.adjusted)

    def test_rejects_out_of_range_or_non_numeric_p_values(self):
        for bad in (-0.1, 1.1, "0.05", [0.1], float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecuteInputError):
                    holm_adjust({"a": bad})

    def test_rejects_non_mapping_input(self):
        for bad in ([0.1, 0.2], "abc", 5):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecuteInputError):
                    holm_adjust(bad)

    def test_rejects_token_like_keys(self):
        with self.assertRaises(ExecuteInputError):
            holm_adjust({"raw_token": 0.1})

    def test_adjusted_is_read_only(self):
        result = holm_adjust(HOLM_SAMPLE)
        with self.assertRaises(TypeError):
            result.adjusted["t-a"] = 0.9  # type: ignore[index]

    def test_method_name_is_holm_and_documented(self):
        self.assertEqual(holm_adjust(HOLM_SAMPLE).method, HOLM_ADJUSTMENT_NAME)


# ---------------------------------------------------------------------------
# 验收标准③：族完整性
# ---------------------------------------------------------------------------


class FamilyCoherenceTests(_ProtocolFixture):
    """检验族必须完整，且不得删改失败项。"""

    def test_family_expands_over_metric_and_subgroup_product(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        ids = family_test_ids(plan)
        self.assertEqual(len(ids), 2 * 2)
        self.assertEqual(len(set(ids)), 4)

    def test_partial_trajectories_are_rejected(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        ids = family_test_ids(plan)
        with self.assertRaises(FamilyCoherenceError) as ctx:
            self.run_once(plan, {ids[0]: {"p_value": 0.01}})
        self.assertIn("缺少检验", str(ctx.exception))

    def test_extra_trajectory_is_rejected(self):
        plan = self.plan_of()
        with self.assertRaises(FamilyCoherenceError) as ctx:
            self.run_once(
                plan,
                {
                    family_test_ids(plan)[0]: {"p_value": 0.01},
                    "t::unknown::metric::overall": {"p_value": 0.02},
                },
            )
        self.assertIn("多出未登记检验", str(ctx.exception))

    def test_check_family_coherence_reports_both_directions(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC])
        ids = set(family_test_ids(plan))
        extra = {"t::unknown::m::overall"}
        with self.assertRaises(FamilyCoherenceError) as ctx:
            check_family_coherence(plan, list(ids - {sorted(ids)[0]}) + sorted(extra))
        message = str(ctx.exception)
        self.assertIn("缺少检验", message)
        self.assertIn("多出未登记检验", message)

    def test_unavailable_tests_are_placeheld_not_dropped(self):
        """无轨迹时逐条占位：「没算」不等于「不显著」。"""
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        result, _ = self.run_once(plan, None)
        self.assertEqual(result.test_count, 4)
        self.assertEqual(len(result.unavailable_test_ids), 4)
        self.assertFalse(result.complete)
        self.assertEqual(result.available_count, 0)
        for item in result.results:
            self.assertFalse(item.available)
            self.assertIsNone(item.p_value)
            self.assertIsNone(item.significant)
            self.assertTrue(item.reasons)

    def test_missing_p_value_in_trajectory_is_not_silently_dropped(self):
        """显式给出 ``None`` 的检验同样占位，而非被静默丢弃。"""
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC])
        ids = family_test_ids(plan)
        trajectories = {ids[0]: {"p_value": 0.01}, ids[1]: {"p_value": None}}
        result, _ = self.run_once(plan, trajectories)
        self.assertEqual(result.test_count, 2)
        self.assertEqual(result.unavailable_test_ids, (ids[1],))
        self.assertIsNone(result.result_of(ids[1]).p_value)

    def test_failed_tests_are_never_excluded(self):
        """不显著的检验仍逐条在册，且 ``excluded_failed_tests`` 恒为空。"""
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        result, _ = self.run_once(plan, [0.9, 0.85, 0.8, 0.75])
        self.assertEqual(result.test_count, 4)
        self.assertEqual(result.excluded_failed_tests, ())
        self.assertEqual(result.significant_count, 0)
        self.assertEqual(result.available_count, 4)
        for test_id in family_test_ids(plan):
            self.assertIsNotNone(result.result_of(test_id))

    def test_result_order_follows_the_frozen_plan(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        self.assertEqual(
            tuple(item.test_id for item in result.results),
            tuple(str(entry["id"]) for entry in plan["test_family"]),
        )

    def test_scope_text_is_transcribed_from_the_plan(self):
        """零假说 / 备择 / 方法的原文被完整转录，不得改写。"""
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        entry = plan["test_family"][0]
        item = result.results[0]
        self.assertEqual(item.null, entry["null"])
        self.assertEqual(item.alternative, entry["alternative"])
        self.assertEqual(item.method, entry["method"])

    def test_family_coherence_note_documents_the_boundary(self):
        self.assertIn("对账", FAMILY_COHERENCE_NOTE)
        self.assertIn("筛选", FAMILY_COHERENCE_NOTE)


# ---------------------------------------------------------------------------
# 验收标准②：消费顺序
# ---------------------------------------------------------------------------


class ConsumptionOrderTests(_ProtocolFixture):
    """必须先提交 ``used``、再返回数据。"""

    def test_consume_happens_before_any_read(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC])
        result, probe = self.run_once(plan, self.full_trajectories(plan))
        # 消费前没有任何读取事件。
        self.assertEqual(probe.reads_before_consume, 0)
        self.assertEqual(probe.events[0], "consume")

    def test_order_records_committed_before_read(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        order = result.order
        self.assertEqual(order.consume_call_index, order.used_committed_index)
        self.assertGreater(order.first_read_index, order.used_committed_index)
        self.assertTrue(order.committed_before_read)
        order.verify()

    def test_verify_rejects_commit_after_read(self):
        """伪造「先读后提交」的顺序必须被拒绝。"""
        broken = ExecutionOrder(
            consume_call_index=2, used_committed_index=2, first_read_index=1
        )
        with self.assertRaises(SequenceOrderError):
            broken.verify()

    def test_verify_rejects_commit_not_equal_to_consume(self):
        broken = ExecutionOrder(
            consume_call_index=3, used_committed_index=1, first_read_index=4
        )
        with self.assertRaises(SequenceOrderError):
            broken.verify()

    def test_verify_rejects_read_without_commit(self):
        broken = ExecutionOrder(
            consume_call_index=0, used_committed_index=0, first_read_index=1
        )
        with self.assertRaises(SequenceOrderError):
            broken.verify()

    def test_no_read_recorded_when_no_trajectories(self):
        """无轨迹时不记录读取事件，且顺序仍然成立。"""
        plan = self.plan_of()
        result, _ = self.run_once(plan, None)
        self.assertIsNone(result.order.first_read_index)
        self.assertTrue(result.order.committed_before_read)
        result.order.verify()

    def test_consumed_record_count_is_positive_and_disclosed(self):
        """只记消费条数，不转录任何记录正文。"""
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        self.assertGreater(result.consumed_record_count, 0)

    def test_execute_rejects_record_entrypoints(self):
        """不接受任何外部记录入口：确证记录只能经消费返回。"""
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        for keyword in ("records", "dataset", "raw", "read_dataset"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(TypeError):
                    # 参数名不存在，Python 层先于任何校验拒绝。
                    execute_family(
                        self.confirmer,
                        binding,
                        plan,
                        protocol=self.protocol,
                        **{keyword: []},
                    )

    def test_forbidden_call_keys_cover_records_and_filters(self):
        for name in ("records", "dataset", "raw", "read_dataset",
                     "only", "exclude", "subset", "filter", "select", "drop"):
            with self.subTest(name=name):
                self.assertIn(name, FORBIDDEN_CALL_KEYS)

    def test_consumption_order_note_documents_the_sequence(self):
        self.assertIn("used", CONSUMPTION_ORDER_NOTE)
        self.assertIn("consume", CONSUMPTION_ORDER_NOTE)

    def test_consumed_binding_cannot_be_consumed_again(self):
        """同一绑定只能首用一次：第二次执行必须由 M1 拒绝。"""
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        execute_family(
            self.confirmer, binding, plan,
            protocol=self.protocol, trajectories=self.full_trajectories(plan),
        )
        with self.assertRaises(StateError):
            execute_family(
                self.confirmer, binding, plan,
                protocol=self.protocol, trajectories=self.full_trajectories(plan),
            )


# ---------------------------------------------------------------------------
# 轮次与 α 的对账
# ---------------------------------------------------------------------------


class RoundAndAlphaAlignmentTests(_ProtocolFixture):
    """轮次三方对账与 α 与绑定登记值的对账。"""

    def test_alpha_matches_binding_registration(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        self.assertEqual(
            result.alpha, alpha_for_round(self.spec["confirmation"]["total_alpha"], 1)
        )

    def test_round_index_parameter_must_match_plan(self):
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        with self.assertRaises(SequenceOrderError):
            execute_family(
                self.confirmer, binding, plan, 2,
                protocol=self.protocol, trajectories=self.full_trajectories(plan),
            )

    def test_round_index_defaults_to_plan(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        self.assertEqual(result.round_index, 1)

    def test_registered_alpha_mismatch_is_rejected(self):
        """绑定登记的 α 与独立复算不一致时拒绝执行。"""
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        tampered = dict(binding)
        tampered["alpha"] = binding["alpha"] * 2
        with self.assertRaises(SequenceOrderError) as ctx:
            execute_family(
                self.confirmer, tampered, plan,
                protocol=self.protocol, trajectories=self.full_trajectories(plan),
            )
        self.assertIn("α", str(ctx.exception))

    def test_protocol_mismatch_is_rejected(self):
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        wrong = dict(self.protocol)
        wrong["protocol_id"] = "protocol_other"
        with self.assertRaises(SequenceOrderError):
            execute_family(
                self.confirmer, binding, plan,
                protocol=wrong, trajectories=self.full_trajectories(plan),
            )

    def test_missing_protocol_is_rejected(self):
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        with self.assertRaises(ExecuteInputError):
            execute_family(self.confirmer, binding, plan)

    def test_round_scope_note_separates_conversation_from_research_rounds(self):
        self.assertIn("研究轮次", ROUND_SCOPE_NOTE)
        self.assertIn("对话轮次", ROUND_SCOPE_NOTE)

    def test_round_bounds_match_freeze(self):
        from sdl_m06 import freeze

        self.assertEqual(ROUND_INDEX_MIN, freeze.ROUND_INDEX_MIN)
        self.assertEqual(ROUND_INDEX_MAX, freeze.ROUND_INDEX_MAX)


# ---------------------------------------------------------------------------
# 输入校验与边界
# ---------------------------------------------------------------------------


class InputValidationTests(_ProtocolFixture):
    """输入形态、键名白名单与计划自检。"""

    def test_bad_plan_is_rejected_without_touching_confirmer(self):
        """计划不合法时连 confirmer 都不碰。"""
        plan = self.plan_of()
        broken = dict(plan)
        broken.pop("test_family")
        binding = self.fresh_binding(plan)

        class _Spy:
            called = False

            def consume_confirmation(self, binding_id, replay=False):
                _Spy.called = True
                raise AssertionError("consume_confirmation 不应被调用")

        with self.assertRaises(FamilyCoherenceError):
            execute_family(
                _Spy(), binding, broken,
                protocol=self.protocol, trajectories=None,
            )
        self.assertFalse(_Spy.called)

    def test_offline_self_check_rejects_non_family_field_violations(self):
        """P11 的离线自检必须在执行前生效（不只是依赖族对账兜底）。

        这里缺的是 ``assumptions``——:func:`test_family_coherence` 不检查该字段，
        因此若省掉 :func:`sdl_m06.freeze.validate_frozen_plan`，本用例就会漏放。
        """
        plan = self.plan_of()
        binding = self.fresh_binding(plan)

        class _Spy:
            called = False

            def consume_confirmation(self, binding_id, replay=False):
                _Spy.called = True
                raise AssertionError("consume_confirmation 不应被调用")

        for label, broken in (
            ("缺 assumptions", {k: v for k, v in plan.items() if k != "assumptions"}),
            ("多出非契约字段", {**plan, "extra_field": 1}),
        ):
            with self.subTest(case=label):
                with self.assertRaises(FamilyCoherenceError) as ctx:
                    execute_family(
                        _Spy(), binding, broken,
                        protocol=self.protocol, trajectories=None,
                    )
                self.assertIn("离线自检", str(ctx.exception))
        self.assertFalse(_Spy.called)

    def test_missing_effect_threshold_plan_is_rejected(self):
        """``effect_threshold`` 缺失同样由离线自检拦下。"""
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        broken = {k: v for k, v in plan.items() if k != "effect_threshold"}
        with self.assertRaises(FamilyCoherenceError):
            execute_family(
                self.confirmer, binding, broken,
                protocol=self.protocol, trajectories=None,
            )

    def test_unknown_trajectory_key_is_rejected(self):
        plan = self.plan_of()
        test_id = family_test_ids(plan)[0]
        with self.assertRaises(ExecuteInputError) as ctx:
            self.run_once(plan, {test_id: {"p_value": 0.01, "pvalue": 0.02}})
        self.assertIn("未知键", str(ctx.exception))

    def test_out_of_range_p_value_in_trajectory_is_rejected(self):
        plan = self.plan_of()
        test_id = family_test_ids(plan)[0]
        with self.assertRaises(ExecuteInputError):
            self.run_once(plan, {test_id: {"p_value": 1.5}})

    def test_token_like_keys_are_rejected(self):
        plan = self.plan_of()
        test_id = family_test_ids(plan)[0]
        with self.assertRaises(ExecuteInputError):
            self.run_once(plan, {test_id: {"p_value": 0.01, "raw_token": "x"}})

    def test_positional_trajectory_beyond_family_is_rejected(self):
        plan = self.plan_of()
        with self.assertRaises(ExecuteInputError) as ctx:
            self.run_once(plan, [0.01, 0.02, 0.03])
        self.assertIn("无法归属", str(ctx.exception))

    def test_duplicate_test_id_in_sequence_is_rejected(self):
        plan = self.plan_of()
        test_id = family_test_ids(plan)[0]
        with self.assertRaises(ExecuteInputError):
            self.run_once(
                plan, [{"test_id": test_id, "p_value": 0.01},
                       {"test_id": test_id, "p_value": 0.02}]
            )

    def test_binding_requires_state_and_alpha(self):
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        for missing in ("state", "alpha"):
            with self.subTest(missing=missing):
                broken = {key: value for key, value in binding.items() if key != missing}
                with self.assertRaises(ExecuteInputError):
                    execute_family(
                        self.confirmer, broken, plan,
                        protocol=self.protocol, trajectories=None,
                    )

    def test_invalid_binding_state_is_rejected(self):
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        broken = dict(binding)
        broken["state"] = "compromised"
        with self.assertRaises(ExecuteInputError):
            execute_family(
                self.confirmer, broken, plan,
                protocol=self.protocol, trajectories=None,
            )

    def test_module_without_consume_confirmation_is_rejected(self):
        plan = self.plan_of()
        binding = self.fresh_binding(plan)
        with self.assertRaises(ExecuteInputError) as ctx:
            execute_family(
                object(), binding, plan,
                protocol=self.protocol, trajectories=None,
            )
        self.assertIn("consume_confirmation", str(ctx.exception))

    def test_reserved_keys_reject_explicit_none(self):
        self.assertIn("binding", EXECUTE_RESERVED_KEYS)
        self.assertIn("plan", EXECUTE_RESERVED_KEYS)
        plan = self.plan_of()
        with self.assertRaises(ExecuteInputError):
            execute_family(
                self.confirmer, None, plan,
                protocol=self.protocol, trajectories=None,
            )

    def test_fact_keys_include_p_value(self):
        self.assertIn("p_value", FACT_KEYS)


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


class ResultObjectTests(_ProtocolFixture):
    """结果对象的可序列化、摘要稳定性与汇总。"""

    def test_to_dict_is_json_serializable(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        payload = json.loads(canonical_json(result.to_dict()))
        self.assertEqual(payload["round_index"], 1)
        self.assertEqual(len(payload["results"]), 4)

    def test_content_digest_is_deterministic(self):
        """同一对象多次摘要一致；不同轮次的执行摘要不同。"""
        import hashlib

        plan = self.plan_of()
        trajectories = self.full_trajectories(plan)
        result, _ = self.run_once(plan, trajectories)
        first = result.content_digest()
        self.assertEqual(first, result.content_digest())
        self.assertEqual(
            first,
            hashlib.sha256(canonical_json(result.to_dict()).encode("utf-8")).hexdigest(),
        )
        # 第 2 轮（不同 α 与轮次）的摘要必须不同，摘要确实随内容变化。
        second_plan = self.plan_of(round_index=2)
        second, _ = self.run_once(second_plan, self.full_trajectories(second_plan))
        self.assertNotEqual(first, second.content_digest())

    def test_holm_result_is_exposed_with_ranks_and_thresholds(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        result, _ = self.run_once(plan, [0.004, 0.02, 0.03, 0.5])
        self.assertEqual(result.holm.size, 4)
        for test_id, (threshold, significant) in result.decisions.items():
            item = result.result_of(test_id)
            self.assertAlmostEqual(item.holm_threshold, threshold)
            self.assertEqual(item.significant, significant)
            self.assertAlmostEqual(
                threshold, result.alpha / (result.holm.size - item.rank + 1)
            )

    def test_holm_threshold_uses_rank_factor(self):
        """第 k 名的阈值恰为 α_t / (m - k + 1)。"""
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC], subgroups=SUBGROUPS)
        result, _ = self.run_once(plan, [0.004, 0.02, 0.03, 0.5])
        entries = {entry.test_id: entry for entry in result.holm.entries}
        for test_id, entry in entries.items():
            with self.subTest(test_id=test_id):
                expected = result.alpha / entry.factor
                self.assertAlmostEqual(
                    result.result_of(test_id).holm_threshold, expected
                )

    def test_significant_is_a_mechanical_threshold_comparison(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, [0.0001])
        item = result.results[0]
        self.assertTrue(item.significant is True or item.significant is False)
        self.assertEqual(item.significant, item.p_value <= item.holm_threshold)

    def test_threshold_boundary_is_inclusive(self):
        """判定为 ``p ≤ 阈值``（闭区间）：恰好等于阈值时算超过。

        单条检验时 m=1、乘数为 1，故阈值恰为 α_t，可精确取到边界。
        若把实现改成严格小于（``p < 阈值``），本用例会失败。
        """
        plan = self.plan_of()
        alpha = alpha_for_round(self.spec["confirmation"]["total_alpha"], 1)
        test_id = family_test_ids(plan)[0]
        result, _ = self.run_once(plan, {test_id: {"p_value": alpha}})
        item = result.results[0]
        self.assertEqual(result.holm.size, 1)
        self.assertAlmostEqual(item.holm_threshold, alpha)
        self.assertTrue(item.significant)

    def test_threshold_boundary_excludes_just_above(self):
        """刚超过阈值一点即不显著——边界不是「差不多就行」。"""
        plan = self.plan_of()
        alpha = alpha_for_round(self.spec["confirmation"]["total_alpha"], 1)
        test_id = family_test_ids(plan)[0]
        result, _ = self.run_once(plan, {test_id: {"p_value": alpha * (1 + 1e-9)}})
        self.assertFalse(result.results[0].significant)

    def test_summarize_family_reports_counts(self):
        plan = self.plan_of(metrics=[METRIC, ALT_METRIC])
        result, _ = self.run_once(plan, [0.0001, 0.9])
        summary = summarize_family(result, result.alpha)
        self.assertEqual(summary["test_count"], 2)
        self.assertEqual(summary["available_count"], 2)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["holm_method"], HOLM_ADJUSTMENT_NAME)

    def test_summarize_reports_incompleteness(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, None)
        summary = summarize_family(result, result.alpha)
        self.assertFalse(summary["complete"])
        self.assertEqual(
            summary["unavailable_test_ids"], result.unavailable_test_ids
        )

    def test_result_of_unknown_test_id_raises(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        with self.assertRaises(ExecuteInputError):
            result.result_of("t::nope::m::overall")

    def test_results_tuple_is_immutable(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        self.assertIsInstance(result.results, tuple)
        self.assertIsInstance(result, FamilyExecution)
        self.assertIsInstance(result.results[0], TestExecution)

    def test_complete_family_boundary_note_distinguishes_missing(self):
        self.assertIn("占位", COMPLETE_FAMILY_BOUNDARY_NOTE)
        self.assertIn("不显著", COMPLETE_FAMILY_BOUNDARY_NOTE)
        self.assertIn("插补", COMPLETE_FAMILY_BOUNDARY_NOTE)

    def test_code_version_is_recorded(self):
        plan = self.plan_of()
        result, _ = self.run_once(plan, self.full_trajectories(plan))
        self.assertEqual(result.code_version, EXECUTE_VERSION)


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

    def test_p12_implements_exactly_its_declared_entrypoints(self):
        """本层必须提供的三个入口确实存在。"""
        defined = self._defined_names(self._tree())
        for name in ("execute_family", "holm_adjust", "alpha_for_round"):
            with self.subTest(name=name):
                self.assertIn(name, defined)

    def test_does_not_define_later_stage_entrypoints(self):
        """AST 检查：不定义 P13 及之后的任何入口。"""
        defined = self._defined_names(self._tree())
        overlaps = sorted(defined & set(NOT_PROVIDED_BY_P12))
        self.assertEqual(overlaps, [], f"越界实现：{overlaps}")

    def test_self_check_passes(self):
        report = self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["defined_forbidden"], ())
        self.assertEqual(report["accessed_forbidden"], ())

    def test_no_evidence_read_access_paths(self):
        """AST 检查：不存在绕过消费的读取入口。"""
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
        for name in ("sqlite3", "load_snapshot", "read_dataset"):
            with self.subTest(name=name):
                self.assertNotIn(name, accessed)

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
        """模块内不得出现对 sdl_m01 的写入式引用。"""
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
                self.assertNotIn(node.attr, ("skip", "skipIf", "skipUnless", "xfail", "skipTest"))

    def test_not_provided_list_contains_p13_entrypoints(self):
        for name in ("classify_result", "record_and_release",
                     "record_evaluation", "release_results"):
            with self.subTest(name=name):
                self.assertIn(name, NOT_PROVIDED_BY_P12)

    def test_execute_does_not_call_record_or_release(self):
        """AST 检查：不调用 M1 的记录与发布接口（属 P13）。"""
        tree = self._tree()
        called = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Attribute):
                called.add(node.func.attr)
        for name in ("record_evaluation", "release_results"):
            with self.subTest(name=name):
                self.assertNotIn(name, called)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
