"""Validation and canonical representations for the Module 01 JSON contract."""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError


def canonical_json(value: Any) -> str:
    """Canonical JSON; reject non-JSON objects and non-finite numbers."""
    def check(item: Any) -> None:
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValidationError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is None or isinstance(item, (str, bool, int)):
            pass
        elif isinstance(item, float) and math.isfinite(item):
            pass
        else:
            raise ValidationError("Inputs must contain only finite JSON values")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_time(value: Any, label: str = "timestamp") -> datetime:
    """Parse an explicitly timezone-aware ISO 8601 timestamp into UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a timezone-aware ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a nonempty string")
    return value


def _texts(value: Any, label: str, *, empty: bool = False) -> list:
    if not isinstance(value, list) or (not value and not empty):
        raise ValidationError(f"{label} must be a {'possibly empty' if empty else 'nonempty'} list")
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        raise ValidationError(f"{label} contains duplicates")
    return value


def _number(value: Any, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            (isinstance(value, float) and not math.isfinite(value))):
        raise ValidationError(f"{label} must be finite numeric")
    return value


def _purpose(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"E|V|C[1-9][0-9]*", value):
        raise ValidationError("Purposes must be E, V, or C followed by a positive integer")


def purpose_names(spec: dict) -> list[str]:
    split = spec["split"]
    names = (split["cutoffs"] if split["strategy"] == "temporal" else
             set(split["environment_assignments"].values()) if "environment_assignments" in split else
             split["allocation"])
    return sorted(names, key=lambda name: (0, 0) if name == "E" else
                  (1, 0) if name == "V" else (2, int(name[1:])))


def validate_spec(spec: dict) -> dict:
    """Validate a complete protocol, preserving unknown metadata and input objects."""
    _mapping(spec, "spec")
    canonical_json(spec)
    result = copy.deepcopy(spec)
    task = _mapping(result.get("task"), "task")
    for name in ("objects", "time_scope", "target_quantity", "weighting", "eligibility"):
        _text(task.get(name), f"task.{name}")
    for name in ("environments", "claim_types"):
        _texts(task.get(name), f"task.{name}")
    schema = _mapping(result.get("schema"), "schema")
    _text(schema.get("version"), "schema.version")
    fields = _mapping(schema.get("fields"), "schema.fields")
    if not fields:
        raise ValidationError("schema.fields must not be empty")
    schema.setdefault("missing_codes", [None])
    if not isinstance(schema["missing_codes"], list):
        raise ValidationError("schema.missing_codes must be a list")
    if any(isinstance(value, (list, dict)) for value in schema["missing_codes"]):
        raise ValidationError("schema.missing_codes must contain scalar JSON values")
    schema.setdefault("source_required", True)
    if not isinstance(schema["source_required"], bool):
        raise ValidationError("schema.source_required must be boolean")
    for name, field in fields.items():
        _text(name, "field name")
        _mapping(field, f"schema.fields.{name}")
        if field.get("type") not in ("number", "integer", "string", "boolean", "object", "array"):
            raise ValidationError(f"Unsupported type for {name}")
        if field.get("role") not in ("feature", "target", "context", "metadata", "identifier"):
            raise ValidationError(f"Unsupported role for {name}")
        _text(field.get("unit"), f"schema.fields.{name}.unit")
        field.setdefault("required", True)
        field.setdefault("missing_allowed", False)
        for flag in ("required", "missing_allowed"):
            if not isinstance(field[flag], bool):
                raise ValidationError(f"{name}.{flag} must be boolean")
        field.setdefault("range_action", "flag")
        if field["range_action"] not in ("flag", "quarantine", "exclude"):
            raise ValidationError(f"Invalid range_action for {name}")
        for bound in ("minimum", "maximum"):
            if field.get(bound) is not None:
                _number(field[bound], f"{name}.{bound}")
                if field["type"] not in ("number", "integer"):
                    raise ValidationError(f"Numeric range specified for nonnumeric field {name}")
        if field.get("minimum") is not None and field.get("maximum") is not None:
            if field["minimum"] > field["maximum"]:
                raise ValidationError(f"Reversed range for {name}")
        if "categories" in field:
            if not isinstance(field["categories"], list) or not field["categories"]:
                raise ValidationError(f"{name}.categories must be a nonempty list")
    dependence = _mapping(result.get("dependence"), "dependence")
    for name in ("record_unit", "split_unit", "inference_unit", "namespace"):
        _text(dependence.get(name), f"dependence.{name}")
    _texts(dependence.get("group_fields"), "dependence.group_fields")
    dependence.setdefault("assumptions", [])
    _texts(dependence["assumptions"], "dependence.assumptions", empty=True)
    quality = _mapping(result.get("quality"), "quality")
    _text(quality.get("version"), "quality.version")
    quality.setdefault("notes", [])
    _texts(quality["notes"], "quality.notes", empty=True)
    confirmation = _mapping(result.get("confirmation"), "confirmation")
    alpha = _number(confirmation.get("total_alpha"), "confirmation.total_alpha")
    if not 0 < alpha < 1:
        raise ValidationError("confirmation.total_alpha must lie strictly between 0 and 1")
    for name in ("sampling_plan", "stopping_rule"):
        _text(confirmation.get(name), f"confirmation.{name}")
    result.setdefault("identification_gaps", [])
    if not isinstance(result["identification_gaps"], list):
        raise ValidationError("identification_gaps must be a list")
    split = _mapping(result.get("split"), "split")
    strategy = split.get("strategy")
    if strategy not in ("grouped", "environment", "temporal"):
        raise ValidationError("split.strategy must be grouped, environment, or temporal")
    split.setdefault("seed", 0)
    if isinstance(split["seed"], bool) or not isinstance(split["seed"], int):
        raise ValidationError("split.seed must be an integer")
    if strategy == "temporal":
        if "allocation" in split or "environment_assignments" in split:
            raise ValidationError("Temporal split requires cutoffs, not allocation or assignments")
        cutoffs = _mapping(split.get("cutoffs"), "split.cutoffs")
        if not cutoffs:
            raise ValidationError("split.cutoffs must not be empty")
        for name in cutoffs:
            _purpose(name)
        bounds = [parse_time(cutoffs[name], f"cutoff {name}") for name in purpose_names(result)]
        if any(left >= right for left, right in zip(bounds, bounds[1:])):
            raise ValidationError("Temporal cutoffs must increase in E, V, C1... order")
        split.setdefault("gap_seconds", 0)
        gap = _number(split["gap_seconds"], "split.gap_seconds")
        if gap < 0:
            raise ValidationError("split.gap_seconds must be nonnegative")
        if any((right - left).total_seconds() <= gap for left, right in zip(bounds, bounds[1:])):
            raise ValidationError("Temporal gap eliminates a partition")
    elif "environment_assignments" in split:
        if strategy != "environment" or "allocation" in split or "cutoffs" in split:
            raise ValidationError("Environment assignments require an environment split without allocation")
        assignments = _mapping(split["environment_assignments"], "split.environment_assignments")
        if set(assignments) != set(task["environments"]):
            raise ValidationError("Environment assignments must cover exactly the declared environments")
        for value in assignments.values():
            _purpose(value)
    else:
        if "cutoffs" in split:
            raise ValidationError("cutoffs require the temporal strategy")
        allocation = _mapping(split.get("allocation"), "split.allocation")
        if not allocation:
            raise ValidationError("split.allocation must not be empty")
        for name, weight in allocation.items():
            _purpose(name)
            if not 0 <= _number(weight, f"allocation.{name}") <= 1:
                raise ValidationError("Allocation weights must be between 0 and 1")
        if not math.isclose(sum(allocation.values()), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValidationError("Allocation weights must sum to one")
    return result
