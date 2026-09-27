"""Small, deterministic synthetic inputs; no statistical discovery is asserted."""

from copy import deepcopy


def sample_spec():
    return {
        "task": {
            "objects": "new experimental batches",
            "environments": ["lab-A"],
            "time_scope": "2026 synthetic experiment",
            "claim_types": ["prediction"],
            "target_quantity": "batch mean loss gain",
            "weighting": "equal_batch",
            "eligibility": "all registered batches",
        },
        "schema": {
            "version": "1",
            "fields": {
                "X1": {
                    "type": "number", "role": "feature", "unit": "mol/L",
                    "required": True, "missing_allowed": True,
                    "minimum": 0, "maximum": None, "range_action": "flag",
                },
                "Y": {
                    "type": "number", "role": "target", "unit": "mol/(L*s)",
                    "required": True,
                },
            },
            "missing_codes": [None, "NA"],
            "source_required": True,
        },
        "dependence": {
            "record_unit": "reading", "split_unit": "batch",
            "inference_unit": "batch", "group_fields": ["batch"],
            "namespace": "acceptance-experiment",
            "assumptions": ["independent batches"],
        },
        "split": {
            "strategy": "grouped",
            "allocation": {"E": 0.6, "V": 0.2, "C1": 0.2},
            "seed": 42,
        },
        "quality": {"version": "1", "notes": []},
        "confirmation": {
            "total_alpha": 0.05,
            "sampling_plan": "new independent batches",
            "stopping_rule": "fixed batch count",
        },
        "identification_gaps": [],
    }


def sample_records(groups=10, readings=2, start=0):
    records = []
    for group in range(start, start + groups):
        for reading in range(readings):
            records.append({
                "record_id": f"r-{group:03}-{reading:02}",
                "group_ids": {"batch": f"batch-{group:03}"},
                "environment": "lab-A",
                "event_time": "2026-01-01T10:00:00+00:00",
                "available_time": "2026-01-01T11:00:00+00:00",
                "values": {"X1": group + 0.1 * reading, "Y": group * 2.0 + reading},
                "units": {"X1": "mol/L", "Y": "mol/(L*s)"},
                "source": "synthetic://module-01-acceptance",
                "operator_annotation": f"private-observation-{group:03}-{reading:02}",
            })
    return records


def frozen_plan(protocol, spec=None, round_index=1):
    spec = spec or sample_spec()
    return {
        "protocol_id": protocol["protocol_id"],
        "round_index": round_index,
        "hypotheses": [{
            "id": "h1", "version": "1", "statement": "candidate improves prediction",
            "prediction": "positive batch mean loss gain", "scope": "new lab-A batches",
            "representation": {"features": ["X1"]}, "model": "fixed linear candidate",
            "parameters": {"slope": 2.0, "intercept": 0.0},
        }],
        "preprocessing": {"steps": [], "fit_dataset_refs": [protocol["resources"]["E"]]},
        "primary_metric": "batch mean squared loss improvement",
        "test_family": [{
            "id": "t1", "hypothesis_id": "h1", "null": "mean gain <= 0.1",
            "alternative": "mean gain > 0.1", "method": "external prespecified batch test",
        }],
        "effect_threshold": 0.1,
        "sampling_plan": spec["confirmation"]["sampling_plan"],
        "stopping_rule": spec["confirmation"]["stopping_rule"],
        "inference_unit": spec["dependence"]["inference_unit"],
        "eligibility": spec["task"]["eligibility"],
        "quality_rules_version": spec["quality"]["version"],
        "assumptions": [{
            "name": "independent batches", "justification": "synthetic fixture declaration only",
        }],
    }


def evaluation(status="inconclusive"):
    return {
        "status": status,
        "metrics": {},
        "notes": "Synthetic lifecycle fixture; no statistical evaluation was performed.",
        "code_version": "acceptance-fixture-v1",
    }


def renamed(records, prefix="renamed-"):
    result = deepcopy(records)
    for record in result:
        record["record_id"] = prefix + record["record_id"]
    return result
