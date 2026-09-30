"""CSV -> provenance-bearing M1 observations, then grouped evidence partitioning."""
import csv
import hashlib
import math
from pathlib import Path
from .models import TaskSpec

def load_observations(spec: TaskSpec, path: Path):
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Observation CSV exceeds 20 MiB")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    records, ids = [], set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = set(spec.features + [spec.target, spec.group_column])
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Observation CSV is missing declared columns")
        for index, row in enumerate(reader, 2):
            if len(records) >= 20000:
                raise ValueError("Observation row budget exceeds 20000")
            if not row[spec.group_column].strip():
                raise ValueError(f"Unresolved observation group at CSV row {index}")
            values = {name: float(row[name]) for name in spec.features + [spec.target]}
            if any(not math.isfinite(v) for v in values.values()):
                raise ValueError(f"Nonfinite observation at CSV row {index}")
            rid = row.get("record_id") or f"row-{index}"
            if rid in ids:
                raise ValueError("Duplicate observation record_id")
            ids.add(rid)
            timestamp = row.get("event_time") or "2026-01-01T00:00:00+00:00"
            environment = row.get("environment") or spec.environments[0]
            records.append({"record_id": rid, "group_ids": {spec.group_column: row[spec.group_column]},
                "values": values, "units": dict(spec.units), "event_time": timestamp,
                "available_time": timestamp, "environment": environment,
                "source": {"source_id": spec.dataset_id, "sha256": source_hash,
                           "path": path.name, "row": index}})
    if len({r["group_ids"][spec.group_column] for r in records}) < 30:
        raise ValueError("At least 30 distinct observation groups are required")
    return records, source_hash

def protocol_spec(spec: TaskSpec):
    return {
        "task": {"objects": spec.dataset_id, "environments": spec.environments,
            "time_scope": "declared observation file", "claim_types": ["prediction"],
            "target_quantity": "median group MSE improvement over frozen E constant baseline",
            "weighting": "equal_group", "eligibility": "declared finite complete records"},
        "schema": {"version": "ogflow-1", "fields": {
            name: {"type": "number", "role": "target" if name == spec.target else "feature",
                "unit": spec.units[name], "required": True, "missing_allowed": False,
                "minimum": None, "maximum": None, "range_action": "flag"}
            for name in spec.features + [spec.target]}, "missing_codes": [None], "source_required": True},
        "dependence": {"record_unit": "reading", "split_unit": spec.group_column,
            "inference_unit": spec.group_column, "group_fields": [spec.group_column],
            "namespace": spec.dataset_id, "assumptions": [spec.confirmation.independence_assumption]},
        "split": {"strategy": "grouped", "allocation": {"E": 0.6, "V": 0.2, "C1": 0.2}, "seed": spec.split_seed},
        "quality": {"version": "1", "notes": []},
        "confirmation": {"total_alpha": spec.confirmation.alpha,
            "sampling_plan": "fixed preregistered grouped CSV split",
            "stopping_rule": "one frozen family; no interim peeking; no reused confirmation groups"},
        "identification_gaps": ["Group independence is a task-owner declaration, not established by software.",
            "Predictive improvement does not establish causality."]}

