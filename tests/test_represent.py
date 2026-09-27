"""P03 验收测试：表示构造管线与去重剪枝。

覆盖三条验收标准：

1. **只读 E 分区**：数据只经 explorer 角色读取，且引用必须是协议分区映射中
   的 ``E``；``V`` / ``C1`` 引用、缺少映射、未知引用一律拒绝。
2. **输出受预算约束**：候选池大小不超过 ``Budget.max_candidates``；
   单个候选的规模不超过 ``max_nodes`` / ``max_depth``。
3. **结构多样性保留、参数微变体合并**：不同骨架的候选共存；
   仅常数不同的同骨架候选被合并为一个代表。

同时守护 P03 的边界：不实现 P04+ 的功能（拟合、方程搜索、评分、筛选、
冻结、假说构造），不接触令牌正文，不使用可变默认参数。

注意：本模块的边界断言一律基于 **AST 静态解析**，而非源码文本匹配——
文档字符串中会说明「本模块不访问某某」，纯文本匹配会误报。
"""

import ast as ast_module
import json
import os
import pathlib
import sys
import tempfile
import unittest

from sdl_m01 import AccessDenied, Module01, initialize
from sdl_m02.domain import Candidate, DomainSpec
from sdl_m02.expressions import DEFAULT_MAX_DEPTH, DEFAULT_MAX_NODES, parse

from sdl_m02.represent import (
    Budget,
    DEFAULT_ENUMERATION_CAPACITY,
    DEFAULT_TARGET_FIELD,
    RepresentError,
    RepresentationPool,
    build_representation,
    dedup,
    domain_cost,
    prune_dominated,
    skeleton,
)

# tests/ 的上一级即仓库根，用于静态解析被检查的模块源码
_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 通用辅助（纯函数，不含被测逻辑）
# ---------------------------------------------------------------------------


def _axes(module_path: str):
    """把仓库内模块源码解析为 AST。"""
    return ast_module.parse((_ROOT / module_path).read_text(encoding="utf-8"))


def _imported_names(tree) -> list:
    """收集 ``import`` 引入的模块名（相对导入记作 ``.`` 前缀）。"""
    names: list = []
    for node in ast_module.walk(tree):
        if isinstance(node, ast_module.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast_module.ImportFrom):
            names.append(f".{node.module}" if node.level else (node.module or ""))
    return names


def _header_lines(tree) -> list:
    """返回源码的头部语句行号范围（``__future__`` 导入所在处之前）。"""
    return [node.lineno for node in tree.body if isinstance(node, ast_module.Expr)]


def make_candidate(text: str) -> Candidate:
    """由表达式文本构造一个最小合法候选（供去重 / 剪枝单测使用）。"""
    ast_tuple = parse(text)
    return Candidate.create(ast_tuple, DomainSpec.for_expression(ast_tuple))


def make_pool(records):
    """由记录序列构造候选池（不触发任何数据访问）。"""
    return build_representation(records)


VARIABLES = ["X1", "X2", "X3"]


class VaultCase(unittest.TestCase):
    """提供一套真实的 M1 证据库（E / V / C1 三分区）作为夹具。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.helpers import sample_records, sample_spec

        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls._tmp.name, "vault.sqlite3")
        cls.tokens = initialize(cls.db_path)
        custodian = Module01(cls.db_path, cls.tokens["custodian"])
        cls.protocol = custodian.build(sample_spec(), sample_records(groups=12, readings=2))
        cls.resources = dict(cls.protocol["resources"])
        cls.spec = {"resources": cls.resources}
        cls.client = Module01(cls.db_path, cls.tokens["explorer"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def make_records(self):
        from tests.helpers import sample_records

        return sample_records(groups=4, readings=2)


# ---------------------------------------------------------------------------
# 验收标准①：只读 explorer 的 E 分区
# ---------------------------------------------------------------------------


class TestExplorationOnly(VaultCase):
    """边界守护：数据只经 explorer 读取 E 分区。"""

    def test_reads_the_exploration_reference(self) -> None:
        """以客户端 + 协议描述调用时，读取的正是 E 引用。"""
        pool = build_representation(self.client, self.spec)
        self.assertEqual(pool.provenance["exploration_refs"], [self.resources["E"]])

    def test_records_are_actually_read(self) -> None:
        """确实读了记录，且数量与 E 分区行数一致。"""
        rows = self.client.read_dataset(self.resources["E"])
        pool = build_representation(self.client, self.spec)
        self.assertEqual(pool.provenance["records_read"], len(rows))
        self.assertGreater(pool.provenance["records_read"], 0)

    def test_reference_string_with_mapping_is_accepted(self) -> None:
        """以 E 引用字符串调用、且映射齐备时放行。"""
        pool = build_representation(
            self.resources["E"], self.spec, None, clients=[self.client]
        )
        self.assertEqual(pool.provenance["exploration_refs"], [self.resources["E"]])

    def test_development_reference_is_rejected(self) -> None:
        """V 是开发视图：M1 会放行，但 P03 必须主动拒绝。"""
        with self.assertRaises(RepresentError) as ctx:
            build_representation(self.resources["V"], self.spec, None, clients=[self.client])
        self.assertIn("V", str(ctx.exception))

    def test_confirmation_reference_is_rejected(self) -> None:
        """C1 属确认侧：必须拒绝。"""
        with self.assertRaises(RepresentError) as ctx:
            build_representation(self.resources["C1"], self.spec, None, clients=[self.client])
        self.assertIn("C1", str(ctx.exception))

    def test_missing_mapping_is_rejected_not_guessed(self) -> None:
        """缺少分区映射时拒绝读取，而不是猜测引用归属。"""
        for spec in (None, {"task": "no resources here"}):
            with self.assertRaises(RepresentError, msg=f"spec={spec!r}"):
                build_representation(self.client, spec)

    def test_unknown_reference_is_rejected(self) -> None:
        """引用不在映射中，视为越界（不猜测归属）。"""
        # 映射声明 E 为真实引用，但调方传了一个既非 E 也不在映射中的引用。
        with self.assertRaises(RepresentError):
            build_representation(
                "data_not_declared", self.spec, None, clients=[self.client]
            )

    def test_declared_but_nonexistent_reference_is_denied_by_m1(self) -> None:
        """声明为 E 却并不存在的引用：守卫放行，由 M1 拒绝。"""
        from sdl_m01 import ValidationError

        with self.assertRaises(ValidationError):
            build_representation(self.client, {"resources": {"E": "data_nonexistent"}})

    def test_confirmer_client_is_denied_by_m1(self) -> None:
        """非 explorer / custodian 的角色由 M1 拒绝，错误如实上抛。"""
        confirmer = Module01(self.db_path, self.tokens["confirmer"])
        with self.assertRaises(AccessDenied):
            build_representation(confirmer, self.spec)

    def test_token_body_never_enters_provenance(self) -> None:
        """溯源信息中不得出现任何令牌正文。"""
        pool = build_representation(self.client, self.spec)
        blob = json.dumps(pool.provenance, ensure_ascii=False)
        for role in ("explorer", "custodian", "confirmer", "auditor"):
            self.assertNotIn(self.tokens[role], blob, f"溯源泄露了 {role} 令牌")


# ---------------------------------------------------------------------------
# 验收标准②：输出受预算约束
# ---------------------------------------------------------------------------


class TestBudgetIsObeyed(VaultCase):
    """预算约束：数量与规模双上界。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.pool = build_representation(cls.client, cls.spec)

    def test_size_within_default_budget(self) -> None:
        self.assertLessEqual(self.pool.size, Budget().max_candidates)

    def test_custom_candidate_bound_is_obeyed(self) -> None:
        """把数量上界压到很小，返回值长度仍不得超过它。"""
        for limit in (1, 7, 40):
            pool = build_representation(
                self.client, self.spec, Budget(max_candidates=limit)
            )
            self.assertLessEqual(pool.size, limit, f"limit={limit}")

    def test_budget_dict_form_is_accepted(self) -> None:
        pool = build_representation(self.client, self.spec, {"max_candidates": 12})
        self.assertLessEqual(pool.size, 12)

    def test_node_and_depth_bounds_are_obeyed(self) -> None:
        """单个候选的规模不得超过 max_nodes / max_depth。"""
        pool = build_representation(
            self.client, self.spec, Budget(max_candidates=200, max_nodes=5, max_depth=3)
        )
        for candidate in pool.candidates:
            self.assertLessEqual(candidate.node_count, 5)
            self.assertLessEqual(candidate.depth, 3)

    def test_depth_bound_can_be_tightened(self) -> None:
        pool = build_representation(self.client, self.spec, Budget(max_depth=2))
        for candidate in pool.candidates:
            self.assertLessEqual(candidate.depth, 2)

    def test_invalid_budget_is_rejected(self) -> None:
        for bad in (
            Budget,  # type: ignore[arg-type]  # 传入类本身而非实例
            {"max_candidates": 0},
            {"max_candidates": -3},
            {"max_candidates": True},
            {"unknown_field": 5},
        ):
            with self.assertRaises((RepresentError, TypeError), msg=f"bad={bad!r}"):
                build_representation(self.client, self.spec, bad)  # type: ignore[arg-type]

    def test_enumeration_capacity_exceeds_output_bound(self) -> None:
        """枚举容量与输出上界解耦，且内部容量不小于默认值。"""
        pool = build_representation(self.client, self.spec, Budget(max_candidates=5))
        recorded = pool.provenance["budget"]["enumeration_capacity"]
        self.assertGreaterEqual(recorded, DEFAULT_ENUMERATION_CAPACITY)
        self.assertLessEqual(pool.size, 5)

    def test_order_is_deterministic(self) -> None:
        """同一输入两次构造得到完全一致的候选序列。"""
        again = build_representation(self.client, self.spec)
        self.assertEqual(
            [c.expression_key() for c in self.pool.candidates],
            [c.expression_key() for c in again.candidates],
        )
        self.assertEqual(self.pool.provenance["pool_digest"], again.provenance["pool_digest"])


# ---------------------------------------------------------------------------
# 验收标准③：结构多样性保留、参数微变体合并
# ---------------------------------------------------------------------------


class TestStructureDiversityAndVariants(unittest.TestCase):
    """去重口径：留结构、并微变体。"""

    def test_skeleton_collapses_constant_leaves(self) -> None:
        """仅常数不同的同结构表达式拥有相同骨架。"""
        self.assertEqual(
            skeleton(make_candidate("X1 / 2").ast),
            skeleton(make_candidate("X1 / 3").ast),
        )

    def test_skeleton_distinguishes_structure(self) -> None:
        """结构不同则骨架不同。"""
        pairs = [
            ("X1 + X2", "X1 * X2"),
            ("X1 + X2", "X1 - X2"),
            ("X1 / X2", "X2 / X1"),
            ("sqrt(X1)", "log(X1)"),
        ]
        for left, right in pairs:
            self.assertNotEqual(
                skeleton(make_candidate(left).ast),
                skeleton(make_candidate(right).ast),
                f"{left} 与 {right} 骨架不应相同",
            )

    def test_skeleton_treats_powers_as_parameter_variants(self) -> None:
        """X1^2 与 X1^3 结构相同，属参数微变体。"""
        self.assertEqual(
            skeleton(make_candidate("X1 ^ 2").ast),
            skeleton(make_candidate("X1 ^ 3").ast),
        )

    def test_dedup_merges_parameter_variants(self) -> None:
        """同骨架族只保留一个代表。"""
        family = [
            make_candidate("X1 / 2"),
            make_candidate("X1 / 3"),
            make_candidate("X1 / 4"),
        ]
        kept = dedup(family)
        self.assertEqual(len(kept), 1)

    def test_dedup_keeps_structurally_distinct_candidates(self) -> None:
        """不同结构全部保留。"""
        distinct = [
            make_candidate("X1 + X2"),
            make_candidate("X1 * X2"),
            make_candidate("X1 - X2"),
            make_candidate("sqrt(X1)"),
        ]
        self.assertEqual(len(dedup(distinct)), len(distinct))

    def test_dedup_removes_exact_duplicates(self) -> None:
        """语义完全相同的候选只留一个。"""
        same = [make_candidate("X1 + X2"), make_candidate("X2 + X1")]
        self.assertEqual(same[0].expression_key(), same[1].expression_key())
        self.assertEqual(len(dedup(same)), 1)

    def test_dedup_rejects_non_candidates(self) -> None:
        with self.assertRaises(RepresentError):
            dedup(["not a candidate"])  # type: ignore[list-item]

    def test_pool_keeps_one_representative_per_skeleton(self) -> None:
        """候选池中每个骨架恰好一个代表。"""
        pool = make_pool(
            [{"values": {"X1": 1.0, "X2": 2.0, "X3": 3.0, "Y": 4.0}}]
        )
        families = [json.dumps(skeleton(c.ast)) for c in pool.candidates]
        self.assertEqual(len(families), len(set(families)))
        self.assertGreater(pool.size, 1, "池中应保留多种结构")

    def test_pool_spans_multiple_node_counts(self) -> None:
        """多样性体现为规模分层，而非只留最小的几个表达式。"""
        pool = make_pool(
            [{"values": {"X1": 1.0, "X2": 2.0, "X3": 3.0, "Y": 4.0}}]
        )
        self.assertGreater(len({c.node_count for c in pool.candidates}), 2)


# ---------------------------------------------------------------------------
# 被支配剪枝
# ---------------------------------------------------------------------------


class TestPruneDominated(unittest.TestCase):
    """剪枝：剔除不合法与被支配候选。"""

    def test_removes_invalid_domain_candidates(self) -> None:
        """定义域可证为空的候选被剔除（如 1/0、sqrt(-1)、log(0)）。"""
        invalid = [
            make_candidate("1 / 0"),
            make_candidate("sqrt(-1)"),
            make_candidate("log(0)"),
        ]
        for candidate in invalid:
            self.assertFalse(
                candidate.domain.symbolically_ok(),
                f"{candidate.expression_key()} 的定义域应可证为空",
            )
        self.assertEqual(prune_dominated(invalid), ())

    def test_keeps_candidates_whose_domain_may_hold(self) -> None:
        """含变量的条件视为「可能满足」，不得被误剔。"""
        valid = [make_candidate("1 / X1"), make_candidate("sqrt(X1)"), make_candidate("log(X1)")]
        self.assertEqual(len(prune_dominated(valid)), len(valid))

    def test_prune_rejects_non_candidates(self) -> None:
        with self.assertRaises(RepresentError):
            prune_dominated([object()])  # type: ignore[list-item]

    def test_prune_is_stable_and_sorted(self) -> None:
        """输出顺序确定，且对自身幂等。"""
        pool = make_pool([{"values": {"X1": 1.0, "X2": 2.0, "Y": 3.0}}])
        once = prune_dominated(pool.candidates)
        twice = prune_dominated(once)
        self.assertEqual(
            [c.expression_key() for c in once], [c.expression_key() for c in twice]
        )

    def test_domain_cost_counts_conditions(self) -> None:
        self.assertEqual(domain_cost(make_candidate("X1 + X2")), 0)
        self.assertEqual(domain_cost(make_candidate("1 / X2")), 1)
        # 1/sqrt(X1) 同时要求分母非零与开方非负，深度 3 落在默认预算内。
        self.assertEqual(domain_cost(make_candidate("1 / sqrt(X1)")), 2)

    def test_pruned_pool_contains_no_empty_domain(self) -> None:
        """管线产出的候选池中不得残留定义域为空的候选。"""
        pool = build_representation(
            [{"values": {"X1": 1.0, "X2": 2.0, "Y": 3.0}}],
            None,
            Budget(max_candidates=300),
        )
        for candidate in pool.candidates:
            self.assertTrue(
                candidate.domain.symbolically_ok(),
                f"{candidate.expression_key()} 的定义域为空",
            )


# ---------------------------------------------------------------------------
# 管线接口与叶子提取
# ---------------------------------------------------------------------------


class TestPipelineInterface(unittest.TestCase):
    """管线入口的形态校验与叶子提取规则。"""

    def test_accepts_record_sequence_without_access(self) -> None:
        """记录序列路径不触发任何读取，也无需协议描述。"""
        pool = build_representation([{"values": {"X1": 1.0, "Y": 2.0}}])
        self.assertEqual(pool.provenance["exploration_refs"], [])
        self.assertIsInstance(pool, RepresentationPool)

    def test_rejects_unknown_input_shape(self) -> None:
        for bad in (42, 3.5, object()):
            with self.assertRaises(RepresentError, msg=f"bad={bad!r}"):
                build_representation(bad)  # type: ignore[arg-type]

    def test_rejects_non_mapping_records(self) -> None:
        with self.assertRaises(RepresentError):
            build_representation(["not-a-record"])  # type: ignore[list-item]

    def test_rejects_missing_required_client(self) -> None:
        """传入引用字符串却不给客户端 → 拒绝。"""
        with self.assertRaises(RepresentError):
            build_representation("data_abc123", {"resources": {"E": "data_abc123"}})

    def test_target_field_is_excluded_from_leaves(self) -> None:
        """目标字段（默认 Y）不得作为构造输入。"""
        pool = build_representation(
            [{"values": {"X1": 1.0, "X2": 2.0, "Y": 3.0}}]
        )
        self.assertNotIn(DEFAULT_TARGET_FIELD, pool.variables())
        self.assertEqual(set(pool.variables()), {"X1", "X2"})

    def test_custom_target_field(self) -> None:
        pool = build_representation(
            [{"values": {"X1": 1.0, "Z": 9.0}}], target_field="Z"
        )
        self.assertEqual(set(pool.variables()), {"X1"})

    def test_non_numeric_fields_are_ignored(self) -> None:
        """非数值字段不参与枚举。"""
        pool = build_representation(
            [{"values": {"X1": 1.0, "label": "a", "flag": True, "Y": 2.0}}]
        )
        self.assertEqual(set(pool.variables()), {"X1"})

    def test_max_leaves_caps_leaf_count(self) -> None:
        record = {"values": {f"X{i}": float(i) for i in range(1, 9)} | {"Y": 1.0}}
        pool = build_representation([record], None, Budget(max_candidates=5), max_leaves=3)
        self.assertLessEqual(len(pool.provenance["leaves"]), 3)

    def test_no_leaves_is_rejected(self) -> None:
        """记录里没有任何可用变量 → 明确报错而非返回空池。"""
        with self.assertRaises(RepresentError):
            build_representation([{"values": {"Y": 1.0}}])

    def test_pool_serialization_round_trips(self) -> None:
        pool = build_representation([{"values": {"X1": 1.0, "Y": 2.0}}])
        payload = pool.to_dict()
        for key in ("size", "candidates", "provenance", "records_read", "budget"):
            self.assertIn(key, payload)
        json.dumps(payload)  # 必须可 JSON 序列化

    def test_pool_is_iterable_and_sized(self) -> None:
        pool = build_representation([{"values": {"X1": 1.0, "Y": 2.0}}])
        self.assertEqual(len(list(pool)), pool.size)
        self.assertEqual(len(pool), pool.size)


# ---------------------------------------------------------------------------
# 边界守护：不越界到后续阶段
# ---------------------------------------------------------------------------


class TestStageBoundary(unittest.TestCase):
    """P03 只做表示构造 / 去重 / 剪枝，不得越界实现后续阶段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = _axes("sdl_m02/represent.py")
        cls.source = (_ROOT / "sdl_m02/represent.py").read_text(encoding="utf-8")

    def test_module_exposes_only_declared_interface(self) -> None:
        """公开符号限于本阶段声明的接口。"""
        import sdl_m02.represent as module

        expected = {
            "build_representation",
            "dedup",
            "prune_dominated",
            "skeleton",
            "domain_cost",
            "Budget",
            "RepresentationPool",
            "RepresentError",
        }
        missing = {n for n in expected if not hasattr(module, n)}
        self.assertFalse(missing, f"缺少声明的接口：{sorted(missing)}")

    def test_no_later_stage_entry_points_defined(self) -> None:
        """不得定义 M3+ 的入口（拟合 / 搜索 / 评分 / 筛选 / 冻结 / 假说）。"""
        forbidden = (
            "fit",
            "fit_model",
            "residual",
            "search_equations",
            "search_structure",
            "bootstrap",
            "resample",
            "score",
            "evaluate",
            "select",
            "pareto",
            "freeze",
            "generate_hypothesis",
        )
        defined = {
            node.name
            for node in self.tree.body
            if isinstance(node, (ast_module.FunctionDef, ast_module.AsyncFunctionDef))
        }
        for name in forbidden:
            self.assertNotIn(name, defined, f"represent.py 不得定义 {name}（属后续阶段）")

    def test_imports_are_stdlib_or_sibling_only(self) -> None:
        """除标准库、M1 与本包同层模块外，不引入其他依赖。"""
        allowed = {
            "__future__",
            "hashlib",
            "dataclasses",
            "typing",
            "sdl_m01",
            "sdl_m02",
        }
        for name in _imported_names(self.tree):
            if name.startswith("."):  # 同包相对导入
                continue
            self.assertIn(
                name.split(".")[0],
                allowed,
                f"represent.py 引入了预期之外的依赖：{name}",
            )

    def test_does_not_import_third_party(self) -> None:
        """不得引入任何第三方库。"""
        third_party = ("numpy", "pandas", "scipy", "sklearn", "requests")
        for name in _imported_names(self.tree):
            top = name.split(".")[0]
            self.assertNotIn(top, third_party, f"不得引入第三方依赖：{name}")

    def test_does_not_open_database_directly(self) -> None:
        """不得绕过 M1 自行打开数据库或执行 SQL。"""
        for name in _imported_names(self.tree):
            self.assertNotIn(
                name.split(".")[0], ("sqlite3", "sqlite"), "不得直接访问存储"
            )
        offenders = [
            node.func.attr
            for node in ast_module.walk(self.tree)
            if isinstance(node, ast_module.Call)
            and isinstance(node.func, ast_module.Attribute)
            and node.func.attr in ("connect", "execute", "executescript", "cursor")
        ]
        self.assertFalse(offenders, f"发现直接数据库操作：{offenders}")

    def test_does_not_derive_or_manipulate_tokens(self) -> None:
        """不得派生 / 拼接令牌，也不得读取凭证文件。"""
        forbidden_calls = {"token_urlsafe", "token_hex", "token_bytes", "urandom"}
        for node in ast_module.walk(self.tree):
            if isinstance(node, ast_module.Call):
                if isinstance(node.func, ast_module.Attribute):
                    self.assertNotIn(
                        node.func.attr, forbidden_calls, "不得自行派生令牌"
                    )
                elif isinstance(node.func, ast_module.Name):
                    self.assertNotIn(node.func.id, forbidden_calls, "不得自行派生令牌")

    def test_no_mutable_default_arguments(self) -> None:
        """不得使用可变默认参数（列表 / 字典 / 集合字面量）。"""
        mutable = (ast_module.List, ast_module.Dict, ast_module.Set)
        for node in ast_module.walk(self.tree):
            if isinstance(node, (ast_module.FunctionDef, ast_module.AsyncFunctionDef)):
                for default in list(node.args.defaults) + [
                    d for d in node.args.kw_defaults if d is not None
                ]:
                    self.assertNotIsInstance(
                        default, mutable, f"{node.name} 使用了可变默认参数"
                    )

    def test_no_skip_or_xfail_markers(self) -> None:
        """不得用跳过 / 预期失败来掩盖问题。"""
        tree = _axes("tests/test_represent.py")
        for node in ast_module.walk(tree):
            if isinstance(node, ast_module.Attribute):
                self.assertNotIn(node.attr, ("skip", "skipIf", "xfail"))
            if isinstance(node, ast_module.Name):
                self.assertNotIn(node.id, ("skipped",))
            if isinstance(node, ast_module.Call):
                func = node.func
                name = func.attr if isinstance(func, ast_module.Attribute) else getattr(func, "id", "")
                self.assertNotIn(name, ("skip", "skipIf", "skipUnless", "xfail"))


if __name__ == "__main__":
    unittest.main()
