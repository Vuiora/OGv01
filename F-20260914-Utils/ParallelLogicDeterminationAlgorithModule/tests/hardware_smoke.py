"""Opt-in real-device validation. Run: python -m tests.hardware_smoke --output PATH.

Each device is the sole inventory for its job, so CPU fallback cannot pass a GPU/NPU test.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import argparse
import importlib.metadata
from pathlib import Path
import platform

from plda import PLDAModule, Program, RuntimeConfig, Task, Tensor, discover_devices
from plda.io import dumps
from plda.kernels import scalar_execute


def request_for(device):
    # Exactly representable binary fractions make validation independent of
    # random seeds while still covering signed, nonconstant data and reductions.
    a = Tensor.from_values([((i * 7) % 31 - 15) / 16 for i in range(64 * 64)], (64, 64), "float32")
    b = Tensor.from_values([((i * 3) % 23 - 11) / 16 for i in range(64 * 64)], (64, 64), "float32")
    definitions = [("add", ("a", "b"), {}), ("multiply", ("a", "b"), {}),
                   ("scale", ("a",), {"factor": 1.5}), ("relu", ("a",), {}),
                   ("identity", ("a",), {}), ("matmul", ("a", "b"), {}), ("sum", ("a",), {})]
    tasks = tuple(Task(op, op, refs, params, devices=(device.kind,), numerical_mode="relaxed",
                       timeout_s=120, max_retries=0) for op, refs, params in definitions)
    return Program({"a": a, "b": b}, tasks, tuple(t.id for t in tasks))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/hardware-validation.json"))
    args = parser.parse_args()
    devices, diagnostics = discover_devices()
    report = {"time_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "platform": platform.platform(), "packages": {}, "diagnostics": diagnostics, "devices": []}
    for name in ("numpy", "cupy-cuda13x", "openvino", "cuda-toolkit"):
        try:
            report["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    all_passed = True
    for device in devices:
        print(f"Testing {device.id}: {device.name or device.target}", flush=True)
        program = request_for(device)
        with PLDAModule([device], RuntimeConfig(job_timeout_s=900)) as module:
            result = module.run(program)
        checks = {}
        for task in program.tasks:
            expected = scalar_execute(task, [program.inputs[k] for k in task.inputs])
            actual = result.outputs.get(task.id)
            if actual is None:
                checks[task.id] = {"passed": False, "error": result.errors.get(task.id)}
                continue
            pairs = list(zip(actual.values(), expected.values()))
            max_error = max((abs(a - b) for a, b in pairs), default=0)
            # Test tolerance for this bounded input family, not a module-wide accuracy promise.
            checks[task.id] = {"passed": all(abs(a - b) <= 1e-4 + 1e-3 * abs(b) for a, b in pairs),
                               "max_absolute_error": max_error, "shape": actual.spec.shape}
        committed_devices = {e.device for e in result.events if e.kind == "COMMITTED"}
        passed = result.state == "SUCCEEDED" and all(c["passed"] for c in checks.values()) and committed_devices == {device.id}
        all_passed &= passed
        report["devices"].append({"device": asdict(device), "passed": passed,
                                  "state": result.state, "elapsed_s": result.elapsed_s,
                                  "checks": checks, "errors": result.errors,
                                  "events": [asdict(e) for e in result.events]})
        print(f"  {result.state}; checks={[(k,v['passed']) for k,v in checks.items()]}", flush=True)
        args.output.write_text(dumps(report) + "\n", encoding="utf-8")
    report["all_discovered_devices_passed"] = all_passed
    report["tested_kinds"] = sorted({d.kind for d in devices})
    args.output.write_text(dumps(report) + "\n", encoding="utf-8")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
