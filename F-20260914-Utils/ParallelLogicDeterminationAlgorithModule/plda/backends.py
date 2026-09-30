"""Real CPU / CUDA-CuPy / Intel OpenVINO adapters. No simulated accelerators."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import time
from .kernels import array_execute, infer, scalar_execute
from .model import Device, Task, Tensor


class BackendUnavailable(RuntimeError):
    pass


def discover_devices(cpu_slots: int | None = None) -> tuple[list[Device], list[str]]:
    devices = [Device("cpu:0", "CPU", "cpu", "host", slots=cpu_slots or min(os.cpu_count() or 1, 4),
                      memory_bytes=1024**3)]
    diagnostics = []
    if importlib.util.find_spec("cupy") is not None:
        try:
            import cupy as cp
            for index in range(cp.cuda.runtime.getDeviceCount()):
                with cp.cuda.Device(index):
                    free, _ = cp.cuda.runtime.memGetInfo()
                    name = cp.cuda.runtime.getDeviceProperties(index)["name"]
                    if isinstance(name, bytes):
                        name = name.decode("utf-8")
                devices.append(Device(f"cuda:{index}", "GPU", "cupy", str(index),
                                      memory_bytes=max(1, int(free * 0.5)), launch_ms=500, name=name))
        except Exception as exc:
            diagnostics.append(f"CuPy discovery unavailable: {type(exc).__name__}: {exc}")
    else:
        diagnostics.append("CuPy not installed; CUDA GPU backend unavailable")
    if importlib.util.find_spec("openvino") is not None:
        try:
            import openvino as ov
            core = ov.Core()
            for target in core.available_devices:
                kind = target.split(".")[0]
                if kind in {"GPU", "NPU"}:
                    name = core.get_property(target, "FULL_DEVICE_NAME")
                    # OpenCL enumeration can also expose the NVIDIA GPU. The Intel
                    # adapter intentionally avoids advertising it a second time.
                    if kind == "GPU" and "Intel" not in name:
                        diagnostics.append(f"Excluded non-Intel OpenVINO GPU {target}: {name}; use its native adapter")
                        continue
                    devices.append(Device(f"ov:{target}", kind, "openvino", target, launch_ms=1000, name=name))
        except Exception as exc:
            diagnostics.append(f"OpenVINO discovery unavailable: {type(exc).__name__}: {exc}")
    else:
        diagnostics.append("OpenVINO not installed; OpenVINO GPU/NPU backends unavailable")
    return devices, diagnostics


def _host_arrays(inputs):
    import numpy as np
    return [np.frombuffer(t.data, dtype="<f4" if t.spec.dtype == "float32" else "<f8").reshape(t.spec.shape)
            for t in inputs]


def _to_tensor(array, spec):
    import numpy as np
    data = np.asarray(array, dtype="<f4" if spec.dtype == "float32" else "<f8")
    if data.shape != spec.shape:
        raise ValueError(f"backend output shape mismatch: {data.shape} != {spec.shape}")
    return Tensor(spec, data.tobytes(order="C"))


def _openvino_execute(task, inputs, target):
    try:
        import numpy as np
        import openvino as ov
        from openvino import opset13 as ops
    except ImportError as exc:
        raise BackendUnavailable("OpenVINO and NumPy are required") from exc
    core = ov.Core()
    if target not in core.available_devices:
        raise BackendUnavailable(f"OpenVINO device {target} unavailable")
    arrays = _host_arrays(inputs)
    parameters = [ops.parameter(list(a.shape), np.float32, name=f"input_{i}") for i, a in enumerate(arrays)]
    a = parameters[0]
    if task.op == "add":
        out = ops.add(a, parameters[1])
    elif task.op == "multiply":
        out = ops.multiply(a, parameters[1])
    elif task.op == "scale":
        out = ops.multiply(a, ops.constant(np.float32(task.params["factor"])))
    elif task.op == "relu":
        out = ops.relu(a)
    elif task.op == "identity":
        out = ops.add(a, ops.constant(np.float32(0)))
    elif task.op == "matmul":
        out = ops.matmul(a, parameters[1], False, False)
    elif task.op == "sum":
        out = ops.reduce_sum(a, ops.constant(np.arange(arrays[0].ndim, dtype=np.int64)), False)
    else:
        raise ValueError(f"unsupported OpenVINO operation {task.op}")
    model = ov.Model([out], parameters, f"plda_{task.id}")
    # Explicit target prevents AUTO silently executing NPU-assigned work on CPU.
    compiled = core.compile_model(model, target)
    request = compiled.create_infer_request()
    result = request.infer({i: array for i, array in enumerate(arrays)})
    return _to_tensor(result[compiled.output(0)], infer(task, [x.spec for x in inputs]))


def execute(task: Task, inputs: list[Tensor], device: Device) -> tuple[Tensor, str]:
    spec = infer(task, [x.spec for x in inputs])
    if device.backend == "cpu":
        if task.numerical_mode == "relaxed" and importlib.util.find_spec("numpy") is not None:
            import numpy as np
            return _to_tensor(array_execute(np, task, _host_arrays(inputs)), spec), "numpy-native"
        return scalar_execute(task, inputs), "scalar-reference"
    if task.numerical_mode != "relaxed":
        raise ValueError("accelerator execution requires relaxed numerical mode")
    if device.backend == "cupy":
        try:
            import cupy as cp
        except ImportError as exc:
            raise BackendUnavailable("CuPy/CUDA backend is not installed") from exc
        with cp.cuda.Device(int(device.target)):
            arrays = [cp.asarray(a) for a in _host_arrays(inputs)]
            result = array_execute(cp, task, arrays)
            host = cp.asnumpy(result)  # Device-to-host transfer completes before commit.
            cp.cuda.get_current_stream().synchronize()
            return _to_tensor(host, spec), "cupy-cuda"
    if device.backend == "openvino":
        return _openvino_execute(task, inputs, device.target), f"openvino:{device.target}"
    raise BackendUnavailable(device.backend)


def worker(task: Task, inputs: list[Tensor], device: Device, destination: str) -> None:
    """One isolated attempt. Publish metadata last; partial output is never committed."""
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    base = Path(destination)
    started = time.monotonic()
    try:
        tensor, mode = execute(task, inputs, device)
        (base / "output.bin").write_bytes(tensor.data)
        status = {"ok": True, "execution_mode": mode, "finished_at": time.monotonic(),
                  "elapsed_ms": (time.monotonic() - started) * 1000,
                  "input_bytes": sum(t.spec.nbytes for t in inputs), "output_bytes": tensor.spec.nbytes}
    except Exception as exc:
        name = type(exc).__name__
        code = "KERNEL_ERROR"
        if isinstance(exc, BackendUnavailable):
            code = "BACKEND_UNAVAILABLE"
        elif isinstance(exc, MemoryError) or "OutOfMemory" in name:
            code = "OUT_OF_MEMORY"
        elif isinstance(exc, (ValueError, TypeError, OverflowError)):
            code = "INVALID_EXECUTION"
        status = {"ok": False, "code": code, "message": f"{name}: {exc}"[:2000]}
    temp = base / "status.tmp"
    temp.write_text(json.dumps(status), encoding="utf-8")
    os.replace(temp, base / "status.json")
