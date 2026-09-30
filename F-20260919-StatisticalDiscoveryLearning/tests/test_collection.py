"""P17 验收测试：自主取数执行器（M9）。

覆盖四条验收标准：

1. **建议 → 采样真正闭环。** :class:`AutonomousCollector` 同时充当主循环的
   sink（收 M7 计划）与 supplier（按*上一轮*计划取数）；取数决策溯源
   （``trace()``）必须能证明「本轮采的观测 = 上一轮 M7 建议的观测」。
2. **不替 M7 重排、不替 M7 命名。** :func:`plan_to_requests` 严格按计划
   现有顺序遍历，采样请求的 ``rank`` / ``observation_rank`` 均为原样转录；
   结果对象恒输出 ``ranking_basis == "heuristic"`` 与 ``ranking_transcribed``。
3. **「没采」与「采了为空」必须可区分。** 六类原因码分列；``no_mapping`` /
   ``no_plan`` / ``budget_exhausted`` / ``source_failed`` 属**没采**，
   ``source_empty`` 属**采了为空**（设计齐备、确实驱动了数据源、返回空）。
   绝不用 0 条记录冒充「取过了」。
4. **取数不授予任何证据等级、不执行统计检验。** 常量恒为 ``False``；
   ``grade`` / ``p_value`` 一类入参在调用点被拒绝；模块不定义任何数据访问
   或统计入口（AST 复核，见 :func:`sdl_m09.collection.self_check`）。

另覆盖：采样目录解析顺序、预算与批次上限、溯源字段完整性、数据源失败降级、
冻结性（``MappingProxyType``）、确定性可复现、与真实 M1 金库及 P16 主循环的
集成，以及 P17 的横向约束——模块层不导入 ``sdl_m01``、不引入第三方依赖、
不使用 skip / xfail、无省略号占位。
"""

import ast as pyast
import inspect
import json
import pathlib
import tempfile
import unittest

from sdl_m01 import Module01, initialize

from sdl_m07.acquisition import (
    ACQUISITION_PLAN_EXECUTES_COLLECTION,
    RANKING_BASIS_HEURISTIC,
)

from sdl_pipeline.loop import LoopBudget, synthetic_records, synthetic_spec

from sdl_m09.collection import (
    CATALOGUE_REQUIRED_NOTE,
    COLLECTION_BOUNDARY_NOTE,
    COLLECTION_EXECUTES_COLLECTION,
    COLLECTION_GRANTS_EVIDENCE_GRADE,
    COLLECTION_REASONS,
    COLLECTION_RUNS_STATISTICS,
    COLLECTION_VERSION,
    EMPTY_VS_ABSENT_NOTE,
    FORBIDDEN_ACCESS_NAMES,
    NOT_COLLECTED_REASONS,
    NOT_PROVIDED_BY_P17,
    REASON_BUDGET_EXHAUSTED,
    REASON_COLLECTED,
    REASON_NO_MAPPING,
    REASON_NO_PLAN,
    REASON_SOURCE_EMPTY,
    REASON_SOURCE_FAILED,
    SYNTHETIC_SOURCE_NOTICE,
    AutonomousCollector,
    CollectedBatch,
    CollectionBudget,
    CollectionError,
    CollectionInputError,
    CollectionOutcome,
    CollectionPolicyError,
    RegularityReport,
    SamplingCatalogue,
    SamplingRequest,
    SyntheticSource,
    collect,
    extract_discovered_regularities,
    plan_to_requests,
    run_autonomous_discovery,
    self_check,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

#: 模块源码路径，用于静态检查边界声明与依赖。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m09" / "collection.py"

#: 测试文件自身路径，用于检查本文件未使用 skip / xfail。
TEST_PATH = pathlib.Path(__file__).resolve()

#: 一份够用的兜底采样设计（2 批 × 2 读数）。
DESIGN = {"environment": "lab-A", "batches": 2, "readings": 2}


def make_plan(*observation_ids, purpose="exploration", heuristic=True):
    """构造一份字典形态的 M7 取证计划（字段名与 ``AcquisitionPlan.to_dict`` 对齐）。"""
    items = []
    for rank, observation_id in enumerate(observation_ids):
        item = {
            "observation_id": observation_id,
            "rank": rank,
            "near_boundary": rank == 0,
            "rationale": f"M7 依据-{observation_id}",
            "ranking_basis": RANKING_BASIS_HEURISTIC,
        }
        if heuristic:
            # 排序值随 rank 递减，模拟「越靠前越值得看」。
            item["heuristic_value"] = float(10 - rank)
        items.append(item)
    return {"purpose": purpose, "items": items, "count": len(items)}


def make_request(observation_id="obs-0000", *, rank=0, design=None, **overrides):
    """构造一条采样请求。"""
    body = dict(
        observation_id=observation_id,
        rank=rank,
        observation_rank=None,
        design=design if design is not None else DESIGN,
        purpose="exploration",
        near_boundary=False,
        rationale="测试构造",
    )
    body.update(overrides)
    return SamplingRequest(**body)


class FakeRegistrar:
    """登记入口替身：只记录调用，不做任何判定。"""

    def __init__(self, *, ref_prefix="data_"):
        self.calls = []
        self.ref_prefix = ref_prefix

    def add_confirmation(self, protocol_id, records, purpose):
        self.calls.append(
            {"protocol_id": protocol_id, "count": len(records), "purpose": purpose}
        )
        return {"ref": f"{self.ref_prefix}{purpose.lower()}"}


class RecordingSource:
    """数据源替身：按请求返回可变条数，并记录被请求的观测。"""

    notice = "记录用合成源"

    def __init__(self, *, records_per_request=2):
        self.records_per_request = records_per_request
        self.seen = []

    def __call__(self, request, round_index=0):
        self.seen.append(request.observation_id)
        return [
            {
                "record_id": f"rec-{request.observation_id}-{index}",
                "group_ids": {"batch": f"b-{request.observation_id}-{index}"},
                "environment": str(request.design.get("environment") or "lab-A"),
                "event_time": "2026-01-01T10:00:00+00:00",
                "available_time": "2026-01-01T11:00:00+00:00",
                "values": {"X1": float(index + 1), "X2": 1.0, "Y": float(index + 2)},
                "units": {"X1": "mol/L", "X2": "mol/L", "Y": "mol/(L*s)"},
                "source": "synthetic://recording",
                "operator_annotation": "private-recording",
            }
            for index in range(self.records_per_request)
        ]


class EmptySource:
    """数据源替身：设计齐备但总返回空（**采了为空**）。"""

    notice = "空返回合成源"

    def __call__(self, request, round_index=0):
        return []


class BrokenSource:
    """数据源替身：抛错（**没采**，须降级而不中断）。"""

    notice = "故障合成源"

    def __call__(self, request, round_index=0):
        raise RuntimeError("模拟数据源故障")


class _Entry:
    """鸭子类型的证据条目替身（只提供规律提取读取的字段）。"""

    def __init__(self, hypothesis_id, grade, status, round_index, *, evidence_identity=None):
        self.hypothesis_id = hypothesis_id
        self.hypothesis_version = "1"
        self.grade = grade
        self.status = status
        self.grade_basis = "first_independent_confirmation" if grade in ("E1", "E2") else "not_supported"
        self.round_index = round_index
        self.environments = ("lab-A",)
        self.record_count = 12
        self.evidence_identity = evidence_identity or f"{hypothesis_id}-{round_index}"


class _Knowledge:
    """鸭子类型的知识库版本替身。"""

    def __init__(self, version, entries):
        self.version = version
        self.entries = tuple(entries)


# ---------------------------------------------------------------------------
# 边界：一律 AST，绝不做源码文本匹配
# ---------------------------------------------------------------------------


def _tail_name(node) -> str:
    """取调用 / 装饰器表达式的末尾名字（``a.b.skip`` → ``skip``）。"""
    if isinstance(node, pyast.Attribute):
        return node.attr
    if isinstance(node, pyast.Name):
        return node.id
    if isinstance(node, pyast.Call):
        return _tail_name(node.func)
    return ""


def _detect_violations(source_text: str):
    """复刻 :func:`self_check` 的判定逻辑，用于对任意源码做双向验证。

    :return: ``(defined_other_stages, accessed_forbidden)`` 两个升序元组。
    """
    tree = pyast.parse(source_text)
    defined = {
        node.name
        for node in pyast.walk(tree)
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef))
    }
    defined |= {
        target.id
        for node in pyast.walk(tree)
        if isinstance(node, pyast.Assign)
        for target in node.targets
        if isinstance(target, pyast.Name)
    }
    accessed: set[str] = set()
    for node in pyast.walk(tree):
        if isinstance(node, pyast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, pyast.Name):
            accessed.add(node.id)
        elif isinstance(node, pyast.alias):
            accessed.add(node.name.split(".")[-1])
    return (
        tuple(sorted(defined & set(NOT_PROVIDED_BY_P17))),
        tuple(sorted(accessed & set(FORBIDDEN_ACCESS_NAMES))),
    )


class BoundaryTests(unittest.TestCase):
    """守护「本层不判定 / 不统计 / 不直连存储」。"""

    def test_self_check_passes(self):
        """模块的 AST 级边界自检必须通过。"""
        report = self_check()
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["defined_other_stages"], ())
        self.assertEqual(report["accessed_forbidden"], ())

    def test_self_check_shape(self):
        """自检返回结构稳定，且报告了被检查的名字数量。"""
        report = self_check()
        self.assertEqual(
            set(report),
            {
                "defined_other_stages",
                "accessed_forbidden",
                "grants_evidence_grade",
                "runs_statistics",
                "executes_collection",
                "ok",
                "checked_names",
            },
        )
        self.assertEqual(report["checked_names"], len(NOT_PROVIDED_BY_P17) + len(FORBIDDEN_ACCESS_NAMES))
        self.assertTrue(report["executes_collection"])

    def test_self_check_reads_real_source(self):
        """自检读的是真实源码文件（防止自检对象错位成空文件）。"""
        source_path = pathlib.Path(inspect.getsourcefile(self_check) or __file__)
        self.assertEqual(source_path.resolve(), MODULE_PATH.resolve())
        self.assertGreater(source_path.stat().st_size, 20_000)

    def test_not_provided_names_are_not_defined(self):
        """本层不得定义 ``NOT_PROVIDED_BY_P17`` 中的任何名字。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in pyast.walk(tree)
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef))
        }
        overlap = sorted(defined & set(NOT_PROVIDED_BY_P17))
        self.assertEqual(overlap, [], msg=f"越界定义：{overlap}")

    def test_boundary_names_are_declared(self):
        """越界接口名必须写在边界声明里（否则本用例会因名单为空而空洞通过）。"""
        self.assertGreaterEqual(len(NOT_PROVIDED_BY_P17), 10)
        for name in (
            "acquisition_plan",
            "divergence",
            "archive_round",
            "update_knowledge_version",
            "execute_family",
            "compute_p_value",
            "run_loop",
        ):
            with self.subTest(name=name):
                self.assertIn(name, NOT_PROVIDED_BY_P17)
        self.assertGreaterEqual(len(FORBIDDEN_ACCESS_NAMES), 8)

    def test_detector_catches_planted_violation(self):
        """自检的判定逻辑必须能抓到真实违规（否则是一个空洞门禁）。

        构造一段「确实定义了越界名 + 确实访问了被禁入口」的源码，
        断言两侧都被识别为违规。
        """
        planted = (
            "import sqlite3\n"
            "def run_loop(custodian, protocol_id):\n"
            "    conn = sqlite3.connect('vault.sqlite3')\n"
            "    return custodian.read_dataset(protocol_id)\n"
        )
        defined, accessed = _detect_violations(planted)
        self.assertIn("run_loop", defined)
        self.assertTrue({"sqlite3", "connect", "read_dataset"} & set(accessed))

    def test_detector_ignores_boundary_declarations(self):
        """边界声明本身（docstring / 常量里写出被禁名字）不得被误判。

        本模块的 docstring 与 ``NOT_PROVIDED_BY_P17`` 里**必须**写出
        ``read_dataset`` / ``grade_for`` 等名字——若门禁用文本匹配，
        会把边界声明判成违规。这里证明 AST 判定不会。
        """
        declaration_only = (
            '"""本层不做 read_dataset，也不调用 grade_for。"""\n'
            'NOT_PROVIDED = ("read_dataset", "grade_for", "run_loop")\n'
            "def collect(request):\n"
            "    return request\n"
        )
        defined, accessed = _detect_violations(declaration_only)
        self.assertEqual(defined, ())
        self.assertEqual(accessed, ())

    def test_forbidden_names_not_accessed_in_module(self):
        """本模块不得出现对被禁数据访问入口的属性访问或导入。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Attribute):
                with self.subTest(attr=node.attr):
                    self.assertNotIn(node.attr, set(FORBIDDEN_ACCESS_NAMES))

    def test_only_stdlib_and_project_modules(self):
        """只允许标准库与既有的项目内模块；不得引入第三方依赖。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        allowed = {
            "__future__",
            "ast",
            "dataclasses",
            "hashlib",
            "inspect",
            "json",
            "pathlib",
            "tempfile",
            "types",
            "typing",
            # 项目内模块：口径常量（M7/M8）与端到端编排的惰性复用（M1/P16）。
            "sdl_m01",
            "sdl_m07",
            "sdl_m08",
            "sdl_pipeline",
        }
        found: set[str] = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, pyast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        self.assertEqual(sorted(found - allowed), [])

    def test_module_import_does_not_pull_sdl_m01(self):
        """模块层（顶层）不得导入 ``sdl_m01``：真实金库只在编排函数内惰性导入。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        top_level: set[str] = set()
        for node in tree.body:
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    top_level.add(alias.name)
            elif isinstance(node, pyast.ImportFrom) and node.module:
                top_level.add(node.module)
        self.assertFalse(any("sdl_m01" in name for name in top_level))
        self.assertFalse(any("sdl_pipeline" in name for name in top_level))

    def test_module_has_no_ellipsis_placeholder(self):
        """交付物不得含省略号占位（AST 判定裸 ``...``，不误伤 ``tuple[str, ...]``）。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        placeholders = [
            f"line{node.lineno}"
            for node in pyast.walk(tree)
            if isinstance(node, pyast.Expr)
            and isinstance(node.value, pyast.Constant)
            and node.value.value is Ellipsis
        ]
        self.assertEqual(placeholders, [])
        self.assertNotIn("…", MODULE_PATH.read_text(encoding="utf-8"))

    def test_module_docstring_exists_and_is_chinese(self):
        """模块 docstring 存在且为中文。"""
        import sdl_m09.collection as module

        doc = module.__doc__ or ""
        self.assertGreater(len(doc), 1_000)
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in doc))

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

    def test_boundary_note_denies_grading_and_causality(self):
        """边界说明必须同时否认「授予等级」与「判定因果」。"""
        self.assertIn("不授予证据等级", COLLECTION_BOUNDARY_NOTE)
        self.assertIn("不执行统计检验", COLLECTION_BOUNDARY_NOTE)
        self.assertIn("不判定任何结论的真假", COLLECTION_BOUNDARY_NOTE)

    def test_synthetic_source_disclaims_reality(self):
        """合成源标注必须否认现实含义。"""
        self.assertIn("合成实现", SYNTHETIC_SOURCE_NOTICE)
        self.assertIn("不构成对任何现实规律的支持", SYNTHETIC_SOURCE_NOTICE)
        self.assertIn("也不构成因果关系", SYNTHETIC_SOURCE_NOTICE)

    def test_catalogue_note_states_design_is_caller_supplied(self):
        """目录缺失时不臆造设计这一口径必须写在说明里。"""
        self.assertIn("由调用方提供", CATALOGUE_REQUIRED_NOTE)
        self.assertIn("绝不臆造", CATALOGUE_REQUIRED_NOTE)

    def test_empty_vs_absent_note_is_explicit(self):
        """「没采」与「采了为空」的区分口径必须显式声明。"""
        self.assertIn("没采", EMPTY_VS_ABSENT_NOTE)
        self.assertIn("采了为空", EMPTY_VS_ABSENT_NOTE)
        self.assertIn("绝不用", EMPTY_VS_ABSENT_NOTE)

    def test_exception_hierarchy(self):
        """异常层级：输入/策略错误都落在 :class:`CollectionError` 之下。"""
        self.assertTrue(issubclass(CollectionInputError, CollectionError))
        self.assertTrue(issubclass(CollectionPolicyError, CollectionError))
        self.assertTrue(issubclass(CollectionError, ValueError))

    def test_version_and_executes_flag_are_complements(self):
        """M7（建议层）与 M9（执行层）的执行标志必须互为补集。"""
        self.assertIs(ACQUISITION_PLAN_EXECUTES_COLLECTION, False)
        self.assertIs(COLLECTION_EXECUTES_COLLECTION, True)
        self.assertIs(COLLECTION_GRANTS_EVIDENCE_GRADE, False)
        self.assertIs(COLLECTION_RUNS_STATISTICS, False)
        self.assertTrue(COLLECTION_VERSION.startswith("P17"))


# ---------------------------------------------------------------------------
# 采样请求与目录
# ---------------------------------------------------------------------------


class SamplingRequestTests(unittest.TestCase):
    """:class:`SamplingRequest` 的形态、冻结性与序列化。"""

    def test_frozen_and_mappings_immutable(self):
        request = make_request()
        with self.assertRaises(Exception):
            request.rank = 5  # type: ignore[misc]
        with self.assertRaises(TypeError):
            request.design["batches"] = 99  # type: ignore[index]
        with self.assertRaises(TypeError):
            request.provenance["x"] = 1  # type: ignore[index]

    def test_batches_and_readings_are_read_from_design(self):
        request = make_request(design={"environment": "lab-A", "batches": 3, "readings": 7})
        self.assertEqual(request.batches, 3)
        self.assertEqual(request.readings, 7)

    def test_missing_design_counts_are_zero_not_guessed(self):
        """设计未指定批次数时不得臆造——如实为 0。"""
        request = make_request(design={"environment": "lab-A"})
        self.assertEqual(request.batches, 0)
        self.assertEqual(request.readings, 0)

    def test_boolean_design_counts_are_rejected_as_numbers(self):
        """布尔不是数量（避免 ``True`` 被当成 1）。"""
        request = make_request(design={"batches": True, "readings": 1})
        self.assertEqual(request.batches, 0)

    def test_validation_rejects_bad_input(self):
        with self.assertRaises(CollectionInputError):
            make_request(observation_id="")
        with self.assertRaises(CollectionInputError):
            make_request(rank=-1)
        with self.assertRaises(CollectionInputError):
            make_request(design=42)  # type: ignore[arg-type]
        with self.assertRaises(CollectionInputError):
            make_request(purpose="")

    def test_serialization_is_deterministic(self):
        first = make_request()
        second = make_request()
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.content_digest(), second.content_digest())
        self.assertEqual(len(first.content_digest()), 64)

    def test_to_dict_declares_heuristic_transcription(self):
        """采样请求必须声明排序是**转录**自 M7 的启发式，而非本层重算。"""
        payload = make_request().to_dict()
        self.assertEqual(payload["ranking_basis"], RANKING_BASIS_HEURISTIC)
        self.assertIs(payload["is_heuristic"], True)
        self.assertIs(payload["ranking_transcribed"], True)
        self.assertEqual(payload["collection_version"], COLLECTION_VERSION)


class SamplingCatalogueTests(unittest.TestCase):
    """采样目录的解析顺序与视图。"""

    def setUp(self):
        self.catalogue = SamplingCatalogue(
            {"obs-0000": {"environment": "lab-A", "batches": 1, "readings": 1}},
            by_purpose={"confirmation": {"environment": "lab-B", "batches": 5, "readings": 5}},
            default_design={"environment": "lab-Z", "batches": 9, "readings": 9},
        )

    def test_observation_match_wins(self):
        design = self.catalogue.resolve("obs-0000", "confirmation")
        self.assertEqual(design["environment"], "lab-A")

    def test_purpose_match_beats_default(self):
        design = self.catalogue.resolve("obs-9999", "confirmation")
        self.assertEqual(design["environment"], "lab-B")

    def test_default_is_last_resort(self):
        design = self.catalogue.resolve("obs-9999", "exploration")
        self.assertEqual(design["environment"], "lab-Z")

    def test_no_match_returns_none(self):
        catalogue = SamplingCatalogue({"obs-0000": DESIGN})
        self.assertIsNone(catalogue.resolve("obs-9999", "exploration"))

    def test_has_default_and_size(self):
        self.assertTrue(self.catalogue.has_default)
        self.assertEqual(self.catalogue.size, 1)
        self.assertFalse(SamplingCatalogue({"a": DESIGN}).has_default)

    def test_serialization_is_deterministic_and_lists_keys_only(self):
        payload = self.catalogue.to_dict()
        self.assertEqual(payload["observation_ids"], ["obs-0000"])
        self.assertEqual(payload["purposes"], ["confirmation"])
        self.assertTrue(payload["has_default"])
        self.assertEqual(
            self.catalogue.canonical_json(), SamplingCatalogue(
                {"obs-0000": {"environment": "lab-A", "batches": 1, "readings": 1}},
                by_purpose={"confirmation": {"environment": "lab-B", "batches": 5, "readings": 5}},
                default_design={"environment": "lab-Z", "batches": 9, "readings": 9},
            ).canonical_json()
        )


# ---------------------------------------------------------------------------
# 计划翻译：保序与如实上报
# ---------------------------------------------------------------------------


class PlanToRequestsTests(unittest.TestCase):
    """把 M7 计划翻译成采样请求。"""

    def test_preserves_plan_order_without_reordering(self):
        """采样请求必须严格保持计划顺序——绝不按本层口径重排。"""
        plan = make_plan("obs-0002", "obs-0000", "obs-0001")
        catalogue = SamplingCatalogue(default_design=DESIGN)
        requests, skipped = plan_to_requests(plan, catalogue)
        self.assertEqual([r.observation_id for r in requests], ["obs-0002", "obs-0000", "obs-0001"])
        self.assertEqual([r.rank for r in requests], [0, 1, 2])
        self.assertEqual(skipped, ())

    def test_transcribes_heuristic_value_and_boundary_flag(self):
        plan = make_plan("obs-0000", "obs-0001")
        requests, _ = plan_to_requests(plan, SamplingCatalogue(default_design=DESIGN))
        self.assertEqual(requests[0].observation_rank, 10.0)
        self.assertEqual(requests[1].observation_rank, 9.0)
        self.assertTrue(requests[0].near_boundary)
        self.assertFalse(requests[1].near_boundary)

    def test_provenance_records_plan_digest(self):
        plan = make_plan("obs-0000")
        requests, _ = plan_to_requests(plan, SamplingCatalogue(default_design=DESIGN))
        provenance = requests[0].provenance
        self.assertIn("plan_digest", provenance)
        self.assertEqual(provenance["plan_purpose"], "exploration")
        self.assertIn("未重排", provenance["source"])

    def test_none_plan_is_reported_as_not_collected(self):
        requests, skipped = plan_to_requests(None, SamplingCatalogue())
        self.assertEqual(requests, ())
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], REASON_NO_PLAN)

    def test_plan_without_items_is_no_plan(self):
        requests, skipped = plan_to_requests({"available": False, "reason": "缺成本"}, SamplingCatalogue())
        self.assertEqual(requests, ())
        self.assertEqual(skipped[0]["reason"], REASON_NO_PLAN)

    def test_no_catalogue_marks_all_as_not_collected(self):
        """没有目录时全部记为**没采**，绝不臆造设计。"""
        plan = make_plan("obs-0000", "obs-0001")
        requests, skipped = plan_to_requests(plan, None)
        self.assertEqual(requests, ())
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all(item["reason"] == REASON_NO_MAPPING for item in skipped))

    def test_unmapped_observation_is_reported_not_fabricated(self):
        """未在目录中匹配到的观测记为没采，不臆造一份设计。"""
        plan = make_plan("obs-0000", "obs-0001")
        catalogue = SamplingCatalogue({"obs-0000": DESIGN})
        requests, skipped = plan_to_requests(plan, catalogue)
        self.assertEqual([r.observation_id for r in requests], ["obs-0000"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["observation_id"], "obs-0001")
        self.assertEqual(skipped[0]["reason"], REASON_NO_MAPPING)

    def test_max_requests_budget_reports_overflow(self):
        plan = make_plan("obs-0000", "obs-0001", "obs-0002")
        requests, skipped = plan_to_requests(
            plan, SamplingCatalogue(default_design=DESIGN), max_requests=2
        )
        self.assertEqual([r.observation_id for r in requests], ["obs-0000", "obs-0001"])
        self.assertEqual([item["reason"] for item in skipped], [REASON_BUDGET_EXHAUSTED])

    def test_max_requests_zero_yields_all_budget_exhausted(self):
        plan = make_plan("obs-0000")
        requests, skipped = plan_to_requests(
            plan, SamplingCatalogue(default_design=DESIGN), max_requests=0
        )
        self.assertEqual(requests, ())
        self.assertEqual(skipped[0]["reason"], REASON_BUDGET_EXHAUSTED)

    def test_malformed_inputs_rejected(self):
        with self.assertRaises(CollectionInputError):
            plan_to_requests(42, SamplingCatalogue())  # type: ignore[arg-type]
        with self.assertRaises(CollectionInputError):
            plan_to_requests({"items": "abc"}, SamplingCatalogue())  # type: ignore[arg-type]
        with self.assertRaises(CollectionInputError):
            plan_to_requests({"items": []}, SamplingCatalogue(), max_requests=-1)

    def test_non_mapping_item_is_reported(self):
        plan = {"purpose": "exploration", "items": [{"observation_id": "obs-0000"}, "bad"]}
        requests, skipped = plan_to_requests(plan, SamplingCatalogue(default_design=DESIGN))
        self.assertEqual(len(requests), 1)
        self.assertEqual(skipped[0]["reason"], REASON_NO_MAPPING)

    def test_purpose_falls_back_to_plan_purpose(self):
        plan = {"purpose": "confirmation", "items": [{"observation_id": "obs-0000", "rank": 0}]}
        requests, _ = plan_to_requests(plan, SamplingCatalogue(default_design=DESIGN))
        self.assertEqual(requests[0].purpose, "confirmation")

    def test_level_kwargs_rejected(self):
        """试图让本层授予等级 / 给出结论的入参必须被拒绝。"""
        for banned in ({"grade": "E2"}, {"p_value": 0.05}, {"verdict": "supported"}, {"status": "supported"}):
            with self.subTest(banned=banned):
                with self.assertRaises(CollectionPolicyError):
                    plan_to_requests(make_plan("obs-0000"), SamplingCatalogue(default_design=DESIGN), **banned)

    def test_generator_input_is_rejected_clearly(self):
        """生成器不是序列：必须给出明确的输入错误，而不是静默漏读。"""
        with self.assertRaises(CollectionInputError):
            plan_to_requests({"items": iter([])}, SamplingCatalogue())


# ---------------------------------------------------------------------------
# 单条取数
# ---------------------------------------------------------------------------


class CollectTests(unittest.TestCase):
    """驱动数据源取一条请求。"""

    def test_synthetic_source_returns_records(self):
        request = make_request(design={"environment": "lab-A", "batches": 2, "readings": 3})
        records = collect(request, SyntheticSource(), round_index=2)
        self.assertEqual(len(records), 6)
        self.assertEqual(len({record["group_ids"]["batch"] for record in records}), 2)

    def test_synthetic_source_empty_when_design_incomplete(self):
        """设计不完整时返回空（采了为空），不臆造数量。"""
        request = make_request(design={"environment": "lab-A"})
        self.assertEqual(collect(request, SyntheticSource()), [])

    def test_synthetic_source_uses_fresh_batch_ranges(self):
        """同一合成源连续取数必须落在**不同批次号段**，避免 M1 重复登记闸门。"""
        source = SyntheticSource()
        request = make_request(design={"environment": "lab-A", "batches": 2, "readings": 1})
        first = {record["group_ids"]["batch"] for record in collect(request, source)}
        second = {record["group_ids"]["batch"] for record in collect(request, source)}
        self.assertEqual(first & second, set())

    def test_single_argument_source_is_supported(self):
        """只接受单个参数的数据源实现也应被兼容。"""

        def source(request):
            return [{"record_id": "r-1", "group_ids": {"batch": "b-1"}, "values": {"Y": 1.0}}]

        self.assertEqual(len(collect(make_request(), source)), 1)

    def test_none_result_is_treated_as_empty(self):
        self.assertEqual(collect(make_request(), lambda request, round_index=0: None), [])

    def test_validation_rejects_bad_input(self):
        with self.assertRaises(CollectionInputError):
            collect({"not": "a request"}, lambda r, i=0: [])  # type: ignore[arg-type]
        with self.assertRaises(CollectionInputError):
            collect(make_request(), 42)  # type: ignore[arg-type]
        with self.assertRaises(CollectionInputError):
            collect(make_request(), lambda r, i=0: "not a sequence")

    def test_source_error_propagates_for_caller_to_degrade(self):
        """单条取数不吞异常；降级由采集器负责。"""
        with self.assertRaises(RuntimeError):
            collect(make_request(), BrokenSource())

    def test_level_kwargs_rejected(self):
        with self.assertRaises(CollectionPolicyError):
            collect(make_request(), SyntheticSource(), grade="E2")


# ---------------------------------------------------------------------------
# 采集结果对象
# ---------------------------------------------------------------------------


class CollectionOutcomeTests(unittest.TestCase):
    """「没采」与「采了为空」的分列视图。"""

    @staticmethod
    def _make_outcome(results, skipped=()):
        """构造一份结果对象（注意：**不能**把这个夹具命名为 ``_outcome``——
        那是 ``unittest.TestCase`` 的内部属性，会遮蔽方法调用）。"""
        return CollectionOutcome(
            round_index=2,
            purpose="exploration",
            requests=(),
            results=tuple(results),
            skipped=tuple(skipped),
        )

    def test_reason_counts_cover_all_reason_codes(self):
        outcome = self._make_outcome([])
        self.assertEqual(set(outcome.reason_counts), set(COLLECTION_REASONS))
        self.assertTrue(all(value == 0 for value in outcome.reason_counts.values()))

    def test_boundaries_between_empty_and_absent(self):
        """采了为空与没采必须落在不同的属性视图里。"""
        outcome = self._make_outcome(
            [
                CollectedBatch("obs-a", REASON_COLLECTED, True, 4, registered_ref="data_a"),
                CollectedBatch("obs-b", REASON_SOURCE_EMPTY, False, 0),
                CollectedBatch("obs-c", REASON_NO_MAPPING, False, 0),
                CollectedBatch("obs-d", REASON_SOURCE_FAILED, False, 0, error="boom"),
            ]
        )
        self.assertEqual([b.observation_id for b in outcome.collected_batches], ["obs-a"])
        self.assertEqual([b.observation_id for b in outcome.empty_batches], ["obs-b"])
        self.assertEqual([b.observation_id for b in outcome.not_collected], ["obs-c", "obs-d"])
        self.assertEqual(outcome.total_records, 4)
        self.assertEqual(outcome.registered_refs, ("data_a",))

    def test_skipped_entries_feed_reason_counts(self):
        outcome = self._make_outcome([], skipped=[{"observation_id": "x", "reason": REASON_NO_PLAN}])
        self.assertEqual(outcome.reason_counts[REASON_NO_PLAN], 1)

    def test_not_collected_reason_set_is_complement_of_empty(self):
        self.assertNotIn(REASON_SOURCE_EMPTY, NOT_COLLECTED_REASONS)
        self.assertNotIn(REASON_COLLECTED, NOT_COLLECTED_REASONS)
        self.assertIn(REASON_NO_MAPPING, NOT_COLLECTED_REASONS)
        self.assertIn(REASON_SOURCE_FAILED, NOT_COLLECTED_REASONS)

    def test_collected_batch_rejects_unknown_reason(self):
        with self.assertRaises(CollectionInputError):
            CollectedBatch("obs-a", "made_up_reason", True, 1)

    def test_to_dict_flags_collection_flags(self):
        outcome = self._make_outcome([])
        payload = outcome.to_dict()
        self.assertIs(payload["executes_collection"], True)
        self.assertIs(payload["grants_evidence_grade"], False)
        self.assertIs(payload["runs_statistics"], False)
        self.assertEqual(payload["collected_batch_count"], 0)

    def test_render_is_human_readable(self):
        outcome = self._make_outcome([CollectedBatch("obs-a", REASON_COLLECTED, True, 2)])
        text = outcome.render()
        self.assertIn("自主取数", text)
        self.assertIn("obs-a", text)


# ---------------------------------------------------------------------------
# 自主取数执行器：闭环与降级
# ---------------------------------------------------------------------------


class AutonomousCollectorTests(unittest.TestCase):
    """同时充当 sink 与 supplier 的执行器。"""

    def _collector(self, *, source=None, catalogue=None, budget=None, fallback=None):
        return AutonomousCollector(
            source=source or RecordingSource(),
            registrar=FakeRegistrar(),
            catalogue=catalogue or SamplingCatalogue(default_design=DESIGN),
            budget=budget or CollectionBudget(max_batches=16, max_requests_per_round=1),
            fallback_design=fallback,
        )

    def test_registrar_must_expose_add_confirmation(self):
        with self.assertRaises(CollectionInputError):
            AutonomousCollector(source=RecordingSource(), registrar=object())

    def test_driven_by_previous_round_plan(self):
        """闭环核心：本轮采的观测必须来自**上一轮**的建议。"""
        collector = self._collector()
        plan = make_plan("obs-0007", "obs-0006")
        collector.observe(1, plan)
        collector(2, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual([r.observation_id for r in outcome.requests], ["obs-0007"])
        self.assertEqual(collector.batches_used, 2)

    def test_first_round_without_previous_plan_is_not_collected(self):
        """首轮之前没有「上一轮」，且无兜底时如实记为没采。"""
        collector = self._collector()
        collector(1, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual(outcome.requests, ())
        self.assertEqual(outcome.reason_counts[REASON_NO_PLAN], 1)

    def test_plan_observed_after_supply_does_not_leak_backwards(self):
        """本轮观察到的计划不得被本轮自己消费（只影响下一轮）。"""
        collector = self._collector()
        collector.observe(2, make_plan("obs-0002"))
        collector(2, "proto-1")
        self.assertEqual(collector.outcomes[0].requests, ())

    def test_registration_goes_through_injected_registrar(self):
        collector = self._collector()
        collector.observe(1, make_plan("obs-0000"))
        collector(2, "proto-1")
        registrar = collector.registrar
        self.assertEqual(len(registrar.calls), 1)
        self.assertEqual(registrar.calls[0]["protocol_id"], "proto-1")
        self.assertEqual(registrar.calls[0]["purpose"], "C2")
        self.assertEqual(collector.outcomes[0].registered_refs, ("data_c2",))

    def test_purposes_are_unique_across_rounds(self):
        collector = self._collector()
        plan = make_plan("obs-0000")
        collector.observe(1, plan)
        collector(2, "proto-1")
        collector.observe(2, plan)
        collector(3, "proto-1")
        purposes = [call["purpose"] for call in collector.registrar.calls]
        self.assertEqual(len(purposes), len(set(purposes)))

    def test_per_round_request_cap_is_enforced(self):
        collector = self._collector(budget=CollectionBudget(max_batches=16, max_requests_per_round=1))
        collector.observe(1, make_plan("obs-0000", "obs-0001", "obs-0002"))
        collector(2, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual(len(outcome.requests), 1)
        self.assertEqual(outcome.reason_counts[REASON_BUDGET_EXHAUSTED], 2)

    def test_batch_budget_exhaustion_is_reported(self):
        """批次预算用尽必须如实记为没采，绝不静默跳过。"""
        collector = self._collector(budget=CollectionBudget(max_batches=2, max_requests_per_round=1))
        collector.observe(1, make_plan("obs-0000"))
        collector(2, "proto-1")
        collector.observe(2, make_plan("obs-0001"))
        collector(3, "proto-1")
        second = collector.outcomes[1]
        self.assertEqual(second.reason_counts[REASON_BUDGET_EXHAUSTED], 1)
        self.assertEqual(collector.batches_used, 2)

    def test_empty_source_is_emptiness_not_failure(self):
        """设计齐备、确实驱动了数据源、返回空 → 采了为空（不是失败）。"""
        collector = self._collector(source=EmptySource())
        collector.observe(1, make_plan("obs-0000"))
        collector(2, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual([b.observation_id for b in outcome.empty_batches], ["obs-0000"])
        self.assertEqual(outcome.not_collected, ())
        self.assertEqual(outcome.reason_counts[REASON_SOURCE_EMPTY], 1)
        self.assertEqual(len(collector.registrar.calls), 0)

    def test_broken_source_degrades_without_aborting(self):
        """数据源抛错必须降级为一条失败记录，绝不中断整轮。"""
        collector = self._collector(source=BrokenSource())
        collector.observe(1, make_plan("obs-0000"))
        collector(2, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual(outcome.reason_counts[REASON_SOURCE_FAILED], 1)
        self.assertIn("RuntimeError", outcome.results[0].error or "")

    def test_unmapped_observation_is_reported(self):
        collector = self._collector(catalogue=SamplingCatalogue({"obs-0000": DESIGN}))
        collector.observe(1, make_plan("obs-0000", "obs-9999"))
        collector(2, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual([r.observation_id for r in outcome.requests], ["obs-0000"])
        self.assertEqual([item["observation_id"] for item in outcome.skipped], ["obs-9999"])

    def test_fallback_design_used_only_when_no_plan(self):
        collector = self._collector(
            fallback={"environment": "lab-A", "batches": 1, "readings": 1}
        )
        collector(1, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual([r.observation_id for r in outcome.requests], ["fallback"])
        self.assertEqual(outcome.reason_counts[REASON_COLLECTED], 1)
        self.assertTrue(any("兜底" in note for note in outcome.notes))

    def test_empty_design_source_failure_does_not_abort_other_requests(self):
        """单条失败不得吞掉同轮其余请求。"""

        class Flaky:
            notice = "时好时坏"

            def __init__(self):
                self.calls = 0

            def __call__(self, request, round_index=0):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("首条失败")
                return [{"record_id": "r", "group_ids": {"batch": "b"}, "values": {"Y": 1.0}}]

        collector = self._collector(
            source=Flaky(),
            budget=CollectionBudget(max_batches=16, max_requests_per_round=2),
        )
        collector.observe(1, make_plan("obs-0000", "obs-0001"))
        collector(2, "proto-1")
        outcome = collector.outcomes[0]
        self.assertEqual(outcome.reason_counts[REASON_SOURCE_FAILED], 1)
        self.assertEqual(outcome.reason_counts[REASON_COLLECTED], 1)

    def test_trace_proves_suggestion_to_sampling_causality(self):
        """溯源必须能证明「本轮采样 = 上一轮建议」。"""
        collector = self._collector()
        plan = make_plan("obs-0005", "obs-0004")
        collector.observe(1, plan)
        collector(2, "proto-1")
        trace = collector.trace()
        self.assertEqual(len(trace["rounds"]), 1)
        entry = trace["rounds"][0]
        self.assertEqual(entry["round_index"], 2)
        self.assertEqual(entry["driven_by_plan_round"], 1)
        self.assertEqual(entry["plan_item_ids"], ["obs-0005", "obs-0004"])
        self.assertEqual(entry["requests"], ["obs-0005"])
        self.assertIsNotNone(entry["plan_digest"])

    def test_trace_reports_budget_and_purposes(self):
        collector = self._collector(budget=CollectionBudget(max_batches=8, max_requests_per_round=1))
        collector.observe(1, make_plan("obs-0000"))
        collector(2, "proto-1")
        trace = collector.trace()
        self.assertEqual(trace["batches_used"], 2)
        self.assertEqual(trace["budget"]["max_batches"], 8)
        self.assertEqual(trace["registered_purposes"], ["C2"])

    def test_observed_plans_view_is_read_only(self):
        collector = self._collector()
        collector.observe(1, make_plan("obs-0000"))
        view = collector.observed_plans()
        with self.assertRaises(TypeError):
            view[9] = {}  # type: ignore[index]

    def test_serialization_is_deterministic(self):
        def build():
            collector = self._collector()
            collector.observe(1, make_plan("obs-0000"))
            collector(2, "proto-1")
            return collector

        self.assertEqual(build().canonical_json(), build().canonical_json())

    def test_budget_validation(self):
        for bad in ({"max_batches": 0}, {"max_requests_per_round": 0}, {"max_batches": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(CollectionInputError):
                    CollectionBudget(**bad)

    def test_round_index_must_be_positive(self):
        collector = self._collector()
        with self.assertRaises(CollectionInputError):
            collector.collect_round(0, "proto-1", plan=None)


# ---------------------------------------------------------------------------
# 规律提取
# ---------------------------------------------------------------------------


class RegularityExtractionTests(unittest.TestCase):
    """从知识库版本中提取「发掘到的规律」。"""

    def setUp(self):
        self.prior = _Knowledge("K0", [])
        self.now = _Knowledge(
            "K7",
            [
                _Entry("h-confirmed", "E2", "supported", 1),
                _Entry("h-low-grade", "E0", "inconclusive", 1),
                _Entry("h-refuted", "E1", "refuted", 2),
                _Entry("h-supported", "E1", "supported", 2),
                _Entry("h-e0-supported", "E0", "supported", 3),
            ],
        )

    def test_only_definitive_conclusions_count_as_regularities(self):
        """只有 supported+确证级 或 refuted 才算规律；E0 / inconclusive 不算。"""
        report = extract_discovered_regularities(self.now, prior_knowledge=self.prior)
        ids = {item.hypothesis_id for item in report.regularities}
        self.assertEqual(ids, {"h-confirmed", "h-refuted", "h-supported"})
        self.assertNotIn("h-low-grade", ids)
        self.assertNotIn("h-e0-supported", ids)

    def test_supported_and_refuted_partition(self):
        report = extract_discovered_regularities(self.now, prior_knowledge=self.prior)
        self.assertEqual({item.hypothesis_id for item in report.supported}, {"h-confirmed", "h-supported"})
        self.assertEqual({item.hypothesis_id for item in report.refuted}, {"h-refuted"})

    def test_relation_labels(self):
        report = extract_discovered_regularities(self.now, prior_knowledge=self.prior)
        for item in report.regularities:
            if item.status == "supported":
                self.assertEqual(item.relation, "supported_regularity")
                self.assertTrue(item.is_confirmatory)
            else:
                self.assertEqual(item.relation, "refuted_regularity")

    def test_is_new_relative_to_prior_knowledge(self):
        """相对起始知识库判定新增：已存在的条目标为非新增。"""
        prior = _Knowledge("K1", [_Entry("h-confirmed", "E2", "supported", 1)])
        report = extract_discovered_regularities(self.now, prior_knowledge=prior)
        by_id = {item.hypothesis_id: item for item in report.regularities}
        self.assertFalse(by_id["h-confirmed"].is_new)
        self.assertTrue(by_id["h-supported"].is_new)
        self.assertEqual([item.hypothesis_id for item in report.new_supported], ["h-supported"])

    def test_without_prior_all_are_new(self):
        report = extract_discovered_regularities(self.now)
        self.assertTrue(all(item.is_new for item in report.regularities))

    def test_knowledge_version_is_carried(self):
        report = extract_discovered_regularities(self.now, prior_knowledge=self.prior)
        self.assertEqual(report.knowledge_version, "K7")

    def test_report_serialization_and_render(self):
        report = extract_discovered_regularities(self.now, prior_knowledge=self.prior)
        payload = report.to_dict()
        self.assertEqual(payload["supported_count"], 2)
        self.assertEqual(payload["refuted_count"], 1)
        self.assertEqual(payload["new_supported_count"], 2)
        self.assertIn("规律", report.render())
        self.assertIsInstance(report, RegularityReport)

    def test_level_kwargs_rejected(self):
        with self.assertRaises(CollectionPolicyError):
            extract_discovered_regularities(self.now, promote_to="causal")

    def test_missing_attributes_are_not_fabricated(self):
        """条目缺字段时按缺省读取，不插补不臆造。"""

        class Bare:
            pass

        report = extract_discovered_regularities(_Knowledge("K1", [Bare()]))
        self.assertEqual(report.regularities, ())


# ---------------------------------------------------------------------------
# 集成：真实 M1 金库 + P16 主循环
# ---------------------------------------------------------------------------


class _VaultCase(unittest.TestCase):
    """真实 M1 证据库夹具。"""

    GROUPS = 20

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-m09-")
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "vault.sqlite3"
        self.tokens = initialize(str(self.db))
        self.custodian = Module01(str(self.db), self.tokens["custodian"])
        self.protocol = self.custodian.build(
            synthetic_spec(), synthetic_records(groups=self.GROUPS)
        )
        self.protocol_id = self.protocol["protocol_id"]


class VaultIntegrationTests(_VaultCase):
    """与真实 M1 金库的集成：登记只能经公开接口。"""

    def _collector(self, **overrides):
        kwargs = dict(
            source=SyntheticSource(),
            registrar=self.custodian,
            catalogue=SamplingCatalogue(default_design=DESIGN),
            budget=CollectionBudget(max_batches=10, max_requests_per_round=1),
        )
        kwargs.update(overrides)
        return AutonomousCollector(**kwargs)

    def test_collected_data_is_registered_in_vault(self):
        collector = self._collector()
        collector.observe(1, make_plan("obs-0000"))
        collector(2, self.protocol_id)
        outcome = collector.outcomes[0]
        self.assertEqual(outcome.reason_counts[REASON_COLLECTED], 1)
        refs = outcome.registered_refs
        self.assertEqual(len(refs), 1)
        # 登记的引用必须真的出现在协议的公开视图里。
        resources = self.custodian.describe(self.protocol_id)["resources"]
        self.assertIn("C2", resources)
        self.assertEqual(resources["C2"], refs[0])

    def test_multiple_rounds_do_not_trip_duplicate_content_gate(self):
        """连续多轮取数不得触发 M1 的「不得重复登记已见内容」闸门。"""
        collector = self._collector()
        plan = make_plan("obs-0000")
        collector.observe(1, plan)
        collector(2, self.protocol_id)
        collector.observe(2, plan)
        collector(3, self.protocol_id)
        self.assertEqual([o.reason_counts[REASON_COLLECTED] for o in collector.outcomes], [1, 1])
        self.assertEqual(collector.trace()["registered_purposes"], ["C2", "C3"])

    def test_batch_budget_limits_against_real_vault(self):
        collector = self._collector(budget=CollectionBudget(max_batches=2, max_requests_per_round=1))
        plan = make_plan("obs-0000")
        collector.observe(1, plan)
        collector(2, self.protocol_id)
        collector.observe(2, plan)
        collector(3, self.protocol_id)
        self.assertEqual(collector.outcomes[1].reason_counts[REASON_BUDGET_EXHAUSTED], 1)
        self.assertEqual(collector.batches_used, 2)


class EndToEndTests(unittest.TestCase):
    """端到端：自主取数 → 确证 → 规律清单。"""

    #: 收紧预算以控制测试时长：本用例验证的是**闭环与口径**，不是恢复比例候选。
    BUDGET = dict(max_rounds=2, max_candidates=60, freeze_cap=2, n_resamples=2)

    def _run(self, *, output=None, **overrides):
        kwargs = dict(
            output=output,
            budget=LoopBudget(**self.BUDGET),
            collection_budget=CollectionBudget(max_batches=6, max_requests_per_round=1),
            groups=60,
        )
        kwargs.update(overrides)
        return run_autonomous_discovery(**kwargs)

    def test_loop_is_closed_by_suggestion(self):
        """端到端闭环：本轮实际采样的观测必须来自上一轮的 M7 建议。"""
        payload = self._run()
        rounds = payload["collection_trace"]["rounds"]
        self.assertGreaterEqual(len(rounds), 1)
        for entry in rounds:
            self.assertTrue(entry["plan_item_ids"], msg="M7 应当给出了建议条目")
            self.assertTrue(entry["requests"], msg="本轮应当按建议发起了采样")
            # 每一条采样请求都必须能在所依据的计划条目里找到。
            for observation_id in entry["requests"]:
                self.assertIn(observation_id, entry["plan_item_ids"])

    def test_payload_declares_boundaries(self):
        payload = self._run()
        self.assertEqual(payload["collection_version"], COLLECTION_VERSION)
        self.assertEqual(payload["boundary_note"], COLLECTION_BOUNDARY_NOTE)
        self.assertIn("regularities", payload)
        self.assertIn("collection", payload)
        self.assertIn("collection_trace", payload)
        self.assertIn("archive", payload)

    def test_discovered_regularities_are_reported(self):
        payload = self._run()
        report = payload["regularities"]
        self.assertEqual(report["count"], report["supported_count"] + report["refuted_count"])
        self.assertGreater(report["count"], 0, msg="端到端应当至少发掘出一条明确结论")
        for item in report["regularities"]:
            with self.subTest(hypothesis=item["hypothesis_id"]):
                self.assertIn(item["relation"], {"supported_regularity", "refuted_regularity"})

    def test_archive_contains_still_missing_evidence_list(self):
        """主循环档案必须仍带「仍缺证据的问题清单」，且逐条列出所缺证据。

        停止**不**意味着其余假说为假——因此每个未决问题都必须给出
        ``missing_evidence``（还缺什么），而不是被静默丢弃。
        """
        payload = self._run()
        open_questions = payload["archive"]["open_questions"]
        self.assertIsInstance(open_questions, list)
        self.assertGreater(len(open_questions), 0)
        for question in open_questions:
            with self.subTest(question=question.get("question_id")):
                self.assertIn("missing_evidence", question)
                self.assertIsInstance(question["missing_evidence"], list)
                self.assertGreater(len(question["missing_evidence"]), 0)

    def test_output_directory_written(self):
        with tempfile.TemporaryDirectory(prefix="sdl-m09-e2e-") as tmp:
            payload = self._run(output=tmp)
            directory = pathlib.Path(tmp)
            for name in (
                "regularities.json",
                "collection.json",
                "collection-trace.json",
                "discovery-archive.json",
                "说明.md",
            ):
                with self.subTest(name=name):
                    self.assertTrue((directory / name).exists())
            written = json.loads((directory / "regularities.json").read_text(encoding="utf-8"))
            self.assertEqual(written["count"], payload["regularities"]["count"])
            note = (directory / "说明.md").read_text(encoding="utf-8")
            self.assertIn("合成验证标注", note)
            self.assertIn("边界声明", note)

    def test_deterministic_with_fixed_recorded_at(self):
        """给定固定归档时间与固定种子，结果须可复现。"""
        first = self._run(recorded_at="2026-01-01T00:00:00+00:00")
        second = self._run(recorded_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(
            first["regularities"]["regularities"],
            second["regularities"]["regularities"],
        )
        self.assertEqual(
            [r["requests"] for r in first["collection_trace"]["rounds"]],
            [r["requests"] for r in second["collection_trace"]["rounds"]],
        )

    def test_level_kwargs_rejected(self):
        with self.assertRaises(CollectionPolicyError):
            run_autonomous_discovery(output=None, promote_to="causal")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
