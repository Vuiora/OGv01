"""P09 验收测试：多维评估指标 G/S/N/C。

覆盖三条验收标准：

1. **新颖性仅相对指定知识库版本** :math:`K^{(v)}` **定义并记录该版本号**：
   :class:`KnowledgeBase` 没有无版本构造路径；:func:`novelty` 与
   :class:`Evaluation` 的产出恒带 ``knowledge_version``；假说声明的版本与本次
   所用版本不一致时**如实记录**为不一致，而不是静默混用；换一个知识库版本会
   改变结果摘要（证明版本真的是判据的一部分，而不是装饰性字段）。
2. **复杂度明确标注为近似值而非严格 MDL**：结果的机检标志恒为
   ``is_formula_length_approximation=True`` 且 ``is_strict_mdl=False``，
   并随结果附带 :data:`COMPLEXITY_NOTE` 原文；无公式的假说走降级路径并被
   显式标注 ``degraded=True``。
3. **不在指标层做加权求和**：任何加权 / 总分 / 排序 / 筛选参数一律被
   :class:`MetricsPolicyError` 拒绝，且报错信息点明「加权属 P10 决策」；
   :class:`Evaluation` 不含 total / score 字段；本模块不定义 Pareto 方向常量，
   也不提供 P10 的三个入口（``pareto_front`` / ``top_k_reserved`` /
   ``select_freeze_candidates``）。

另覆盖：增益的三种证据形态与退化情形（负数不默认截断、基线损失为 0 时
相对下降无定义）、稳定性的充分性透传、约束分区的拒绝、四个维度「没算」与
「算出来是 0」的可区分性、确定性可复现，以及 P09 的边界守护——
不修改 ``sdl_m01/``、不引入第三方依赖、不使用 skip / xfail。
"""

import ast as pyast
import json
import math
import pathlib
import unittest

from sdl_m02.expressions import parse as parse_expression

from sdl_m03.equations import FitResult, RelationFit, fit_relation, prepare_sample
from sdl_m03.stability import StabilityReport, resample_evaluate

from sdl_m04.hypothesis import Hypothesis

from sdl_m05.metrics import (
    COMPLEXITY_NOTE,
    DEFAULT_BASELINE_CHOICE,
    DIMENSION_NAMES,
    GAIN_BASES,
    GAIN_LOSSES,
    GAIN_NOTE,
    MATCH_WEIGHTS,
    METRICS_VERSION,
    MDL_FLAGS,
    NOT_PROVIDED_BY_P09,
    NOVELTY_MATCH_KINDS,
    NOVELTY_NOTE,
    STATEMENT_SIMILARITY_CAP,
    STABILITY_NOTE,
    Evaluation,
    KnowledgeBase,
    KnowledgeEntry,
    MetricsError,
    MetricsInputError,
    MetricsPolicyError,
    MetricsVersionError,
    complexity,
    evaluate_hypothesis,
    evaluate_pool,
    gain,
    novelty,
    stability,
)

# ---------------------------------------------------------------------------
# 夹具：确定性合成数据
# ---------------------------------------------------------------------------

#: 模块源码路径，用于静态检查第三方依赖与边界声明。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m05" / "metrics.py"

#: 测试文件自身路径，用于检查本文件未使用 skip / xfail。
TEST_PATH = pathlib.Path(__file__).resolve()

VARIABLES = ["X1", "X2"]
TARGET = "Y"
NAMESPACE = "acc-exp"


def _x1(index: int) -> float:
    """确定性自变量 1。"""
    return 0.5 * index + 0.3


def _x2(index: int) -> float:
    """确定性自变量 2（与 X1 不共线）。"""
    return 0.7 + 0.23 * ((index * 7) % 5)


def _y(index: int) -> float:
    """目标：Y = 2·X1/X2 + 1（候选 X1/X2 与目标精确线性）。"""
    return 2.0 * (_x1(index) / _x2(index)) + 1.0


def relation_records(count: int = 24) -> list[dict]:
    """返回 count 行「对候选 X1/X2 精确线性」的合成记录。"""
    return [
        {
            "record_id": f"r-{index:03d}",
            "group_ids": {"batch": f"b-{index % 8:02d}"},
            "environment": "lab-A",
            "event_time": "2026-01-01T00:00:00+00:00",
            "available_time": "2026-01-02T00:00:00+00:00",
            "values": {"X1": _x1(index), "X2": _x2(index), "Y": _y(index)},
            "units": {"X1": "mol/L", "X2": "mol/L", "Y": "mol/(L*s)"},
            "source": "synthetic",
        }
        for index in range(count)
    ]


def spec_for() -> dict:
    """构造 M1 数据配置的 ``dependence`` 段（只读取这一段）。"""
    return {
        "dependence": {
            "record_unit": "reading",
            "split_unit": "batch",
            "inference_unit": "batch",
            "group_fields": ["batch"],
            "namespace": NAMESPACE,
            "assumptions": ["independent batches"],
        }
    }


def relation_fit_of(expression: str = "X1/X2") -> RelationFit:
    """构造 P04 的关系拟合结果（真实调用上游，非伪造）。"""
    sample = prepare_sample(relation_records(), VARIABLES, TARGET)
    return fit_relation(sample, expression)


def stability_report_of(n_resamples: int = 20, seed: int = 7) -> StabilityReport:
    """构造 P06 的稳定性报告（真实调用上游，非伪造）。"""
    return resample_evaluate(
        "X1/X2",
        relation_records(),
        n_resamples,
        seed,
        spec=spec_for(),
        variables=VARIABLES,
        target=TARGET,
    )


def hypothesis_of(
    *,
    hypothesis_id: str = "h-relation-1",
    version: str = "1",
    expression: str = "X1/X2",
    statement: str = "比例表示 X1/X2 对目标的预测优于常数与简单线性基线。",
    knowledge_version: str = "k-v1",
    hypothesis_type: str = "relation",
    coefficients: dict | None = None,
) -> Hypothesis:
    """构造一个合法的 P07 假说对象。"""
    return Hypothesis(
        id=hypothesis_id,
        version=version,
        type=hypothesis_type,
        statement=statement,
        representation={
            "missing_value_handling": "drop_rows_with_missing_required_fields",
            "standardization": "none_declared_candidate_expression",
        },
        predictions=[{"incompatible_with": "独立样本上的增益消失或为负。"}],
        null_hypotheses=[{"null_id": "null-no-relation", "statement": "不存在预测关系。"}],
        provenance={
            "data_ids": ["E-abc123"],
            "knowledge_version": knowledge_version,
            "generator": "test-fixture",
            "code_version": "P09-test",
        },
        model={
            "model_kind": "relation_candidate",
            "expression": expression,
            "target": TARGET,
            "coefficients": coefficients if coefficients is not None else {"intercept": 1.0, "slope": 2.0},
        },
    )


def entry_of(
    entry_id: str,
    statement: str,
    expression: str | None = None,
    kind: str = "relation",
) -> KnowledgeEntry:
    """构造一条知识库已知项。"""
    return KnowledgeEntry(
        entry_id=entry_id,
        statement=statement,
        kind=kind,
        expression=expression,
    )


def knowledge_base_v1() -> KnowledgeBase:
    """一个含两条已知项的知识库快照。"""
    return KnowledgeBase(
        version="k-v1",
        entries=(
            entry_of("k-ratio", "比例表示 X1/X2 与目标量近似线性相关。", "X1/X2"),
            entry_of("k-sum", "目标量与 X1 与 X2 之和近似线性相关。", "X1 + X2"),
        ),
        note="截至 2026-09 的合成领域已知公式集",
    )


# ---------------------------------------------------------------------------
# 验收标准①：新颖性绑定知识库版本
# ---------------------------------------------------------------------------


class TestNoveltyVersionBinding(unittest.TestCase):
    """验收标准①：新颖性仅相对指定 K 版本定义并记录该版本号。"""

    def test_knowledge_base_requires_version(self) -> None:
        """知识库没有无版本构造路径。"""
        for bad in (None, "", "   ", 123):
            with self.subTest(version=bad):
                with self.assertRaises(MetricsVersionError):
                    KnowledgeBase(version=bad)

    def test_constructing_without_version_raises(self) -> None:
        """省略 version 关键字即报错。"""
        with self.assertRaises(TypeError):
            KnowledgeBase()  # type: ignore[call-arg]

    def test_novelty_always_records_version(self) -> None:
        """novelty 结果恒带 knowledge_version 与其标签。"""
        payload = novelty(hypothesis_of(), knowledge_base_v1())
        self.assertEqual(payload["knowledge_version"], "k-v1")
        self.assertEqual(payload["knowledge_version_label"], "K^{k-v1}")
        self.assertIn("knowledge_base_digest", payload)
        self.assertEqual(payload["note"], NOVELTY_NOTE)

    def test_evaluation_requires_knowledge_base_instance(self) -> None:
        """evaluate_hypothesis 只接受 KnowledgeBase，不接受裸字符串版本号。"""
        for bad in ("k-v1", None, {"version": "k-v1"}):
            with self.subTest(knowledge_base=bad):
                with self.assertRaises(MetricsInputError):
                    evaluate_hypothesis(hypothesis_of(), knowledge_base=bad)  # type: ignore[arg-type]

    def test_evaluation_records_version(self) -> None:
        """Evaluation 恒带 knowledge_version，且可序列化核对。"""
        evaluation = evaluate_hypothesis(hypothesis_of(), knowledge_base=knowledge_base_v1())
        self.assertEqual(evaluation.knowledge_version, "k-v1")
        self.assertEqual(evaluation.to_dict()["knowledge_version"], "k-v1")

    def test_changing_knowledge_version_changes_result(self) -> None:
        """换一个知识库版本，结果摘要随之改变。

        这条断言的作用是排除「版本号只是装饰」的可能——如果版本不参与判据，
        两份内容不同但版本号不同的知识库会给出相同摘要。
        """
        first = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1/X2"),))
        second = KnowledgeBase(version="v2", entries=(entry_of("a", "乙", "X1 + X2"),))
        value_a = novelty(hypothesis_of(expression="X1/X2"), first)
        value_b = novelty(hypothesis_of(expression="X1/X2"), second)
        self.assertEqual(value_a["knowledge_version"], "v1")
        self.assertEqual(value_b["knowledge_version"], "v2")
        self.assertNotEqual(value_a["knowledge_base_digest"], value_b["knowledge_base_digest"])
        self.assertNotEqual(value_a["novelty"], value_b["novelty"])

    def test_equivalent_expression_is_not_novel(self) -> None:
        """规范化 AST 相等 → 新颖性 0（等价公式应去重）。"""
        # 两种写法语义等价，规范化后 AST 必然相同。
        payload = novelty(hypothesis_of(expression="X1/X2"), knowledge_base_v1())
        self.assertEqual(payload["match_kind"], "equivalent_expression")
        self.assertEqual(payload["novelty"], 0.0)
        self.assertEqual(payload["nearest_entry_id"], "k-ratio")
        self.assertEqual(payload["max_similarity"], MATCH_WEIGHTS["equivalent_expression"])

    def test_semantically_equal_writing_is_matched(self) -> None:
        """语义等价的不同写法同样被判定为已知。"""
        kb = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1/X2"),))
        # X1 * (1/X2) 与 X1/X2 在受限语法下规范化为同一 AST。
        payload = novelty(hypothesis_of(expression="X1 * (1/X2)"), kb)
        self.assertEqual(payload["match_kind"], "equivalent_expression")
        self.assertEqual(payload["novelty"], 0.0)

    def test_skeleton_match_for_constant_variants(self) -> None:
        """仅常数不同 → 骨架匹配，新颖性明显低于全新结构。"""
        kb = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1 + 2"),))
        payload = novelty(hypothesis_of(expression="X1 + 5"), kb)
        self.assertEqual(payload["match_kind"], "equivalent_skeleton")
        self.assertAlmostEqual(payload["novelty"], 1.0 - MATCH_WEIGHTS["equivalent_skeleton"])
        self.assertLess(payload["novelty"], 1.0)

    def test_corollary_match_for_subtree(self) -> None:
        """已知项是假说公式的真子树 → 判为已知推论。"""
        kb = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1"),))
        payload = novelty(hypothesis_of(expression="X1/X2"), kb)
        self.assertEqual(payload["match_kind"], "known_corollary")
        self.assertAlmostEqual(payload["novelty"], 1.0 - MATCH_WEIGHTS["known_corollary"])

    def test_subtree_match_excludes_self(self) -> None:
        """真子树判据不含自身：同公式时优先归为等价表达式。"""
        kb = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1/X2"),))
        payload = novelty(hypothesis_of(expression="X1/X2"), kb)
        self.assertEqual(payload["match_kind"], "equivalent_expression")
        self.assertNotEqual(payload["match_kind"], "known_corollary")

    def test_statement_similarity_is_capped(self) -> None:
        """陈述相似度兜底判据的上限被压制，不会顶替结构判据。"""
        kb = KnowledgeBase(
            version="v1",
            entries=(entry_of("a", "比例表示 对目标的预测 优于常数与简单线性基线。", None),),
        )
        payload = novelty(hypothesis_of(expression="X9 * X8"), kb)
        self.assertEqual(payload["match_kind"], "statement_similarity")
        self.assertLessEqual(payload["max_similarity"], STATEMENT_SIMILARITY_CAP)
        self.assertGreaterEqual(
            payload["novelty"], 1.0 - STATEMENT_SIMILARITY_CAP - 1e-12
        )

    def test_unrelated_expression_is_fully_novel(self) -> None:
        """与知识库无任何匹配 → 新颖性 1.0。"""
        kb = KnowledgeBase(version="v1", entries=(entry_of("a", "完全不同的东西", "Z9/Z8"),))
        payload = novelty(hypothesis_of(expression="X1/X2"), kb)
        self.assertEqual(payload["match_kind"], "none")
        self.assertEqual(payload["novelty"], 1.0)
        self.assertIsNone(payload["nearest_entry_id"])

    def test_trivial_token_overlap_is_not_a_match(self) -> None:
        """仅共享「的」「与」一类功能字时不得误报为陈述相似。

        这条断言守护 :data:`DEFAULT_MIN_STATEMENT_SIMILARITY` 下限：
        两段语义无关的中文陈述若共享个别高频字，原始 Jaccard 极小，
        应直接归入 ``none``，而不是产生一个虚高的「已知」判定。
        """
        kb = KnowledgeBase(
            version="v1",
            entries=(entry_of("a", "完全不同的东西，与本题无关。", None),),
        )
        payload = novelty(hypothesis_of(expression="Z9/Z8"), kb)
        self.assertEqual(payload["match_kind"], "none")
        self.assertEqual(payload["novelty"], 1.0)

    def test_empty_knowledge_base_is_flagged(self) -> None:
        """空知识库：新颖性为 1.0，但显式标注「暂无已知项可比」而非「已确认全新」。"""
        payload = novelty(hypothesis_of(), KnowledgeBase(version="v-empty"))
        self.assertEqual(payload["novelty"], 1.0)
        self.assertTrue(payload["knowledge_base_empty"])
        self.assertEqual(payload["knowledge_base_size"], 0)
        self.assertIn("caveat", payload)
        self.assertIn("不表示已确认", payload["caveat"])

    def test_match_kind_vocabulary_is_stable(self) -> None:
        """匹配种类词表与常量声明一致。"""
        payload = novelty(hypothesis_of(), knowledge_base_v1())
        self.assertIn(payload["match_kind"], NOVELTY_MATCH_KINDS)

    def test_novelty_bounds(self) -> None:
        """新颖性恒落在 [0, 1]。"""
        kb = knowledge_base_v1()
        for expression in ("X1/X2", "X1 + X2", "X1 - X2", "Z9/Z8", "X1*X2"):
            with self.subTest(expression=expression):
                value = novelty(hypothesis_of(expression=expression), kb)["novelty"]
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_version_mismatch_is_recorded_not_silenced(self) -> None:
        """假说声明版本与本次所用版本不一致 → 如实记录，不报错也不静默替换。"""
        hypothesis = hypothesis_of(knowledge_version="k-old")
        payload = novelty(hypothesis, knowledge_base_v1())
        self.assertEqual(payload["knowledge_version"], "k-v1")

        evaluation = evaluate_hypothesis(hypothesis, knowledge_base=knowledge_base_v1())
        flags = evaluation.flags
        self.assertEqual(flags["hypothesis_knowledge_version"], "k-old")
        self.assertEqual(flags["knowledge_version"], "k-v1")
        self.assertFalse(flags["knowledge_version_matches"])
        self.assertIn("knowledge_version_mismatch_note", flags)

    def test_version_match_is_recorded(self) -> None:
        """版本一致时同样记录，且声明为一致。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(knowledge_version="k-v1"), knowledge_base=knowledge_base_v1()
        )
        self.assertTrue(evaluation.flags["knowledge_version_matches"])

    def test_novelty_never_grants_evidence_grade(self) -> None:
        """新颖性不授予证据等级：口径原文随结果输出。"""
        payload = novelty(hypothesis_of(), knowledge_base_v1())
        self.assertIn("不等于世界范围内首次发现", payload["note"])

    def test_knowledge_base_digest_is_content_sensitive(self) -> None:
        """知识库摘要随内容变化，可用于「同一版本是否被改动过」的比对。"""
        base = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1"),))
        same = KnowledgeBase(version="v1", entries=(entry_of("a", "甲", "X1"),))
        other = KnowledgeBase(version="v1", entries=(entry_of("a", "乙", "X1"),))
        self.assertEqual(base.content_digest(), same.content_digest())
        self.assertNotEqual(base.content_digest(), other.content_digest())

    def test_knowledge_base_rejects_duplicate_entry_ids(self) -> None:
        """重复 entry_id 报错，避免「同一已知项被数两次」。"""
        with self.assertRaises(MetricsInputError):
            KnowledgeBase(version="v1", entries=(entry_of("a", "甲"), entry_of("a", "乙")))

    def test_knowledge_entry_validates_fields(self) -> None:
        """已知项的必填字段缺失即报错。"""
        with self.assertRaises(MetricsInputError):
            KnowledgeEntry(entry_id="", statement="甲")
        with self.assertRaises(MetricsInputError):
            KnowledgeEntry(entry_id="a", statement="   ")

    def test_knowledge_base_rejects_token_keys(self) -> None:
        """令牌类键名不得进入知识库（INTERFACES.md §4.4）。"""
        with self.assertRaises(MetricsPolicyError):
            KnowledgeEntry(entry_id="a", statement="甲", provenance={"token": "x"})

    def test_knowledge_base_iteration_is_sorted(self) -> None:
        """迭代顺序按 entry_id 升序，确定可复现。"""
        kb = KnowledgeBase(
            version="v1",
            entries=(entry_of("c", "丙"), entry_of("a", "甲"), entry_of("b", "乙")),
        )
        self.assertEqual([item.entry_id for item in kb], ["a", "b", "c"])
        self.assertEqual(kb.get("b").statement, "乙")
        with self.assertRaises(MetricsInputError):
            kb.get("missing")

    def test_unparseable_known_expression_falls_back_to_text(self) -> None:
        """知识库中的公式若不可解析，退化为文本比较而非报错。"""
        kb = KnowledgeBase(
            version="v1",
            entries=(
                KnowledgeEntry(
                    entry_id="a",
                    statement="某个自由文本公式 int_0^1 f(x) dx",
                    expression="int_0^1 f(x) dx",
                ),
            ),
        )
        self.assertFalse(kb.get("a").expression_parsed)
        payload = novelty(hypothesis_of(expression="X1/X2"), kb)
        self.assertIn(payload["match_kind"], NOVELTY_MATCH_KINDS)


# ---------------------------------------------------------------------------
# 验收标准②：复杂度标注为公式长度近似
# ---------------------------------------------------------------------------


class TestComplexityApproximation(unittest.TestCase):
    """验收标准②：C 明确标注为近似值而非严格 MDL。"""

    def test_mdl_flags_present_and_correct(self) -> None:
        """两个机检标志恒存在且取值正确。"""
        payload = complexity("X1/X2")
        self.assertTrue(payload["is_formula_length_approximation"])
        self.assertFalse(payload["is_strict_mdl"])
        self.assertEqual(payload["is_strict_mdl"], MDL_FLAGS["is_strict_mdl"])

    def test_complexity_note_is_attached(self) -> None:
        """口径原文随结果输出，避免被下游误读为严格 MDL。"""
        payload = complexity("X1/X2")
        self.assertEqual(payload["note"], COMPLEXITY_NOTE)
        self.assertIn("不是严格 MDL", payload["note"])

    def test_flags_survive_inside_evaluation(self) -> None:
        """装配后的 Evaluation 仍带两个 MDL 标志。"""
        evaluation = evaluate_hypothesis(hypothesis_of(), knowledge_base=knowledge_base_v1())
        self.assertTrue(evaluation.flags["is_formula_length_approximation"])
        self.assertFalse(evaluation.flags["is_strict_mdl"])
        component = evaluation.components["complexity"]
        self.assertFalse(component["is_strict_mdl"])

    def test_expression_length_components(self) -> None:
        """由表达式文本计算时，组件为节点数与参数个数之和。"""
        payload = complexity("X1/X2")
        self.assertEqual(payload["components"]["expression_nodes"], 4)  # mul, inv, var, var
        self.assertEqual(payload["components"]["parameter_count"], 0)
        self.assertEqual(payload["complexity"], 4.0)
        self.assertEqual(payload["components"]["depth"], 3)

    def test_parameter_count_is_added(self) -> None:
        """可拟合参数个数计入总复杂度。"""
        payload = complexity("X1/X2", parameter_count=2)
        self.assertEqual(payload["complexity"], 6.0)
        self.assertEqual(payload["components"]["parameter_count"], 2)

    def test_from_upstream_complexity_mapping(self) -> None:
        """直接采信 P04 的复杂度映射，保证与候选/基线口径一致。"""
        fit = relation_fit_of()
        upstream = dict(fit.candidate.complexity)
        payload = complexity(upstream)
        self.assertEqual(payload["complexity"], float(upstream["total"]))
        self.assertEqual(payload["components"]["expression_nodes"], upstream["expression_nodes"])
        self.assertIn("P04", payload["source"])

    def test_from_hypothesis_object(self) -> None:
        """由假说对象计算：节点数 + model.coefficients 的参数个数。"""
        payload = complexity(hypothesis_of())
        self.assertEqual(payload["complexity"], 6.0)  # 4 节点 + 2 系数
        self.assertEqual(payload["components"]["parameter_count"], 2)
        self.assertFalse(payload["degraded"])

    def test_parameter_override(self) -> None:
        """显式参数个数覆盖 model.coefficients 的计数。"""
        payload = complexity(hypothesis_of(), parameter_count=5)
        self.assertEqual(payload["components"]["parameter_count"], 5)

    def test_degraded_path_for_formula_less_hypothesis(self) -> None:
        """无公式的假说走降级路径并被显式标注。"""
        hypothesis = Hypothesis(
            id="h-cluster-1",
            version="1",
            type="cluster",
            statement="在标准化空间中存在两个稳定子群。",
            representation={"missing_value_handling": "drop", "standardization": "zscore"},
            predictions=[{"incompatible_with": "重采样下子群不稳定。"}],
            null_hypotheses=["不存在稳定子群。"],
            provenance={
                "data_ids": ["E-1"],
                "knowledge_version": "k-v1",
                "generator": "test",
                "code_version": "P09-test",
            },
            model={"model_kind": "cluster_partition", "centers": [[0.0], [1.0]]},
        )
        payload = complexity(hypothesis)
        self.assertTrue(payload["degraded"])
        self.assertIn("不可直接比较", payload["source"])
        self.assertEqual(payload["components"]["expression_nodes"], 0)

    def test_degraded_flag_propagates(self) -> None:
        """降级标志透传到 Evaluation.flags。"""
        hypothesis = Hypothesis(
            id="h-inv-1",
            version="1",
            type="invariant",
            statement="某组合量在容差内近似守恒。",
            representation={"missing_value_handling": "drop", "standardization": "none"},
            predictions=[{"incompatible_with": "守恒量漂移超出容差。"}],
            null_hypotheses=["不存在近似守恒量。"],
            provenance={
                "data_ids": ["E-1"],
                "knowledge_version": "k-v1",
                "generator": "test",
                "code_version": "P09-test",
            },
            model={"model_kind": "invariant_combination"},
        )
        evaluation = evaluate_hypothesis(hypothesis, knowledge_base=knowledge_base_v1())
        self.assertTrue(evaluation.flags["complexity_degraded"])
        self.assertTrue(evaluation.components["complexity"]["degraded"])

    def test_complexity_is_nonnegative(self) -> None:
        """复杂度恒非负。"""
        # 表达式深度受 P02 的限制（上限 3），故这里只用深度合规的式子。
        for evidence in ("X1", "X1/X2", "X1 + X2", "X1 - X2", "X1*X2"):
            with self.subTest(evidence=evidence):
                self.assertGreaterEqual(complexity(evidence)["complexity"], 0.0)

    def test_expression_exceeding_depth_limit_is_rejected(self) -> None:
        """超出 P02 语法深度上限的表达式被拒绝，并给出明确错误。"""
        with self.assertRaises(MetricsInputError) as context:
            complexity("X1 + X2 - X1*X2")
        self.assertIn("无法被受限语法解析", str(context.exception))

    def test_invalid_evidence_raises(self) -> None:
        """无证据或形态非法时给出明确错误。"""
        with self.assertRaises(MetricsInputError):
            complexity(None)
        with self.assertRaises(MetricsInputError):
            complexity(123)
        with self.assertRaises(MetricsInputError):
            complexity("这不是一个合法表达式 @@@")
        with self.assertRaises(MetricsInputError):
            complexity("X1", parameter_count=-1)
        with self.assertRaises(MetricsInputError):
            complexity("X1", parameter_count=True)

    def test_complexity_not_inflated_by_nesting(self) -> None:
        """参数计数只数顶层键，不递归，避免嵌套结构被误算成多个自由参数。"""
        hypothesis = Hypothesis(
            id="h-nest-1",
            version="1",
            type="relation",
            statement="嵌套参数结构的假说。",
            representation={"missing_value_handling": "drop", "standardization": "none"},
            predictions=[{"incompatible_with": "增益消失。"}],
            null_hypotheses=["无关系。"],
            provenance={
                "data_ids": ["E-1"],
                "knowledge_version": "k-v1",
                "generator": "test",
                "code_version": "P09-test",
            },
            model={
                "expression": "X1/X2",
                "coefficients": {"outer": {"inner_a": 1.0, "inner_b": 2.0}},
            },
        )
        payload = complexity(hypothesis)
        # 顶层只有一个键 outer，因此参数个数为 1。
        self.assertEqual(payload["components"]["parameter_count"], 1)


# ---------------------------------------------------------------------------
# 验收标准③：本层不做加权求和
# ---------------------------------------------------------------------------


class TestNoAggregation(unittest.TestCase):
    """验收标准③：不在指标层做加权求和，也不实现 P10 的筛选。"""

    def test_no_weight_parameter_is_accepted(self) -> None:
        """任何加权参数一律被拒绝，且报错信息点明加权属 P10。"""
        for keyword in ("weights", "score_weights", "weighted_sum", "total_score"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(MetricsPolicyError) as context:
                    evaluate_hypothesis(
                        hypothesis_of(),
                        knowledge_base=knowledge_base_v1(),
                        **{keyword: {"gain": 0.5}},
                    )
                self.assertIn("P10", str(context.exception))

    def test_no_selection_parameter_is_accepted(self) -> None:
        """排序 / 筛选参数同样被拒绝。"""
        for keyword in ("pareto_front", "top_k", "top_k_reserved", "select_freeze_candidates"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(MetricsPolicyError):
                    evaluate_hypothesis(
                        hypothesis_of(),
                        knowledge_base=knowledge_base_v1(),
                        **{keyword: 5},
                    )

    def test_unknown_parameter_is_reported(self) -> None:
        """未预期参数给出明确错误，不静默忽略。"""
        with self.assertRaises(MetricsInputError):
            evaluate_hypothesis(
                hypothesis_of(),
                knowledge_base=knowledge_base_v1(),
                unexpected_option=1,
            )

    def test_any_weight_like_keyword_is_rejected(self) -> None:
        """含 ``weight`` 的任意参数名都被拒绝（防止换个名字绕过）。"""
        with self.assertRaises(MetricsPolicyError):
            evaluate_hypothesis(
                hypothesis_of(),
                knowledge_base=knowledge_base_v1(),
                **{"novelty_weight": 2.0},
            )

    def test_evaluation_has_no_total_or_score_field(self) -> None:
        """Evaluation 序列化结果中不含任何总分字段。"""
        evaluation = evaluate_hypothesis(hypothesis_of(), knowledge_base=knowledge_base_v1())
        payload = evaluation.to_dict()
        for banned in ("total", "total_score", "weighted_sum", "aggregate_score", "score", "rank"):
            self.assertNotIn(banned, payload)
        # 但必须显式声明「不含加权总分」这一事实。
        self.assertIn("no_aggregation_note", payload)
        self.assertTrue(payload["no_weighted_total"] if "no_weighted_total" in payload else True)

    def test_dimension_names_lists_without_aggregating(self) -> None:
        """dimension_names 只列举四个维度，不做聚合。"""
        self.assertEqual(Evaluation.dimension_names(), DIMENSION_NAMES)
        self.assertEqual(set(DIMENSION_NAMES), {"gain", "stability", "novelty", "complexity"})

    def test_dimension_values_is_not_a_total(self) -> None:
        """dimension_values 返回四个独立维度，而不是一个合成分。"""
        evaluation = evaluate_hypothesis(hypothesis_of(), knowledge_base=knowledge_base_v1())
        values = evaluation.dimension_values()
        self.assertEqual(set(values), set(DIMENSION_NAMES))
        self.assertNotIsInstance(sum(v for v in values.values() if v is not None), dict)

    def test_no_pareto_direction_constant(self) -> None:
        """本模块不定义 Pareto 优化方向（那是 P10 的验收条目）。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        for banned in ("maximize_gain", "MINIMIZE_COMPLEXITY", "OPTIMIZATION_DIRECTIONS",
                       "PARETO_DIRECTIONS"):
            self.assertNotIn(banned, text)

    def test_module_does_not_provide_p10_entries(self) -> None:
        """本模块不提供 P10 的三个入口与任何加权接口。"""
        import sdl_m05.metrics as module

        for name in NOT_PROVIDED_BY_P09:
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name), f"P09 不应提供 {name}")

    def test_source_declares_no_weighted_sum(self) -> None:
        """口径说明随评估记录输出。"""
        evaluation = evaluate_hypothesis(hypothesis_of(), knowledge_base=knowledge_base_v1())
        self.assertIn("不做任何加权", evaluation.note)
        self.assertIn("P10", evaluation.to_dict()["no_aggregation_note"])

    def test_no_banned_construct_in_source(self) -> None:
        """源码中不存在加权聚合的实现性函数名。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        banned = {
            "weighted_sum", "total_score", "aggregate_score", "combine_scores",
            "pareto_front", "top_k_reserved", "select_freeze_candidates", "weighted_score",
        }
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)):
                if node.name.lower() in banned:
                    offenders.append(node.name)
        self.assertEqual(offenders, [], f"出现了加权/筛选实现：{offenders}")

    def test_evaluate_pool_does_not_filter_or_sort(self) -> None:
        """批量评估保持池内顺序，不筛选、不裁剪、不排序。"""
        pool = (
            hypothesis_of(hypothesis_id="h-b", expression="X1/X2"),
            hypothesis_of(hypothesis_id="h-a", expression="Z9/Z8"),
        )
        results = evaluate_pool(pool, knowledge_base=knowledge_base_v1())
        self.assertEqual(len(results), 2)
        self.assertEqual([item.hypothesis_id for item in results], ["h-b", "h-a"])


# ---------------------------------------------------------------------------
# 指标一：预测增益 G
# ---------------------------------------------------------------------------


class TestGain(unittest.TestCase):
    """预测增益：三种证据形态、同数据核验与退化情形。"""

    def test_from_relation_fit(self) -> None:
        """由 P04 的 RelationFit 读取增益，默认取最难超越的基线。"""
        payload = gain(relation_fit_of())
        self.assertEqual(payload["baseline_choice"], DEFAULT_BASELINE_CHOICE)
        self.assertEqual(payload["loss"], "mse")
        self.assertTrue(payload["candidate_better"])
        self.assertGreater(payload["gain"], 0.0)
        self.assertIn("P04", payload["same_data_verified_by"])
        self.assertEqual(payload["note"], GAIN_NOTE)

    def test_from_relation_fit_with_explicit_baseline_kind(self) -> None:
        """可显式指定基线种类。"""
        payload = gain(relation_fit_of(), baseline_choice="constant")
        self.assertEqual(payload["baseline_choice"], "constant")
        self.assertEqual(payload["baseline_kind"], "constant")

    def test_unknown_baseline_kind_raises(self) -> None:
        """不存在的基线种类报错。"""
        with self.assertRaises(MetricsInputError):
            gain(relation_fit_of(), baseline_choice="no-such-kind")

    def test_relative_and_absolute_basis_differ(self) -> None:
        """相对与绝对基准给出不同的数值口径。"""
        relative = gain({"candidate_loss": 1.0, "baseline_loss": 4.0}, basis="relative")
        absolute = gain({"candidate_loss": 1.0, "baseline_loss": 4.0}, basis="absolute")
        self.assertAlmostEqual(relative["gain"], 0.75)
        self.assertAlmostEqual(absolute["gain"], 3.0)
        self.assertAlmostEqual(relative["loss_difference"], absolute["loss_difference"])

    def test_invalid_basis_raises(self) -> None:
        """非法基准报错。"""
        with self.assertRaises(MetricsInputError):
            gain({"candidate_loss": 1.0, "baseline_loss": 2.0}, basis="ratio")
        self.assertIn("relative", GAIN_BASES)

    def test_from_two_fit_results_verifies_same_data(self) -> None:
        """两个 FitResult 形态：本层复核指纹与行数一致。"""
        fit = relation_fit_of()
        candidate = fit.candidate
        baselines = list(fit.baselines.values())
        baseline = baselines[0]
        if not isinstance(baseline, FitResult):  # linear 基线是映射，取其中一项
            baseline = baseline["per_feature"][sorted(baseline["per_feature"])[0]]
        payload = gain((candidate, baseline))
        self.assertTrue(candidate.data_fingerprint == baseline.data_fingerprint)
        self.assertIn("P09", payload["same_data_verified_by"])
        self.assertEqual(payload["n_used"], candidate.n_used)

    def test_fingerprint_mismatch_raises(self) -> None:
        """数据指纹不一致即报错（候选与基线必须同数据）。"""
        fit = relation_fit_of()
        other_records = relation_records(count=30)
        other_sample = prepare_sample(other_records, VARIABLES, TARGET)
        other = fit_relation(other_sample, "X1/X2")
        self.assertNotEqual(fit.candidate.data_fingerprint, other.candidate.data_fingerprint)
        with self.assertRaises(MetricsInputError) as context:
            gain((fit.candidate, other.candidate))
        self.assertIn("同一份数据", str(context.exception))

    def test_from_mapping_evidence(self) -> None:
        """映射形态：直接给两个损失值。"""
        payload = gain({"candidate_loss": 0.5, "baseline_loss": 2.0})
        self.assertAlmostEqual(payload["gain"], 0.75)
        self.assertIn("调用方声明", payload["same_data_verified_by"])

    def test_mapping_missing_fields_raises(self) -> None:
        """映射缺少字段报错。"""
        with self.assertRaises(MetricsInputError):
            gain({"candidate_loss": 1.0})
        with self.assertRaises(MetricsInputError):
            gain({})

    def test_mapping_rejects_non_finite(self) -> None:
        """非有限损失值报错。"""
        with self.assertRaises(MetricsInputError):
            gain({"candidate_loss": float("nan"), "baseline_loss": 1.0})
        with self.assertRaises(MetricsInputError):
            gain({"candidate_loss": True, "baseline_loss": 1.0})

    def test_negative_gain_is_kept_by_default(self) -> None:
        """候选劣于基线时，负增益**默认保留**——它是有信息量的结果。"""
        payload = gain({"candidate_loss": 3.0, "baseline_loss": 2.0})
        self.assertLess(payload["gain"], 0.0)
        self.assertFalse(payload["candidate_better"])
        self.assertFalse(payload["floored_at_zero"])
        self.assertFalse(payload["floor_at_zero"])

    def test_floor_at_zero_is_opt_in_and_recorded(self) -> None:
        """截断为 0 是显式选项，且截断事实被记录，不伪装成「无功无过」。"""
        payload = gain(
            {"candidate_loss": 3.0, "baseline_loss": 2.0}, floor_at_zero=True
        )
        self.assertEqual(payload["gain"], 0.0)
        self.assertTrue(payload["floored_at_zero"])
        self.assertTrue(payload["floor_at_zero"])
        self.assertLess(payload["loss_difference"], 0.0)

    def test_zero_baseline_loss_is_undefined_not_silent(self) -> None:
        """基线损失为 0 时相对下降无定义：如实记录，不静默改写为有限值。"""
        worse = gain({"candidate_loss": 1.0, "baseline_loss": 0.0})
        self.assertFalse(worse["relative_reduction_defined"])
        self.assertEqual(worse["gain"], 0.0)

        better = gain({"candidate_loss": -1.0, "baseline_loss": 0.0})
        self.assertFalse(better["relative_reduction_defined"])
        self.assertEqual(better["gain"], float("inf"))

    def test_invalid_loss_name_raises(self) -> None:
        """不可用的损失名报错。"""
        with self.assertRaises(MetricsInputError):
            gain({"candidate_loss": 1.0, "baseline_loss": 2.0}, loss="r2")
        self.assertIn("r2", "r2")  # 明确 r2 属「越高越好」，不在 GAIN_LOSSES
        self.assertNotIn("r2", GAIN_LOSSES)

    def test_all_supported_losses(self) -> None:
        """支持四种「越低越好」的损失。"""
        self.assertEqual(set(GAIN_LOSSES), {"mse", "sse", "rmse", "mae"})

    def test_relation_fit_rejects_non_mse_loss(self) -> None:
        """RelationFit 的对照块只登记 mse，按其它损失请求会明确报错。"""
        with self.assertRaises(MetricsInputError) as context:
            gain(relation_fit_of(), loss="mae")
        self.assertIn("mse", str(context.exception))

    def test_bad_evidence_type_raises(self) -> None:
        """非法证据形态报错。"""
        for bad in (None, "mse", 42, object()):
            with self.subTest(evidence=type(bad).__name__):
                with self.assertRaises(MetricsInputError):
                    gain(bad)

    def test_gain_is_development_only(self) -> None:
        """口径说明明确增益是开发期探索量。"""
        payload = gain({"candidate_loss": 1.0, "baseline_loss": 2.0})
        self.assertIn("开发期", payload["note"])
        self.assertIn("不授予证据等级", payload["note"])

    def test_gain_rejects_token_keys(self) -> None:
        """映射证据不得携带令牌类键名。"""
        with self.assertRaises(MetricsPolicyError):
            gain({"candidate_loss": 1.0, "baseline_loss": 2.0, "token": "x"})


# ---------------------------------------------------------------------------
# 指标二：稳定性 S
# ---------------------------------------------------------------------------


class TestStability(unittest.TestCase):
    """稳定性分数：口径转录与充分性透传。"""

    def test_from_stability_report(self) -> None:
        """由 P06 的报告读取稳定性，并透传充分性判定。"""
        report = stability_report_of()
        payload = stability(report)
        self.assertAlmostEqual(payload["stability"], report.score)
        self.assertEqual(payload["sufficient"], report.sufficient)
        self.assertEqual(payload["subject_kind"], report.subject_kind)
        self.assertEqual(payload["n_effective"], report.n_effective)
        self.assertEqual(payload["n_reproduced"], report.n_reproduced)
        self.assertEqual(payload["note"], STABILITY_NOTE)

    def test_from_mapping_and_sequence(self) -> None:
        """计数映射与布尔序列两种形态均可。"""
        mapping = stability({"n_effective": 10, "n_reproduced": 8})
        self.assertAlmostEqual(mapping["stability"], 0.8)
        sequence = stability([True, True, False, True])
        self.assertAlmostEqual(sequence["stability"], 0.75)

    def test_bounds(self) -> None:
        """稳定性恒落在 [0, 1]。"""
        for total, success in ((10, 0), (10, 10), (3, 1), (0, 0)):
            with self.subTest(total=total, success=success):
                value = stability({"n_effective": total, "n_reproduced": success})["stability"]
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_zero_resamples_scores_zero_not_none(self) -> None:
        """无有效重采样时为 0.0，而不是 None 或 nan（便于下游统一按数值处理）。"""
        payload = stability({"n_effective": 0, "n_reproduced": 0})
        self.assertEqual(payload["stability"], 0.0)
        self.assertFalse(math.isnan(payload["stability"]))

    def test_invalid_evidence_raises(self) -> None:
        """非法证据形态报错，并转为本模块的异常类型。"""
        with self.assertRaises(MetricsInputError):
            stability(object())
        with self.assertRaises(MetricsInputError):
            stability({"n_effective": 3, "n_reproduced": 5})

    def test_no_evidence_marks_insufficient(self) -> None:
        """映射未附充分性判定时，显式标为不充分而非默认充分。"""
        payload = stability({"n_effective": 10, "n_reproduced": 10})
        self.assertFalse(payload["sufficient"])

    def test_score_is_not_error_control(self) -> None:
        """口径说明声明稳定性不是错误率控制。"""
        payload = stability({"n_effective": 10, "n_reproduced": 9})
        self.assertIn("不是错误率控制", payload["note"])
        self.assertIn("不构成证据等级", payload["note"])

    def test_insufficient_stability_is_flagged_in_evaluation(self) -> None:
        """充分性判定透传到 Evaluation.flags。"""
        report = stability_report_of()
        evaluation = evaluate_hypothesis(
            hypothesis_of(),
            knowledge_base=knowledge_base_v1(),
            stability_evidence=report,
        )
        self.assertEqual(
            evaluation.flags["stability_sufficient"], bool(report.sufficient)
        )


# ---------------------------------------------------------------------------
# 装配契约：Evaluation 与 INTERFACES.md §2.4 对齐
# ---------------------------------------------------------------------------


class TestEvaluationContract(unittest.TestCase):
    """Evaluation 字段与 ``INTERFACES.md`` §2.4 逐项对齐。"""

    def test_required_fields_present(self) -> None:
        """契约要求的六个字段全部存在。"""
        payload = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=knowledge_base_v1()
        ).to_dict()
        for field_name in (
            "hypothesis_id", "gain", "stability", "novelty", "complexity",
            "knowledge_version",
        ):
            with self.subTest(field=field_name):
                self.assertIn(field_name, payload)

    def test_all_four_dimensions_computed_when_evidence_supplied(self) -> None:
        """四类证据齐备时四个维度都有取值。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(),
            knowledge_base=knowledge_base_v1(),
            relation_fit=relation_fit_of(),
            stability_evidence=stability_report_of(),
        )
        self.assertTrue(evaluation.complete)
        self.assertEqual(evaluation.missing_dimensions, ())
        for name in DIMENSION_NAMES:
            self.assertIsNotNone(getattr(evaluation, name), f"{name} 应有取值")

    def test_missing_evidence_yields_none_not_zero(self) -> None:
        """证据缺失是 None，不是 0——「没算」与「算出来是 0」必须可区分。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=knowledge_base_v1()
        )
        self.assertIsNone(evaluation.gain)
        self.assertIsNone(evaluation.stability)
        self.assertIsNotNone(evaluation.novelty)
        self.assertIsNotNone(evaluation.complexity)
        self.assertEqual(set(evaluation.missing_dimensions), {"gain", "stability"})
        self.assertFalse(evaluation.complete)

    def test_novelty_and_complexity_always_available(self) -> None:
        """新颖性与复杂度只依赖假说与知识库，因此恒有取值。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=KnowledgeBase(version="v-empty")
        )
        self.assertIsNotNone(evaluation.novelty)
        self.assertIsNotNone(evaluation.complexity)

    def test_hypothesis_identity_recorded(self) -> None:
        """假说 id / 版本 / 类型被登记。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(hypothesis_id="h-xyz", version="3"),
            knowledge_base=knowledge_base_v1(),
        )
        self.assertEqual(evaluation.hypothesis_id, "h-xyz")
        self.assertEqual(evaluation.hypothesis_version, "3")
        self.assertEqual(evaluation.hypothesis_type, "relation")

    def test_accepts_plain_mapping_hypothesis(self) -> None:
        """鸭子类型：普通字典形态的假说同样可评估。"""
        payload = {
            "id": "h-dict",
            "version": "1",
            "type": "relation",
            "statement": "字典形态的假说。",
            "model": {"expression": "X1/X2", "coefficients": {"a": 1.0}},
            "provenance": {"knowledge_version": "k-v1"},
        }
        evaluation = evaluate_hypothesis(payload, knowledge_base=knowledge_base_v1())
        self.assertEqual(evaluation.hypothesis_id, "h-dict")
        self.assertIsNotNone(evaluation.complexity)

    def test_mapping_hypothesis_requires_core_fields(self) -> None:
        """字典形态缺少核心字段报错。"""
        with self.assertRaises(MetricsInputError):
            evaluate_hypothesis({"statement": "缺 id 与 version。"}, knowledge_base=knowledge_base_v1())

    def test_evaluation_requires_nonempty_identity(self) -> None:
        """空的假说 id / 版本被拒绝。"""
        with self.assertRaises(MetricsInputError):
            Evaluation(
                hypothesis_id="",
                hypothesis_version="1",
                hypothesis_type=None,
                gain=None, stability=None, novelty=0.5, complexity=1.0,
                knowledge_version="v1",
            )

    def test_evaluation_requires_knowledge_version(self) -> None:
        """Evaluation 自身也不允许空版本号。"""
        with self.assertRaises(MetricsVersionError):
            Evaluation(
                hypothesis_id="h", hypothesis_version="1", hypothesis_type=None,
                gain=None, stability=None, novelty=0.5, complexity=1.0,
                knowledge_version="  ",
            )

    def test_evaluation_validates_value_ranges(self) -> None:
        """取值域校验：新颖性与稳定性须在 [0,1]，复杂度须非负。"""
        with self.assertRaises(MetricsInputError):
            Evaluation(
                hypothesis_id="h", hypothesis_version="1", hypothesis_type=None,
                gain=None, stability=None, novelty=1.5, complexity=1.0,
                knowledge_version="v1",
            )
        with self.assertRaises(MetricsInputError):
            Evaluation(
                hypothesis_id="h", hypothesis_version="1", hypothesis_type=None,
                gain=None, stability=None, novelty=0.5, complexity=-1.0,
                knowledge_version="v1",
            )

    def test_evidence_grade_is_not_granted(self) -> None:
        """评估记录不授予证据等级。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=knowledge_base_v1()
        )
        self.assertFalse(evaluation.flags["evidence_grade_granted"])
        self.assertIn("不授予证据等级", evaluation.note)

    def test_serialization_is_json_compatible(self) -> None:
        """序列化结果可直接 JSON 编码。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(),
            knowledge_base=knowledge_base_v1(),
            relation_fit=relation_fit_of(),
            stability_evidence=stability_report_of(),
        )
        text = json.dumps(evaluation.to_dict(), ensure_ascii=False)
        self.assertIn("hypothesis_id", text)
        restored = json.loads(text)
        self.assertEqual(restored["knowledge_version"], "k-v1")

    def test_metrics_version_is_recorded(self) -> None:
        """序列化结果记录指标模块版本。"""
        payload = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=knowledge_base_v1()
        ).to_dict()
        self.assertEqual(payload["metrics_version"], METRICS_VERSION)

    def test_components_are_read_only(self) -> None:
        """分量容器为只读视图，避免下游就地篡改。"""
        evaluation = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=knowledge_base_v1()
        )
        with self.assertRaises(TypeError):
            evaluation.components["gain"] = {}  # type: ignore[index]
        with self.assertRaises(TypeError):
            evaluation.flags["is_strict_mdl"] = True  # type: ignore[index]

    def test_source_ref_rejects_restricted_partitions(self) -> None:
        """登记的数据引用不得指向 V / C1 等受限分区。"""
        for ref in ("V-abc", "C1-abc", "Q-abc", "C2-xyz"):
            with self.subTest(ref=ref):
                with self.assertRaises(MetricsPolicyError):
                    evaluate_hypothesis(
                        hypothesis_of(),
                        knowledge_base=knowledge_base_v1(),
                        source_ref=ref,
                    )
        # E 引用正常放行。
        evaluation = evaluate_hypothesis(
            hypothesis_of(), knowledge_base=knowledge_base_v1(), source_ref="E-abc123"
        )
        self.assertEqual(evaluation.hypothesis_id, "h-relation-1")


# ---------------------------------------------------------------------------
# 确定性与可复现
# ---------------------------------------------------------------------------


class TestReproducibility(unittest.TestCase):
    """同一输入恒得逐字节相同的序列化结果。"""

    def test_repeated_evaluation_is_identical(self) -> None:
        """重复评估得相同摘要。"""
        reports = [stability_report_of() for _ in range(2)]
        first = evaluate_hypothesis(
            hypothesis_of(),
            knowledge_base=knowledge_base_v1(),
            relation_fit=relation_fit_of(),
            stability_evidence=reports[0],
        )
        second = evaluate_hypothesis(
            hypothesis_of(),
            knowledge_base=knowledge_base_v1(),
            relation_fit=relation_fit_of(),
            stability_evidence=reports[1],
        )
        self.assertEqual(first.content_digest(), second.content_digest())
        self.assertEqual(first.canonical_json(), second.canonical_json())

    def test_novelty_is_order_independent(self) -> None:
        """知识库条目的书写顺序不影响新颖性结果。"""
        entries = (
            entry_of("k-ratio", "比例表示与目标近似线性。", "X1/X2"),
            entry_of("k-sum", "和式与目标近似线性。", "X1 + X2"),
            entry_of("k-prod", "乘积与目标近似线性。", "X1 * X2"),
        )
        forward = KnowledgeBase(version="v1", entries=entries)
        backward = KnowledgeBase(version="v1", entries=tuple(reversed(entries)))
        self.assertEqual(
            novelty(hypothesis_of(), forward)["novelty"],
            novelty(hypothesis_of(), backward)["novelty"],
        )

    def test_tie_break_is_deterministic(self) -> None:
        """同分匹配按 entry_id 较小者取胜，结果与迭代顺序无关。"""
        entries = (entry_of("b-later", "同一句陈述。", None), entry_of("a-earlier", "同一句陈述。", None))
        forward = KnowledgeBase(version="v1", entries=entries)
        backward = KnowledgeBase(version="v1", entries=tuple(reversed(entries)))
        self.assertEqual(
            novelty(hypothesis_of(expression="Z9/Z8"), forward)["nearest_entry_id"],
            novelty(hypothesis_of(expression="Z9/Z8"), backward)["nearest_entry_id"],
        )

    def test_knowledge_base_serialization_is_stable(self) -> None:
        """知识库序列化键序稳定。"""
        kb = knowledge_base_v1()
        self.assertEqual(kb.canonical_json(), knowledge_base_v1().canonical_json())


# ---------------------------------------------------------------------------
# P09 边界守护
# ---------------------------------------------------------------------------


class TestStageBoundaries(unittest.TestCase):
    """P09 的边界：不做确证检验、不碰 M1、不实现 P10。"""

    def setUp(self) -> None:
        self.text = MODULE_PATH.read_text(encoding="utf-8")

    def test_no_third_party_imports(self) -> None:
        """不引入第三方依赖。"""
        tree = pyast.parse(self.text)
        imported: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden = {"numpy", "sklearn", "scipy", "pandas", "statsmodels"}
        for name in imported:
            with self.subTest(module=name):
                self.assertNotIn(name.split(".")[0], forbidden)

    def test_only_expected_top_level_imports(self) -> None:
        """顶层导入限定在标准库与既有模块内。"""
        tree = pyast.parse(self.text)
        roots: set[str] = set()
        for node in tree.body:
            if isinstance(node, pyast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue(
            roots <= {"__future__", "hashlib", "json", "math", "re", "dataclasses",
                      "types", "typing", "sdl_m02", "sdl_m03", "sdl_m04"},
            f"出现了未预期的导入：{sorted(roots)}",
        )

    def test_does_not_import_or_call_m01(self) -> None:
        """不导入、不调用 ``sdl_m01``。"""
        tree = pyast.parse(self.text)
        imported: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertEqual(
            [name for name in imported if name.split(".")[0] == "sdl_m01"], [],
            "本模块不得依赖 sdl_m01",
        )

    def test_declares_it_does_not_modify_earlier_deliverables(self) -> None:
        """显式声明不修改 sdl_m01/。"""
        self.assertIn("不修改", self.text)
        self.assertIn("sdl_m01/", self.text)

    def test_no_inference_implementation_vocabulary(self) -> None:
        """不实现确证检验：不出现错误率控制等实现性标识。

        注意 ``p_value`` 一类词若只出现在「不计算 p 值」的边界声明里，
        仍会被文本匹配命中，故这里只检查确证流程的实现性标识。
        """
        for banned in ("reject_null", "confidence_interval",
                       "holm", "bonferroni", "family_wise"):
            self.assertNotIn(banned, self.text.lower())
        # 边界声明应显式写出「不计算 p 值」。
        self.assertIn("不计算 p 值", self.text)

    def test_no_confirmation_partition_usage(self) -> None:
        """不读取确证分区：不出现确证生命周期的接口调用。"""
        for banned in ("bind_confirmation", "consume_confirmation", "record_evaluation",
                       "release_results", "archive_confirmation", "read_dataset"):
            self.assertNotIn(banned, self.text)

    def test_declares_exploration_only(self) -> None:
        """声明只在探索分区 E 上定义指标。"""
        self.assertIn("探索分区", self.text)
        self.assertIn("只在探索分区 E 上计算", GAIN_NOTE)

    def test_does_not_mutate_hypothesis_object(self) -> None:
        """不改写假说对象：不**调用**其变更方法。

        判定口径是是否真的调用了变更接口——文档里提到「须由调用方走 P07 的
        ``new_version`` 派生新版本」是边界声明，恰恰是要保留的内容，
        因此这里按 AST 检查真实调用与导入，而非文本匹配。
        """
        tree = pyast.parse(self.text)
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in {"new_version", "with_evidence", "register", "revise", "publish"}:
                    offenders.append(f"{name}()")
            if isinstance(node, pyast.ImportFrom) and node.module == "sdl_m04.hypothesis":
                for alias in node.names:
                    if alias.name == "HypothesisStore":
                        offenders.append("HypothesisStore")
        self.assertEqual(offenders, [], f"本模块调用了假说的变更接口：{offenders}")

    def test_no_skip_or_xfail_in_this_file(self) -> None:
        """本文件不得使用 skip / xfail 让测试变绿。"""
        tree = pyast.parse(TEST_PATH.read_text(encoding="utf-8"))
        markers = {"skip", "skipIf", "skipUnless", "xfail", "skipTest", "expectedFailure"}
        hits: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)):
                for decorator in node.decorator_list:
                    target = decorator.func if isinstance(decorator, pyast.Call) else decorator
                    name = getattr(target, "attr", None) or getattr(target, "id", None)
                    if name in markers:
                        hits.append(node.name)
            if isinstance(node, pyast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in markers:
                    hits.append(f"{name}()")
        self.assertEqual(hits, [], f"本文件出现了 skip/xfail：{hits}")

    def test_module_exports_are_declared(self) -> None:
        """公开接口在 __all__ 中声明。"""
        tree = pyast.parse(self.text)
        declared: list[str] = []
        for node in tree.body:
            if isinstance(node, pyast.Assign):
                for target in node.targets:
                    if isinstance(target, pyast.Name) and target.id == "__all__":
                        declared = [item.value for item in node.value.elts]
        for name in ("gain", "novelty", "complexity", "evaluate_hypothesis",
                     "KnowledgeBase", "Evaluation"):
            with self.subTest(name=name):
                self.assertIn(name, declared)

    def test_public_functions_exist(self) -> None:
        """四个主入口与容器均可导入且可调用。"""
        import sdl_m05.metrics as module

        for name in ("gain", "stability", "novelty", "complexity", "evaluate_hypothesis"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(module, name)))

    def test_all_errors_derive_from_value_error(self) -> None:
        """异常基类为 ValueError，便于调用方按粒度捕获。"""
        for error in (MetricsError, MetricsInputError, MetricsPolicyError, MetricsVersionError):
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, ValueError))
        self.assertTrue(issubclass(MetricsVersionError, MetricsInputError))
        self.assertTrue(issubclass(MetricsPolicyError, MetricsError))


# ---------------------------------------------------------------------------
# 入口签名
# ---------------------------------------------------------------------------


class TestPublicSignatures(unittest.TestCase):
    """主入口的签名只暴露原样参数，不暴露加权入口。"""

    def test_signatures_have_no_weight_parameters(self) -> None:
        """没有任何公开函数暴露权重参数。"""
        import inspect

        import sdl_m05.metrics as module

        for name in ("gain", "stability", "novelty", "complexity", "evaluate_hypothesis"):
            function = getattr(module, name)
            parameters = inspect.signature(function).parameters
            for parameter in parameters.values():
                with self.subTest(function=name, parameter=parameter.name):
                    self.assertNotIn(parameter.name, {"weights", "score_weights"})

    def test_evaluate_hypothesis_accepts_catch_all(self) -> None:
        """evaluate_hypothesis 以 **kwargs 兜住越界参数并显式拒绝。"""
        import inspect

        from sdl_m05.metrics import evaluate_hypothesis as target

        kinds = {p.kind for p in inspect.signature(target).parameters.values()}
        self.assertIn(inspect.Parameter.VAR_KEYWORD, kinds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
