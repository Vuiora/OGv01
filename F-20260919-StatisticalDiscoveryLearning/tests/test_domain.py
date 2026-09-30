"""P02 验收测试：候选变量枚举与定义域追踪。

覆盖三条验收标准：

1. 对给定的 3 变量输入，枚举结果在声明上限内且不含重复语义；
2. 除法候选的定义域显式记录 ``|X2| > ε`` 一类条件（并覆盖开方非负、取对数正）；
3. 定义域检查为纯函数，不依赖数据集。

同时守护 P02 的边界：不实现去重剪枝（属 P03）、不接触任何数据分区、
不对候选表达式做数据求值。
"""

import json
import math
import unittest

from sdl_m02.domain import (
    DEFAULT_EPS,
    DEFAULT_MAX_CANDIDATES,
    DomainCondition,
    DomainError,
    DomainSpec,
    Candidate,
    collect_domain_conditions,
    domain_ok,
    enumerate_candidates,
)
from sdl_m02.expressions import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    parse,
    to_canonical_json,
)

VARIABLES = ["X1", "X2", "X3"]


class TestEnumerateBoundedAndUnique(unittest.TestCase):
    """验收标准①：枚举有上界，且不含重复语义。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.candidates = enumerate_candidates(VARIABLES)

    def test_returns_nonempty_list(self) -> None:
        self.assertIsInstance(self.candidates, list)
        self.assertGreater(len(self.candidates), 0)

    def test_all_items_are_candidates(self) -> None:
        for item in self.candidates:
            self.assertIsInstance(item, Candidate)

    def test_respects_declared_upper_bound(self) -> None:
        """候选数量不超过声明的 max_candidates 上界。"""
        self.assertLessEqual(len(self.candidates), DEFAULT_MAX_CANDIDATES)

    def test_custom_upper_bound_is_obeyed(self) -> None:
        """把上界压到很小，返回值长度仍不得超过它。"""
        small = enumerate_candidates(VARIABLES, max_candidates=25)
        self.assertLessEqual(len(small), 25)
        self.assertGreater(len(small), 0)

    def test_no_duplicate_semantics(self) -> None:
        """任意两个候选的规范式不同 —— 无重复语义。"""
        keys = [c.expression_key() for c in self.candidates]
        self.assertEqual(len(keys), len(set(keys)), "枚举结果中存在语义重复的候选")

    def test_candidate_ids_are_unique_and_stable(self) -> None:
        """candidate_id 唯一，且同一语义恒得同一 id。"""
        ids = [c.candidate_id for c in self.candidates]
        self.assertEqual(len(ids), len(set(ids)))
        again = enumerate_candidates(VARIABLES)
        self.assertEqual(ids, [c.candidate_id for c in again])

    def test_enumeration_is_deterministic(self) -> None:
        """同样的输入两次调用得到完全相同的序列。"""
        again = enumerate_candidates(VARIABLES)
        self.assertEqual(
            [c.expression_key() for c in self.candidates],
            [c.expression_key() for c in again],
        )

    def test_variable_order_does_not_change_the_set(self) -> None:
        """变量声明顺序变化不改变候选集合（只可能改变排序输入）。"""
        shuffled = enumerate_candidates(["X3", "X1", "X2"])
        self.assertEqual(
            {c.expression_key() for c in self.candidates},
            {c.expression_key() for c in shuffled},
        )

    def test_respects_depth_and_node_budgets(self) -> None:
        """每个候选都落在深度与节点数预算内。"""
        for candidate in self.candidates:
            self.assertLessEqual(candidate.depth, DEFAULT_MAX_DEPTH, candidate.candidate_id)
            self.assertLessEqual(candidate.node_count, DEFAULT_MAX_NODES, candidate.candidate_id)

    def test_expressions_are_canonical_and_serializable(self) -> None:
        """expression 是 JSON 兼容编码，且已经过规范化。"""
        for candidate in self.candidates:
            payload = json.loads(json.dumps(candidate.expression))
            # 规范式的编码再解析回来，应与候选自身的规范式一致。
            self.assertEqual(candidate.expression, payload)
            self.assertEqual(candidate.expression_key(), to_canonical_json(candidate.ast))

    def test_covers_three_variables_and_arithmetic(self) -> None:
        """3 变量输入的枚举覆盖到加、乘、幂与一元变换等结构。"""
        kinds = {c.ast[0] for c in self.candidates}
        for expected in ("var", "num", "add", "mul", "pow", "inv", "neg"):
            self.assertIn(expected, kinds, f"枚举结果缺少 {expected} 结构")
        used = set()
        for candidate in self.candidates:
            used |= _variables_of(candidate.ast)
        self.assertEqual(used, set(VARIABLES))

    def test_invalid_variables_rejected(self) -> None:
        with self.assertRaises(DomainError):
            enumerate_candidates([])
        with self.assertRaises(DomainError):
            enumerate_candidates(["X1", "X1"])
        with self.assertRaises(DomainError):
            enumerate_candidates("X1")  # 字符串不是变量名序列


class TestDivisionCarriesDomain(unittest.TestCase):
    """验收标准②：除法等候选显式携带定义域条件。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.candidates = enumerate_candidates(VARIABLES)
        cls.inv_candidates = [c for c in cls.candidates if _contains_inv(c.ast)]

    def test_enumeration_actually_contains_division_candidates(self) -> None:
        self.assertGreater(len(self.inv_candidates), 0, "枚举结果中没有除法（inv）候选")

    def test_every_division_candidate_records_nonzero_condition(self) -> None:
        """每个含除法的候选，其定义域都必须包含 nonzero 条件，且不含空定义域。"""
        for candidate in self.inv_candidates:
            kinds = [cond.kind for cond in candidate.domain.conditions]
            self.assertIn("nonzero", kinds, candidate.candidate_id)
            self.assertFalse(candidate.domain.is_empty, candidate.candidate_id)

    def test_nonzero_condition_targets_the_denominator_exactly(self) -> None:
        """``X1 / X2`` 的条件应精确落在分母 ``X2`` 上，并写明 |X2| > ε。"""
        spec = DomainSpec.for_expression(parse("X1 / X2"))
        nonzero = [cond for cond in spec.conditions if cond.kind == "nonzero"]
        self.assertEqual(len(nonzero), 1)
        self.assertEqual(nonzero[0].operand, ("var", "X2"))
        self.assertEqual(nonzero[0].variables, ("X2",))
        self.assertIn("|X2| >", nonzero[0].render(spec.epsilon))

    def test_epsilon_is_recorded_in_the_condition_text(self) -> None:
        spec = DomainSpec.for_expression(parse("X1 / X2"))
        self.assertIn(f"{DEFAULT_EPS:g}", spec.describe())

    def test_sqrt_requires_nonnegative(self) -> None:
        spec = DomainSpec.for_expression(parse("sqrt(X2)"))
        kinds = [cond.kind for cond in spec.conditions]
        self.assertEqual(kinds, ["nonnegative"])
        self.assertEqual(spec.conditions[0].operand, ("var", "X2"))

    def test_log_requires_positive(self) -> None:
        spec = DomainSpec.for_expression(parse("log(X2)"))
        kinds = [cond.kind for cond in spec.conditions]
        self.assertEqual(kinds, ["positive"])
        self.assertEqual(spec.conditions[0].operand, ("var", "X2"))

    def test_abs_and_exp_produce_no_conditions(self) -> None:
        for text in ("abs(X1)", "exp(X2)", "X1 + X2", "3 * X1", "X1 ^ 2"):
            spec = DomainSpec.for_expression(parse(text))
            self.assertTrue(spec.is_empty, f"{text} 不应产生定义域条件")

    def test_nested_conditions_are_collected_together(self) -> None:
        """``log(X1) / sqrt(X2)`` 应同时记录三种条件。

        注意：该表达式的规范式深度为 4，超出候选枚举的深度预算（3），
        故此处显式放宽 ``max_depth``——定义域收集与规模预算彼此正交，
        本用例只检验前者。
        """
        spec = DomainSpec.for_expression(parse("log(X1) / sqrt(X2)", max_depth=4))
        kinds = sorted(cond.kind for cond in spec.conditions)
        self.assertEqual(kinds, ["nonnegative", "nonzero", "positive"])

    def test_conditions_are_deduplicated_and_ordered(self) -> None:
        """同一条件重复出现只保留一条，且顺序确定。"""
        spec = DomainSpec.for_expression(parse("(X1 / X2) + (X3 / X2)", max_depth=4))
        nonzero_operands = [
            to_canonical_json(cond.operand)
            for cond in spec.conditions
            if cond.kind == "nonzero"
        ]
        self.assertEqual(nonzero_operands, ['["var","X2"]'])
        # 顺序确定：重复构造得到相同序列
        again = DomainSpec.for_expression(parse("(X1 / X2) + (X3 / X2)", max_depth=4))
        self.assertEqual(
            [c.kind for c in spec.conditions], [c.kind for c in again.conditions]
        )

    def test_two_kinds_within_depth_budget(self) -> None:
        """深度预算内的表达式同样能携带多类条件（``1 / sqrt(X1)``）。"""
        spec = DomainSpec.for_expression(parse("1 / sqrt(X1)"))
        self.assertEqual(
            sorted(cond.kind for cond in spec.conditions), ["nonnegative", "nonzero"]
        )

    def test_domain_describe_is_readable(self) -> None:
        spec = DomainSpec.for_expression(parse("X1 / X2"))
        self.assertIn("|X2| >", spec.describe())
        self.assertEqual(DomainSpec.for_expression(parse("X1 + X2")).describe(), "全实数")

    def test_condition_rejects_unknown_kind(self) -> None:
        with self.assertRaises(DomainError):
            DomainCondition(kind="whatever", operand=("var", "X1"))

    def test_domain_spec_rejects_bad_epsilon(self) -> None:
        for bad in (0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(DomainError):
                DomainSpec(conditions=(), epsilon=bad)


class TestDomainOkIsPure(unittest.TestCase):
    """验收标准③：定义域检查为纯函数，不依赖数据集。"""

    def test_division_holds_away_from_zero(self) -> None:
        candidate = _candidate_for("1 / X1")
        self.assertTrue(domain_ok(candidate, {"X1": 2.0}))
        self.assertFalse(domain_ok(candidate, {"X1": 0.0}))
        self.assertFalse(domain_ok(candidate, {"X1": 1e-15}))
        self.assertTrue(domain_ok(candidate, {"X1": -3.0}))

    def test_sqrt_domain(self) -> None:
        candidate = _candidate_for("sqrt(X1)")
        self.assertTrue(domain_ok(candidate, {"X1": 4.0}))
        self.assertTrue(domain_ok(candidate, {"X1": 0.0}))
        self.assertFalse(domain_ok(candidate, {"X1": -1.0}))

    def test_log_domain(self) -> None:
        candidate = _candidate_for("log(X1)")
        self.assertTrue(domain_ok(candidate, {"X1": 1.0}))
        self.assertFalse(domain_ok(candidate, {"X1": 0.0}))
        self.assertFalse(domain_ok(candidate, {"X1": -2.0}))

    def test_unconditional_expression_always_ok(self) -> None:
        candidate = _candidate_for("X1 + X2")
        self.assertTrue(candidate.domain.is_empty)
        for point in ({"X1": 0.0, "X2": 0.0}, {"X1": 1e300, "X2": -5.0}):
            self.assertTrue(domain_ok(candidate, point))

    def test_accepts_domain_spec_and_bare_ast(self) -> None:
        ast = parse("1 / X2")
        spec = DomainSpec.for_expression(ast)
        self.assertTrue(domain_ok(spec, {"X2": 1.0}))
        self.assertFalse(domain_ok(spec, {"X2": 0.0}))
        self.assertTrue(domain_ok(ast, {"X2": 1.0}))
        self.assertFalse(domain_ok(ast, {"X2": 0.0}))

    def test_repeated_calls_are_deterministic(self) -> None:
        """纯函数：同一输入反复调用结果一致，且不依赖调用顺序。"""
        candidate = _candidate_for("log(X1) / X2")
        point = {"X1": 3.0, "X2": 2.0}
        results = [domain_ok(candidate, point) for _ in range(5)]
        self.assertEqual(results, [True] * 5)

    def test_point_is_not_mutated(self) -> None:
        """检查过程不得改写调用方传入的取值点。"""
        candidate = _candidate_for("sqrt(X1) / X2")
        point = {"X1": 4.0, "X2": 2.0}
        snapshot = dict(point)
        domain_ok(candidate, point)
        self.assertEqual(point, snapshot)

    def test_missing_variable_is_a_caller_error(self) -> None:
        candidate = _candidate_for("1 / X2")
        with self.assertRaises(DomainError):
            domain_ok(candidate, {"X1": 1.0})

    def test_non_numeric_value_is_rejected(self) -> None:
        candidate = _candidate_for("1 / X2")
        for bad in ("x", True, None, float("nan")):
            with self.assertRaises(DomainError):
                domain_ok(candidate, {"X2": bad})

    def test_invalid_point_type_is_rejected(self) -> None:
        candidate = _candidate_for("1 / X2")
        with self.assertRaises(DomainError):
            domain_ok(candidate, [1.0, 2.0])  # type: ignore[arg-type]

    def test_none_point_is_a_structural_check_only(self) -> None:
        """不给取值点时，只判定与取值无关的常量条件。"""
        ok_candidate = _candidate_for("1 / X1")
        self.assertTrue(domain_ok(ok_candidate, None))

        empty_domain = _candidate_for("1 / 0")
        self.assertFalse(empty_domain.domain.symbolically_ok())
        self.assertFalse(domain_ok(empty_domain, None))

    def test_eps_controls_the_nonzero_threshold(self) -> None:
        """ε 是除法条件的判定阈值：放宽 ε 后，近零取值被判为不满足。"""
        candidate = _candidate_for("1 / X1")
        point = {"X1": 1e-6}
        # 默认 ε = 1e-9：|1e-6| > 1e-9，成立
        self.assertTrue(domain_ok(candidate, point))
        # 收紧到 ε = 1e-3：|1e-6| > 1e-3 不成立
        self.assertFalse(domain_ok(candidate, point, eps=1e-3))
        self.assertTrue(domain_ok(candidate, point, eps=1e-12))

    def test_invalid_target_type_is_rejected(self) -> None:
        with self.assertRaises(DomainError):
            domain_ok("1 / X1", {"X1": 1.0})  # type: ignore[arg-type]


class TestNoDataAccess(unittest.TestCase):
    """边界守护：本模块不接触任何数据分区，也不实现 P03 的去重剪枝。"""

    @classmethod
    def setUpClass(cls) -> None:
        import sdl_m02.domain as domain_module

        cls.module = domain_module
        # 解析真实语法树，而不是扫描源码文本——
        # 文档字符串中会说明「本模块不引用某某」，纯文本匹配会误报。
        cls.tree = _parse_module_source("sdl_m02/domain.py")

    def test_does_not_import_module_01(self) -> None:
        """不导入 sdl_m01：不存在任何数据／令牌／分区访问路径。"""
        imported = _imported_module_names(self.tree)
        for name in imported:
            self.assertFalse(name.startswith("sdl_m01"), f"domain.py 不得导入 {name}")

    def test_imports_are_stdlib_or_sibling_only(self) -> None:
        """除标准库与本包的 expressions 外，不引入任何第三方依赖。"""
        for name in _imported_module_names(self.tree):
            if name.startswith("."):  # 同包内的相对导入（expressions）
                continue
            top = name.split(".")[0]
            self.assertTrue(
                top in _STDLIB_ALLOWLIST,
                f"domain.py 引入了预期之外的依赖：{name}",
            )

    def test_no_data_access_symbols_defined(self) -> None:
        """模块内不得定义任何与角色令牌／证据库／分区访问相关的符号。"""
        import ast as ast_module

        forbidden = (
            "token",
            "evidence",
            "custodian",
            "explorer",
            "confirmer",
            "auditor",
            "read_dataset",
            "Module01",
        )
        defined = {
            node.name
            for node in self.tree.body
            if isinstance(node, (ast_module.FunctionDef, ast_module.AsyncFunctionDef, ast_module.ClassDef))
        }
        for name in forbidden:
            self.assertNotIn(name, defined, f"domain.py 不得定义 {name}")

    def test_provenance_declares_no_data_references(self) -> None:
        """候选的 provenance 明确声明不含数据引用。"""
        for candidate in enumerate_candidates(VARIABLES, max_candidates=30):
            self.assertEqual(candidate.provenance.get("data_refs"), [])
            dumped = json.dumps(candidate.to_dict(), ensure_ascii=False)
            for forbidden in ("token", "sqlite", "db_path", "protocol_id"):
                self.assertNotIn(forbidden, dumped)

    def test_unit_is_unset_in_this_stage(self) -> None:
        """量纲签名留待后续阶段，本阶段一律为未定。"""
        for candidate in enumerate_candidates(VARIABLES, max_candidates=20):
            self.assertIsNone(candidate.unit)

    def test_does_not_implement_pruning_api(self) -> None:
        """P03 的去重剪枝接口不得提前出现。"""
        for name in ("dedup", "prune_dominated", "build_representation"):
            self.assertFalse(
                hasattr(self.module, name), f"domain.py 不得实现 {name}（属 P03）"
            )

    def test_module_does_not_evaluate_candidates_over_data(self) -> None:
        """模块不得提供「把候选应用到数据集」的批量求值入口。"""
        for name in ("evaluate_expression", "evaluate_candidates", "fit", "score"):
            self.assertFalse(
                hasattr(self.module, name),
                f"domain.py 不得提供 {name}：候选的数据求值属 M3",
            )


class TestSerialization(unittest.TestCase):
    """候选与定义域的序列化／反序列化往返一致。"""

    def test_candidate_to_dict_has_interface_fields(self) -> None:
        candidate = _candidate_for("X1 / X2")
        payload = candidate.to_dict()
        for key in (
            "candidate_id",
            "expression",
            "depth",
            "node_count",
            "domain",
            "unit",
            "provenance",
        ):
            self.assertIn(key, payload)

    def test_candidate_id_is_hash_derived_and_stable(self) -> None:
        """语义等价的不同写法得到同一个 candidate_id。"""
        left = _candidate_for("X2 + X1")
        right = _candidate_for("X1 + X2")
        self.assertEqual(left.candidate_id, right.candidate_id)
        self.assertTrue(left.candidate_id.startswith("cand-"))

    def test_domain_spec_round_trip(self) -> None:
        spec = DomainSpec.for_expression(parse("log(X1) / sqrt(X2)", max_depth=4))
        restored = DomainSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
        self.assertEqual(
            [c.kind for c in restored.conditions], [c.kind for c in spec.conditions]
        )
        self.assertEqual(restored.epsilon, spec.epsilon)
        self.assertEqual(restored.describe(), spec.describe())
        for condition in restored.conditions:
            self.assertIsInstance(condition, DomainCondition)
            self.assertTrue(condition.operand)

    def test_candidate_depth_and_node_count_match_expressions(self) -> None:
        from sdl_m02.expressions import depth as expr_depth
        from sdl_m02.expressions import node_count as expr_node_count

        for candidate in enumerate_candidates(VARIABLES, max_candidates=40):
            self.assertEqual(candidate.depth, expr_depth(candidate.ast))
            self.assertEqual(candidate.node_count, expr_node_count(candidate.ast))

    def test_collect_conditions_accepts_canonical_ast(self) -> None:
        """``collect_domain_conditions`` 对已规范 AST 也成立（幂等桥接）。"""
        canonical = parse("X1 / X2")
        again = collect_domain_conditions(canonical)
        self.assertEqual([c.kind for c in again], ["nonzero"])
        # 二次收取结果相同，说明 ``_to_raw`` 桥接保持语义
        self.assertEqual(
            [to_canonical_json(c.operand) for c in again],
            [to_canonical_json(c.operand) for c in collect_domain_conditions(parse("X1 / X2"))],
        )

    def test_invalid_ast_is_rejected(self) -> None:
        with self.assertRaises(DomainError):
            collect_domain_conditions(("nonexistent", "X1"))
        with self.assertRaises(DomainError):
            DomainSpec.for_expression(())


class TestEvaluateInternalConsistency(unittest.TestCase):
    """定义域条件所引用的子树，其取值应与 Python 语义一致。"""

    def test_condition_value_matches_manual_computation(self) -> None:
        spec = DomainSpec.for_expression(parse("sqrt(X1 + X2)"))
        condition = spec.conditions[0]
        self.assertAlmostEqual(condition.evaluate({"X1": 1.0, "X2": 3.0}), 4.0)
        self.assertAlmostEqual(condition.evaluate({"X1": 2.5, "X2": 1.5}), 4.0)

    def test_inv_operand_value_is_the_denominator(self) -> None:
        spec = DomainSpec.for_expression(parse("X1 / X2"))
        condition = spec.conditions[0]
        self.assertAlmostEqual(condition.evaluate({"X1": 99.0, "X2": 2.0}), 2.0)
        self.assertTrue(math.isfinite(condition.evaluate({"X1": 0.0, "X2": 7.0})))


# ---------------------------------------------------------------------------
# 测试内部工具
# ---------------------------------------------------------------------------


def _candidate_for(text: str) -> Candidate:
    """由表达式文本构造一个候选对象（复用 Public API）。"""
    ast = parse(text)
    return Candidate.create(ast, DomainSpec.for_expression(ast))


def _contains_inv(ast: tuple) -> bool:
    """判断（规范式）AST 中是否出现倒数节点。"""
    if ast[0] == "inv":
        return True
    for child in ast[1:]:
        if isinstance(child, tuple) and _contains_inv(child):
            return True
    return False


def _variables_of(ast: tuple) -> set:
    """收集（规范式）AST 中出现的变量名。"""
    if ast[0] == "var":
        return {ast[1]}
    found = set()
    for child in ast[1:]:
        if isinstance(child, tuple):
            found |= _variables_of(child)
    return found


def _parse_module_source(relative_path: str):
    """解析仓库内模块源码为 AST（用于静态边界检查）。"""
    import ast as ast_module
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    return ast_module.parse((root / relative_path).read_text(encoding="utf-8"))


def _imported_module_names(tree) -> list[str]:
    """收集模块中 ``import`` 引入的外部模块名。

    相对导入（``from .expressions import ...``）记作 ``"."`` 前缀，
    表示「同包内导入」，不属于外部依赖。
    """
    import ast as ast_module

    names: list[str] = []
    for node in ast_module.walk(tree):
        if isinstance(node, ast_module.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast_module.ImportFrom):
            names.append(f".{node.module}" if node.level else (node.module or ""))
    return names


#: 允许使用的标准库顶层模块（本模块实际用到的全部依赖）。
_STDLIB_ALLOWLIST = frozenset(
    {
        "__future__",
        "hashlib",
        "math",
        "dataclasses",
        "typing",
    }
)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
