"""P01 验收测试：受限表达式语法与规范化。

覆盖三条验收标准：

1. 相同语义的不同写法规范化后 AST 相等；
2. 深度 > 3 或节点数超限的表达式被拒绝；
3. 语法子集能表达加、减、乘、除与一元变换。

同时守护 P01 的边界：不执行求值、不实现变量枚举。
"""

import json
import unittest

from sdl_m02.expressions import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    SUPPORTED_FUNCTIONS,
    ExpressionError,
    ExpressionLimitError,
    ExpressionSyntaxError,
    depth,
    from_ast,
    node_count,
    normalize,
    parse,
    to_ast,
    to_canonical_json,
    variables,
)


class CanonicalizationTests(unittest.TestCase):
    """验收 1：语义等价的不同写法规范化后 AST 相等。"""

    def test_addition_commutativity(self):
        self.assertEqual(parse("x + y"), parse("y + x"))

    def test_multiplication_commutativity(self):
        self.assertEqual(parse("x * y"), parse("y * x"))

    def test_addition_associativity(self):
        self.assertEqual(parse("(x + y) + z"), parse("x + (y + z)"))

    def test_multiplication_associativity(self):
        self.assertEqual(parse("(x * y) * z"), parse("x * (y * z)"))

    def test_subtraction_as_negative_addition(self):
        """`x - y` 与 `x + (-y)` 应归一为同一 AST。"""
        self.assertEqual(parse("x - y"), parse("x + (-y)"))

    def test_division_as_inverse_multiplication(self):
        """`x / y` 应改写为「乘上分母的逆元」。"""
        self.assertEqual(
            parse("x / y"),
            normalize(("mul", ("var", "x"), ("inv", ("var", "y")))),
        )

    def test_double_negation_cancels(self):
        self.assertEqual(parse("-(-x)"), parse("x"))

    def test_neutral_elements_removed(self):
        self.assertEqual(parse("x + 0"), parse("x"))
        self.assertEqual(parse("x * 1"), parse("x"))
        self.assertEqual(parse("0 * x"), parse("0"))

    def test_constant_folding(self):
        self.assertEqual(parse("2 + 3"), parse("5"))
        self.assertEqual(parse("2 * 3 * 4"), parse("24"))

    def test_constants_in_different_notations(self):
        """`2`、`2.0`、`2.000` 不应产生不同 AST。"""
        self.assertEqual(parse("x + 2"), parse("x + 2.0"))
        self.assertEqual(parse("x + 2"), parse("x + 2.000"))

    def test_like_terms_merged(self):
        self.assertEqual(parse("x + x"), parse("2 * x"))

    def test_like_factors_merged(self):
        self.assertEqual(parse("x * x"), parse("x^2"))

    def test_power_identities(self):
        self.assertEqual(parse("x^1"), parse("x"))
        self.assertEqual(parse("x^0"), parse("1"))

    def test_equivalent_complex_forms(self):
        """一个稍复杂的等价变换组合。"""
        self.assertEqual(
            parse("(a + b) * 2"),
            parse("2 * (b + a)"),
        )

    def test_normalize_is_idempotent(self):
        # 注意：以下表达式均需落在深度上限 3 以内。
        for text in ("2*x + y", "x + y", "-(-x)", "x/y", "x*x", "(a + b) * 2"):
            once = parse(text)
            self.assertEqual(normalize(once), once, f"{text} 的规范化不幂等")

    def test_normalize_accepts_canonical_nodes(self):
        """normalize() 必须能接受已经规范化的 AST（含 inv / pow / n-ary）。"""
        for text in ("x / y", "x * x", "x^2", "x + y + z"):
            ast = parse(text, max_depth=4)
            self.assertEqual(normalize(ast), ast, f"{text} 的规范式不可再次规范化")

    def test_canonical_json_stable_under_equivalence(self):
        self.assertEqual(
            to_canonical_json(parse("x + y")),
            to_canonical_json(parse("y + x")),
        )

    def test_canonicalization_can_be_disabled(self):
        """关闭规范化时应保留原始二元结构，两者不相等。"""
        raw = parse("x + y", canonicalize=False)
        self.assertEqual(raw, ("add", ("var", "x"), ("var", "y")))
        self.assertNotEqual(raw, parse("y + x", canonicalize=False))


class LimitTests(unittest.TestCase):
    """验收 2：深度或节点数超限被拒绝。"""

    def test_default_depth_limit_is_three(self):
        self.assertEqual(DEFAULT_MAX_DEPTH, 3)

    def test_depth_within_limit_accepted(self):
        # depth(x) = 1, depth(-x) = 2, depth(x + y) = 2, depth(-(x+y)) = 3
        self.assertEqual(depth(parse("-(x + y)")), 3)

    def test_depth_over_limit_rejected(self):
        # 原始 AST 深度 4：neg -> add -> add -> var
        with self.assertRaises(ExpressionLimitError):
            parse("-(x + y + z)")

    def test_too_deep_expression_rejected(self):
        with self.assertRaises(ExpressionLimitError):
            parse("-(a + (b + c))")

    def test_node_count_over_limit_rejected(self):
        # 构造一个在深度内但节点数超限的表达式
        many = " + ".join(["x"] * (DEFAULT_MAX_NODES + 5))
        with self.assertRaises(ExpressionLimitError):
            parse(many)

    def test_custom_limits_respected(self):
        with self.assertRaises(ExpressionLimitError):
            parse("x + y + z", max_depth=2)
        # 放宽后应可通过
        self.assertIsNotNone(parse("-(x + y + z)", max_depth=4))

    def test_custom_node_limit(self):
        with self.assertRaises(ExpressionLimitError):
            parse("x + y + z", max_nodes=3)
        self.assertIsNotNone(parse("x + y + z", max_nodes=10))

    def test_limits_are_expression_limit_error(self):
        """超限必须是可捕获的专门异常，而非泛化 Exception。"""
        self.assertTrue(issubclass(ExpressionLimitError, ExpressionError))
        try:
            parse("-(a + (b + c))")
        except ExpressionLimitError:
            pass
        else:  # pragma: no cover
            self.fail("应抛出 ExpressionLimitError")


class SyntaxSubsetTests(unittest.TestCase):
    """验收 3：语法子集覆盖加、减、乘、除与一元变换。

    注意：规范化会按字典序对加法／乘法的操作数排序，因此断言**不依赖操作数顺序**，
    而是用集合式比较或「与手工构造的规范式相等」来验证。
    """

    def test_addition(self):
        ast = parse("x + y")
        self.assertEqual(ast[0], "add")
        self.assertEqual(set(ast[1:]), {("var", "x"), ("var", "y")})

    def test_subtraction_rewritten(self):
        ast = parse("x - y")
        self.assertEqual(ast[0], "add")
        self.assertEqual(set(ast[1:]), {("var", "x"), ("neg", ("var", "y"))})

    def test_multiplication(self):
        ast = parse("x * y")
        self.assertEqual(ast[0], "mul")
        self.assertEqual(set(ast[1:]), {("var", "x"), ("var", "y")})

    def test_division_rewritten(self):
        ast = parse("x / y")
        self.assertEqual(ast[0], "mul")
        self.assertEqual(set(ast[1:]), {("var", "x"), ("inv", ("var", "y"))})

    def test_unary_minus(self):
        self.assertEqual(parse("-x"), ("neg", ("var", "x")))

    def test_unary_plus_is_transparent(self):
        self.assertEqual(parse("+x"), parse("x"))

    def test_all_supported_functions_parse(self):
        for func in SUPPORTED_FUNCTIONS:
            ast = parse(f"{func}(x)")
            self.assertEqual(ast[0], "func")
            self.assertEqual(ast[1], func)

    def test_unsupported_function_rejected(self):
        with self.assertRaises(ExpressionSyntaxError):
            parse("sin(x)")

    def test_operator_precedence(self):
        # 乘法优先于加法：x + y * z 应为 add(..., mul(y, z)) 而非 mul(add(...), z)
        ast = parse("x + y * z")
        self.assertEqual(ast[0], "add")
        children = set(ast[1:])
        self.assertIn(("var", "x"), children)
        self.assertIn(("mul", ("var", "y"), ("var", "z")), children)

    def test_parentheses_override_precedence(self):
        ast = parse("(x + y) * z")
        self.assertEqual(ast[0], "mul")
        children = set(ast[1:])
        self.assertIn(("var", "z"), children)
        # (x + y) 应为 add 子式
        self.assertTrue(
            any(isinstance(c, tuple) and c[0] == "add" for c in children),
            f"应包含加法子式，实际为 {children}",
        )

    def test_numeric_literals(self):
        self.assertEqual(parse("3.5"), ("num", 3.5))
        self.assertEqual(parse(".5"), ("num", 0.5))
        self.assertEqual(parse("1e3"), ("num", 1000.0))

    def test_syntax_errors(self):
        for bad in ("x +", "* x", "(x", "x)", "", "x @ y"):
            with self.assertRaises(ExpressionSyntaxError, msg=f"{bad!r} 应报语法错误"):
                parse(bad)

    def test_variable_exponent_rejected(self):
        with self.assertRaises(ExpressionSyntaxError):
            parse("x^y")

    def test_negative_exponent_rejected(self):
        with self.assertRaises(ExpressionSyntaxError):
            parse("x^-1")


class BoundaryTests(unittest.TestCase):
    """守护 P01 的边界：不求值、不枚举、可序列化。"""

    def test_module_does_not_expose_evaluation(self):
        """P01 明确不执行求值——不应存在 eval/execute/apply 之类的公开入口。"""
        import sdl_m02.expressions as mod

        for name in ("evaluate", "eval", "execute", "apply_to_data"):
            self.assertFalse(hasattr(mod, name), f"P01 不应提供 {name}")

    def test_module_does_not_implement_enumeration(self):
        """变量枚举属 P02，不应出现在本模块。"""
        import sdl_m02.expressions as mod

        self.assertFalse(hasattr(mod, "enumerate_candidates"))

    def test_ast_is_hashable(self):
        """规范式是元组，可直接哈希——这是 P03 去重的基础。"""
        self.assertIsInstance(hash(parse("x + y")), int)

    def test_roundtrip_through_serialization(self):
        # 均落在深度上限 3 以内
        for text in ("2*x + y", "-x", "abs(x)", "x^2", "x + y", "x/y"):
            ast = parse(text)
            restored = from_ast(to_ast(ast))
            self.assertEqual(restored, ast, f"{text} 序列化往返后不等")
            self.assertEqual(json.loads(json.dumps(to_ast(ast))), to_ast(ast))

    def test_variables_collected(self):
        self.assertEqual(variables(parse("a + b*c")), {"a", "b", "c"})
        self.assertEqual(variables(parse("-a")), {"a"})
        self.assertEqual(variables(parse("2 + 3")), set())

    def test_depth_and_node_count_are_measurable(self):
        ast = parse("x + y")
        self.assertEqual(depth(ast), 2)
        self.assertEqual(node_count(ast), 3)

    def test_no_data_access_imports(self):
        """本模块不得引入 M1 或任何数据访问依赖。"""
        import sdl_m02.expressions as mod

        with open(mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import sdl_m01", src)
        self.assertNotIn("sqlite3", src)


if __name__ == "__main__":
    unittest.main()
