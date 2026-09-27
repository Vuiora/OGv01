"""P07 验收测试：假说对象与不可覆盖版本。

覆盖三条验收标准：

1. **同一 ID 的新版本保留 ``parent_ids`` 链**：``new_version`` 的父链恒为
   「旧链 + 旧版本标识」，只追加不覆盖；跨多代修订链条完整；调用方无法在
   ``changes`` 里自己指定 ``id`` / ``parent_ids`` / ``version``。
2. **已发布版本不可原地修改**：frozen dataclass 挡住属性赋值、深度冻结挡住
   嵌套修改、``publish`` / ``with_evidence`` 的状态守卫挡住生命周期绕过、
   ``HypothesisStore`` 挡住重复登记（没有覆盖写入口）；修改只能经新版本。
3. **``provenance`` 缺失即报错而非默认填充**：整体缺失、不是映射、缺少任意
   必填键、必填键为空、``data_ids`` 为空等情形均抛
   :class:`HypothesisProvenanceError`；且模块源码中不存在任何 provenance 缺省值。

另覆盖：``representation`` 完整变换校验、``predictions`` 可证伪性机检、
标识与版本号的解析往返、序列化往返与规范式稳定性、内容摘要、
仓库只追加与父链回溯，以及 P07 的边界守护——不做假说生成（P08）、
不计算任何指标（P09）、不授予证据等级、不导入 M2/M3 模块、不含第三方依赖。
"""

import ast as pyast
import inspect
import json
import pathlib
import unittest

from sdl_m04.hypothesis import (
    FORBIDDEN_KEYS,
    HYPOTHESIS_FIELDS,
    HYPOTHESIS_MODULE_VERSION,
    HYPOTHESIS_STATUSES,
    HYPOTHESIS_TYPES,
    IDENTITY_SEPARATOR,
    PROVENANCE_REQUIRED_KEYS,
    REPRESENTATION_REQUIRED_KEYS,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    STATUS_RETIRED,
    Hypothesis,
    HypothesisImmutabilityError,
    HypothesisProvenanceError,
    HypothesisStore,
    HypothesisValidationError,
    bump_version,
    make_identity,
    parse_identity,
    require_provenance,
    require_representation,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

#: 模块源码路径，用于静态检查依赖与「无缺省 provenance」等结构性约定。
MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "sdl_m04" / "hypothesis.py"


def provenance() -> dict:
    """一份合法的来源信息（每次返回新对象，避免测试间互相污染）。"""
    return {
        "data_ids": ["E/batch-001", "E/batch-002"],
        "knowledge_version": "K-2026-09",
        "generator": "P07-manual-fixture",
        "code_version": "acceptance-fixture-v1",
    }


def representation() -> dict:
    """一份合法的完整变换声明。"""
    return {
        "missing_value_handling": "drop_rows_with_missing_target",
        "standardization": "zscore_on_E_fit_only",
        "features": ["X1", "X2"],
    }


def predictions() -> list:
    """一份合法的可证伪预测：每条都写明什么结果与假说不相容。"""
    return [
        {
            "expectation": "批均值增益为正",
            "incompatible_with": "在 E 上批均值增益 ≤ 0",
        },
        {
            "expectation": "关系在重采样后仍复现",
            "incompatible_with": "重采样中该关系复现比例低于预设门槛",
        },
    ]


def hypothesis(**overrides) -> Hypothesis:
    """构造一个合法的草稿假说，允许按关键字覆盖字段。"""
    payload = {
        "id": "h-ratio",
        "version": "1",
        "type": "relation",
        "statement": "Y 与 X1/X2 的比值呈线性关系",
        "representation": representation(),
        "predictions": predictions(),
        "null_hypotheses": ["Y 与 X1/X2 的比值无关"],
        "provenance": provenance(),
        "scope": {"objects": "new batches", "environments": ["lab-A"],
                  "time_scope": "2026", "exclusions": ["已污染批次"]},
        "model": "Y ≈ a + b·(X1/X2)",
        "fitted_parameters": {"a": 0.0, "b": 2.0},
        "alternatives": ["Y 与 X1 单独线性相关"],
        "assumptions": ["批次独立"],
        "complexity": {"formula_length": 7},
        "development": {"metrics": {"error": 0.01}},
        "confirmation_plan": {"primary_metric": "batch mean squared loss"},
        "evidence_log": [],
        "status": STATUS_DRAFT,
    }
    payload.update(overrides)
    return Hypothesis(**payload)


# ---------------------------------------------------------------------------
# 验收标准 ③：provenance 缺失即报错，不得默认填充
# ---------------------------------------------------------------------------


class ProvenanceRequiredTests(unittest.TestCase):
    """验收标准 ③。"""

    def test_missing_provenance_raises_not_defaults(self):
        """整体缺失 provenance 必须报错，而不是补一个默认值继续跑。"""
        with self.assertRaises(HypothesisProvenanceError):
            hypothesis(provenance=None)

    def test_from_dict_missing_provenance_raises_provenance_error(self):
        """反序列化时缺失 provenance 抛来源异常（而非一般字段异常）。"""
        payload = hypothesis().to_dict()
        payload.pop("provenance")
        with self.assertRaises(HypothesisProvenanceError):
            Hypothesis.from_dict(payload)

    def test_each_required_key_is_mandatory(self):
        """四个必填键逐一缺失，都必须报错。"""
        for key in PROVENANCE_REQUIRED_KEYS:
            with self.subTest(missing=key):
                payload = provenance()
                payload.pop(key)
                with self.assertRaises(HypothesisProvenanceError):
                    hypothesis(provenance=payload)

    def test_blank_required_key_is_rejected(self):
        """必填键存在但为空，等同缺失——不得被当作「已提供」。"""
        for key in PROVENANCE_REQUIRED_KEYS:
            with self.subTest(blank=key):
                payload = provenance()
                # data_ids 是列表，空白体现在元素上；其余是字符串，直接置空白。
                payload[key] = ["   "] if key == "data_ids" else "   "
                with self.assertRaises(HypothesisProvenanceError):
                    hypothesis(provenance=payload)

    def test_empty_data_ids_rejected(self):
        """``data_ids`` 为空表示假说不指向任何数据，必须报错。"""
        payload = provenance()
        payload["data_ids"] = []
        with self.assertRaises(HypothesisProvenanceError):
            hypothesis(provenance=payload)

    def test_non_string_data_id_rejected(self):
        payload = provenance()
        payload["data_ids"] = ["E/ok", 42]
        with self.assertRaises(HypothesisProvenanceError):
            hypothesis(provenance=payload)

    def test_provenance_not_a_mapping_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(provenance=["E/batch-001"])

    def test_require_provenance_returns_mapping(self):
        """独立校验函数在合法输入上原样返回映射。"""
        payload = provenance()
        self.assertIs(require_provenance(payload), payload)

    def test_extra_provenance_keys_allowed(self):
        """额外来源键允许保留（例如样本指纹），但必填键一个都不能少。"""
        payload = provenance()
        payload["sample_fingerprint"] = "abc123"
        self.assertEqual(
            hypothesis(provenance=payload).provenance["sample_fingerprint"], "abc123"
        )

    def test_no_default_provenance_values_in_source(self):
        """源码中不得出现 provenance 的缺省填充（结构性检查）。

        若未来有人加了「缺 generator 就填 'unknown'」这类兜底，本测试会失败。
        """
        source = MODULE_PATH.read_text(encoding="utf-8")
        for key in PROVENANCE_REQUIRED_KEYS:
            for pattern in (
                f'provenance.get("{key}"',
                f"provenance.get('{key}'",
                f'{{"{key}": "{key}"',
                f'"unknown"',
                f"'unknown'",
            ):
                self.assertNotIn(
                    pattern,
                    source,
                    f"疑似为 provenance.{key} 提供缺省值：{pattern}",
                )

    def test_token_like_keys_rejected(self):
        """令牌类键名不得出现在 provenance / 假说对象中。"""
        for key in sorted(FORBIDDEN_KEYS):
            with self.subTest(forbidden=key):
                payload = provenance()
                payload[key] = "redacted"
                with self.assertRaises(HypothesisValidationError):
                    hypothesis(provenance=payload)

    def test_token_like_key_rejected_case_insensitively(self):
        payload = provenance()
        payload["Role_Token"] = "redacted"
        with self.assertRaises(HypothesisValidationError):
            hypothesis(provenance=payload)


# ---------------------------------------------------------------------------
# 验收标准 ①：同 ID 新版本保留 parent_ids 链
# ---------------------------------------------------------------------------


class ParentChainTests(unittest.TestCase):
    """验收标准 ①。"""

    def test_first_version_has_empty_chain(self):
        self.assertEqual(hypothesis().parent_ids, ())
        self.assertEqual(hypothesis().generation, 0)

    def test_new_version_appends_parent_identity(self):
        v1 = hypothesis()
        v2 = v1.new_version(changes={"statement": "修订后的陈述"})
        self.assertEqual(v2.id, v1.id)
        self.assertEqual(v2.version, "2")
        self.assertEqual(v2.parent_ids, ("h-ratio@1",))
        self.assertEqual(v2.statement, "修订后的陈述")
        # 旧版本未被改动。
        self.assertEqual(v1.statement, "Y 与 X1/X2 的比值呈线性关系")
        self.assertEqual(v1.parent_ids, ())

    def test_chain_is_append_only_across_generations(self):
        """多代修订：链条逐代累加，祖先一个不少。"""
        v1 = hypothesis()
        v2 = v1.new_version(changes={"statement": "第二版"})
        v3 = v2.new_version(changes={"statement": "第三版"})
        v4 = v3.new_version(changes={"statement": "第四版"})
        self.assertEqual(v2.parent_ids, ("h-ratio@1",))
        self.assertEqual(v3.parent_ids, ("h-ratio@1", "h-ratio@2"))
        self.assertEqual(v4.parent_ids, ("h-ratio@1", "h-ratio@2", "h-ratio@3"))
        self.assertEqual(v4.generation, 3)
        self.assertEqual(v4.lineage_ids(), ("h-ratio@1", "h-ratio@2", "h-ratio@3"))

    def test_new_version_resets_status_to_draft(self):
        """发布状态不继承：改了内容就必须重新发布。"""
        v2 = hypothesis().publish().new_version(changes={"statement": "改了"})
        self.assertEqual(v2.status, STATUS_DRAFT)
        self.assertTrue(v2.is_draft)

    def test_caller_cannot_forge_parent_ids(self):
        """调用方不得在 changes 中指定派生字段。"""
        for forbidden in ("id", "parent_ids", "version", "status"):
            with self.subTest(field=forbidden):
                with self.assertRaises(HypothesisImmutabilityError):
                    hypothesis().new_version(changes={forbidden: "hacked"})

    def test_explicit_version_argument_is_used(self):
        v2 = hypothesis().new_version(version="7")
        self.assertEqual(v2.version, "7")
        self.assertEqual(v2.parent_ids, ("h-ratio@1",))

    def test_same_version_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis().new_version(version="1")

    def test_invalid_version_argument_rejected(self):
        for bad in ("0", "01", "v2", "2.0", "", "x"):
            with self.subTest(version=bad):
                with self.assertRaises(HypothesisValidationError):
                    hypothesis().new_version(version=bad)

    def test_new_version_preserves_untouched_fields(self):
        """未在 changes 中提及的字段逐项保留。"""
        v1 = hypothesis()
        v2 = v1.new_version(changes={"statement": "只改陈述"})
        for name in ("id", "type", "representation", "predictions",
                     "null_hypotheses", "provenance", "scope", "model"):
            self.assertEqual(getattr(v2, name), getattr(v1, name), name)


# ---------------------------------------------------------------------------
# 验收标准 ②：已发布版本不可原地修改
# ---------------------------------------------------------------------------


class ImmutabilityTests(unittest.TestCase):
    """验收标准 ②。"""

    def test_attribute_assignment_is_blocked(self):
        h = hypothesis().publish()
        for name in ("statement", "status", "version", "provenance", "predictions"):
            with self.subTest(attribute=name):
                with self.assertRaises(Exception):
                    setattr(h, name, "changed")

    def test_nested_mapping_is_read_only(self):
        """深度冻结：连 provenance 内部的值也改不动。"""
        h = hypothesis().publish()
        with self.assertRaises(TypeError):
            h.provenance["generator"] = "tampered"

    def test_nested_sequence_is_immutable(self):
        h = hypothesis().publish()
        self.assertIsInstance(h.predictions, tuple)
        with self.assertRaises(TypeError):
            h.predictions[0]["expectation"] = "tampered"

    def test_publish_only_changes_status(self):
        draft = hypothesis()
        published = draft.publish()
        before, after = draft.to_dict(), published.to_dict()
        self.assertEqual(before.pop("status"), STATUS_DRAFT)
        self.assertEqual(after.pop("status"), STATUS_PUBLISHED)
        self.assertEqual(before, after)

    def test_publish_does_not_change_identity(self):
        draft = hypothesis()
        published = draft.publish()
        self.assertEqual(published.identity, draft.identity)
        self.assertEqual(published.parent_ids, draft.parent_ids)

    def test_double_publish_rejected(self):
        with self.assertRaises(HypothesisImmutabilityError):
            hypothesis().publish().publish()

    def test_published_version_rejects_evidence_append(self):
        """已发布版本连追加证据都属于修改，必须走 new_version。"""
        published = hypothesis().publish()
        with self.assertRaises(HypothesisImmutabilityError):
            published.with_evidence("new observation")

    def test_draft_may_append_evidence(self):
        draft = hypothesis()
        updated = draft.with_evidence({"at": "2026-09-23", "note": "探索线索 E0"})
        self.assertEqual(len(updated.evidence_log), 1)
        self.assertEqual(len(draft.evidence_log), 0, "原对象不应被改动")

    def test_retired_is_terminal(self):
        retired = hypothesis().retire()
        self.assertTrue(retired.is_retired)
        with self.assertRaises(HypothesisImmutabilityError):
            retired.publish()
        with self.assertRaises(HypothesisImmutabilityError):
            retired.retire()
        with self.assertRaises(HypothesisImmutabilityError):
            retired.with_evidence("late note")

    def test_retired_cannot_spawn_new_version(self):
        with self.assertRaises(HypothesisImmutabilityError):
            hypothesis().retire().new_version(changes={"statement": "试图修订"})

    def test_hood_invariant_of_draft_flow(self):
        """草稿 → 追加证据 → 发布：每一步都返回新对象，历史对象不变。"""
        v1 = hypothesis()
        v1b = v1.with_evidence("trace-1")
        v1c = v1b.publish()
        self.assertEqual(v1.evidence_log, ())
        self.assertEqual(len(v1b.evidence_log), 1)
        self.assertTrue(v1c.is_published)
        self.assertEqual(v1b.status, STATUS_DRAFT)


# ---------------------------------------------------------------------------
# 版本仓库：只追加
# ---------------------------------------------------------------------------


class StoreTests(unittest.TestCase):
    """仓库层对「不可覆盖」的强制。"""

    def test_register_then_get_roundtrip(self):
        store = HypothesisStore()
        h = store.register(hypothesis())
        self.assertIs(store.get("h-ratio@1"), h)
        self.assertIn("h-ratio@1", store)
        self.assertEqual(len(store), 1)

    def test_register_same_identity_rejected(self):
        """没有覆盖写入口：同一标识再登记一次即报错。"""
        store = HypothesisStore()
        store.register(hypothesis())
        with self.assertRaises(HypothesisImmutabilityError):
            store.register(hypothesis(statement="不同的内容但同一标识"))

    def test_register_rejects_non_hypothesis(self):
        store = HypothesisStore()
        with self.assertRaises(HypothesisValidationError):
            store.register({"id": "h-ratio", "version": "1"})

    def test_revise_keeps_published_version_intact(self):
        """核心断言：修订已发布版本后，旧版本在仓库中逐字节不变。"""
        store = HypothesisStore()
        draft = store.register(hypothesis())
        store.publish("h-ratio@1")
        frozen_snapshot = store.get("h-ratio@1").to_dict()
        self.assertEqual(frozen_snapshot["status"], STATUS_PUBLISHED)

        revised = store.revise("h-ratio@1", changes={"statement": "修订后的陈述"})
        self.assertEqual(revised.version, "2")
        self.assertEqual(revised.parent_ids, ("h-ratio@1",))
        self.assertEqual(revised.status, STATUS_DRAFT)
        # 仓库中 v1 仍是已发布版本，且逐字段未被改动。
        self.assertEqual(store.get("h-ratio@1").to_dict(), frozen_snapshot)
        # 当初登记时拿到的那份草稿对象本身也不受影响（对象不可变）。
        self.assertEqual(draft.status, STATUS_DRAFT)
        self.assertEqual(draft.statement, frozen_snapshot["statement"])

    def test_publish_via_store_marks_status(self):
        store = HypothesisStore()
        store.register(hypothesis())
        published = store.publish("h-ratio@1")
        self.assertTrue(published.is_published)
        self.assertTrue(store.get("h-ratio@1").is_published)

    def test_store_double_publish_rejected(self):
        store = HypothesisStore()
        store.register(hypothesis())
        store.publish("h-ratio@1")
        with self.assertRaises(HypothesisImmutabilityError):
            store.publish("h-ratio@1")

    def test_get_unknown_identity_raises(self):
        store = HypothesisStore()
        with self.assertRaises(HypothesisValidationError):
            store.get("h-ratio@1")

    def test_id_version_index_and_ordering(self):
        store = HypothesisStore()
        store.register(hypothesis(version="1"))
        store.register(hypothesis(version="2", parent_ids=["h-ratio@1"]))
        store.register(hypothesis(id="h-other", version="1"))
        self.assertEqual(store.versions("h-ratio"), ("1", "2"))
        self.assertEqual(store.ids(), ("h-other", "h-ratio"))
        self.assertEqual(store.latest("h-ratio").version, "2")
        self.assertEqual([h.version for h in store.history("h-ratio")], ["1", "2"])
        # 迭代顺序确定：先按 id，再按版本号数值升序。
        self.assertEqual([h.identity for h in store],
                         ["h-other@1", "h-ratio@1", "h-ratio@2"])

    def test_latest_of_unknown_id_raises(self):
        with self.assertRaises(HypothesisValidationError):
            HypothesisStore().latest("nope")

    def test_lineage_walks_back_to_root(self):
        store = HypothesisStore()
        store.register(hypothesis(version="1"))
        store.register(hypothesis(version="2", parent_ids=["h-ratio@1"]))
        store.register(hypothesis(version="3", parent_ids=["h-ratio@1", "h-ratio@2"]))
        chain = store.lineage("h-ratio@3")
        self.assertEqual([h.identity for h in chain],
                         ["h-ratio@1", "h-ratio@2", "h-ratio@3"])

    def test_lineage_stops_at_missing_ancestor(self):
        """分批复建历史时父标识可能尚未登记：回溯到缺口即停，不报错。"""
        store = HypothesisStore()
        store.register(hypothesis(version="3", parent_ids=["h-ratio@1", "h-ratio@2"]))
        chain = store.lineage("h-ratio@3")
        self.assertEqual([h.identity for h in chain], ["h-ratio@3"])

    def test_strict_chain_enforces_registered_parents(self):
        strict = HypothesisStore(strict_chain=True)
        with self.assertRaises(HypothesisValidationError):
            strict.register(hypothesis(version="2", parent_ids=["h-ratio@1"]))
        # 补齐父版本后即可登记。
        strict.register(hypothesis(version="1"))
        strict.register(hypothesis(version="2", parent_ids=["h-ratio@1"]))
        self.assertEqual(len(strict), 2)

    def test_strict_chain_rejects_non_bool(self):
        with self.assertRaises(HypothesisValidationError):
            HypothesisStore(strict_chain="yes")

    def test_store_serialization_roundtrip(self):
        store = HypothesisStore()
        store.register(hypothesis())
        store.publish("h-ratio@1")
        store.revise("h-ratio@1", changes={"statement": "第二版"})
        payload = store.to_dict()
        restored = HypothesisStore.from_dict(payload)
        self.assertEqual(restored.to_dict(), payload)

    def test_store_from_dict_rejects_duplicate_identity(self):
        store = HypothesisStore()
        store.register(hypothesis())
        payload = store.to_dict()
        payload["hypotheses"].append(payload["hypotheses"][0])
        with self.assertRaises(HypothesisImmutabilityError):
            HypothesisStore.from_dict(payload)

    def test_store_from_dict_checks_declared_size(self):
        store = HypothesisStore()
        store.register(hypothesis())
        payload = store.to_dict()
        payload["size"] = 99
        with self.assertRaises(HypothesisValidationError):
            HypothesisStore.from_dict(payload)

    def test_store_from_dict_rejects_unknown_field(self):
        store = HypothesisStore()
        store.register(hypothesis())
        payload = store.to_dict()
        payload["extra"] = 1
        with self.assertRaises(HypothesisValidationError):
            HypothesisStore.from_dict(payload)

    def test_store_to_dict_is_deterministic(self):
        store = HypothesisStore()
        store.register(hypothesis(version="2", parent_ids=["h-ratio@1"]))
        store.register(hypothesis(version="1"))
        first = json.dumps(store.to_dict(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(store.to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 字段校验：representation / predictions / 受控词表
# ---------------------------------------------------------------------------


class FieldValidationTests(unittest.TestCase):
    """字段级契约。"""

    def test_representation_requires_both_keys(self):
        for key in REPRESENTATION_REQUIRED_KEYS:
            with self.subTest(missing=key):
                payload = representation()
                payload.pop(key)
                with self.assertRaises(HypothesisValidationError):
                    hypothesis(representation=payload)

    def test_representation_must_be_mapping(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(representation="zscore")

    def test_representation_empty_mapping_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(representation={})

    def test_require_representation_returns_mapping(self):
        payload = representation()
        self.assertIs(require_representation(payload), payload)

    def test_predictions_require_incompatible_with(self):
        bad = [{"expectation": "增益为正"}]  # 缺 incompatible_with
        with self.assertRaises(HypothesisValidationError):
            hypothesis(predictions=bad)

    def test_predictions_must_be_non_empty(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(predictions=[])

    def test_blank_incompatible_with_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(predictions=[{"incompatible_with": "   "}])

    def test_predictions_items_must_be_mappings(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(predictions=["增益为正"])

    def test_null_hypotheses_required(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(null_hypotheses=[])
        with self.assertRaises(HypothesisValidationError):
            hypothesis(null_hypotheses=["   "])

    def test_unknown_type_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(type="causal")

    def test_all_declared_types_accepted(self):
        for kind in HYPOTHESIS_TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(hypothesis(type=kind).type, kind)

    def test_unknown_status_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(status="confirmed")

    def test_statuses_are_lifecycle_not_evidence_grades(self):
        """状态词表是生命周期标记，不得出现 E0/E1/E2 之类的证据等级。"""
        for status in HYPOTHESIS_STATUSES:
            self.assertNotIn("E", status)
        self.assertEqual(set(HYPOTHESIS_STATUSES),
                         {STATUS_DRAFT, STATUS_PUBLISHED, STATUS_RETIRED})

    def test_statement_must_be_non_blank(self):
        for bad in ("", "   ", None, 42):
            with self.subTest(statement=bad):
                with self.assertRaises(HypothesisValidationError):
                    hypothesis(statement=bad)

    def test_scope_must_be_mapping(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(scope=["lab-A"])

    def test_non_finite_number_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(fitted_parameters={"b": float("inf")})
        with self.assertRaises(HypothesisValidationError):
            hypothesis(fitted_parameters={"b": float("nan")})

    def test_non_jsonable_value_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(scope={"env": object()})

    def test_model_accepts_string_mapping_or_none(self):
        self.assertEqual(hypothesis(model=None).model, None)
        self.assertEqual(hypothesis(model="linear").model, "linear")
        self.assertEqual(hypothesis(model={"form": "linear"}).model["form"], "linear")
        with self.assertRaises(HypothesisValidationError):
            hypothesis(model=3)
        with self.assertRaises(HypothesisValidationError):
            hypothesis(model="   ")


# ---------------------------------------------------------------------------
# 标识与版本号
# ---------------------------------------------------------------------------


class IdentityTests(unittest.TestCase):
    """标识与版本号的构造与解析。"""

    def test_make_and_parse_roundtrip(self):
        identity = make_identity("h-ratio", "12")
        self.assertEqual(identity, "h-ratio@12")
        self.assertEqual(parse_identity(identity), ("h-ratio", "12"))
        self.assertEqual(hypothesis().identity, "h-ratio@1")

    def test_parse_rejects_missing_separator(self):
        with self.assertRaises(HypothesisValidationError):
            parse_identity("h-ratio")

    def test_parse_rejects_bad_version(self):
        for bad in ("h-ratio@", "h-ratio@0", "h-ratio@x", "h-ratio@1.0"):
            with self.subTest(identity=bad):
                with self.assertRaises(HypothesisValidationError):
                    parse_identity(bad)

    def test_id_charset_is_restricted(self):
        """id 的字符集排除分隔符，使 id@version 的解析唯一。"""
        for bad in ("h ratio", "h@ratio", "-leading", ".leading", "", "h/x"):
            with self.subTest(id=bad):
                with self.assertRaises(HypothesisValidationError):
                    make_identity(bad, "1")

    def test_bump_version(self):
        self.assertEqual(bump_version("1"), "2")
        self.assertEqual(bump_version("9"), "10")
        self.assertEqual(bump_version("99"), "100")

    def test_bump_version_rejects_bad_input(self):
        for bad in ("0", "01", "v1", "", 1, None):
            with self.subTest(version=bad):
                with self.assertRaises(HypothesisValidationError):
                    bump_version(bad)

    def test_separator_is_at_sign(self):
        self.assertEqual(IDENTITY_SEPARATOR, "@")

    def test_parent_ids_must_be_identities(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(parent_ids=["h-ratio"])  # 缺版本号
        with self.assertRaises(HypothesisValidationError):
            hypothesis(parent_ids=[42])

    def test_duplicate_parent_ids_rejected(self):
        with self.assertRaises(HypothesisValidationError):
            hypothesis(parent_ids=["h-ratio@1", "h-ratio@1"])


# ---------------------------------------------------------------------------
# 序列化与内容摘要
# ---------------------------------------------------------------------------


class SerializationTests(unittest.TestCase):
    """往返一致、键序稳定、摘要可复现。"""

    def test_to_dict_has_all_contract_fields(self):
        payload = hypothesis().to_dict()
        self.assertEqual(tuple(payload), HYPOTHESIS_FIELDS)

    def test_roundtrip_preserves_content(self):
        h = hypothesis()
        self.assertEqual(Hypothesis.from_dict(h.to_dict()), h)
        self.assertEqual(Hypothesis.from_dict(h.to_dict()).canonical_json(),
                         h.canonical_json())

    def test_roundtrip_after_publish_and_revision(self):
        h = hypothesis().publish().new_version(changes={"statement": "第二版"})
        restored = Hypothesis.from_dict(json.loads(json.dumps(h.to_dict(),
                                                              ensure_ascii=False)))
        self.assertEqual(restored, h)
        self.assertEqual(restored.parent_ids, ("h-ratio@1",))

    def test_json_serializable(self):
        text = json.dumps(hypothesis().to_dict(), ensure_ascii=False)
        self.assertIn("h-ratio", text)

    def test_from_dict_rejects_unknown_field(self):
        payload = hypothesis().to_dict()
        payload["mystery"] = 1
        with self.assertRaises(HypothesisValidationError):
            Hypothesis.from_dict(payload)

    def test_from_dict_rejects_missing_required_field(self):
        for key in ("id", "version", "type", "statement", "representation",
                    "predictions", "null_hypotheses"):
            with self.subTest(missing=key):
                payload = hypothesis().to_dict()
                payload.pop(key)
                with self.assertRaises(HypothesisValidationError):
                    Hypothesis.from_dict(payload)

    def test_from_dict_rejects_non_mapping(self):
        with self.assertRaises(HypothesisValidationError):
            Hypothesis.from_dict(["not", "a", "mapping"])

    def test_equality_is_order_insensitive_for_mappings(self):
        a = hypothesis(scope={"b": 2, "a": 1})
        b = hypothesis(scope={"a": 1, "b": 2})
        self.assertEqual(a, b)
        self.assertEqual(a.canonical_json(), b.canonical_json())

    def test_inequality_on_different_content(self):
        self.assertNotEqual(hypothesis(), hypothesis(statement="别的陈述"))

    def test_hash_is_consistent_with_equality(self):
        """哈希与判等必须一致：内容相同则哈希相同（Python 数据模型要求）。"""
        a = hypothesis()
        self.assertEqual(hash(a), hash(hypothesis()))
        b = hypothesis(scope={"a": 1, "b": 2})
        c = hypothesis(scope={"b": 2, "a": 1})
        self.assertEqual(b, c)
        self.assertEqual(hash(b), hash(c))
        self.assertEqual(len({b, c}), 1)

    def test_hash_distinguishes_different_content(self):
        """同标识不同内容的对象必须能在集合中区分开。"""
        a = hypothesis()
        b = hypothesis(statement="别的陈述")
        self.assertNotEqual(a, b)
        self.assertEqual(len({a, b}), 2)
        c = a.new_version()
        self.assertNotEqual(hash(a), hash(c))

    def test_content_digest_changes_after_publish(self):
        draft = hypothesis()
        self.assertNotEqual(draft.content_digest(), draft.publish().content_digest())

    def test_content_digest_is_stable_and_sized(self):
        h = hypothesis()
        self.assertEqual(h.content_digest(), hypothesis().content_digest())
        self.assertEqual(len(h.content_digest()), 64)

    def test_module_version_is_recorded_in_store(self):
        store = HypothesisStore()
        self.assertEqual(store.to_dict()["module_version"], HYPOTHESIS_MODULE_VERSION)


# ---------------------------------------------------------------------------
# P07 边界守护
# ---------------------------------------------------------------------------


class BoundaryTests(unittest.TestCase):
    """本阶段不得越界：不生成假说（P08）、不计算指标（P09）。"""

    @classmethod
    def setUpClass(cls):
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        cls.tree = pyast.parse(cls.source)

    def test_no_third_party_imports(self):
        """只用标准库；不得导入 M2 / M3 或任何第三方包。"""
        allowed = {"__future__", "hashlib", "json", "re", "dataclasses", "types",
                   "typing"}
        imported: set[str] = set()
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, pyast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertTrue(
            imported <= allowed,
            f"出现非预期导入：{sorted(imported - allowed)}",
        )

    def test_does_not_import_earlier_modules(self):
        """P07 依赖只有 P00：不得导入 sdl_m02 / sdl_m03。

        只检查**导入语句**（文档中提到「不导入 sdl_m03」是允许的）。
        """
        for node in pyast.walk(self.tree):
            modules: list[str] = []
            if isinstance(node, pyast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, pyast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                self.assertFalse(
                    module.startswith("sdl_m02") or module.startswith("sdl_m03"),
                    f"不得导入前序模块：{module}",
                )

    def test_no_metric_computation_functions(self):
        """不得出现指标计算入口（属 P09）。"""
        public = {
            name for name, obj in inspect.getmembers(
                __import__("sdl_m04.hypothesis", fromlist=["*"]), inspect.isfunction
            )
        }
        for forbidden in ("gain", "novelty", "stability_score", "evaluate_hypothesis",
                          "pareto", "weighted_score"):
            self.assertNotIn(forbidden, public)

    def test_no_pattern_generation_entrypoints(self):
        """不得出现从模式生成假说的入口（属 P08）。"""
        public = {
            name for name, obj in inspect.getmembers(
                __import__("sdl_m04.hypothesis", fromlist=["*"]), inspect.isfunction
            )
        }
        for forbidden in ("hypotheses_from_patterns", "attach_nulls",
                          "attach_alternatives"):
            self.assertNotIn(forbidden, public)

    def test_no_evidence_grade_vocabulary(self):
        """不授予证据等级：不得出现 E0/E1/E2 赋值或「因果发现」判定。"""
        for token in ("E0", "E1", "E2", "evidence_grade"):
            # 说明文字中可提及「证据等级属 P15」，但不得作为取值出现。
            self.assertNotIn(f'"{token}"', self.source)
            self.assertNotIn(f"'{token}'", self.source)

    def test_no_token_persistence(self):
        """不得读取、打印或持久化令牌正文。"""
        for token in ("print(", "logging", "open(", "sqlite3"):
            self.assertNotIn(token, self.source)

    def test_no_ellipsis_placeholders(self):
        """禁止省略号占位：不得出现裸 ``...`` 语句。

        注意类型注解中的 ``tuple[str, ...]`` 是合法的，因此这里检查的是
        **语句级**的 ``Ellipsis``（即 ``...`` 被单独用作函数体占位）。
        """
        for node in pyast.walk(self.tree):
            if isinstance(node, pyast.Expr) and isinstance(node.value, pyast.Constant):
                self.assertIsNot(
                    node.value.value, Ellipsis,
                    f"第 {node.lineno} 行出现裸省略号占位",
                )
        for marker in ("pass  # TODO", "NotImplementedError"):
            self.assertNotIn(marker, self.source)

    def test_class_is_frozen(self):
        """对象不可变是结构性的：dataclass 必须声明 frozen=True。"""
        node = next(
            n for n in self.tree.body
            if isinstance(n, pyast.ClassDef) and n.name == "Hypothesis"
        )
        decorators = [pyast.unparse(d) for d in node.decorator_list]
        self.assertTrue(any("frozen=True" in d for d in decorators),
                        f"Hypothesis 必须 frozen，实际装饰器：{decorators}")

    def test_store_has_no_overwrite_method(self):
        """仓库不得提供任何「覆盖」入口。"""
        store_methods = {n.name for n in self.tree.body
                         if isinstance(n, pyast.ClassDef) and n.name == "HypothesisStore"
                         for n in n.body if isinstance(n, pyast.FunctionDef)}
        for forbidden in ("update", "overwrite", "replace", "set_version"):
            self.assertNotIn(forbidden, store_methods)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
