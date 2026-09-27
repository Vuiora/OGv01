"""P14 验收测试：主动取证与反例搜索。

覆盖三条验收标准：

1. **排序标注为启发式而非 EIG。** 排序值是「候选预测分歧 / 采样成本」这一
   单一比值，结果对象恒输出 ``ranking_basis == "heuristic"`` 与
   ``is_heuristic is True``；模块内**不存在**任何名为
   ``expected_information_gain`` / ``eig`` / ``mutual_information`` 的函数、
   类或字段（用 AST 复核，见 :func:`sdl_m07.acquisition.self_check`）；
   任何试图传入假说概率、观测模型、信息量的参数都在调用点被拒绝。
2. **确证类取证须含「采样前冻结」声明。** ``confirmation`` 与
   ``counterexample`` 两种目的强制要求 ``pre_sampling_freeze``，且
   ``freeze_before_sampling`` 必须字面为 ``True``、``frozen_candidates`` 与
   ``frozen_protocol`` 均须非空；缺任一项即 :class:`AcquisitionPolicyError`，
   且模块**不提供**关闭该校验的参数。
3. **计划不自动执行采集。** :data:`ACQUISITION_PLAN_EXECUTES_COLLECTION` 恒为
   ``False``；计划对象的 ``executes_collection`` / ``is_advisory`` 是常量属性；
   模块不定义任何数据访问或采集入口（AST 复核）。

另覆盖：分歧口径（「没算」与「算出来是 0」可区分）、缺成本不被当作 0 成本、
边界接近度只作平局裁决不参与加权、预算与条目上限的排除清单、确定性可复现、
输入形态（对象 / 字典 / 生成器）、封存记录字段与令牌键名的拒绝、
以及 P14 的边界守护——不修改 ``sdl_m01/``、不引入第三方依赖、不使用
skip / xfail、不实现 P15/P16 的内容。
"""

import ast as pyast
import inspect
import json
import math
import pathlib
import unittest

from sdl_m07.acquisition import (
    ACQUISITION_GRANTS_EVIDENCE_GRADE,
    ACQUISITION_ITEM_NOTE,
    ACQUISITION_PLAN_EXECUTES_COLLECTION,
    ACQUISITION_PURPOSES,
    ACQUISITION_RANKING_IS_HEURISTIC,
    ACQUISITION_VERSION,
    BOUNDARY_PROXIMITY_EPSILON,
    BOUNDARY_TIEBREAK_NOTE,
    COMPARISON_TOLERANCE,
    CONFIRMATION_GRADE_PURPOSES,
    DEFAULT_MAX_ITEMS,
    DIVERGENCE_EPSILON,
    FORBIDDEN_RANKING_KEYS,
    FORBIDDEN_RANKING_NAMES,
    FORBIDDEN_TOKEN_KEYS,
    FREEZE_BEFORE_SAMPLING_FLAG,
    HEURISTIC_DEFINITION_NOTE,
    MISSING_COST_POLICY,
    MISSING_PREDICTION_POLICY,
    NOT_PROVIDED_BY_P14,
    PLAN_IS_ADVISORY_NOTE,
    PRE_SAMPLING_FREEZE_NOTE,
    PRE_SAMPLING_FREEZE_REQUIRED_KEYS,
    PURPOSE_CONFIRMATION,
    PURPOSE_COUNTEREXAMPLE,
    PURPOSE_EXPLORATION,
    PURPOSE_FREEZE_REQUIREMENT,
    RANKING_BASIS_HEURISTIC,
    RANKING_NOT_EIG_NOTE,
    SEALED_KEYS,
    UNDETERMINED_DIVERGENCE,
    VALUE_MODE_NORMALIZED,
    VALUE_MODE_SPREAD,
    VALUE_MODES,
    AcquisitionError,
    AcquisitionInputError,
    AcquisitionPlan,
    AcquisitionPolicyError,
    DivergenceReport,
    ObservationDivergence,
    acquisition_plan,
    divergence,
    self_check,
)

# ---------------------------------------------------------------------------
# 夹具：确定性合成输入
# ---------------------------------------------------------------------------

#: 模块源码路径，用于静态检查第三方依赖与边界声明。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m07" / "acquisition.py"

#: 测试文件自身路径，用于检查本文件未使用 skip / xfail。
TEST_PATH = pathlib.Path(__file__).resolve()

#: 竞争假说标识。
H1 = "h-alpha@1"
H2 = "h-beta@1"
H3 = "h-gamma@1"


def observation(
    observation_id: str,
    predictions: dict | None,
    *,
    cost: float | None = None,
    boundary_distance: float | None = None,
    near_boundary: bool | None = None,
) -> dict:
    """构造一条字典形态的建议观测。"""
    record: dict = {"observation_id": observation_id, "predictions": predictions or {}}
    if cost is not None:
        record["sampling_cost"] = cost
    if boundary_distance is not None:
        record["boundary_distance"] = boundary_distance
    if near_boundary is not None:
        record["near_boundary"] = near_boundary
    return record


class _ObservationObject:
    """对象形态的建议观测，用于验证鸭子类型读取路径。"""

    def __init__(
        self,
        observation_id: str,
        predictions: dict,
        sampling_cost: float | None = None,
        boundary_distance: float | None = None,
        near_boundary: bool | None = None,
    ) -> None:
        self.observation_id = observation_id
        self.predictions = predictions
        self.sampling_cost = sampling_cost
        self.boundary_distance = boundary_distance
        self.near_boundary = near_boundary


class _HypothesisObject:
    """对象形态的假说，只提供 ``identity`` 属性。"""

    def __init__(self, identity: str) -> None:
        self.identity = identity


def freeze_declaration(**overrides) -> dict:
    """构造一份合法的「采样前冻结」声明。"""
    payload = {
        "freeze_before_sampling": True,
        "frozen_candidates": [H1, H2],
        "frozen_protocol": {
            "sampling_plan": "new independent batches",
            "primary_metric": "mse_improvement",
            "effect_threshold": 0.01,
            "stopping_rule": "fixed batch count",
        },
        "frozen_at": "2026-09-23T06:00:00+08:00",
    }
    payload.update(overrides)
    return payload


def report_of(observations, hypotheses=(H1, H2), **kwargs) -> DivergenceReport:
    """构造分歧报告的便捷夹具。"""
    return divergence(list(hypotheses), observations, **kwargs)


# ---------------------------------------------------------------------------
# 验收标准①：排序标注为启发式而非 EIG
# ---------------------------------------------------------------------------


class TestHeuristicRankingLabel(unittest.TestCase):
    """验收标准①：排序值明确标注为启发式，不得命名为预期信息增益。"""

    def test_ranking_basis_constant_is_heuristic(self):
        """排序依据是字面量 ``heuristic``，且启发式标记恒为 True。"""
        self.assertEqual(RANKING_BASIS_HEURISTIC, "heuristic")
        self.assertIs(ACQUISITION_RANKING_IS_HEURISTIC, True)
        self.assertIs(type(ACQUISITION_RANKING_IS_HEURISTIC), bool)

    def test_divergence_report_declares_heuristic(self):
        """分歧报告恒输出启发式声明，且显式否认是 EIG。"""
        report = report_of(
            [observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)]
        )
        payload = report.to_dict()
        self.assertEqual(payload["ranking_basis"], "heuristic")
        self.assertIs(payload["is_heuristic"], True)
        self.assertIs(payload["is_expected_information_gain"], False)
        # 口径原文必须随对象输出，不能只在 docstring 里。
        self.assertEqual(payload["ranking_note"], RANKING_NOT_EIG_NOTE)
        self.assertIn("预期信息增益", payload["ranking_note"])
        self.assertEqual(payload["heuristic_definition_note"], HEURISTIC_DEFINITION_NOTE)

    def test_plan_declares_heuristic(self):
        """取证计划同样恒输出启发式声明。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        plan = acquisition_plan(report)
        payload = plan.to_dict()
        self.assertEqual(payload["ranking_basis"], "heuristic")
        self.assertIs(payload["is_heuristic"], True)
        self.assertIs(payload["is_expected_information_gain"], False)
        self.assertEqual(plan.ranking_basis, "heuristic")
        self.assertIs(plan.is_heuristic, True)

    def test_entry_declares_heuristic(self):
        """逐观测条目也携带启发式标记，便于单条复核。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        entry = report.entries[0]
        self.assertIsInstance(entry, ObservationDivergence)
        self.assertEqual(entry.to_dict()["ranking_basis"], "heuristic")
        self.assertIs(entry.to_dict()["is_heuristic"], True)
        self.assertIs(entry.to_dict()["is_expected_information_gain"], False)

    def test_ranking_value_field_named_heuristic_value(self):
        """排序值字段名为 ``heuristic_value``，不是任何信息论名称。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=2.0)])
        entry = report.entries[0]
        # 分歧 3.0 ÷ 成本 2.0
        self.assertAlmostEqual(entry.heuristic_value, 1.5, places=12)
        keys = entry.to_dict().keys()
        self.assertIn("heuristic_value", keys)
        for forbidden in FORBIDDEN_RANKING_NAMES:
            self.assertNotIn(forbidden, keys)

    def test_module_does_not_define_eig_names(self):
        """AST 复核：模块没有定义任何 EIG 类名字（含字段名同形的方法）。"""
        result = self_check()
        self.assertEqual(result["defined_forbidden_ranking"], ())
        self.assertIs(result["ok"], True)

    def test_eig_names_actually_present_as_declarations(self):
        """被禁名字**必须**出现在边界声明中，证明上面那条检查不是因为名单为空。"""
        self.assertIn("expected_information_gain", FORBIDDEN_RANKING_NAMES)
        self.assertIn("mutual_information", FORBIDDEN_RANKING_NAMES)
        self.assertGreaterEqual(len(FORBIDDEN_RANKING_NAMES), 5)

    def test_eig_like_kwargs_rejected_on_divergence(self):
        """``divergence`` 拒绝假说概率 / 观测模型 / 信息量等参数。"""
        for key in (
            "expected_information_gain",
            "information_gain",
            "mutual_information",
            "entropy",
            "likelihood",
            "prior",
            "posterior",
            "observation_model",
            "hypothesis_probabilities",
        ):
            with self.subTest(key=key):
                with self.assertRaises(AcquisitionPolicyError) as ctx:
                    divergence(
                        [H1, H2],
                        [observation("o1", {H1: 1.0, H2: 2.0})],
                        **{key: 1.0},
                    )
                self.assertIn("启发式", str(ctx.exception))
                self.assertIn("预期信息增益", str(ctx.exception))

    def test_eig_like_kwargs_rejected_on_plan(self):
        """``acquisition_plan`` 同样拒绝 EIG 类参数与加权总分入口。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 2.0}, cost=1.0)])
        for key in ("weights", "total_score", "utility", "information"):
            with self.subTest(key=key):
                with self.assertRaises(AcquisitionPolicyError):
                    acquisition_plan(report, **{key: {"gain": 1.0}})

    def test_substring_fallback_blocks_renaming(self):
        """子串兜底：换个名字（如 ``my_entropy_hack``）同样被挡。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 2.0}, cost=1.0)])
        with self.assertRaises(AcquisitionPolicyError):
            acquisition_plan(report, **{"custom_information_bonus": 1.0})
        with self.assertRaises(AcquisitionPolicyError):
            divergence(
                [H1, H2],
                [observation("o1", {H1: 1.0, H2: 2.0})],
                **{"shannon_entropy_estimate": 0.5},
            )

    def test_ranking_basis_constant_not_a_probability_model(self):
        """启发式口径声明中明说缺少三样前提（概率、似然、信息量）。"""
        for token in ("概率", "似然", "信息量"):
            self.assertIn(token, RANKING_NOT_EIG_NOTE)

    def test_forbidden_ranking_keys_nonempty(self):
        """禁止入参名单非空且有兜底语义。"""
        self.assertGreaterEqual(len(FORBIDDEN_RANKING_KEYS), 10)
        self.assertIn("expected_information_gain", FORBIDDEN_RANKING_KEYS)


# ---------------------------------------------------------------------------
# 验收标准②：确证类取证须含「采样前冻结」声明
# ---------------------------------------------------------------------------


class TestPreSamplingFreeze(unittest.TestCase):
    """验收标准②：涉及确证的数据采集，计划中必须写明采样前固定候选与方案。"""

    def test_confirmation_grade_purposes_are_declared(self):
        """确证类取证目的集合显式声明，含确证与反例搜索。"""
        self.assertEqual(
            CONFIRMATION_GRADE_PURPOSES,
            frozenset({PURPOSE_CONFIRMATION, PURPOSE_COUNTEREXAMPLE}),
        )
        self.assertNotIn(PURPOSE_EXPLORATION, CONFIRMATION_GRADE_PURPOSES)
        # 三个目的都要有对应的冻结要求说明。
        for purpose in ACQUISITION_PURPOSES:
            self.assertIn(purpose, PURPOSE_FREEZE_REQUIREMENT)
            self.assertTrue(PURPOSE_FREEZE_REQUIREMENT[purpose].strip())

    def test_counterexample_is_confirmation_grade(self):
        """反例搜索的产物会进入确证链条，因此与确证取证同受约束。"""
        self.assertIn(PURPOSE_COUNTEREXAMPLE, CONFIRMATION_GRADE_PURPOSES)

    def test_confirmation_without_freeze_is_rejected(self):
        """确证取证缺 ``pre_sampling_freeze`` → 拒绝，且报错说明必填项。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        with self.assertRaises(AcquisitionPolicyError) as ctx:
            acquisition_plan(report, purpose=PURPOSE_CONFIRMATION)
        message = str(ctx.exception)
        self.assertIn("采样前", message)
        for key in PRE_SAMPLING_FREEZE_REQUIRED_KEYS:
            self.assertIn(key, message)

    def test_counterexample_without_freeze_is_rejected(self):
        """反例搜索同样不能省略冻结声明。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        with self.assertRaises(AcquisitionPolicyError):
            acquisition_plan(report, purpose=PURPOSE_COUNTEREXAMPLE)

    def test_exploration_does_not_require_freeze(self):
        """探索取证不进入确证链条，不要求冻结声明。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        plan = acquisition_plan(report, purpose=PURPOSE_EXPLORATION)
        self.assertIs(plan.requires_pre_sampling_freeze, False)
        self.assertIs(plan.pre_sampling_freeze_declared, False)
        self.assertIsNone(plan.pre_sampling_freeze)
        self.assertEqual(plan.ids(), ("o1",))

    def test_freeze_flag_must_be_literally_true(self):
        """``freeze_before_sampling`` 必须字面为 True；真值替代品一律拒绝。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for bad in (False, 1, "true", "yes"):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionPolicyError) as ctx:
                    acquisition_plan(
                        report,
                        purpose=PURPOSE_CONFIRMATION,
                        pre_sampling_freeze=freeze_declaration(
                            freeze_before_sampling=bad
                        ),
                    )
                self.assertIn("True", str(ctx.exception))

    def test_freeze_flag_none_counts_as_missing(self):
        """``freeze_before_sampling=None`` 视为未提供该笔填项，同样被拒。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        with self.assertRaises(AcquisitionPolicyError) as ctx:
            acquisition_plan(
                report,
                purpose=PURPOSE_CONFIRMATION,
                pre_sampling_freeze=freeze_declaration(freeze_before_sampling=None),
            )
        self.assertIn(FREEZE_BEFORE_SAMPLING_FLAG, str(ctx.exception))
        self.assertIn("必填项", str(ctx.exception))

    def test_frozen_candidates_must_be_nonempty(self):
        """``frozen_candidates`` 不得缺失或为空。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for bad in (None, [], (), ""):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionPolicyError):
                    acquisition_plan(
                        report,
                        purpose=PURPOSE_CONFIRMATION,
                        pre_sampling_freeze=freeze_declaration(
                            frozen_candidates=bad
                        ),
                    )

    def test_frozen_protocol_must_be_nonempty(self):
        """``frozen_protocol`` 不得缺失或为空。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for bad in (None, {}):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionPolicyError):
                    acquisition_plan(
                        report,
                        purpose=PURPOSE_CONFIRMATION,
                        pre_sampling_freeze=freeze_declaration(frozen_protocol=bad),
                    )

    def test_missing_required_key_reported_by_name(self):
        """任一笔填项缺失时，报错信息点名缺了哪一项。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        payload = freeze_declaration()
        del payload["frozen_protocol"]
        with self.assertRaises(AcquisitionPolicyError) as ctx:
            acquisition_plan(
                report,
                purpose=PURPOSE_CONFIRMATION,
                pre_sampling_freeze=payload,
            )
        self.assertIn("frozen_protocol", str(ctx.exception))

    def test_valid_freeze_is_echoed_into_plan_and_items(self):
        """合法声明被原样带入计划与每条入选建议，便于逐条复核。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        plan = acquisition_plan(
            report,
            purpose=PURPOSE_CONFIRMATION,
            pre_sampling_freeze=freeze_declaration(),
        )
        self.assertIs(plan.requires_pre_sampling_freeze, True)
        self.assertIs(plan.pre_sampling_freeze_declared, True)
        self.assertEqual(plan.frozen_candidate_ids(), (H1, H2))
        self.assertIs(plan.pre_sampling_freeze[FREEZE_BEFORE_SAMPLING_FLAG], True)
        item = plan.items[0]
        self.assertIs(item.confirmation_grade, True)
        self.assertIs(item.freeze_declared, True)
        self.assertEqual(item.purpose, PURPOSE_CONFIRMATION)
        self.assertEqual(item.to_dict()["purpose"], PURPOSE_CONFIRMATION)
        payload = plan.to_dict()
        self.assertIs(payload["requires_pre_sampling_freeze"], True)
        self.assertIs(payload["pre_sampling_freeze_declared"], True)
        self.assertEqual(payload["frozen_candidate_ids"], [H1, H2])
        self.assertEqual(payload["pre_sampling_freeze_note"], PRE_SAMPLING_FREEZE_NOTE)

    def test_freeze_note_present_in_every_plan(self):
        """冻结口径原文随每个计划输出——探索类也不例外（口径不应按情形消失）。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for purpose in ACQUISITION_PURPOSES:
            with self.subTest(purpose=purpose):
                plan = acquisition_plan(
                    report,
                    purpose=purpose,
                    pre_sampling_freeze=freeze_declaration(),
                )
                self.assertEqual(
                    plan.to_dict()["pre_sampling_freeze_note"],
                    PRE_SAMPLING_FREEZE_NOTE,
                )

    def test_no_parameter_can_disable_freeze_check(self):
        """**关键**：不存在任何可以关闭冻结校验的参数。

        未知参数一律被拒（不静默忽略），且即使参数名听起来像是「放宽校验」的
        开关，缺声明的确证类调用依然失败——该校验不受任何入参影响。
        """
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for key in (
            "skip_freeze_check",
            "allow_missing_freeze",
            "require_freeze",
            "enforce_freeze",
            "strict_freeze",
        ):
            with self.subTest(key=key):
                with self.assertRaises(AcquisitionError):
                    acquisition_plan(
                        report,
                        purpose=PURPOSE_CONFIRMATION,
                        **{key: False},
                    )
        # 决定性用例：把「看起来能关掉校验」的参数与**合法**冻结声明一起传，
        # 参数被拒的事实证明该校验在调用层就无法被参数化，而不是碰巧缺声明。
        with self.assertRaises(AcquisitionError):
            acquisition_plan(
                report,
                purpose=PURPOSE_CONFIRMATION,
                pre_sampling_freeze=freeze_declaration(),
                **{"require_freeze": False},
            )

    def test_freeze_validation_has_no_bypass_path(self):
        """签名复核：``acquisition_plan`` 的具名参数里没有任何校验开关。"""
        signature = inspect.signature(acquisition_plan)
        allowed = {
            "source",
            "purpose",
            "max_items",
            "cost_budget",
            "pre_sampling_freeze",
            "rejected",
        }
        self.assertEqual(set(signature.parameters), allowed)
        # 唯一的可变参数必须是 ``**kwargs``（用于拒绝未知参数），
        # 而不是可以吞掉任意口径开关并反过来使用它们。
        self.assertEqual(
            signature.parameters["rejected"].kind,
            inspect.Parameter.VAR_KEYWORD,
        )

    def test_invalid_purpose_rejected(self):
        """取证目的必须落在受控词表内。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for bad in ("comfirmation", "", None, 123, "EXPLORATION"):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionInputError):
                    acquisition_plan(report, purpose=bad)

    def test_freeze_declaration_carries_no_sealed_fields(self):
        """冻结声明同样要过封存字段闸门。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        with self.assertRaises(AcquisitionPolicyError):
            acquisition_plan(
                report,
                purpose=PURPOSE_CONFIRMATION,
                pre_sampling_freeze=freeze_declaration(
                    frozen_protocol={"snapshot": {"records": []}}
                ),
            )


# ---------------------------------------------------------------------------
# 验收标准③：计划为纯建议对象，不自动触发采集
# ---------------------------------------------------------------------------


class TestAdvisoryOnly(unittest.TestCase):
    """验收标准③：计划不自动执行采集。"""

    def test_constant_declares_no_collection(self):
        """模块级常量恒声明「不执行采集」「不授予证据等级」。"""
        self.assertIs(ACQUISITION_PLAN_EXECUTES_COLLECTION, False)
        self.assertIs(ACQUISITION_GRANTS_EVIDENCE_GRADE, False)

    def test_plan_properties_are_constant(self):
        """计划对象的三个定位属性是常量属性，不参与构造。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        for purpose in ACQUISITION_PURPOSES:
            with self.subTest(purpose=purpose):
                plan = acquisition_plan(
                    report,
                    purpose=purpose,
                    pre_sampling_freeze=freeze_declaration(),
                )
                self.assertIs(plan.executes_collection, False)
                self.assertIs(plan.is_advisory, True)
                self.assertIs(plan.grants_evidence_grade, False)

    def test_plan_payload_declares_advisory_status(self):
        """序列化载荷恒含三项定位声明与口径原文。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        payload = acquisition_plan(report).to_dict()
        self.assertIs(payload["executes_collection"], False)
        self.assertIs(payload["is_advisory"], True)
        self.assertIs(payload["grants_evidence_grade"], False)
        self.assertEqual(payload["plan_is_advisory_note"], PLAN_IS_ADVISORY_NOTE)

    def test_plan_dataclass_has_no_collection_constructor_field(self):
        """构造签名里不存在任何「执行 / 采集 / 获取」开关。"""
        import dataclasses

        names = {f.name for f in dataclasses.fields(AcquisitionPlan)}
        for forbidden in ("execute", "executes_collection", "collect", "fetch", "run"):
            self.assertNotIn(forbidden, names)

    def test_module_defines_no_access_or_collection_entry(self):
        """AST 复核：模块没有定义任何数据访问或采集入口名。"""
        result = self_check()
        self.assertEqual(result["defined_other_stages"], ())
        self.assertEqual(result["accessed_forbidden"], ())
        self.assertIs(result["ok"], True)

    def test_forbidden_access_names_present_as_declarations(self):
        """被禁的访问名必须写在边界声明里，且确实不出现在函数定义中。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in pyast.walk(tree)
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef))
        }
        for name in ("read_dataset", "consume_confirmation", "fetch", "collect", "sqlite3"):
            with self.subTest(name=name):
                self.assertIn(name, NOT_PROVIDED_BY_P14)
                self.assertNotIn(name, defined)

    def test_plan_object_is_frozen(self):
        """计划对象不可变：外部无法事后塞入采集动作。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        plan = acquisition_plan(report)
        with self.assertRaises(Exception):
            plan.items = ()  # type: ignore[misc]
        with self.assertRaises(Exception):
            plan.purpose = PURPOSE_CONFIRMATION  # type: ignore[misc]

    def test_no_import_of_data_access_modules(self):
        """静态复核：模块只依赖标准库，未导入任何数据访问相关模块。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        third_party_like = {"sqlite3", "requests", "numpy", "pandas", "scipy"}
        self.assertEqual(imported & third_party_like, set())
        # 也不应导入本项目其它模块（保持 M7 与探索/确证侧解耦）。
        others = {f"sdl_m0{n}" for n in range(1, 9)} | {"sdl_pipeline"}
        self.assertEqual(imported & others, set())


# ---------------------------------------------------------------------------
# 分歧口径：「没算」与「算出来是 0」必须可区分
# ---------------------------------------------------------------------------


class TestDivergenceSemantics(unittest.TestCase):
    """分歧计算的口径与可区分性。"""

    def test_spread_is_max_minus_min(self):
        """原始分歧 = 预测最大值 − 预测最小值。"""
        report = report_of(
            [observation("o1", {H1: 1.0, H2: 5.0, H3: 3.0}, cost=1.0)],
            hypotheses=(H1, H2, H3),
        )
        entry = report.entries[0]
        self.assertAlmostEqual(entry.divergence, 4.0, places=12)
        self.assertEqual(entry.participating, (H1, H2, H3))

    def test_unanimous_is_computed_zero_not_missing(self):
        """**关键**：各假说预测一致 → 分歧是算出来的 0，不是 ``None``。"""
        report = report_of(
            [
                observation("same", {H1: 2.0, H2: 2.0}, cost=1.0),
                observation("absent", {H1: 2.0}, cost=1.0),
            ]
        )
        unanimous = report.entry_of("same")
        absent = report.entry_of("absent")
        self.assertEqual(unanimous.divergence, 0.0)
        self.assertIsNotNone(unanimous.divergence)
        self.assertIs(unanimous.comparable, True)
        self.assertIs(unanimous.unanimous, True)
        self.assertIsNone(absent.divergence)
        self.assertIsNone(absent.divergence, UNDETERMINED_DIVERGENCE)
        self.assertIs(absent.comparable, False)
        self.assertIs(absent.unanimous, False)
        self.assertEqual(report.unanimous_ids, ("same",))
        self.assertEqual(report.undetermined_ids, ("absent",))
        self.assertEqual(report.comparable_ids, ("same",))

    def test_fewer_than_two_predictions_is_undetermined(self):
        """给出预测的假说少于 2 个 → 不可计算，记 None，不插补为 0。"""
        report = report_of(
            [
                observation("zero", {}, cost=1.0),
                observation("one", {H1: 3.0}, cost=1.0),
                observation("two", {H1: 3.0, H2: None}, cost=1.0),
            ]
        )
        for obs_id in ("zero", "one", "two"):
            with self.subTest(obs_id=obs_id):
                entry = report.entry_of(obs_id)
                self.assertIsNone(entry.divergence)
                self.assertIsNone(entry.normalized_divergence)
                self.assertIsNone(entry.heuristic_value)
                self.assertIs(entry.rankable, False)
        self.assertEqual(report.undetermined_ids, ("zero", "one", "two"))
        self.assertEqual(report.comparable_ids, ())

    def test_explicit_none_equals_absent_prediction(self):
        """显式 ``None`` 与完全未给出预测一律计为「未给出预测」。"""
        report = report_of(
            [
                observation("a", {H1: 1.0, H2: None, H3: None}, cost=1.0),
                observation("b", {H1: 1.0}, cost=1.0),
            ],
            hypotheses=(H1, H2, H3),
        )
        for obs_id in ("a", "b"):
            entry = report.entry_of(obs_id)
            self.assertIsNone(entry.divergence)
            self.assertEqual(entry.missing_predictions, (H2, H3))

    def test_normalized_mode_bounds(self):
        """归一化口径：分歧 / (最大绝对值 + ε)，落于 [0, 2]。"""
        report = report_of(
            [observation("o1", {H1: -2.0, H2: 2.0}, cost=1.0)],
            value_mode=VALUE_MODE_NORMALIZED,
        )
        entry = report.entries[0]
        expected = 4.0 / (2.0 + DIVERGENCE_EPSILON)
        self.assertAlmostEqual(entry.normalized_divergence, expected, places=12)
        self.assertLessEqual(entry.normalized_divergence, 2.0)
        self.assertGreaterEqual(entry.normalized_divergence, 0.0)
        self.assertAlmostEqual(entry.heuristic_value, expected, places=12)

    def test_normalized_mode_zero_scale_is_zero(self):
        """预测值全为 0 时归一化分歧为 0，且不发生除零。"""
        report = report_of(
            [observation("o1", {H1: 0.0, H2: 0.0}, cost=1.0)],
            value_mode=VALUE_MODE_NORMALIZED,
        )
        entry = report.entries[0]
        self.assertEqual(entry.normalized_divergence, 0.0)
        self.assertIs(math.isfinite(entry.heuristic_value), True)

    def test_spread_is_default_value_mode(self):
        """默认口径是原始离散度。"""
        report = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)])
        self.assertEqual(report.value_mode, VALUE_MODE_SPREAD)
        self.assertIn(VALUE_MODE_SPREAD, VALUE_MODES)
        self.assertIn(VALUE_MODE_NORMALIZED, VALUE_MODES)

    def test_invalid_value_mode_rejected(self):
        """非法口径被拒绝。"""
        with self.assertRaises(AcquisitionInputError):
            report_of(
                [observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)],
                value_mode="entropy_reduction",
            )

    def test_missing_policy_notes_output(self):
        """缺口口径原文随报告输出。"""
        payload = report_of([observation("o1", {H1: 1.0, H2: 4.0}, cost=1.0)]).to_dict()
        self.assertEqual(payload["missing_prediction_policy"], MISSING_PREDICTION_POLICY)
        self.assertEqual(payload["missing_cost_policy"], MISSING_COST_POLICY)

    def test_predictions_for_unknown_hypothesis_rejected(self):
        """指向未声明假说的预测被拒绝（两份输入不同源）。"""
        with self.assertRaises(AcquisitionInputError) as ctx:
            report_of([observation("o1", {H1: 1.0, "h-unknown@1": 2.0})])
        self.assertIn("h-unknown@1", str(ctx.exception))


# ---------------------------------------------------------------------------
# 排序与预算
# ---------------------------------------------------------------------------


class TestRankingAndBudget(unittest.TestCase):
    """排序键与预算裁剪。"""

    def test_ratio_ordering(self):
        """比值大者先入选。"""
        report = report_of(
            [
                # 分歧 1 / 成本 10 = 0.1
                observation("cheap_but_flat", {H1: 0.0, H2: 1.0}, cost=10.0),
                # 分歧 8 / 成本 1 = 8.0
                observation("sharp", {H1: 0.0, H2: 8.0}, cost=1.0),
                # 分歧 3 / 成本 1 = 3.0
                observation("middle", {H1: 0.0, H2: 3.0}, cost=1.0),
            ]
        )
        self.assertEqual(report.ranked_ids(), ("sharp", "middle", "cheap_but_flat"))
        self.assertEqual(acquisition_plan(report).ids(), ("sharp", "middle", "cheap_but_flat"))

    def test_lower_cost_wins_at_equal_divergence(self):
        """分歧相同时，成本低者优先。"""
        report = report_of(
            [
                observation("expensive", {H1: 0.0, H2: 4.0}, cost=8.0),
                observation("cheap", {H1: 0.0, H2: 4.0}, cost=2.0),
            ]
        )
        self.assertEqual(report.ranked_ids(), ("cheap", "expensive"))

    def test_boundary_is_tiebreak_only(self):
        """**关键**：边界接近度只在比值相同时作平局裁决，不参与加权。

        两条观测的分歧 / 成本完全相同，仅边界距离不同：近者优先。
        若边界被折算成分数，则比值不再相同，本用例的比值断言会失败。
        """
        report = report_of(
            [
                observation(
                    "far", {H1: 0.0, H2: 4.0}, cost=2.0, boundary_distance=100.0
                ),
                observation(
                    "near", {H1: 0.0, H2: 4.0}, cost=2.0, boundary_distance=0.0
                ),
            ]
        )
        near = report.entry_of("near")
        far = report.entry_of("far")
        self.assertAlmostEqual(near.heuristic_value, far.heuristic_value, places=12)
        self.assertIs(near.near_boundary, True)
        self.assertIs(far.near_boundary, False)
        self.assertEqual(report.ranked_ids(), ("near", "far"))

    def test_explicit_near_boundary_overrides_distance(self):
        """显式 ``near_boundary`` 优先于由距离推导。"""
        report = report_of(
            [
                observation("explicit", {H1: 0.0, H2: 4.0}, cost=2.0, near_boundary=False),
                observation("derived", {H1: 0.0, H2: 4.0}, cost=2.0, boundary_distance=0.0),
            ]
        )
        self.assertIs(report.entry_of("explicit").near_boundary, False)
        self.assertIs(report.entry_of("derived").near_boundary, True)
        self.assertEqual(report.ranked_ids(), ("derived", "explicit"))

    def test_boundary_threshold_constant(self):
        """接近边界的绝对阈值是公开常量。"""
        self.assertGreater(BOUNDARY_PROXIMITY_EPSILON, 0.0)
        self.assertIn("不参与加权", BOUNDARY_TIEBREAK_NOTE)

    def test_unknown_cost_excluded_not_treated_as_zero(self):
        """**关键**：成本未知不被当作 0 成本，单列上报。"""
        report = report_of(
            [
                observation("priced", {H1: 0.0, H2: 1.0}, cost=5.0),
                # 分歧极大但成本未知——若被当作 0 成本，它会排第一。
                observation("unpriced_huge", {H1: 0.0, H2: 1000.0}),
            ]
        )
        entry = report.entry_of("unpriced_huge")
        self.assertIsNone(entry.sampling_cost)
        self.assertIsNone(entry.heuristic_value)
        self.assertIs(entry.rankable, False)
        self.assertEqual(report.unknown_cost_ids, ("unpriced_huge",))
        plan = acquisition_plan(report)
        self.assertEqual(plan.ids(), ("priced",))
        self.assertEqual(plan.excluded_unknown_cost, ("unpriced_huge",))
        self.assertEqual(plan.spent_cost, 5.0)

    def test_undetermined_excluded_from_plan(self):
        """分歧不可计算的观测不入计划，单列上报。"""
        report = report_of(
            [
                observation("calc", {H1: 0.0, H2: 4.0}, cost=1.0),
                observation("noc", {H1: 0.0}, cost=1.0),
            ]
        )
        plan = acquisition_plan(report)
        self.assertEqual(plan.ids(), ("calc",))
        self.assertEqual(plan.excluded_undetermined, ("noc",))

    def test_max_items_limits_and_reports_overflow(self):
        """条目上限生效，超出者单列。"""
        observations = [
            observation(f"o{i}", {H1: 0.0, H2: float(i + 1)}, cost=1.0)
            for i in range(5)
        ]
        report = report_of(observations)
        plan = acquisition_plan(report, max_items=2)
        self.assertEqual(len(plan), 2)
        # 分歧最大者优先：o4(5) 与 o3(4)
        self.assertEqual(plan.ids(), ("o4", "o3"))
        self.assertEqual(plan.excluded_over_limit, ("o2", "o1", "o0"))
        self.assertEqual(plan.max_items, 2)

    def test_default_max_items(self):
        """默认条目上限是公开常量。"""
        report = report_of(
            [
                observation(f"o{i}", {H1: 0.0, H2: float(i + 1)}, cost=1.0)
                for i in range(DEFAULT_MAX_ITEMS + 5)
            ]
        )
        plan = acquisition_plan(report)
        self.assertEqual(len(plan), DEFAULT_MAX_ITEMS)
        self.assertEqual(plan.max_items, DEFAULT_MAX_ITEMS)
        self.assertEqual(len(plan.excluded_over_limit), 5)

    def test_cost_budget_limits_and_reports(self):
        """成本预算生效，超出者单列；已花费如实累计。"""
        report = report_of(
            [
                observation("big", {H1: 0.0, H2: 100.0}, cost=10.0),
                observation("small", {H1: 0.0, H2: 1.0}, cost=2.0),
            ]
        )
        plan = acquisition_plan(report, cost_budget=11.0)
        self.assertEqual(plan.ids(), ("big",))
        self.assertEqual(plan.spent_cost, 10.0)
        self.assertEqual(plan.remaining_budget(), 1.0)
        self.assertEqual(plan.excluded_over_budget, ("small",))

        plan2 = acquisition_plan(report, cost_budget=12.0)
        self.assertEqual(plan2.ids(), ("big", "small"))
        self.assertEqual(plan2.spent_cost, 12.0)
        self.assertEqual(plan2.remaining_budget(), 0.0)
        # 「超预算」之所以成立，是因为它本来可排序——先确认这点，避免空断言。
        self.assertIs(report.entry_of("small").rankable, True)
        self.assertIs(report.entry_of("big").rankable, True)

    def test_invalid_budget_and_limits_rejected(self):
        """预算与上限的非法取值被拒绝。"""
        report = report_of([observation("o1", {H1: 0.0, H2: 4.0}, cost=1.0)])
        for bad in (0, -1.0, float("nan"), float("inf"), "5"):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionInputError):
                    acquisition_plan(report, cost_budget=bad)
        for bad in (0, -1, 2.5, True, "3"):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionInputError):
                    acquisition_plan(report, max_items=bad)

    def test_partition_is_exhaustive(self):
        """入选与四类排除清单构成一个划分（无遗漏、无重复）。"""
        report = report_of(
            [
                observation("ok1", {H1: 0.0, H2: 9.0}, cost=1.0),
                observation("ok2", {H1: 0.0, H2: 1.0}, cost=1.0),
                observation("undet", {H1: 0.0}, cost=1.0),
                observation("nocost", {H1: 0.0, H2: 9.0}),
                observation("overlimit", {H1: 0.0, H2: 5.0}, cost=1.0),
            ]
        )
        plan = acquisition_plan(report, max_items=1)
        all_ids = {e.observation_id for e in report.entries}
        combined = set(plan.ids())
        combined |= set(plan.excluded_undetermined)
        combined |= set(plan.excluded_unknown_cost)
        combined |= set(plan.excluded_over_budget)
        combined |= set(plan.excluded_over_limit)
        self.assertEqual(combined, all_ids)
        self.assertEqual(len(combined), len(all_ids))

    def test_plan_rejects_non_report_source(self):
        """计划必须建立在同一份分歧报告之上，不能障过报告直接排序。"""
        for bad in ({}, [], None, "report"):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionInputError):
                    acquisition_plan(bad)

    def test_plan_rejects_unknown_parameter(self):
        """未知参数不被静默忽略——静默失效会让调用方误以为某个开关生效了。"""
        report = report_of([observation("o1", {H1: 0.0, H2: 4.0}, cost=1.0)])
        with self.assertRaises(AcquisitionInputError) as ctx:
            acquisition_plan(report, fabricated_option=True)
        self.assertIn("fabricated_option", str(ctx.exception))

    def test_unknown_parameter_on_divergence_also_rejected(self):
        """``divergence`` 的未知参数同样不被静默忽略。"""
        with self.assertRaises(AcquisitionInputError) as ctx:
            report_of(
                [observation("o1", {H1: 0.0, H2: 1.0}, cost=1.0)],
                totally_unknown=True,
            )
        self.assertIn("totally_unknown", str(ctx.exception))


# ---------------------------------------------------------------------------
# 输入形态、确定性与封存闸门
# ---------------------------------------------------------------------------


class TestInputShapesAndGuards(unittest.TestCase):
    """输入形态、可复现性与受限内容闸门。"""

    def test_minimum_two_hypotheses_required(self):
        """竞争假说至少 2 个，否则「分歧」无意义。"""
        for bad in ([], [H1], (_ for _ in ()), "h-alpha@1"):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionInputError):
                    divergence(bad, [observation("o1", {H1: 1.0, H2: 2.0})])

    def test_duplicate_hypotheses_rejected(self):
        """重复假说标识被拒绝（会重复计数）。"""
        with self.assertRaises(AcquisitionInputError) as ctx:
            divergence([H1, H2, H1], [observation("o1", {H1: 1.0, H2: 2.0})])
        self.assertIn(H1, str(ctx.exception))

    def test_duplicate_observation_ids_rejected(self):
        """重复观测标识被拒绝。"""
        with self.assertRaises(AcquisitionInputError):
            report_of(
                [
                    observation("dup", {H1: 0.0, H2: 1.0}, cost=1.0),
                    observation("dup", {H1: 0.0, H2: 2.0}, cost=1.0),
                ]
            )

    def test_object_and_dict_forms_agree(self):
        """对象形态与字典形态产出同一结论。"""
        dict_report = report_of(
            [observation("o1", {H1: 1.0, H2: 5.0}, cost=2.0)]
        )
        obj_report = report_of(
            [_ObservationObject("o1", {H1: 1.0, H2: 5.0}, sampling_cost=2.0)]
        )
        self.assertEqual(dict_report.canonical_json(), obj_report.canonical_json())

    def test_hypothesis_objects_accepted(self):
        """假说可以是对象（取 ``identity``）。"""
        report = divergence(
            [_HypothesisObject(H1), _HypothesisObject(H2)],
            [observation("o1", {H1: 1.0, H2: 5.0}, cost=1.0)],
        )
        self.assertEqual(report.hypotheses, (H1, H2))
        self.assertAlmostEqual(report.entries[0].divergence, 4.0, places=12)

    def test_generator_input_accepted(self):
        """观测集合可以是生成器。"""
        report = report_of(
            (o for o in [observation("o1", {H1: 1.0, H2: 5.0}, cost=1.0)])
        )
        self.assertEqual(report.comparable_ids, ("o1",))

    def test_deterministic_and_reproducible(self):
        """同一输入重复调用产出完全相同的规范式 JSON 与摘要。"""
        observations = [
            observation("a", {H1: 0.0, H2: 4.0}, cost=2.0),
            observation("b", {H1: 1.0, H2: 1.0}, cost=1.0),
            observation("c", {H1: 0.0}, cost=1.0),
        ]
        first = report_of(observations)
        second = report_of(observations)
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.content_digest(), second.content_digest())
        plan1 = acquisition_plan(first)
        plan2 = acquisition_plan(second)
        self.assertEqual(plan1.canonical_json(), plan2.canonical_json())
        self.assertEqual(plan1.content_digest(), plan2.content_digest())

    def test_digest_changes_with_content(self):
        """摘要随内容变化（反向用例：证明摘要不是在算常量）。"""
        base = report_of([observation("o1", {H1: 0.0, H2: 4.0}, cost=1.0)])
        shifted = report_of([observation("o1", {H1: 0.0, H2: 5.0}, cost=1.0)])
        self.assertNotEqual(base.content_digest(), shifted.content_digest())

    def test_sealed_record_fields_rejected(self):
        """封存记录字段一律拒绝——取证计划描述的是规格，不是记录。"""
        for key in sorted(SEALED_KEYS):
            with self.subTest(key=key):
                payload = observation("o1", {H1: 1.0, H2: 2.0}, cost=1.0)
                payload[key] = ["leaked"]
                with self.assertRaises(AcquisitionPolicyError) as ctx:
                    report_of([payload])
                self.assertIn("封存", str(ctx.exception))

    def test_nested_sealed_fields_rejected(self):
        """嵌套在预测子结构里的封存字段同样被拦。"""
        payload = observation("o1", {H1: 1.0, H2: 2.0}, cost=1.0)
        payload["meta"] = {"nested": {"snapshot": {"rows": []}}}
        with self.assertRaises(AcquisitionPolicyError):
            report_of([payload])

    def test_token_keys_rejected(self):
        """令牌类键名被拒绝（INTERFACES.md §4.4）。"""
        for key in sorted(FORBIDDEN_TOKEN_KEYS):
            with self.subTest(key=key):
                payload = observation("o1", {H1: 1.0, H2: 2.0}, cost=1.0)
                payload[key] = "s3cret"
                with self.assertRaises(AcquisitionPolicyError):
                    report_of([payload])

    def test_non_finite_predictions_rejected(self):
        """预测值必须是有限实数。"""
        for bad in (float("nan"), float("inf"), -float("inf"), "1.0", True):
            with self.subTest(bad=bad):
                with self.assertRaises(AcquisitionInputError):
                    report_of([observation("o1", {H1: 0.0, H2: bad}, cost=1.0)])

    def test_unknown_parameter_on_divergence_rejected(self):
        """``divergence`` 的未知参数不被静默忽略（与计划入口口径一致）。"""
        with self.assertRaises(AcquisitionInputError) as ctx:
            report_of(
                [observation("o1", {H1: 0.0, H2: 1.0}, cost=1.0)],
                some_unknown_flag=True,
            )
        self.assertIn("some_unknown_flag", str(ctx.exception))

    def test_report_navigation_helpers(self):
        """报告提供按标识取条目与排序视图。"""
        report = report_of(
            [
                observation("low", {H1: 0.0, H2: 1.0}, cost=1.0),
                observation("high", {H1: 0.0, H2: 9.0}, cost=1.0),
            ]
        )
        self.assertEqual(report.entry_of("high").observation_id, "high")
        with self.assertRaises(AcquisitionInputError):
            report.entry_of("missing")
        self.assertEqual(report.ranked_ids(), ("high", "low"))
        self.assertEqual(len(report), 2)
        self.assertEqual([e.observation_id for e in report], ["low", "high"])

    def test_notes_present_in_plan(self):
        """计划携带全部口径说明，不接受静默省略。"""
        report = report_of([observation("o1", {H1: 0.0, H2: 4.0}, cost=1.0)])
        plan = acquisition_plan(report)
        self.assertIn(RANKING_NOT_EIG_NOTE, plan.notes)
        self.assertIn(PRE_SAMPLING_FREEZE_NOTE, plan.notes)
        self.assertIn(PLAN_IS_ADVISORY_NOTE, plan.notes)
        self.assertIn(BOUNDARY_TIEBREAK_NOTE, plan.notes)
        self.assertIn(MISSING_COST_POLICY, plan.notes)
        self.assertIn(ACQUISITION_ITEM_NOTE, plan.notes)

    def test_exception_hierarchy(self):
        """异常层级：政策违规与输入错误都可按基类捕获。"""
        self.assertTrue(issubclass(AcquisitionPolicyError, AcquisitionError))
        self.assertTrue(issubclass(AcquisitionInputError, AcquisitionError))
        self.assertTrue(issubclass(AcquisitionError, ValueError))
        self.assertFalse(issubclass(AcquisitionPolicyError, AcquisitionInputError))

    def test_version_and_module_doc(self):
        """版本常量存在，模块 docstring 说明启发式口径。"""
        self.assertTrue(ACQUISITION_VERSION.startswith("P14"))
        import sdl_m07.acquisition as module

        doc = module.__doc__ or ""
        self.assertIn("启发式", doc)
        self.assertIn("采样前固定", doc)
        self.assertIn("不自动触发任何数据获取", doc)


# ---------------------------------------------------------------------------
# 项目级边界：冻结目录、依赖、跳过标记
# ---------------------------------------------------------------------------


class TestProjectBoundaries(unittest.TestCase):
    """P14 的横向约束：冻结目录、标准库依赖、无 skip/xfail。"""

    def test_module_docstring_exists_and_is_chinese(self):
        """模块 docstring 存在且为中文（含中文字符）。"""
        import sdl_m07.acquisition as module

        doc = module.__doc__ or ""
        self.assertGreater(len(doc), 500)
        self.assertIs(
            any("\u4e00" <= ch <= "\u9fff" for ch in doc),
            True,
        )

    def test_module_has_no_ellipsis_placeholder(self):
        """交付物不得含省略号占位（AST 判定裸 ``...``，不误伤 ``tuple[str, ...]``）。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        placeholders = []
        for node in pyast.walk(tree):
            # 裸 Ellipsis 作为独立语句，或作为函数体的唯一内容 → 占位符。
            if isinstance(node, pyast.Expr) and isinstance(node.value, pyast.Constant):
                if node.value.value is Ellipsis:
                    placeholders.append(f"line{node.lineno}")
            elif isinstance(node, pyast.Constant) and node.value is Ellipsis:
                # 出现在 AnnAssign / Subscript 里的 Ellipsis 是类型标注的一部分，
                # 属合法用法；这里只拒绝「单独出现」的形态。
                continue
        self.assertEqual(placeholders, [])
        # 中文省略号在正文里也不应出现。
        self.assertNotIn("…", MODULE_PATH.read_text(encoding="utf-8"))

    def test_test_file_has_no_skip_or_xfail(self):
        """本测试文件自身不使用 skip / xfail（AST 级判定）。"""
        tree = pyast.parse(TEST_PATH.read_text(encoding="utf-8"))
        markers = {"skip", "skipIf", "skipUnless", "expectedFailure", "xfail"}
        hits = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    name = _tail_name(decorator)
                    if name in markers:
                        hits.append(f"{node.name}:@{name}")
            elif isinstance(node, pyast.Call):
                name = _tail_name(node.func)
                if name in markers:
                    hits.append(f"line{getattr(node, 'lineno', '?')}:{name}")
        self.assertEqual(hits, [])

    def test_frozen_directory_not_touched_by_import(self):
        """导入本模块不会引入或改写 ``sdl_m01/`` 的任何文件。

        以「模块源码既未导入 sdl_m01、也未提及写文件接口」做静态判定，
        比 mtime 比对更适合单元测试（mtime 属仓库级门禁的职责）。
        """
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(any("sdl_m01" in name for name in imported))
        # 也不写文件：模块只读自身源码用于自检。
        text = MODULE_PATH.read_text(encoding="utf-8")
        for writer in ("open(", "write_text(", "write_bytes(", "os.remove", "shutil"):
            with self.subTest(writer=writer):
                # 允许读自身源码（read_text），但不允许写入形态。
                self.assertNotIn(f".{writer}", text)

    def test_only_standard_library(self):
        """只用标准库（白名单判定，避免遗漏）。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {
            "__future__",
            "ast",
            "hashlib",
            "inspect",
            "json",
            "math",
            "pathlib",
            "dataclasses",
            "types",
            "typing",
        }
        self.assertEqual(imported - allowed, set())

    def test_self_check_shape(self):
        """自检返回结构稳定，且列出了被检查的名字数量。"""
        result = self_check()
        self.assertEqual(
            set(result),
            {
                "defined_forbidden_ranking",
                "defined_other_stages",
                "accessed_forbidden",
                "ok",
                "checked_names",
            },
        )
        self.assertGreater(result["checked_names"], 20)
        self.assertIs(result["ok"], True)

    def test_inspect_uses_real_source_path(self):
        """自检读的是真实源码文件（防止自检对象错位成空文件）。"""
        source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
        self.assertEqual(source_path.resolve(), MODULE_PATH.resolve())
        self.assertGreater(source_path.stat().st_size, 10000)

    def test_forbidden_names_declared_but_not_defined(self):
        """越界接口名必须写在边界声明里，同时确实没有被实现。

        没有第一个断言，本用例会因为「名单为空」而空洞通过。
        """
        self.assertGreaterEqual(len(NOT_PROVIDED_BY_P14), 20)
        for name in (
            "archive_round",
            "update_knowledge_version",
            "revise_or_retire",
            "run_loop",
            "build_frozen_plan",
            "execute_family",
        ):
            self.assertIn(name, NOT_PROVIDED_BY_P14)
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in pyast.walk(tree)
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef))
        }
        self.assertEqual(defined & set(NOT_PROVIDED_BY_P14), set())


def _tail_name(node) -> str:
    """取调用 / 装饰器表达式的末尾名字（``a.b.skip`` → ``skip``）。"""
    if isinstance(node, pyast.Attribute):
        return node.attr
    if isinstance(node, pyast.Name):
        return node.id
    if isinstance(node, pyast.Call):
        return _tail_name(node.func)
    return ""


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
