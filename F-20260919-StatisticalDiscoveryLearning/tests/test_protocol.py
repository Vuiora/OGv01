"""Integration checks of observable evidence isolation and irreversible use."""

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from sdl_m01 import Module01, initialize
from sdl_m01.errors import AccessDenied, IntegrityError, StateError, ValidationError
from tests.helpers import evaluation, frozen_plan, renamed, sample_records, sample_spec


class ProtocolAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdl-m01-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "vault.sqlite3"
        self.tokens = initialize(self.db)
        self.custodian = Module01(self.db, self.tokens["custodian"])
        self.explorer = Module01(self.db, self.tokens["explorer"])
        self.confirmer = Module01(self.db, self.tokens["confirmer"])
        self.auditor = Module01(self.db, self.tokens["auditor"])
        self.spec = sample_spec()
        self.records = sample_records()
        self.protocol = self.custodian.build(self.spec, self.records)
        self.refs = self.protocol["resources"]

    def bind(self, round_index=1, ref=None):
        return self.confirmer.bind_confirmation(
            ref or self.refs["C1"], frozen_plan(self.protocol, self.spec, round_index)
        )

    def consume_and_record(self, status="inconclusive"):
        binding = self.bind()
        contents = self.confirmer.consume_confirmation(binding["binding_id"])
        result = self.confirmer.record_evaluation(binding["binding_id"], evaluation(status))
        return binding, contents, result

    def test_tokens_authenticate_roles_and_cannot_reset_existing_vault(self):
        self.assertEqual(set(self.tokens), {"custodian", "explorer", "confirmer", "auditor"})
        self.assertEqual(len(set(self.tokens.values())), 4)
        with self.assertRaises(AccessDenied):
            Module01(self.db, "invalid-bearer-token")
        with self.assertRaises((ValidationError, FileExistsError, StateError)):
            initialize(self.db)
        with self.assertRaises(AccessDenied):
            self.explorer.build(self.spec, sample_records(start=100))
        with self.assertRaises(AccessDenied):
            self.explorer.ledger()
        with self.assertRaises(AccessDenied):
            self.explorer.verify_integrity()

    def test_client_role_cannot_be_changed_to_bypass_authorization(self):
        binding, _, _ = self.consume_and_record()
        with self.assertRaises((AttributeError, AccessDenied)):
            self.explorer.role = "custodian"
        self.assertEqual(self.explorer.role, "explorer")
        with self.assertRaises(AccessDenied):
            self.explorer.quality(self.refs["C1"])
        with self.assertRaises((AccessDenied, StateError)):
            self.explorer.results(binding["binding_id"])

    def test_public_protocol_has_opaque_refs_and_no_observation_content(self):
        self.assertTrue({"E", "V", "C1"}.issubset(self.refs))
        for actor in (self.explorer, self.confirmer, self.auditor):
            public = actor.describe(self.protocol["protocol_id"])
            payload = json.dumps(public, ensure_ascii=False)
            self.assertNotIn("private-observation-", payload)
            self.assertNotIn("synthetic://module-01-acceptance", payload)
            self.assertNotIn('"fingerprint"', payload)
            self.assertNotIn('"missing_rates"', payload)
            self.assertNotIn('"status_counts"', payload)
            self.assertNotIn('"raw_records"', payload)
            for token in self.tokens.values():
                self.assertNotIn(token, payload)

    def test_sealed_contents_and_quality_never_available_to_explorer(self):
        for actor in (self.explorer, self.custodian, self.confirmer, self.auditor):
            with self.subTest(actor=actor):
                with self.assertRaises(AccessDenied):
                    actor.read_dataset(self.refs["C1"])
        with self.assertRaises(AccessDenied):
            self.explorer.quality(self.refs["C1"])
        self.assertIsInstance(self.custodian.quality(self.refs["C1"]), dict)
        self.assertIsInstance(self.auditor.quality(self.refs["C1"]), dict)
        self.assertIsInstance(self.explorer.quality(self.refs["E"]), dict)
        self.assertIsInstance(self.explorer.quality(self.refs["V"]), dict)
        with self.assertRaises((StateError, ValidationError)):
            self.confirmer.consume_confirmation(self.refs["C1"])

    def test_exploration_snapshot_is_copied_and_reads_do_not_mutate_storage(self):
        expected = self.explorer.read_dataset(self.refs["E"])
        self.assertTrue(expected)
        self.assertTrue(all("_quality" in row for row in expected))
        self.records[0]["values"]["Y"] = -987654.0
        self.spec["task"]["objects"] = "mutated after build"
        returned = self.explorer.read_dataset(self.refs["E"])
        returned[0]["values"]["Y"] = -987655.0
        self.assertEqual(self.explorer.read_dataset(self.refs["E"]), expected)
        self.assertNotIn("mutated after build", json.dumps(self.explorer.describe(self.protocol["protocol_id"])))

    def test_complete_freeze_required_and_exploration_only_fit_scope(self):
        for missing in ("hypotheses", "test_family", "stopping_rule", "inference_unit"):
            plan = frozen_plan(self.protocol)
            del plan[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(ValidationError):
                    self.confirmer.bind_confirmation(self.refs["C1"], plan)
        plan = frozen_plan(self.protocol)
        plan["preprocessing"]["fit_dataset_refs"] = [self.refs["V"]]
        with self.assertRaises(ValidationError):
            self.confirmer.bind_confirmation(self.refs["C1"], plan)
        plan["preprocessing"]["fit_dataset_refs"] = [self.refs["C1"]]
        with self.assertRaises(ValidationError):
            self.confirmer.bind_confirmation(self.refs["C1"], plan)
        with self.assertRaises((ValidationError, StateError)):
            self.confirmer.bind_confirmation(self.refs["E"], frozen_plan(self.protocol))

    def test_freeze_rejects_policy_mismatch_nonfinite_and_unknown_tests(self):
        changes = (
            ("protocol_id", "other-protocol"), ("inference_unit", "reading"),
            ("quality_rules_version", "99"), ("sampling_plan", "pick good-looking batches"),
            ("stopping_rule", "until significant"), ("eligibility", "only accurate predictions"),
            ("effect_threshold", float("nan")), ("round_index", 0),
        )
        for key, value in changes:
            plan = frozen_plan(self.protocol)
            plan[key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    self.confirmer.bind_confirmation(self.refs["C1"], plan)
        plan = frozen_plan(self.protocol)
        plan["test_family"][0]["hypothesis_id"] = "unregistered-hypothesis"
        with self.assertRaises(ValidationError):
            self.confirmer.bind_confirmation(self.refs["C1"], plan)

    def test_frozen_inputs_are_copied_and_revised_candidate_cannot_reuse_batch(self):
        plan = frozen_plan(self.protocol)
        binding = self.confirmer.bind_confirmation(self.refs["C1"], plan)
        self.assertAlmostEqual(binding["alpha"], 0.025)
        self.assertTrue(binding["plan_digest"])
        plan["hypotheses"][0]["parameters"]["slope"] = 9000
        plan["hypotheses"][0]["version"] = "2"
        plan["round_index"] = 2
        with closing(sqlite3.connect(self.db)) as connection, connection:
            stored = json.loads(connection.execute(
                "SELECT plan FROM bindings WHERE id = ?", (binding["binding_id"],)
            ).fetchone()[0])
        self.assertEqual(stored["hypotheses"][0]["parameters"]["slope"], 2.0)
        self.assertEqual(stored["hypotheses"][0]["version"], "1")
        self.assertEqual(stored["round_index"], 1)
        with self.assertRaises((StateError, ValidationError)):
            self.confirmer.bind_confirmation(self.refs["C1"], plan)
        rows = self.confirmer.consume_confirmation(binding["binding_id"])
        self.assertTrue(rows)
        with self.assertRaises((StateError, ValidationError)):
            self.confirmer.bind_confirmation(self.refs["C1"], plan)
        self.assertEqual(rows, self.confirmer.consume_confirmation(binding["binding_id"], replay=True))

    def test_consumption_is_persistently_single_use_and_explicit_replay_is_same_evidence(self):
        binding = self.bind()
        before = len(self.auditor.ledger())
        contents = self.confirmer.consume_confirmation(binding["binding_id"])
        # A distinct SQLite connection sees the committed 'used' state at the
        # moment consume has returned data, even if no evaluation follows.
        with closing(sqlite3.connect(self.db)) as connection, connection:
            state = connection.execute(
                "SELECT state FROM datasets WHERE ref = ?", (self.refs["C1"],)
            ).fetchone()[0]
            consumed = connection.execute(
                "SELECT COUNT(*) FROM ledger WHERE action = 'consume'"
            ).fetchone()[0]
        self.assertEqual(state, "used")
        self.assertEqual(consumed, 1)
        after = self.auditor.ledger()
        self.assertGreater(len(after), before)
        reopened = Module01(self.db, self.tokens["confirmer"])
        with self.assertRaises(StateError):
            reopened.consume_confirmation(binding["binding_id"])
        contents[0]["values"]["Y"] = -987655.0
        replay = reopened.consume_confirmation(binding["binding_id"], replay=True)
        self.assertNotEqual(contents, replay)
        for actor in (self.explorer, self.custodian):
            with self.assertRaises(AccessDenied):
                actor.read_dataset(self.refs["C1"])

    def test_simultaneous_consumers_get_only_one_first_use(self):
        binding = self.bind()
        ready = threading.Barrier(2)

        def consume_once():
            actor = Module01(self.db, self.tokens["confirmer"])
            ready.wait(timeout=10)
            try:
                return "success", actor.consume_confirmation(binding["binding_id"])
            except StateError:
                return "already-used", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume_once) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(result[0] for result in results), ["already-used", "success"])
        self.assertTrue(next(rows for status, rows in results if status == "success"))
        self.assertIsInstance(self.auditor.verify_integrity(), dict)

    def test_failed_evaluation_is_preserved_and_same_binding_can_recover(self):
        binding = self.bind()
        with self.assertRaises(StateError):
            self.confirmer.record_evaluation(binding["binding_id"], evaluation())
        self.confirmer.consume_confirmation(binding["binding_id"])
        failed = evaluation("failed")
        failed["notes"] = "simulated external executor failure before computing a result"
        self.confirmer.record_evaluation(binding["binding_id"], failed)
        with self.assertRaises(StateError):
            self.confirmer.consume_confirmation(binding["binding_id"])
        self.confirmer.consume_confirmation(binding["binding_id"], replay=True)
        self.confirmer.record_evaluation(binding["binding_id"], evaluation("inconclusive"))
        with self.assertRaises(StateError):
            self.confirmer.record_evaluation(binding["binding_id"], evaluation("supported"))
        ledger = json.dumps(self.auditor.ledger())
        self.assertIn("failed", ledger)

    def test_results_require_release_then_archive_preserves_old_identity(self):
        binding = self.bind()
        bid = binding["binding_id"]
        with self.assertRaises(StateError):
            self.confirmer.release_results(bid)
        with self.assertRaises((AccessDenied, StateError)):
            self.explorer.results(bid)
        self.confirmer.consume_confirmation(bid)
        self.confirmer.record_evaluation(bid, evaluation())
        with self.assertRaises(StateError):
            self.custodian.archive_confirmation(bid)
        self.confirmer.release_results(bid)
        released = self.explorer.results(bid)
        self.assertIn("inconclusive", json.dumps(released))
        self.assertNotIn("private-observation-", json.dumps(released))
        historical = self.custodian.archive_confirmation(bid)["historical_ref"]
        rows = self.explorer.read_dataset(historical)
        self.assertTrue(rows)
        with self.assertRaises(AccessDenied):
            self.explorer.read_dataset(self.refs["C1"])
        with self.assertRaises((StateError, ValidationError)):
            self.confirmer.bind_confirmation(historical, frozen_plan(self.protocol, round_index=2))
        with self.assertRaises((StateError, ValidationError)):
            self.confirmer.bind_confirmation(self.refs["C1"], frozen_plan(self.protocol, round_index=2))

    def test_emergency_inspection_irreversibly_compromises_sealed_evidence(self):
        rows = self.custodian.inspect_confirmation(self.refs["C1"], "authorized synthetic audit fixture")
        self.assertTrue(rows)
        with self.assertRaises((StateError, ValidationError)):
            self.bind()
        self.assertIn("compromis", json.dumps(self.auditor.ledger()).lower())

    def test_bound_evidence_can_be_compromised_and_then_cannot_be_consumed(self):
        binding = self.bind()
        self.custodian.mark_compromised(self.refs["C1"], "accidental external exposure reported")
        with self.assertRaises(StateError):
            self.confirmer.consume_confirmation(binding["binding_id"])

    def test_only_new_units_enter_added_confirmation_and_round_budget_is_unique(self):
        self.bind()
        with self.assertRaises((ValidationError, StateError)):
            self.custodian.add_confirmation(self.protocol["protocol_id"], renamed(self.records), "C2")
        old_unit_new_reading = sample_records(groups=1)
        old_unit_new_reading[0]["record_id"] = "new-row-old-batch"
        old_unit_new_reading[0]["values"]["Y"] += 800
        with self.assertRaises((ValidationError, StateError)):
            self.custodian.add_confirmation(self.protocol["protocol_id"], old_unit_new_reading[:1], "C2")
        new_ref = self.custodian.add_confirmation(
            self.protocol["protocol_id"], sample_records(groups=2, start=100), "C2"
        )["ref"]
        with self.assertRaises((ValidationError, StateError)):
            self.bind(round_index=1, ref=new_ref)
        binding = self.bind(round_index=2, ref=new_ref)
        self.assertAlmostEqual(binding["alpha"], 0.0125)
        with self.assertRaises(AccessDenied):
            self.explorer.read_dataset(new_ref)

    def test_rebuild_cannot_rename_records_to_reset_evidence_identity(self):
        self.consume_and_record()
        changed = copy.deepcopy(self.spec)
        changed["split"]["seed"] = 9182
        with self.assertRaises((ValidationError, StateError)):
            self.custodian.build(changed, renamed(self.records))

    def test_rejected_ingestion_rolls_back_fresh_records_in_same_request(self):
        fresh = sample_records(groups=2, start=200)
        mixed = fresh + renamed(self.records[:1])
        with self.assertRaises((ValidationError, StateError)):
            self.custodian.add_confirmation(self.protocol["protocol_id"], mixed, "C2")
        # The rejected mixed request must not poison its otherwise-new records.
        added = self.custodian.add_confirmation(self.protocol["protocol_id"], fresh, "C2")
        self.assertTrue(added["ref"])
        self.assertIsInstance(self.auditor.verify_integrity(), dict)

    def test_authenticated_denials_are_logged_without_tokens_or_secret_payloads(self):
        before = len(self.auditor.ledger())
        with self.assertRaises(AccessDenied):
            self.explorer.read_dataset(self.refs["C1"])
        events = self.auditor.ledger()
        self.assertGreater(len(events), before)
        log = json.dumps(events[before:])
        self.assertNotIn("private-observation-", log)
        for token in self.tokens.values():
            self.assertNotIn(token, log)
        self.assertTrue(any(word in log.lower() for word in ("denied", "error", "failed")))

    def test_healthy_vault_integrity_survives_failure_and_lifecycle_events(self):
        binding, _, _ = self.consume_and_record()
        self.confirmer.release_results(binding["binding_id"])
        self.custodian.archive_confirmation(binding["binding_id"])
        with self.assertRaises(AccessDenied):
            self.explorer.read_dataset(self.refs["C1"])
        self.assertIsInstance(self.auditor.verify_integrity(), dict)

    @staticmethod
    def drop_write_guards(connection, table):
        """Emulate an administrator with direct DB access, beyond API security."""
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        ).fetchall()
        for (name,) in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')

    def tamper_confirmation_snapshot(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.drop_write_guards(connection, "snapshots")
            connection.execute(
                "UPDATE snapshots SET payload = ? WHERE id = "
                "(SELECT snapshot_id FROM datasets WHERE ref = ?)",
                ("[]", self.refs["C1"]),
            )

    def test_snapshot_tampering_is_detected_before_confirmation_return(self):
        binding = self.bind()
        self.tamper_confirmation_snapshot()
        with self.assertRaises(IntegrityError):
            self.confirmer.consume_confirmation(binding["binding_id"])
        with self.assertRaises(IntegrityError):
            self.auditor.verify_integrity()

    def test_failed_consumption_after_digest_check_remains_used_and_is_logged(self):
        binding = self.bind()
        self.tamper_confirmation_snapshot()
        with self.assertRaises(IntegrityError):
            self.confirmer.consume_confirmation(binding["binding_id"])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            state = connection.execute(
                "SELECT state FROM datasets WHERE ref = ?", (self.refs["C1"],)
            ).fetchone()[0]
        self.assertEqual(state, "used")
        events = self.auditor.ledger()
        failures = [event for event in events if event["action"] == "operation_failed"]
        self.assertTrue(any(
            event["details"].get("error_type") == "IntegrityError"
            and "consum" in event["details"].get("operation", "")
            for event in failures
        ))
        with self.assertRaises(StateError):
            self.confirmer.consume_confirmation(binding["binding_id"])

    def test_ledger_tampering_breaks_chain_verification(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.drop_write_guards(connection, "ledger")
            connection.execute(
                "UPDATE ledger SET action = 'fabricated-event' "
                "WHERE seq = (SELECT MIN(seq) FROM ledger)"
            )
        with self.assertRaises(IntegrityError):
            self.auditor.verify_integrity()

    def test_database_guards_reject_ordinary_snapshot_and_ledger_overwrites(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE snapshots SET payload = '[]'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM ledger")
        self.assertIsInstance(self.auditor.verify_integrity(), dict)


if __name__ == "__main__":
    unittest.main()
