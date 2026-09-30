"""Versioned JSON boundary for routing/standardization modules; no code evaluation."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from .model import Access, AffineAccess, Instruction, Loop, Program, Task, Tensor


def program_from_dict(value: dict) -> Program:
    allowed = {"version", "source_kind", "inputs", "tasks", "outputs", "instructions", "loops"}
    if set(value) - allowed:
        raise ValueError(f"unknown request fields: {sorted(set(value) - allowed)}")
    if value.get("version") != 1:
        raise ValueError("request version must be 1")
    if value.get("source_kind", "standard") not in {"raw", "standard", "analysis"}:
        raise ValueError("source_kind must be raw, standard or analysis")
    inputs = {}
    for key, payload in value.get("inputs", {}).items():
        if isinstance(payload, list):
            inputs[key] = Tensor.from_values(payload)
        else:
            if set(payload) - {"values", "shape", "dtype"}:
                raise ValueError(f"unknown tensor fields for {key}")
            inputs[key] = Tensor.from_values(payload["values"], payload.get("shape"), payload.get("dtype", "float64"))
    tasks = []
    for payload in value.get("tasks", []):
        payload = dict(payload)
        for field in ("inputs", "depends_on", "devices"):
            if field in payload:
                payload[field] = tuple(payload[field])
        payload["accesses"] = tuple(Access(**a) for a in payload.get("accesses", []))
        tasks.append(Task(**payload))
    instructions = []
    for payload in value.get("instructions", []):
        payload = dict(payload)
        payload["accesses"] = tuple(Access(**a) for a in payload.get("accesses", []))
        payload["depends_on"] = tuple(payload.get("depends_on", ()))
        instructions.append(Instruction(**payload))
    loops = []
    for payload in value.get("loops", []):
        payload = dict(payload)
        payload["accesses"] = tuple(AffineAccess(**a) for a in payload.get("accesses", []))
        loops.append(Loop(**payload))
    return Program(inputs, tuple(tasks), tuple(value["outputs"]), tuple(instructions), tuple(loops))


def load_program(path) -> Program:
    return program_from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def report_dict(plan, devices=()) -> dict:
    from .planner import eligibility, placement_cost_ms, reservation_bytes
    placements = {}
    for task in plan.program.tasks:
        placements[task.id] = [{"device": d.id, "eligible": eligibility(task, d, plan.specs)[0],
                                "reason": eligibility(task, d, plan.specs)[1],
                                "estimated_ms": placement_cost_ms(task, d, plan.specs),
                                "reservation_bytes": reservation_bytes(task, plan.specs),
                                "fits_budget": reservation_bytes(task, plan.specs) <= d.memory_bytes}
                               for d in devices]
    return {"version": 1, "tlp": asdict(plan.graph), "ilp": plan.ilp, "simd": plan.simd,
            "priorities": plan.priorities, "host_buffer_bytes": plan.host_buffer_bytes,
            "placements": placements,
            "correctness_scope": "complete declared IR and pure registered kernels; unknown effects are serialized",
            "cost_scope": "estimates, not measured speedup or a hardware optimality guarantee"}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2,
                      default=lambda x: asdict(x) if is_dataclass(x) else str(x))
