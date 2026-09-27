"""Behavioral tests for record semantics, dependency isolation, and time leakage."""

import copy
import unittest

from sdl_m01.data import assess_records, partition_records, quality_report
from sdl_m01.errors import ValidationError
from sdl_m01.models import canonical_json, parse_time, validate_spec


def spec_fixture():
    return {
        "task": {"objects": "registered batches", "environments": ["A", "B", "C"],
                 "time_scope": "2026", "claim_types": ["prediction"],
                 "target_quantity": "batch mean loss", "weighting": "equal_batch",
                 "eligibility": "all registered batches"},
        "schema": {"version": "1", "fields": {
            "x": {"type": "number", "role": "feature", "unit": "mol/L", "minimum": 0,
                  "missing_allowed": True},
            "y": {"type": "number", "role": "target", "unit": "s"}},
            "missing_codes": [None, "NA"]},
        "dependence": {"record_unit": "reading", "split_unit": "batch", "inference_unit": "batch",
                       "group_fields": ["batch"], "namespace": "experiment", "assumptions": ["independent batches"]},
        "split": {"strategy": "grouped", "allocation": {"E": 0.6, "V": 0.2, "C1": 0.2}, "seed": 42},
        "quality": {"version": "1", "notes": []},
        "confirmation": {"total_alpha": 0.05, "sampling_plan": "new batches", "stopping_rule": "fixed count"},
    }


def row(index=0, **updates):
    result = {"record_id": f"r{index}", "group_ids": {"batch": f"b{index}"},
              "environment": "A", "event_time": "2026-01-01T00:00:00Z",
              "available_time": "2026-01-01T00:00:00Z", "values": {"x": 2.0, "y": 3.0},
              "units": {"x": "mol/L", "y": "s"}, "source": {"file": "instrument.log", "line": index}}
    result.update(updates)
    return result


def partition_map(partitions):
    return {item["record"]["record_id"]: purpose for purpose, items in partitions.items() for item in items}


class SpecificationTests(unittest.TestCase):
    def test_validated_defaults_are_copied(self):
        raw = spec_fixture()
        normalized = validate_spec(raw)
        self.assertTrue(normalized["schema"]["fields"]["y"]["required"])
        self.assertNotIn("required", raw["schema"]["fields"]["y"])
        normalized["task"]["environments"].append("D")
        self.assertEqual(len(raw["task"]["environments"]), 3)

    def test_malformed_contracts_are_rejected(self):
        for section, name, value in [("task", "objects", ""), ("dependence", "group_fields", []),
                                     ("confirmation", "total_alpha", True), ("split", "seed", True)]:
            with self.subTest(section=section, name=name):
                spec = spec_fixture()
                spec[section][name] = value
                with self.assertRaises(ValidationError):
                    validate_spec(spec)
        spec = spec_fixture()
        spec["split"]["allocation"] = {"E": .5, "V": .3}
        with self.assertRaises(ValidationError):
            validate_spec(spec)

    def test_timestamp_requires_timezone_and_normalizes_offsets(self):
        self.assertEqual(parse_time("2026-01-01T08:00:00+08:00"), parse_time("2026-01-01T00:00:00Z"))
        with self.assertRaises(ValidationError):
            parse_time("2026-01-01T00:00:00")
        with self.assertRaises(ValidationError):
            canonical_json({"x": float("nan")})


class AssessmentTests(unittest.TestCase):
    def test_raw_provenance_and_unknown_fields_are_preserved(self):
        record = row(extra_metadata={"calibration": "unknown"})
        before = copy.deepcopy(record)
        assessed = assess_records([record], spec_fixture())[0]
        self.assertEqual(assessed["status"], "usable")
        self.assertEqual(assessed["record"], before)
        assessed["record"]["values"]["x"] = 99
        self.assertEqual(record, before)
        renamed = copy.deepcopy(record)
        renamed["record_id"] = "another-id"
        self.assertEqual(assess_records([record], spec_fixture())[0]["fingerprint"],
                         assess_records([renamed], spec_fixture())[0]["fingerprint"])
        renamed["source"]["line"] = 999
        self.assertNotEqual(assess_records([record], spec_fixture())[0]["fingerprint"],
                            assess_records([renamed], spec_fixture())[0]["fingerprint"])

    def test_missing_and_outliers_are_not_silently_deleted_or_imputed(self):
        records = [row(0, values={"x": "NA", "y": 3}), row(1, values={"x": -10, "y": 3}),
                   row(2, values={"x": 1, "y": None}), row(3, units={"x": "unknown", "y": "s"})]
        assessed = assess_records(records, spec_fixture())
        self.assertEqual([item["status"] for item in assessed], ["flagged", "flagged", "quarantined", "quarantined"])
        self.assertEqual(assessed[0]["record"]["values"]["x"], "NA")
        self.assertEqual(assessed[1]["record"]["values"]["x"], -10)
        report = quality_report(assessed, spec_fixture())
        self.assertEqual(report["missing_rates"]["x"], {"missing": 1, "denominator": 4, "rate": .25})
        self.assertEqual(report["unit_conflicts"]["by_field"]["x"], 1)

    def test_duplicates_all_quarantined_without_arbitrary_winner(self):
        first = row(0)
        second = row(1, record_id="r0")
        copy_with_new_id = copy.deepcopy(first)
        copy_with_new_id["record_id"] = "different-id"
        assessed = assess_records([first, second, copy_with_new_id], spec_fixture())
        self.assertTrue(all(item["status"] == "quarantined" for item in assessed))
        self.assertIn("duplicate_content", assessed[2]["reasons"])
        self.assertIn("duplicate_record_id", assessed[1]["reasons"])

    def test_future_features_quarantine_but_late_targets_are_recorded(self):
        on_time = row(0, prediction_time="2026-01-01T08:00:00+08:00",
                      available_time={"x": "2026-01-01T00:00:00Z", "y": "2026-01-02T00:00:00Z"})
        late = row(1, prediction_time="2026-01-01T08:00:00+08:00",
                   available_time={"x": "2026-01-01T00:00:01Z", "y": "2026-01-01T00:00:00Z"})
        assessed = assess_records([on_time, late], spec_fixture())
        self.assertEqual(assessed[0]["status"], "flagged")
        self.assertIn("target_available_after_prediction:y", assessed[0]["reasons"])
        self.assertEqual(assessed[1]["status"], "quarantined")

    def test_empty_quality_report_has_explicit_undefined_rate(self):
        report = quality_report([], spec_fixture())
        self.assertEqual(report["missing_rates"]["x"]["denominator"], 0)
        self.assertIsNone(report["missing_rates"]["x"]["rate"])

    def test_missing_values_do_not_hide_supplied_unit_conflicts(self):
        assessed = assess_records([row(values={"x": None, "y": 3}, units={"x": "unknown", "y": "s"})], spec_fixture())
        self.assertEqual(assessed[0]["status"], "quarantined")
        self.assertIn("unit_conflict:x", assessed[0]["reasons"])


class GroupPartitionTests(unittest.TestCase):
    def test_partition_is_reproducible_under_reordering_and_quality_changes(self):
        spec = spec_fixture()
        records = [row(index) for index in range(120)]
        original = partition_records(assess_records(records, spec), spec)
        self.assertEqual({key: len(value) for key, value in original.items()}, {"E": 72, "V": 24, "C1": 24, "Q": 0})
        records[4]["units"]["x"] = "bad-unit"
        changed = partition_records(assess_records(list(reversed(records)), spec), spec)
        self.assertEqual(partition_map(original), partition_map(changed))

    def test_multiple_dependency_fields_form_connected_components(self):
        spec = spec_fixture()
        spec["dependence"]["group_fields"] = ["batch", "subject"]
        records = [row(index, group_ids={"batch": f"b{index}", "subject": f"s{index}"}) for index in range(8)]
        records[1]["group_ids"]["batch"] = "b0"
        records[2]["group_ids"]["subject"] = "s1"
        partitions = partition_records(assess_records(records, spec), spec)
        mapping = partition_map(partitions)
        self.assertEqual(mapping["r0"], mapping["r1"])
        self.assertEqual(mapping["r1"], mapping["r2"])
        purpose_by_key = {}
        for purpose, items in partitions.items():
            for item in items:
                for key in item["unit_keys"]:
                    self.assertEqual(purpose_by_key.setdefault(key, purpose), purpose)

    def test_unknown_identity_is_unassigned_without_moving_known_groups(self):
        records = [row(index) for index in range(10)]
        base = partition_map(partition_records(assess_records(records, spec_fixture()), spec_fixture()))
        records.append(row(99, group_ids={}))
        output = partition_records(assess_records(records, spec_fixture()), spec_fixture())
        self.assertEqual(output["Q"][0]["status"], "quarantined")
        self.assertEqual({key: value for key, value in partition_map(output).items() if key != "r99"}, base)

    def test_nonzero_partitions_receive_one_group_or_reject(self):
        spec = spec_fixture()
        spec["split"]["allocation"] = {"E": .98, "V": .01, "C1": .01}
        output = partition_records(assess_records([row(index) for index in range(3)], spec), spec)
        self.assertEqual([len(output[name]) for name in ("E", "V", "C1")], [1, 1, 1])
        with self.assertRaises(ValidationError):
            partition_records(assess_records([row(0), row(1)], spec), spec)

    def test_environment_holdout_and_conflicting_identity_rejected(self):
        spec = spec_fixture()
        spec["split"] = {"strategy": "environment", "environment_assignments": {"A": "E", "B": "V", "C": "C1"}}
        records = [row(index, environment=environment) for index, environment in enumerate(["A", "B", "C"])]
        self.assertEqual(partition_map(partition_records(assess_records(records, spec), spec)), {"r0": "E", "r1": "V", "r2": "C1"})
        records[1]["group_ids"] = {"batch": "b0"}
        with self.assertRaises(ValidationError):
            partition_records(assess_records(records, spec), spec)


class TemporalPartitionTests(unittest.TestCase):
    def temporal_spec(self):
        spec = spec_fixture()
        spec["split"] = {"strategy": "temporal", "cutoffs": {"E": "2026-01-02T00:00:00Z", "V": "2026-01-03T00:00:00Z", "C1": "2026-01-04T00:00:00Z"}, "gap_seconds": 60}
        return spec

    def test_time_cutoffs_gap_and_late_training_labels(self):
        spec = self.temporal_spec()
        records = [row(0, available_time={"x": "2026-01-01T00:00:00Z", "y": "2026-01-02T12:00:00Z"}),
                   row(1, event_time="2026-01-01T23:59:30Z", available_time="2026-01-01T23:59:30Z"),
                   row(2, event_time="2026-01-02T08:00:00+08:00", available_time="2026-01-02T00:00:00Z"),
                   row(3, event_time="2026-01-04T00:00:00Z", available_time="2026-01-04T00:00:00Z")]
        original_assessed = assess_records(records, spec)
        output = partition_records(original_assessed, spec)
        self.assertEqual(partition_map(output), {"r0": "E", "r2": "V", "r1": "Q", "r3": "Q"})
        self.assertEqual(output["E"][0]["status"], "quarantined")
        self.assertIn("availability_after_partition_cutoff:y", output["E"][0]["reasons"])
        self.assertEqual(original_assessed[0]["status"], "usable")
        self.assertIn("temporal_gap", output["Q"][0]["reasons"])

    def test_same_subject_may_cross_time_but_invalid_time_never_trains(self):
        spec = self.temporal_spec()
        records = [row(0), row(1, group_ids={"batch": "b0"}, event_time="2026-01-03T12:00:00Z", available_time="2026-01-03T12:00:00Z"),
                   row(2, event_time="not-a-time")]
        output = partition_records(assess_records(records, spec), spec)
        self.assertEqual(partition_map(output), {"r0": "E", "r1": "C1", "r2": "Q"})
        self.assertEqual(output["Q"][0]["status"], "quarantined")


if __name__ == "__main__":
    unittest.main()
