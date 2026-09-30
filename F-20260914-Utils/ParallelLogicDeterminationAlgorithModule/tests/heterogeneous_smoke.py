"""Exercise CPU, GPU and NPU together in one real scheduled job."""
from dataclasses import asdict
from pathlib import Path
from plda import PLDAModule, Program, RuntimeConfig, Task, Tensor, discover_devices
from plda.io import dumps


def main():
    devices, diagnostics = discover_devices()
    # One real backend of each kind, with CUDA preferred over Intel GPU for this test.
    selected = []
    for kind in ("CPU", "GPU", "NPU"):
        candidates = [d for d in devices if d.kind == kind]
        if not candidates:
            raise RuntimeError(f"real {kind} required for this opt-in integration test")
        selected.append(min(candidates, key=lambda d: (d.backend != "cupy", d.id)))
    values = [((i * 7) % 31 - 15) / 16 for i in range(4096)]
    tensor = Tensor.from_values(values, (64, 64), "float32")
    tasks = tuple(Task(f"on_{kind}", "scale", ("x",), {"factor": factor},
                       devices=(kind,), numerical_mode="relaxed", timeout_s=120, max_retries=0)
                  for kind, factor in (("CPU", 2), ("GPU", 3), ("NPU", 4)))
    tasks += (Task("merge_ab", "add", ("on_CPU", "on_GPU"), devices=("CPU",)),
              Task("result", "add", ("merge_ab", "on_NPU"), devices=("CPU",)))
    program = Program({"x": tensor}, tasks, ("result",))
    with PLDAModule(selected, RuntimeConfig(job_timeout_s=300)) as module:
        result = module.run(program)
    starts = {e.task: e.time_s for e in result.events if e.kind == "RESERVED"}
    commits = {e.task: e.time_s for e in result.events if e.kind == "COMMITTED"}
    roots = ["on_CPU", "on_GPU", "on_NPU"]
    concurrent = all(k in starts and k in commits for k in roots) and max(starts[k] for k in roots) < min(commits[k] for k in roots)
    expected = Tensor.from_values([x * 9 for x in values], (64, 64), "float32")
    correct = result.state == "SUCCEEDED" and result.outputs["result"] == expected
    summary = {"passed": correct and concurrent, "correct_output": correct,
               "root_execution_intervals_overlap": concurrent,
               "devices": [asdict(d) for d in selected], "diagnostics": diagnostics,
               "result": result.to_dict()}
    Path("docs/heterogeneous-validation.json").write_text(dumps(summary) + "\n", encoding="utf-8")
    print(dumps({"passed": summary["passed"], "correct_output": correct,
                 "root_execution_intervals_overlap": concurrent,
                 "starts": starts, "commits": commits, "errors": result.errors}))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
