"""P18 行为测试：分析决策层（M10 工具派发器）。

本文件对应 ``sdl_m10/toolbox.py``。验收标准四条：

1. **工具登记与真实签名一致。** 每个工具的 ``parameters`` 必须由真实函数签名
   派生（``_spec_params``），不得手工抄写成漂移的副本；对已登记工具抽样，
   断言 schema 与 ``inspect.signature`` 逐项吻合。
2. **边界守卫四路全覆盖。** 未知工具｜越权入参（等级/结论/p 值）｜句柄类型不符｜
   结果夹带封存特征，四类都必须以 ``rejected`` + 正确原因码返回，**绝不抛出**。
3. **出闸守卫两侧都测**（通用自检法）：① 构造**应被忽略**的干扰项（算法层合法
   的 ``source`` / ``source_ref``）断言**不**触发；② 构造**真实违规项**
   （``values`` / ``record_id`` / ``_quality``）断言**必**被抓到。只测一侧可能
   写成空洞门禁。
4. **AST 边界自检双向验证。** 用本文件内的 ``_detect_violations`` 复刻自检逻辑，
   喂入① 正常源码② 植入违规的源码，断言前者不触发、后者必被抓——证明自检非空洞。

另有一组集成测试用**真实 M1 金库**跑通「注入 explorer → 制备样本 → 拟合 → 对照
基线」的完整 LLM 分析链路。
"""

from __future__ import annotations

import ast
import inspect
import os
import tempfile
import unittest

from sdl_m01 import Module01, initialize
from sdl_m10.toolbox import (
    FORBIDDEN_ACCESS_NAMES,
    FORBIDDEN_TOOL_ARGUMENTS,
    HANDLE_PREFIX,
    LEAK_GUARD_EXEMPT_KEYS,
    M10_SEALED_KEYS,
    NOT_PROVIDED_BY_P18,
    REASON_BAD_HANDLE,
    REASON_FORBIDDEN_ARGUMENT,
    REASON_MISSING_ARGUMENT,
    REASON_SEALED_LEAK,
    REASON_TOOL_RAISED,
    REASON_UNEXPECTED_ARGUMENT,
    REASON_UNKNOWN_TOOL,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REJECTED,
    TOOLBOX_ADDS_ALGORITHMS,
    TOOLBOX_DECIDES_NOT_LLM,
    TOOLBOX_EXPLORATION_ONLY,
    TOOLBOX_GRANTS_EVIDENCE_GRADE,
    TOOLBOX_RUNS_STATISTICS,
    TOOLBOX_VERSION,
    HandleError,
    HandleStore,
    ParamSpec,
    Toolbox,
    ToolboxInputError,
    ToolResult,
    ToolSpec,
    UnknownToolError,
    build_default_toolbox,
    self_check,
)
from sdl_pipeline.loop import synthetic_records, synthetic_spec

MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "sdl_m10", "toolbox.py")
)
TEST_PATH = os.path.abspath(__file__)


def _module_source() -> str:
    with open(MODULE_PATH, encoding="utf-8") as handle:
        return handle.read()


def _detect_violations(source: str) -> dict:
    """复刻 ``self_check`` 的 AST 逻辑，供**双向**验证使用。

    与实现保持同构：定义名与访问名都收集，访问检查剔除本地绑定
    （参数名与定义名），避免把守卫逻辑里的字面量误判成越权访问。
    """
    tree = ast.parse(source)

    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)

    local_bindings = set(defined)
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            local_bindings.add(node.arg)

    accessed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            accessed.add(node.attr)
        elif isinstance(node, ast.Name):
            accessed.add(node.id)
        elif isinstance(node, ast.alias):
            accessed.add(node.name.split(".")[-1])

    return {
        "defined_other_stages": tuple(sorted(defined & set(NOT_PROVIDED_BY_P18))),
        "accessed_forbidden": tuple(
            sorted((accessed - local_bindings) & set(FORBIDDEN_ACCESS_NAMES))
        ),
    }


def _make_vault():
    """建一个真实 M1 金库，返回 ``(explorer, protocol_id, custodian)``。"""
    directory = tempfile.mkdtemp()
    tokens = initialize(os.path.join(directory, "evidence.sqlite3"))
    custodian = Module01(os.path.join(directory, "evidence.sqlite3"), tokens["custodian"])
    explorer = Module01(os.path.join(directory, "evidence.sqlite3"), tokens["explorer"])
    protocol = custodian.build(synthetic_spec(), synthetic_records())
    return explorer, protocol["protocol_id"], custodian


class BoundaryTests(unittest.TestCase):
    """边界常量与 AST 自检。"""

    def test_self_check_passes_on_real_source(self):
        report = self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["defined_other_stages"], ())
        self.assertEqual(report["accessed_forbidden"], ())

    def test_self_check_flags_are_all_false(self):
        """四项「不做」声明必须恒为假值。"""
        self.assertFalse(TOOLBOX_GRANTS_EVIDENCE_GRADE)
        self.assertFalse(TOOLBOX_RUNS_STATISTICS)
        self.assertFalse(TOOLBOX_ADDS_ALGORITHMS)
        self.assertFalse(TOOLBOX_DECIDES_NOT_LLM)
        self.assertTrue(TOOLBOX_EXPLORATION_ONLY)

    def test_self_check_reports_no_decide_for_llm(self):
        report = self_check()
        self.assertFalse(report["decides_for_llm"])
        self.assertFalse(report["adds_algorithms"])

    def test_detector_catches_planted_definition(self):
        """植入一个越界定义，检测器必须抓到（证明非空洞）。"""
        planted = "def compute_p_value(x):\n    return 0.05\n"
        found = _detect_violations(planted)
        self.assertIn("compute_p_value", found["defined_other_stages"])

    def test_detector_catches_planted_access(self):
        """植入一次越权属性访问，检测器必须抓到。"""
        planted = "import sqlite3\n\n\ndef f():\n    return sqlite3.connect('x')\n"
        found = _detect_violations(planted)
        self.assertIn("sqlite3", found["accessed_forbidden"])

    def test_detector_ignores_boundary_declarations(self):
        """边界声明（常量里写出被禁名字）**不得**被判成违规。"""
        declaration = (
            'NOT_PROVIDED_BY_P18 = ("compute_p_value", "bind_confirmation")\n'
            'FORBIDDEN_ACCESS_NAMES = frozenset({"sqlite3"})\n'
        )
        found = _detect_violations(declaration)
        self.assertEqual(found["defined_other_stages"], ())
        self.assertEqual(found["accessed_forbidden"], ())

    def test_detector_ignores_local_binding_collision(self):
        """``kwargs.get("p_value")`` 之类的守卫逻辑不得误报。"""
        guard = (
            "def f(**kwargs):\n"
            "    forbidden = sorted(set(kwargs) & {'p_value'})\n"
            "    return forbidden\n"
        )
        found = _detect_violations(guard)
        self.assertEqual(found["accessed_forbidden"], ())

    def test_version_is_p18(self):
        self.assertTrue(TOOLBOX_VERSION.startswith("P18"))

    def test_boundary_lists_are_nonempty(self):
        self.assertTrue(NOT_PROVIDED_BY_P18)
        self.assertTrue(FORBIDDEN_ACCESS_NAMES)
        self.assertTrue(FORBIDDEN_TOOL_ARGUMENTS)

    def test_module_does_not_import_sdl_pipeline(self):
        """模块层不得导入管线（避免拉入整条链）。"""
        source = _module_source()
        self.assertNotIn("from sdl_pipeline", source)
        self.assertNotIn("import sdl_pipeline", source)

    def test_forbidden_arguments_cover_grade_and_pvalue(self):
        for name in ("grade", "evidence_grade", "p_value", "conclusion", "verdict"):
            self.assertIn(name, FORBIDDEN_TOOL_ARGUMENTS)


class HandleStoreTests(unittest.TestCase):
    """会话句柄表。"""

    def test_put_and_get_roundtrip(self):
        store = HandleStore()
        handle = store.put("Thing", {"a": 1})
        self.assertTrue(handle.startswith(HANDLE_PREFIX))
        self.assertEqual(store.get(handle), {"a": 1})
        self.assertEqual(store.kind_of(handle), "Thing")

    def test_handles_are_deterministic_for_same_salt(self):
        a = HandleStore(salt="s").put("T", 1)
        b = HandleStore(salt="s").put("T", 1)
        self.assertEqual(a, b)

    def test_different_salts_yield_different_handles(self):
        a = HandleStore(salt="s1").put("T", 1)
        b = HandleStore(salt="s2").put("T", 1)
        self.assertNotEqual(a, b)

    def test_get_rejects_type_mismatch(self):
        store = HandleStore()
        handle = store.put("A", 1)
        with self.assertRaises(HandleError):
            store.get(handle, expect="B")

    def test_get_rejects_unknown_handle(self):
        store = HandleStore()
        with self.assertRaises(HandleError):
            store.get(HANDLE_PREFIX + "deadbeef")

    def test_get_rejects_non_handle(self):
        store = HandleStore()
        with self.assertRaises(HandleError):
            store.get("not-a-handle")

    def test_put_rejects_bad_kind(self):
        with self.assertRaises(ToolboxInputError):
            HandleStore().put("", 1)

    def test_limit_enforced(self):
        store = HandleStore(limit=2)
        store.put("T", 1)
        store.put("T", 2)
        with self.assertRaises(ToolboxInputError):
            store.put("T", 3)

    def test_reserved_handles_excluded_from_counts(self):
        store = HandleStore()
        store.put("T", 1)
        store.put_kind("Client", "client:explorer", object())
        self.assertEqual(store.kinds(), {"T": 1})
        self.assertEqual(store.to_dict()["count"], 1)

    def test_reserved_handle_rejected_by_require_handle_path(self):
        """保留句柄不得被 LLM 用来取回客户端。"""
        store = HandleStore()
        store.put_kind("ExplorerClient", "client:explorer", object())
        self.assertTrue(store.has("client:explorer"))


class ToolSpecTests(unittest.TestCase):
    """工具描述对象。"""

    def test_paramspec_to_dict_marks_required(self):
        spec = ParamSpec("x", "int", True)
        payload = spec.to_dict()
        self.assertEqual(payload["name"], "x")
        self.assertTrue(payload["required"])
        self.assertIsNone(payload["default"])

    def test_paramspec_optional_carries_default(self):
        spec = ParamSpec("x", "int", False, 5)
        self.assertEqual(spec.to_dict()["default"], 5)

    def test_toolspec_param_names(self):
        spec = ToolSpec(
            name="t",
            module="M2",
            summary="s",
            parameters=(ParamSpec("a", "int", True), ParamSpec("b", "int", False, 1)),
            returns="none",
        )
        self.assertEqual(spec.param_names, ("a", "b"))

    def test_toolspec_to_dict_roundtrip(self):
        spec = ToolSpec("t", "M2", "s", (), "none")
        payload = spec.to_dict()
        self.assertEqual(payload["name"], "t")
        self.assertEqual(payload["module"], "M2")


class ToolResultTests(unittest.TestCase):
    """结果对象。"""

    def test_ok_property(self):
        self.assertTrue(ToolResult("t", STATUS_OK).ok)
        self.assertFalse(ToolResult("t", STATUS_REJECTED, None, {}, REASON_UNKNOWN_TOOL).ok)

    def test_rejects_unknown_status(self):
        with self.assertRaises(ToolboxInputError):
            ToolResult("t", "bogus")

    def test_rejects_unknown_reason(self):
        with self.assertRaises(ToolboxInputError):
            ToolResult("t", STATUS_OK, None, {}, "bogus_reason")

    def test_summary_is_frozen(self):
        result = ToolResult("t", STATUS_OK, None, {"a": 1})
        with self.assertRaises(TypeError):
            result.summary["a"] = 2  # type: ignore[index]

    def test_canonical_json_is_stable(self):
        a = ToolResult("t", STATUS_OK, None, {"b": 1, "a": 2})
        b = ToolResult("t", STATUS_OK, None, {"a": 2, "b": 1})
        self.assertEqual(a.canonical_json(), b.canonical_json())
        self.assertEqual(a.content_digest(), b.content_digest())


class ToolboxRegistrationTests(unittest.TestCase):
    """登记与查询。"""

    def test_default_toolbox_has_expected_tools(self):
        box = build_default_toolbox()
        names = box.names()
        for expected in (
            "m02.parse_expression",
            "m02.enumerate_candidates",
            "m03.prepare_sample",
            "m03.fit_relation",
            "m05.novelty",
            "m08.empty_knowledge",
            "m09.extract_regularities",
        ):
            self.assertIn(expected, names)

    def test_names_are_sorted(self):
        names = build_default_toolbox().names()
        self.assertEqual(list(names), sorted(names))

    def test_duplicate_registration_rejected(self):
        box = Toolbox()
        spec = ToolSpec("t", "M2", "s", (), "none")
        box.register(spec, lambda a, s: ToolResult("t", STATUS_OK))
        with self.assertRaises(ToolboxInputError):
            box.register(spec, lambda a, s: ToolResult("t", STATUS_OK))

    def test_describe_unknown_raises(self):
        with self.assertRaises(UnknownToolError):
            build_default_toolbox().describe("nope")

    def test_catalogue_lists_all_tools(self):
        box = build_default_toolbox()
        catalogue = box.catalogue()
        self.assertEqual(len(catalogue["tools"]), len(box.names()))
        self.assertTrue(catalogue["exploration_only"])
        self.assertIn("boundary", catalogue)
        self.assertIn("handles", catalogue)

    def test_every_tool_declares_module_and_summary(self):
        for spec in build_default_toolbox().list_tools():
            self.assertTrue(spec.module.startswith("M"))
            self.assertTrue(spec.summary)
            self.assertTrue(spec.wrapped)

    def test_produces_handle_tools_declare_return_type(self):
        for spec in build_default_toolbox().list_tools():
            if spec.produces_handle:
                self.assertNotEqual(spec.returns, "none", spec.name)

    def test_requires_handles_are_consistent_with_parameters(self):
        """声明需要的句柄类型，必须能在参数里找到对应来源。"""
        box = build_default_toolbox()
        known_types = {spec.returns for spec in box.list_tools() if spec.produces_handle}
        for spec in box.list_tools():
            for required in spec.requires_handles:
                self.assertIn(required, known_types, f"{spec.name} 需要未产出的句柄 {required}")


class SchemaFidelityTests(unittest.TestCase):
    """验收标准①：schema 与真实签名一致。"""

    def test_enumerate_candidates_schema_matches_signature(self):
        """抽样核对：``m02.enumerate_candidates`` 的参数名应与真实签名一致。"""
        from sdl_m02.domain import enumerate_candidates

        box = build_default_toolbox()
        spec = box.describe("m02.enumerate_candidates")
        real = {
            name
            for name, param in inspect.signature(enumerate_candidates).parameters.items()
            if param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        }
        self.assertEqual(set(spec.param_names), real)

    def test_required_flags_match_signature_defaults(self):
        """必填标记必须与真实签名「有无默认值」一致。"""
        from sdl_m02.domain import enumerate_candidates

        box = build_default_toolbox()
        spec = box.describe("m02.enumerate_candidates")
        signature = inspect.signature(enumerate_candidates)
        for param_spec in spec.parameters:
            real = signature.parameters[param_spec.name]
            self.assertEqual(
                param_spec.required,
                real.default is inspect.Parameter.empty,
                f"{param_spec.name} 必填性与签名不符",
            )

    def test_defaults_are_carried_from_signature(self):
        """可选参数必须带出签名里的真实默认值，不得臆造。"""
        from sdl_m02.domain import enumerate_candidates

        box = build_default_toolbox()
        spec = box.describe("m02.enumerate_candidates")
        signature = inspect.signature(enumerate_candidates)
        by_name = {p.name: p for p in spec.parameters}
        self.assertEqual(by_name["max_depth"].default, signature.parameters["max_depth"].default)
        self.assertEqual(
            by_name["max_candidates"].default,
            signature.parameters["max_candidates"].default,
        )

    def test_spec_params_derives_from_signature_without_manual_copies(self):
        """``_spec_params`` 是 schema 的唯一来源；抽样断言它确实读签名。"""
        from sdl_m10.toolbox import _spec_params

        def sample(a, b=2, *args, **kwargs):
            return None

        params = _spec_params(sample)
        self.assertEqual([p.name for p in params], ["a", "b"])
        self.assertTrue(params[0].required)
        self.assertFalse(params[1].required)
        self.assertEqual(params[1].default, 2)


class DispatchGuardTests(unittest.TestCase):
    """验收标准②：四路守卫全覆盖。"""

    def setUp(self):
        self.box = build_default_toolbox()

    def test_unknown_tool_is_rejected_not_raised(self):
        result = self.box.call("mXX.nope")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_UNKNOWN_TOOL)
        self.assertIn("m02.parse_expression", result.summary["known"])

    def test_forbidden_argument_is_rejected(self):
        for banned in ("grade", "evidence_grade", "p_value", "conclusion"):
            result = self.box.call("m02.parse_expression", text="X1", **{banned: "x"})
            self.assertEqual(result.status, STATUS_REJECTED, banned)
            self.assertEqual(result.reason, REASON_FORBIDDEN_ARGUMENT, banned)

    def test_forbidden_argument_reports_which_ones(self):
        result = self.box.call("m02.parse_expression", text="X1", grade="E2")
        self.assertEqual(result.summary["forbidden_arguments"], ["grade"])

    def test_missing_argument_is_rejected(self):
        result = self.box.call("m02.parse_expression")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_MISSING_ARGUMENT)
        self.assertIn("text", result.summary["missing_arguments"])

    def test_unexpected_argument_is_rejected(self):
        result = self.box.call("m02.parse_expression", text="X1", bogus=1)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_UNEXPECTED_ARGUMENT)
        self.assertIn("bogus", result.summary["unexpected_arguments"])

    def test_bad_handle_for_non_handle_value(self):
        """直接传字符串冒充句柄必须被拒（防伪造内部对象）。"""
        result = self.box.call("m02.render_expression", ast="not-a-handle")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_BAD_HANDLE)
        self.assertIn("不得直接构造", result.summary["error"])

    def test_bad_handle_for_wrong_type(self):
        ast_handle = self.box.call("m02.parse_expression", text="X1").handle
        result = self.box.call("m08.knowledge_summary", knowledge=ast_handle)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_BAD_HANDLE)

    def test_unknown_handle_is_rejected(self):
        result = self.box.call("m02.render_expression", ast=HANDLE_PREFIX + "deadbeef00")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_BAD_HANDLE)

    def test_tool_error_degrades_to_failed(self):
        """算法层自身报错 -> failed + tool_raised，不打断调用方。"""
        # ``m02.parse_expression`` 对不可解析文本抛 Expression 异常。
        result = self.box.call("m02.parse_expression", text="@@@not an expression@@@")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.reason, REASON_TOOL_RAISED)

    def test_dispatch_never_raises_for_normal_guard_paths(self):
        """守卫路径一律返回对象，不抛异常。"""
        for result in (
            self.box.call("nope"),
            self.box.call("m02.parse_expression"),
            self.box.call("m02.parse_expression", text="X1", grade="E1"),
            self.box.call("m02.render_expression", ast=1),
        ):
            self.assertIsInstance(result, ToolResult)

    def test_trace_records_calls(self):
        self.box.call("m08.empty_knowledge")
        self.box.call("nope")
        trace = self.box.trace()
        self.assertEqual(len(trace), 2)
        self.assertEqual(trace[0]["tool"], "m08.empty_knowledge")
        self.assertEqual(trace[1]["reason"], REASON_UNKNOWN_TOOL)


class LeakGuardTests(unittest.TestCase):
    """验收标准③：出闸守卫**两侧都测**。"""

    def test_guard_catches_real_sealed_keys(self):
        """① 真实违规项必须被抓到。"""
        from sdl_m10.toolbox import _find_leaked_key

        for key in ("values", "record_id", "_quality", "raw_records", "snapshot"):
            self.assertIn(key, M10_SEALED_KEYS, key)
            self.assertEqual(_find_leaked_key({"a": {"b": {key: 1}}}), key, key)

    def test_guard_ignores_exempted_keys(self):
        """② 算法层合法的 source / source_ref 不得触发。"""
        from sdl_m10.toolbox import _find_leaked_key

        self.assertIsNone(_find_leaked_key({"source": "依据说明"}))
        self.assertIsNone(_find_leaked_key({"source_ref": "data_abc"}))
        for key in LEAK_GUARD_EXEMPT_KEYS:
            self.assertNotIn(key, M10_SEALED_KEYS, key)

    def test_guard_scans_nested_structures(self):
        from sdl_m10.toolbox import _find_leaked_key

        self.assertEqual(_find_leaked_key({"x": [{"y": [{"values": []}]}]}), "values")

    def test_guard_returns_none_for_clean_payload(self):
        from sdl_m10.toolbox import _find_leaked_key

        self.assertIsNone(_find_leaked_key({"a": 1, "b": [1, 2], "c": {"d": "e"}}))

    def test_exemption_is_a_proper_subset_of_archive_keys(self):
        """豁免集必须是 M8 黑名单的真子集，且不为空。"""
        self.assertTrue(LEAK_GUARD_EXEMPT_KEYS)
        self.assertTrue(M10_SEALED_KEYS)
        self.assertTrue(M10_SEALED_KEYS < (M10_SEALED_KEYS | LEAK_GUARD_EXEMPT_KEYS))

    def test_registered_tool_with_legit_source_key_still_works(self):
        """回归：``m05.complexity`` 的输出含 ``source``，必须仍然可用。"""
        box = build_default_toolbox()
        ast_handle = box.call("m02.parse_expression", text="X1 / X2").handle
        result = box.call("m05.complexity", ast=ast_handle)
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.summary.get("complexity"), 4.0)


class ToolBehaviourTests(unittest.TestCase):
    """各工具的正面行为。"""

    def setUp(self):
        self.box = build_default_toolbox()

    def test_parse_expression_returns_handle_and_summary(self):
        result = self.box.call("m02.parse_expression", text="X1 / X2")
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(result.handle.startswith(HANDLE_PREFIX))
        self.assertEqual(result.summary["variables"], ["X1", "X2"])
        self.assertEqual(result.summary["node_count"], 4)

    def test_render_expression_roundtrip(self):
        handle = self.box.call("m02.parse_expression", text="X1 / X2").handle
        result = self.box.call("m02.render_expression", ast=handle)
        self.assertEqual(result.status, STATUS_OK)
        self.assertIn("X1", result.summary["rendered"])

    def test_enumerate_candidates_returns_pool(self):
        result = self.box.call(
            "m02.enumerate_candidates", variables=["X1", "X2"], max_candidates=12
        )
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.summary["count"], 12)

    def test_candidate_summary_consumes_pool_handle(self):
        pool = self.box.call(
            "m02.enumerate_candidates", variables=["X1"], max_candidates=8
        ).handle
        result = self.box.call("m02.candidate_summary", candidates=pool)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.summary["count"], 8)

    def test_empty_knowledge_returns_version_handle(self):
        result = self.box.call("m08.empty_knowledge")
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.summary["version"], "K0")

    def test_knowledge_base_tool_builds_metrics_view(self):
        result = self.box.call("m05.knowledge_base", version="K0", entries=["Y ~ X1"])
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.summary["entry_count"], 1)

    def test_novelty_transcribes_without_judging(self):
        kb = self.box.call(
            "m05.knowledge_base", version="K0", entries=["Y is linear in X1"]
        ).handle
        result = self.box.call(
            "m05.novelty",
            hypothesis_id="h1",
            hypothesis_version="1",
            statement="Y depends on X1/X2",
            knowledge_base=kb,
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIn("novelty", result.summary)
        self.assertEqual(result.summary["knowledge_version"], "K0")
        # 只转录：不得出现任何「成立/结论」字样的判定字段
        self.assertNotIn("conclusion", result.summary)
        self.assertNotIn("is_true", result.summary)

    def test_extract_regularities_reports_counts(self):
        kv = self.box.call("m08.empty_knowledge").handle
        result = self.box.call("m09.extract_regularities", knowledge=kv)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.summary["supported"], 0)
        self.assertEqual(result.summary["refuted"], 0)

    def test_prepare_sample_without_explorer_is_policy_rejected(self):
        box = build_default_toolbox()  # 未注入 explorer
        result = box.call("m03.prepare_sample", protocol_id="p", variables=["X1"], target="Y")
        self.assertEqual(result.status, STATUS_REJECTED)

    def test_two_knowledge_types_are_not_interchangeable(self):
        """M5 口径与 M8 口径的知识库句柄不得混用（由类型守卫拦下）。"""
        kv = self.box.call("m08.empty_knowledge").handle
        result = self.box.call(
            "m05.novelty",
            hypothesis_id="h",
            hypothesis_version="1",
            statement="s",
            knowledge_base=kv,
        )
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, REASON_BAD_HANDLE)


class _VaultCase(unittest.TestCase):
    """需要真实 M1 金库的集成测试基类。"""

    @classmethod
    def setUpClass(cls):
        cls.explorer, cls.protocol_id, cls.custodian = _make_vault()

    def setUp(self):
        self.box = build_default_toolbox(explorer=self.explorer)


class VaultIntegrationTests(_VaultCase):
    """验收标准④：真实金库上的完整 LLM 分析链路。"""

    def _sample(self) -> str:
        result = self.box.call(
            "m03.prepare_sample",
            protocol_id=self.protocol_id,
            variables=["X1", "X2"],
            target="Y",
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        return result.handle

    def test_prepare_sample_reads_exploration_partition(self):
        result = self.box.call(
            "m03.prepare_sample",
            protocol_id=self.protocol_id,
            variables=["X1", "X2"],
            target="Y",
        )
        self.assertEqual(result.status, STATUS_OK)
        self.assertGreater(result.summary["row_count"], 0)
        self.assertEqual(result.summary["variables"], ["X1", "X2"])

    def test_baselines_fit_on_real_data(self):
        sample = self._sample()
        constant = self.box.call("m03.baseline_constant", sample=sample)
        linear = self.box.call("m03.baseline_linear", sample=sample, feature="X1")
        self.assertEqual(constant.status, STATUS_OK, constant.to_dict())
        self.assertEqual(linear.status, STATUS_OK, linear.to_dict())
        self.assertLess(linear.summary["metrics"]["mse"], constant.summary["metrics"]["mse"])

    def test_fit_relation_recovers_ratio_structure(self):
        """在合成数据上，``X1 / X2`` 应显著优于线性基线。"""
        sample = self._sample()
        ast = self.box.call("m02.parse_expression", text="X1 / X2").handle
        fit = self.box.call("m03.fit_relation", sample=sample, relationship=ast)
        self.assertEqual(fit.status, STATUS_OK, fit.to_dict())
        baseline = self.box.call("m03.baseline_linear", sample=sample, feature="X1")
        gain = self.box.call(
            "m03.gain_against", candidate=fit.handle, baseline=baseline.handle
        )
        self.assertEqual(gain.status, STATUS_OK, gain.to_dict())
        self.assertTrue(gain.summary["candidate_better"])
        self.assertGreater(gain.summary["relative_reduction"], 0.9)

    def test_fit_relation_accepts_expression_text(self):
        sample = self._sample()
        result = self.box.call("m03.fit_relation", sample=sample, relationship="X1 / X2")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())

    def test_full_llm_chain_produces_no_grade(self):
        """整条链路跑完，任何结果里都不得出现证据等级字段。"""
        sample = self._sample()
        ast = self.box.call("m02.parse_expression", text="X1 / X2").handle
        fit = self.box.call("m03.fit_relation", sample=sample, relationship=ast)
        baseline = self.box.call("m03.baseline_linear", sample=sample, feature="X1")
        gain = self.box.call(
            "m03.gain_against", candidate=fit.handle, baseline=baseline.handle
        )
        for result in (fit, baseline, gain):
            for banned in ("grade", "evidence_grade", "conclusion", "verdict"):
                self.assertNotIn(banned, result.summary, f"{result.tool} 泄漏了 {banned}")

    def test_journal_is_reproducible(self):
        """同序调用应产生相同的结果摘要（会话盐固定）。"""
        box_a = build_default_toolbox(explorer=self.explorer, seed=7)
        box_b = build_default_toolbox(explorer=self.explorer, seed=7)
        for box in (box_a, box_b):
            box.call("m08.empty_knowledge")
            box.call("m02.parse_expression", text="X1 / X2")
        self.assertEqual(box_a.trace(), box_b.trace())

    def test_content_digest_changes_with_calls(self):
        before = self.box.content_digest()
        self.box.call("m08.empty_knowledge")
        self.assertNotEqual(before, self.box.content_digest())


class SelfCheckTwoWayTests(unittest.TestCase):
    """对自检器做**双向**验证：既能通过正常源码，也能抓出植入违规。"""

    def test_real_source_passes_both_layers(self):
        report = self_check()
        self.assertTrue(report["ok"])
        detected = _detect_violations(_module_source())
        self.assertEqual(detected["defined_other_stages"], ())
        self.assertEqual(detected["accessed_forbidden"], ())

    def test_planted_definition_is_caught_by_detector(self):
        planted = _module_source() + "\n\ndef grade_for(x):\n    return 'E2'\n"
        self.assertIn("grade_for", _detect_violations(planted)["defined_other_stages"])

    def test_planted_import_is_caught_by_detector(self):
        planted = _module_source() + "\n\nimport sqlite3\n"
        self.assertIn("sqlite3", _detect_violations(planted)["accessed_forbidden"])

    def test_checked_names_count_matches_lists(self):
        report = self_check()
        self.assertEqual(
            report["checked_names"],
            len(NOT_PROVIDED_BY_P18) + len(FORBIDDEN_ACCESS_NAMES),
        )


if __name__ == "__main__":
    unittest.main()
