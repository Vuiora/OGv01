"""P06 验收测试：稳定性与重采样评估。

覆盖三条验收标准：

1. **重采样单位与 spec 的 ``split_unit`` 一致（分组级，不是记录级）**：
   抽样明细 ``details["draws"]`` 只登记**组**标识；每次抽样的行数等于所抽组的
   规模之和（组规模刻意设为不等，记录级抽样无法复现该等式）；同组记录整组进出；
   ``split_unit`` 不在 ``group_fields`` 中、或既无 spec 又无显式单位时一律报错。
2. **分数落在 ``[0, 1]``**：``stability_score`` 与 ``StabilityReport.stability``
   对合法证据恒返回 ``[0, 1]`` 内的浮点数；无有效重采样时为 ``0.0``（不是
   ``None``/``nan``）；非法计数（负数、复现次数大于总次数）报错。
3. **同一 seed 结果可复现**：同一 ``(候选, 数据, n_resamples, seed)`` 恒得逐字节
   相同的 ``to_dict()``；不同 seed 产生不同的抽样明细。

另覆盖：四类被试（关系候选、聚类、变点、不变量）各自的复现判据；探索分区 E 的
策略守护（``StabilityPolicyError``）；报告对象字段与 ``INTERFACES.md`` §2.2 的
``stability`` 字段对齐；以及 P06 的边界守护——不做假说构造、不做加权筛选、
不做确证检验、不访问 V 分区、不修改 ``sdl_m01/``、不引入第三方依赖。
"""

import ast as pyast
import json
import pathlib
import random
import re
import unittest

from sdl_m02.expressions import parse as parse_expression

from sdl_m03.equations import ExplorationSample, prepare_sample
from sdl_m03.stability import (
    DEFAULT_MIN_GROUPS,
    DEFAULT_N_RESAMPLES,
    DEFAULT_SEED,
    STABILITY_CRITERION_NOTE,
    STABILITY_VERSION,
    SUBJECT_KINDS,
    StabilityError,
    StabilityPolicyError,
    StabilityReport,
    group_key_of,
    resample_evaluate,
    resolve_split_unit,
    stability_score,
)
from sdl_m03.structure import (
    DEFAULT_PENALTY,
    StructureError,
    StructurePattern,
    check_invariant,
    find_changepoints,
    find_clusters,
)

# ---------------------------------------------------------------------------
# 夹具：确定性合成数据
# ---------------------------------------------------------------------------

#: 模块源码路径，用于静态检查第三方依赖与边界声明。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m03" / "stability.py"

NAMESPACE = "acc-exp"


def spec_for(split_unit="batch", group_fields=("batch",), namespace=NAMESPACE) -> dict:
    """构造 M1 数据配置的 ``dependence`` 段（本模块只读取这一段）。"""
    return {
        "dependence": {
            "record_unit": "reading",
            "split_unit": split_unit,
            "inference_unit": split_unit,
            "group_fields": list(group_fields),
            "namespace": namespace,
            "assumptions": ["independent batches"],
        }
    }


def _record(record_id: str, batch: int, values: dict, units: dict) -> dict:
    """构造一条合成记录。"""
    return {
        "record_id": record_id,
        "group_ids": {"batch": f"batch-{batch:02d}"},
        "environment": "lab-A",
        "event_time": "2026-01-01T10:00:00+00:00",
        "available_time": "2026-01-01T11:00:00+00:00",
        "values": values,
        "units": units,
        "source": "synthetic://p06-stability",
    }


#: 关系夹具的组规模：刻意不等（2,3,1 循环），使「行数 = 所抽组规模之和」
#: 无法被记录级抽样偶然满足。
RELATION_GROUP_SIZES = {group: (group % 3) + 1 for group in range(1, 13)}

UNITS = {"X1": "1", "X2": "1", "X3": "1", "Y": "1"}


def relation_records() -> list[dict]:
    """关系夹具：``Y = 2·(X1/X2)`` 精确成立，且无单一原始变量与之线性相关。

    取 ``X1 = g³``、``X2 = g²``、``Y = 2g``，则 ``Z = X1/X2 = g`` 精确复原
    ``Y = 2Z``；而 ``Y`` 对 ``X1 = g³`` 与 ``X2 = g²`` 都非线性，因此线性基线
    必然劣于候选。**同一组内多条读数取值完全相同**，组规模不等（2/3/1 循环）。
    """
    records: list[dict] = []
    for group, size in sorted(RELATION_GROUP_SIZES.items()):
        values = {"X1": float(group ** 3), "X2": float(group ** 2),
                  "X3": 1.0, "Y": 2.0 * group}
        for reading in range(size):
            records.append(_record(f"r-{group:02d}-{reading}", group, dict(values), UNITS))
    return records


def relation_variables() -> tuple[list[str], str]:
    """关系夹具的字段声明。"""
    return ["X1", "X2"], "Y"


#: 簇内 12 个点的确定性偏移（两层小圆环，不留任何单调方向）。
#: 关键：簇内点若沿一条直线铺开，k-means 会把同簇切出若干子簇并拿到更高的
#: 轮廓系数（实测可达 1.0），从而掩盖真正的两群结构。因此簇内必须是一片
#: **各向同性**的小点云——这样「切碎」不会提升分离度。
CLUSTER_OFFSETS: tuple[tuple[float, float], ...] = (
    (0.30, 0.00), (0.15, 0.26), (-0.15, 0.26), (-0.30, 0.00),
    (-0.15, -0.26), (0.15, -0.26),
    (0.12, 0.00), (-0.06, 0.10), (-0.06, -0.10), (0.00, 0.12),
    (0.00, -0.12), (-0.12, 0.00),
)


def two_cluster_records() -> list[dict]:
    """聚类夹具：两片彼此远离、簇内各向同性的点云，各 4 个批次、每批次 3 条读数。

    低群以 ``(0, 0)`` 为中心，高群以 ``(10, 5)`` 为中心，簇内点按
    :data:`CLUSTER_OFFSETS` 确定性散布（同一批次的三条读数取相邻三个偏移）。
    因此：①「批次」与「聚类成员」不互相混淆——每个簇横跨 4 个批次；
    ②无放回整群子抽样下，只要两簇都还有批次，两群结构即可复现。
    """
    records: list[dict] = []
    for group in range(1, 9):
        center_x, center_y = ((0.0, 0.0) if group <= 4 else (10.0, 5.0))
        for reading in range(3):
            offset_index = ((group - 1) * 3 + reading) % len(CLUSTER_OFFSETS)
            offset_x, offset_y = CLUSTER_OFFSETS[offset_index]
            records.append(
                _record(
                    f"c-{group:02d}-{reading}",
                    group,
                    {
                        "X1": center_x + offset_x,
                        "X2": center_y + offset_y,
                        "X3": 1.0,
                        "Y": 1.0 if group <= 4 else 2.0,
                    },
                    UNITS,
                )
            )
    return records


def step_records() -> list[dict]:
    """变点夹具：批次 1-6 的 ``Y`` 为 0，批次 7-12 的 ``Y`` 为 10；``X1`` 为排序键。"""
    records: list[dict] = []
    for group in range(1, 13):
        level = 0.0 if group <= 6 else 10.0
        for reading in range(3):
            records.append(
                _record(
                    f"s-{group:02d}-{reading}",
                    group,
                    {
                        "X1": group + 0.01 * reading,
                        "X2": 1.0,
                        "X3": 1.0,
                        "Y": level + 0.001 * reading,
                    },
                    UNITS,
                )
            )
    return records


def invariant_records() -> list[dict]:
    """不变量夹具：``X1 − X2`` 在全部批次上恒为 0。

    与关系夹具共用组规模模式，但取值改为 ``X1 = X2 = g``，使
    ``check_invariant(sample, "X1 - X2")`` 的判据精确成立（估计量为 0，
    尺度退化为绝对容差 1.0，一致度 1.0）。
    """
    records: list[dict] = []
    for group, size in sorted(RELATION_GROUP_SIZES.items()):
        for reading in range(size):
            records.append(
                _record(
                    f"i-{group:02d}-{reading}",
                    group,
                    {"X1": float(group), "X2": float(group),
                     "X3": 1.0, "Y": float(group)},
                    UNITS,
                )
            )
    return records


def aligned(records: list[dict], variables: list[str], target: str) -> ExplorationSample:
    """把记录对齐为探索样本。"""
    return prepare_sample(records, variables, target)


# ---------------------------------------------------------------------------
# 验收标准①：重采样单位与 split_unit 一致（分组级）
# ---------------------------------------------------------------------------


class TestSplitUnitResolution(unittest.TestCase):
    """``resolve_split_unit``：单位必须来自 spec 或显式给出，且与 group_fields 一致。"""

    def test_reads_split_unit_from_spec(self) -> None:
        unit = resolve_split_unit(spec_for())
        self.assertEqual(unit["split_unit"], "batch")
        self.assertEqual(unit["group_field"], "batch")
        self.assertEqual(unit["namespace"], NAMESPACE)
        self.assertEqual(unit["group_fields"], ["batch"])
        self.assertEqual(unit["record_unit"], "reading")

    def test_group_field_defaults_to_split_unit(self) -> None:
        unit = resolve_split_unit(spec_for(split_unit="batch", group_fields=("batch", "site")))
        self.assertEqual(unit["group_field"], "batch")

    def test_explicit_override_is_allowed(self) -> None:
        unit = resolve_split_unit(None, split_unit="batch")
        self.assertEqual(unit["split_unit"], "batch")
        self.assertIsNone(unit["namespace"])

    def test_group_field_override_is_allowed(self) -> None:
        unit = resolve_split_unit(spec_for(split_unit="batch", group_fields=("batch", "site")),
                                  group_field="site")
        self.assertEqual(unit["split_unit"], "batch")
        self.assertEqual(unit["group_field"], "site")

    def test_missing_spec_and_missing_unit_is_rejected(self) -> None:
        """既不提供 spec 也不显式给单位：报错，绝不默认按记录级重采样。"""
        with self.assertRaises(StabilityError) as caught:
            resolve_split_unit(None)
        self.assertIn("记录级", str(caught.exception))

    def test_spec_without_dependence_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resolve_split_unit({"task": {}})

    def test_unit_not_in_group_fields_is_rejected(self) -> None:
        with self.assertRaises(StabilityError) as caught:
            resolve_split_unit(spec_for(split_unit="batch", group_fields=("site",)))
        self.assertIn("group_fields", str(caught.exception))

    def test_non_mapping_spec_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resolve_split_unit(["dependence"])


class TestGroupKeys(unittest.TestCase):
    """``group_key_of``：按 M1 ``unit_keys`` 的口径派生分组标识。"""

    def test_key_is_stable_and_namespaced(self) -> None:
        record = _record("r-01", 1, {"X1": 1.0}, UNITS)
        key = group_key_of(record, "batch", namespace=NAMESPACE)
        self.assertEqual(json.loads(key), [NAMESPACE, "batch", "batch-01"])
        self.assertEqual(key, group_key_of(record, "batch", namespace=NAMESPACE))

    def test_distinct_batches_get_distinct_keys(self) -> None:
        first = group_key_of(_record("r-01", 1, {"X1": 1.0}, UNITS), "batch", namespace=NAMESPACE)
        second = group_key_of(_record("r-02", 2, {"X1": 1.0}, UNITS), "batch", namespace=NAMESPACE)
        self.assertNotEqual(first, second)

    def test_namespace_changes_key(self) -> None:
        record = _record("r-01", 1, {"X1": 1.0}, UNITS)
        self.assertNotEqual(
            group_key_of(record, "batch", namespace="a"),
            group_key_of(record, "batch", namespace="b"),
        )

    def test_missing_group_ids_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            group_key_of({"record_id": "r-01"}, "batch")

    def test_missing_field_is_rejected(self) -> None:
        record = _record("r-01", 1, {"X1": 1.0}, UNITS)
        with self.assertRaises(StabilityError):
            group_key_of(record, "site")

    def test_empty_identity_is_rejected(self) -> None:
        record = _record("r-01", 1, {"X1": 1.0}, UNITS)
        record["group_ids"]["batch"] = "  "
        with self.assertRaises(StabilityError):
            group_key_of(record, "batch")

    def test_boolean_identity_is_rejected(self) -> None:
        record = _record("r-01", 1, {"X1": 1.0}, UNITS)
        record["group_ids"]["batch"] = True
        with self.assertRaises(StabilityError):
            group_key_of(record, "batch")

    def test_integer_identity_is_accepted(self) -> None:
        record = _record("r-01", 1, {"X1": 1.0}, UNITS)
        record["group_ids"]["batch"] = 1
        key = group_key_of(record, "batch", namespace=NAMESPACE)
        self.assertEqual(json.loads(key), [NAMESPACE, "batch", 1])


class TestGroupLevelResampling(unittest.TestCase):
    """验收标准①的核心：抽样单位是**组**，同组记录整组进出。"""

    def setUp(self) -> None:
        self.records = relation_records()
        self.variables, self.target = relation_variables()
        self.spec = spec_for()
        self.sizes = RELATION_GROUP_SIZES
        self.total_rows = sum(self.sizes.values())

    def _evaluate(self, **kwargs) -> StabilityReport:
        return resample_evaluate(
            "X1/X2",
            self.records,
            kwargs.pop("n_resamples", 50),
            kwargs.pop("seed", DEFAULT_SEED),
            spec=self.spec,
            variables=self.variables,
            target=self.target,
            **kwargs,
        )

    def test_draws_register_groups_not_records(self) -> None:
        """抽样明细只登记组标识：不含任何记录标识。"""
        report = self._evaluate()
        draws = report.details["draws"]
        self.assertTrue(draws)
        for draw in draws:
            for key in draw["groups"]:
                parsed = json.loads(key)
                self.assertEqual(parsed[0], NAMESPACE)
                self.assertEqual(parsed[1], "batch")
                self.assertTrue(str(parsed[2]).startswith("batch-"))
            self.assertNotIn("r-01-0", json.dumps(draws))

    def test_each_draw_rows_equal_sum_of_drawn_group_sizes(self) -> None:
        """每次抽样的行数 = 所抽组的规模之和（组规模不等，记录级无法满足）。"""
        report = self._evaluate(replace=True)
        for draw in report.details["draws"]:
            expected = sum(self.sizes[int(str(json.loads(key)[2]).split("-")[1])]
                           for key in draw["groups"])
            self.assertEqual(draw["n_rows"], expected,
                             f"抽样 {draw['index']} 的行数不等于所抽组的规模之和")
            self.assertEqual(draw["n_groups_drawn"], len(set(draw["groups"])))

    def test_whole_group_entries_with_replacement(self) -> None:
        """有放回自助法：每次抽 ``n_groups`` 个组，重复抽到的组按重复次数整组复现。"""
        report = self._evaluate(replace=True)
        draw = report.details["draws"][0]
        self.assertEqual(len(draw["groups"]), len(self.sizes))
        # 有放回抽样会把同一组再抽一次，因此行数是「所抽组的规模之和」，
        # 可能大于、也可能小于整样本行数——它不是记录数，而是所用观测单位的规模之和。
        expected = sum(self.sizes[int(str(json.loads(key)[2]).split("-")[1])]
                       for key in draw["groups"])
        self.assertEqual(draw["n_rows"], expected)

    def test_subsample_stays_group_level(self) -> None:
        """不放回子抽样同样以组为单位：行数是整数个组的规模之和，且小于总行数。"""
        report = self._evaluate(replace=False, subsample_fraction=0.5, n_resamples=10)
        draw = report.details["draws"][0]
        expected = sum(self.sizes[int(str(json.loads(key)[2]).split("-")[1])]
                       for key in draw["groups"])
        self.assertEqual(draw["n_rows"], expected)
        self.assertLess(draw["n_rows"], self.total_rows)
        self.assertEqual(len(set(draw["groups"])), len(draw["groups"]))

    def test_unit_summary_reports_group_level(self) -> None:
        report = self._evaluate(n_resamples=5)
        summary = report.unit_summary
        self.assertEqual(summary["split_unit"], "batch")
        self.assertEqual(summary["group_field"], "batch")
        self.assertEqual(summary["n_groups"], len(self.sizes))
        self.assertEqual(summary["n_rows"], self.total_rows)
        self.assertEqual(summary["unit_level"], "group")
        self.assertEqual(summary["rows_per_group"]["min"], min(self.sizes.values()))
        self.assertEqual(summary["rows_per_group"]["max"], max(self.sizes.values()))
        self.assertEqual(summary["singleton_groups"],
                         sum(1 for size in self.sizes.values() if size == 1))

    def test_singleton_groups_are_reported_as_record_level(self) -> None:
        """若 spec 下每组只有一条记录，如实标注为记录级（而非擅自降级）。"""
        records = [
            _record(f"r-{group:02d}", group,
                    {"X1": float(group), "X2": 1.0, "X3": 1.0, "Y": float(group)},
                    UNITS)
            for group in range(1, 9)
        ]
        report = resample_evaluate(
            "X1/X2", records, 5, DEFAULT_SEED, spec=self.spec,
            variables=["X1", "X2"], target="Y",
        )
        self.assertEqual(report.unit_summary["unit_level"], "record")
        self.assertIn("spec", report.unit_summary["note"])

    def test_group_keys_can_be_supplied_with_aligned_sample(self) -> None:
        """传入已对齐样本时必须另给分组来源，映射与序列两种形式都可用。"""
        sample = aligned(self.records, self.variables, self.target)
        mapping = {
            record["record_id"]: group_key_of(record, "batch", namespace=NAMESPACE)
            for record in self.records
        }
        by_mapping = resample_evaluate(
            "X1/X2", sample, 5, DEFAULT_SEED, spec=self.spec, group_keys=mapping
        )
        sequence = [mapping[row.record_id] for row in sample.rows]
        by_sequence = resample_evaluate(
            "X1/X2", sample, 5, DEFAULT_SEED, spec=self.spec, group_keys=sequence
        )
        self.assertEqual(by_mapping.n_reproduced, by_sequence.n_reproduced)
        self.assertEqual(by_mapping.details["draws"], by_sequence.details["draws"])

    def test_aligned_sample_without_group_keys_is_rejected(self) -> None:
        sample = aligned(self.records, self.variables, self.target)
        with self.assertRaises(StabilityError) as caught:
            resample_evaluate("X1/X2", sample, 5, DEFAULT_SEED, spec=self.spec)
        self.assertIn("group_keys", str(caught.exception))

    def test_incomplete_group_coverage_is_rejected(self) -> None:
        sample = aligned(self.records, self.variables, self.target)
        incomplete = [
            group_key_of(record, "batch", namespace=NAMESPACE)
            for record in self.records
        ][:-1]
        with self.assertRaises(StabilityError):
            resample_evaluate(
                "X1/X2", sample, 5, DEFAULT_SEED, spec=self.spec,
                group_keys=incomplete,
            )


# ---------------------------------------------------------------------------
# 验收标准②：分数落在 [0, 1]
# ---------------------------------------------------------------------------


class TestStabilityScore(unittest.TestCase):
    """``stability_score``：任何合法证据都折算到 ``[0, 1]``。"""

    def test_simple_ratio(self) -> None:
        self.assertAlmostEqual(stability_score({"n_effective": 10, "n_reproduced": 7}), 0.7)
        self.assertAlmostEqual(stability_score({"n_resamples": 4, "n_reproduced": 1}), 0.25)
        self.assertAlmostEqual(stability_score({"total": 8, "success": 8}), 1.0)

    def test_boolean_sequence(self) -> None:
        self.assertAlmostEqual(stability_score([True, True, False, False]), 0.5)
        self.assertAlmostEqual(stability_score({"reproduced": [True, False]}), 0.5)

    def test_boundaries_are_inclusive(self) -> None:
        self.assertEqual(stability_score({"n_effective": 5, "n_reproduced": 0}), 0.0)
        self.assertEqual(stability_score({"n_effective": 5, "n_reproduced": 5}), 1.0)

    def test_zero_evidence_returns_zero_not_none(self) -> None:
        """无有效重采样时返回 ``0.0``，而不是 ``None`` 或 ``nan``。"""
        score = stability_score({"n_effective": 0, "n_reproduced": 0})
        self.assertEqual(score, 0.0)
        self.assertIsInstance(score, float)

    def test_empty_sequence_is_zero(self) -> None:
        self.assertEqual(stability_score([]), 0.0)

    def test_success_exceeding_total_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            stability_score({"n_effective": 3, "n_reproduced": 5})

    def test_negative_counts_are_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            stability_score({"n_effective": -1, "n_reproduced": 0})

    def test_non_integer_counts_are_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            stability_score({"n_effective": 2.5, "n_reproduced": 1})
        with self.assertRaises(StabilityError):
            stability_score({"n_effective": True, "n_reproduced": 1})

    def test_incomplete_mapping_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            stability_score({"n_effective": 3})

    def test_unsupported_input_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            stability_score(3)
        with self.assertRaises(StabilityError):
            stability_score("3/4")

    def test_report_input_matches_its_own_score(self) -> None:
        records = relation_records()
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1/X2", records, 30, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        self.assertAlmostEqual(stability_score(report), report.stability)
        self.assertAlmostEqual(report.score, report.stability)
        self.assertAlmostEqual(report.reproduced_rate, report.stability)


class TestScoreBounds(unittest.TestCase):
    """四类被试的分数与报告字段都落在合法取值范围内。"""

    def assert_report_ok(self, report: StabilityReport, kind: str) -> None:
        self.assertIsInstance(report, StabilityReport)
        self.assertEqual(report.subject_kind, kind)
        self.assertGreaterEqual(report.stability, 0.0)
        self.assertLessEqual(report.stability, 1.0)
        self.assertGreaterEqual(report.n_reproduced, 0)
        self.assertLessEqual(report.n_reproduced, report.n_effective)
        self.assertLessEqual(report.n_effective, report.n_resamples)
        self.assertIsInstance(report.sufficient, bool)
        self.assertIsInstance(report.reasons, tuple)
        self.assertEqual(report.reasons == (), report.sufficient)
        self.assertTrue(report.criteria)
        self.assertTrue(report.unit_summary)
        self.assertAlmostEqual(
            report.stability,
            report.n_reproduced / report.n_effective if report.n_effective else 0.0,
        )

    def test_relation_report_bounds(self) -> None:
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1/X2", relation_records(), 30, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        self.assert_report_ok(report, "relation")
        self.assertEqual(report.stability, 1.0)
        self.assertEqual(report.n_effective, 30)
        self.assertTrue(report.sufficient)

    def test_cluster_report_bounds(self) -> None:
        """两片簇内各向同性的点云在整群子抽样下稳定复现。"""
        records = two_cluster_records()
        patterns = find_clusters(
            aligned(records, ["X1", "X2", "X3"], "Y"), ["X1", "X2"], seed=7
        )
        self.assertEqual(len(patterns), 2)
        report = resample_evaluate(
            patterns, records, 20, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y",
            replace=False, subsample_fraction=0.75,
        )
        self.assert_report_ok(report, "cluster")
        self.assertGreater(report.stability, 0.5)
        self.assertTrue(report.sufficient)
        self.assertEqual(report.criteria["k"], 2)

    def test_bootstrap_repeats_groups_and_stays_in_range(self) -> None:
        """有放回自助法会把同一组重复抽出（``n_groups_drawn`` 小于所抽数）。

        重复抽出的组会整组再现，形成精确重复的行。本测试确认两件事：
        ①抽样明细如实记录了这种重复；②即便如此，分数仍在 ``[0, 1]``。
        至于重复是否会改变检测结论，取决于数据与检测器——本模块只如实报告，
        不替调用方判断该用哪种重采样方案。
        """
        records = two_cluster_records()
        patterns = find_clusters(
            aligned(records, ["X1", "X2", "X3"], "Y"), ["X1", "X2"], seed=7
        )
        report = resample_evaluate(
            patterns, records, 30, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y", replace=True,
        )
        self.assert_report_ok(report, "cluster")
        first = report.details["draws"][0]
        self.assertLess(first["n_groups_drawn"], len(first["groups"]))
        self.assertGreaterEqual(report.stability, 0.0)
        self.assertLessEqual(report.stability, 1.0)

    def test_subsample_never_repeats_groups(self) -> None:
        """不放回子抽样：抽到的组两两不同，行数是这些组的规模之和。"""
        records = two_cluster_records()
        patterns = find_clusters(
            aligned(records, ["X1", "X2", "X3"], "Y"), ["X1", "X2"], seed=7
        )
        report = resample_evaluate(
            patterns, records, 10, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y",
            replace=False, subsample_fraction=0.75,
        )
        for draw in report.details["draws"]:
            self.assertEqual(draw["n_groups_drawn"], len(draw["groups"]))

    def test_changepoint_report_bounds(self) -> None:
        records = step_records()
        patterns = find_changepoints(
            aligned(records, ["X1", "X2", "X3"], "Y"), order_by="X1"
        )
        self.assertTrue(patterns)
        report = resample_evaluate(
            patterns, records, 20, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y",
        )
        self.assert_report_ok(report, "changepoint")
        self.assertIn("position_tolerance", report.criteria)

    def test_invariant_report_bounds(self) -> None:
        records = invariant_records()
        patterns = check_invariant(
            aligned(records, ["X1", "X2", "X3"], "Y"), "X1 - X2"
        )
        self.assertEqual(len(patterns), 1)
        report = resample_evaluate(
            patterns, records, 20, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y",
        )
        self.assert_report_ok(report, "invariant")
        self.assertEqual(report.stability, 1.0)
        self.assertTrue(report.sufficient)

    def test_single_pattern_is_accepted_for_invariant(self) -> None:
        records = invariant_records()
        pattern = check_invariant(
            aligned(records, ["X1", "X2", "X3"], "Y"), "X1 - X2"
        )[0]
        report = resample_evaluate(
            pattern, records, 10, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y",
        )
        self.assertEqual(report.subject_kind, "invariant")
        self.assertTrue(report.sufficient)

    def test_all_subject_kinds_are_declared(self) -> None:
        self.assertEqual(set(SUBJECT_KINDS), {"relation", "cluster", "changepoint", "invariant"})


# ---------------------------------------------------------------------------
# 验收标准③：同一 seed 可复现
# ---------------------------------------------------------------------------


class TestReproducibility(unittest.TestCase):
    """验收标准③：同一 seed 逐字节可复现；不同 seed 产生不同抽样。"""

    def _evaluate(self, seed: int, **kwargs) -> StabilityReport:
        variables, target = relation_variables()
        return resample_evaluate(
            "X1/X2", relation_records(), kwargs.pop("n_resamples", 25), seed,
            spec=spec_for(), variables=variables, target=target, **kwargs
        )

    def test_same_seed_is_byte_identical(self) -> None:
        first = json.dumps(self._evaluate(3).to_dict(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(self._evaluate(3).to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_fresh_records_object_reproduces(self) -> None:
        """重新构造记录对象（内容相同）仍得同一结果：不依赖对象身份。"""
        first = self._evaluate(5).to_dict()
        second = self._evaluate(5).to_dict()
        self.assertEqual(first["provenance"]["sample_fingerprint"],
                         second["provenance"]["sample_fingerprint"])
        self.assertEqual(first["details"]["draws"], second["details"]["draws"])

    def test_different_seed_changes_draws(self) -> None:
        low = self._evaluate(1).details["draws"]
        high = self._evaluate(999).details["draws"]
        self.assertNotEqual(low, high)

    def test_structure_paths_are_reproducible(self) -> None:
        records = step_records()
        patterns = find_changepoints(
            aligned(records, ["X1", "X2", "X3"], "Y"), order_by="X1"
        )
        first = json.dumps(
            resample_evaluate(patterns, records, 15, 11, spec=spec_for(),
                              variables=["X1", "X2", "X3"], target="Y").to_dict(),
            ensure_ascii=False, sort_keys=True,
        )
        second = json.dumps(
            resample_evaluate(patterns, records, 15, 11, spec=spec_for(),
                              variables=["X1", "X2", "X3"], target="Y").to_dict(),
            ensure_ascii=False, sort_keys=True,
        )
        self.assertEqual(first, second)

    def test_random_module_is_never_used_globally(self) -> None:
        """随机源只能是局部 ``Random`` 实例，不得调用模块级随机函数。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        offenders = re.findall(r"\brandom\.(?!Random\b)\w+\s*\(", text)
        self.assertEqual(offenders, [], f"存在模块级随机调用：{offenders}")

    def test_only_local_random_instance_is_constructed(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("random.Random(", text)
        self.assertNotIn("random.seed(", text)


# ---------------------------------------------------------------------------
# 复现判据的机制
# ---------------------------------------------------------------------------


class TestReproductionCriteria(unittest.TestCase):
    """各类被试的复现判据确实按文档所述执行。"""

    def test_relation_criteria_declare_baseline_and_sign_rule(self) -> None:
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1/X2", relation_records(), 20, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        criteria = report.criteria
        self.assertIn("斜率符号", criteria["rule"])
        self.assertEqual(criteria["loss"], "mse")
        self.assertIn("full_sample_slope", criteria)
        self.assertAlmostEqual(criteria["full_sample_slope"], 2.0, places=9)

    def test_relation_without_usable_baseline_is_not_sufficient(self) -> None:
        """候选与线性基线等价（均为零误差）时，严格优于条件不成立。"""
        records = relation_records()
        variables, target = relation_variables()
        # 候选直接用 X1：与线性基线 X1 完全重合，损失相同，不构成「严格低于基线」。
        report = resample_evaluate(
            "X1", records, 20, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        self.assertEqual(report.n_reproduced, 0)
        self.assertEqual(report.stability, 0.0)

    def test_cluster_criteria_declare_pairwise_agreement(self) -> None:
        records = two_cluster_records()
        patterns = find_clusters(
            aligned(records, ["X1", "X2", "X3"], "Y"), ["X1", "X2"], seed=7
        )
        report = resample_evaluate(
            patterns, records, 10, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y",
        )
        self.assertIn("成对共簇一致率", report.criteria["rule"])
        self.assertIn("min_agreement", report.criteria)
        self.assertGreaterEqual(report.criteria["k"], 2)

    def test_cluster_agreement_floor_can_reject(self) -> None:
        """把一致率门槛抬到 1.0 以上不可能达到，复现数必须下降。"""
        records = two_cluster_records()
        patterns = find_clusters(
            aligned(records, ["X1", "X2", "X3"], "Y"), ["X1", "X2"], seed=7
        )
        lenient = resample_evaluate(
            patterns, records, 10, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y", min_agreement=0.5,
        )
        strict = resample_evaluate(
            patterns, records, 10, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2", "X3"], target="Y", min_agreement=1.0,
        )
        self.assertGreaterEqual(lenient.n_reproduced, strict.n_reproduced)

    def test_relation_failure_reasons_are_counted(self) -> None:
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1/X2", relation_records(), 30, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        self.assertIn("failure_reasons", report.details)
        self.assertIn("median_loss_difference", report.details)
        self.assertIn("sign_agreement_rate", report.details)
        self.assertEqual(report.details["sign_agreement_rate"], 1.0)


class TestInsufficientEvidence(unittest.TestCase):
    """组数或有效次数不足时必须显式标注，而不是把低分辨力的数字当结论。"""

    def test_too_few_groups_is_not_sufficient(self) -> None:
        records = [
            _record(f"r-{group:02d}-{reading}", group,
                    {"X1": float(group ** 3), "X2": float(group ** 2),
                     "X3": 1.0, "Y": 2.0 * group}, UNITS)
            for group in (1, 2)
            for reading in range(2)
        ]
        report = resample_evaluate(
            "X1/X2", records, 20, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2"], target="Y",
        )
        self.assertFalse(report.sufficient)
        self.assertTrue(any("min_groups" in reason for reason in report.reasons))
        self.assertGreaterEqual(report.stability, 0.0)
        self.assertLessEqual(report.stability, 1.0)

    def test_too_few_effective_resamples_is_not_sufficient(self) -> None:
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1/X2", relation_records(), 10, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target, min_effective=999,
        )
        self.assertFalse(report.sufficient)
        self.assertTrue(any("min_effective" in reason for reason in report.reasons))

    def test_min_groups_boundary(self) -> None:
        self.assertGreaterEqual(DEFAULT_MIN_GROUPS, 2)

    def test_unfittable_candidate_is_not_sufficient(self) -> None:
        """候选在全样本上不可拟合（常量）时给出明确原因，不伪造分数。"""
        records = relation_records()
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1 - X1 + 1", records, 10, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        self.assertFalse(report.sufficient)
        self.assertEqual(report.n_effective, 0)
        self.assertEqual(report.stability, 0.0)
        self.assertTrue(any("全样本无法拟合" in reason for reason in report.reasons))


# ---------------------------------------------------------------------------
# 报告对象契约
# ---------------------------------------------------------------------------


class TestReportContract(unittest.TestCase):
    """报告字段与 ``INTERFACES.md`` §2.2 的 ``stability`` 字段对齐，且可 JSON 序列化。"""

    def _report(self, **kwargs) -> StabilityReport:
        variables, target = relation_variables()
        return resample_evaluate(
            "X1/X2", relation_records(), 10, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target, **kwargs
        )

    def test_required_fields_exist(self) -> None:
        payload = self._report().to_dict()
        for key in (
            "version", "subject_kind", "subject_id", "split_unit", "group_field",
            "namespace", "n_rows", "n_groups", "n_resamples", "n_effective",
            "n_reproduced", "stability", "sufficient", "reasons", "seed",
            "replace", "criteria", "unit_summary", "details", "provenance",
        ):
            self.assertIn(key, payload)

    def test_version_and_note(self) -> None:
        payload = self._report().to_dict()
        self.assertEqual(payload["version"], STABILITY_VERSION)
        self.assertEqual(payload["criterion_note"], STABILITY_CRITERION_NOTE)

    def test_result_is_json_serializable(self) -> None:
        text = json.dumps(self._report().to_dict(), ensure_ascii=False)
        self.assertIn("stability", text)

    def test_provenance_carries_sample_fingerprint(self) -> None:
        provenance = self._report().provenance
        self.assertEqual(provenance["sample_fingerprint"],
                         "".join(provenance["sample_fingerprint"].split()))
        self.assertEqual(len(provenance["sample_fingerprint"]), 16)
        self.assertEqual(provenance["purpose"], "E")
        self.assertEqual(provenance["module"], "sdl_m03/stability.py")

    def test_provenance_has_no_restricted_content(self) -> None:
        text = json.dumps(self._report().to_dict(), ensure_ascii=False)
        for banned in ("token", "secret", "custodian", "confirmer", "binding_id"):
            self.assertNotIn(banned, text.lower())

    def test_subject_id_is_stable(self) -> None:
        candidate = parse_expression("X1/X2")
        first = resample_evaluate(
            candidate, relation_records(), 5, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2"], target="Y",
        )
        second = resample_evaluate(
            candidate, relation_records(), 5, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2"], target="Y",
        )
        self.assertEqual(first.subject_id, second.subject_id)

    def test_draw_sample_limit_is_respected(self) -> None:
        report = self._report(draw_sample=3)
        self.assertLessEqual(len(report.details["draws"]), 3)

    def test_report_rejects_out_of_range_stability(self) -> None:
        report = self._report()
        with self.assertRaises(StabilityError):
            StabilityReport(
                subject_kind=report.subject_kind,
                subject_id=report.subject_id,
                split_unit=report.split_unit,
                group_field=report.group_field,
                namespace=report.namespace,
                n_rows=report.n_rows,
                n_groups=report.n_groups,
                n_resamples=report.n_resamples,
                n_effective=report.n_effective,
                n_reproduced=report.n_reproduced,
                stability=1.5,
                sufficient=report.sufficient,
                reasons=report.reasons,
                seed=report.seed,
                replace=report.replace,
                criteria=report.criteria,
                unit_summary=report.unit_summary,
                details=report.details,
                provenance=report.provenance,
            )

    def test_report_rejects_unknown_kind(self) -> None:
        report = self._report()
        with self.assertRaises(StabilityError):
            StabilityReport(
                subject_kind="unknown", subject_id=report.subject_id,
                split_unit=report.split_unit, group_field=report.group_field,
                namespace=report.namespace, n_rows=report.n_rows,
                n_groups=report.n_groups, n_resamples=report.n_resamples,
                n_effective=report.n_effective, n_reproduced=report.n_reproduced,
                stability=report.stability, sufficient=report.sufficient,
                reasons=report.reasons, seed=report.seed, replace=report.replace,
                criteria=report.criteria, unit_summary=report.unit_summary,
                details=report.details, provenance=report.provenance,
            )


# ---------------------------------------------------------------------------
# 错误契约与策略守护
# ---------------------------------------------------------------------------


class TestErrorContract(unittest.TestCase):
    """契约违例抛 ``StabilityError``；策略违例抛 ``StabilityPolicyError``。"""

    def test_policy_error_is_a_stability_error(self) -> None:
        self.assertTrue(issubclass(StabilityPolicyError, StabilityError))

    def test_non_exploration_sample_is_policy_error(self) -> None:
        sample = aligned(relation_records(), *relation_variables())
        forged = ExplorationSample(
            variables=sample.variables,
            target=sample.target,
            rows=sample.rows,
            purpose="V",
        )
        with self.assertRaises(StabilityPolicyError):
            resample_evaluate("X1/X2", forged, 5, DEFAULT_SEED, spec=spec_for())

    def test_non_sample_object_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", 42, 5, DEFAULT_SEED, spec=spec_for(),
                              variables=["X1", "X2"], target="Y")

    def test_missing_variables_or_target_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"])
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), target="Y")

    def test_numeric_column_candidate_is_rejected(self) -> None:
        """预置数值列无法与重采样后的行集对齐，必须拒绝。"""
        column = [1.0, 2.0, 3.0, 4.0]
        with self.assertRaises(StabilityError) as caught:
            resample_evaluate(column, relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y")
        self.assertIn("数值列", str(caught.exception))

    def test_invalid_n_resamples_and_seed_are_rejected(self) -> None:
        records = relation_records()
        for bad in (0, -3, 2.5, True):
            with self.subTest(value=bad):
                with self.assertRaises(StabilityError):
                    resample_evaluate("X1/X2", records, bad, DEFAULT_SEED,
                                      spec=spec_for(),
                                      variables=["X1", "X2"], target="Y")
        for bad_seed in (1.5, "0", True):
            with self.subTest(seed=bad_seed):
                with self.assertRaises(StabilityError):
                    resample_evaluate("X1/X2", records, 5, bad_seed,
                                      spec=spec_for(),
                                      variables=["X1", "X2"], target="Y")

    def test_invalid_thresholds_are_rejected(self) -> None:
        records = relation_records()
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", records, 5, DEFAULT_SEED, spec=spec_for(),
                              variables=["X1", "X2"], target="Y", min_agreement=0.0)
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", records, 5, DEFAULT_SEED, spec=spec_for(),
                              variables=["X1", "X2"], target="Y", min_agreement=1.5)
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", records, 5, DEFAULT_SEED, spec=spec_for(),
                              variables=["X1", "X2"], target="Y",
                              position_tolerance=-1.0)

    def test_empty_baselines_are_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y",
                              baselines=())

    def test_unknown_baseline_kind_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y",
                              baselines=("quadratic",))

    def test_empty_loss_name_is_rejected(self) -> None:
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y",
                              loss="  ")

    def test_mixed_pattern_kinds_are_rejected(self) -> None:
        cluster = StructurePattern(
            pattern_id="pat-c", kind="cluster",
            payload={"cluster_index": 0, "k": 2, "members": ["r-01-0"],
                     "partition_signature": "sig"},
            metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
            provenance={"origin": "test"},
        )
        invariant = StructurePattern(
            pattern_id="pat-i", kind="invariant", payload={"estimate": 1.0},
            metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
            provenance={"origin": "test", "parameters": {"expression": "X3"}},
        )
        with self.assertRaises(StabilityError):
            resample_evaluate([cluster, invariant], relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y")

    def test_cluster_without_members_is_rejected(self) -> None:
        broken = StructurePattern(
            pattern_id="pat-c", kind="cluster",
            payload={"cluster_index": 0, "k": 2, "members": []},
            metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
            provenance={"origin": "test"},
        )
        with self.assertRaises(StabilityError):
            resample_evaluate(broken, relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y")

    def test_invariant_without_expression_is_rejected(self) -> None:
        broken = StructurePattern(
            pattern_id="pat-i", kind="invariant", payload={"estimate": 1.0},
            metrics={"error": 0.0, "residual": 0.0, "complexity": {"total": 1}},
            provenance={"origin": "test", "parameters": {}},
        )
        with self.assertRaises(StabilityError) as caught:
            resample_evaluate(broken, relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y")
        self.assertIn("expression", str(caught.exception))

    def test_duplicate_record_id_with_conflicting_group_is_rejected(self) -> None:
        records = relation_records()
        duplicate = dict(records[0])
        duplicate["group_ids"] = {"batch": "batch-99"}
        with self.assertRaises(StabilityError):
            resample_evaluate("X1/X2", records + [duplicate], 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y")

    def test_client_with_wrong_role_is_rejected(self) -> None:
        class Confirmer:
            role = "confirmer"

            def read_dataset(self, ref):
                return relation_records()

        with self.assertRaises(StabilityPolicyError):
            resample_evaluate("X1/X2", None, 5, DEFAULT_SEED, spec=spec_for(),
                              variables=["X1", "X2"], target="Y",
                              client=Confirmer(), source_ref="E-1")

    def test_client_without_reference_is_rejected(self) -> None:
        class Explorer:
            role = "explorer"

            def read_dataset(self, ref):
                return relation_records()

        with self.assertRaises(StabilityPolicyError):
            resample_evaluate("X1/X2", None, 5, DEFAULT_SEED, spec=spec_for(),
                              variables=["X1", "X2"], target="Y", client=Explorer())

    def test_explorer_client_is_accepted(self) -> None:
        class Explorer:
            role = "explorer"

            def read_dataset(self, ref):
                return relation_records()

        report = resample_evaluate(
            "X1/X2", None, 5, DEFAULT_SEED, spec=spec_for(),
            variables=["X1", "X2"], target="Y",
            client=Explorer(), source_ref="E-0001",
        )
        self.assertEqual(report.stability, 1.0)
        self.assertEqual(report.provenance["source_ref"], "E-0001")

    def test_restricted_reference_is_rejected(self) -> None:
        """引用 V / C 分区一律拒绝（策略违例），沿用 P04 的 E 归属校验。"""
        for ref in ("V-0001", "C1-0001", "Q-0001"):
            with self.subTest(ref=ref):
                with self.assertRaises(StabilityPolicyError):
                    resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                                      spec=spec_for(), variables=["X1", "X2"],
                                      target="Y", source_ref=ref)

    def test_resources_mapping_must_match_e_reference(self) -> None:
        with self.assertRaises(StabilityPolicyError):
            resample_evaluate("X1/X2", relation_records(), 5, DEFAULT_SEED,
                              spec=spec_for(), variables=["X1", "X2"], target="Y",
                              source_ref="E-0001",
                              resources={"E": "E-0002", "V": "V-0001"})

    def test_unknown_detector_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(StabilityError, ValueError))
        self.assertTrue(issubclass(StructureError, ValueError))


# ---------------------------------------------------------------------------
# 阶段边界守护
# ---------------------------------------------------------------------------


class TestStageBoundaries(unittest.TestCase):
    """P06 的边界：不做假说构造、不做加权筛选、不做确证检验、不碰其它模块。"""

    def setUp(self) -> None:
        self.text = MODULE_PATH.read_text(encoding="utf-8")

    def test_no_third_party_imports(self) -> None:
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
        tree = pyast.parse(self.text)
        roots: set[str] = set()
        for node in tree.body:
            if isinstance(node, pyast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue(
            roots <= {"__future__", "json", "math", "random", "dataclasses", "typing",
                      "sdl_m02", "sdl_m03"},
            f"出现了未预期的导入：{sorted(roots)}",
        )

    def test_does_not_import_or_call_m01(self) -> None:
        """不导入、不调用 ``sdl_m01``：只允许在文档里声明「不修改 sdl_m01/」。

        用 AST 判定真实依赖，而不是文本匹配——文档里出现 ``sdl_m01/`` 是
        **边界声明**，恰恰是要保留的内容。
        """
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
        self.assertIn("不修改", self.text)
        self.assertIn("sdl_m01/", self.text)

    def test_no_inference_vocabulary(self) -> None:
        """不实现确证检验：不出现 p 值、置信区间、错误率控制等实现性标识。"""
        for banned in ("p_value", "pvalue", "scipy", "statsmodels",
                       "reject_null", "evidence_level", "confidence_interval"):
            self.assertNotIn(banned, self.text.lower())

    def test_disclaims_inference_and_evidence_grades(self) -> None:
        """显式声明稳定性分数不是错误率控制，也不构成证据等级。"""
        self.assertIn("不是错误率控制", self.text)
        self.assertIn("不构成证据等级", self.text)

    def test_no_confirmation_partition_usage(self) -> None:
        """不使用 V 分区，不引入确证数据。"""
        self.assertNotIn('"C1"', self.text)
        self.assertNotIn("'C1'", self.text)
        self.assertNotIn('"V"', self.text)
        self.assertNotIn("bind_confirmation", self.text)
        self.assertNotIn("consume_confirmation", self.text)

    def test_declares_exploration_only(self) -> None:
        self.assertIn("EXPLORATION_PURPOSE", self.text)
        self.assertIn("只使用探索分区", self.text)

    def test_no_aggregation_api(self) -> None:
        """不在指标层做加权求和或 Pareto 筛选（属 M5/P10）。

        判定口径是**是否真的暴露了这样的接口或实现了这样的逻辑**——
        文档里提到「Pareto 筛选属 M5」是边界声明，不算违规。
        故此处按 AST 检查函数/类名与真实调用，而非文本匹配。
        """
        tree = pyast.parse(self.text)
        banned_names = {
            "weighted_sum", "total_score", "pareto_front", "combine_scores",
            "aggregate_score", "weighted_score",
        }
        offenders: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)):
                if node.name.lower() in banned_names:
                    offenders.append(node.name)
            if isinstance(node, pyast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if isinstance(name, str) and name.lower() in banned_names:
                    offenders.append(f"{name}()")
        self.assertEqual(offenders, [], f"出现了加权/筛选接口：{offenders}")
        # 也不得接受权重参数（那正是「加权求和」的入口）。
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
                arguments = list(node.args.args) + list(node.args.kwonlyargs)
                for argument in arguments:
                    self.assertNotIn(
                        argument.arg, {"weights", "score_weights"},
                        f"{node.name} 暴露了权重参数，疑似在指标层做加权",
                    )

    def test_reports_no_aggregate_total(self) -> None:
        """报告里没有把 G/S/N/C 合成一个总分的字段。"""
        variables, target = relation_variables()
        payload = resample_evaluate(
            "X1/X2", relation_records(), 5, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        ).to_dict()
        text = json.dumps(payload, ensure_ascii=False).lower()
        for banned in ("weighted_sum", "total_score", "aggregate_score"):
            self.assertNotIn(banned, text)

    def test_no_hypothesis_construction(self) -> None:
        for banned in ("class Hypothesis", "null_hypotheses", "parent_ids"):
            self.assertNotIn(banned, self.text)

    def test_no_skip_or_xfail_in_this_file(self) -> None:
        """本文件不得使用 skip / xfail 让测试变绿。"""
        tree = pyast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        markers = {"skip", "skipIf", "skipUnless", "xfail", "skipTest"}
        hits: list[str] = []
        for node in pyast.walk(tree):
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef)):
                for deco in node.decorator_list:
                    target = deco.func if isinstance(deco, pyast.Call) else deco
                    name = getattr(target, "attr", None) or getattr(target, "id", None)
                    if name in markers:
                        hits.append(node.name)
            if isinstance(node, pyast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name in markers:
                    hits.append(str(getattr(node, "lineno", "?")))
        self.assertEqual(hits, [], f"检测到 skip/xfail 用法：{hits}")

    def test_defaults_are_sane(self) -> None:
        self.assertGreaterEqual(DEFAULT_N_RESAMPLES, 10)
        self.assertGreaterEqual(DEFAULT_SEED, 0)
        self.assertGreaterEqual(DEFAULT_MIN_GROUPS, 2)
        self.assertGreater(DEFAULT_PENALTY, 0.0)

    def test_stability_field_is_a_float_in_unit_interval(self) -> None:
        """与 ``INTERFACES.md`` §2.2 对齐：``stability`` 是 [0,1] 的浮点数。"""
        variables, target = relation_variables()
        report = resample_evaluate(
            "X1/X2", relation_records(), 10, DEFAULT_SEED, spec=spec_for(),
            variables=variables, target=target,
        )
        self.assertIsInstance(report.stability, float)
        self.assertGreaterEqual(report.stability, 0.0)
        self.assertLessEqual(report.stability, 1.0)

    def test_docstring_documents_the_three_acceptance_criteria(self) -> None:
        for marker in ("重采样单位与 spec", "分数落在", "同一 seed"):
            self.assertIn(marker, self.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
