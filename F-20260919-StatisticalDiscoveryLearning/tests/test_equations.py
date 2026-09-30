"""P04 验收测试：关系与方程搜索。

覆盖三条验收标准：

1. **拟合只用探索分区 E**：非 explorer 角色的客户端、指向 V/C1 的引用、
   非 E 用途一律被拒；不存在绕过令牌的读取路径。
2. **结果必含误差、残差与复杂度**：三项恒存在且不可为空，
   缺项的结果对象在构造层面即被拒绝。
3. **基线不可省略，且与候选在同一数据上比较**：空基线集合报错；
   基线与候选的数据指纹、行数必须逐项一致，否则报错。

另覆盖拟合数学正确性、定义域限制、残差摘要口径、纯函数性与确定性，
并守护 P04 的边界：不做结构模式搜索（P05）、不做稳定性与重采样（P06）、
不计算 p 值、不授予证据等级、不修改 sdl_m01/。
"""

import inspect
import math
import unittest

from sdl_m02.domain import Candidate, DomainSpec
from sdl_m02.expressions import parse
from sdl_m03.equations import (
    BASELINE_KINDS,
    COMPLEXITY_NOTE,
    DEFAULT_BASELINES,
    EQUATION_VERSION,
    EXPLORATION_PURPOSE,
    EXPLORER_ROLE,
    EquationError,
    EquationPolicyError,
    ExplorationSample,
    FitResult,
    RelationFit,
    ResidualSummary,
    baseline_constant,
    baseline_linear,
    compare_on_same_data,
    evaluate_expression,
    fit_relation,
    formula_length,
    gain_against,
    prepare_sample,
    render_expression,
    residual_summary,
    restrict_to_domain,
)

# ---------------------------------------------------------------------------
# 夹具：确定性合成数据
# ---------------------------------------------------------------------------

VARIABLES = ["X1", "X2"]


def _x1(index: int) -> float:
    """确定性自变量 1。"""
    return 0.5 * index + 0.3


def _x2(index: int) -> float:
    """确定性自变量 2（与 X1 不共线）。"""
    return 0.7 + 0.23 * ((index * 7) % 5)


def _y(index: int) -> float:
    """目标：Y = 2·X1 + 0.2·X2 + 1。

    因为候选 ``X1 + 0.1*X2`` 记作 Z，则恰好有 Y = 2·Z + 1，
    即该候选与目标之间是**精确**线性关系，残差应趋近于浮点精度。
    """
    return 2.0 * _x1(index) + 0.2 * _x2(index) + 1.0


def linear_records(count: int = 12) -> list[dict]:
    """返回 count 行「对某候选精确线性」的合成记录。"""
    return [
        {
            "record_id": f"r-{index:03d}",
            "values": {"X1": _x1(index), "X2": _x2(index), "Y": _y(index)},
            "units": {"X1": "mol/L", "X2": "mol/L", "Y": "mol/(L*s)"},
        }
        for index in range(count)
    ]


def sample_of(records: list[dict] | None = None) -> ExplorationSample:
    """构造标准探索样本。"""
    return prepare_sample(records or linear_records(), VARIABLES, "Y")


CANDIDATE_TEXT = "X1 + 0.1*X2"


# ---------------------------------------------------------------------------
# 验收标准①：只经 explorer 角色读取 E 分区
# ---------------------------------------------------------------------------


class TestExplorationOnlyAccess(unittest.TestCase):
    """验收标准①：数据访问只走探索侧、只读 E。"""

    def test_explorer_role_client_is_accepted(self) -> None:
        """explorer 角色的客户端 + E 引用可正常读取。"""

        class FakeExplorer:
            role = EXPLORER_ROLE

            def __init__(self) -> None:
                self.calls: list[str] = []

            def read_dataset(self, ref):
                self.calls.append(ref)
                return linear_records()

        client = FakeExplorer()
        sample = prepare_sample(None, VARIABLES, "Y", client=client, source_ref="E-abc123")
        self.assertEqual(client.calls, ["E-abc123"])
        self.assertEqual(len(sample), 12)
        self.assertEqual(sample.provenance["access_mode"], "client")
        self.assertEqual(sample.provenance["role"], EXPLORER_ROLE)

    def test_non_explorer_roles_are_rejected(self) -> None:
        """custodian / confirmer / auditor 角色一律拒绝。"""

        class FakeClient:
            def __init__(self, role: str) -> None:
                self.role = role

            def read_dataset(self, ref):  # pragma: no cover - 不应被调用
                raise AssertionError("非探索角色不得触发数据读取")

        for role in ("custodian", "confirmer", "auditor"):
            with self.subTest(role=role):
                with self.assertRaises(EquationPolicyError):
                    prepare_sample(
                        None, VARIABLES, "Y", client=FakeClient(role), source_ref="E-abc123"
                    )

    def test_client_read_requires_explicit_ref(self) -> None:
        """通过客户端读取时必须显式给出 E 引用。"""

        class FakeExplorer:
            role = EXPLORER_ROLE

            def read_dataset(self, ref):  # pragma: no cover - 不应被调用
                raise AssertionError("缺少引用时不得触发读取")

        with self.assertRaises(EquationPolicyError):
            prepare_sample(None, VARIABLES, "Y", client=FakeExplorer())

    def test_restricted_ref_prefixes_are_rejected(self) -> None:
        """指向 V / C1 / Q / H_ 的引用一律拒绝。"""
        for ref in ("V-0011", "C1-9b10", "C2-aaaa", "Q-ff00", "H_ab12"):
            with self.subTest(ref=ref):
                with self.assertRaises(EquationPolicyError):
                    prepare_sample(linear_records(), VARIABLES, "Y", source_ref=ref)

    def test_purpose_must_be_exploration(self) -> None:
        """purpose 不是 E 时直接拒绝。"""
        for purpose in ("V", "C1", "Q", "H_abc"):
            with self.subTest(purpose=purpose):
                with self.assertRaises(EquationPolicyError):
                    prepare_sample(linear_records(), VARIABLES, "Y", purpose=purpose)

    def test_resources_mapping_pins_the_e_ref(self) -> None:
        """提供分区映射时，引用必须恰好等于映射中的 E 项。"""
        resources = {"E": "E-aaa111", "V": "V-bbb222", "C1": "C1-ccc333"}

        sample = prepare_sample(
            linear_records(), VARIABLES, "Y", source_ref="E-aaa111", resources=resources
        )
        self.assertEqual(sample.source_ref, "E-aaa111")

        with self.assertRaises(EquationPolicyError):
            prepare_sample(
                linear_records(), VARIABLES, "Y", source_ref="V-bbb222", resources=resources
            )

        # 未登记在映射中的引用同样拒绝：不猜测归属。
        with self.assertRaises(EquationPolicyError):
            prepare_sample(
                linear_records(), VARIABLES, "Y", source_ref="E-zzz999", resources=resources
            )

    def test_resources_without_e_ref_is_rejected(self) -> None:
        """映射缺少 E 项时无法确认归属，拒绝读取。"""
        with self.assertRaises(EquationPolicyError):
            prepare_sample(
                linear_records(), VARIABLES, "Y", source_ref="E-abc123",
                resources={"V": "V-0011"},
            )

    def test_database_path_is_not_a_record_source(self) -> None:
        """数据库路径不被当作数据来源：本模块没有直接取数能力。"""
        with self.assertRaises(EquationError):
            prepare_sample("demo-output/evidence.sqlite3", VARIABLES, "Y")

    def test_config_mapping_is_not_a_record_source(self) -> None:
        """配置字典不被当作数据来源。"""
        with self.assertRaises(EquationError):
            prepare_sample({"db_path": "evidence.sqlite3"}, VARIABLES, "Y")

    def test_no_public_function_accepts_a_token(self) -> None:
        """任何公开函数的签名都不得出现令牌参数。"""
        for name in (
            "prepare_sample", "fit_relation", "baseline_constant", "baseline_linear",
            "restrict_to_domain", "residual_summary", "compare_on_same_data",
            "gain_against",
        ):
            function = globals()[name]
            parameters = set(inspect.signature(function).parameters)
            with self.subTest(function=name):
                self.assertFalse(
                    {"token", "role_token", "bearer"} & parameters,
                    f"{name} 不应接受令牌类参数",
                )

    def test_module_has_no_read_path_beyond_read_dataset(self) -> None:
        """本模块不导入 M1，也不包含直接取数的入口。"""
        import sdl_m03.equations as module

        source = inspect.getsource(module)
        self.assertNotIn("import sqlite3", source)
        self.assertNotIn("from sdl_m01", source)
        self.assertNotIn("import sdl_m01", source)


# ---------------------------------------------------------------------------
# 验收标准②：误差 / 残差 / 复杂度三项恒存在
# ---------------------------------------------------------------------------


class TestThreeComponentsAlwaysPresent(unittest.TestCase):
    """验收标准②：误差、残差、复杂度三项不可省略。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fit = fit_relation(sample_of(), CANDIDATE_TEXT)

    def test_candidate_has_all_three(self) -> None:
        candidate = self.fit.candidate
        self.assertTrue(candidate.metrics)
        self.assertIsInstance(candidate.residual_summary, ResidualSummary)
        self.assertTrue(candidate.complexity)

    def test_metrics_contain_error_terms(self) -> None:
        for key in ("sse", "mse", "rmse", "mae", "max_abs_error"):
            with self.subTest(key=key):
                self.assertIn(key, self.fit.candidate.metrics)

    def test_residuals_are_pointwise(self) -> None:
        candidate = self.fit.candidate
        self.assertEqual(len(candidate.residuals), candidate.n_used)
        self.assertEqual(
            len(candidate.residual_summary.residuals), candidate.residual_summary.n
        )

    def test_complexity_declares_length_approximation(self) -> None:
        complexity = self.fit.candidate.complexity
        self.assertIn("expression_nodes", complexity)
        self.assertIn("parameter_count", complexity)
        self.assertEqual(
            complexity["total"],
            complexity["expression_nodes"] + complexity["parameter_count"],
        )
        self.assertEqual(complexity["note"], COMPLEXITY_NOTE)

    def test_complexity_note_denies_strict_mdl(self) -> None:
        """复杂度必须以文字明示「不是严格 MDL」。"""
        self.assertIn("不是严格 MDL", COMPLEXITY_NOTE)
        self.assertIn("不是严格 MDL", self.fit.candidate.to_dict()["complexity_note"])

    def test_baselines_also_carry_all_three(self) -> None:
        for fit in self.fit.baselines.values():
            items = [fit] if isinstance(fit, FitResult) else list(fit["per_feature"].values())
            for item in items:
                with self.subTest(name=item.name):
                    self.assertTrue(item.metrics)
                    self.assertIsInstance(item.residual_summary, ResidualSummary)
                    self.assertTrue(item.complexity)

    def test_fitresult_refuses_missing_metrics(self) -> None:
        candidate = self.fit.candidate
        with self.assertRaises(EquationError):
            FitResult(
                kind="candidate",
                name="缺误差",
                parameters=candidate.parameters,
                metrics={},
                residual_summary=candidate.residual_summary,
                complexity=candidate.complexity,
                data_fingerprint=candidate.data_fingerprint,
                n_used=candidate.n_used,
            )

    def test_fitresult_refuses_missing_complexity(self) -> None:
        candidate = self.fit.candidate
        with self.assertRaises(EquationError):
            FitResult(
                kind="candidate",
                name="缺复杂度",
                parameters=candidate.parameters,
                metrics=candidate.metrics,
                residual_summary=candidate.residual_summary,
                complexity={},
                data_fingerprint=candidate.data_fingerprint,
                n_used=candidate.n_used,
            )

    def test_fitresult_refuses_missing_parameters(self) -> None:
        candidate = self.fit.candidate
        with self.assertRaises(EquationError):
            FitResult(
                kind="candidate",
                name="缺参数",
                parameters={},
                metrics=candidate.metrics,
                residual_summary=candidate.residual_summary,
                complexity=candidate.complexity,
                data_fingerprint=candidate.data_fingerprint,
                n_used=candidate.n_used,
            )

    def test_fitresult_refuses_none_residual_summary(self) -> None:
        candidate = self.fit.candidate
        with self.assertRaises(EquationError):
            FitResult(
                kind="candidate",
                name="缺残差",
                parameters=candidate.parameters,
                metrics=candidate.metrics,
                residual_summary=None,  # type: ignore[arg-type]
                complexity=candidate.complexity,
                data_fingerprint=candidate.data_fingerprint,
                n_used=candidate.n_used,
            )

    def test_fitresult_refuses_inconsistent_n_used(self) -> None:
        """n_used 必须与残差摘要一致，避免「报告行数」与「实际行数」脱节。"""
        candidate = self.fit.candidate
        with self.assertRaises(EquationError):
            FitResult(
                kind="candidate",
                name="行数不一致",
                parameters=candidate.parameters,
                metrics=candidate.metrics,
                residual_summary=candidate.residual_summary,
                complexity=candidate.complexity,
                data_fingerprint=candidate.data_fingerprint,
                n_used=candidate.n_used + 1,
            )

    def test_serialisation_keeps_three_components(self) -> None:
        payload = self.fit.to_dict()
        self.assertIn("metrics", payload["candidate"])
        self.assertIn("residuals", payload["candidate"])
        self.assertIn("complexity", payload["candidate"])


# ---------------------------------------------------------------------------
# 验收标准③：基线不可省略、且与候选同数据
# ---------------------------------------------------------------------------


class TestBaselinesMandatoryAndComparable(unittest.TestCase):
    """验收标准③：基线必算，且与候选在同一数据上比较。"""

    def test_empty_baselines_are_rejected(self) -> None:
        for empty in ((), [],):
            with self.subTest(empty=empty):
                with self.assertRaises(EquationError):
                    fit_relation(sample_of(), CANDIDATE_TEXT, baselines=empty)

    def test_unknown_baseline_kind_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            fit_relation(sample_of(), CANDIDATE_TEXT, baselines=("constant", "bootstrap"))

    def test_default_baselines_include_both_kinds(self) -> None:
        self.assertEqual(set(DEFAULT_BASELINES), set(BASELINE_KINDS))
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        self.assertIn("constant", fit.baselines)
        self.assertIn("linear", fit.baselines)

    def test_declared_baselines_appear_exactly(self) -> None:
        only_constant = fit_relation(sample_of(), CANDIDATE_TEXT, baselines=("constant",))
        self.assertEqual(list(only_constant.baselines), ["constant"])
        only_linear = fit_relation(sample_of(), CANDIDATE_TEXT, baselines=("linear",))
        self.assertEqual(list(only_linear.baselines), ["linear"])

    def test_relation_fit_refuses_empty_baselines(self) -> None:
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        with self.assertRaises(EquationError):
            RelationFit(
                candidate=fit.candidate,
                baselines={},
                comparison=fit.comparison,
                restriction=fit.restriction,
                sample=fit.sample,
                sample_input=fit.sample_input,
            )

    def test_baseline_and_candidate_share_one_fingerprint(self) -> None:
        """候选与全部基线必须落在同一数据指纹上。"""
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        fingerprints = {fit.candidate.data_fingerprint}
        for payload in fit.baselines.values():
            items = [payload] if isinstance(payload, FitResult) else list(
                payload["per_feature"].values()
            )
            fingerprints.update(item.data_fingerprint for item in items)
        self.assertEqual(len(fingerprints), 1, "候选与基线使用了不同数据")

    def test_baseline_and_candidate_share_row_count(self) -> None:
        """候选与基线必须使用相同行集（行数一致）。"""
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        for payload in fit.baselines.values():
            items = [payload] if isinstance(payload, FitResult) else list(
                payload["per_feature"].values()
            )
            for item in items:
                with self.subTest(name=item.name):
                    self.assertEqual(item.n_used, fit.candidate.n_used)

    def test_same_data_report_declares_fingerprint_and_rows(self) -> None:
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        block = fit.comparison["same_data"]
        self.assertEqual(block["data_fingerprint"], fit.fingerprint)
        self.assertEqual(block["n_used"], fit.candidate.n_used)
        self.assertEqual(block["loss"], "mse")
        self.assertGreaterEqual(len(block["table"]), 3)

    def test_compare_on_same_data_rejects_mismatched_fingerprint(self) -> None:
        """直接在数据指纹不同的两个结果上核验必须报错。"""
        first = fit_relation(sample_of(), CANDIDATE_TEXT)
        second = fit_relation(sample_of(linear_records(8)), CANDIDATE_TEXT)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        with self.assertRaises(EquationError):
            compare_on_same_data(first.candidate, second.baselines["constant"])

    def test_gain_against_rejects_mismatched_fingerprint(self) -> None:
        first = fit_relation(sample_of(), CANDIDATE_TEXT)
        second = fit_relation(sample_of(linear_records(8)), CANDIDATE_TEXT)
        with self.assertRaises(EquationError):
            gain_against(first.candidate, second.baselines["constant"])

    def test_no_full_sample_baseline_against_restricted_candidate(self) -> None:
        """核心反例：候选被定义域限制后，基线不得仍用全量数据。

        构造一个会因病态行被限制的候选，再手工用**全量**样本算基线，
        此时两者指纹不同，对照必须报错（证明不可比对照无法通过）。
        """
        records = []
        for index in range(12):
            x2 = 0.0 if index == 3 else 1.0 + 0.05 * index
            records.append(
                {
                    "record_id": f"r-{index:03d}",
                    "values": {"X1": _x1(index), "X2": x2, "Y": _y(index)},
                }
            )
        full = prepare_sample(records, VARIABLES, "Y")
        fit = fit_relation(full, "X1/X2")
        self.assertGreater(fit.restriction["domain_out"], 0)

        full_sample_baseline = baseline_constant(full)
        with self.assertRaises(EquationError):
            gain_against(fit.candidate, full_sample_baseline)
        with self.assertRaises(EquationError):
            compare_on_same_data(fit.candidate, full_sample_baseline)

    def test_restriction_is_reported_with_reasons(self) -> None:
        records = linear_records(10)
        records[2]["values"]["X2"] = 0.0
        fit = fit_relation(prepare_sample(records, VARIABLES, "Y"), "X1/X2")
        self.assertEqual(fit.restriction["input"], 10)
        self.assertEqual(fit.restriction["kept"], 9)
        self.assertEqual(fit.restriction["domain_out"], 1)
        self.assertEqual(fit.sample_input, 10)
        self.assertEqual(len(fit.sample), 9)

    def test_restrict_domain_false_keeps_all_rows(self) -> None:
        records = linear_records(10)
        records[2]["values"]["X2"] = 0.0
        sample = prepare_sample(records, VARIABLES, "Y")
        with self.assertRaises(EquationError):
            # 关掉定义域限制后 1/0 会产生非有限值，必须报错而不是默默填空。
            fit_relation(sample, "X1/X2", restrict_domain=False)

    def test_hardest_baseline_is_reported(self) -> None:
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        hardest = fit.comparison["hardest_baseline"]
        self.assertIsNotNone(hardest)
        others = [
            item
            for payload in fit.baselines.values()
            for item in ([payload] if isinstance(payload, FitResult) else payload["per_feature"].values())
        ]
        self.assertAlmostEqual(hardest["baseline_mse"], min(item.mse for item in others))

    def test_comparison_disclaims_inference(self) -> None:
        """对照结论必须写明不构成确证检验。"""
        fit = fit_relation(sample_of(), CANDIDATE_TEXT)
        note = fit.comparison["note"]
        self.assertIn("不构成确证检验", note)
        self.assertIn("不计算 p 值", note)
        self.assertIn("不授予证据等级", note)

    def test_results_carry_no_p_value_or_evidence_grade(self) -> None:
        """结果中不得出现 p 值、证据等级等确证阶段字段。"""
        payload = fit_relation(sample_of(), CANDIDATE_TEXT).to_dict()
        text = repr(payload).lower()
        for forbidden in ("p_value", "pvalue", "\"p\"", "evidence_grade", "grade", "significan"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


# ---------------------------------------------------------------------------
# 拟合数学正确性
# ---------------------------------------------------------------------------


class TestFittingCorrectness(unittest.TestCase):
    """拟合与基线的数值正确性。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = sample_of()
        cls.fit = fit_relation(cls.sample, CANDIDATE_TEXT)

    def test_candidate_recovers_exact_relation(self) -> None:
        """Y = 2·(X1 + 0.1·X2) + 1 是精确关系，残差应趋近于零。"""
        candidate = self.fit.candidate
        self.assertLess(candidate.mse, 1e-20)
        self.assertAlmostEqual(candidate.parameters["slope"], 2.0, places=9)
        self.assertAlmostEqual(candidate.parameters["intercept"], 1.0, places=9)

    def test_candidate_beats_both_baselines(self) -> None:
        comparison = self.fit.comparison
        self.assertTrue(comparison["gain_over_hardest_baseline"]["beats_baseline"])
        for kind in BASELINE_KINDS:
            block = comparison["baselines"][kind]
            if kind == "constant":
                self.assertTrue(block["single"]["beats_baseline"])
            else:
                self.assertTrue(block["best"]["beats_baseline"])

    def test_constant_baseline_mse_is_variance(self) -> None:
        """常数基线的 MSE 等于目标取值的总体方差。"""
        constant = baseline_constant(self.sample)
        y = self.sample.y()
        mean = sum(y) / len(y)
        variance = sum((value - mean) ** 2 for value in y) / len(y)
        self.assertAlmostEqual(constant.mse, variance, places=12)
        self.assertEqual(constant.parameters["slope"], 0.0)
        self.assertAlmostEqual(constant.parameters["intercept"], mean, places=12)

    def test_linear_baseline_recovers_exact_linear_data(self) -> None:
        """对精确线性数据，线性基线应精确还原斜率与截距。"""
        records = [
            {"record_id": f"r{i}", "values": {"X1": float(i), "X2": float(i % 3), "Y": 3.0 * i - 5.0}}
            for i in range(10)
        ]
        sample = prepare_sample(records, VARIABLES, "Y")
        fit = baseline_linear(sample, "X1")
        self.assertAlmostEqual(fit.parameters["slope"], 3.0, places=9)
        self.assertAlmostEqual(fit.parameters["intercept"], -5.0, places=9)
        self.assertLess(fit.mse, 1e-22)

    def test_candidate_equal_to_linear_baseline_when_identity(self) -> None:
        """候选就是某个原始变量时，它与该变量的线性基线应当完全等价。"""
        fit = fit_relation(self.sample, "X1")
        baseline = fit.baselines["linear"]["per_feature"]["X1"]
        self.assertEqual(fit.candidate.data_fingerprint, baseline.data_fingerprint)
        self.assertAlmostEqual(fit.candidate.mse, baseline.mse, places=15)
        self.assertAlmostEqual(
            fit.candidate.parameters["slope"], baseline.parameters["slope"], places=12
        )

    def test_linear_features_can_be_selected(self) -> None:
        fit = fit_relation(self.sample, CANDIDATE_TEXT, linear_features=("X2",))
        self.assertEqual(list(fit.baselines["linear"]["per_feature"]), ["X2"])

    def test_unknown_linear_feature_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            fit_relation(self.sample, CANDIDATE_TEXT, linear_features=("X9",))

    def test_constant_feature_is_omitted_from_linear_baselines(self) -> None:
        """无变异的字段无法定义线性基线，应被如实排除并记录。"""
        records = []
        for index in range(10):
            records.append(
                {
                    "record_id": f"r{index}",
                    "values": {"X1": _x1(index), "X2": 2.5, "Y": _y(index)},
                }
            )
        fit = fit_relation(prepare_sample(records, VARIABLES, "Y"), "X1")
        self.assertEqual(list(fit.baselines["linear"]["per_feature"]), ["X1"])
        self.assertEqual(fit.baselines["linear"]["unusable_no_variation"], ["X2"])

    def test_all_features_constant_makes_linear_baseline_impossible(self) -> None:
        records = [
            {"record_id": f"r{i}", "values": {"X1": 1.0, "X2": 2.0, "Y": 3.0 + i}}
            for i in range(6)
        ]
        sample = prepare_sample(records, VARIABLES, "Y")
        with self.assertRaises(EquationError):
            fit_relation(sample, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def test_constant_candidate_is_rejected(self) -> None:
        """Z 为常量的候选等价于常数基线，不应被当作关系拟合。"""
        with self.assertRaises(EquationError):
            fit_relation(self.sample, [1.0] * len(self.sample))

    def test_candidate_accepts_candidate_object(self) -> None:
        ast = parse("X1 + 0.1*X2")
        candidate = Candidate.create(ast, DomainSpec.for_expression(ast))
        fit = fit_relation(self.sample, candidate)
        self.assertLess(fit.candidate.mse, 1e-20)

    def test_candidate_accepts_plain_value_column(self) -> None:
        """预置数值列可用作候选 Z，且不带表达式负载。"""
        column = [_x1(index) + 0.1 * _x2(index) for index in range(len(self.sample))]
        fit = fit_relation(self.sample, column)
        self.assertLess(fit.candidate.mse, 1e-20)
        self.assertIsNone(fit.candidate.expression)

    def test_value_column_length_mismatch_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            fit_relation(self.sample, [1.0, 2.0, 3.0])

    def test_missing_variable_is_reported(self) -> None:
        with self.assertRaises(EquationError):
            fit_relation(self.sample, "X9 + 1")

    def test_too_few_rows_is_rejected(self) -> None:
        records = linear_records(2)
        sample = prepare_sample(records, VARIABLES, "Y")
        records[1]["values"]["X2"] = 0.0
        sample = prepare_sample(records, VARIABLES, "Y")
        with self.assertRaises(EquationError):
            fit_relation(sample, "X1/X2")

    def test_gain_against_gives_signed_difference(self) -> None:
        fit = fit_relation(self.sample, CANDIDATE_TEXT)
        gain = gain_against(fit.candidate, fit.baselines["constant"])
        self.assertGreater(gain["loss_difference"], 0.0)
        self.assertTrue(gain["candidate_better"])
        self.assertAlmostEqual(
            gain["loss_difference"],
            fit.baselines["constant"].mse - fit.candidate.mse,
            places=15,
        )


# ---------------------------------------------------------------------------
# 样本准备
# ---------------------------------------------------------------------------


class TestSamplePreparation(unittest.TestCase):
    """对齐、排除与指纹。"""

    def test_rows_are_aligned_and_ordered(self) -> None:
        sample = sample_of()
        self.assertEqual(len(sample), 12)
        self.assertEqual(sample.variables, ("X1", "X2"))
        self.assertEqual(sample.target, "Y")
        self.assertEqual(sample.rows[0].record_id, "r-000")

    def test_fingerprint_is_stable_and_content_sensitive(self) -> None:
        first = sample_of()
        second = sample_of()
        self.assertEqual(first.fingerprint, second.fingerprint)

        changed = linear_records()
        changed[5]["values"]["Y"] += 1.0
        self.assertNotEqual(first.fingerprint, sample_of(changed).fingerprint)

    def test_quality_excluded_rows_are_dropped_and_counted(self) -> None:
        records = linear_records(10)
        records[0]["_quality"] = {"status": "quarantined", "reasons": ["unit conflict"]}
        records[1]["_quality"] = {"status": "excluded", "reasons": ["duplicate"]}
        sample = prepare_sample(records, VARIABLES, "Y")
        self.assertEqual(len(sample), 8)
        self.assertEqual(sample.excluded["quality"], 2)

    def test_missing_values_are_excluded_not_imputed(self) -> None:
        records = linear_records(6)
        records[2]["values"]["X2"] = None
        records[3]["values"]["Y"] = "NA"
        sample = prepare_sample(records, VARIABLES, "Y")
        self.assertEqual(len(sample), 4)
        self.assertEqual(sample.excluded["missing"], 2)
        self.assertEqual(len(sample.rows), 4)

    def test_nonnumeric_values_are_excluded(self) -> None:
        records = linear_records(5)
        records[1]["values"]["X1"] = "high"
        records[2]["values"]["Y"] = float("nan")
        sample = prepare_sample(records, VARIABLES, "Y")
        self.assertEqual(len(sample), 3)
        self.assertEqual(sample.excluded["nonnumeric"], 2)

    def test_unknown_fields_are_ignored_but_record_id_preserved(self) -> None:
        records = linear_records(4)
        records[0]["operator_annotation"] = "private-note"
        sample = prepare_sample(records, VARIABLES, "Y")
        self.assertEqual(sample.rows[0].record_id, "r-000")
        self.assertNotIn("operator_annotation", sample.rows[0].values)

    def test_duplicate_field_names_are_rejected(self) -> None:
        with self.assertRaises(EquationError):
            prepare_sample(linear_records(), ["X1", "X1"], "Y")

    def test_empty_variable_list_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            prepare_sample(linear_records(), [], "Y")

    def test_target_may_not_be_a_feature(self) -> None:
        with self.assertRaises(EquationError):
            prepare_sample(linear_records(), ["X1", "Y"], "Y")

    def test_non_string_target_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            prepare_sample(linear_records(), VARIABLES, "")

    def test_non_mapping_record_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            prepare_sample([1, 2, 3], VARIABLES, "Y")

    def test_column_lookup(self) -> None:
        sample = sample_of()
        self.assertEqual(sample.column("X1")[0], _x1(0))
        with self.assertRaises(EquationError):
            sample.column("X9")

    def test_subset_marks_restricted_out(self) -> None:
        sample = sample_of()
        part = sample.subset([0, 1, 2])
        self.assertEqual(len(part), 3)
        self.assertEqual(part.excluded["restricted_out"], 9)
        self.assertNotEqual(part.fingerprint, sample.fingerprint)


# ---------------------------------------------------------------------------
# 定义域限制
# ---------------------------------------------------------------------------


class TestDomainRestriction(unittest.TestCase):
    """定义域条件的限制行为。"""

    def test_division_domain_excludes_zero_denominator(self) -> None:
        records = linear_records(8)
        records[4]["values"]["X2"] = 0.0
        sample = prepare_sample(records, VARIABLES, "Y")
        restricted = restrict_to_domain(sample, "X1/X2")
        self.assertEqual(restricted.detail["input"], 8)
        self.assertEqual(restricted.detail["kept"], 7)
        self.assertEqual(restricted.detail["domain_out"], 1)
        self.assertIn("|X2|", restricted.detail["domain"])

    def test_all_rows_out_of_domain(self) -> None:
        records = [
            {"record_id": f"r{i}", "values": {"X1": float(i + 1), "X2": 0.0, "Y": float(i)}}
            for i in range(6)
        ]
        sample = prepare_sample(records, VARIABLES, "Y")
        restricted = restrict_to_domain(sample, "X1/X2")
        self.assertEqual(restricted.detail["kept"], 0)
        self.assertEqual(len(restricted.values), 0)

    def test_sqrt_domain_excludes_negative_argument(self) -> None:
        records = [
            {"record_id": f"r{i}", "values": {"X1": float(i - 3), "X2": 1.0 + i, "Y": float(i)}}
            for i in range(8)
        ]
        sample = prepare_sample(records, VARIABLES, "Y")
        restricted = restrict_to_domain(sample, "sqrt(X1)")
        self.assertEqual(restricted.detail["domain_out"], 3)
        self.assertIn(">= 0", restricted.detail["domain"])

    def test_log_domain_excludes_nonpositive_argument(self) -> None:
        records = [
            {"record_id": f"r{i}", "values": {"X1": float(i - 2), "X2": 1.0 + i, "Y": float(i)}}
            for i in range(8)
        ]
        sample = prepare_sample(records, VARIABLES, "Y")
        restricted = restrict_to_domain(sample, "log(X1)")
        # X1 = -2, -1, 0 被排除（0 不满足 > 0）
        self.assertEqual(restricted.detail["domain_out"], 3)
        self.assertIn("> 0", restricted.detail["domain"])

    def test_value_column_bypasses_domain_check_but_drops_nonfinite(self) -> None:
        sample = sample_of(linear_records(5))
        column = [1.0, 2.0, float("inf"), 4.0, 5.0]
        restricted = restrict_to_domain(sample, column)
        self.assertEqual(restricted.detail["nonfinite"], 1)
        self.assertEqual(restricted.detail["kept"], 4)
        self.assertIn("未检查", restricted.detail["domain"])

    def test_values_stay_aligned_with_rows_after_restriction(self) -> None:
        """限制后 Z 列必须与保留行逐行对应，不得错位。"""
        records = linear_records(6)
        records[0]["values"]["X2"] = 0.0
        sample = prepare_sample(records, VARIABLES, "Y")
        restricted = restrict_to_domain(sample, "X1/X2")
        for row, value in zip(restricted.sample.rows, restricted.values):
            expected = row.values["X1"] / row.values["X2"]
            self.assertAlmostEqual(value, expected, places=12)

    def test_unknown_variable_is_reported(self) -> None:
        with self.assertRaises(EquationError):
            restrict_to_domain(sample_of(), "X9 + 1")

    def test_non_sample_input_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            restrict_to_domain([1, 2, 3], "X1")  # type: ignore[arg-type]

    def test_bad_expression_text_is_reported(self) -> None:
        with self.assertRaises(EquationError):
            restrict_to_domain(sample_of(), "X1 +")

    def test_bare_number_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            restrict_to_domain(sample_of(), 3.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 残差摘要
# ---------------------------------------------------------------------------


class TestResidualSummary(unittest.TestCase):
    """残差与误差指标的口径。"""

    def test_exact_values(self) -> None:
        summary = residual_summary([1.0, 2.0, 3.0], [1.0, 2.0, 5.0])
        self.assertEqual(summary.n, 3)
        self.assertEqual(summary.residuals, (0.0, 0.0, -2.0))
        self.assertAlmostEqual(summary.sse, 4.0)
        self.assertAlmostEqual(summary.mse, 4.0 / 3.0)
        self.assertAlmostEqual(summary.rmse, math.sqrt(4.0 / 3.0))
        self.assertAlmostEqual(summary.mae, 2.0 / 3.0)
        self.assertAlmostEqual(summary.max_abs_error, 2.0)

    def test_perfect_fit_has_zero_residuals(self) -> None:
        summary = residual_summary([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        self.assertEqual(summary.sse, 0.0)
        self.assertEqual(summary.mae, 0.0)
        self.assertTrue(summary.r2_defined)
        self.assertAlmostEqual(summary.r2, 1.0)

    def test_r2_zero_for_constant_prediction(self) -> None:
        truth = [1.0, 2.0, 3.0, 4.0]
        mean = 2.5
        summary = residual_summary(truth, [mean] * 4)
        self.assertAlmostEqual(summary.r2, 0.0, places=12)

    def test_r2_undefined_when_truth_has_no_variation(self) -> None:
        """真值无变异时 R² 无定义，须显式标记而不是给出误导性的数值。"""
        summary = residual_summary([5.0, 5.0, 5.0], [5.0, 5.0, 5.0])
        self.assertFalse(summary.r2_defined)
        self.assertEqual(summary.r2, 1.0)

        summary = residual_summary([5.0, 5.0, 5.0], [5.0, 5.0, 6.0])
        self.assertFalse(summary.r2_defined)
        self.assertEqual(summary.r2, 0.0)

    def test_mean_residual_and_std(self) -> None:
        summary = residual_summary([0.0, 0.0, 0.0], [-1.0, 0.0, 1.0])
        self.assertAlmostEqual(summary.mean_residual, 0.0)
        self.assertAlmostEqual(summary.residual_std, math.sqrt(2.0 / 3.0))

    def test_length_mismatch_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            residual_summary([1.0, 2.0], [1.0])

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            residual_summary([], [])

    def test_non_finite_input_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            residual_summary([1.0, float("nan")], [1.0, 1.0])
        with self.assertRaises(EquationError):
            residual_summary([1.0, 2.0], [1.0, float("inf")])

    def test_non_numeric_input_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            residual_summary([1.0, "x"], [1.0, 1.0])  # type: ignore[list-item]
        with self.assertRaises(EquationError):
            residual_summary("12", "12")

    def test_serialisation_can_omit_pointwise_values(self) -> None:
        summary = residual_summary([1.0, 2.0], [1.0, 1.0])
        self.assertNotIn("residuals", summary.to_dict(with_values=False))
        self.assertIn("residuals", summary.to_dict(with_values=True))


# ---------------------------------------------------------------------------
# 求值、渲染与复杂度
# ---------------------------------------------------------------------------


class TestEvaluationAndComplexity(unittest.TestCase):
    """表达式求值、渲染与复杂度近似。"""

    def test_evaluates_arithmetic(self) -> None:
        point = {"X1": 3.0, "X2": 4.0}
        self.assertAlmostEqual(evaluate_expression(parse("X1 + X2"), point), 7.0)
        self.assertAlmostEqual(evaluate_expression(parse("X1 - X2"), point), -1.0)
        self.assertAlmostEqual(evaluate_expression(parse("X1 * X2"), point), 12.0)
        self.assertAlmostEqual(evaluate_expression(parse("X1 / X2"), point), 0.75)
        self.assertAlmostEqual(evaluate_expression(parse("X1^2"), point), 9.0)
        self.assertAlmostEqual(evaluate_expression(parse("abs(X1 - 5)"), point), 2.0)
        self.assertAlmostEqual(evaluate_expression(parse("sqrt(X2)"), point), 2.0)

    def test_evaluates_raw_binary_ast(self) -> None:
        raw = parse("X1 - X2", canonicalize=False)
        self.assertAlmostEqual(evaluate_expression(raw, {"X1": 5.0, "X2": 2.0}), 3.0)
        raw_div = parse("X1 / X2", canonicalize=False)
        self.assertAlmostEqual(evaluate_expression(raw_div, {"X1": 6.0, "X2": 3.0}), 2.0)

    def test_missing_variable_is_reported(self) -> None:
        with self.assertRaises(EquationError):
            evaluate_expression(parse("X1 + X9"), {"X1": 1.0})

    def test_non_finite_value_is_reported(self) -> None:
        with self.assertRaises(EquationError):
            evaluate_expression(parse("X1"), {"X1": float("inf")})
        with self.assertRaises(EquationError):
            evaluate_expression(parse("X1"), {"X1": True})

    def test_empty_ast_is_rejected(self) -> None:
        with self.assertRaises(EquationError):
            evaluate_expression((), {})

    def test_render_is_readable_and_injective(self) -> None:
        self.assertEqual(render_expression(parse("2*x + 3")), render_expression(parse("2*x + 3")))
        self.assertNotEqual(render_expression(parse("x + 1")), render_expression(parse("x + 2")))
        self.assertIn("X1", render_expression(parse("X1/X2")))

    def test_formula_length_tracks_size(self) -> None:
        small = formula_length(parse("X1"))
        large = formula_length(parse("X1 + X2*3"))
        self.assertLess(small["expression_nodes"], large["expression_nodes"])
        self.assertLess(small["rendered_length"], large["rendered_length"])

    def test_complexity_grows_with_expression_size(self) -> None:
        """复杂度必须单调整于公式规模，才可用于 M5 的复杂度轴。"""
        simple = fit_relation(sample_of(), "X1")
        complex_ = fit_relation(sample_of(), "X1 + 0.1*X2")
        self.assertLess(
            simple.candidate.complexity["total"], complex_.candidate.complexity["total"]
        )

    def test_version_is_declared(self) -> None:
        self.assertEqual(EQUATION_VERSION, "1")
        self.assertEqual(EXPLORATION_PURPOSE, "E")
        self.assertEqual(EXPLORER_ROLE, "explorer")


# ---------------------------------------------------------------------------
# 纯函数性与确定性
# ---------------------------------------------------------------------------


class TestPurityAndDeterminism(unittest.TestCase):
    """本模块不写模块级可变状态，同一输入恒得同一输出。"""

    def test_repeated_fits_are_identical(self) -> None:
        sample = sample_of()
        first = fit_relation(sample, CANDIDATE_TEXT).to_dict()
        second = fit_relation(sample, CANDIDATE_TEXT).to_dict()
        self.assertEqual(first, second)

    def test_call_order_does_not_matter(self) -> None:
        """先算别的候选，不得影响后续结果（无隐藏状态）。"""
        sample = sample_of()
        baseline_before = fit_relation(sample, CANDIDATE_TEXT).to_dict()
        for other in ("X1", "X2", "X1*X2", "sqrt(X1)", "X1/X2"):
            fit_relation(sample, other)
        baseline_after = fit_relation(sample, CANDIDATE_TEXT).to_dict()
        self.assertEqual(baseline_before, baseline_after)

    def test_sample_is_immutable_across_calls(self) -> None:
        sample = sample_of()
        fingerprint = sample.fingerprint
        rows = len(sample.rows)
        fit_relation(sample, "X1/X2")
        fit_relation(sample, "X1")
        self.assertEqual(sample.fingerprint, fingerprint)
        self.assertEqual(len(sample.rows), rows)

    def test_restriction_does_not_mutate_input_sample(self) -> None:
        records = linear_records(8)
        records[2]["values"]["X2"] = 0.0
        sample = prepare_sample(records, VARIABLES, "Y")
        restrict_to_domain(sample, "X1/X2")
        self.assertEqual(len(sample), 8)
        self.assertNotIn("restricted_out", sample.excluded)

    def test_module_has_no_mutable_module_level_containers(self) -> None:
        """模块级不得存在可变的字典 / 列表 / 集合（杜绝跨调用串味）。"""
        import sdl_m03.equations as module

        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertNotIsInstance(value, (list, dict, set))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
