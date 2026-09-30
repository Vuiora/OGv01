"""Opt-in large-data correctness and timing smoke; does not change the PLDA runtime.

Run: python -m tests.large_data_smoke --size 2048 --repeats 7
Reports both the current PLDA path and a separate context-reuse experiment.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib.metadata
import json
import multiprocessing as mp
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time
import traceback

from plda import PLDAModule, Program, RuntimeConfig, Task, Tensor, TensorSpec, discover_devices
from plda.io import dumps


def single_thread_environment():
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"


def check(actual, expected):
    import numpy as np
    if actual.shape != expected.shape:
        return {"passed": False, "reason": "shape mismatch"}
    error = np.abs(actual.astype(np.float64) - expected)
    # Fixed before testing. Inputs are signed binary fractions, exactly representable
    # in FP16; accumulation of <=2048 +/- terms is also exactly representable.
    tolerance = 1e-5 + 1e-4 * np.abs(expected)
    finite = bool(np.isfinite(actual).all())
    return {"passed": finite and bool((error <= tolerance).all()), "finite": finite,
            "max_absolute_error": float(error.max(initial=0)),
            "rms_error": float(np.sqrt(np.mean(error * error))),
            "violating_elements": int(np.count_nonzero(error > tolerance)),
            "elements": int(actual.size), "atol": 1e-5, "rtol": 1e-4}


def summary(samples):
    return {"median_ms": statistics.median(samples), "min_ms": min(samples),
            "max_ms": max(samples), "samples_ms": samples}


def reuse_worker(device, directory, repeats, warmup):
    """Separate bounded experiment: one process, one model/context per operation."""
    single_thread_environment()
    import numpy as np
    root = Path(directory)
    report = {"device": asdict(device), "operations": {}, "passed": False}
    try:
        a, b = np.load(root / "a.npy"), np.load(root / "b.npy")
        cp = core = ops = ov = None
        begin = time.perf_counter()
        if device.backend == "cupy":
            import cupy as cp
            cp.cuda.Device(int(device.target)).use()
            cp.cuda.runtime.free(0)  # Initialize the context before measuring model setup.
        elif device.backend == "openvino":
            import openvino as ov
            from openvino import opset13 as ops
            core = ov.Core()
        report["backend_initialization_ms"] = (time.perf_counter() - begin) * 1000
        for op in ("matmul", "add"):
            begin = time.perf_counter()
            targets = [device.target]
            if device.backend == "cpu":
                def run():
                    return a @ b if op == "matmul" else a + b
            elif device.backend == "cupy":
                def run():
                    x, y = cp.asarray(a), cp.asarray(b)
                    z = x @ y if op == "matmul" else x + y
                    host = cp.asnumpy(z)
                    cp.cuda.get_current_stream().synchronize()
                    return host
            else:
                x = ops.parameter(list(a.shape), np.float32, name="a")
                y = ops.parameter(list(b.shape), np.float32, name="b")
                out = ops.matmul(x, y, False, False) if op == "matmul" else ops.add(x, y)
                model = ov.Model([out], [x, y], f"plda_large_{op}")
                compiled = core.compile_model(model, device.target)
                targets = list(compiled.get_property("EXECUTION_DEVICES"))
                request = compiled.create_infer_request()
                def run():
                    outputs = request.infer({0: a, 1: b})
                    return outputs[compiled.output(0)].copy()
            setup_ms = (time.perf_counter() - begin) * 1000
            begin = time.perf_counter()
            actual = run()
            first_ms = (time.perf_counter() - begin) * 1000
            expected = np.load(root / f"reference_{op}.npy")
            first_check = check(actual, expected)
            for _ in range(warmup):
                run()
            samples = []
            for _ in range(repeats):
                begin = time.perf_counter()
                actual = run()
                samples.append((time.perf_counter() - begin) * 1000)
            record = {"setup_ms": setup_ms, "first_host_to_host_ms": first_ms,
                      "warm_host_to_host": summary(samples), "execution_targets": targets,
                      "first_check": first_check, "last_check": check(actual, expected)}
            # CUDA events distinguish resident GPU kernel time from host round trips.
            # This metric is not compared with OpenVINO API timings as if identical.
            if cp is not None:
                x, y = cp.asarray(a), cp.asarray(b)
                for _ in range(warmup):
                    z = x @ y if op == "matmul" else x + y
                cp.cuda.get_current_stream().synchronize()
                kernel_samples = []
                for _ in range(repeats):
                    start, end = cp.cuda.Event(), cp.cuda.Event()
                    start.record()
                    z = x @ y if op == "matmul" else x + y
                    end.record()
                    end.synchronize()
                    kernel_samples.append(float(cp.cuda.get_elapsed_time(start, end)))
                record["resident_cuda_event"] = summary(kernel_samples)
            report["operations"][op] = record
            print(f"  reuse {device.id} {op}: {statistics.median(samples):.3f} ms, "
                  f"check={record['last_check']['passed']}", flush=True)
        report["passed"] = all(v["first_check"]["passed"] and v["last_check"]["passed"]
                               for v in report["operations"].values())
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        (root / "reuse-result.json").write_text(dumps(report) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=2048)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, default=Path("docs/large-data-validation.json"))
    args = parser.parse_args()
    if not 64 <= args.size <= 2048 or not 1 <= args.repeats <= 30 or not 0 <= args.warmup <= 10 or args.timeout <= 0:
        parser.error("size 64..2048, repeats 1..30, warmup 0..10 and positive timeout required")
    single_thread_environment()
    import numpy as np
    devices, diagnostics = discover_devices(cpu_slots=1)
    devices = [replace(d, slots=1) for d in devices]
    rng = np.random.default_rng(20260914)
    n = args.size
    a = (rng.integers(0, 2, size=(n, n), dtype=np.int8).astype(np.float32) * 2 - 1) / 32
    b = (rng.integers(0, 2, size=(n, n), dtype=np.int8).astype(np.float32) * 2 - 1) / 32
    reference_start = time.perf_counter()
    reference_matmul = a.astype(np.float64) @ b.astype(np.float64)
    reference_add = a.astype(np.float64) + b.astype(np.float64)
    report = {"time_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "platform": platform.platform(), "size": n, "dtype": "float32", "seed": 20260914,
              "input_bytes": a.nbytes + b.nbytes, "output_bytes_per_op": a.nbytes,
              "matmul_flops": 2 * n**3, "reference": "NumPy binary64, BLAS threads limited to 1",
              "reference_seconds": time.perf_counter() - reference_start,
              "repeats": args.repeats, "warmup": args.warmup,
              "diagnostics": diagnostics, "devices": [], "packages": {},
              "scope": "PLDA one-shot end-to-end plus separate host-to-host context-reuse experiment; no runtime optimization applied"}
    for package in ("numpy", "cupy-cuda13x", "openvino"):
        report["packages"][package] = importlib.metadata.version(package)
    print(f"Data: {n}x{n}; input={report['input_bytes']/1024**2:.1f} MiB; "
          f"matmul={report['matmul_flops']/1e9:.2f} GFLOP", flush=True)
    spec = TensorSpec((n, n), "float32")
    inputs = {"a": Tensor(spec, a.tobytes()), "b": Tensor(spec, b.tobytes())}
    references = {"matmul": reference_matmul, "add": reference_add}
    with tempfile.TemporaryDirectory(prefix="plda-large-smoke-") as directory:
        root = Path(directory)
        np.save(root / "a.npy", a)
        np.save(root / "b.npy", b)
        for op, reference in references.items():
            np.save(root / f"reference_{op}.npy", reference)
        for device in devices:
            print(f"PLDA {device.id}: {device.name or device.target}", flush=True)
            tasks = tuple(Task(op, op, ("a", "b"), numerical_mode="relaxed", devices=(device.kind,),
                               timeout_s=args.timeout, max_retries=0, workspace_bytes=64 * 1024**2)
                          for op in ("matmul", "add"))
            program = Program(inputs, tasks, ("matmul", "add"))
            with PLDAModule([device], RuntimeConfig(job_timeout_s=2 * args.timeout + 10)) as module:
                result = module.run(program)
            checks = {}
            for op in references:
                tensor = result.outputs.get(op)
                checks[op] = (check(np.frombuffer(tensor.data, dtype="<f4").reshape(n, n), references[op])
                              if tensor else {"passed": False, "error": result.errors.get(op)})
            commits = {e.task: e for e in result.events if e.kind == "COMMITTED"}
            reservations = {e.task: e for e in result.events if e.kind == "RESERVED"}
            one_shot = {"state": result.state, "elapsed_s": result.elapsed_s, "checks": checks,
                        "errors": result.errors, "events": [asdict(e) for e in result.events],
                        "task_end_to_end_ms": {op: (e.time_s - reservations[op].time_s) * 1000
                                               for op, e in commits.items()}}
            one_shot["passed"] = (result.state == "SUCCEEDED" and all(v["passed"] for v in checks.values())
                                   and {e.device for e in commits.values()} == {device.id})
            record = {"device": asdict(device), "plda": one_shot}
            print(f"  PLDA {result.state}: {result.elapsed_s:.3f} s, check={one_shot['passed']}", flush=True)
            report["devices"].append(record)
            args.output.write_text(dumps(report) + "\n", encoding="utf-8")
            process = mp.get_context("spawn").Process(target=reuse_worker,
                        args=(device, directory, args.repeats, args.warmup))
            begin = time.perf_counter()
            process.start()
            process.join(args.timeout)
            if process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                record["reuse"] = {"passed": False, "error": "experiment process timed out"}
            elif process.exitcode != 0:
                record["reuse"] = {"passed": False, "error": f"process exit code {process.exitcode}"}
            else:
                record["reuse"] = json.loads((root / "reuse-result.json").read_text(encoding="utf-8"))
            record["reuse"]["experiment_process_s"] = time.perf_counter() - begin
            process.close()
            args.output.write_text(dumps(report) + "\n", encoding="utf-8")
    report["tested_kinds"] = sorted({d.kind for d in devices})
    report["passed"] = all(d["plda"]["passed"] and d["reuse"]["passed"] for d in report["devices"])
    args.output.write_text(dumps(report) + "\n", encoding="utf-8")
    print(f"Large-data smoke passed={report['passed']}; report={args.output}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
