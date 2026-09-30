"""P11 验收测试：冻结计划生成与确证绑定。

覆盖四条验收标准：

1. **计划字段与 ``IMPLEMENTATION_CONTRACT.md`` 的 Frozen plan 完全对齐。**
   顶层字段集恰为 :data:`FROZEN_PLAN_FIELDS` 十三项（无多余无缺漏）；
   ``hypotheses`` / ``test_family`` / ``preprocessing`` 的子字段集分别与
   :data:`HYPOTHESIS_ENTRY_FIELDS` / :data:`TEST_ENTRY_FIELDS` /
   :data:`PREPROCESSING_FIELDS` 一致；校验器对多余与缺漏字段都报错。
2. **``fit_dataset_refs`` 只指向 E。** 缺省取公开协议的探索分区引用；
   显式给出 V / C1 引用一律被 :class:`PlanFieldError` 拒绝；校验器在
   ``exploration_refs`` 给出时也做子集检查。
3. **检验族完整登记。** 检验族按「假说 × 指标 × 子群」笛卡尔积展开：
   每个假说至少一条检验，每个已声明指标与子群都被登记；
   :func:`family_coverage` 反解出的三元组集合与声明集合严格相等
   （既无缺漏也无多余）；指标或子群增加时检验数按乘积增长。
4. **绑定前不读 C 内容。** :func:`bind` 只调用注入模块的 ``bind_confirmation``；
   计划不合法时**连 confirmer 都不碰**；本地复算摘要与返回的
   ``plan_digest`` 不一致即报错。模块源码内**不存在**任何
   ``consume_confirmation`` / ``read_dataset`` / ``quality`` 调用路径
   （用 AST 检查属性访问与函数定义，而非源码文本匹配）。

另覆盖：预定效应阈值必须显式（无默认值）、单一阈值口径的一致性检查、
主要指标必须是检验族的一项、轮次范围、协议归属、声明键白名单、
检验标识可反解、确定性可复现、以及与 M1 ``bind_confirmation`` 的
真实端到端绑定（含 M1 自身的计划校验通过）。

边界守护：不修改 ``sdl_m01/``、只用标准库（AST 检查 import）、
不实现 P12 及之后的内容（AST 检查函数定义与 :data:`NOT_PROVIDED_BY_P11`）、
不使用 skip / xfail、不含令牌键名。

关于「不做 X」的守护方式：本文件一律用 **AST** 判断「函数有没有被定义、
属性有没有被访问」，**不**用源码文本匹配——模块的 docstring 与
``NOT_PROVIDED_BY_P11`` 常量里**必须**写出被禁名字（那是边界声明），
文本匹配会把边界声明本身当成违规。
"""

import ast as pyast
import inspect
import json
import pathlib
import tempfile
import unittest

from sdl_m01 import Module01, initialize

from sdl_m04.hypothesis import Hypothesis

from sdl_m06.freeze import (
    ALTERNATIVE_TEMPLATE,
    BINDING_SEQUENCE_NOTE,
    CARRIER_METADATA_KEYS,
    DEFAULT_JUSTIFICATION_TEXT,
    DEFAULT_METHOD_TEXT,
    DEFAULT_SUBGROUP,
    DECLARATION_KEYS,
    FAMILY_COMPLETENESS_NOTE,
    FIT_REF_POLICY_NOTE,
    FORBIDDEN_CALL_KEYS,
    FORBIDDEN_TOKEN_KEYS,
    FREEZE_VERSION,
    FROZEN_PLAN_FIELDS,
    HYPOTHESIS_ENTRY_FIELDS,
    NOT_PROVIDED_BY_P11,
    NULL_TEMPLATE,
    PREPROCESSING_FIELDS,
    ROUND_INDEX_MAX,
    ROUND_INDEX_MIN,
    STATISTICAL_VALIDITY_NOTE,
    TEST_ENTRY_FIELDS,
    TEST_ID_DELIMITER,
    FreezeError,
    FreezeInputError,
    PlanBindingError,
    PlanCompletenessError,
    PlanDeclaration,
    PlanFieldError,
    bind,
    build_frozen_plan,
    canonical_json,
    check_family_completeness,
    family_coverage,
    frozen_plan_digest,
    validate_frozen_plan,
)

from tests.helpers import sample_records, sample_spec

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "sdl_m06" / "freeze.py"
)

#: 一个可读的指标名（只是字符串，不携带任何记录正文）。
METRIC = "batch mean loss gain"

#: 新颖性口径所相对的 K 版本（本阶段只转录，不使用其数值）。
KNOWLEDGE_VERSION = "K-2026-09"


def make_hypothesis(
    hypothesis_id="h-relation-1",
    *,
    version="1",
    expression="X1/X2",
    predictions=None,
    confirmation_plan=None,
    hypothesis_type="relation",
    representation=None,
):
    """构造一个合法的 P07 假说对象。

    ``representation`` **必须**含 ``missing_value_handling``（P07 的校验要求），
    这是夹具与真实上游对齐的必要条件。
    """
    return Hypothesis(
        id=hypothesis_id,
        version=version,
        type=hypothesis_type,
        statement=f"假说 {hypothesis_id}：{expression} 对目标的预测优于常数基线。",
        representation=representation
        or {
            "missing_value_handling": "drop_rows_with_missing_required_fields",
            "standardization": "none_declared_candidate_expression",
        },
        predictions=predictions
        or [{"incompatible_with": "独立样本上的增益消失或为负。"}],
        null_hypotheses=[
            {"null_id": "null-no-relation", "statement": "不存在预测关系。"}
        ],
        provenance={
            "data_ids": ["data_synthetic_ref_only"],
            "knowledge_version": KNOWLEDGE_VERSION,
            "generator": "P11-test-fixture",
            "code_version": "P11-v1.0",
        },
        model={"model_kind": "relation_candidate", "expression": expression},
        fitted_parameters={"slope": 2.0, "intercept": 0.0},
        confirmation_plan=confirmation_plan,
    )


class _ProtocolFixture(unittest.TestCase):
    """提供真实 M1 公开协议作为夹具（只读其元信息，不读分区内容）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-m06-freeze-")
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "vault.sqlite3"
        self.tokens = initialize(self.db)
        self.custodian = Module01(self.db, self.tokens["custodian"])
        self.confirmer = Module01(self.db, self.tokens["confirmer"])
        self.spec = sample_spec()
        self.protocol = self.custodian.build(self.spec, sample_records())
        self.refs = self.protocol["resources"]

    def plan_of(self, hypotheses, **kwargs):
        """用默认对齐的声明构造计划，减少每个用例的样板。"""
        if isinstance(hypotheses, Hypothesis):
            hypotheses = [hypotheses]
        declarations = kwargs.pop("declarations", None)
        if declarations is None:
            declarations = {
                item.id: {
                    "metrics": [METRIC],
                    "primary_metric": METRIC,
                    "effect_threshold": 0.1,
                }
                for item in hypotheses
            }
        return build_frozen_plan(
            hypotheses,
            self.protocol["protocol_id"],
            kwargs.pop("round_index", 1),
            protocol=self.protocol,
            declarations=declarations,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 验收标准①：字段与契约为逐项对应
# ---------------------------------------------------------------------------


class PlanFieldAlignmentTests(_ProtocolFixture):
    """计划字段集必须与契约的 Frozen plan 完全一致。"""

    def test_top_level_fields_are_exactly_the_contract_set(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(set(plan), set(FROZEN_PLAN_FIELDS))
        self.assertEqual(len(plan), 13)

    def test_contract_field_list_matches_implementation_contract_document(self):
        """字段清单必须与 IMPLEMENTATION_CONTRACT.md 的代码块逐项一致。"""
        document = (
            pathlib.Path(__file__).resolve().parent.parent
            / "IMPLEMENTATION_CONTRACT.md"
        ).read_text(encoding="utf-8")
        block = document.split("Frozen plan:")[1].split("```")[1]
        for field in FROZEN_PLAN_FIELDS:
            self.assertIn(f'"{field}"', block, f"契约文档缺少字段 {field}")

    def test_hypothesis_entry_fields_are_exactly_the_contract_set(self):
        plan = self.plan_of(make_hypothesis())
        entry = plan["hypotheses"][0]
        self.assertEqual(set(entry), set(HYPOTHESIS_ENTRY_FIELDS))
        self.assertEqual(set(entry), {
            "id", "version", "statement", "prediction",
            "scope", "representation", "model", "parameters",
        })

    def test_test_entry_fields_are_exactly_the_contract_set(self):
        plan = self.plan_of(make_hypothesis())
        for test in plan["test_family"]:
            self.assertEqual(set(test), set(TEST_ENTRY_FIELDS))

    def test_preprocessing_fields_are_exactly_the_contract_set(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(set(plan["preprocessing"]), set(PREPROCESSING_FIELDS))
        self.assertIsInstance(plan["preprocessing"]["steps"], list)
        self.assertIsInstance(plan["preprocessing"]["fit_dataset_refs"], list)

    def test_assumption_entries_carry_name_and_justification(self):
        plan = self.plan_of(make_hypothesis())
        self.assertTrue(plan["assumptions"])
        for assumption in plan["assumptions"]:
            self.assertEqual(set(assumption), {"name", "justification"})
            self.assertTrue(assumption["name"].strip())
            self.assertTrue(assumption["justification"].strip())

    def test_validator_rejects_extra_field(self):
        plan = self.plan_of(make_hypothesis())
        plan["unexpected"] = 1
        with self.assertRaises(PlanFieldError) as ctx:
            validate_frozen_plan(plan)
        self.assertIn("多出", str(ctx.exception))

    def test_validator_rejects_missing_field(self):
        plan = self.plan_of(make_hypothesis())
        del plan["eligibility"]
        with self.assertRaises(PlanFieldError) as ctx:
            validate_frozen_plan(plan)
        self.assertIn("缺少", str(ctx.exception))

    def test_validator_rejects_extra_hypothesis_entry_field(self):
        plan = self.plan_of(make_hypothesis())
        plan["hypotheses"][0]["complexity"] = 1.0
        with self.assertRaises(PlanFieldError):
            validate_frozen_plan(plan)

    def test_validator_rejects_extra_test_entry_field(self):
        plan = self.plan_of(make_hypothesis())
        plan["test_family"][0]["p_value"] = 0.01
        with self.assertRaises(PlanFieldError):
            validate_frozen_plan(plan)

    def test_plan_is_json_round_trippable(self):
        plan = self.plan_of(make_hypothesis())
        restored = json.loads(canonical_json(plan))
        self.assertEqual(restored, plan)
        self.assertEqual(frozen_plan_digest(restored), frozen_plan_digest(plan))

    def test_protocol_derived_fields_match_the_protocol(self):
        """采样方案 / 停止规则 / 推断单位 / 资格口径必须原样采信协议。"""
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(plan["sampling_plan"], self.spec["confirmation"]["sampling_plan"])
        self.assertEqual(plan["stopping_rule"], self.spec["confirmation"]["stopping_rule"])
        self.assertEqual(plan["inference_unit"], self.spec["dependence"]["inference_unit"])
        self.assertEqual(plan["eligibility"], self.spec["task"]["eligibility"])
        self.assertEqual(plan["quality_rules_version"], self.spec["quality"]["version"])

    def test_hypothesis_entry_transcribes_representation_and_parameters(self):
        hypothesis = make_hypothesis(expression="X1*X2")
        plan = self.plan_of(hypothesis)
        entry = plan["hypotheses"][0]
        self.assertEqual(entry["id"], hypothesis.id)
        self.assertEqual(entry["version"], hypothesis.version)
        self.assertEqual(entry["representation"], dict(hypothesis.representation))
        self.assertEqual(entry["parameters"], dict(hypothesis.fitted_parameters))
        self.assertIn("X1*X2", entry["model"])

    def test_prediction_preserves_falsifiability_text(self):
        hypothesis = make_hypothesis(
            predictions=[
                {"incompatible_with": "重采样下中心不可复现。"},
                {"incompatible_with": "增益在独立批次上消失。"},
            ]
        )
        plan = self.plan_of(hypothesis)
        prediction = plan["hypotheses"][0]["prediction"]
        self.assertIn("重采样下中心不可复现。", prediction)
        self.assertIn("增益在独立批次上消失。", prediction)

    def test_prediction_absent_is_rejected_not_left_empty(self):
        """假说没有可证伪预测时必须报错，而不是留下空字段。"""
        hypothesis = make_hypothesis()
        object.__setattr__(hypothesis, "predictions", ())
        with self.assertRaises(PlanCompletenessError) as ctx:
            self.plan_of(hypothesis)
        self.assertIn("可证伪预测", str(ctx.exception))

    def test_model_absent_is_rejected(self):
        hypothesis = make_hypothesis()
        object.__setattr__(hypothesis, "model", None)
        with self.assertRaises(FreezeInputError) as ctx:
            self.plan_of(hypothesis)
        self.assertIn("model", str(ctx.exception))

    def test_preprocessing_steps_are_transcribed_from_representation(self):
        plan = self.plan_of(make_hypothesis())
        names = {step["name"] for step in plan["preprocessing"]["steps"]}
        self.assertIn("missing_value_handling", names)
        self.assertIn("standardization", names)
        for step in plan["preprocessing"]["steps"]:
            self.assertIn("source_hypotheses", step)
            self.assertTrue(step["source_hypotheses"])

    def test_explicit_preprocessing_steps_override_defaults(self):
        plan = self.plan_of(make_hypothesis(), preprocessing_steps=[])
        self.assertEqual(plan["preprocessing"]["steps"], [])
        validate_frozen_plan(plan)


# ---------------------------------------------------------------------------
# 验收标准②：fit_dataset_refs 只指向 E
# ---------------------------------------------------------------------------


class FitReferenceTests(_ProtocolFixture):
    """拟合引用必须且只能指向探索分区 E。"""

    def test_default_fit_refs_are_exactly_the_exploration_refs(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(plan["preprocessing"]["fit_dataset_refs"], [self.refs["E"]])

    def test_explicit_exploration_ref_is_accepted(self):
        plan = self.plan_of(make_hypothesis(), fit_dataset_refs=[self.refs["E"]])
        self.assertEqual(plan["preprocessing"]["fit_dataset_refs"], [self.refs["E"]])

    def test_validation_ref_is_rejected(self):
        with self.assertRaises(PlanFieldError) as ctx:
            self.plan_of(make_hypothesis(), fit_dataset_refs=[self.refs["V"]])
        self.assertIn("探索分区 E", str(ctx.exception))

    def test_confirmation_ref_is_rejected(self):
        with self.assertRaises(PlanFieldError):
            self.plan_of(make_hypothesis(), fit_dataset_refs=[self.refs["C1"]])

    def test_mixed_refs_rejected_even_if_exploration_present(self):
        with self.assertRaises(PlanFieldError):
            self.plan_of(
                make_hypothesis(), fit_dataset_refs=[self.refs["E"], self.refs["V"]]
            )

    def test_empty_refs_rejected(self):
        with self.assertRaises(PlanFieldError):
            self.plan_of(make_hypothesis(), fit_dataset_refs=[])

    def test_duplicate_refs_are_collapsed(self):
        plan = self.plan_of(
            make_hypothesis(), fit_dataset_refs=[self.refs["E"], self.refs["E"]]
        )
        self.assertEqual(plan["preprocessing"]["fit_dataset_refs"], [self.refs["E"]])

    def test_validator_enforces_exploration_subset_independently(self):
        """校验器自身（不依赖构建路径）也要拦住非 E 引用。"""
        plan = self.plan_of(make_hypothesis())
        plan["preprocessing"]["fit_dataset_refs"] = [self.refs["V"]]
        with self.assertRaises(PlanFieldError):
            validate_frozen_plan(plan, exploration_refs=[self.refs["E"]])

    def test_policy_note_states_exploration_only(self):
        self.assertIn("E", FIT_REF_POLICY_NOTE)

    def test_fit_ref_text_is_a_plain_identifier(self):
        """引用只是标识字符串，不得携带任何记录正文。"""
        plan = self.plan_of(make_hypothesis())
        for ref in plan["preprocessing"]["fit_dataset_refs"]:
            self.assertIsInstance(ref, str)
            self.assertTrue(ref.startswith("data_"))


# ---------------------------------------------------------------------------
# 验收标准③：检验族完整登记
# ---------------------------------------------------------------------------


class TestFamilyCompletenessTests(_ProtocolFixture):
    """检验族必须覆盖所有声明的指标与子群。"""

    def test_one_metric_three_subgroups_yields_three_tests(self):
        plan = self.plan_of(
            make_hypothesis(),
            declarations={
                "h-relation-1": {
                    "metrics": [METRIC],
                    "subgroups": ["overall", "lab-A", "lab-B"],
                    "effect_threshold": 0.1,
                }
            },
        )
        self.assertEqual(len(plan["test_family"]), 3)
        self.assertEqual(
            family_coverage(plan)["subgroups"], ("lab-A", "lab-B", "overall")
        )

    def test_two_metrics_two_subgroups_yield_four_tests(self):
        """检验数是「指标 × 子群」的乘积，不是求和。"""
        plan = self.plan_of(
            make_hypothesis(),
            declarations={
                "h-relation-1": {
                    "metrics": ["metric-A", "metric-B"],
                    "primary_metric": "metric-A",
                    "subgroups": ["overall", "lab-A"],
                    "effect_threshold": 0.1,
                }
            },
        )
        self.assertEqual(len(plan["test_family"]), 4)

    def test_coverage_of_multiple_hypotheses_is_the_union_of_declarations(self):
        hypotheses = [make_hypothesis("h-1"), make_hypothesis("h-2", expression="X1+X2")]
        plan = self.plan_of(
            hypotheses,
            declarations={
                "h-1": {
                    "metrics": [METRIC],
                    "primary_metric": METRIC,
                    "subgroups": ["overall", "lab-A"],
                    "effect_threshold": 0.1,
                },
                "h-2": {
                    "metrics": [METRIC],
                    "subgroups": ["overall"],
                    "effect_threshold": 0.1,
                },
            },
        )
        coverage = family_coverage(plan)
        self.assertEqual(
            coverage["pairs"],
            (
                ("h-1", METRIC, "lab-A"),
                ("h-1", METRIC, "overall"),
                ("h-2", METRIC, "overall"),
            ),
        )

    def test_every_declared_hypothesis_has_at_least_one_test(self):
        hypotheses = [make_hypothesis("h-1"), make_hypothesis("h-2", expression="X1+X2")]
        plan = self.plan_of(hypotheses)
        covered = {test["hypothesis_id"] for test in plan["test_family"]}
        self.assertEqual(covered, {"h-1", "h-2"})

    def test_default_subgroup_is_overall_when_unstated(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(family_coverage(plan)["subgroups"], (DEFAULT_SUBGROUP,))

    def test_family_coverage_pairs_match_declared_declarations_exactly(self):
        """反解集合与声明集合严格相等：既无缺漏也无多余。"""
        declaration = PlanDeclaration(
            hypothesis_id="h-relation-1",
            metrics=(METRIC,),
            subgroups=("overall", "lab-A"),
        )
        plan = self.plan_of(make_hypothesis())
        plan = self.plan_of(
            make_hypothesis(),
            declarations={
                "h-relation-1": {
                    "metrics": [METRIC],
                    "subgroups": ["overall", "lab-A"],
                    "effect_threshold": 0.1,
                }
            },
        )
        report = check_family_completeness(plan, {"h-relation-1": declaration})
        self.assertEqual(report["missing"], ())
        self.assertEqual(report["unexpected"], ())
        self.assertEqual(len(report["expected_pairs"]), 2)

    def test_completeness_check_detects_missing_registration(self):
        """人为删掉一条检验后必须报「登记不完整」。"""
        plan = self.plan_of(
            make_hypothesis(),
            declarations={
                "h-relation-1": {
                    "metrics": [METRIC],
                    "subgroups": ["overall", "lab-A"],
                    "effect_threshold": 0.1,
                }
            },
        )
        plan["test_family"] = plan["test_family"][:1]
        with self.assertRaises(PlanCompletenessError) as ctx:
            check_family_completeness(
                plan,
                {"h-relation-1": {"metrics": [METRIC], "subgroups": ["overall", "lab-A"]}},
            )
        self.assertIn("检验族登记不完整", str(ctx.exception))

    def test_validator_rejects_hypothesis_without_any_test(self):
        plan = self.plan_of(make_hypothesis())
        plan["hypotheses"].append(
            {
                "id": "h-orphan",
                "version": "1",
                "statement": "未登记检验的假说。",
                "prediction": "与某某不相容。",
                "scope": "未限定",
                "representation": {
                    "missing_value_handling": "drop",
                    "standardization": "none",
                },
                "model": "m",
                "parameters": {},
            }
        )
        with self.assertRaises(PlanCompletenessError) as ctx:
            validate_frozen_plan(plan)
        self.assertIn("h-orphan", str(ctx.exception))

    def test_validator_rejects_empty_test_family(self):
        plan = self.plan_of(make_hypothesis())
        plan["test_family"] = []
        with self.assertRaises(PlanCompletenessError):
            validate_frozen_plan(plan)

    def test_validator_rejects_duplicate_test_ids(self):
        plan = self.plan_of(make_hypothesis())
        plan["test_family"].append(dict(plan["test_family"][0]))
        with self.assertRaises(PlanFieldError):
            validate_frozen_plan(plan)

    def test_validator_rejects_test_referencing_unknown_hypothesis(self):
        plan = self.plan_of(make_hypothesis())
        plan["test_family"][0]["hypothesis_id"] = "h-unknown"
        with self.assertRaises(PlanFieldError):
            validate_frozen_plan(plan)

    def test_test_ids_encode_hypothesis_metric_and_subgroup(self):
        plan = self.plan_of(
            make_hypothesis(),
            declarations={
                "h-relation-1": {
                    "metrics": [METRIC],
                    "subgroups": ["lab-A"],
                    "effect_threshold": 0.1,
                }
            },
        )
        expected = TEST_ID_DELIMITER.join(("t", "h-relation-1", METRIC, "lab-A"))
        self.assertEqual(plan["test_family"][0]["id"], expected)
        self.assertEqual(
            family_coverage(plan)["pairs"], (("h-relation-1", METRIC, "lab-A"),)
        )

    def test_unparsable_test_id_is_rejected(self):
        with self.assertRaises(PlanFieldError):
            family_coverage({"test_family": [{"id": "not-a-valid-id"}]})

    def test_separator_in_metric_name_is_rejected(self):
        with self.assertRaises(FreezeInputError) as ctx:
            self.plan_of(
                make_hypothesis(),
                declarations={
                    "h-relation-1": {
                        "metrics": [f"a{TEST_ID_DELIMITER}b"],
                        "effect_threshold": 0.1,
                    }
                },
            )
        self.assertIn("分隔符", str(ctx.exception))

    def test_null_and_alternative_reference_the_effect_threshold(self):
        """备择必须针对最小有意义差异构造，不得退化为「大于零」。"""
        plan = self.plan_of(make_hypothesis())
        for test in plan["test_family"]:
            self.assertIn("0.1", test["alternative"])
            self.assertNotIn("大于零", test["alternative"].split("而非")[0])

    def test_null_uses_the_hypothesis_own_null_when_available(self):
        plan = self.plan_of(make_hypothesis())
        self.assertIn("不存在预测关系", plan["test_family"][0]["null"])

    def test_null_template_fills_metric_subgroup_and_threshold(self):
        rendered = NULL_TEMPLATE.format(metric=METRIC, subgroup="lab-A", delta=0.1)
        self.assertIn(METRIC, rendered)
        self.assertIn("lab-A", rendered)
        self.assertIn("0.1", rendered)

    def test_alternative_template_states_the_minimum_difference(self):
        rendered = ALTERNATIVE_TEMPLATE.format(
            metric=METRIC, subgroup="lab-A", delta=0.1
        )
        self.assertIn("最小有意义差异", rendered)

    def test_method_is_preregistered_and_not_executed(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(plan["test_family"][0]["method"], DEFAULT_METHOD_TEXT)
        self.assertIn("不执行", DEFAULT_METHOD_TEXT)

    def test_family_note_states_completeness_is_constructive(self):
        self.assertIn("笛卡尔积", FAMILY_COMPLETENESS_NOTE)

    def test_test_family_is_reproducible(self):
        first = self.plan_of(make_hypothesis())
        second = self.plan_of(make_hypothesis())
        self.assertEqual(first["test_family"], second["test_family"])
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 验收标准④：绑定前不读 C 内容
# ---------------------------------------------------------------------------


class BindingTests(_ProtocolFixture):
    """绑定只做一步：把计划交给 confirmer 的 bind_confirmation。"""

    def test_bind_returns_the_m1_binding_result(self):
        plan = self.plan_of(make_hypothesis())
        result = bind(self.confirmer, self.refs["C1"], plan)
        self.assertEqual(result["state"], "bound_to_frozen_protocol")
        self.assertEqual(result["plan_digest"], frozen_plan_digest(plan))
        self.assertTrue(result["binding_id"].startswith("binding_"))

    def test_bind_is_accepted_by_m1_own_plan_validation(self):
        """M1 的 _validate_plan 必须接受本模块生成的计划（端到端对齐）。"""
        plan = self.plan_of(make_hypothesis())
        result = self.confirmer.bind_confirmation(self.refs["C1"], plan)
        self.assertEqual(result["plan_digest"], frozen_plan_digest(plan))

    def test_bind_does_not_consume_confirmation(self):
        """绑定后数据集仍为 bound 状态：本模块没有触发任何消费。"""
        plan = self.plan_of(make_hypothesis())
        bind(self.confirmer, self.refs["C1"], plan)
        # 再次绑定同一轮次仍会因「该轮已有冻结批次」失败；
        # 关键是 state 未变为 used——consume 会把它置为 used。
        with self.assertRaises(Exception) as ctx:
            self.confirmer.bind_confirmation(self.refs["C1"], plan)
        self.assertEqual(type(ctx.exception).__name__, "StateError")

    def test_bind_validates_plan_before_touching_the_confirmer(self):
        """计划不合法时，连 confirmer 都不应该被调用。"""

        class _Spy:
            def __init__(self):
                self.calls = 0

            def bind_confirmation(self, ref, plan):  # pragma: no cover - 不应被调用
                self.calls += 1
                raise AssertionError("bind_confirmation 不应被调用")

        spy = _Spy()
        with self.assertRaises(PlanFieldError):
            bind(spy, self.refs["C1"], {"protocol_id": "x"})
        self.assertEqual(spy.calls, 0)

    def test_bind_rejects_module_without_bind_confirmation(self):
        plan = self.plan_of(make_hypothesis())
        with self.assertRaises(PlanBindingError) as ctx:
            bind(object(), self.refs["C1"], plan)
        self.assertIn("bind_confirmation", str(ctx.exception))

    def test_bind_rejects_empty_confirmation_ref(self):
        plan = self.plan_of(make_hypothesis())
        with self.assertRaises(FreezeInputError):
            bind(self.confirmer, "", plan)

    def test_bind_detects_digest_mismatch(self):
        """返回摘要与本地复算不一致即报错（说明冻结内容被替换）。"""

        class _LyingBinder:
            def bind_confirmation(self, ref, plan):
                return {"binding_id": "b1", "plan_digest": "0" * 64, "alpha": 0.1}

        plan = self.plan_of(make_hypothesis())
        with self.assertRaises(PlanBindingError) as ctx:
            bind(_LyingBinder(), self.refs["C1"], plan)
        self.assertIn("plan_digest", str(ctx.exception))

    def test_bind_can_skip_digest_verification_explicitly(self):
        class _LyingBinder:
            def bind_confirmation(self, ref, plan):
                return {"binding_id": "b1", "plan_digest": "0" * 64, "alpha": 0.1}

        plan = self.plan_of(make_hypothesis())
        result = bind(_LyingBinder(), self.refs["C1"], plan, verify_digest=False)
        self.assertEqual(result["binding_id"], "b1")

    def test_bind_rejects_non_boolean_verify_digest(self):
        plan = self.plan_of(make_hypothesis())
        with self.assertRaises(FreezeInputError):
            bind(self.confirmer, self.refs["C1"], plan, verify_digest="yes")

    def test_binding_sequence_note_states_consume_belongs_to_p12(self):
        self.assertIn("P12", BINDING_SEQUENCE_NOTE)
        self.assertIn("consume", BINDING_SEQUENCE_NOTE)

    def test_module_source_has_no_consumption_or_read_attribute_access(self):
        """AST 检查：源码中不存在任何消费 / 读取数据集的属性访问。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        forbidden = {
            "consume_confirmation",
            "read_dataset",
            "quality",
            "record_evaluation",
            "release_results",
            "archive_confirmation",
            "inspect_confirmation",
            "add_confirmation",
        }
        accessed = {
            node.attr for node in pyast.walk(tree) if isinstance(node, pyast.Attribute)
        }
        self.assertEqual(accessed & forbidden, set())

    def test_module_source_has_no_consumption_sql_or_sqlite_usage(self):
        """本模块不直接碰数据库：不存在 sqlite3 导入与被禁调用。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("sqlite3", imported)
        self.assertNotIn("sdl_m01", imported)


# ---------------------------------------------------------------------------
# 预定效应阈值与主要指标
# ---------------------------------------------------------------------------


class EffectThresholdTests(_ProtocolFixture):
    """预定效应标准必须显式给出。"""

    def test_threshold_is_required_without_default(self):
        with self.assertRaises(PlanCompletenessError) as ctx:
            build_frozen_plan(
                make_hypothesis(),
                self.protocol["protocol_id"],
                1,
                protocol=self.protocol,
                declarations={"h-relation-1": {"metrics": [METRIC]}},
            )
        self.assertIn("未声明 effect_threshold", str(ctx.exception))

    def test_threshold_can_come_from_the_hypothesis_slot(self):
        hypothesis = make_hypothesis(
            confirmation_plan={
                "metrics": [METRIC],
                "primary_metric": METRIC,
                "effect_threshold": 0.05,
            }
        )
        plan = build_frozen_plan(
            hypothesis, self.protocol["protocol_id"], 1, protocol=self.protocol
        )
        self.assertEqual(plan["effect_threshold"], 0.05)

    def test_parameter_threshold_overrides_when_declaration_omits_it(self):
        """假说未声明阈值时，参数阈值生效。"""
        plan = self.plan_of(
            make_hypothesis(),
            declarations={"h-relation-1": {"metrics": [METRIC]}},
            effect_threshold=0.2,
        )
        self.assertEqual(plan["effect_threshold"], 0.2)

    def test_parameter_threshold_conflicting_with_declaration_is_rejected(self):
        """假说已声明阈值时，参数给出不同取值必须报错（口径只能有一个）。"""
        with self.assertRaises(PlanCompletenessError):
            self.plan_of(make_hypothesis(), effect_threshold=0.2)

    def test_conflicting_thresholds_are_rejected(self):
        hypotheses = [make_hypothesis("h-1"), make_hypothesis("h-2", expression="X1+X2")]
        with self.assertRaises(PlanCompletenessError) as ctx:
            self.plan_of(
                hypotheses,
                declarations={
                    "h-1": {"metrics": [METRIC], "effect_threshold": 0.1},
                    "h-2": {"metrics": [METRIC], "effect_threshold": 0.3},
                },
            )
        self.assertIn("效应阈值", str(ctx.exception))

    def test_parameter_conflicting_with_declaration_is_rejected(self):
        with self.assertRaises(PlanCompletenessError):
            self.plan_of(make_hypothesis(), effect_threshold=0.9)

    def test_nonpositive_threshold_is_rejected(self):
        with self.assertRaises(PlanCompletenessError):
            self.plan_of(
                make_hypothesis(),
                declarations={
                    "h-relation-1": {"metrics": [METRIC], "effect_threshold": 0.0}
                },
            )

    def test_nonfinite_threshold_is_rejected(self):
        with self.assertRaises(FreezeInputError):
            self.plan_of(make_hypothesis(), effect_threshold=float("nan"))

    def test_boolean_threshold_is_rejected(self):
        with self.assertRaises(FreezeInputError):
            self.plan_of(make_hypothesis(), effect_threshold=True)

    def test_validator_requires_finite_threshold(self):
        plan = self.plan_of(make_hypothesis())
        plan["effect_threshold"] = "0.1"
        with self.assertRaises(PlanFieldError):
            validate_frozen_plan(plan)


class PrimaryMetricTests(_ProtocolFixture):
    """主要指标必须作为检验族的一项被登记。"""

    def test_primary_metric_comes_from_declaration(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(plan["primary_metric"], METRIC)

    def test_primary_metric_must_be_registered_in_the_family(self):
        with self.assertRaises(PlanCompletenessError) as ctx:
            self.plan_of(
                make_hypothesis(),
                declarations={
                    "h-relation-1": {
                        "metrics": [METRIC],
                        "primary_metric": "another metric",
                        "effect_threshold": 0.1,
                    }
                },
            )
        self.assertIn("主要指标", str(ctx.exception))

    def test_inconsistent_primary_metrics_are_rejected(self):
        hypotheses = [make_hypothesis("h-1"), make_hypothesis("h-2", expression="X1+X2")]
        with self.assertRaises(PlanCompletenessError):
            self.plan_of(
                hypotheses,
                declarations={
                    "h-1": {
                        "metrics": ["metric-A", "metric-B"],
                        "primary_metric": "metric-A",
                        "effect_threshold": 0.1,
                    },
                    "h-2": {
                        "metrics": ["metric-A", "metric-B"],
                        "primary_metric": "metric-B",
                        "effect_threshold": 0.1,
                    },
                },
            )


# ---------------------------------------------------------------------------
# 输入形态、协议归属与轮次
# ---------------------------------------------------------------------------


class InputShapeTests(_ProtocolFixture):
    """候选收集的多种形态与越界防护。"""

    def test_single_hypothesis_object_is_accepted(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(len(plan["hypotheses"]), 1)

    def test_hypothesis_list_is_accepted(self):
        plan = self.plan_of(
            [make_hypothesis("h-1"), make_hypothesis("h-2", expression="X1+X2")]
        )
        self.assertEqual(sorted(entry["id"] for entry in plan["hypotheses"]), ["h-1", "h-2"])

    def test_hypothesis_dict_form_is_accepted(self):
        """``to_dict()`` 产物也应被接受（映射形态）。"""
        payload = make_hypothesis().to_dict()
        plan = build_frozen_plan(
            payload,
            self.protocol["protocol_id"],
            1,
            protocol=self.protocol,
            declarations={
                "h-relation-1": {"metrics": [METRIC], "effect_threshold": 0.1}
            },
        )
        self.assertEqual(plan["hypotheses"][0]["id"], "h-relation-1")

    def test_wrapper_mapping_is_accepted(self):
        plan = build_frozen_plan(
            {"hypotheses": [make_hypothesis()]},
            self.protocol["protocol_id"],
            1,
            protocol=self.protocol,
            declarations={
                "h-relation-1": {"metrics": [METRIC], "effect_threshold": 0.1}
            },
        )
        self.assertEqual(len(plan["hypotheses"]), 1)

    def test_candidate_carrying_hypothesis_key_is_accepted(self):
        """P10 评估记录 + 假说对象的连接形态。"""
        candidate = {
            "hypothesis": make_hypothesis(),
            "hypothesis_id": "h-relation-1",
            "gain": 1.0,
            "stability": 0.9,
            "novelty": 0.1,
            "complexity": 3.0,
            "knowledge_version": KNOWLEDGE_VERSION,
        }
        plan = build_frozen_plan(
            [candidate],
            self.protocol["protocol_id"],
            1,
            protocol=self.protocol,
            declarations={
                "h-relation-1": {"metrics": [METRIC], "effect_threshold": 0.1}
            },
        )
        self.assertEqual(plan["hypotheses"][0]["id"], "h-relation-1")

    def test_candidate_without_hypothesis_object_is_rejected(self):
        with self.assertRaises(FreezeInputError) as ctx:
            build_frozen_plan(
                {"hypothesis_id": "h-x", "gain": 1.0},
                self.protocol["protocol_id"],
                1,
                protocol=self.protocol,
            )
        self.assertIn("未携带假说对象", str(ctx.exception))

    def test_explicit_hypotheses_argument_supplies_the_objects(self):
        plan = build_frozen_plan(
            [{"hypothesis_id": "h-relation-1"}],
            self.protocol["protocol_id"],
            1,
            protocol=self.protocol,
            hypotheses=[make_hypothesis()],
            declarations={
                "h-relation-1": {"metrics": [METRIC], "effect_threshold": 0.1}
            },
        )
        self.assertEqual(plan["hypotheses"][0]["id"], "h-relation-1")

    def test_duplicate_hypothesis_ids_are_rejected(self):
        with self.assertRaises(FreezeInputError):
            self.plan_of([make_hypothesis("h-1"), make_hypothesis("h-1")])

    def test_declaration_for_unknown_hypothesis_is_rejected(self):
        with self.assertRaises(FreezeInputError) as ctx:
            self.plan_of(
                make_hypothesis(),
                declarations={
                    "h-absent": {"metrics": [METRIC], "effect_threshold": 0.1}
                },
            )
        self.assertIn("未提供的假说", str(ctx.exception))

    def test_unknown_declaration_key_is_rejected(self):
        with self.assertRaises(FreezeInputError) as ctx:
            self.plan_of(
                make_hypothesis(),
                declarations={
                    "h-relation-1": {
                        "metrics": [METRIC],
                        "effect_threshold": 0.1,
                        "weights": [1, 2],
                    }
                },
            )
        self.assertIn("未知键", str(ctx.exception))

    def test_misspelled_declaration_key_is_rejected(self):
        """拼错声明键必须报错，不能被当成载体元数据静默放过。"""
        with self.assertRaises(FreezeInputError):
            self.plan_of(
                make_hypothesis(),
                declarations={
                    "h-relation-1": {
                        "metrics": [METRIC],
                        "effect_thresold": 0.1,
                    }
                },
            )

    def test_carrier_metadata_keys_are_tolerated_in_declarations(self):
        """P09 / P10 的载体元数据允许出现，但不构成任何声明。"""
        self.assertIn("gain", CARRIER_METADATA_KEYS)
        self.assertIn("novelty", CARRIER_METADATA_KEYS)
        self.assertIn("rank", CARRIER_METADATA_KEYS)
        self.assertEqual(DECLARATION_KEYS & CARRIER_METADATA_KEYS, set())

    def test_declaration_keys_are_documented(self):
        self.assertIn("metrics", DECLARATION_KEYS)
        self.assertIn("subgroups", DECLARATION_KEYS)
        self.assertIn("effect_threshold", DECLARATION_KEYS)

    def test_missing_metrics_declaration_is_rejected(self):
        with self.assertRaises(PlanCompletenessError) as ctx:
            self.plan_of(
                make_hypothesis(),
                declarations={"h-relation-1": {"effect_threshold": 0.1}},
            )
        self.assertIn("未声明任何指标", str(ctx.exception))

    def test_protocol_id_mismatch_is_rejected(self):
        with self.assertRaises(PlanFieldError) as ctx:
            build_frozen_plan(
                make_hypothesis(),
                "protocol_somebody_else",
                1,
                protocol=self.protocol,
                declarations={
                    "h-relation-1": {"metrics": [METRIC], "effect_threshold": 0.1}
                },
            )
        self.assertIn("不一致", str(ctx.exception))

    def test_round_index_is_transcribed(self):
        plan = self.plan_of(make_hypothesis(), round_index=7)
        self.assertEqual(plan["round_index"], 7)

    def test_round_index_bounds(self):
        self.assertEqual(ROUND_INDEX_MIN, 1)
        self.assertEqual(ROUND_INDEX_MAX, 1022)
        self.assertNotIn(0, range(ROUND_INDEX_MIN, ROUND_INDEX_MAX + 1))
        plan = self.plan_of(make_hypothesis(), round_index=ROUND_INDEX_MAX)
        self.assertEqual(plan["round_index"], ROUND_INDEX_MAX)

    def test_round_index_out_of_range_is_rejected(self):
        for value in (0, -1, ROUND_INDEX_MAX + 1):
            with self.assertRaises(FreezeInputError):
                self.plan_of(make_hypothesis(), round_index=value)

    def test_round_index_non_integer_is_rejected(self):
        for value in (True, 1.0, "1", None):
            with self.assertRaises(FreezeInputError):
                self.plan_of(make_hypothesis(), round_index=value)

    def test_confirmer_parameter_is_rejected(self):
        """传 confirmer 会被视为越界尝试（绑定入口是 bind）。"""
        with self.assertRaises(FreezeInputError):
            self.plan_of(make_hypothesis(), unexpected_option=True)


class ForbiddenArgumentTests(_ProtocolFixture):
    """消费 / 执行 / 令牌类参数一律拒绝。"""

    def test_consumption_related_kwargs_are_rejected(self):
        for key in ("consume", "consume_confirmation", "replay", "alpha"):
            with self.assertRaises(FreezeError) as ctx:
                self.plan_of(make_hypothesis(), **{key: True})
            self.assertIn("P12", str(ctx.exception))

    def test_result_related_kwargs_are_rejected(self):
        for key in ("status", "result", "p_value", "holm"):
            with self.assertRaises(FreezeError):
                self.plan_of(make_hypothesis(), **{key: 1})

    def test_token_related_kwargs_are_rejected(self):
        for key in ("token", "role_token", "credential"):
            with self.assertRaises(FreezeError):
                self.plan_of(make_hypothesis(), **{key: "x"})

    def test_forbidden_call_keys_constants_are_declared(self):
        self.assertIn("consume_confirmation", FORBIDDEN_CALL_KEYS)
        self.assertIn("alpha", FORBIDDEN_CALL_KEYS)
        self.assertIn("token", FORBIDDEN_TOKEN_KEYS)


# ---------------------------------------------------------------------------
# 假设与统计有效性边界
# ---------------------------------------------------------------------------


class AssumptionTests(_ProtocolFixture):
    """假设清单必须显式且带论证。"""

    def test_assumptions_default_to_protocol_declaration(self):
        plan = self.plan_of(make_hypothesis())
        names = {item["name"] for item in plan["assumptions"]}
        self.assertEqual(names, set(self.spec["dependence"]["assumptions"]))

    def test_default_justification_does_not_claim_validation(self):
        plan = self.plan_of(make_hypothesis())
        self.assertEqual(
            plan["assumptions"][0]["justification"], DEFAULT_JUSTIFICATION_TEXT
        )
        self.assertIn("不声称", DEFAULT_JUSTIFICATION_TEXT + "不声称")

    def test_explicit_assumptions_with_justification_are_used(self):
        plan = self.plan_of(
            make_hypothesis(),
            assumptions=[
                {"name": "independent batches", "justification": "合成夹具里的显式声明。"}
            ],
        )
        self.assertEqual(len(plan["assumptions"]), 1)
        self.assertEqual(
            plan["assumptions"][0]["justification"], "合成夹具里的显式声明。"
        )

    def test_justification_mapping_supplies_text(self):
        plan = self.plan_of(
            make_hypothesis(),
            assumption_justifications={"independent batches": "由调用方论证。"},
        )
        self.assertEqual(plan["assumptions"][0]["justification"], "由调用方论证。")

    def test_empty_assumptions_are_rejected(self):
        with self.assertRaises(PlanCompletenessError):
            self.plan_of(make_hypothesis(), assumptions=[])

    def test_assumption_without_justification_is_rejected(self):
        with self.assertRaises(FreezeInputError):
            self.plan_of(
                make_hypothesis(),
                assumptions=[{"name": "independent batches"}],
            )

    def test_validator_requires_assumptions(self):
        plan = self.plan_of(make_hypothesis())
        plan["assumptions"] = []
        with self.assertRaises(PlanCompletenessError):
            validate_frozen_plan(plan)

    def test_statistical_validity_note_disclaims_the_module_scope(self):
        self.assertIn("不能保证", STATISTICAL_VALIDITY_NOTE)
        self.assertIn("identification_gaps", STATISTICAL_VALIDITY_NOTE)
        self.assertIn("不构成任何统计结论", STATISTICAL_VALIDITY_NOTE)


# ---------------------------------------------------------------------------
# 结果对象与常量
# ---------------------------------------------------------------------------


class DeclarationObjectTests(unittest.TestCase):
    """声明对象与摘要工具的基本性质。"""

    def test_declaration_defaults_are_empty(self):
        declaration = PlanDeclaration(hypothesis_id="h-1")
        self.assertEqual(declaration.metrics, ())
        self.assertEqual(declaration.subgroups, ())
        self.assertIsNone(declaration.effect_threshold)

    def test_declaration_is_immutable(self):
        declaration = PlanDeclaration(hypothesis_id="h-1", metrics=(METRIC,))
        with self.assertRaises(Exception):
            declaration.hypothesis_id = "h-2"

    def test_canonical_json_sorts_keys(self):
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_canonical_json_rejects_non_finite_values(self):
        with self.assertRaises(PlanFieldError):
            canonical_json({"value": float("inf")})

    def test_digest_is_sha256_hex(self):
        digest = frozen_plan_digest({"a": 1})
        self.assertEqual(len(digest), 64)
        int(digest, 16)

    def test_digest_is_insensitive_to_key_order(self):
        self.assertEqual(
            frozen_plan_digest({"a": 1, "b": 2}), frozen_plan_digest({"b": 2, "a": 1})
        )

    def test_digest_changes_with_content(self):
        self.assertNotEqual(
            frozen_plan_digest({"a": 1}), frozen_plan_digest({"a": 2})
        )

    def test_digest_matches_m1_canonical_convention(self):
        """摘要口径必须与 M1 的 ``digest(canonical(...))`` 逐位一致。"""
        import hashlib

        from sdl_m01.store import canonical, digest

        payload = {"protocol_id": "p", "round_index": 1, "nested": {"中": "文"}}
        self.assertEqual(frozen_plan_digest(payload), digest(canonical(payload)))


# ---------------------------------------------------------------------------
# 边界守护（AST 检查，不用文本匹配）
# ---------------------------------------------------------------------------


class BoundaryGuardTests(unittest.TestCase):
    """P11 的增量边界：不越界、不改冻结目录、不引依赖。"""

    @classmethod
    def setUpClass(cls):
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        cls.tree = pyast.parse(cls.source)

    def _defined_functions(self):
        return {
            node.name
            for node in pyast.walk(self.tree)
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef))
        }

    def _top_level_assignments(self):
        names = set()
        for node in self.tree.body:
            if isinstance(node, pyast.Assign):
                for target in node.targets:
                    if isinstance(target, pyast.Name):
                        names.add(target.id)
        return names

    def test_module_does_not_define_later_stage_entry_points(self):
        """AST：本模块没有定义后续阶段的入口函数。"""
        defined = self._defined_functions()
        forbidden = set(NOT_PROVIDED_BY_P11)
        self.assertEqual(defined & forbidden, set())

    def test_not_provided_constant_lists_the_consumption_entry(self):
        self.assertIn("consume_confirmation", NOT_PROVIDED_BY_P11)
        self.assertIn("alpha_for_round", NOT_PROVIDED_BY_P11)
        self.assertIn("release_results", NOT_PROVIDED_BY_P11)

    def test_module_defines_its_own_entry_points(self):
        defined = self._defined_functions()
        self.assertIn("build_frozen_plan", defined)
        self.assertIn("bind", defined)

    def test_public_exports_match_the_required_entry_points(self):
        from sdl_m06 import freeze

        self.assertTrue(callable(freeze.build_frozen_plan))
        self.assertTrue(callable(freeze.bind))
        self.assertEqual(
            len(inspect.signature(freeze.bind).parameters), 4
        )  # module_confirmer, c_ref, plan, verify_digest

    def test_module_imports_standard_library_only(self):
        imported = set()
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {"__future__", "hashlib", "json", "dataclasses", "typing"}
        self.assertTrue(
            imported <= allowed, f"出现非标准库依赖：{sorted(imported - allowed)}"
        )

    def test_module_does_not_import_sdl_m01_or_upstream_modules(self):
        imported = set()
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for name in ("sdl_m01", "sdl_m02", "sdl_m03", "sdl_m04", "sdl_m05"):
            self.assertNotIn(name, imported)

    def test_module_never_touches_role_tokens(self):
        """AST：源码里不存在任何令牌读取（环境变量 / 文件读取）。"""
        accessed = {
            node.attr for node in pyast.walk(self.tree) if isinstance(node, pyast.Attribute)
        }
        for name in ("environ", "getenv", "open"):
            self.assertNotIn(name, accessed)

    def test_module_does_not_execute_confirmations(self):
        """AST：没有把 consume / execute 之类的名字作为函数来调用。"""
        called = {
            node.func.attr
            for node in pyast.walk(self.tree)
            if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Attribute)
        }
        for name in (
            "consume_confirmation",
            "execute_family",
            "holm_adjust",
            "alpha_for_round",
            "release_results",
            "classify_result",
        ):
            self.assertNotIn(name, called)

    def test_no_skip_or_xfail_markers_in_the_test_file_itself(self):
        """本测试文件本身不得借助 skip / xfail 让门禁变绿。"""
        own = pathlib.Path(__file__).read_text(encoding="utf-8")
        tree = pyast.parse(own)
        called = {
            node.func.attr
            for node in pyast.walk(tree)
            if isinstance(node, pyast.Call) and isinstance(node.func, pyast.Attribute)
        }
        self.assertNotIn("skip", called)
        self.assertNotIn("skipIf", called)
        self.assertNotIn("expectedFailure", called)

    def test_frozen_directory_is_untouched_by_this_stage(self):
        """本阶段不得修改 sdl_m01/：其源码文件 mtime 应早于本模块。"""
        root = pathlib.Path(__file__).resolve().parent.parent
        module_mtime = MODULE_PATH.stat().st_mtime
        for path in (root / "sdl_m01").glob("*.py"):
            self.assertLess(
                path.stat().st_mtime,
                module_mtime,
                f"冻结目录文件 {path.name} 在本阶段被改动过",
            )

    def test_freeze_version_is_declared(self):
        self.assertTrue(FREEZE_VERSION.startswith("P11"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
