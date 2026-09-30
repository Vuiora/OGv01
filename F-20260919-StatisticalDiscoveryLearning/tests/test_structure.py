"""P05 验收测试：结构模式搜索。

覆盖三条验收标准：

1. **固定 seed 下可复现**：三个检测函数均为纯函数；聚类初始化用局部随机源、
   不触碰全局随机状态；簇编号按中心坐标重排；同一输入恒得逐字节相同的
   ``to_dict()``；且结果与「样本行顺序」「参数传递形式」无关。
2. **纯标准库简化实现**：静态检查模块源码不含 numpy / sklearn / scipy /
   pandas 等第三方导入，只依赖标准库与仓内既有模块。
3. **参数不适用时返回空结果**：数据不足、特征无变异、簇数不可行、
   分离度不足、无变点通过惩罚、覆盖率不足、不变量判据不成立等情形
   一律返回 ``[]``，而不是抛未捕获异常或伪造结论。

另覆盖：模式对象与 ``INTERFACES.md`` §2.2 的字段对齐（误差 / 残差 / 复杂度
三项恒存在）、稳定度字段在 P05 恒为 ``None``、``StructurePolicyError`` 与
``StructureError`` 的区分、以及 P05 的边界守护——不做重采样稳定性（P06）、
不做关系拟合（P04）、不计算 p 值、不授予证据等级、不修改 ``sdl_m01/``。
"""

import ast as pyast
import copy
import inspect
import json
import pathlib
import random
import re
import unittest

from sdl_m03.equations import ExplorationSample, prepare_sample
from sdl_m03.structure import (
    DEFAULT_MAX_CHANGEPOINTS,
    DEFAULT_MAX_CLUSTERS,
    DEFAULT_MIN_CLUSTER_SIZE,
    DEFAULT_MIN_CONFORMITY,
    DEFAULT_MIN_COVERAGE,
    DEFAULT_MIN_SEGMENT,
    DEFAULT_MIN_SILHOUETTE,
    DEFAULT_PENALTY,
    DEFAULT_SEED,
    DEFAULT_TOLERANCE,
    PATTERN_KINDS,
    STRUCTURE_COMPLEXITY_NOTE,
    STRUCTURE_VERSION,
    StructureError,
    StructurePattern,
    StructurePolicyError,
    changepoint_positions,
    check_invariant,
    cluster_labels,
    find_changepoints,
    find_clusters,
    select_cluster_count,
    silhouette_score,
)

# ---------------------------------------------------------------------------
# 夹具：确定性合成数据
# ---------------------------------------------------------------------------

VARIABLES = ["X1", "X2", "X3"]

#: 模块源码路径，用于静态检查第三方依赖。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m03" / "structure.py"


def _row(record_id: str, x1: float, x2: float, x3: float, y: float) -> dict:
    """构造一条合成记录（只含必要字段）。"""
    return {
        "record_id": record_id,
        "values": {"X1": x1, "X2": x2, "X3": x3, "Y": y},
        "units": {"X1": "mol/L", "X2": "mol/L", "X3": "mol/L", "Y": "mol/(L*s)"},
    }


def two_group_records() -> list[dict]:
    """两簇数据：A 组在 (0, 0)、(10, 5) 附近，X3 为常量。

    X3 恒为 1.0，用于验证「无变异特征被剔除」。
    """
    records: list[dict] = []
    for index in range(30):
        records.append(_row(f"a-{index:03d}", 0.0 + (index % 5) * 0.01, 0.0, 1.0, 1.0))
    for index in range(30):
        records.append(_row(f"b-{index:03d}", 10.0 + (index % 5) * 0.01, 5.0, 1.0, 2.0))
    return records


def one_group_records(count: int = 40) -> list[dict]:
    """单簇数据：完全无结构，所有点重合在 ``(5, 5)``。

    这样聚类的轮廓系数恒为 0（所有点重合，``a = b = 0``），
    用于验证「无结构时默认门槛拦得住」。
    """
    return [_row(f"s-{index:03d}", 5.0, 5.0, 5.0, 5.0) for index in range(count)]


def uniform_records(count: int = 40) -> list[dict]:
    """均匀排布数据：存在刻度的直线加点，k-means 仍会切出簇。

    用于如实记录已知限制——无结构数据下 k-means 依然会给出划分，
    因此**必须由调用方的 ``min_silhouette`` 门槛**来决定接受与否。
    """
    return [
        _row(f"u-{index:03d}", float(index), 1.0, 1.0, float(index))
        for index in range(count)
    ]


def step_records() -> list[dict]:
    """单变点数据：前 30 行均值 0，后 30 行均值 10。"""
    records: list[dict] = []
    for index in range(60):
        level = 0.0 if index < 30 else 10.0
        records.append(
            _row(f"c-{index:03d}", float(index), 1.0, 1.0, level + 0.01 * (index % 3))
        )
    return records


def invariant_records(count: int = 20) -> list[dict]:
    """不变量数据：``X1 * X2`` 恒为 2.0，而 ``X1`` 本身在变。

    与 :func:`sdl_m02.expressions` 的规范式一致：乘法的**左右操作数顺序**
    由规范化决定，故此处用 ``X1*X2`` 作为被检验的表达式。
    """
    return [
        _row(f"i-{index:03d}", 0.5 + 0.1 * index, 2.0 / (0.5 + 0.1 * index), 1.0, 2.5)
        for index in range(count)
    ]


#: 不变量夹具中恒为常数的候选表达式。
INVARIANT_EXPRESSION = "X1*X2"


def sample(records: list[dict], variables: list[str] | None = None) -> ExplorationSample:
    """构造探索分区样本（默认使用全部三个自变量）。"""
    return prepare_sample(records, variables or VARIABLES, "Y")


# ---------------------------------------------------------------------------
# 验收标准②：纯标准库简化实现
# ---------------------------------------------------------------------------


class TestStandardLibraryOnly(unittest.TestCase):
    """验收标准②：不引入 numpy / sklearn 等第三方依赖。"""

    FORBIDDEN = (
        "numpy",
        "scipy",
        "sklearn",
        "pandas",
        "statsmodels",
        "networkx",
        "matplotlib",
    )

    def test_no_third_party_imports(self) -> None:
        """源码中不存在被禁用的第三方导入。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            root = name.split(".")[0]
            with self.subTest(module=name):
                self.assertNotIn(root, self.FORBIDDEN)

    def test_only_expected_top_level_imports(self) -> None:
        """顶层导入只来自标准库与仓内既有模块。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in tree.body:
            if isinstance(node, pyast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue(
            roots <= {"__future__", "hashlib", "json", "math", "random", "dataclasses", "typing", "sdl_m02", "sdl_m03"},
            f"出现了未预期的导入：{sorted(roots)}",
        )

    def test_random_module_is_never_used_globally(self) -> None:
        """随机源只能是局部 ``Random`` 实例，不得调用 ``random.random`` 等模块级函数。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        offenders = re.findall(r"\brandom\.(?!Random\b)\w+\s*\(", text)
        self.assertEqual(offenders, [], f"存在模块级随机调用：{offenders}")


# ---------------------------------------------------------------------------
# 验收标准①：固定 seed 下可复现
# ---------------------------------------------------------------------------


class TestDeterminism(unittest.TestCase):
    """验收标准①：三个检测函数在固定 seed 下逐字节可复现。"""

    def test_clusters_repeatable(self) -> None:
        """同一 seed 两次调用结果完全相同。"""
        data = sample(two_group_records())
        first = [p.to_dict() for p in find_clusters(data, seed=42)]
        second = [p.to_dict() for p in find_clusters(data, seed=42)]
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )

    def test_changepoints_repeatable(self) -> None:
        """变点检测无随机性，重复调用结果完全相同。"""
        data = sample(step_records())
        first = [p.to_dict() for p in find_changepoints(data, order_by="X1")]
        second = [p.to_dict() for p in find_changepoints(data, order_by="X1")]
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_invariant_repeatable(self) -> None:
        """不变量检测无随机性，重复调用结果完全相同。"""
        data = sample(invariant_records())
        first = [p.to_dict() for p in check_invariant(data, INVARIANT_EXPRESSION)]
        second = [p.to_dict() for p in check_invariant(data, INVARIANT_EXPRESSION)]
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_default_seed_is_used_and_documented(self) -> None:
        """不传 seed 时使用固定默认值，且与显式传该默认值等价。"""
        data = sample(two_group_records())
        self.assertEqual(
            [p.to_dict() for p in find_clusters(data)],
            [p.to_dict() for p in find_clusters(data, seed=DEFAULT_SEED)],
        )

    def test_cluster_labels_are_seed_stable(self) -> None:
        """``cluster_labels`` 在同一 seed 下稳定，且返回逐行标签。"""
        data = sample(two_group_records())
        self.assertEqual(
            cluster_labels(data, k=2, seed=5), cluster_labels(data, k=2, seed=5)
        )
        self.assertEqual(len(cluster_labels(data, k=2, seed=5)), len(data.rows))

    def test_global_random_state_is_untouched(self) -> None:
        """调用检测函数不会推进全局随机状态（不污染调用方）。"""
        data = sample(two_group_records())
        random.seed(1234)
        before = random.random()
        random.seed(1234)
        find_clusters(data, seed=99)
        select_cluster_count(data, seed=99)
        after = random.random()
        self.assertEqual(before, after)

    def test_result_independent_of_input_row_order(self) -> None:
        """打乱样本行顺序后，聚类划分（按成员集合）不变。"""
        records = two_group_records()
        data = sample(records)
        shuffling = copy.deepcopy(records)
        random.Random(7).shuffle(shuffling)
        shuffled = sample(shuffling)
        self.assertNotEqual(
            [row.record_id for row in data.rows], [row.record_id for row in shuffled.rows]
        )
        self.assertEqual(
            sorted(sorted(p.payload["members"]) for p in find_clusters(data, seed=3)),
            sorted(sorted(p.payload["members"]) for p in find_clusters(shuffled, seed=3)),
        )

    def test_changepoint_positions_independent_of_row_order(self) -> None:
        """按 ``order_by`` 排序后，行顺序不影响变点位置与阈值。"""
        records = step_records()
        shuffling = copy.deepcopy(records)
        random.Random(11).shuffle(shuffling)
        self.assertEqual(
            [
                (p.payload["position"], p.payload["threshold"])
                for p in find_changepoints(sample(records), order_by="X1")
            ],
            [
                (p.payload["position"], p.payload["threshold"])
                for p in find_changepoints(sample(shuffling), order_by="X1")
            ],
        )

    def test_pattern_ids_are_stable(self) -> None:
        """``pattern_id`` 由语义内容派生，重复调用不变。"""
        data = sample(two_group_records())
        first = [p.pattern_id for p in find_clusters(data, seed=42)]
        second = [p.pattern_id for p in find_clusters(data, seed=42)]
        self.assertEqual(first, second)
        self.assertTrue(all(pid.startswith("pat-") for pid in first))

    def test_pure_functions_have_no_module_state(self) -> None:
        """三个检测函数都是纯函数：调用前后不残留模块级可变状态。"""
        module = inspect.getmodule(find_clusters)
        dirty = {
            name: value
            for name, value in vars(module).items()
            if not name.startswith("__")
            and isinstance(value, (list, dict, set))
        }
        data = sample(two_group_records())
        find_clusters(data, seed=1)
        find_changepoints(sample(step_records()), order_by="X1")
        check_invariant(sample(invariant_records()), INVARIANT_EXPRESSION)
        after = {
            name: value
            for name, value in vars(module).items()
            if not name.startswith("__")
            and isinstance(value, (list, dict, set))
        }
        self.assertEqual(dirty, after)

    def test_pattern_id_depends_on_payload(self) -> None:
        """载荷不同的模式获得不同标识（标识确有区分力）。"""
        first = StructurePattern(
            pattern_id="pat-x",
            kind="invariant",
            payload={"estimate": 1.0},
            metrics={"error": 0, "residual": 0, "complexity": {}},
            provenance={"origin": "test"},
        )
        self.assertEqual(first.pattern_id, "pat-x")
        data = sample(invariant_records())
        patterns = check_invariant(data, INVARIANT_EXPRESSION)
        other = check_invariant(data, "0*X1+1.0")
        if other:
            self.assertNotEqual(patterns[0].pattern_id, other[0].pattern_id)


# ---------------------------------------------------------------------------
# 验收标准③：不适用时返回空结果
# ---------------------------------------------------------------------------


class TestEmptyResultsWhenNotApplicable(unittest.TestCase):
    """验收标准③：参数不适用时返回空列表，而非抛未捕获异常。"""

    def test_too_few_rows_for_clustering(self) -> None:
        """行数不足以构成两个最小规模的簇 → 空结果。"""
        data = sample(one_group_records(3))
        self.assertEqual(find_clusters(data), [])

    def test_too_few_rows_for_changepoints(self) -> None:
        """行数不足以构成两个最小分段 → 空结果。"""
        data = sample(one_group_records(DEFAULT_MIN_SEGMENT))
        self.assertEqual(find_changepoints(data, order_by="X1"), [])

    def test_no_variation_returns_empty_for_changepoints(self) -> None:
        """序列无变异时不存在变点 → 空结果。"""
        records = [_row(f"f-{i:03d}", float(i), 1.0, 1.0, 3.0) for i in range(30)]
        self.assertEqual(find_changepoints(sample(records), order_by="X1"), [])

    def test_all_features_without_variation_returns_empty(self) -> None:
        """全部所选特征均无变异 → 聚类返回空结果。"""
        records = [_row(f"f-{i:03d}", 1.0, 2.0, 3.0, float(i)) for i in range(30)]
        self.assertEqual(find_clusters(sample(records)), [])

    def test_constant_feature_is_dropped_not_fatal(self) -> None:
        """常量特征被剔除，其余特征仍可完成聚类。"""
        data = sample(two_group_records())
        selection = select_cluster_count(data, seed=42)
        self.assertIn("X3", selection["dropped_features"])
        self.assertTrue(selection["kept_features"])
        self.assertTrue(find_clusters(data, seed=42))

    def test_inseparable_data_returns_empty(self) -> None:
        """所有点重合（无任何分离度）→ 空结果（不把噪声当子群）。"""
        data = sample(one_group_records())
        patterns = find_clusters(data, seed=42)
        self.assertEqual(patterns, [])

    def test_high_silhouette_threshold_returns_empty(self) -> None:
        """把分离度门槛提到 1.1 以上 → 空结果。"""
        data = sample(two_group_records())
        self.assertTrue(find_clusters(data, seed=42))
        self.assertEqual(find_clusters(data, seed=42, min_silhouette=2.0), [])

    def test_k_larger_than_rows_returns_empty(self) -> None:
        """``max_clusters`` 远超行数时没有可行簇数 → 空结果。"""
        data = sample(uniform_records(4))
        self.assertEqual(find_clusters(data, max_clusters=99, min_cluster_size=5), [])

    def test_changepoint_zero_penalty_still_needs_segments(self) -> None:
        """惩罚为 0 也要满足最小分段长度，短序列 → 空结果。"""
        records = [_row(f"c-{i:03d}", float(i), 1.0, 1.0, 0.0 if i < 3 else 9.0) for i in range(5)]
        self.assertEqual(
            find_changepoints(sample(records), order_by="X1", min_segment=3), []
        )

    def test_huge_penalty_returns_empty(self) -> None:
        """惩罚极大时任何分裂都不通过 → 空结果。"""
        data = sample(step_records())
        self.assertTrue(find_changepoints(data, order_by="X1"))
        self.assertEqual(find_changepoints(data, order_by="X1", penalty=1e12), [])

    def test_invariant_fails_when_values_vary(self) -> None:
        """表达式取值明显变化 → 空结果。"""
        data = sample(invariant_records())
        self.assertEqual(check_invariant(data, "X1"), [])

    def test_invariant_fails_when_tolerance_too_tight(self) -> None:
        """容差过紧（0）时，带浮点噪声的常量也不再判为不变量。"""
        # X3*2.0 的取值恒为 2.0 加 2e-9 级浮点噪声。
        records = [
            _row(f"n-{index:03d}", float(index), 1.0, 1.0 + 1e-9 * (index % 3), 0.0)
            for index in range(30)
        ]
        data = sample(records)
        self.assertTrue(check_invariant(data, "X3*2.0", tolerance=1e-6))
        self.assertEqual(check_invariant(data, "X3*2.0", tolerance=0.0), [])

    def test_invariant_fails_when_conformity_floor_higher_than_actual(self) -> None:
        """一致度门槛高于实际一致行占比时 → 空结果。"""
        records = invariant_records()
        for index in range(4):
            records[index]["values"]["X2"] = 99.0 + index
        data = sample(records)
        # 16/20 = 80% 一致。
        self.assertEqual(check_invariant(data, INVARIANT_EXPRESSION, tolerance=0.01), [])
        self.assertTrue(
            check_invariant(
                data, INVARIANT_EXPRESSION, tolerance=0.01, min_conformity=0.75
            )
        )

    def test_invariant_fails_when_coverage_too_low(self) -> None:
        """定义域把样本剪得太狠 → 覆盖率不足，返回空结果。"""
        # Z = 1/X1，X1 从 -9 到 10，包含 0 附近的行会被定义域排除。
        records = [
            _row(f"z-{i:03d}", float(index - 9), 1.0, 1.0, 1.0)
            for index, i in enumerate(range(20))
        ]
        data = sample(records)
        self.assertEqual(check_invariant(data, "1/X1", min_coverage=0.99), [])

    def test_invariant_empty_sample_returns_empty(self) -> None:
        """样本为空 → 空结果。"""
        empty = ExplorationSample(variables=("X1",), target="Y", rows=())
        self.assertEqual(check_invariant(empty, "X1"), [])

    def test_selection_reports_no_choice_when_not_feasible(self) -> None:
        """不可行时 ``select_cluster_count`` 如实报告无选择，而非抛错。"""
        data = sample(one_group_records(3))
        selection = select_cluster_count(data, min_cluster_size=2)
        self.assertIsNone(selection["k"])
        self.assertEqual(selection["candidates"], [])

    def test_silhouette_of_single_cluster_is_zero(self) -> None:
        """单簇的轮廓系数约定为 0.0（不产生除零伪结论）。"""
        matrix = [[0.0], [1.0], [2.0]]
        self.assertEqual(silhouette_score(matrix, [0, 0, 0]), 0.0)

    def test_changepoint_positions_returns_list(self) -> None:
        """``changepoint_positions`` 在无变点时返回空列表。"""
        records = [_row(f"f-{i:03d}", float(i), 1.0, 1.0, 3.0) for i in range(30)]
        self.assertEqual(changepoint_positions(sample(records), order_by="X1"), [])


# ---------------------------------------------------------------------------
# 检测正确性
# ---------------------------------------------------------------------------


class TestClusteringCorrectness(unittest.TestCase):
    """聚类在构造数据上的正确性。"""

    def test_two_clear_groups_are_found(self) -> None:
        """两团明显分开的数据被分成两组，每组各 30 行。"""
        patterns = find_clusters(sample(two_group_records()), seed=42)
        self.assertEqual(len(patterns), 2)
        self.assertEqual(sorted(p.payload["size"] for p in patterns), [30, 30])

    def test_group_members_match_source_labels(self) -> None:
        """簇成员与构造时的人工分组一致。"""
        patterns = find_clusters(sample(two_group_records()), seed=42)
        groups = {frozenset(p.payload["members"]) for p in patterns}
        expected_a = frozenset(f"a-{index:03d}" for index in range(30))
        expected_b = frozenset(f"b-{index:03d}" for index in range(30))
        self.assertEqual(groups, {expected_a, expected_b})

    def test_centers_are_in_original_units(self) -> None:
        """簇心给出原始量纲取值，且落在两组附近。"""
        patterns = find_clusters(sample(two_group_records()), seed=42)
        x1_centers = sorted(p.payload["center"]["X1"] for p in patterns)
        self.assertLess(x1_centers[0], 1.0)
        self.assertGreater(x1_centers[1], 9.0)
        x2_centers = sorted(p.payload["center"]["X2"] for p in patterns)
        self.assertAlmostEqual(x2_centers[0], 0.0, places=6)
        self.assertAlmostEqual(x2_centers[1], 5.0, places=6)

    def test_cluster_index_is_ordered_by_center(self) -> None:
        """簇编号按中心坐标排序，故 0 号簇在 X2 上更小。"""
        patterns = find_clusters(sample(two_group_records()), seed=42)
        by_index = {p.payload["cluster_index"]: p for p in patterns}
        self.assertLess(by_index[0].payload["center"]["X2"], by_index[1].payload["center"]["X2"])

    def test_metrics_are_meaningful(self) -> None:
        """簇内误差与平均距离为非负有限值。"""
        for pattern in find_clusters(sample(two_group_records()), seed=42):
            with self.subTest(cluster=pattern.payload["cluster_index"]):
                self.assertGreaterEqual(pattern.error, 0.0)
                self.assertGreaterEqual(pattern.residual, 0.0)

    def test_different_seeds_may_agree_on_clear_structure(self) -> None:
        """结构极清晰时，不同种子的划分一致（划分而非编号）。"""
        data = sample(two_group_records())
        baseline = sorted(sorted(p.payload["members"]) for p in find_clusters(data, seed=1))
        for seed in (2, 3, 17, 101):
            with self.subTest(seed=seed):
                self.assertEqual(
                    sorted(sorted(p.payload["members"]) for p in find_clusters(data, seed=seed)),
                    baseline,
                )

    def test_k_equals_row_count_supported(self) -> None:
        """``k`` 等于行数在参数上合法（每点一簇）。"""
        data = sample(uniform_records(4))
        self.assertEqual(len(cluster_labels(data, k=4, seed=0)), 4)


class TestChangepointCorrectness(unittest.TestCase):
    """变点检测在构造数据上的正确性。"""

    def test_single_step_is_detected_at_boundary(self) -> None:
        """单变点被定位在第 30 行（索引 30 处）。"""
        patterns = find_changepoints(sample(step_records()), order_by="X1")
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0].payload["position"], 30)

    def test_threshold_between_neighbouring_orders(self) -> None:
        """阈值落在变点前后两个排序键的中点。"""
        patterns = find_changepoints(sample(step_records()), order_by="X1")
        self.assertAlmostEqual(patterns[0].payload["threshold"], 29.5, places=6)

    def test_mean_shift_sign_and_magnitude(self) -> None:
        """均值漂移为正且接近 10。"""
        pattern = find_changepoints(sample(step_records()), order_by="X1")[0]
        self.assertGreater(pattern.payload["mean_shift"], 0.0)
        self.assertAlmostEqual(pattern.payload["mean_shift"], 10.0, places=2)

    def test_segments_partition_the_series(self) -> None:
        """分段合计覆盖全部行，无重复无遗漏。"""
        patterns = find_changepoints(sample(step_records()), order_by="X1")
        pattern = patterns[0]
        self.assertEqual(pattern.payload["before"]["count"] + pattern.payload["after"]["count"], 60)
        self.assertEqual(pattern.payload["segments_total"], 2)

    def test_no_order_by_uses_row_order(self) -> None:
        """``order_by=None`` 按样本行序，同样能找到构造的变点。"""
        patterns = find_changepoints(sample(step_records()))
        self.assertEqual([p.payload["position"] for p in patterns], [30])

    def test_explicit_series_column_is_supported(self) -> None:
        """可直接传入数值列作为被检测序列。"""
        data = sample(step_records())
        column = [row.target for row in data.rows]
        patterns = find_changepoints(data, order_by="X1", series=column)
        self.assertEqual([p.payload["position"] for p in patterns], [30])

    def test_expression_series_is_supported(self) -> None:
        """可传入表达式作为被检测序列。"""
        data = sample(step_records())
        patterns = find_changepoints(data, order_by="X1", series="Y*1.0")
        self.assertEqual([p.payload["position"] for p in patterns], [30])

    def test_max_changepoints_is_respected(self) -> None:
        """多点数据下变点数量不超过上限。"""
        records: list[dict] = []
        for index in range(90):
            level = 0.0 if index < 30 else (10.0 if index < 60 else 20.0)
            records.append(_row(f"m-{index:03d}", float(index), 1.0, 1.0, level))
        data = sample(records)
        self.assertEqual(len(find_changepoints(data, order_by="X1", max_changepoints=1)), 1)
        self.assertLessEqual(len(find_changepoints(data, order_by="X1", max_changepoints=2)), 2)

    def test_zero_penalty_finds_all_steps(self) -> None:
        """惩罚为 0 时两个台阶都应被发现。"""
        records: list[dict] = []
        for index in range(90):
            level = 0.0 if index < 30 else (10.0 if index < 60 else 20.0)
            records.append(_row(f"m-{index:03d}", float(index), 1.0, 1.0, level))
        patterns = find_changepoints(sample(records), order_by="X1", penalty=0.0, max_changepoints=5)
        self.assertEqual([p.payload["position"] for p in patterns], [30, 60])

    def test_segment_rmse_is_recorded(self) -> None:
        """每条变点模式附带分段 RMSE，便于人工复核。"""
        pattern = find_changepoints(sample(step_records()), order_by="X1")[0]
        self.assertIn("segment_rmse", pattern.metrics)
        self.assertGreaterEqual(float(pattern.metrics["segment_rmse"]), 0.0)

    def test_nonfinite_rows_are_dropped_and_counted(self) -> None:
        """被检测序列含非有限值时，该行被剔除并在来源信息中计数（不填补）。"""
        data = sample(step_records())
        column = [row.target for row in data.rows]
        column[0] = float("nan")
        column[5] = float("inf")
        patterns = find_changepoints(data, order_by="X1", series=column)
        self.assertTrue(patterns)
        self.assertEqual(patterns[0].provenance["parameters"]["dropped_nonfinite"], 2)
        # 剔除两行后剩余 58 行，构造的台阶仍在原第 30 行（剔除点都在其左侧）。
        self.assertEqual(patterns[0].payload["before"]["count"] + patterns[0].payload["after"]["count"], 58)


class TestInvariantCorrectness(unittest.TestCase):
    """不变量检测在构造数据上的正确性。"""

    def test_constant_expression_is_detected(self) -> None:
        """``X1*X2`` 在构造数据上恒为 2.0，被识别为不变量。"""
        patterns = check_invariant(sample(invariant_records()), INVARIANT_EXPRESSION)
        self.assertEqual(len(patterns), 1)
        self.assertAlmostEqual(patterns[0].payload["estimate"], 2.0, places=9)
        self.assertEqual(patterns[0].payload["counterexample_count"], 0)
        self.assertAlmostEqual(patterns[0].payload["conformity"], 1.0, places=9)

    def test_criterion_and_scale_are_reported(self) -> None:
        """判据、容差与尺度分母的选取都随结果一起输出。"""
        payload = check_invariant(sample(invariant_records()), INVARIANT_EXPRESSION)[0].payload
        self.assertEqual(payload["basis"], "relative")
        self.assertEqual(payload["tolerance"], DEFAULT_TOLERANCE)
        self.assertEqual(payload["min_conformity"], DEFAULT_MIN_CONFORMITY)
        self.assertIn("一致行", payload["criterion"])
        self.assertAlmostEqual(payload["scale"], abs(payload["estimate"]), places=9)

    def test_absolute_fallback_near_zero(self) -> None:
        """估计量中位数接近零时，尺度退化为 1.0（绝对容差），并如实标注。"""
        records = [_row(f"z-{i:03d}", float(i), 1.0, 1.0, 0.0) for i in range(20)]
        patterns = check_invariant(sample(records), "X1*0.0")
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0].payload["basis"], "absolute")
        self.assertEqual(patterns[0].payload["scale"], 1.0)

    def test_tolerated_outliers_are_listed_as_counterexamples(self) -> None:
        """少量超标行不否决不变量，而被列为反例（按偏离度降序）。"""
        records = invariant_records()
        # 3/20 = 15% 超标，低于 10% 的一致度门槛 → 应被否决。
        for index in range(3):
            records[index]["values"]["X2"] = 50.0 + index
        rejected = check_invariant(
            sample(records), INVARIANT_EXPRESSION, tolerance=0.05, min_conformity=0.9
        )
        self.assertEqual(rejected, [])
        # 放宽一致度门槛到 85% 后应被接受，且反例被逐条列出。
        patterns = check_invariant(
            sample(records), INVARIANT_EXPRESSION, tolerance=0.05, min_conformity=0.85
        )
        self.assertEqual(len(patterns), 1)
        payload = patterns[0].payload
        self.assertEqual(payload["counterexample_count"], 3)
        deviations = [item["deviation"] for item in payload["counterexamples"]]
        self.assertEqual(deviations, sorted(deviations, reverse=True))
        self.assertGreaterEqual(payload["conformity"], 0.85)

    def test_counterexample_limit_is_respected(self) -> None:
        """反例清单长度不超过给定上限。"""
        records = invariant_records()
        for index in range(0, 4):
            records[index]["values"]["X2"] = 50.0 + index
        patterns = check_invariant(
            sample(records),
            INVARIANT_EXPRESSION,
            tolerance=0.001,
            min_conformity=0.5,
            max_counterexamples=2,
        )
        self.assertEqual(len(patterns), 1)
        payload = patterns[0].payload
        self.assertEqual(len(payload["counterexamples"]), 2)
        self.assertGreater(payload["counterexample_count"], 2)

    def test_conformity_floor_is_enforced(self) -> None:
        """一致行占比不足门槛 → 空结果；放宽门槛后同一数据被接受。"""
        # X1 恒为 1.0，X2 的 16 行取 2.0（乘积 2.0）、4 行取 3.0（乘积 3.0）。
        # 中位数仍为 2.0，故一致行 16/20 = 80%，反例 4 条偏离 50%。
        records = [
            _row(f"g-{index:03d}", 1.0, 2.0 if index < 16 else 3.0, 1.0, float(index))
            for index in range(20)
        ]
        data = sample(records)
        # 低于默认 90% 门槛 → 空结果。
        self.assertEqual(check_invariant(data, INVARIANT_EXPRESSION, tolerance=0.05), [])
        accepted = check_invariant(
            data, INVARIANT_EXPRESSION, tolerance=0.05, min_conformity=0.75
        )
        self.assertEqual(len(accepted), 1)
        payload = accepted[0].payload
        self.assertAlmostEqual(payload["conformity"], 0.8, places=9)
        self.assertEqual(payload["counterexample_count"], 4)

    def test_coverage_is_recorded(self) -> None:
        """覆盖率与定义域限制统计随结果一起输出。"""
        payload = check_invariant(sample(invariant_records()), INVARIANT_EXPRESSION)[0].payload
        self.assertEqual(payload["n_used"], payload["n_rows"])
        self.assertEqual(payload["n_excluded"], 0)
        self.assertAlmostEqual(payload["coverage"], 1.0, places=9)

    def test_numeric_column_relationship_is_supported(self) -> None:
        """可直接传入预先算好的数值列作为候选表示。"""
        data = sample(invariant_records())
        column = [row.values["X1"] * row.values["X2"] for row in data.rows]
        patterns = check_invariant(data, column)
        self.assertEqual(len(patterns), 1)
        self.assertAlmostEqual(patterns[0].payload["estimate"], 2.0, places=9)


# ---------------------------------------------------------------------------
# 模式对象契约
# ---------------------------------------------------------------------------


class TestPatternContract(unittest.TestCase):
    """模式对象与 ``INTERFACES.md`` §2.2 的字段对齐。"""

    def _all_patterns(self) -> list[StructurePattern]:
        return [
            *find_clusters(sample(two_group_records()), seed=42),
            *find_changepoints(sample(step_records()), order_by="X1"),
            *check_invariant(sample(invariant_records()), INVARIANT_EXPRESSION),
        ]

    def test_required_fields_exist(self) -> None:
        """每条模式都有 ``pattern_id`` / ``kind`` / ``payload`` / ``metrics``
        / ``stability`` / ``provenance`` 六个契约字段。"""
        for pattern in self._all_patterns():
            dumped = pattern.to_dict()
            with self.subTest(kind=pattern.kind):
                self.assertEqual(
                    set(dumped),
                    {"pattern_id", "kind", "payload", "metrics", "stability", "provenance"},
                )

    def test_kind_is_known(self) -> None:
        """模式种类落在 ``PATTERN_KINDS`` 内。"""
        for pattern in self._all_patterns():
            with self.subTest(kind=pattern.kind):
                self.assertIn(pattern.kind, PATTERN_KINDS)

    def test_three_metric_components_always_present(self) -> None:
        """误差、残差、复杂度三项恒存在且非空。"""
        for pattern in self._all_patterns():
            with self.subTest(kind=pattern.kind):
                self.assertIn("error", pattern.metrics)
                self.assertIn("residual", pattern.metrics)
                self.assertIsInstance(pattern.metrics["complexity"], dict)
                self.assertTrue(pattern.metrics["complexity"])
                self.assertEqual(pattern.metrics["complexity_note"], STRUCTURE_COMPLEXITY_NOTE)

    def test_stability_is_none_in_this_stage(self) -> None:
        """P05 不评估稳定性，故 ``stability`` 恒为 ``None``。"""
        for pattern in self._all_patterns():
            with self.subTest(kind=pattern.kind):
                self.assertIsNone(pattern.stability)
                self.assertIsNone(pattern.to_dict()["stability"])

    def test_provenance_has_no_restricted_content(self) -> None:
        """来源信息只含抽象引用，不含令牌、记录正文或受限质量报告。"""
        for pattern in self._all_patterns():
            text = json.dumps(pattern.to_dict()["provenance"], ensure_ascii=False)
            with self.subTest(kind=pattern.kind):
                self.assertNotIn("token", text.lower())
                self.assertNotIn("_quality", text)
                self.assertEqual(pattern.provenance["module"], "sdl_m03/structure.py")
                self.assertEqual(pattern.provenance["module_version"], STRUCTURE_VERSION)
                self.assertEqual(pattern.provenance["purpose"], "E")

    def test_provenance_carries_sample_fingerprint(self) -> None:
        """来源信息带有样本指纹，使「用了哪份数据」可核验。"""
        data = sample(two_group_records())
        for pattern in find_clusters(data, seed=42):
            with self.subTest(cluster=pattern.payload["cluster_index"]):
                self.assertEqual(pattern.provenance["sample_fingerprint"], data.fingerprint)

    def test_result_is_json_serializable(self) -> None:
        """所有模式都能序列化为合法 JSON。"""
        for pattern in self._all_patterns():
            with self.subTest(kind=pattern.kind):
                text = json.dumps(pattern.to_dict(), ensure_ascii=False, sort_keys=True)
                self.assertEqual(json.loads(text)["kind"], pattern.kind)

    def test_metrics_missing_component_is_rejected(self) -> None:
        """构造缺项模式在对象层面即被拒绝。"""
        for missing in ("error", "residual", "complexity"):
            with self.subTest(missing=missing):
                metrics = {"error": 0.0, "residual": 0.0, "complexity": {"total": 1}}
                del metrics[missing]
                with self.assertRaises(StructureError):
                    StructurePattern(
                        pattern_id="pat-x",
                        kind="cluster",
                        payload={"a": 1},
                        metrics=metrics,
                        provenance={"origin": "test"},
                    )

    def test_empty_payload_or_provenance_is_rejected(self) -> None:
        """空载荷或空来源同样被拒绝。"""
        base = dict(
            pattern_id="pat-x",
            kind="cluster",
            payload={"a": 1},
            metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
            provenance={"origin": "test"},
        )
        with self.assertRaises(StructureError):
            StructurePattern(**{**base, "payload": {}})
        with self.assertRaises(StructureError):
            StructurePattern(**{**base, "provenance": {}})

    def test_unknown_kind_is_rejected(self) -> None:
        """未知模式种类被拒绝。"""
        with self.assertRaises(StructureError):
            StructurePattern(
                pattern_id="pat-x",
                kind="not-a-kind",
                payload={"a": 1},
                metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
                provenance={"origin": "test"},
            )

    def test_stability_out_of_range_is_rejected(self) -> None:
        """稳定性字段一旦给出，必须落在 [0, 1]。"""
        base = dict(
            pattern_id="pat-x",
            kind="cluster",
            payload={"a": 1},
            metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
            provenance={"origin": "test"},
        )
        StructurePattern(**base, stability=0.0)
        StructurePattern(**base, stability=1.0)
        with self.assertRaises(StructureError):
            StructurePattern(**base, stability=1.5)
        with self.assertRaises(StructureError):
            StructurePattern(**base, stability=-0.1)


# ---------------------------------------------------------------------------
# 错误约定：契约违规 vs 统计不适用
# ---------------------------------------------------------------------------


class TestErrorContract(unittest.TestCase):
    """契约层面的违规抛错，统计层面的不适用返回空结果。"""

    def test_non_sample_object_is_rejected(self) -> None:
        """传入非样本对象一律抛 ``StructureError``。"""
        for bogus in (None, [], {}, ("X1",), 3):
            with self.subTest(value=bogus):
                with self.assertRaises(StructureError):
                    find_clusters(bogus)
                with self.assertRaises(StructureError):
                    find_changepoints(bogus)
                with self.assertRaises(StructureError):
                    check_invariant(bogus, "X1")

    def test_non_exploration_purpose_is_policy_error(self) -> None:
        """样本用途不是 E 时抛 ``StructurePolicyError``。"""
        data = sample(two_group_records())
        tampered = ExplorationSample(
            variables=data.variables,
            target=data.target,
            rows=data.rows,
            purpose="V",
        )
        with self.assertRaises(StructurePolicyError):
            find_clusters(tampered)
        self.assertTrue(issubclass(StructurePolicyError, StructureError))

    def test_unknown_feature_is_rejected(self) -> None:
        """字段不在样本变量中时抛错，而不是静默跳过。"""
        data = sample(two_group_records())
        with self.assertRaises(StructureError):
            find_clusters(data, features=["X1", "不存在的字段"])

    def test_invalid_k_and_seed_are_rejected(self) -> None:
        """``k`` / ``seed`` 类型或取值非法时抛错。"""
        data = sample(two_group_records())
        with self.assertRaises(StructureError):
            cluster_labels(data, k=0)
        with self.assertRaises(StructureError):
            cluster_labels(data, k=2, seed="42")
        with self.assertRaises(StructureError):
            cluster_labels(data, k=2, seed=True)
        with self.assertRaises(StructureError):
            find_clusters(data, seed=1.5)
        with self.assertRaises(StructureError):
            cluster_labels(data, k=len(data.rows) + 1)

    def test_invalid_capacity_parameters_are_rejected(self) -> None:
        """容量类参数必须是正整数。"""
        data = sample(two_group_records())
        with self.assertRaises(StructureError):
            find_clusters(data, max_clusters=0)
        with self.assertRaises(StructureError):
            find_clusters(data, min_cluster_size=-1)
        with self.assertRaises(StructureError):
            find_changepoints(data, min_segment=0)
        with self.assertRaises(StructureError):
            find_changepoints(data, max_changepoints=0)

    def test_negative_tolerance_and_penalty_are_rejected(self) -> None:
        """容差与惩罚不得为负。"""
        data = sample(invariant_records())
        with self.assertRaises(StructureError):
            check_invariant(data, "X1", tolerance=-1e-9)
        with self.assertRaises(StructureError):
            find_changepoints(sample(step_records()), order_by="X1", penalty=-0.5)

    def test_invalid_coverage_floor_is_rejected(self) -> None:
        """覆盖率与一致度门槛必须落在 (0, 1]。"""
        data = sample(invariant_records())
        for bad in (0, 1.5, -0.1, "0.5"):
            with self.subTest(value=bad):
                with self.assertRaises(StructureError):
                    check_invariant(data, "X1", min_coverage=bad)
                with self.assertRaises(StructureError):
                    check_invariant(data, "X1", min_conformity=bad)

    def test_invalid_order_by_is_rejected(self) -> None:
        """``order_by`` 必须是存在的字段名或 ``None``。"""
        data = sample(step_records())
        for bad in ("", "  ", "不存在的字段"):
            with self.subTest(value=bad):
                with self.assertRaises(StructureError):
                    find_changepoints(data, order_by=bad)

    def test_unparsable_expression_is_rejected(self) -> None:
        """无法解析的表达式文本抛错。"""
        data = sample(invariant_records())
        with self.assertRaises(StructureError):
            check_invariant(data, "X1 +* 2")

    def test_expression_with_missing_variable_is_rejected(self) -> None:
        """表达式引用样本中不存在的变量时抛错。"""
        data = sample(invariant_records())
        with self.assertRaises(StructureError):
            check_invariant(data, "X9*2.0")

    def test_numeric_column_length_mismatch_is_rejected(self) -> None:
        """数值列长度与样本行数不一致时抛错。"""
        data = sample(invariant_records())
        with self.assertRaises(StructureError):
            check_invariant(data, [1.0, 2.0])
        with self.assertRaises(StructureError):
            find_changepoints(data, series=[1.0, 2.0, 3.0])

    def test_silhouette_matrix_mismatch_is_rejected(self) -> None:
        """矩阵行数与标签数不一致时抛错。"""
        with self.assertRaises(StructureError):
            silhouette_score([[0.0], [1.0]], [0])


# ---------------------------------------------------------------------------
# P05 边界守护
# ---------------------------------------------------------------------------


class TestStageBoundaries(unittest.TestCase):
    """守护 P05 的边界：不做 P06 的重采样、不做 P04 的拟合并评估。"""

    def test_no_resampling_api(self) -> None:
        """不存在重采样 / 稳定性评估公开接口（属 P06）。"""
        module = inspect.getmodule(find_clusters)
        for name in dir(module):
            with self.subTest(name=name):
                self.assertNotIn("resample", name.lower())
                self.assertNotIn("bootstrap", name.lower())
                self.assertNotIn("stability_score", name.lower())

    def test_no_fitting_api(self) -> None:
        """不存在关系拟合或基线对照公开接口（属 P04）。"""
        module = inspect.getmodule(find_clusters)
        exported = getattr(module, "__all__", [])
        for name in ("fit_relation", "baseline_constant", "baseline_linear"):
            self.assertNotIn(name, exported)

    def test_no_inference_vocabulary_in_module(self) -> None:
        """模块不产出 p 值 / 显著性 / 证据等级等推断性字段。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("p_value", "pvalue", "significance", "evidence_level", '"p值"'):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_stability_field_documented_as_p06(self) -> None:
        """``stability`` 字段的文档明确标注属 P06。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("属 P06", text)
        self.assertIn("P06", inspect.getdoc(StructurePattern) or "")

    def test_module_does_not_touch_other_modules(self) -> None:
        """模块的**代码**只导入 ``sdl_m02`` / ``sdl_m03``，不导入 ``sdl_m01`` 及后续模块。

        文档中声明「不修改 sdl_m01/」是允许且必要的，故此处检查导入语句而非全文。
        """
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                imported.append(node.module)
        roots = {name.split(".")[0] for name in imported}
        for forbidden in ("sdl_m01", "sdl_m04", "sdl_m05", "sdl_m06", "sdl_m07", "sdl_m08"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, roots)

    def test_module_declares_it_does_not_modify_m01(self) -> None:
        """模块文档显式声明不修改 ``sdl_m01/``（继承 ``INTERFACES.md`` §4 的禁止事项）。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("不修改", text)

    def test_pattern_kinds_cover_this_stage(self) -> None:
        """本阶段产出的三种模式都在契约种类内。"""
        for kind in ("cluster", "changepoint", "invariant"):
            self.assertIn(kind, PATTERN_KINDS)

    def test_no_skip_or_xfail_in_this_file(self) -> None:
        """本测试文件本身不得使用 skip / xfail 掩盖问题。

        断言字符串用拼接构造，避免该断言文本自身被扫描命中（自指）。
        """
        text = pathlib.Path(__file__).read_text(encoding="utf-8")
        for token in ("skip" + "If", "skip" + "Unless", "expected" + "Failure", "@unittest." + "skip"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_defaults_are_sane(self) -> None:
        """默认参数取值落在合理区间。"""
        self.assertGreaterEqual(DEFAULT_MAX_CLUSTERS, 2)
        self.assertGreaterEqual(DEFAULT_MIN_CLUSTER_SIZE, 2)
        self.assertGreaterEqual(DEFAULT_MIN_SEGMENT, 2)
        self.assertGreaterEqual(DEFAULT_MAX_CHANGEPOINTS, 1)
        self.assertGreaterEqual(DEFAULT_MIN_SILHOUETTE, 0.0)
        self.assertGreaterEqual(DEFAULT_PENALTY, 0.0)
        self.assertGreaterEqual(DEFAULT_TOLERANCE, 0.0)
        self.assertTrue(0.0 < DEFAULT_MIN_COVERAGE <= 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
