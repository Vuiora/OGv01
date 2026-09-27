"""Deterministic quality assessment and evidence partitioning; no learned transforms."""

from __future__ import annotations

import copy
import hashlib
import math
from collections import Counter
from datetime import timedelta
from typing import Any

from .errors import ValidationError
from .models import canonical_json, parse_time, purpose_names, validate_spec

_SEVERITY = {"usable": 0, "flagged": 1, "excluded": 2, "quarantined": 3}


def _mark(item: dict, status: str, reason: str) -> None:
    if _SEVERITY[status] > _SEVERITY[item["status"]]:
        item["status"] = status
    if reason not in item["reasons"]:
        item["reasons"].append(reason)


def _missing(record: dict, name: str, spec: dict) -> bool:
    values = record.get("values")
    if not isinstance(values, dict) or name not in values:
        return True
    value = values[name]
    # Typed comparison prevents False/0 and True/1 from becoming missing aliases.
    return any(type(value) is type(code) and value == code for code in spec["schema"]["missing_codes"])


def _availability(record: dict, name: str):
    value = record.get("available_time")
    return parse_time(value.get(name) if isinstance(value, dict) else value, f"available_time.{name}")


def _identity(value: Any) -> bool:
    return ((isinstance(value, str) and bool(value.strip())) or
            (isinstance(value, int) and not isinstance(value, bool)))


def assess_records(records: list[dict], spec: dict) -> list[dict]:
    """Keep raw observations intact and attach deterministic, auditable quality decisions."""
    spec = validate_spec(spec)
    if not isinstance(records, list):
        raise ValidationError("records must be a list")
    canonical_json(records)
    assessed = []
    for raw in records:
        if not isinstance(raw, dict):
            raise ValidationError("Every record must be an object")
        record = copy.deepcopy(raw)
        fingerprint = hashlib.sha256(canonical_json({k: v for k, v in record.items() if k != "record_id"}).encode("utf-8")).hexdigest()
        item = {"record": record, "status": "usable", "reasons": [], "fingerprint": fingerprint, "unit_keys": []}
        if not isinstance(record.get("record_id"), str) or not record["record_id"].strip():
            _mark(item, "quarantined", "invalid_record_id")
        groups = record.get("group_ids")
        group_fields = spec["dependence"]["group_fields"]
        if not isinstance(groups, dict) or any(not _identity(groups.get(name)) for name in group_fields):
            _mark(item, "quarantined", "unresolved_group_identity")
        else:
            item["unit_keys"] = sorted(canonical_json([spec["dependence"]["namespace"], name, groups[name]]) for name in group_fields)
        environment = record.get("environment")
        if not isinstance(environment, str) or not environment.strip():
            _mark(item, "quarantined", "invalid_environment")
        elif environment not in spec["task"]["environments"]:
            _mark(item, "excluded", "environment_outside_scope")
        try:
            event_time = parse_time(record.get("event_time"), "event_time")
        except ValidationError:
            event_time = None
            _mark(item, "quarantined", "invalid_event_time")
        prediction_time = None
        if "prediction_time" in record:
            try:
                prediction_time = parse_time(record["prediction_time"], "prediction_time")
            except ValidationError:
                _mark(item, "quarantined", "invalid_prediction_time")
        source = record.get("source")
        if spec["schema"]["source_required"] and not ((isinstance(source, str) and source.strip()) or (isinstance(source, dict) and source)):
            _mark(item, "quarantined", "missing_source")
        values = record.get("values")
        if not isinstance(values, dict):
            values = {}
            _mark(item, "quarantined", "invalid_values")
        units = record.get("units")
        if not isinstance(units, dict):
            units = {}
            _mark(item, "quarantined", "invalid_units")
        mask = record.get("missing_mask", {})
        if not isinstance(mask, dict) or any(not isinstance(reason, str) or not reason.strip() for reason in mask.values()):
            mask = {}
            _mark(item, "quarantined", "invalid_missing_mask")
        availability = record.get("available_time")
        if not isinstance(availability, (str, dict)):
            _mark(item, "quarantined", "invalid_available_time")
        elif isinstance(availability, str):
            try:
                parse_time(availability)
            except ValidationError:
                _mark(item, "quarantined", "invalid_available_time")
        for name, field in spec["schema"]["fields"].items():
            missing = _missing(record, name, spec)
            # A missing measurement may omit its unit, but a supplied unknown
            # unit is still an unresolved semantic conflict.
            if (name in units or not missing) and units.get(name) != field["unit"]:
                _mark(item, "quarantined", f"unit_conflict:{name}")
            if missing:
                allowed = field["missing_allowed"] or (name not in values and not field["required"])
                _mark(item, "flagged" if allowed else "quarantined", f"missing:{name}")
                continue
            if name in mask:
                _mark(item, "quarantined", f"missing_mask_conflict:{name}")
            value = values[name]
            expected = field["type"]
            valid = {"number": isinstance(value, (int, float)) and not isinstance(value, bool),
                     "integer": isinstance(value, int) and not isinstance(value, bool),
                     "string": isinstance(value, str), "boolean": isinstance(value, bool),
                     "object": isinstance(value, dict), "array": isinstance(value, list)}[expected]
            if not valid:
                _mark(item, "quarantined", f"invalid_type:{name}")
            if valid and expected in ("number", "integer"):
                outside = ((field.get("minimum") is not None and value < field["minimum"]) or
                           (field.get("maximum") is not None and value > field["maximum"]))
                if outside:
                    _mark(item, {"flag": "flagged", "quarantine": "quarantined", "exclude": "excluded"}[field["range_action"]], f"out_of_range:{name}")
            if "categories" in field and not any(type(value) is type(category) and value == category for category in field["categories"]):
                _mark(item, "quarantined", f"unknown_category:{name}")
            try:
                available = _availability(record, name)
            except ValidationError:
                _mark(item, "quarantined", f"invalid_available_time:{name}")
                continue
            if prediction_time is not None and available > prediction_time:
                if field["role"] == "feature":
                    _mark(item, "quarantined", f"feature_unavailable_at_prediction:{name}")
                elif field["role"] == "target":
                    _mark(item, "flagged", f"target_available_after_prediction:{name}")
        assessed.append(item)
    ids = Counter(item["record"].get("record_id") for item in assessed if isinstance(item["record"].get("record_id"), str))
    fingerprints = Counter(item["fingerprint"] for item in assessed)
    for item in assessed:
        record_id = item["record"].get("record_id")
        if isinstance(record_id, str) and ids[record_id] > 1:
            _mark(item, "quarantined", "duplicate_record_id")
        if fingerprints[item["fingerprint"]] > 1:
            _mark(item, "quarantined", "duplicate_content")
        item["reasons"].sort()
    return assessed


def _components(items: list[dict], *, environment: bool = False) -> list[list[int]]:
    parents = list(range(len(items)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    owners = {}
    for index, item in enumerate(items):
        keys = [("unit", key) for key in item["unit_keys"]]
        if environment:
            keys.append(("environment", item["record"]["environment"]))
        for key in keys:
            if key in owners:
                parents[find(index)] = find(owners[key])
            else:
                owners[key] = index
    groups = {}
    for index in range(len(items)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _counts(size: int, names: list[str], allocation: dict) -> dict:
    active = [name for name in names if allocation[name] > 0]
    if size < len(active):
        raise ValidationError("Not enough independent partition groups for every nonzero allocation")
    ideal = {name: size * allocation[name] for name in names}
    counts = {name: math.floor(ideal[name]) for name in names}
    remaining = size - sum(counts.values())
    for name in sorted(active, key=lambda name: (-(ideal[name] - counts[name]), names.index(name)))[:remaining]:
        counts[name] += 1
    for name in active:
        if counts[name] == 0:
            donor = max((candidate for candidate in active if counts[candidate] > 1), key=lambda candidate: (counts[candidate] - ideal[candidate], counts[candidate], -names.index(candidate)))
            counts[donor] -= 1
            counts[name] = 1
    return counts


def _row_order(item: dict):
    return (str(item["record"].get("record_id", "")), item["fingerprint"])


def partition_records(assessed: list[dict], spec: dict) -> dict[str, list[dict]]:
    """Assign identities before quality filtering; quarantine never reshuffles a group."""
    spec = validate_spec(spec)
    names = purpose_names(spec)
    result = {name: [] for name in names}
    result["Q"] = []
    items = copy.deepcopy(assessed)
    split = spec["split"]
    if split["strategy"] == "temporal":
        bounds = [parse_time(split["cutoffs"][name]) for name in names]
        gap = timedelta(seconds=split["gap_seconds"])
        for item in items:
            record = item["record"]
            try:
                event = parse_time(record.get("event_time"))
            except ValidationError:
                result["Q"].append(item)
                continue
            if not item["unit_keys"]:
                result["Q"].append(item)
                continue
            position = next((index for index, bound in enumerate(bounds) if event < bound), None)
            if position is None:
                _mark(item, "excluded", "outside_temporal_window")
                result["Q"].append(item)
                continue
            information_cutoff = bounds[position] - (gap if position < len(bounds) - 1 else timedelta(0))
            if event >= information_cutoff:
                _mark(item, "excluded", "temporal_gap")
                result["Q"].append(item)
                continue
            for name in spec["schema"]["fields"]:
                if _missing(record, name, spec):
                    continue
                try:
                    available = _availability(record, name)
                except ValidationError:
                    _mark(item, "quarantined", f"invalid_available_time:{name}")
                    continue
                if available >= information_cutoff:
                    _mark(item, "quarantined", f"availability_after_partition_cutoff:{name}")
            result[names[position]].append(item)
    else:
        eligible = []
        is_environment = split["strategy"] == "environment"
        for item in items:
            if not item["unit_keys"] or (is_environment and item["record"].get("environment") not in spec["task"]["environments"]):
                result["Q"].append(item)
            else:
                eligible.append(item)
        components = _components(eligible, environment=is_environment)
        if "environment_assignments" in split:
            for component in components:
                assignments = {split["environment_assignments"][eligible[index]["record"]["environment"]] for index in component}
                if len(assignments) != 1:
                    raise ValidationError("A connected identity spans conflicting environment assignments")
                result[next(iter(assignments))].extend(eligible[index] for index in component)
        else:
            def ordering(component):
                keys = sorted({key for index in component for key in eligible[index]["unit_keys"]})
                if is_environment:
                    keys += sorted({"environment:" + eligible[index]["record"]["environment"] for index in component})
                return hashlib.sha256(canonical_json([split["seed"], keys]).encode("utf-8")).hexdigest()
            components.sort(key=ordering)
            counts = _counts(len(components), names, split["allocation"])
            offset = 0
            for name in names:
                for component in components[offset:offset + counts[name]]:
                    result[name].extend(eligible[index] for index in component)
                offset += counts[name]
    for rows in result.values():
        for item in rows:
            item["reasons"].sort()
        rows.sort(key=_row_order)
    return result


def quality_report(assessed: list[dict], spec: dict) -> dict:
    """Descriptive quality counts with denominators, never an effective sample size."""
    spec = validate_spec(spec)
    total = len(assessed)
    statuses = Counter(item["status"] for item in assessed)
    missing = {}
    conflicts = {}
    for name in spec["schema"]["fields"]:
        count = sum(_missing(item["record"], name, spec) for item in assessed)
        missing[name] = {"missing": count, "denominator": total, "rate": count / total if total else None}
        conflicts[name] = sum(f"unit_conflict:{name}" in item["reasons"] for item in assessed)
    identities = {key for item in assessed for key in item["unit_keys"]}
    resolved = [item for item in assessed if item["unit_keys"]]
    environments = Counter(item["record"].get("environment") for item in assessed if isinstance(item["record"].get("environment"), str))
    covered = sorted(set(environments).intersection(spec["task"]["environments"]))
    return {
        "records": total,
        "status_counts": {status: statuses[status] for status in _SEVERITY},
        "missing_rates": missing,
        "unit_conflicts": {"by_field": conflicts, "total_field_conflicts": sum(conflicts.values())},
        "duplicate_record_id_rows": sum("duplicate_record_id" in item["reasons"] for item in assessed),
        "duplicate_content_rows": sum("duplicate_content" in item["reasons"] for item in assessed),
        "group_counts": {"unique_declared_identities": len(identities), "connected_identity_groups": len(_components(resolved)), "unresolved_rows": total - len(resolved)},
        "environment_coverage": {"covered": covered, "declared": spec["task"]["environments"], "denominator": len(spec["task"]["environments"]), "rate": len(covered) / len(spec["task"]["environments"]), "row_counts": dict(sorted(environments.items()))},
        "rules_version": spec["quality"]["version"],
        "notes": list(spec["quality"]["notes"]) + ["Group counts are descriptive identities, not an effective independent sample size.", "Missingness mechanisms and measurement accuracy are not established by these counts."],
    }
