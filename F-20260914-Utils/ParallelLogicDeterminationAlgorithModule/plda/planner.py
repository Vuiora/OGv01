"""Turn immutable dataflow and explicit effects into a schedulable plan."""
from __future__ import annotations

from dataclasses import dataclass, replace
from .analysis import DependencyReport, analyze_ilp, analyze_simd, dependencies
from .kernels import ELEMENTWISE, infer
from .model import Access, AffineAccess, Device, Loop, MachineModel, Program, TensorSpec


@dataclass
class Plan:
    program: Program
    graph: DependencyReport
    specs: dict[str, TensorSpec]
    priorities: dict[str, float]
    ilp: dict
    simd: list[dict]
    host_buffer_bytes: int


def prepare(program: Program, machine: MachineModel | None = None) -> Plan:
    if any(not isinstance(k, str) or not k for k in program.inputs):
        raise ValueError("input ids must be nonempty strings")
    specs = {k: v.spec for k, v in program.inputs.items()}
    tasks, prior, nodes = list(program.tasks), set(), []
    for task in tasks:
        if task.id in specs:
            raise ValueError(f"duplicate input/output id {task.id}")
        missing = set(task.inputs) - specs.keys()
        if missing:
            raise ValueError(f"{task.id}: inputs must precede use: {sorted(missing)}")
        specs[task.id] = infer(task, [specs[k] for k in task.inputs])
        data_dependencies = tuple(k for k in dict.fromkeys(task.inputs) if k in prior)
        accesses = tuple(Access(k, "read") for k in task.inputs) + (Access(task.id, "write"),)
        nodes.append(replace(task, depends_on=tuple(dict.fromkeys(task.depends_on + data_dependencies)),
                             accesses=accesses + task.accesses))
        prior.add(task.id)
    if not program.outputs or len(set(program.outputs)) != len(program.outputs):
        raise ValueError("program requires distinct output ids")
    if set(program.outputs) - specs.keys():
        raise ValueError("unknown program output")
    graph = dependencies(nodes, {t.id: t.estimated_ms.get("CPU", 1.0) for t in tasks})
    successors = {k: set() for k in graph.order}
    for e in graph.edges:
        successors[e.source].add(e.target)
    costs = {t.id: min(t.estimated_ms.values(), default=1.0) for t in tasks}
    priorities = {}
    for k in reversed(graph.order):
        priorities[k] = costs[k] + max((priorities[s] for s in successors[k]), default=0)
    simd = [analyze_simd(loop) for loop in program.loops]
    for task in tasks:
        spec = specs[task.id]
        if task.op in ELEMENTWISE:
            accesses = tuple(AffineAccess(k, "read", spec.itemsize, width=spec.itemsize)
                             for k in dict.fromkeys(task.inputs))
            accesses += (AffineAccess(task.id, "write", spec.itemsize, width=spec.itemsize),)
            loop = Loop(f"task:{task.id}", spec.size, accesses, dtype=spec.dtype)
            report = analyze_simd(loop)
            report["execution_note"] = "analysis only; runtime uses scalar CPU or a native array backend"
            simd.append(report)
    return Plan(program, graph, specs, priorities, analyze_ilp(program.instructions, machine), simd,
                sum(s.nbytes for s in specs.values()))


def eligibility(task, device: Device, specs: dict[str, TensorSpec]) -> tuple[bool, str]:
    if device.kind not in task.devices:
        return False, "device kind excluded by task"
    if device.kind != "CPU" and task.numerical_mode == "strict":
        return False, "strict arithmetic uses the CPU reference"
    if device.backend == "openvino":
        if task.op not in {"add", "multiply", "scale", "relu", "identity", "matmul", "sum"}:
            return False, "operator not in OpenVINO adapter capability set"
        if any(specs[k].dtype != "float32" or specs[k].size == 0 for k in (*task.inputs, task.id)):
            return False, "OpenVINO adapter requires nonempty float32 tensors"
    return True, "kernel/device contract compatible; driver execution still validates support"


def reservation_bytes(task, specs) -> int:
    # Logical tensor staging plus an explicit workspace allowance, not an OS RSS limit.
    return 2 * (sum(specs[k].nbytes for k in set(task.inputs)) + specs[task.id].nbytes) + task.workspace_bytes


def placement_cost_ms(task, device: Device, specs) -> float:
    transfer = sum(specs[k].nbytes for k in set(task.inputs)) + specs[task.id].nbytes
    compute = task.estimated_ms.get(device.kind, task.estimated_ms.get("CPU", 1.0))
    return device.launch_ms + compute + 1000 * transfer / device.bandwidth_bytes_per_s
