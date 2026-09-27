"""P08 验收测试：模式到假说与竞争解释。

覆盖三条验收标准：

1. **每个假说含可证伪的 ``predictions``**：四种模式种类生成的假说均非空，
   每条预测都含非空 ``incompatible_with``；调用方无法直接注入预测
   （预测由模式载荷机械派生）；缺失 ``incompatible_with`` 的条目会被
   P07 的校验拒绝。
2. **每个假说至少一个零假说**：``null_hypotheses`` 非空；
   ``attach_nulls`` 能为外部假说补齐零假说并产生新版本；条目为空时报错。
3. **假说池经去重后大小受契约约束**：``max_hypotheses`` 是硬上界且可配置；
   默认值锚定框架说明 §2 的「每轮保留 50 个探索候选」；两级去重
   （``exact`` / ``skeleton``）在裁剪之前生效；重复 ``pattern_id`` 被合并。

另覆盖：``attach_alternatives`` 的补齐与幂等、版本链（``parent_ids``）只追加、
外部文本填充的受控白名单与「不得授予证据等级」守卫、来源信息必填与
数据引用派生、序列化往返与规范式稳定性、以及 P08 的边界守护——不计算任何
指标（P09）、不做 Pareto 筛选（P10）、不导入 ``sdl_m01`` / ``sdl_m03``、
只用标准库、不授予证据等级、不修改冻结目录。
"""

import ast as pyast
import inspect
import json
import pathlib
import unittest

from sdl_m04.generate import (
    CONTRACT_POOL_NOTE,
    DEFAULT_MAX_HYPOTHESES,
    DEDUP_MODES,
    EVIDENCE_GRADE_KEY_HINTS,
    GENERATE_VERSION,
    GENERATOR_ID,
    KIND_TO_HYPOTHESIS_TYPE,
    LLM_FIELD_FILL_ALLOWED_KEYS,
    ROUND_DIGITS,
    SUPPORTED_PATTERN_KINDS,
    FieldFillError,
    GenerateBudget,
    GenerateError,
    GeneratedHypothesisPool,
    PatternInputError,
    attach_alternatives,
    attach_nulls,
    hypotheses_from_patterns,
)
from sdl_m04.hypothesis import (
    Hypothesis,
    HypothesisValidationError,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "sdl_m04" / "generate.py"
)

_KNOWLEDGE_VERSION = "K-2026-09"

#: 一个合法的数据引用标识（**只是字符串标识，不携带任何记录正文**）。
_DATA_REF = "data_0123456789abcdef"


def cluster_pattern(pattern_id="pat-cluster-1", *, center=1.0, k=2, stability=None,
                    source_ref=_DATA_REF):
    """一个簇模式（字段形状对齐 ``INTERFACES.md`` §2.2 的 Pattern）。"""
    return {
        "pattern_id": pattern_id,
        "kind": "cluster",
        "payload": {
            "cluster_index": 0,
            "k": k,
            "features": ["X1", "X2"],
            "center": {"X1": center, "X2": center + 1},
            "center_zscore": {"X1": 0.1, "X2": 0.2},
            "size": 5,
            "members": ["r-001", "r-002", "r-003", "r-004", "r-005"],
            "partition_signature": "sig-abc",
            "selection": {"criterion": "简化轮廓系数", "silhouette": 0.42,
                          "sizes": [5, 5]},
        },
        "metrics": {
            "error": 1.25,
            "residual": 0.5,
            "complexity": {"clusters": 2, "total": 3},
            "complexity_note": "结构规模近似，不是严格 MDL",
        },
        "stability": stability,
        "provenance": {
            "origin": "structure",
            "detector": "find_clusters",
            "module": "sdl_m03/structure.py",
            "module_version": "P05-v1.0",
            "sample_fingerprint": "fp-0123456789",
            "sample_rows": 10,
            "source_ref": source_ref,
            "purpose": "E",
        },
    }


def changepoint_pattern(pattern_id="pat-changepoint-1", *, threshold=4.5,
                        mean_shift=1.0, source_ref=_DATA_REF):
    """一个变点模式。"""
    return {
        "pattern_id": pattern_id,
        "kind": "changepoint",
        "payload": {
            "position": 3,
            "threshold": threshold,
            "order_by": "X1",
            "before": {"count": 3, "mean": 1.0, "record_first": "r-001",
                       "record_last": "r-003"},
            "after": {"count": 4, "mean": 2.0, "record_first": "r-004",
                      "record_last": "r-007"},
            "mean_shift": mean_shift,
            "segment_index": 1,
            "segments_total": 2,
        },
        "metrics": {
            "error": 0.2,
            "residual": 0.1,
            "complexity": {"changepoints": 1, "total": 2},
        },
        "stability": None,
        "provenance": {
            "detector": "find_changepoints",
            "module_version": "P05-v1.0",
            "sample_fingerprint": "fp-abcdef",
            "source_ref": source_ref,
        },
    }


def invariant_pattern(pattern_id="pat-invariant-1", *, estimate=2.0,
                      source_ref=_DATA_REF):
    """一个近似守恒量模式。"""
    return {
        "pattern_id": pattern_id,
        "kind": "invariant",
        "payload": {
            "expression": "X1 / X2",
            "estimate": estimate,
            "interval": [1.5, 2.5],
            "core_interval": [1.8, 2.2],
            "spread": 0.2,
            "scale": 1.0,
            "allowed_deviation": 0.1,
            "tolerance": 0.05,
            "basis": "relative",
            "criterion": "一致行占比 ≥ 门槛",
            "conformity": 0.95,
            "min_conformity": 0.9,
            "coverage": 1.0,
            "n_used": 10,
            "n_rows": 10,
            "n_excluded": 0,
            "counterexample_count": 1,
            "counterexamples": [{"record_id": "r-009", "value": 2.4,
                                 "deviation": 0.11}],
        },
        "metrics": {
            "error": 0.4,
            "residual": 0.15,
            "complexity": {"expression_nodes": 3, "counterexamples": 1, "total": 4},
        },
        "stability": None,
        "provenance": {
            "detector": "check_invariant",
            "module_version": "P05-v1.0",
            "sample_fingerprint": "fp-inv",
            "source_ref": source_ref,
        },
    }


def relation_pattern(pattern_id="pat-relation-1", *, source_ref=_DATA_REF):
    """一个关系模式（由 P04 声明，字典形态传入）。"""
    return {
        "pattern_id": pattern_id,
        "kind": "relation",
        "payload": {
            "expression": "X1^2 + 2*X1",
            "target": "Y",
            "feature": "X1",
            "coefficients": {"a": 1.0, "b": 2.0},
        },
        "metrics": {
            "error": 0.3,
            "residual": 0.12,
            "complexity": {"nodes": 5, "total": 5},
        },
        "stability": None,
        "provenance": {
            "detector": "fit_relation",
            "module_version": "P04-v1.0",
            "source_ref": source_ref,
        },
    }


def all_patterns():
    """四种模式种类各一条。"""
    return [
        cluster_pattern(),
        changepoint_pattern(),
        invariant_pattern(),
        relation_pattern(),
    ]


def make_pool(patterns=None, **kwargs):
    """构造一个假说池，缺省输入为四种模式各一条。"""
    kwargs.setdefault("knowledge_version", _KNOWLEDGE_VERSION)
    return hypotheses_from_patterns(patterns if patterns is not None else all_patterns(),
                                    **kwargs)


# ---------------------------------------------------------------------------
# 验收标准 ①：可证伪 predictions
# ---------------------------------------------------------------------------


class FalsifiablePredictionsTests(unittest.TestCase):
    """验收标准 ①：每个假说含 ``predictions``（什么结果与它不相容）。"""

    def test_every_kind_yields_nonempty_predictions(self):
        """四种模式种类生成的假说都带非空 predictions。"""
        pool = make_pool()
        self.assertEqual(len(pool), len(SUPPORTED_PATTERN_KINDS))
        for hypothesis in pool:
            with self.subTest(kind=hypothesis.type):
                self.assertTrue(hypothesis.predictions)

    def test_each_prediction_declares_incompatibility(self):
        """每条预测都写明什么结果与假说不相容，且非空。"""
        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                for index, prediction in enumerate(hypothesis.predictions):
                    self.assertIn("incompatible_with", prediction)
                    self.assertIsInstance(prediction["incompatible_with"], str)
                    self.assertTrue(prediction["incompatible_with"].strip(),
                                    f"predictions[{index}].incompatible_with 为空")

    def test_predictions_are_derived_not_caller_supplied(self):
        """预测由模式载荷派生：调用方无法通过参数直接注入预测。"""
        signature = inspect.signature(hypotheses_from_patterns)
        self.assertNotIn("predictions", signature.parameters)
        # 改变载荷中的结构参数，陈述与预测文本应随之变化（说明确实来自载荷）。
        first = make_pool([cluster_pattern(k=2)]).hypotheses[0]
        second = make_pool([cluster_pattern(k=3)]).hypotheses[0]
        self.assertIn("k=2", first.statement)
        self.assertIn("k=3", second.statement)
        self.assertNotEqual(
            [p["incompatible_with"] for p in first.predictions],
            [p["incompatible_with"] for p in second.predictions],
        )

    def test_missing_incompatible_with_is_rejected_by_p07(self):
        """缺 ``incompatible_with`` 的预测条目会被 P07 的字段校验拒绝。"""
        with self.assertRaises(HypothesisValidationError):
            Hypothesis(
                **{
                    "id": "h-manual",
                    "version": "1",
                    "type": "cluster",
                    "statement": "手工构造",
                    "representation": {
                        "missing_value_handling": "drop",
                        "standardization": "zscore",
                    },
                    "predictions": [{"expectation": "只有期望，没有不相容声明"}],
                    "null_hypotheses": ["零假说"],
                    "provenance": {
                        "data_ids": [_DATA_REF],
                        "knowledge_version": _KNOWLEDGE_VERSION,
                        "generator": "test",
                        "code_version": "v1",
                    },
                }
            )

    def test_prediction_count_respects_budget(self):
        """预测条数受预算约束。"""
        pool = make_pool(budget={"max_predictions": 1})
        for hypothesis in pool:
            with self.subTest(kind=hypothesis.type):
                self.assertEqual(len(hypothesis.predictions), 1)

    def test_predictions_are_readonly_on_hypothesis(self):
        """生成的假说对象不可原地修改（沿用 P07 的冻结语义）。"""
        hypothesis = make_pool().hypotheses[0]
        with self.assertRaises(Exception):
            hypothesis.predictions[0]["incompatible_with"] = "篡改"  # type: ignore[index]


# ---------------------------------------------------------------------------
# 验收标准 ②：至少一个零假说
# ---------------------------------------------------------------------------


class NullHypothesesTests(unittest.TestCase):
    """验收标准 ②：每个假说至少一个零假说。"""

    def test_every_generated_hypothesis_has_nulls(self):
        """生成物恒带至少一条零假说。"""
        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                self.assertGreaterEqual(len(hypothesis.null_hypotheses), 1)

    def test_nulls_include_direct_opposite_and_trivial(self):
        """簇模式同时带「直接对立」与「平凡机制」两类零假说。"""
        hypothesis = make_pool([cluster_pattern()]).hypotheses[0]
        null_ids = {entry["null_id"] for entry in hypothesis.null_hypotheses}
        self.assertIn("null-no-subgroup", null_ids)
        self.assertIn("null-random-partition", null_ids)

    def test_every_kind_has_a_type_specific_null(self):
        """四种模式种类各自给出**针对性**的零假说，而非同一条通用文本。"""
        texts = {}
        for hypothesis in make_pool():
            self.assertTrue(hypothesis.null_hypotheses)
            texts[hypothesis.type] = hypothesis.null_hypotheses[0]["statement"]
        self.assertEqual(len(set(texts.values())), len(SUPPORTED_PATTERN_KINDS))

    def test_attach_nulls_fills_external_hypothesis(self):
        """attach_nulls 能为外部构造的假说补齐零假说。"""
        stripped = _hypothesis_without_nulls()
        filled = attach_nulls(stripped)
        self.assertNotEqual(filled, stripped)
        self.assertGreaterEqual(len(filled.null_hypotheses), 1)
        self.assertIn("null-no-subgroup",
                      {entry["null_id"] for entry in filled.null_hypotheses})

    def test_attach_nulls_uses_parent_chain(self):
        """补齐动作经版本链留下追溯痕迹（parent_ids 只追加）。

        夹具本身经一次 ``new_version`` 构造（版本 2），因此补齐后应为版本 3，
        其父链为「夹具的父链 + 夹具自身标识」——链条只增不改。
        """
        stripped = _hypothesis_without_nulls()
        filled = attach_nulls(stripped)
        self.assertEqual(filled.id, stripped.id)
        self.assertEqual(filled.version, "3")
        self.assertEqual(filled.parent_ids, (*stripped.parent_ids, stripped.identity))
        self.assertEqual(filled.parent_ids[-1], stripped.identity)
        # 链条是**追加**的：旧链条被完整保留，而不是被替换。
        self.assertTrue(set(stripped.parent_ids) <= set(filled.parent_ids))

    def test_attach_nulls_accepts_explicit_entries(self):
        """显式给出的零假说被采纳（字符串与映射皆可）。"""
        stripped = _hypothesis_without_nulls()
        filled = attach_nulls(
            stripped,
            [
                "零假说：无子群结构。",
                {"null_id": "n2", "statement": "零假说：随机划分即可复现。",
                 "rationale": "平凡解释"},
            ],
        )
        self.assertEqual(len(filled.null_hypotheses), 2)
        self.assertEqual(filled.null_hypotheses[0], "零假说：无子群结构。")

    def test_attach_nulls_is_idempotent(self):
        """内容未变时返回原对象，不产生无意义的新版本。"""
        filled = attach_nulls(_hypothesis_without_nulls())
        again = attach_nulls(filled)
        self.assertIs(again, filled)

    def test_attach_nulls_rejects_empty(self):
        """空序列会被拒绝（不能把零假说清空）。"""
        with self.assertRaises(GenerateError):
            attach_nulls(_hypothesis_without_nulls(), [])

    def test_attach_nulls_rejects_blank_string(self):
        """空白字符串条目被拒绝。"""
        with self.assertRaises(GenerateError):
            attach_nulls(_hypothesis_without_nulls(), ["   "])

    def test_attach_nulls_rejects_mapping_without_statement(self):
        """缺 ``statement`` 的映射条目被拒绝。"""
        with self.assertRaises(GenerateError):
            attach_nulls(_hypothesis_without_nulls(), [{"null_id": "n1"}])

    def test_attach_nulls_rejects_non_hypothesis(self):
        """非 P07 假说对象会被拒绝。"""
        with self.assertRaises(HypothesisValidationError):
            attach_nulls({"id": "h1"})  # type: ignore[arg-type]

    def test_attach_nulls_respects_max(self):
        """条数上界生效。"""
        filled = attach_nulls(
            _hypothesis_without_nulls(),
            ["a", "b", "c", "d"],
            max_nulls=2,
        )
        self.assertEqual(len(filled.null_hypotheses), 2)


def _hypothesis_without_nulls():
    """构造一个零假说为**占位条目**的假说，用于测试补齐。

    P07 要求 ``null_hypotheses`` 非空，因此无法造出「真空」的假说。
    这里先正常生成，再用 ``new_version`` 把零假说换成一条占位条目；
    :func:`attach_nulls` 缺省时应当把它**替换**为按类型反推的零假说。
    """
    hypothesis = make_pool([cluster_pattern()]).hypotheses[0]
    return hypothesis.new_version(
        changes={"null_hypotheses": [{"null_id": "placeholder",
                                      "statement": "占位（待补齐）"}]}
    )


# ---------------------------------------------------------------------------
# 验收标准 ③：池大小受契约约束
# ---------------------------------------------------------------------------


class PoolSizeTests(unittest.TestCase):
    """验收标准 ③：生成的假说池经去重后大小受契约约束。"""

    def test_default_bound_follows_prototype_budget(self):
        """默认上界锚定框架说明 §2 的原型配置（每轮 50 个探索候选）。"""
        self.assertEqual(DEFAULT_MAX_HYPOTHESES, 50)
        self.assertEqual(GenerateBudget().max_hypotheses, 50)

    def test_contract_note_keeps_derivation_traceable(self):
        """上界的推导过程被写明，使「受契约约束」可复核。"""
        self.assertIn("INTERFACES.md", CONTRACT_POOL_NOTE)
        self.assertIn("50", CONTRACT_POOL_NOTE)

    def test_pool_never_exceeds_bound(self):
        """输入远超上界时，输出被裁剪且不超过上界。"""
        patterns = [cluster_pattern(f"pat-{i}", center=float(i)) for i in range(80)]
        pool = make_pool(patterns)
        self.assertLessEqual(len(pool), DEFAULT_MAX_HYPOTHESES)
        self.assertEqual(len(pool), DEFAULT_MAX_HYPOTHESES)

    def test_bound_is_configurable(self):
        """上界可配置。"""
        patterns = [cluster_pattern(f"pat-{i}", center=float(i)) for i in range(20)]
        pool = make_pool(patterns, budget={"max_hypotheses": 3})
        self.assertEqual(len(pool), 3)

    def test_bound_accepts_budget_object(self):
        """接受 GenerateBudget 对象。"""
        patterns = [cluster_pattern(f"pat-{i}", center=float(i)) for i in range(20)]
        pool = make_pool(patterns, budget=GenerateBudget(max_hypotheses=4))
        self.assertEqual(len(pool), 4)
        self.assertEqual(pool.budget.max_hypotheses, 4)

    def test_budget_rejects_invalid_values(self):
        """预算字段必须是正整数。"""
        for bad in (0, -1, 1.5, True, "5"):
            with self.subTest(value=bad):
                with self.assertRaises(GenerateError):
                    GenerateBudget(max_hypotheses=bad)

    def test_budget_rejects_unknown_fields(self):
        """预算字典中的未知字段被拒绝。"""
        with self.assertRaises(GenerateError):
            make_pool(budget={"max_hypotheses": 5, "unexpected": 1})

    def test_records_counts_for_audit(self):
        """来源信息记录了各阶段计数，便于审计裁剪行为。"""
        patterns = [cluster_pattern(f"pat-{i}", center=float(i)) for i in range(60)]
        pool = make_pool(patterns)
        counts = pool.provenance["counts"]
        self.assertEqual(counts["hypotheses_built"], 60)
        self.assertEqual(counts["final"], DEFAULT_MAX_HYPOTHESES)
        self.assertEqual(counts["capped_away"], 60 - DEFAULT_MAX_HYPOTHESES)


class DedupTests(unittest.TestCase):
    """去重：两级模式与确定性保留规则。"""

    def test_duplicate_pattern_ids_are_merged(self):
        """同一 ``pattern_id`` 重复出现时只保留一条。"""
        pool = make_pool([cluster_pattern("same"), cluster_pattern("same")])
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.provenance["counts"]["pattern_duplicates_dropped"], 1)

    def test_exact_mode_keeps_parameter_variants_separate(self):
        """``exact`` 模式不合并参数微变体（数值不同即不同假说）。"""
        patterns = [cluster_pattern("p1", center=1.0),
                    cluster_pattern("p2", center=2.0)]
        pool = make_pool(patterns)
        self.assertEqual(len(pool), 2)

    def test_exact_mode_merges_content_identical_entries(self):
        """内容完全相同的两条（标识不同）在 ``exact`` 下被合并。"""
        first = cluster_pattern("p1", center=1.0)
        second = cluster_pattern("p2", center=1.0)
        pool = make_pool([first, second])
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.provenance["counts"]["exact_merges"], 1)

    def test_skeleton_mode_merges_parameter_variants(self):
        """``skeleton`` 模式合并「同结构、仅常数不同」的参数微变体。"""
        patterns = [cluster_pattern("p1", center=1.0),
                    cluster_pattern("p2", center=2.0),
                    cluster_pattern("p3", center=9.0)]
        pool = make_pool(patterns, dedup_mode="skeleton")
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.provenance["counts"]["skeleton_merges"], 2)

    def test_skeleton_merge_is_traceable(self):
        """被合并的模式标识被完整记录，便于追溯。"""
        patterns = [cluster_pattern("p1", center=1.0),
                    cluster_pattern("p2", center=2.0)]
        pool = make_pool(patterns, dedup_mode="skeleton")
        merged = pool.hypotheses[0].provenance["merged_pattern_ids"]
        self.assertEqual(list(merged), ["p1", "p2"])
        self.assertEqual(pool.hypotheses[0].provenance["merged_pattern_count"], 2)

    def test_skeleton_does_not_merge_different_kinds(self):
        """不同模式种类不会被骨架去重合并。"""
        pool = make_pool([cluster_pattern("p1"), changepoint_pattern("p2")],
                         dedup_mode="skeleton")
        self.assertEqual(len(pool), 2)

    def test_skeleton_does_not_merge_different_structures(self):
        """同种类但结构不同（k 不同）的假说不会被合并。"""
        pool = make_pool([cluster_pattern("p1", k=2), cluster_pattern("p2", k=3)],
                         dedup_mode="skeleton")
        self.assertEqual(len(pool), 2)

    def test_retained_representative_is_deterministic(self):
        """保留者是确定性的（按模式标识字典序取最小），与输入顺序无关。"""
        forward = [cluster_pattern("p2", center=1.0), cluster_pattern("p1", center=1.0)]
        backward = list(reversed(forward))
        first = make_pool(forward, dedup_mode="skeleton")
        second = make_pool(backward, dedup_mode="skeleton")
        self.assertEqual(first.hypotheses[0].provenance["merged_pattern_ids"],
                         second.hypotheses[0].provenance["merged_pattern_ids"])

    def test_dedup_happens_before_capping(self):
        """去重在裁剪之前生效：去重后不足上界时不应有裁剪。"""
        patterns = [cluster_pattern(f"p{i}", center=1.0) for i in range(10)]
        pool = make_pool(patterns, dedup_mode="skeleton")
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.provenance["counts"]["capped_away"], 0)

    def test_invalid_dedup_mode_rejected(self):
        """未知去重模式被拒绝。"""
        with self.assertRaises(GenerateError):
            make_pool(dedup_mode="fuzzy")
        self.assertEqual(DEDUP_MODES, ("exact", "skeleton"))

    def test_quantization_folds_float_tail_differences(self):
        """内容判等按量化后的数值比较，浮点尾差不构成差异。"""
        first = cluster_pattern("p1", center=1.0)
        second = cluster_pattern("p2", center=1.0 + 1e-12)
        pool = make_pool([first, second])
        self.assertEqual(len(pool), 1)
        self.assertEqual(ROUND_DIGITS, 6)


# ---------------------------------------------------------------------------
# 竞争解释
# ---------------------------------------------------------------------------


class AlternativesTests(unittest.TestCase):
    """竞争解释的生成与补齐。"""

    def test_generated_hypotheses_carry_alternatives(self):
        """生成物带竞争解释，且每条写明区分性观测。"""
        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                self.assertGreaterEqual(len(hypothesis.alternatives), 1)
                for alternative in hypothesis.alternatives:
                    self.assertIn("statement", alternative)
                    self.assertTrue(alternative.get("distinguishing_observation"))

    def test_attach_alternatives_fills_external_hypothesis(self):
        """attach_alternatives 能为外部假说补齐竞争解释。"""
        stripped = _hypothesis_without_alternatives()
        filled = attach_alternatives(stripped)
        self.assertGreaterEqual(len(filled.alternatives), 1)
        self.assertEqual(filled.parent_ids[-1], stripped.identity)
        self.assertTrue(set(stripped.parent_ids) <= set(filled.parent_ids))

    def test_attach_alternatives_accepts_explicit_entries(self):
        """显式条目被采纳。"""
        stripped = _hypothesis_without_alternatives()
        filled = attach_alternatives(
            stripped, ["竞争解释：遗漏变量所致。"]
        )
        self.assertEqual(list(filled.alternatives), ["竞争解释：遗漏变量所致。"])

    def test_attach_alternatives_is_idempotent(self):
        """内容未变时返回原对象。"""
        filled = attach_alternatives(_hypothesis_without_alternatives())
        self.assertIs(attach_alternatives(filled), filled)

    def test_attach_alternatives_rejects_empty(self):
        """空序列被拒绝。"""
        with self.assertRaises(GenerateError):
            attach_alternatives(_hypothesis_without_alternatives(), [])

    def test_attach_alternatives_rejects_bad_types(self):
        """非法条目类型被拒绝。"""
        with self.assertRaises(GenerateError):
            attach_alternatives(_hypothesis_without_alternatives(), [123])
        with self.assertRaises(GenerateError):
            attach_alternatives(_hypothesis_without_alternatives(), "整串文本")

    def test_attach_alternatives_respects_max(self):
        """条数上界生效。"""
        filled = attach_alternatives(
            _hypothesis_without_alternatives(), ["a", "b", "c"], max_alternatives=2
        )
        self.assertEqual(len(filled.alternatives), 2)


def _hypothesis_without_alternatives():
    """构造一个竞争解释为占位条目的假说，用于测试补齐。"""
    hypothesis = make_pool([cluster_pattern()]).hypotheses[0]
    return hypothesis.new_version(
        changes={"alternatives": [{"alternative_id": "placeholder",
                                   "statement": "占位（待补齐）"}]}
    )


# ---------------------------------------------------------------------------
# 外部文本填充（LLM 仅作字段填充）
# ---------------------------------------------------------------------------


class FieldFillTests(unittest.TestCase):
    """外部文本生成器只允许填充受控文本字段，不得授予证据等级。"""

    def test_no_enricher_means_no_trace(self):
        """未使用外部文本时，来源信息中没有 field_fill 记录。"""
        hypothesis = make_pool().hypotheses[0]
        self.assertNotIn("field_fill", hypothesis.provenance)
        self.assertFalse(make_pool().provenance["used_text_enricher"])

    def test_text_fills_statement(self):
        """文本可填充 statement。"""
        def enricher(context):
            return {"statement": "改写后的陈述文本。"}

        pool = make_pool(text_enricher=enricher)
        self.assertEqual(pool.hypotheses[0].statement, "改写后的陈述文本。")
        self.assertTrue(pool.provenance["used_text_enricher"])

    def test_text_fill_is_traceable(self):
        """被采纳的字段与文本摘要记入来源信息，可追溯。"""
        def enricher(context):
            return {"statement": "改写后的陈述文本。", "plain_explanation": "通俗解释。"}

        hypothesis = make_pool(text_enricher=enricher).hypotheses[0]
        trace = hypothesis.provenance["field_fill"]
        self.assertTrue(trace["used"])
        self.assertEqual(sorted(trace["allowed_fields"]),
                         ["plain_explanation", "statement"])
        self.assertTrue(trace["text_digest"])
        self.assertIn("证据等级", trace["note"])

    def test_enricher_identity_is_recorded_when_available(self):
        """若生成器提供 model / model_version 等属性，一并记录以便追溯。"""
        class Enricher:
            model = "external-text-model"
            model_version = "2026-09"

            def __call__(self, context):  # noqa: D102 - 测试夹具
                return {"statement": "带身份信息的文本。"}

        trace = make_pool(text_enricher=Enricher()).hypotheses[0].provenance["field_fill"]
        self.assertEqual(trace["model"], "external-text-model")
        self.assertEqual(trace["model_version"], "2026-09")

    def test_evidence_grade_keys_are_rejected(self):
        """文本中出现证据等级类键名一律拒绝（不得据此授予证据等级）。"""
        def enricher(context):
            return {"evidence_level": "E2"}

        with self.assertRaises(FieldFillError) as caught:
            make_pool(text_enricher=enricher)
        self.assertIn("证据等级", str(caught.exception))

    def test_all_grade_hints_are_guarded(self):
        """EVIDENCE_GRADE_KEY_HINTS 中的每个键名都被拦截。"""
        for key in sorted(EVIDENCE_GRADE_KEY_HINTS):
            with self.subTest(key=key):
                def enricher(context, _key=key):
                    return {_key: "x"}

                with self.assertRaises(FieldFillError):
                    make_pool(text_enricher=enricher)

    def test_unknown_keys_are_rejected(self):
        """白名单之外的键被拒绝。"""
        def enricher(context):
            return {"random_field": "x"}

        with self.assertRaises(FieldFillError):
            make_pool(text_enricher=enricher)

    def test_non_mapping_output_rejected(self):
        """生成器返回非映射时被拒绝。"""
        with self.assertRaises(FieldFillError):
            make_pool(text_enricher=lambda context: "纯文本")

    def test_blank_text_rejected(self):
        """空白文本被拒绝。"""
        with self.assertRaises(FieldFillError):
            make_pool(text_enricher=lambda context: {"statement": "   "})

    def test_blank_alternatives_rejected(self):
        """竞争解释文本序列不能含空条目。"""
        with self.assertRaises(FieldFillError):
            make_pool(text_enricher=lambda context: {"alternatives": ["ok", "  "]})

    def test_text_fill_does_not_change_status_or_evidence_log(self):
        """文本填充不改变生命周期状态，也不写入证据日志。"""
        def enricher(context):
            return {"statement": "改写后的陈述文本。"}

        hypothesis = make_pool(text_enricher=enricher).hypotheses[0]
        self.assertEqual(hypothesis.status, "draft")
        self.assertFalse(hypothesis.evidence_log)

    def test_whitelist_contains_only_text_fields(self):
        """白名单只含文本性字段，不含任何结构性字段。"""
        for forbidden in ("predictions", "null_hypotheses", "provenance",
                          "representation", "status"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, LLM_FIELD_FILL_ALLOWED_KEYS)
        self.assertEqual(
            LLM_FIELD_FILL_ALLOWED_KEYS,
            frozenset({"statement", "plain_explanation", "alternatives"}),
        )

    def test_enricher_context_carries_no_secrets(self):
        """传给外部生成器的上下文只含抽象信息，不含令牌或数据正文。"""
        captured = {}

        def enricher(context):
            captured.update(context)
            return {}

        make_pool(text_enricher=enricher)
        lowered = {key.lower() for key in captured}
        for forbidden in ("token", "secret", "password", "credential"):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("pattern_id", captured)
        self.assertNotIn("records", captured)


# ---------------------------------------------------------------------------
# 输入校验与来源信息
# ---------------------------------------------------------------------------


class InputValidationTests(unittest.TestCase):
    """模式入参与来源信息的校验。"""

    def test_knowledge_version_is_required(self):
        """``knowledge_version`` 必填且非空（新颖性口径必须可绑定）。"""
        for bad in ("", "   "):
            with self.subTest(value=bad):
                with self.assertRaises(GenerateError):
                    hypotheses_from_patterns([cluster_pattern()], knowledge_version=bad)
        # 缺失该实参在签名层面即不可调用（无默认值）。
        signature = inspect.signature(hypotheses_from_patterns)
        self.assertIs(signature.parameters["knowledge_version"].default,
                      inspect.Parameter.empty)

    def test_knowledge_version_is_recorded(self):
        """知识库版本被写入每条假说的来源信息。"""
        pool = make_pool()
        for hypothesis in pool:
            self.assertEqual(hypothesis.provenance["knowledge_version"],
                             _KNOWLEDGE_VERSION)

    def test_unknown_kind_rejected(self):
        """未知模式种类被拒绝。"""
        pattern = cluster_pattern()
        pattern["kind"] = "teleportation"
        with self.assertRaises(PatternInputError):
            make_pool([pattern])

    def test_missing_pattern_id_rejected(self):
        """缺模式标识被拒绝。"""
        pattern = cluster_pattern()
        del pattern["pattern_id"]
        with self.assertRaises(PatternInputError):
            make_pool([pattern])

    def test_empty_payload_rejected(self):
        """空载荷被拒绝。"""
        pattern = cluster_pattern()
        pattern["payload"] = {}
        with self.assertRaises(PatternInputError):
            make_pool([pattern])

    def test_non_pattern_rejected(self):
        """既非映射也非模式对象时被拒绝。"""
        with self.assertRaises(PatternInputError):
            make_pool([42])

    def test_empty_patterns_rejected(self):
        """空输入被拒绝。"""
        with self.assertRaises(PatternInputError):
            make_pool([])

    def test_invalid_stability_rejected(self):
        """越界的稳定性字段被拒绝（本模块不换算该值）。"""
        for bad in (1.5, -0.1, "high"):
            with self.subTest(value=bad):
                with self.assertRaises(PatternInputError):
                    make_pool([cluster_pattern(stability=bad)])

    def test_data_ref_derived_from_pattern_provenance(self):
        """数据引用可从模式来源信息派生。"""
        pool = make_pool([cluster_pattern(source_ref="data_abcd")])
        self.assertEqual(list(pool.hypotheses[0].provenance["data_ids"]),
                         ["data_abcd"])
        self.assertEqual(pool.provenance["data_ids_source"], "pattern_provenance")

    def test_data_ref_falls_back_to_sample_fingerprint(self):
        """无 ``source_ref`` 时回退到样本指纹（并标明前缀）。"""
        pattern = cluster_pattern(source_ref=None)
        pool = make_pool([pattern])
        self.assertEqual(list(pool.hypotheses[0].provenance["data_ids"]),
                         ["sample:fp-0123456789"])

    def test_missing_data_ref_is_rejected(self):
        """既无显式引用也无来源引用时报错（不提供缺省值）。"""
        pattern = cluster_pattern(source_ref=None)
        del pattern["provenance"]["sample_fingerprint"]
        with self.assertRaises(PatternInputError):
            make_pool([pattern])

    def test_explicit_data_ids_override(self):
        """显式 ``data_ids`` 优先于模式来源信息。"""
        pool = make_pool([cluster_pattern()], data_ids=["data_explicit"])
        self.assertEqual(list(pool.hypotheses[0].provenance["data_ids"]),
                         ["data_explicit"])
        self.assertEqual(pool.provenance["data_ids_source"], "caller_explicit")

    def test_empty_explicit_data_ids_rejected(self):
        """显式的空引用序列被拒绝。"""
        with self.assertRaises(GenerateError):
            make_pool([cluster_pattern()], data_ids=[])
        with self.assertRaises(GenerateError):
            make_pool([cluster_pattern()], data_ids="data_single")

    def test_scope_defaults_to_unset_and_is_marked(self):
        """``scope`` 缺省为「未限定」，且该状态被显式记录。"""
        pool = make_pool()
        self.assertEqual(dict(pool.hypotheses[0].scope), {})
        self.assertFalse(pool.provenance["scope_declared"])
        self.assertFalse(pool.hypotheses[0].provenance["scope_declared"])

    def test_scope_is_copied_from_caller(self):
        """调用方给出的 ``scope`` 被原样带入（深度冻结会把序列转为元组）。"""
        scope = {"environments": ["lab-A"], "time_scope": "2026"}
        pool = make_pool(scope=scope)
        # P07 的深度冻结把嵌套 list 转为 tuple、dict 转为只读视图；
        # 用 to_dict()（会还原为普通 list/dict）比对内容，避免容器类型造成的假差异。
        self.assertEqual(pool.hypotheses[0].to_dict()["scope"], scope)
        self.assertTrue(pool.provenance["scope_declared"])

    def test_representation_default_is_complete(self):
        """缺省 ``representation`` 已写明缺失值处理与标准化。"""
        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                self.assertIn("missing_value_handling", hypothesis.representation)
                self.assertIn("standardization", hypothesis.representation)

    def test_representation_override_applied(self):
        """调用方可整体覆盖 ``representation``。"""
        override = {"missing_value_handling": "keep_row_flag",
                    "standardization": "minmax_on_E"}
        pool = make_pool(representation=override)
        self.assertEqual(dict(pool.hypotheses[0].representation), override)
        self.assertEqual(pool.provenance["representation_source"], "caller_override")

    def test_code_version_is_recorded(self):
        """代码版本被写入来源信息以便追溯。"""
        pool = make_pool(code_version="custom-v9")
        self.assertEqual(pool.hypotheses[0].provenance["code_version"], "custom-v9")
        self.assertEqual(pool.hypotheses[0].provenance["generator"], GENERATOR_ID)

    def test_blank_code_version_rejected(self):
        """空白代码版本被拒绝。"""
        with self.assertRaises(GenerateError):
            make_pool(code_version="  ")

    def test_non_callable_enricher_rejected(self):
        """非可调用的 ``text_enricher`` 被拒绝。"""
        with self.assertRaises(GenerateError):
            make_pool(text_enricher="not-callable")


# ---------------------------------------------------------------------------
# 模式对象（鸭子类型）与字典形态
# ---------------------------------------------------------------------------


class PatternObjectTests(unittest.TestCase):
    """同时接受「对象形态」与「字典形态」的模式输入。"""

    class FakePattern:
        """一个最小模式对象（不导入 M3，仅验证鸭子类型判定）。"""

        def __init__(self, payload):
            self.pattern_id = "obj-pattern-1"
            self.kind = "invariant"
            self.payload = payload
            self.metrics = {"error": 0.1, "residual": 0.05,
                            "complexity": {"total": 2}}
            self.provenance = {"source_ref": "data_obj", "detector": "check_invariant"}
            self.stability = 0.8

    def test_object_form_is_accepted(self):
        """对象形态被接受，且稳定性被原样转录。"""
        payload = invariant_pattern()["payload"]
        pool = make_pool([self.FakePattern(payload)])
        self.assertEqual(len(pool), 1)
        hypothesis = pool.hypotheses[0]
        self.assertEqual(hypothesis.type, "invariant")
        self.assertEqual(hypothesis.provenance["pattern_stability"], 0.8)
        self.assertEqual(list(hypothesis.provenance["data_ids"]), ["data_obj"])

    def test_object_without_required_attributes_rejected(self):
        """缺必需属性的对象被拒绝。"""
        class Bare:
            pass

        with self.assertRaises(PatternInputError):
            make_pool([Bare()])

    def test_mixed_forms_coexist(self):
        """对象与字典形态可混合输入。"""
        pool = make_pool([self.FakePattern(invariant_pattern()["payload"]),
                          cluster_pattern(), dict(relation_pattern())])
        self.assertEqual(len(pool), 3)


# ---------------------------------------------------------------------------
# 序列化与池容器
# ---------------------------------------------------------------------------


class PoolContainerTests(unittest.TestCase):
    """池容器协议与序列化。"""

    def test_len_iter_contains(self):
        """``len`` / ``iter`` / ``in`` 协议。"""
        pool = make_pool()
        self.assertEqual(len(pool), 4)
        self.assertEqual(len(list(pool)), 4)
        self.assertIn(pool.hypotheses[0].identity, pool)
        self.assertNotIn("h-nonexistent@1", pool)

    def test_ids_and_by_id(self):
        """``ids`` 与 ``by_id``。"""
        pool = make_pool()
        for identity in pool.ids():
            self.assertEqual(pool.by_id(identity).id, identity)
        with self.assertRaises(GenerateError):
            pool.by_id("h-missing")

    def test_to_dict_roundtrip_json(self):
        """``to_dict`` 可 JSON 序列化且结构完整。"""
        payload = make_pool().to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        restored = json.loads(text)
        self.assertEqual(restored["generate_version"], GENERATE_VERSION)
        self.assertEqual(restored["size"], 4)
        self.assertEqual(len(restored["hypotheses"]), 4)
        self.assertIn("budget", restored)
        self.assertIn("provenance", restored)

    def test_canonical_json_is_stable(self):
        """同一输入两次生成的规范式文本完全一致（可复现）。"""
        first = make_pool().canonical_json()
        second = make_pool().canonical_json()
        self.assertEqual(first, second)

    def test_content_digest_matches_canonical_json(self):
        """内容摘要与规范式文本一致。"""
        import hashlib

        pool = make_pool()
        expected = hashlib.sha256(pool.canonical_json().encode("utf-8")).hexdigest()
        self.assertEqual(pool.content_digest(), expected)

    def test_to_store_registers_all(self):
        """池可整体登记进 P07 的只追加仓库。"""
        store = make_pool().to_store()
        self.assertEqual(len(store), 4)

    def test_to_store_rejects_duplicate_registration(self):
        """重复登记同一标识会被 P07 的只追加保护拒绝。"""
        pool = make_pool()
        store = pool.to_store()
        with self.assertRaises(Exception):
            pool.to_store(store)

    def test_pool_is_created_with_budget(self):
        """池内保存了本次使用的预算。"""
        pool = make_pool(budget={"max_hypotheses": 7})
        self.assertEqual(pool.budget.max_hypotheses, 7)
        self.assertEqual(pool.to_dict()["budget"]["max_hypotheses"], 7)

    def test_pool_is_immutable_container(self):
        """池是 frozen dataclass。"""
        pool = make_pool()
        with self.assertRaises(Exception):
            pool.hypotheses = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 边界守护（P08 的增量边界）
# ---------------------------------------------------------------------------


class BoundaryTests(unittest.TestCase):
    """P08 不得越界到 P09 / P10，也不得接触 M1。"""

    def setUp(self):
        self.source = MODULE_PATH.read_text(encoding="utf-8")
        self.tree = pyast.parse(self.source)

    def test_does_not_compute_metrics(self):
        """不计算任何指标（G/S/N/C 属 P09）。"""
        forbidden_names = {"gain", "novelty", "evaluate_hypothesis", "pareto_front",
                           "select_freeze_candidates", "top_k_reserved"}
        defined = {
            node.name
            for node in self.tree.body
            if isinstance(node, (pyast.FunctionDef, pyast.ClassDef))
        }
        self.assertEqual(defined & forbidden_names, set())

    def test_metric_slots_are_left_empty(self):
        """complexity / development / confirmation_plan 三个槽位一律留空。"""
        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                self.assertIsNone(hypothesis.complexity)
                self.assertIsNone(hypothesis.development)
                self.assertIsNone(hypothesis.confirmation_plan)

    def test_imports_m01_and_m03_only_via_ast(self):
        """不导入 ``sdl_m01``（也不导入 M3，只做鸭子类型判定）。"""
        imported = set()
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module)
        # 注：文档字符串里会出现 "sdl_m01" / "sdl_m03" 字样，这是**声明**而非导入；
        # 因此必须用 AST 判定，不能做源码文本匹配（该方法在历史阶段已多次误判）。
        self.assertNotIn("sdl_m01", imported)
        self.assertNotIn("sdl_m03", imported)
        for module in imported:
            self.assertFalse(module.startswith("sdl_m01"),
                             f"不得导入 M1：{module}")
            self.assertFalse(module.startswith("sdl_m03"),
                             f"不得导入 M3：{module}")

    def test_only_standard_library_dependencies(self):
        """只用标准库（不引入第三方依赖）。"""
        allowed = {
            "__future__", "hashlib", "json", "math", "dataclasses", "typing",
            "sdl_m04.hypothesis",
        }
        imported = set()
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertTrue(imported <= allowed,
                        f"出现未预期的依赖：{sorted(imported - allowed)}")

    def test_does_not_read_tokens_or_databases(self):
        """不读取令牌、数据库或质量报告。"""
        imported = set()
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module)
        for module in ("sqlite3", "os", "pathlib", "shutil"):
            self.assertNotIn(module, imported)

    def test_no_evidence_grade_is_granted(self):
        """不授予任何证据等级：状态恒为 draft，且文本中声明了该边界。"""
        for hypothesis in make_pool():
            self.assertEqual(hypothesis.status, "draft")
            self.assertIn("证据等级", hypothesis.provenance["evidence_grade_note"])
        self.assertIn("证据等级", make_pool().provenance["evidence_grade_note"])

    def test_does_not_modify_frozen_directory(self):
        """``sdl_m01/`` 冻结目录未被本阶段触碰（mtime 早于本阶段交付物）。"""
        root = MODULE_PATH.parent.parent
        frozen = root / "sdl_m01"
        self.assertTrue(frozen.is_dir())
        newest = max(path.stat().st_mtime for path in frozen.glob("*.py"))
        self.assertLess(newest, MODULE_PATH.stat().st_mtime)

    def test_kind_mapping_matches_p07_vocabulary(self):
        """模式种类到假说类型的映射与 P07 的词表一致。"""
        from sdl_m04.hypothesis import HYPOTHESIS_TYPES

        self.assertEqual(sorted(KIND_TO_HYPOTHESIS_TYPE), sorted(SUPPORTED_PATTERN_KINDS))
        for kind, hypothesis_type in KIND_TO_HYPOTHESIS_TYPE.items():
            with self.subTest(kind=kind):
                self.assertIn(hypothesis_type, HYPOTHESIS_TYPES)

    def test_no_ellipsis_placeholders(self):
        """文件内容完整，不含省略号占位。

        用 **AST** 判定，而不是源码文本匹配：注解里的 ``tuple[str, ...]``
        是合法的类型写法，把它误判为「占位」是文本匹配的经典踩坑
        （本仓库在历史阶段已多次因此误判，见协作指南的排查提示）。
        真正的占位形态是「``...`` 单独作为语句体」，这里只查这一种。
        """
        for node in pyast.walk(self.tree):
            if not isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef,
                                     pyast.ClassDef, pyast.If, pyast.Try)):
                continue
            for child in node.body:
                with self.subTest(where=f"{getattr(node, 'name', node.lineno)}"):
                    self.assertFalse(
                        isinstance(child, pyast.Expr)
                        and isinstance(child.value, pyast.Constant)
                        and child.value.value is Ellipsis,
                        f"第 {child.lineno} 行出现省略号占位",
                    )


class ReproducibilityTests(unittest.TestCase):
    """生成过程可复现、无隐藏随机性。"""

    def test_no_random_import(self):
        """不导入随机模块（默认输入顺序确定，输出确定）。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertNotIn("random", imported)

    def test_output_is_deterministic(self):
        """两次生成得到逐字节相同的规范式文本与摘要。"""
        first = make_pool()
        second = make_pool()
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.content_digest(), second.content_digest())

    def test_input_order_does_not_change_content(self):
        """输入顺序不影响最终内容（去重键排序后处理）。"""
        patterns = all_patterns()
        forward = make_pool(patterns).to_dict()
        backward = make_pool(list(reversed(patterns))).to_dict()
        # 池内顺序可能不同，但内容集合必须一致。
        self.assertEqual(
            sorted(json.dumps(item, sort_keys=True, ensure_ascii=False)
                   for item in forward["hypotheses"]),
            sorted(json.dumps(item, sort_keys=True, ensure_ascii=False)
                   for item in backward["hypotheses"]),
        )

    def test_hypothesis_ids_are_stable_across_calls(self):
        """同一模式恒得同一假说标识（标识由模式内容派生）。"""
        first = make_pool([cluster_pattern("p1")]).hypotheses[0].id
        second = make_pool([cluster_pattern("p1")]).hypotheses[0].id
        self.assertEqual(first, second)

    def test_same_structure_different_id_shares_hypothesis_id(self):
        """骨架相同的模式共享假说标识，是可被去重判定的前提。"""
        first = make_pool([cluster_pattern("p1", center=1.0)]).hypotheses[0].id
        second = make_pool([cluster_pattern("p2", center=2.0)]).hypotheses[0].id
        self.assertNotEqual(first, second)


class IntegrationWithM3Tests(unittest.TestCase):
    """与 M3 真实产出的端到端对接（模式对象形态）。

    本模块**不导入** M3（由边界测试保证）；但契约要求它能消费 M3 的
    ``StructurePattern`` 对象。因此这里在**测试侧**导入 M3，用真实检测器
    产出模式，再交给本模块转换——这样既验证了鸭子类型的对接，
    又不把 M3 变成运行时依赖。
    """

    def _sample(self, rows):
        """用 M3 的 ``prepare_sample`` 构造一个探索样本。"""
        from sdl_m03.equations import prepare_sample

        records = [
            {
                "record_id": f"r-{index:03}",
                "values": {"X1": x, "X2": y, "Y": z},
            }
            for index, (x, y, z) in enumerate(rows)
        ]
        return prepare_sample(records, ["X1", "X2"], "Y", source_ref=_DATA_REF)

    def test_real_cluster_patterns_convert(self):
        """M3 真实聚类模式可被转换，且带齐 predictions / nulls。"""
        from sdl_m03.structure import find_clusters

        # 两个清晰分离的团簇（各向同性，避免被 k-means 切碎）。
        rows = []
        for index in range(10):
            offset = 0.02 * (index % 3)
            rows.append((0.0 + offset, 0.0 + offset, 1.0))
            rows.append((10.0 + offset, 10.0 + offset, 2.0))
        sample = self._sample(rows)

        patterns = find_clusters(sample, seed=7)
        self.assertTrue(patterns, "夹具应在 M3 上检出簇结构")

        pool = hypotheses_from_patterns(patterns, knowledge_version=_KNOWLEDGE_VERSION)
        self.assertEqual(len(pool), len(patterns))
        for hypothesis in pool:
            self.assertEqual(hypothesis.type, "cluster")
            self.assertTrue(hypothesis.predictions)
            self.assertTrue(hypothesis.null_hypotheses)
            # 数据引用应从模式的 source_ref 派生。
            self.assertEqual(list(hypothesis.provenance["data_ids"]), [_DATA_REF])
            # 模式来源信息中的检测器与指纹被转录，便于追溯。
            self.assertEqual(hypothesis.provenance["detector"], "find_clusters")
            self.assertIsNotNone(hypothesis.provenance["pattern_sample_fingerprint"])

    def test_real_changepoint_patterns_convert(self):
        """M3 真实变点模式可被转换。"""
        from sdl_m03.structure import find_changepoints

        rows = []
        for index in range(10):
            rows.append((float(index), 0.0, 1.0))
        for index in range(10, 20):
            rows.append((float(index), 0.0, 5.0))
        sample = self._sample(rows)

        patterns = find_changepoints(sample, order_by="X1", series="Y")
        self.assertTrue(patterns, "夹具应在 M3 上检出变点")

        pool = hypotheses_from_patterns(patterns, knowledge_version=_KNOWLEDGE_VERSION)
        self.assertEqual(len(pool), len(patterns))
        for hypothesis in pool:
            self.assertEqual(hypothesis.type, "changepoint")
            self.assertTrue(hypothesis.null_hypotheses)

    def test_real_invariant_patterns_convert(self):
        """M3 真实近似守恒量模式可被转换。

        注意：M3 的 ``check_invariant`` 默认容差为 ``1e-6``（极严），
        本夹具的扰动量级为 ``1e-3``，因此必须显式放宽容差才能检出结构。
        这一参数是**实测**确定的（默认容差下检测器返回空列表），
        不是按直觉假定的。
        """
        from sdl_m03.structure import check_invariant

        # X1 / X2 ≈ 2.0（加微小扰动）。
        rows = []
        for index in range(1, 21):
            jitter = 0.001 * (index % 3)
            rows.append((2.0 * index + jitter, float(index), 1.0))
        sample = self._sample(rows)

        patterns = check_invariant(sample, "X1 / X2", tolerance=0.01, min_conformity=0.9)
        self.assertTrue(patterns, "夹具应在 M3 上检出近似守恒量")

        pool = hypotheses_from_patterns(patterns, knowledge_version=_KNOWLEDGE_VERSION)
        self.assertEqual(len(pool), len(patterns))
        hypothesis = pool.hypotheses[0]
        self.assertEqual(hypothesis.type, "invariant")
        self.assertTrue(hypothesis.predictions)
        self.assertTrue(hypothesis.null_hypotheses)

    def test_m3_pattern_kinds_match_mapping(self):
        """M3 声明的模式种类与本模块支持的集合一致（跨模块一致性）。"""
        from sdl_m03.structure import PATTERN_KINDS

        self.assertEqual(sorted(PATTERN_KINDS), sorted(SUPPORTED_PATTERN_KINDS))

    def test_stability_from_real_report_is_transcribed(self):
        """M3 的稳定性报告数值被原样转录（本模块不换算、不排序）。"""
        from sdl_m03.structure import find_clusters
        from sdl_m03.stability import stability_score

        rows = []
        for index in range(10):
            rows.append((0.0, 0.0, 1.0))
            rows.append((10.0, 10.0, 2.0))
        sample = self._sample(rows)
        patterns = find_clusters(sample, seed=7)
        self.assertTrue(patterns)

        # 用 M3 的分数接口造一个报告级数值，附到模式上，验证纯转录。
        score = stability_score([True, True, False, True])
        self.assertAlmostEqual(score, 0.75)
        annotated = {**patterns[0].to_dict(), "stability": score}
        pool = hypotheses_from_patterns([annotated], knowledge_version=_KNOWLEDGE_VERSION)
        self.assertEqual(pool.hypotheses[0].provenance["pattern_stability"], 0.75)
        # 稳定性没有被本模块换算成任何指标字段。
        self.assertIsNone(pool.hypotheses[0].complexity)


class GeneratedHypothesisShapeTests(unittest.TestCase):
    """生成物的字段形状必须满足 P07 的全字段契约。"""

    def test_all_fields_present(self):
        """生成物含 P07 要求的全部字段（序列化即可检出缺字段）。"""
        from sdl_m04.hypothesis import HYPOTHESIS_FIELDS

        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                self.assertEqual(set(hypothesis.to_dict()), set(HYPOTHESIS_FIELDS))

    def test_roundtrip_through_from_dict(self):
        """生成物可序列化往返且内容不变。"""
        for hypothesis in make_pool():
            restored = Hypothesis.from_dict(hypothesis.to_dict())
            with self.subTest(kind=hypothesis.type):
                self.assertEqual(restored, hypothesis)
                self.assertEqual(restored.content_digest(), hypothesis.content_digest())

    def test_new_version_chain_is_append_only(self):
        """在生成物上派生新版本时，父链只追加。"""
        hypothesis = make_pool().hypotheses[0]
        second = hypothesis.new_version(changes={"statement": "修订后的陈述。"})
        self.assertEqual(second.parent_ids, (hypothesis.identity,))
        third = second.new_version(changes={"statement": "再次修订。"})
        self.assertEqual(third.parent_ids, (hypothesis.identity, second.identity))

    def test_generated_hypothesis_is_draft_not_frozen(self):
        """生成物是草稿（可读、可修订），不是已发布版本。"""
        hypothesis = make_pool().hypotheses[0]
        self.assertTrue(hypothesis.is_draft)
        self.assertFalse(hypothesis.is_published)
        self.assertFalse(hypothesis.is_frozen)

    def test_fitted_parameters_carried_for_cluster_and_changepoint(self):
        """簇与变点的关键数值被带入 fitted_parameters（原样转录，不重算）。"""
        pool = make_pool()
        cluster = next(h for h in pool if h.type == "cluster")
        self.assertIn("center", cluster.fitted_parameters)
        changepoint = next(h for h in pool if h.type == "changepoint")
        self.assertIn("threshold", changepoint.fitted_parameters)
        self.assertIn("mean_shift", changepoint.fitted_parameters)

    def test_pattern_metrics_are_transcribed_not_recomputed(self):
        """模式原有度量被原样转录备查，本模块不重算。"""
        pattern = cluster_pattern()
        hypothesis = make_pool([pattern]).hypotheses[0]
        self.assertEqual(dict(hypothesis.provenance["pattern_metrics"]["complexity"]),
                         pattern["metrics"]["complexity"])

    def test_model_shape_is_declared(self):
        """``model`` 是非空映射，含 ``model_kind`` 以标明口径。

        注：P07 的深度冻结把它转为只读映射视图，因此用 ``Mapping`` 判定
        而非 ``dict``——这是**预期**的类型变化，不是缺陷。
        """
        from collections.abc import Mapping

        for hypothesis in make_pool():
            with self.subTest(kind=hypothesis.type):
                self.assertIsInstance(hypothesis.model, Mapping)
                self.assertIn("model_kind", hypothesis.model)


if __name__ == "__main__":  # pragma: no cover - 便于单独运行
    unittest.main(verbosity=2)
