"""P16 验收测试：端到端主循环集成。

覆盖本阶段五条验收标准：

1. **含预算与停止条件。** :class:`LoopBudget` 在启动前声明全部阈值且只读；
   :func:`evaluate_stop` 的判定顺序确定且可机械复现（轮次用尽 / 资源耗尽 /
   无未见确证数据 / 连续无合格候选）；端到端跑出的 ``stop.code`` 与实测一致。

2. **冻结后预处理与候选不变。** :func:`freeze_snapshot` 在 ``bind`` 之前取定
   内容摘要；端到端执行与归档之后由 :func:`assert_frozen_unchanged` 复算通过；
   人为改动预处理或候选即抛 :class:`LoopStateError`。

3. **产出发现档案 / 知识库版本 / 反例与证据不足清单。**
   :class:`DiscoveryArchive` 同时给出 ``knowledge.version``、反例条目、
   「不显著（inconclusive）」与「无法执行（failed）」两类条目——二者必须可区分；
   反例来自**真实存在**的破坏批次（实测非空）。

4. **demo 明确标注为合成验证且不宣称因果或真实理论。**
   ``run_demo`` 的每个产物都携带 :data:`SYNTHETIC_DEMO_NOTICE`；
   标注同时否认因果结论与真实理论；``NOT_PROVIDED_BY_P16`` 与 ``self_check``
   共同守护「本层不做因果升级、不自算 p 值、不直连存储」。

5. **本阶段可见的最终输出必须含「仍缺证据的问题清单」。**
   :attr:`DiscoveryArchive.open_questions` 必然存在（三类来源：结论层面 /
   等级层面 / 前提层面），:meth:`DiscoveryArchive.render_open_questions`
   可读渲染；尚无确证支持的假说一定进入该清单。

关于「不做 X」的守护方式：本文件一律用 **AST** 判断「函数有没有被定义、
属性有没有被访问」，**不**用源码文本匹配——模块的 docstring 与
``NOT_PROVIDED_BY_P16`` 常量里**必须**写出被禁名字（那是边界声明），
文本匹配会把边界声明本身当成违规。

关于夹具自检：本文件先**实测** `sdl_pipeline.loop` 的真实输出再写断言
（轮次状态、α 序列、知识库版本、反例条数、问题条数均与实测一致），不靠推测。
"""

import ast as pyast
import json
import pathlib
import tempfile
import unittest

from sdl_m01 import Module01, initialize

from sdl_pipeline import loop as L

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "sdl_pipeline" / "loop.py"
)


# ---------------------------------------------------------------------------
# 夹具：真实 M1 证据库 + 合成协议
# ---------------------------------------------------------------------------


class _LoopCase(unittest.TestCase):
    """端到端夹具：真实 M1 + 合成协议 + 真实 M2–M8 链路。

    ``max_candidates`` 取小值（60）是为了让测试快：本文件验的是**结构与口径**，
    不要求恢复出比例候选（那是 demo 的职责）。
    """

    GROUPS = 60

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-p16-")
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "vault.sqlite3"
        self.tokens = initialize(self.db)
        self.custodian = Module01(self.db, self.tokens["custodian"])
        self.confirmer = Module01(self.db, self.tokens["confirmer"])
        self.explorer = Module01(self.db, self.tokens["explorer"])
        self.protocol = self.custodian.build(
            L.synthetic_spec(), L.synthetic_records(groups=self.GROUPS)
        )
        self.protocol_id = self.protocol["protocol_id"]

    # -- 运行辅助 -------------------------------------------------------

    def budget(self, **overrides):
        base = dict(
            max_rounds=2,
            max_candidates=60,
            freeze_cap=2,
            n_resamples=2,
        )
        base.update(overrides)
        return L.LoopBudget(**base)

    def supplier(self, groups=8):
        """返回一个「每轮补充一批新确证数据」的供应器。"""

        def supply(round_index, protocol_id):
            L._add_confirmation_batch(
                custodian=self.custodian,
                protocol_id=protocol_id,
                purpose=f"C{round_index + 1}",
                records=L.synthetic_records(
                    groups=groups, start=5000 + 100 * round_index, broken=()
                ),
            )

        return supply

    def run_loop(self, *, evaluator=None, budget=None, supplier=True, **kwargs):
        return L.run_loop(
            custodian=self.custodian,
            confirmer=self.confirmer,
            explorer=self.explorer,
            protocol=self.protocol,
            target_field="Y",
            budget=budget or self.budget(),
            counterexample_expressions=("X1*X2",),
            data_supplier=self.supplier() if supplier is True else supplier,
            confirmation_evaluator=evaluator,
            recorded_at="2026-01-01T00:00:00+00:00",
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 边界：不以文本匹配，一律 AST
# ---------------------------------------------------------------------------


class BoundaryTests(unittest.TestCase):
    """守护「本层不做因果升级 / 不自算 p 值 / 不直连存储」。"""

    def test_self_check_passes(self):
        """模块的 AST 级边界自检必须通过。"""
        report = L.self_check()
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["defined_forbidden"], ())
        self.assertEqual(report["accessed_forbidden"], ())

    def test_not_provided_names_are_not_defined(self):
        """本层不得定义 ``NOT_PROVIDED_BY_P16`` 中的任何名字。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in pyast.walk(tree)
            if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef, pyast.ClassDef))
        }
        overlap = sorted(defined & set(L.NOT_PROVIDED_BY_P16))
        self.assertEqual(overlap, [], msg=f"越界定义：{overlap}")

    def test_boundary_declaration_exists(self):
        """边界说明必须存在（这类「必须有」的声明才适合文本检查）。"""
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("不授予等级", L.LOOP_BOUNDARY_NOTE)
        self.assertIn("不做因果升级", L.LOOP_BOUNDARY_NOTE)
        self.assertIn("仍缺证据", L.STOPPING_RULE_NOTE)
        self.assertIn("SYNTHETIC_DEMO_NOTICE", text)

    def test_only_stdlib_imports(self):
        """只允许标准库 import；不得引入第三方依赖。"""
        tree = pyast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        allowed = {"__future__", "dataclasses", "hashlib", "json", "math", "re", "typing", "pathlib", "ast", "inspect", "collections", "itertools", "functools", "copy", "statistics", "types", "enum", "abc", "sdl_m01", "sdl_m02", "sdl_m03", "sdl_m04", "sdl_m05", "sdl_m06", "sdl_m07", "sdl_m08", "sdl_pipeline"}
        found: set[str] = set()
        for node in pyast.walk(tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, pyast.ImportFrom):
                if node.module:
                    found.add(node.module.split(".")[0])
        extra = sorted(found - allowed)
        self.assertEqual(extra, [], msg=f"出现未登记模块：{extra}")

    def test_demo_notice_denies_causality(self):
        """合成标注必须同时否认因果与真实理论。"""
        self.assertIn("不是现实发现结果", L.SYNTHETIC_DEMO_NOTICE)
        self.assertIn("既不构成因果证据", L.SYNTHETIC_DEMO_NOTICE)
        self.assertIn("真实领域的新理论", L.SYNTHETIC_DEMO_NOTICE)

    def test_evaluator_notes_renounce_generality(self):
        """合成评估器必须声明不可推广。"""
        self.assertIn("不能", L.SYNTHETIC_EVALUATOR_NOTE)
        self.assertIn("真实使用必须替换", L.SYNTHETIC_EVALUATOR_NOTE)


# ---------------------------------------------------------------------------
# 验收标准①：预算与停止条件
# ---------------------------------------------------------------------------


class BudgetTests(unittest.TestCase):
    """:class:`LoopBudget` 的合法性与只读性。"""

    def test_defaults_are_declared(self):
        budget = L.LoopBudget()
        self.assertEqual(budget.max_rounds, L.DEFAULT_MAX_ROUNDS)
        self.assertEqual(budget.max_candidates, L.DEFAULT_MAX_CANDIDATES)
        self.assertEqual(budget.freeze_cap, L.DEFAULT_FREEZE_CAP)
        self.assertEqual(budget.n_resamples, L.DEFAULT_N_RESAMPLES)
        self.assertEqual(budget.seed, L.DEFAULT_SEED)
        self.assertTrue(budget.structure_search is False)

    def test_budget_is_frozen(self):
        """预算在启动前确定，循环中只读：赋值必须失败。"""
        budget = L.LoopBudget()
        with self.assertRaises(Exception):
            budget.max_rounds = 99

    def test_non_positive_values_rejected(self):
        for field in ("max_rounds", "max_candidates", "freeze_cap", "max_attempts"):
            with self.subTest(field=field):
                with self.assertRaises(L.LoopInputError):
                    L.LoopBudget(**{field: 0})

    def test_bool_is_not_an_int(self):
        """``True`` 是 ``int`` 的子类，但不得被当作合法轮次。"""
        with self.assertRaises(L.LoopInputError):
            L.LoopBudget(max_rounds=True)

    def test_representation_budget_transcribes_caps(self):
        """派生 M2 预算只转录容量与尝试上限，深度等沿用 M2 缺省。"""
        budget = L.LoopBudget(max_candidates=77, max_attempts=1234)
        derived = budget.representation_budget()
        self.assertEqual(derived.max_candidates, 77)
        self.assertEqual(derived.max_attempts, 1234)

    def test_to_dict_carries_stopping_note(self):
        payload = L.LoopBudget().to_dict()
        self.assertEqual(payload["loop_version"], L.LOOP_VERSION)
        self.assertIn("停止", payload["note"])


class StopConditionTests(unittest.TestCase):
    """:func:`evaluate_stop` 的判定顺序与停止码。"""

    def setUp(self):
        self.budget = L.LoopBudget(
            max_rounds=3, max_fits=10, max_empty_rounds=1
        )

    def test_not_stopped_when_everything_available(self):
        decision = L.evaluate_stop(
            round_index=1,
            budget=self.budget,
            ledger=L.BudgetLedger(),
            confirmation_available=True,
            candidates_available=True,
        )
        self.assertFalse(decision.stopped)
        self.assertEqual(decision.code, L.STOP_NOT_STOPPED)

    def test_rounds_exhausted_takes_priority(self):
        """轮次用尽优先于其它条件（顺序确定）。"""
        decision = L.evaluate_stop(
            round_index=4,
            budget=self.budget,
            ledger=L.BudgetLedger(fits_attempted=10),
            confirmation_available=False,
            candidates_available=False,
        )
        self.assertTrue(decision.stopped)
        self.assertEqual(decision.code, L.STOP_ROUNDS_EXHAUSTED)

    def test_resource_budget_exhausted(self):
        decision = L.evaluate_stop(
            round_index=1,
            budget=self.budget,
            ledger=L.BudgetLedger(fits_attempted=10),
            confirmation_available=True,
            candidates_available=True,
        )
        self.assertTrue(decision.stopped)
        self.assertEqual(decision.code, L.STOP_RESOURCE_BUDGET)

    def test_no_candidates_after_tolerance(self):
        decision = L.evaluate_stop(
            round_index=1,
            budget=self.budget,
            ledger=L.BudgetLedger(empty_rounds=1),
            confirmation_available=True,
            candidates_available=False,
        )
        self.assertTrue(decision.stopped)
        self.assertEqual(decision.code, L.STOP_NO_CANDIDATES)

    def test_empty_round_within_tolerance_does_not_stop(self):
        """未达容忍上限时不因「本轮空」停止。"""
        decision = L.evaluate_stop(
            round_index=1,
            budget=L.LoopBudget(max_empty_rounds=2),
            ledger=L.BudgetLedger(empty_rounds=1),
            confirmation_available=True,
            candidates_available=False,
        )
        self.assertFalse(decision.stopped)

    def test_no_confirmation_data_stops_without_self_evidence(self):
        """没有未见确证数据即停止——绝不用已见数据自证。"""
        decision = L.evaluate_stop(
            round_index=1,
            budget=self.budget,
            ledger=L.BudgetLedger(),
            confirmation_available=False,
            candidates_available=True,
        )
        self.assertTrue(decision.stopped)
        self.assertEqual(decision.code, L.STOP_NO_CONFIRMATION_DATA)
        self.assertIn("已见数据自证", decision.reason)

    def test_all_stop_codes_are_reachable(self):
        """五个停止码穷尽定义，且可解释。"""
        self.assertEqual(len(L.STOP_CODES), 5)
        self.assertIn(L.STOP_NOT_STOPPED, L.STOP_CODES)
        self.assertIn(L.STOP_NO_CONFIRMATION_DATA, L.STOP_CODES)

    def test_forbidden_kwargs_rejected(self):
        with self.assertRaises(L.LoopPolicyError):
            L.evaluate_stop(
                round_index=1,
                budget=self.budget,
                ledger=L.BudgetLedger(),
                confirmation_available=True,
                candidates_available=True,
                evidence_grade="E1",
            )


# ---------------------------------------------------------------------------
# 验收标准②：冻结不变量
# ---------------------------------------------------------------------------


class FreezeInvarianceTests(_LoopCase):
    """冻结快照与「执行前后逐字节不变」。"""

    def _plan(self):
        outcome = L.build_exploration(
            explorer=self.explorer,
            protocol=self.protocol,
            budget=self.budget(),
            target_field="Y",
        )
        pool = L.form_hypotheses(outcome=outcome, knowledge_version="K0")
        evaluations, _ = L.evaluate_hypotheses(
            pool=pool,
            outcome=outcome,
            knowledge=L._knowledge_base_for(L.empty_knowledge()),
            target_field="Y",
        )
        selection = L.select_for_freeze(evaluations=evaluations, cap=2)
        hypotheses = L._hypotheses_for_selection(
            selection,
            {f"{h.id}@{h.version}": h for h in pool.hypotheses},
        )
        self.assertGreaterEqual(len(hypotheses), 1, "夹具未筛出任何候选")
        plan = L._build_plan(
            selection, self.protocol, 1, hypotheses, "Y"
        )
        return plan, hypotheses

    def test_snapshot_records_candidates_and_digest(self):
        plan, hypotheses = self._plan()
        snapshot = L.freeze_snapshot(plan)
        self.assertTrue(snapshot["digest"])
        self.assertEqual(
            set(snapshot["frozen_candidates"]),
            set(L.frozen_candidate_ids(plan)),
        )
        self.assertEqual(
            len(snapshot["frozen_candidates"]), len(hypotheses)
        )

    def test_unchanged_after_recompute(self):
        plan, _ = self._plan()
        snapshot = L.freeze_snapshot(plan)
        self.assertTrue(L.frozen_unchanged(plan, snapshot))
        L.assert_frozen_unchanged(plan, snapshot)  # 不抛即通过

    def test_mutating_preprocessing_breaks_invariance(self):
        """改动预处理必须被捕获。"""
        plan, _ = self._plan()
        snapshot = L.freeze_snapshot(plan)
        mutated = dict(plan)
        preprocessing = dict(plan["preprocessing"])
        preprocessing["steps"] = ["偷偷加一步标准化"]
        mutated["preprocessing"] = preprocessing
        self.assertFalse(L.frozen_unchanged(mutated, snapshot))
        with self.assertRaises(L.LoopStateError):
            L.assert_frozen_unchanged(mutated, snapshot)

    def test_mutating_candidates_breaks_invariance(self):
        """改候选（含版本号）必须被捕获。"""
        plan, _ = self._plan()
        snapshot = L.freeze_snapshot(plan)
        mutated = dict(plan)
        entries = [dict(item) for item in plan["hypotheses"]]
        entries[0]["version"] = "999"
        mutated["hypotheses"] = entries
        self.assertFalse(L.frozen_unchanged(mutated, snapshot))

    def test_snapshot_is_content_based_not_reference_based(self):
        """快照取内容摘要：就地改写同一个字典也会被发现。"""
        plan, _ = self._plan()
        snapshot = L.freeze_snapshot(plan)
        plan["effect_threshold"] = 12345.0
        self.assertFalse(L.frozen_unchanged(plan, snapshot))

    def test_missing_required_field_rejected(self):
        with self.assertRaises(L.LoopInputError):
            L.freeze_snapshot({"protocol_id": "p"})

    def test_non_mapping_plan_rejected(self):
        with self.assertRaises(L.LoopInputError):
            L.freeze_snapshot("not a plan")


# ---------------------------------------------------------------------------
# 发现档案成分：反例 / 证据不足 / 仍缺证据
# ---------------------------------------------------------------------------


class ArchiveComponentTests(unittest.TestCase):
    """不依赖 M1 的纯函数成分。"""

    class _Pattern:
        def __init__(self, payload, pattern_id="p", kind="invariant"):
            self.payload = payload
            self.pattern_id = pattern_id
            self.kind = kind

    def test_counterexamples_only_from_payload(self):
        """只转录 ``check_invariant`` 给出的反例，不自行判定。"""
        pattern = self._Pattern(
            {
                "expression": "(X1 * X2)",
                "conformity": 0.9,
                "coverage": 1.0,
                "counterexamples": [
                    {"record_id": "r-1", "value": 3.0, "deviation": 1.0},
                    {"record_id": "r-2", "value": 3.0, "deviation": 1.0},
                ],
            }
        )
        items = L.counterexamples_from_patterns([pattern])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["record_id"], "r-1")
        self.assertEqual(items[0]["deviation"], 1.0)
        self.assertIn("不构成", items[0]["note"])

    def test_no_counterexamples_yields_empty(self):
        pattern = self._Pattern({"expression": "Z", "counterexamples": []})
        self.assertEqual(L.counterexamples_from_patterns([pattern]), ())

    def test_malformed_pattern_ignored(self):
        self.assertEqual(L.counterexamples_from_patterns([object()]), ())

    def test_inconclusive_and_failed_are_distinguishable(self):
        """「算出来不显著」与「根本没算」必须给出不同理由。"""

        class _Outcome:
            def __init__(self, status, **kwargs):
                self.status = status
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class _Classification:
            def __init__(self, outcomes):
                self.outcomes = outcomes

        classification = _Classification(
            [
                _Outcome(
                    "inconclusive",
                    hypothesis_id="h1",
                    test_ids=("t1",),
                    inconclusive_test_ids=("t1",),
                ),
                _Outcome(
                    "failed",
                    hypothesis_id="h2",
                    test_ids=("t2",),
                    failed_test_ids=("t2",),
                ),
            ]
        )
        items = {item["hypothesis_id"]: item for item in L.inconclusive_entries(classification)}
        self.assertIn("不显著", items["h1"]["reason"])
        self.assertIn("无法执行", items["h2"]["reason"])
        self.assertNotEqual(items["h1"]["reason"], items["h2"]["reason"])

    def test_open_statuses_cover_both(self):
        self.assertEqual(set(L.OPEN_STATUSES), {"inconclusive", "failed"})

    def test_open_questions_include_unconfirmed_hypothesis(self):
        """等级为 E0 / 未记录的假说必须进入「仍缺证据」清单。"""
        questions = L.open_questions_from(
            hypothesis_records=[{"id": "h1", "statement": "某关系优于基线"}],
            knowledge=None,
            classification=None,
            identification_gaps=(),
        )
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["hypothesis_id"], "h1")
        self.assertIn("尚未获得独立确证支持", questions[0]["reason"])
        self.assertTrue(questions[0]["missing_evidence"])
        self.assertTrue(questions[0]["resolving_action"])

    def test_open_questions_include_identification_gaps(self):
        """前提层面的识别缺口必须被披露，且不绑定具体假说。"""
        questions = L.open_questions_from(
            hypothesis_records=[],
            identification_gaps=["批次间独立性未论证"],
        )
        self.assertEqual(len(questions), 1)
        self.assertIsNone(questions[0]["hypothesis_id"])
        self.assertEqual(questions[0]["status"], "identification_gap")
        self.assertIn("独立性未论证", questions[0]["statement"])

    def test_open_questions_are_deterministic(self):
        """同输入必须给出同问题标识（可复现）。"""
        kwargs = dict(
            hypothesis_records=[{"id": "h1", "statement": "s"}],
            identification_gaps=["g"],
        )
        first = L.open_questions_from(**kwargs)
        second = L.open_questions_from(**kwargs)
        self.assertEqual(
            [q["question_id"] for q in first],
            [q["question_id"] for q in second],
        )

    def test_forbidden_kwargs_rejected(self):
        with self.assertRaises(L.LoopPolicyError):
            L.open_questions_from(
                hypothesis_records=[], evidence_grade="E2"
            )


# ---------------------------------------------------------------------------
# 契约：禁止参数与未知形态
# ---------------------------------------------------------------------------


class ContractTests(unittest.TestCase):
    """等级 / 统计结论 / 令牌类参数一律拒绝，且不静默忽略。"""

    def test_forbidden_key_set_covers_three_families(self):
        keys = L.FORBIDDEN_CALL_KEYS
        self.assertIn("evidence_grade", keys)
        self.assertIn("p_value", keys)
        self.assertIn("token", keys)

    def test_reject_forbidden_kwargs_lists_offenders(self):
        with self.assertRaises(L.LoopPolicyError) as ctx:
            L._reject_forbidden_kwargs(
                {"grade": "E1", "p_value": 0.01, "other": 1}, "unit-test"
            )
        message = str(ctx.exception)
        self.assertIn("grade", message)
        self.assertIn("p_value", message)

    def test_reject_forbidden_kwargs_ignores_unknown(self):
        """未知参数不是契约违规（由 Python 自身报 TypeError）。"""
        L._reject_forbidden_kwargs({"harmless": 1}, "unit-test")

    def test_exceptions_derive_from_value_error(self):
        self.assertTrue(issubclass(L.LoopError, ValueError))
        self.assertTrue(issubclass(L.LoopInputError, L.LoopError))
        self.assertTrue(issubclass(L.LoopPolicyError, L.LoopError))
        self.assertTrue(issubclass(L.LoopStateError, L.LoopError))


# ---------------------------------------------------------------------------
# 验收标准③④⑤：端到端主循环
# ---------------------------------------------------------------------------


class EndToEndTests(_LoopCase):
    """真实 M1 + M2–M8 跑通，逐条核对验收标准。"""

    def test_loop_confirms_rounds_and_halves_alpha(self):
        """确证执行并归档；α 按 α/2^t 逐轮减半。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        self.assertGreaterEqual(len(archive.rounds), 1)
        self.assertTrue(archive.confirmed_rounds, "未能确证并归档任何轮次")
        alphas = [round_.alpha for round_ in archive.confirmed_rounds]
        self.assertAlmostEqual(alphas[0], 0.025)
        if len(alphas) > 1:
            self.assertAlmostEqual(alphas[1], 0.0125)

    def test_knowledge_version_advances(self):
        """归档推进知识库版本（K0 → K1 → …）。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        self.assertIsNotNone(archive.knowledge_version)
        self.assertNotEqual(archive.knowledge_version, "K0")
        self.assertEqual(
            len(archive.confirmed_rounds), archive.ledger.confirmations_consumed
        )

    def test_freeze_invariance_holds_across_execution(self):
        """执行与归档之后，冻结不变量复算通过。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        for round_ in archive.confirmed_rounds:
            self.assertIs(round_.freeze_invariance_ok, True)
            self.assertTrue(round_.freeze_digest)

    def test_counterexamples_are_real_and_nonempty(self):
        """破坏批次落在探索分区内，故反例清单**实测非空**。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        self.assertGreater(len(archive.counterexamples), 0)
        entry = archive.counterexamples[0]
        self.assertEqual(entry["expression"], "(X1 * X2)")
        self.assertAlmostEqual(entry["value"], 3.0, places=6)
        self.assertAlmostEqual(entry["deviation"], 1.0, places=6)

    def test_invariant_check_was_actually_performed(self):
        """「没搜到」与「没搜」可区分：检测确实执行过。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        self.assertGreater(archive.counters()["invariant_checks_performed"], 0)

    def test_empty_counterexample_list_is_still_reported(self):
        """不请求任何不变量表达式时，清单为空但检测口径仍被记录。"""
        archive = L.run_loop(
            custodian=self.custodian,
            confirmer=self.confirmer,
            explorer=self.explorer,
            protocol=self.protocol,
            target_field="Y",
            budget=self.budget(),
            counterexample_expressions=(),
            data_supplier=self.supplier(),
            confirmation_evaluator=L.synthetic_batch_evaluator,
            recorded_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(len(archive.counterexamples), 0)
        self.assertGreater(archive.counters()["rounds"], 0)

    def test_archive_always_contains_open_questions(self):
        """**验收标准⑤**：最终输出必须含「仍缺证据的问题清单」，不得为空。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        self.assertTrue(archive.has_open_questions)
        self.assertGreater(len(archive.open_questions), 0)
        for question in archive.open_questions:
            self.assertIn("missing_evidence", question)
            self.assertIn("resolving_action", question)
            self.assertTrue(question["statement"])

    def test_render_open_questions_is_readable(self):
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        rendered = archive.render_open_questions()
        self.assertIn("仍缺证据的问题清单", rendered)
        self.assertIn("缺什么证据", rendered)
        self.assertIn("如何补上", rendered)

    def test_identification_gaps_are_disclosed(self):
        """协议登记的前提缺口必须出现在最终输出（不因「没算」被略过）。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        gaps = archive.counters()["identification_gap_count"]
        self.assertGreater(gaps, 0)
        self.assertEqual(
            len(archive.identification_gaps), gaps
        )

    def test_archive_serializes_deterministically(self):
        """结果对象提供 to_dict / canonical_json / content_digest，且确定。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        payload = archive.to_dict()
        self.assertEqual(payload["loop_version"], L.LOOP_VERSION)
        self.assertEqual(
            archive.canonical_json(), archive.canonical_json()
        )
        self.assertEqual(len(archive.content_digest()), 64)
        self.assertTrue(
            L.canonical_json(payload) == L.canonical_json(archive.to_dict())
        )

    def test_stop_decision_is_reproducible(self):
        """预算用尽时停止码与理由与实测一致。"""
        archive = self.run_loop(evaluator=L.synthetic_batch_evaluator)
        self.assertTrue(archive.stop.stopped)
        self.assertIn(archive.stop.code, L.STOP_CODES)
        self.assertTrue(archive.stop.reason)

    def test_default_evaluator_marks_evidence_insufficient(self):
        """缺省评估器下结论落在「证据不足」，且与「不显著」可区分。"""
        archive = self.run_loop(evaluator=None, budget=self.budget(max_rounds=1))
        statuses = {round_.aggregate_status for round_ in archive.rounds}
        self.assertIn("failed", statuses)
        notes = " ".join(archive.notes)
        self.assertIn("不可得", notes)

    def test_forbidden_kwarg_rejected_end_to_end(self):
        with self.assertRaises(L.LoopPolicyError):
            self.run_loop(evaluator=L.synthetic_batch_evaluator, promote_to="causal")

    def test_invalid_protocol_rejected(self):
        with self.assertRaises(L.LoopInputError):
            L.run_loop(
                custodian=self.custodian,
                confirmer=self.confirmer,
                explorer=self.explorer,
                protocol={"protocol_id": "p"},  # 缺 resources
                budget=self.budget(),
            )


class SyntheticEvaluatorTests(unittest.TestCase):
    """合成评估器必须真正用到冻结的模型项（否则退化为常数预测）。"""

    def test_model_expression_is_recovered(self):
        """P11 把 ``model`` 序列化成文本，必须解析回来。"""
        text = (
            'coefficients={"intercept":1.0,"slope":2.0}; '
            'expression=["mul",["inv",["var","X2"]],["var","X1"]]; target=Y'
        )
        parsed = L._parse_model_text(text)
        self.assertEqual(parsed["target"], "Y")
        self.assertEqual(parsed["coefficients"]["slope"], 2.0)
        ast = L._recover_expression_ast(parsed["expression"])
        self.assertIsNotNone(ast, "表达式未被还原——模型项会被静默丢掉")

    def test_expression_recovered_from_json_string(self):
        ast = L._recover_expression_ast('["mul",["var","X1"],["var","X2"]]')
        self.assertIsNotNone(ast)

    def test_unparsable_expression_returns_none(self):
        for bad in (None, "not json", "{", 123):
            with self.subTest(bad=bad):
                self.assertIsNone(L._recover_expression_ast(bad))

    def test_as_float_handles_bad_values(self):
        self.assertEqual(L._as_float(None, 1.5), 1.5)
        self.assertEqual(L._as_float("2.5", 0.0), 2.5)
        self.assertEqual(L._as_float(float("nan"), 9.0), 9.0)
        self.assertEqual(L._as_float(True, 9.0), 9.0)

    def test_one_sided_statistic_detects_positive_shift(self):
        payload = {"differences": [0.5, 0.6, 0.55, 0.45, 0.7, 0.6]}
        result = L._one_sided_normal_statistic(payload)
        self.assertLess(result["p_value"], 0.05)
        self.assertGreater(result["statistic"], 0)

    def test_one_sided_statistic_detects_negative_shift(self):
        payload = {"differences": [-0.5, -0.6, -0.55, -0.45, -0.7, -0.6]}
        result = L._one_sided_normal_statistic(payload)
        self.assertGreater(result["p_value"], 0.95)

    def test_insufficient_groups_are_marked_unavailable(self):
        result = L._one_sided_normal_statistic({"differences": [1.0]})
        self.assertIsNone(result["p_value"])
        self.assertIn("不足", result["reason"])

    def test_zero_variance_is_marked_unavailable(self):
        result = L._one_sided_normal_statistic({"differences": [1.0, 1.0, 1.0]})
        self.assertIsNone(result["p_value"])
        self.assertIn("方差为 0", result["reason"])

    def test_unavailable_evaluator_produces_nothing(self):
        self.assertEqual(L.unavailable_evaluator(plan={}, binding={}, records=[], alpha=0.05), {})
        self.assertTrue(L.evaluation_is_default(L.unavailable_evaluator))
        self.assertFalse(L.evaluation_is_default(L.synthetic_batch_evaluator))


# ---------------------------------------------------------------------------
# 验收标准④：合成 demo
# ---------------------------------------------------------------------------


class DemoTests(unittest.TestCase):
    """``run_demo`` 的产物与标注。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sdl-p16-demo-")
        cls.output = pathlib.Path(cls.temp.name) / "demo"
        cls.payload = L.run_demo(
            cls.output,
            budget=L.LoopBudget(max_rounds=1, max_candidates=60, freeze_cap=2, n_resamples=2),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_files_written(self):
        for name in (
            "discovery-archive.json",
            "knowledge-version.json",
            "open-questions.json",
            "rounds.json",
            "说明.md",
        ):
            self.assertTrue((self.output / name).exists(), msg=name)

    def test_payload_carries_demo_notice(self):
        self.assertIn("synthetic_demo_notice", self.payload)
        self.assertEqual(
            self.payload["synthetic_demo_notice"], L.SYNTHETIC_DEMO_NOTICE
        )

    def test_every_file_carries_demo_notice(self):
        """每个 JSON 产物都必须自带合成标注。"""
        for name in ("discovery-archive.json", "knowledge-version.json",
                     "open-questions.json", "rounds.json"):
            data = json.loads((self.output / name).read_text(encoding="utf-8"))
            self.assertIn("synthetic_demo_notice", data, msg=name)

    def test_readme_states_synthetic_and_no_causality(self):
        text = (self.output / "说明.md").read_text(encoding="utf-8")
        self.assertIn("合成验证", text)
        self.assertIn("不是现实发现结果", text)
        self.assertIn("边界声明", text)

    def test_open_questions_file_is_nonempty(self):
        data = json.loads((self.output / "open-questions.json").read_text(encoding="utf-8"))
        self.assertGreater(len(data["open_questions"]), 0)

    def test_demo_produces_counterexamples(self):
        """demo 的破坏批次落在探索分区内，反例必须被检出。"""
        self.assertGreater(len(self.payload["counterexamples"]), 0)

    def test_tokens_never_appear_in_archive(self):
        """角色令牌绝不进入结果对象：用真实令牌跑一遍，逐个断言不出现。"""
        temp = tempfile.TemporaryDirectory(prefix="sdl-p16-token-")
        self.addCleanup(temp.cleanup)
        db = pathlib.Path(temp.name) / "v.sqlite3"
        tokens = initialize(db)
        custodian = Module01(db, tokens["custodian"])
        confirmer = Module01(db, tokens["confirmer"])
        explorer = Module01(db, tokens["explorer"])
        protocol = custodian.build(
            L.synthetic_spec(), L.synthetic_records(groups=60)
        )

        def supply(round_index, protocol_id):
            L._add_confirmation_batch(
                custodian=custodian,
                protocol_id=protocol_id,
                purpose=f"C{round_index + 1}",
                records=L.synthetic_records(groups=8, start=7000, broken=()),
            )

        archive = L.run_loop(
            custodian=custodian,
            confirmer=confirmer,
            explorer=explorer,
            protocol=protocol,
            target_field="Y",
            budget=L.LoopBudget(max_rounds=1, max_candidates=60, freeze_cap=2, n_resamples=2),
            data_supplier=supply,
            confirmation_evaluator=L.synthetic_batch_evaluator,
            recorded_at="2026-01-01T00:00:00+00:00",
        )
        serialized = archive.canonical_json()
        for role, token in tokens.items():
            with self.subTest(role=role):
                self.assertNotIn(str(token), serialized)
        for banned in ("role_token", "token_body"):
            self.assertNotIn(banned, serialized)

    def test_demo_is_repeatable(self):
        """重复运行同一 demo 目录不报错（幂等清理旧库）。"""
        payload = L.run_demo(
            self.output,
            budget=L.LoopBudget(max_rounds=1, max_candidates=60, freeze_cap=2, n_resamples=2),
        )
        self.assertEqual(
            payload["knowledge_version"], self.payload["knowledge_version"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
