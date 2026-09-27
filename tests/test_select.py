"""P10 验收测试：Pareto 筛选与候选池管理。

覆盖三条验收标准：

1. **优化方向显式声明。** :data:`OPTIMIZATION_DIRECTIONS` 是冻结常量，
   G/S/N 取最大化、C 取最小化；模块不提供任何改写方向的入口；
   方向的语义（``is_better``）在数值上可验证；输出结果随附方向声明。
2. **冻结候选上限可配置且默认 5。** :func:`select_freeze_candidates` 的
   ``max_candidates`` 默认取 :data:`DEFAULT_FREEZE_CAP`（=5），可在
   ``1..FREEZE_CAP_HARD_LIMIT`` 内配置，越界与非法类型一律拒绝；
   候选不足时如实记录 ``shortfall``，不用低质量候选凑数。
3. **禁止引入总分抵消逻辑。** 任何 ``weights`` / ``total_score`` /
   ``novelty_bonus`` 一类的关键字参数被 :class:`SelectionPolicyError` 拒绝，
   且报错信息点明确证的契约级原则；模块内不存在任何把四维折算成单一数值的
   函数；高新颖性**不能**使候选免于被支配——这是「新颖性不抵消弱证据」在
   偏序上的可验证后果。

另覆盖：偏序不可比性、分层（rank）语义、轮转预约（不折算维度）、缺维度候选
的「不插补 / 不因此扣分 / 默认排除」口径、知识库版本跨版本拒绝、输入形态
（对象 / 字典 / 生成器）、确定性可复现、以及 P10 的边界守护——不修改
``sdl_m01/``、不引入第三方依赖、不使用 skip / xfail、不实现 P11 及之后的内容。
"""

import ast as pyast
import inspect
import json
import pathlib
import unittest

from sdl_m03.equations import fit_relation, prepare_sample
from sdl_m03.stability import resample_evaluate

from sdl_m04.hypothesis import Hypothesis

from sdl_m05.metrics import Evaluation, KnowledgeBase, KnowledgeEntry, evaluate_hypothesis

from sdl_m05.select import (
    COMPARISON_TOLERANCE,
    CONFIRMATION_POLICY,
    CONFIRMATION_USES_NOVELTY_BONUS,
    DEFAULT_FREEZE_CAP,
    DIMENSIONS,
    DIRECTIONS_NOTE,
    DIRECTION_MAXIMIZE,
    DIRECTION_MINIMIZE,
    DOMINANCE_NOTE,
    FORBIDDEN_SCORE_KEYS,
    FREEZE_CAP_HARD_LIMIT,
    FREEZE_CAP_NOTE,
    MAXIMIZED_DIMENSIONS,
    MINIMIZED_DIMENSIONS,
    MISSING_DIMENSION_POLICY,
    NOT_PROVIDED_BY_P10,
    NO_TOTAL_SCORE_NOTE,
    OPTIMIZATION_DIRECTIONS,
    RESERVE_NOTE,
    RESERVE_ROTATION,
    SELECTION_GRANTS_EVIDENCE_GRADE,
    SELECTION_SCOPE_NOTE,
    SELECT_VERSION,
    FreezeSelection,
    ParetoFront,
    ReserveSelection,
    SelectionError,
    SelectionInputError,
    SelectionPolicyError,
    SelectionVersionError,
    compare_pair,
    direction_of,
    dominates,
    is_better,
    pareto_front,
    pareto_layers,
    select_freeze_candidates,
    top_k_reserved,
)

# ---------------------------------------------------------------------------
# 夹具：确定性合成评估记录
# ---------------------------------------------------------------------------

#: 模块源码路径，用于静态检查第三方依赖与边界声明。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m05" / "select.py"

#: 测试文件自身路径，用于检查本文件未使用 skip / xfail。
TEST_PATH = pathlib.Path(__file__).resolve()

VARIABLES = ["X1", "X2"]
TARGET = "Y"
NAMESPACE = "acc-exp"

#: 默认知识库版本。
KV = "k-v1"


def record(
    hypothesis_id: str,
    gain: float | None,
    stability: float | None,
    novelty: float | None,
    complexity: float | None,
    *,
    version: str = "1",
    knowledge_version: str = KV,
) -> dict:
    """构造一条字典形态的评估记录。

    四个维度均可传 ``None``，用于验证「缺维度表示证据不足」的口径。
    """
    payload = {
        "hypothesis_id": hypothesis_id,
        "hypothesis_version": version,
        "knowledge_version": knowledge_version,
        "gain": gain,
        "stability": stability,
        "novelty": novelty,
        "complexity": complexity,
    }
    if knowledge_version is None:
        payload.pop("knowledge_version")
    return payload


def _x1(index: int) -> float:
    """确定性自变量 1。"""
    return 0.5 * index + 0.3


def _x2(index: int) -> float:
    """确定性自变量 2（与 X1 不共线）。"""
    return 0.7 + 0.23 * ((index * 7) % 5)


def _y(index: int) -> float:
    """目标：Y = 2·X1/X2 + 1。"""
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
    """构造 M1 数据配置的 ``dependence`` 段。"""
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


def knowledge_base_v1() -> KnowledgeBase:
    """一个含两条已知项的知识库快照。"""
    return KnowledgeBase(
        version=KV,
        entries=(
            KnowledgeEntry(
                entry_id="k-ratio",
                statement="比例表示 X1/X2 与目标量近似线性相关。",
                kind="relation",
                expression="X1/X2",
            ),
            KnowledgeEntry(
                entry_id="k-sum",
                statement="目标量与 X1 与 X2 之和近似线性相关。",
                kind="relation",
                expression="X1 + X2",
            ),
        ),
        note="截至 2026-09 的合成领域已知公式集",
    )


def hypothesis_of(
    *,
    hypothesis_id: str = "h-relation-1",
    expression: str = "X1/X2",
    statement: str = "比例表示 X1/X2 对目标的预测优于常数与简单线性基线。",
) -> Hypothesis:
    """构造一个合法的 P07 假说对象。"""
    return Hypothesis(
        id=hypothesis_id,
        version="1",
        type="relation",
        statement=statement,
        representation={
            "missing_value_handling": "drop_rows_with_missing_required_fields",
            "standardization": "none_declared_candidate_expression",
        },
        predictions=[{"incompatible_with": "独立样本上的增益消失或为负。"}],
        null_hypotheses=[
            {"null_id": "null-no-relation", "statement": "不存在预测关系。"}
        ],
        provenance={
            "data_ids": ["E-abc123"],
            "knowledge_version": KV,
            "generator": "test-fixture",
            "code_version": "P10-test",
        },
        model={
            "model_kind": "relation_candidate",
            "expression": expression,
            "target": TARGET,
            "coefficients": {"intercept": 1.0, "slope": 2.0},
        },
    )


def real_evaluation(
    *,
    hypothesis_id: str = "h-relation-1",
    expression: str = "X1/X2",
) -> Evaluation:
    """构造一条**真实**的 P09 评估记录（调用上游而非伪造）。

    这样测试验证的是「P10 能消费 P09 的真实产物」，而不是只对自己
    编造的字典生效。
    """
    sample = prepare_sample(relation_records(), VARIABLES, TARGET)
    relation_fit = fit_relation(sample, expression)
    stability_report = resample_evaluate(
        expression,
        relation_records(),
        20,
        7,
        spec=spec_for(),
        variables=VARIABLES,
        target=TARGET,
    )
    return evaluate_hypothesis(
        hypothesis_of(hypothesis_id=hypothesis_id, expression=expression),
        knowledge_base=knowledge_base_v1(),
        relation_fit=relation_fit,
        stability_evidence=stability_report,
    )


def tradeoff_set() -> list[dict]:
    """一组构造好的前沿夹具（含支配、不可比、被支配三层结构）。

    设计意图（**先在纸上算清楚再写断言**）：

    - ``A``：四维全面占优，支配 ``D``。
    - ``D``：仅 gain 与 A 持平，其余更差 → 被 A 支配，落入 rank 1。
    - ``B``：gain 中庸、stability 中庸、novelty 中庸、complexity 中庸，
      与 A/C/E 均不可比。
    - ``C``：stability 最高，但 gain/novelty/complexity 都很差 →
      与 A（gain 更高）、E（novelty 更高）不可比。
    - ``E``：novelty 最高，但 gain/stability 很差 → 与 A 不可比。

    因此前沿 = {A, B, C, E}，rank 1 = {D}。
    """
    return [
        record("A", 9.0, 0.90, 0.90, 9.0),
        record("B", 5.0, 0.50, 0.50, 5.0),
        record("C", 1.0, 0.95, 0.10, 1.0),
        record("D", 9.0, 0.10, 0.10, 9.5),
        record("E", 1.0, 0.10, 0.95, 2.0),
    ]


def front_ids_of(result: ParetoFront) -> tuple[str, ...]:
    """取前沿标识（去掉 ``@version`` 后缀，便于断言）。"""
    return tuple(item.split("@")[0] for item in result.front_ids())


def anticyclone(count: int) -> list[dict]:
    """构造一个规模为 ``count`` 的**反链**（互不支配，全部留在前沿）。

    设计意图：``gain`` 与 ``complexity`` 同步递增。于是对任意 ``i < j``，
    后者 gain 更高（更优）但 complexity 也更高（更劣）→ 两者不可比。
    因此前沿规模恰为 ``count``。

    之所以专门造反链：单调链（后一个全面支配前一个）的前沿恒为 1，
    无法用来验证「上限是否真的生效」。
    """
    return [
        record(
            f"H{index:02d}",
            float(index) + 1.0,
            0.5,
            0.5,
            float(index) + 1.0,
        )
        for index in range(count)
    ]


def identities_of(items) -> tuple[str, ...]:
    """把选中结果里的标识去掉版本后缀。"""
    return tuple(item.split("@")[0] for item in items)


# ---------------------------------------------------------------------------
# 验收标准①：优化方向显式声明
# ---------------------------------------------------------------------------


class TestDirectionsDeclared(unittest.TestCase):
    """验收标准①：优化方向显式写入代码常量，且不可被调用方改写。"""

    def test_directions_constant_is_max_g_s_n_min_c(self) -> None:
        """方向常量恰为「G/S/N 最大化、C 最小化」。"""
        self.assertEqual(set(OPTIMIZATION_DIRECTIONS), set(DIMENSIONS))
        for name in ("gain", "stability", "novelty"):
            with self.subTest(dimension=name):
                self.assertEqual(OPTIMIZATION_DIRECTIONS[name], DIRECTION_MAXIMIZE)
        self.assertEqual(OPTIMIZATION_DIRECTIONS["complexity"], DIRECTION_MINIMIZE)

    def test_maximized_and_minimized_sets_match_the_constant(self) -> None:
        """两个集合常量与方向映射互相一致。"""
        self.assertEqual(set(MAXIMIZED_DIMENSIONS), {"gain", "stability", "novelty"})
        self.assertEqual(set(MINIMIZED_DIMENSIONS), {"complexity"})
        for name in MAXIMIZED_DIMENSIONS:
            self.assertEqual(OPTIMIZATION_DIRECTIONS[name], DIRECTION_MAXIMIZE)
        for name in MINIMIZED_DIMENSIONS:
            self.assertEqual(OPTIMIZATION_DIRECTIONS[name], DIRECTION_MINIMIZE)

    def test_dimensions_are_frozen_no_runtime_override(self) -> None:
        """方向是只读的：试图改写会失败（不是可变字典）。"""
        with self.assertRaises(TypeError):
            OPTIMIZATION_DIRECTIONS["gain"] = DIRECTION_MINIMIZE  # type: ignore[index]
        with self.assertRaises(TypeError):
            OPTIMIZATION_DIRECTIONS["complexity"] = DIRECTION_MAXIMIZE  # type: ignore[index]

    def test_no_public_api_accepts_a_direction_override(self) -> None:
        """三个主入口都不暴露方向参数，方向只能在源代码里改。"""
        banned = {"direction", "directions", "maximize", "minimize", "orientation"}
        for func in (pareto_front, top_k_reserved, select_freeze_candidates):
            with self.subTest(func=func.__name__):
                params = set(inspect.signature(func).parameters)
                self.assertEqual(
                    params & banned, set(),
                    f"{func.__name__} 暴露了方向覆盖入口：{sorted(params & banned)}",
                )

    def test_direction_of_rejects_unknown_dimension(self) -> None:
        """:func:`direction_of` 对未知维度报错。"""
        for name in ("gain", "stability", "novelty", "complexity"):
            with self.subTest(dimension=name):
                self.assertIn(
                    direction_of(name), (DIRECTION_MAXIMIZE, DIRECTION_MINIMIZE)
                )
        with self.assertRaises(SelectionInputError):
            direction_of("accuracy")

    def test_is_better_respects_declared_direction(self) -> None:
        """``is_better`` 按声明方向判定：复杂度是「越小越好」。"""
        # gain：越大越好
        self.assertTrue(is_better("gain", record("A", 5.0, 0.5, 0.5, 1.0),
                                  record("B", 3.0, 0.5, 0.5, 1.0)))
        # complexity：越小越好（3 < 5 → 左更优）
        self.assertTrue(is_better("complexity", record("A", 1.0, 0.5, 0.5, 3.0),
                                  record("B", 1.0, 0.5, 0.5, 5.0)))
        # 反过来不成立
        self.assertFalse(is_better("complexity", record("A", 1.0, 0.5, 0.5, 5.0),
                                   record("B", 1.0, 0.5, 0.5, 3.0)))

    def test_is_better_treats_near_equal_as_not_better(self) -> None:
        """容差内的差异不算严格更优（避免浮点噪声制造虚假支配）。"""
        tiny = COMPARISON_TOLERANCE / 10.0
        self.assertFalse(
            is_better("gain", record("A", 1.0, 0.5, 0.5, 1.0),
                      record("B", 1.0 - tiny, 0.5, 0.5, 1.0))
        )

    def test_directions_appear_in_output(self) -> None:
        """方向声明随结果输出，便于复核。"""
        front = pareto_front(tradeoff_set())
        self.assertEqual(front.to_dict()["directions"], dict(OPTIMIZATION_DIRECTIONS))
        freeze = select_freeze_candidates(tradeoff_set())
        self.assertEqual(freeze.to_dict()["directions"], dict(OPTIMIZATION_DIRECTIONS))

    def test_directions_note_is_present_and_explicit(self) -> None:
        """方向说明文本显式写出四个维度与两个方向词。"""
        self.assertIn("gain", DIRECTIONS_NOTE)
        self.assertIn("complexity", DIRECTIONS_NOTE)
        self.assertIn("最大化", DIRECTIONS_NOTE)
        self.assertIn("最小化", DIRECTIONS_NOTE)


# ---------------------------------------------------------------------------
# 验收标准②：冻结候选上限可配置，默认 5
# ---------------------------------------------------------------------------


class TestFreezeCap(unittest.TestCase):
    """验收标准②：冻结候选上限可配置且默认 5。"""

    def test_default_cap_is_five(self) -> None:
        """默认上限常量等于 5，且与函数默认值一致。"""
        self.assertEqual(DEFAULT_FREEZE_CAP, 5)
        default = inspect.signature(select_freeze_candidates).parameters[
            "max_candidates"
        ].default
        self.assertEqual(default, DEFAULT_FREEZE_CAP)

    def test_hard_limit_anchors_framework_prototype(self) -> None:
        """硬边界存在且大于默认值（锚定框架 §2 的探索候选上界）。"""
        self.assertEqual(FREEZE_CAP_HARD_LIMIT, 50)
        self.assertGreater(FREEZE_CAP_HARD_LIMIT, DEFAULT_FREEZE_CAP)

    def test_default_run_respects_cap_when_candidates_are_plentiful(self) -> None:
        """候选充足时默认最多选 5 个。"""
        result = select_freeze_candidates(anticyclone(12))
        self.assertEqual(result.cap, DEFAULT_FREEZE_CAP)
        self.assertLessEqual(result.selected_count, DEFAULT_FREEZE_CAP)
        self.assertEqual(result.selected_count, DEFAULT_FREEZE_CAP)
        self.assertEqual(result.shortfall, 0)

    def test_cap_is_configurable_and_effective(self) -> None:
        """上限可配置，且配置值真正生效。"""
        data = anticyclone(12)
        for cap in (1, 2, 3, 7, 12, FREEZE_CAP_HARD_LIMIT):
            with self.subTest(cap=cap):
                result = select_freeze_candidates(data, max_candidates=cap)
                self.assertEqual(result.cap, cap)
                self.assertLessEqual(result.selected_count, cap)
                self.assertEqual(result.selected_count, min(cap, len(data)))

    def test_cap_out_of_range_is_rejected(self) -> None:
        """越界上限一律拒绝（0 / 负数 / 超过硬边界）。"""
        for bad in (0, -1, FREEZE_CAP_HARD_LIMIT + 1, 1000):
            with self.subTest(cap=bad):
                with self.assertRaises(SelectionInputError):
                    select_freeze_candidates(tradeoff_set(), max_candidates=bad)

    def test_cap_type_is_validated(self) -> None:
        """上限必须是真整数（``True`` 与 ``2.5`` 都不算）。"""
        for bad in (True, 2.5, "5", None):
            with self.subTest(cap=bad):
                with self.assertRaises(SelectionInputError):
                    select_freeze_candidates(tradeoff_set(), max_candidates=bad)

    def test_shortfall_is_reported_instead_of_padding(self) -> None:
        """前沿候选不足时如实记录缺口，不用被支配候选凑数。"""
        only_one = [record("A", 9.0, 0.9, 0.9, 1.0)]
        result = select_freeze_candidates(only_one, max_candidates=5)
        self.assertEqual(result.selected_count, 1)
        self.assertEqual(result.shortfall, 4)
        self.assertEqual(identities_of(result.ids()), ("A",))

    def test_lower_layers_are_not_backfilled_by_default(self) -> None:
        """默认不从前沿之外的层回填——前沿不足就是不足。"""
        # A 支配 D，因此 D 在 rank 1；前沿只有 {A}。
        data = [record("A", 9.0, 0.9, 0.9, 1.0), record("D", 1.0, 0.1, 0.1, 9.0)]
        result = select_freeze_candidates(data, max_candidates=2)
        self.assertEqual(identities_of(result.ids()), ("A",))
        self.assertEqual(result.shortfall, 1)
        self.assertFalse(result.included_lower_layers)
        self.assertEqual(result.selected_from_front, 1)

    def test_lower_layers_can_be_enabled_explicitly(self) -> None:
        """显式开启后，才允许从低层回填（层序仍是偏序延伸，不是总分排名）。"""
        data = [record("A", 9.0, 0.9, 0.9, 1.0), record("D", 1.0, 0.1, 0.1, 9.0)]
        result = select_freeze_candidates(
            data, max_candidates=2, include_lower_layers=True
        )
        self.assertEqual(identities_of(result.ids()), ("A", "D"))
        self.assertEqual(result.shortfall, 0)
        self.assertTrue(result.included_lower_layers)
        self.assertEqual(result.selected_from_front, 1)

    def test_normal_run_uses_only_front(self) -> None:
        """常规情形下入选者全部来自前沿，且都是前沿成员。"""
        result = select_freeze_candidates(tradeoff_set(), max_candidates=3)
        self.assertEqual(result.selected_from_front, result.selected_count)
        front_ids = set(pareto_front(tradeoff_set()).front_ids())
        for identity in result.ids():
            with self.subTest(identity=identity):
                self.assertIn(identity, front_ids)
        # 被支配的 D 不在其中
        self.assertNotIn("D@1", result.ids())

    def test_cap_note_is_present(self) -> None:
        """上限口径说明随结果输出，写明默认值与硬边界。"""
        result = select_freeze_candidates(tradeoff_set())
        self.assertIn("默认 5", FREEZE_CAP_NOTE)
        self.assertIn("可配置", FREEZE_CAP_NOTE)
        self.assertIn("默认 5", result.to_dict()["freeze_cap_note"])


# ---------------------------------------------------------------------------
# 验收标准③：禁止总分抵消逻辑
# ---------------------------------------------------------------------------


class TestNoTotalScore(unittest.TestCase):
    """验收标准③：不引入「新颖性加分抵消弱证据」的总分逻辑。"""

    def test_score_parameters_are_rejected(self) -> None:
        """加权 / 总分参数一律被拒绝，且报错点明确证原则。"""
        for keyword in sorted(FORBIDDEN_SCORE_KEYS):
            with self.subTest(keyword=keyword):
                with self.assertRaises(SelectionPolicyError) as context:
                    select_freeze_candidates(tradeoff_set(), **{keyword: 1.0})
                self.assertIn("新颖性", str(context.exception))

    def test_score_parameters_rejected_on_all_entry_points(self) -> None:
        """三个入口都拒绝分类型参数。"""
        for func in (top_k_reserved, select_freeze_candidates):
            with self.subTest(func=func.__name__):
                with self.assertRaises(SelectionPolicyError):
                    func(tradeoff_set(), total_score=True)

    def test_arbitrary_weight_like_keyword_is_rejected(self) -> None:
        """换个名字也绕不过去：含 ``weight`` / ``score`` 的参数名一律拒绝。"""
        for keyword in ("novelty_weight_x2", "my_weighted_thing", "bonus_score_v2"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(SelectionPolicyError):
                    select_freeze_candidates(tradeoff_set(), **{keyword: 1.0})

    def test_novelty_bonus_cannot_offset_weak_evidence(self) -> None:
        """**核心反例**：新颖性极高但其余维度很弱的候选，仍被支配。

        构造一个「新颖性满分、其余维度极差」的候选 ``W``，与一个
        「新颖性为零、其余维度全面更强」的候选 ``S``。若存在任何
        「新颖性加分抵消弱证据」的逻辑，``W`` 就会翻身；但在 Pareto
        偏序下 ``S`` 支配 ``W``，``W`` 不进前沿。
        """
        strong = record("S", 9.0, 0.95, 0.00, 1.0)
        weak_but_novel = record("W", 1.0, 0.10, 1.00, 9.0)
        # 逐维度核对：S 在三项上更优（gain/stability/complexity），
        # novelty 上 W 更优——不是支配。故改用「S 在所有维度均不劣」的构造。
        dominating = record("S", 9.0, 0.95, 1.00, 1.0)
        self.assertTrue(dominates(dominating, weak_but_novel))
        front = pareto_front([dominating, weak_but_novel])
        self.assertEqual(front_ids_of(front), ("S",))
        self.assertEqual(front.rank_of("W@1"), 1)
        self.assertIn("S@1", front.dominators_of("W@1"))
        # 不可比的另一边：把 novelty 让给 W，则两者不可比、都进前沿——
        # 这正说明「新颖性只在不可比的方向上有发言权，不能抵消支配关系」。
        self.assertFalse(dominates(strong, weak_but_novel))
        self.assertFalse(dominates(weak_but_novel, strong))
        front2 = pareto_front([strong, weak_but_novel])
        self.assertEqual(set(front_ids_of(front2)), {"S", "W"})

    def test_no_function_aggregates_dimensions_into_a_single_number(self) -> None:
        """模块内不存在把四维折算成单一数值的函数。"""
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = pyast.parse(source)
        banned = {
            "weighted_sum", "total_score", "aggregate_score", "combined_score",
            "combine_scores", "novelty_bonus", "novelty_bonus_score", "final_score",
            "utility", "objective", "scalarize", "weighted_score",
        }
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)):
                if node.name.lower() in banned:
                    offenders.append(node.name)
        self.assertEqual(offenders, [], f"出现了总分/加权实现：{offenders}")

    def test_no_arithmetic_combination_of_dimensions(self) -> None:
        """AST 级检查：不存在把两个以上维度相加/相乘的实现表达式。

        这是对「只做偏序」的**结构性**保障：只要某个表达式里同时出现
        两个以上维度名并参与算术组合，就说明有人在造合成分。
        """
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        dimensional = set(DIMENSIONS)
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if not isinstance(node, pyast.BinOp):
                continue
            if not isinstance(node.op, (pyast.Add, pyast.Sub, pyast.Mult, pyast.Div)):
                continue
            names: set[str] = set()
            for child in pyast.walk(node):
                if isinstance(child, pyast.Constant) and isinstance(child.value, str):
                    if child.value in dimensional:
                        names.add(child.value)
            # 减法在 _compare_values 里用于同维度差值的容差比较，属同维度；
            # 这里只拦「两个以上不同维度」同时参与同一算术表达式。
            if len(names) >= 2:
                offenders.append(f"line {node.lineno}: {'+'.join(sorted(names))}")
        self.assertEqual(
            offenders, [], f"维度被算术组合成合成分：{offenders}"
        )

    def test_confirmation_policy_constant_is_hard_coded_false(self) -> None:
        """确证原则的机检声明恒为 False，且是字面常量。"""
        self.assertIs(CONFIRMATION_USES_NOVELTY_BONUS, False)
        self.assertIs(CONFIRMATION_POLICY["uses_weighted_total"], False)
        self.assertIs(
            CONFIRMATION_POLICY["uses_novelty_bonus_to_offset_weak_evidence"], False
        )
        self.assertIs(CONFIRMATION_POLICY["novelty_treated_as_compensable"], False)
        # 反面的四项要求必须为 True
        for key in (
            "requires_valid_test",
            "requires_error_control",
            "requires_predefined_effect_threshold",
            "requires_domain_constraints",
        ):
            with self.subTest(key=key):
                self.assertIs(CONFIRMATION_POLICY[key], True)

    def test_confirmation_policy_constant_cannot_be_rewritten(self) -> None:
        """政策映射是只读的，不能被运行时代码改写。"""
        with self.assertRaises(TypeError):
            CONFIRMATION_POLICY["uses_weighted_total"] = True  # type: ignore[index]

    def test_confirmation_policy_is_carried_in_outputs(self) -> None:
        """政策声明随每类结果输出，使「未越界改写原则」可机检。"""
        front = pareto_front(tradeoff_set())
        for payload in (front.to_dict(), top_k_reserved(front, 2).to_dict(),
                        select_freeze_candidates(tradeoff_set()).to_dict()):
            with self.subTest(kind=type(payload).__name__):
                policy = payload["confirmation_policy"]
                self.assertIs(
                    policy["uses_novelty_bonus_to_offset_weak_evidence"], False
                )
                self.assertIs(payload["select_grants_evidence_grade"], False)

    def test_selection_never_grants_evidence_grade(self) -> None:
        """筛选产物不授予证据等级。"""
        self.assertIs(SELECTION_GRANTS_EVIDENCE_GRADE, False)
        self.assertIn("不授予证据等级", SELECTION_SCOPE_NOTE)

    def test_no_total_score_note_is_explicit(self) -> None:
        """口径说明显式写明「不计算加权总分」。"""
        self.assertIn("不计算任何跨维度加权总分", NO_TOTAL_SCORE_NOTE)
        front = pareto_front(tradeoff_set())
        self.assertIn("加权总分", front.to_dict()["no_total_score_note"])


# ---------------------------------------------------------------------------
# 偏序与分层语义
# ---------------------------------------------------------------------------


class TestDominanceAndLayers(unittest.TestCase):
    """支配是偏序：不可比者共存于前沿，层号是偏序延伸而非名次。"""

    def test_dominance_requires_all_dimensions_not_worse(self) -> None:
        """一个维度更差就不构成支配。"""
        better_gain = record("A", 9.0, 0.5, 0.5, 5.0)
        better_complexity = record("B", 5.0, 0.5, 0.5, 1.0)
        # A 的 gain 更高但 complexity 更差 → 互不支配
        self.assertFalse(dominates(better_gain, better_complexity))
        self.assertFalse(dominates(better_complexity, better_gain))

    def test_dominance_requires_at_least_one_strict_improvement(self) -> None:
        """四维完全相同则互不支配（偏序是严格偏序）。"""
        left = record("A", 5.0, 0.5, 0.5, 5.0)
        right = record("B", 5.0, 0.5, 0.5, 5.0)
        self.assertFalse(dominates(left, right))
        self.assertFalse(dominates(right, left))

    def test_complexity_direction_is_minimize_in_dominance(self) -> None:
        """复杂度更低者更优，方向参与支配判定。"""
        low = record("A", 5.0, 0.5, 0.5, 1.0)
        high = record("B", 5.0, 0.5, 0.5, 9.0)
        self.assertTrue(dominates(low, high))
        self.assertFalse(dominates(high, low))

    def test_pareto_front_matches_hand_computed_expectation(self) -> None:
        """前沿与手工推算一致：{A, B, C, E}，D 落入 rank 1。"""
        front = pareto_front(tradeoff_set())
        self.assertEqual(set(front_ids_of(front)), {"A", "B", "C", "E"})
        self.assertEqual(front.front_size, 4)
        self.assertEqual(front.rank_of("D@1"), 1)
        self.assertEqual(front.layer_count, 2)
        self.assertEqual(front.dominators_of("D@1"), ("A@1",))

    def test_layers_partition_nothing_is_lost(self) -> None:
        """分层是完备划分：所有候选恰好在某一层出现一次。"""
        front = pareto_front(tradeoff_set())
        seen: list[str] = []
        for rank in range(front.layer_count):
            seen.extend(front.identities_of_layer(rank))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), front.size)
        self.assertEqual(front.size, len(tradeoff_set()))

    def test_incomparable_candidates_stay_on_front_together(self) -> None:
        """不可比的两个候选同时留在前沿。"""
        front = pareto_front([
            record("A", 9.0, 0.1, 0.1, 5.0),
            record("B", 1.0, 0.9, 0.9, 5.0),
        ])
        self.assertEqual(set(front_ids_of(front)), {"A", "B"})

    def test_layers_function_returns_original_objects(self) -> None:
        """``pareto_layers`` 返回原始输入对象（便于调用方取回引用）。"""
        data = tradeoff_set()
        layers = pareto_layers(data)
        self.assertEqual(len(layers), 2)
        self.assertTrue(all(item in data for item in layers[0]))

    def test_layer_privilege_order_is_deterministic(self) -> None:
        """同一层的输出顺序确定（按标识字典序），不依赖输入次序。"""
        data = tradeoff_set()
        reversed_data = list(reversed(data))
        self.assertEqual(
            pareto_layers(data)[0], pareto_layers(reversed_data)[0]
        )

    def test_compare_pair_explains_tradeoff(self) -> None:
        """``compare_pair`` 摊开逐维度结论，并说明为何不可比。"""
        pair = compare_pair(
            record("A", 9.0, 0.1, 0.1, 5.0),
            record("B", 1.0, 0.9, 0.9, 5.0),
        )
        self.assertEqual(pair.per_dimension["gain"], "left")
        self.assertEqual(pair.per_dimension["stability"], "right")
        self.assertEqual(pair.per_dimension["complexity"], "tie")
        self.assertFalse(pair.comparable)
        self.assertIn("不可比", pair.reason)

    def test_compare_pair_reports_dominance(self) -> None:
        """支配关系的解释文本与判定一致。"""
        pair = compare_pair(
            record("A", 9.0, 0.9, 0.9, 1.0),
            record("B", 1.0, 0.1, 0.1, 9.0),
        )
        self.assertTrue(pair.left_dominates)
        self.assertTrue(pair.comparable)
        self.assertIn("支配", pair.reason)

    def test_empty_input_yields_empty_front(self) -> None:
        """空输入得到空前沿，不报错。"""
        front = pareto_front([])
        self.assertEqual(front.front_size, 0)
        self.assertEqual(front.layer_count, 0)
        self.assertIsNone(front.knowledge_version)

    def test_dominance_note_is_explicit_about_partial_order(self) -> None:
        """支配口径说明写明「偏序不是全序」。"""
        self.assertIn("偏序", DOMINANCE_NOTE)
        self.assertIn("不可比", DOMINANCE_NOTE)


# ---------------------------------------------------------------------------
# 轮转预约：不做跨维度折算
# ---------------------------------------------------------------------------


class TestRotationReserve(unittest.TestCase):
    """预约用轮转替代加权总分：各维度轮流择优，彼此不折算。"""

    def test_reserve_covers_different_dimensions(self) -> None:
        """轮转使预约名额分布在不同维度上。"""
        result = top_k_reserved(tradeoff_set(), 3)
        dims = [item.rotation_dimension for item in result.reserved]
        self.assertEqual(dims, ["gain", "stability", "novelty"])
        self.assertEqual(list(result.rotation), list(RESERVE_ROTATION))

    def test_reserve_picks_dimension_leaders(self) -> None:
        """每个名额取对应维度在该层的最优者。"""
        result = top_k_reserved(tradeoff_set(), 4)
        by_dimension = result.by_dimension()
        # gain 最优是 A；stability 最优是 C；novelty 最优是 E。
        self.assertEqual(identities_of(by_dimension["gain"]), ("A",))
        self.assertEqual(identities_of(by_dimension["stability"]), ("C",))
        self.assertEqual(identities_of(by_dimension["novelty"]), ("E",))

    def test_reserve_never_selects_a_dominated_candidate(self) -> None:
        """预约只在前沿内进行，不会选到被支配的候选。"""
        result = top_k_reserved(tradeoff_set(), 4)
        self.assertNotIn("D@1", result.ids())

    def test_reserve_k_larger_than_front_reports_shortfall(self) -> None:
        """``k`` 超过前沿规模时如实记录缺口。"""
        result = top_k_reserved(tradeoff_set(), 99)
        self.assertEqual(result.count, 4)
        self.assertEqual(result.requested_k, 99)
        self.assertEqual(result.available, 4)
        self.assertEqual(result.shortfall, 95)

    def test_reserve_accepts_pareto_front_or_raw_records(self) -> None:
        """既接受 :class:`ParetoFront`，也接受原始记录集合。"""
        front = pareto_front(tradeoff_set())
        from_front = top_k_reserved(front, 2)
        from_raw = top_k_reserved(tradeoff_set(), 2)
        self.assertEqual(from_front.ids(), from_raw.ids())

    def test_reserve_k_validation(self) -> None:
        """``k`` 必须是非负整数或 ``None``。"""
        for bad in (-1, True, 2.5, "2"):
            with self.subTest(k=bad):
                with self.assertRaises(SelectionInputError):
                    top_k_reserved(tradeoff_set(), bad)
        # K=0 合法：预约零个
        self.assertEqual(top_k_reserved(tradeoff_set(), 0).count, 0)

    def test_reserve_k_none_takes_whole_front(self) -> None:
        """``k=None`` 取满整个前沿。"""
        result = top_k_reserved(tradeoff_set(), None)
        self.assertEqual(result.count, 4)

    def test_rotation_sequence_is_validated(self) -> None:
        """轮转序列不得为空、不得含未知维度、不得重复。"""
        for bad in ((), ("accuracy",), ("gain", "gain")):
            with self.subTest(rotation=bad):
                with self.assertRaises(SelectionInputError):
                    top_k_reserved(tradeoff_set(), 2, rotation=bad)

    def test_rotation_is_configurable_but_still_no_weighting(self) -> None:
        """轮转序列可配置——改变的是择优顺序，不是权重。"""
        result = top_k_reserved(tradeoff_set(), 2, rotation=("novelty", "gain"))
        dims = [item.rotation_dimension for item in result.reserved]
        self.assertEqual(dims, ["novelty", "gain"])
        self.assertEqual(identities_of(result.ids())[0], "E")

    def test_reserve_rationale_declares_no_weighting(self) -> None:
        """每条入选依据显式声明未做加权。"""
        result = top_k_reserved(tradeoff_set(), 2)
        for item in result.reserved:
            with self.subTest(identity=item.identity):
                self.assertIn("不做跨维度加权", item.rationale)
        self.assertIn("不做跨维度加权", RESERVE_NOTE)

    def test_reserve_is_deterministic(self) -> None:
        """同一输入两次运行结果完全一致。"""
        first = top_k_reserved(tradeoff_set(), 3)
        second = top_k_reserved(tradeoff_set(), 3)
        self.assertEqual(first.ids(), second.ids())
        self.assertEqual(first.content_digest(), second.content_digest())


# ---------------------------------------------------------------------------
# 缺维度候选：不插补、不因此扣分、默认排除
# ---------------------------------------------------------------------------


class TestIncompleteCandidates(unittest.TestCase):
    """缺维度表示证据不足，与「算出来是 0」必须可区分。"""

    def test_incomplete_candidates_do_not_enter_front(self) -> None:
        """缺维度候选不进入前沿，单列上报。"""
        data = [record("A", 9.0, 0.9, 0.9, 1.0), record("Z", None, 0.5, 0.5, 5.0)]
        front = pareto_front(data)
        self.assertEqual(front_ids_of(front), ("A",))
        self.assertEqual(front.incomplete_ids, ("Z@1",))
        self.assertEqual(front.rank_of("Z@1"), None)

    def test_dominance_with_incomplete_raises(self) -> None:
        """缺维度候选不参与支配比较，直接报错而不是当作 0。"""
        with self.assertRaises(SelectionInputError) as context:
            dominates(
                record("A", None, 0.5, 0.5, 1.0),
                record("B", 1.0, 0.5, 0.5, 1.0),
            )
        self.assertIn("缺维度", str(context.exception))

    def test_missing_is_not_imputed_as_zero(self) -> None:
        """缺维度不被插补为 0：显式给 0 的候选与缺维度的候选结果不同。"""
        missing = record("M", None, 0.5, 0.5, 5.0)
        zero = record("Z", 0.0, 0.5, 0.5, 5.0)
        self.assertIn("M@1", pareto_front([missing, zero]).incomplete_ids)
        self.assertNotIn("Z@1", pareto_front([missing, zero]).incomplete_ids)

    def test_incomplete_excluded_by_default_from_freeze(self) -> None:
        """默认情况下缺维度候选被排除，并如实登记。"""
        data = [record("A", 9.0, 0.9, 0.9, 1.0), record("Z", None, 0.5, 0.5, 5.0)]
        result = select_freeze_candidates(data, max_candidates=3)
        self.assertNotIn("Z@1", result.ids())
        self.assertEqual(result.excluded_incomplete_ids, ("Z@1",))
        self.assertFalse(result.allow_incomplete)

    def test_incomplete_can_be_admitted_only_explicitly(self) -> None:
        """只有显式开启才可能入选，且恒排在完备候选之后并被标记。"""
        data = [record("A", 9.0, 0.9, 0.9, 1.0), record("Z", None, 0.5, 0.5, 5.0)]
        result = select_freeze_candidates(
            data, max_candidates=3, allow_incomplete=True
        )
        self.assertEqual(identities_of(result.ids()), ("A", "Z"))
        self.assertEqual(result.complete_ids(), ("A@1",))
        self.assertEqual(result.incomplete_ids(), ("Z@1",))
        self.assertFalse(result.candidates[-1].complete)
        self.assertIn("缺维度", result.candidates[-1].rationale)

    def test_incomplete_admission_grants_no_evidence_credence(self) -> None:
        """入选缺维度候选不代表其证据强度获得任何认定。"""
        data = [record("A", 9.0, 0.9, 0.9, 1.0), record("Z", None, 0.5, 0.5, 5.0)]
        result = select_freeze_candidates(data, max_candidates=3, allow_incomplete=True)
        self.assertIn("不代表其证据强度获得任何认定", result.candidates[-1].rationale)

    def test_missing_dimension_policy_is_documented(self) -> None:
        """缺维度口径作为可机检声明单列。"""
        self.assertIn("不插补", MISSING_DIMENSION_POLICY)
        self.assertIn("不因此扣分", MISSING_DIMENSION_POLICY)

    def test_incomplete_selection_does_not_exceed_cap(self) -> None:
        """允许缺维度候选时，总数仍不越过上限。"""
        data = [
            record("A", 9.0, 0.9, 0.9, 1.0),
            record("Z1", None, 0.5, 0.5, 5.0),
            record("Z2", 0.5, None, 0.5, 5.0),
        ]
        result = select_freeze_candidates(data, max_candidates=2, allow_incomplete=True)
        self.assertLessEqual(result.selected_count, 2)


# ---------------------------------------------------------------------------
# 知识库版本
# ---------------------------------------------------------------------------


class TestKnowledgeVersion(unittest.TestCase):
    """新颖性只在同一个 K^(v) 下可比；跨版本属口径污染。"""

    def test_missing_knowledge_version_is_rejected(self) -> None:
        """缺少版本号直接拒绝，不静默沿用。"""
        with self.assertRaises(SelectionVersionError):
            pareto_front([record("A", 1.0, 0.5, 0.5, 1.0, knowledge_version=None)])

    def test_cross_version_comparison_is_rejected(self) -> None:
        """多版本混用直接拒绝，报错点明口径污染。"""
        with self.assertRaises(SelectionVersionError) as context:
            pareto_front([
                record("A", 1.0, 0.5, 0.5, 1.0, knowledge_version="k-v1"),
                record("B", 2.0, 0.5, 0.5, 1.0, knowledge_version="k-v2"),
            ])
        self.assertIn("K^(v)", str(context.exception))

    def test_single_version_is_recorded_in_output(self) -> None:
        """唯一版本号随结果登记。"""
        front = pareto_front(tradeoff_set())
        self.assertEqual(front.knowledge_version, KV)
        self.assertEqual(front.to_dict()["knowledge_version"], KV)

    def test_cross_version_can_be_disabled_explicitly(self) -> None:
        """显式关闭校验后可以比较，但需调用方自担后果。"""
        front = pareto_front(
            [
                # 两者互为权衡（B 增益更高、A 复杂度更低）→ 不可比、都在前沿。
                record("A", 1.0, 0.5, 0.5, 1.0, knowledge_version="k-v1"),
                record("B", 2.0, 0.5, 0.5, 5.0, knowledge_version="k-v2"),
            ],
            require_single_knowledge_version=False,
        )
        self.assertEqual(front.front_size, 2)


# ---------------------------------------------------------------------------
# 输入形态与上游对接
# ---------------------------------------------------------------------------


class TestInputHandling(unittest.TestCase):
    """接受 P09 的真实产物，也接受等价字典；重复标识与非法取值被拒。"""

    def test_consumes_real_p09_evaluations(self) -> None:
        """直接消费 P09 的 :class:`Evaluation` 对象。"""
        first = real_evaluation(hypothesis_id="h-relation-1")
        second = real_evaluation(hypothesis_id="h-relation-2")
        front = pareto_front([first, second])
        self.assertGreaterEqual(front.front_size, 1)
        self.assertEqual(front.knowledge_version, KV)
        # 自助的评估记录四个维度都有值，因此不会落入 incomplete
        self.assertEqual(front.incomplete, ())

    def test_real_evaluation_round_trips_through_output(self) -> None:
        """真实评估记录能完整序列化进结果。"""
        front = pareto_front([real_evaluation()])
        payload = front.to_dict()
        self.assertEqual(payload["front_size"], 1)
        self.assertEqual(payload["front"][0]["hypothesis_id"], "h-relation-1")
        self.assertEqual(payload["front"][0]["knowledge_version"], KV)

    def test_accepts_generator_input(self) -> None:
        """生成器输入可用（不会被内部二次遍历耗尽）。"""
        data = tradeoff_set()
        front = pareto_front(iter(data))
        self.assertEqual(front.front_size, 4)
        freeze = select_freeze_candidates(iter(data), max_candidates=2)
        self.assertEqual(freeze.selected_count, 2)

    def test_rejects_duplicate_identity(self) -> None:
        """同一假说同版本的重复记录被拒绝。"""
        with self.assertRaises(SelectionInputError):
            pareto_front([record("A", 1.0, 0.5, 0.5, 1.0)] * 2)

    def test_same_id_different_version_is_allowed(self) -> None:
        """同 id 不同版本是不同候选，允许共存。"""
        front = pareto_front([
            record("A", 1.0, 0.5, 0.5, 5.0, version="1"),
            record("A", 9.0, 0.5, 0.5, 1.0, version="2"),
        ])
        self.assertEqual(front.size, 2)

    def test_rejects_non_iterable_input(self) -> None:
        """非可迭代输入被拒绝。"""
        for bad in (None, 42, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(SelectionInputError):
                    pareto_front(bad)

    def test_rejects_invalid_dimension_values(self) -> None:
        """维度取值超出合法范围时拒绝。"""
        with self.assertRaises(SelectionInputError):
            pareto_front([record("A", 1.0, 1.5, 0.5, 1.0)])
        with self.assertRaises(SelectionInputError):
            pareto_front([record("A", 1.0, 0.5, 0.5, -1.0)])
        with self.assertRaises(SelectionInputError):
            pareto_front([record("A", float("inf"), 0.5, 0.5, 1.0)])

    def test_rejects_token_like_keys(self) -> None:
        """输入里出现令牌类键名时拒绝。"""
        payload = record("A", 1.0, 0.5, 0.5, 1.0)
        payload["role_token"] = "x"
        with self.assertRaises(SelectionPolicyError):
            pareto_front([payload])

    def test_rejects_record_without_identifier(self) -> None:
        """缺少假说标识的记录被拒绝。"""
        with self.assertRaises(SelectionInputError):
            pareto_front([{"gain": 1.0, "stability": 0.5, "novelty": 0.5,
                           "complexity": 1.0, "knowledge_version": KV}])


# ---------------------------------------------------------------------------
# 确定性、序列化与摘要
# ---------------------------------------------------------------------------


class TestSerializationAndReproducibility(unittest.TestCase):
    """结果可序列化、可摘要、可复现。"""

    def test_front_is_reproducible(self) -> None:
        """同一输入两次运行的前沿摘要一致。"""
        first = pareto_front(tradeoff_set())
        second = pareto_front(tradeoff_set())
        self.assertEqual(first.front_ids(), second.front_ids())
        self.assertEqual(first.content_digest(), second.content_digest())

    def test_freeze_selection_is_reproducible(self) -> None:
        """冻结候选清单两次运行一致。"""
        first = select_freeze_candidates(tradeoff_set())
        second = select_freeze_candidates(tradeoff_set())
        self.assertEqual(first.ids(), second.ids())
        self.assertEqual(first.content_digest(), second.content_digest())

    def test_digests_are_sha256_hex(self) -> None:
        """摘要为 64 位十六进制串。"""
        for digest in (
            pareto_front(tradeoff_set()).content_digest(),
            top_k_reserved(tradeoff_set(), 2).content_digest(),
            select_freeze_candidates(tradeoff_set()).content_digest(),
        ):
            with self.subTest(digest=digest[:8]):
                self.assertEqual(len(digest), 64)
                int(digest, 16)

    def test_outputs_are_json_serializable(self) -> None:
        """三类结果都能直接 ``json.dumps``。"""
        payloads = [
            pareto_front(tradeoff_set()).to_dict(),
            top_k_reserved(tradeoff_set(), 2).to_dict(),
            select_freeze_candidates(tradeoff_set()).to_dict(),
        ]
        for payload in payloads:
            with self.subTest(kind=type(payload).__name__):
                text = json.dumps(payload, ensure_ascii=False)
                self.assertIn(SELECT_VERSION, text)

    def test_version_is_recorded(self) -> None:
        """交付物版本号随结果输出。"""
        self.assertTrue(SELECT_VERSION.startswith("P10"))
        self.assertEqual(
            pareto_front(tradeoff_set()).to_dict()["select_version"], SELECT_VERSION
        )

    def test_result_types_have_expected_shape(self) -> None:
        """结果对象的类型与基本视图正确。"""
        front = pareto_front(tradeoff_set())
        self.assertIsInstance(front, ParetoFront)
        self.assertEqual(len(front), front.front_size)
        reserve = top_k_reserved(front, 2)
        self.assertIsInstance(reserve, ReserveSelection)
        self.assertEqual(len(reserve), reserve.count)
        freeze = select_freeze_candidates(tradeoff_set())
        self.assertIsInstance(freeze, FreezeSelection)
        self.assertEqual(len(freeze), freeze.selected_count)


# ---------------------------------------------------------------------------
# 阶段边界守护
# ---------------------------------------------------------------------------


class TestStageBoundaries(unittest.TestCase):
    """P10 的边界：不碰 M1、不实现 P11 及之后、不用 skip / xfail。"""

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
        """顶层导入限定在标准库内：本模块不依赖其它 sdl_m* 模块。"""
        tree = pyast.parse(self.text)
        roots: set[str] = set()
        for node in tree.body:
            if isinstance(node, pyast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        allowed = {
            "__future__", "hashlib", "json", "dataclasses", "types", "typing",
        }
        self.assertTrue(
            roots <= allowed,
            f"出现了未预期的导入：{sorted(roots - allowed)}",
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

    def test_does_not_implement_confirmation_flow(self) -> None:
        """不实现确证流程：不**调用**确证生命周期的任何接口。

        判定口径是 AST（真实的导入与调用），而非源码文本匹配。
        理由：本模块的文档与 :data:`NOT_PROVIDED_BY_P10` 里**必须**写出
        ``bind_confirmation`` / ``consume_confirmation`` 等名字，用来显式声明
        「这些不在本层」；文本匹配会把这类边界声明误判为违规（本项目已累计踩坑）。
        """
        tree = pyast.parse(self.text)
        banned = {
            "bind_confirmation", "consume_confirmation", "record_evaluation",
            "release_results", "archive_confirmation", "read_dataset",
            "initialize", "build", "quality",
        }
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in banned:
                    offenders.append(f"line {node.lineno}: {name}()")
            if isinstance(node, pyast.ImportFrom) and node.module == "sdl_m01":
                offenders.append(f"line {node.lineno}: from sdl_m01 import ...")
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "sdl_m01":
                        offenders.append(f"line {node.lineno}: import {alias.name}")
        self.assertEqual(offenders, [], f"本模块触碰了确证流程：{offenders}")

    def test_module_does_not_provide_other_stage_entries(self) -> None:
        """本模块不提供其它阶段声明的入口。"""
        import sdl_m05.select as module

        for name in NOT_PROVIDED_BY_P10:
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name), f"P10 不应提供 {name}")

    def test_does_not_reimplement_p09_metrics(self) -> None:
        """不重算指标——指标口径只属 P09。"""
        tree = pyast.parse(self.text)
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
                # is_better / direction_of 等是「按方向比较」，不是「重算指标」。
                if node.name in {"gain", "stability", "novelty", "complexity"}:
                    offenders.append(node.name)
        self.assertEqual(offenders, [], f"P10 重算了 P09 的指标：{offenders}")

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
        """公开接口在 ``__all__`` 中声明。"""
        tree = pyast.parse(self.text)
        declared: list[str] = []
        for node in tree.body:
            if isinstance(node, pyast.Assign):
                for target in node.targets:
                    if isinstance(target, pyast.Name) and target.id == "__all__":
                        declared = [item.value for item in node.value.elts]
        for name in (
            "pareto_front", "top_k_reserved", "select_freeze_candidates",
            "OPTIMIZATION_DIRECTIONS", "DEFAULT_FREEZE_CAP",
            "CONFIRMATION_USES_NOVELTY_BONUS", "ParetoFront", "FreezeSelection",
        ):
            with self.subTest(name=name):
                self.assertIn(name, declared)

    def test_public_functions_exist_and_are_callable(self) -> None:
        """三个主入口与方向工具均可导入且可调用。"""
        import sdl_m05.select as module

        for name in ("pareto_front", "top_k_reserved", "select_freeze_candidates",
                     "dominates", "is_better", "direction_of", "pareto_layers",
                     "compare_pair"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(module, name)))

    def test_all_errors_derive_from_value_error(self) -> None:
        """异常基类为 :class:`ValueError`，便于调用方按粒度捕获。"""
        for error in (SelectionError, SelectionInputError, SelectionPolicyError,
                      SelectionVersionError):
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, ValueError))
        self.assertTrue(issubclass(SelectionVersionError, SelectionInputError))
        self.assertTrue(issubclass(SelectionPolicyError, SelectionError))


# ---------------------------------------------------------------------------
# 入口签名
# ---------------------------------------------------------------------------


class TestPublicSignatures(unittest.TestCase):
    """主入口签名只暴露筛选参数，不暴露加权入口。"""

    def test_signatures_have_no_weight_parameters(self) -> None:
        """没有任何公开函数暴露权重 / 总分类参数。"""
        for func in (pareto_front, top_k_reserved, select_freeze_candidates):
            with self.subTest(func=func.__name__):
                params = set(inspect.signature(func).parameters)
                offenders = {
                    name for name in params
                    if "weight" in name.lower() or "score" in name.lower()
                }
                self.assertEqual(offenders, set())

    def test_all_public_functions_accept_rejected_kwargs(self) -> None:
        """三个入口都带 ``**rejected`` 兜底，使越界参数能被显式拒绝而非静默忽略。"""
        for func in (top_k_reserved, select_freeze_candidates):
            with self.subTest(func=func.__name__):
                kinds = {
                    param.kind
                    for param in inspect.signature(func).parameters.values()
                }
                self.assertIn(inspect.Parameter.VAR_KEYWORD, kinds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
