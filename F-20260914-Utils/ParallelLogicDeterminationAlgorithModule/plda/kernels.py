"""Audited pure tensor operations and static shape inference for executable plans."""
from __future__ import annotations

import math
from .model import Task, Tensor, TensorSpec

ELEMENTWISE = {"add", "multiply", "scale", "relu", "identity"}
OPERATIONS = ELEMENTWISE | {"matmul", "sum", "slice", "concat"}


def infer(task: Task, inputs: list[TensorSpec]) -> TensorSpec:
    if task.op not in OPERATIONS:
        raise ValueError(f"unsupported executable kernel {task.op!r}; use IR for opaque analysis")
    arity = 2 if task.op in {"add", "multiply", "matmul"} else 1
    if task.op == "concat":
        if not inputs:
            raise ValueError("concat needs inputs")
    elif len(inputs) != arity:
        raise ValueError(f"{task.op} requires {arity} input(s)")
    if len({x.dtype for x in inputs}) != 1:
        raise ValueError("mixed tensor dtypes require explicit conversion outside this module")
    first = inputs[0]
    expected = {"scale": {"factor"}, "slice": {"start", "stop"}}.get(task.op, set())
    if set(task.params) != expected:
        raise ValueError(f"{task.op}: expected params {sorted(expected)}")
    if task.op in {"add", "multiply"} and first.shape != inputs[1].shape:
        raise ValueError("elementwise shapes must match; implicit broadcasting is disabled")
    if task.op == "scale":
        if not isinstance(task.params["factor"], (int, float)) or not math.isfinite(task.params["factor"]):
            raise ValueError("scale factor must be finite")
    if task.op == "matmul":
        if len(first.shape) != 2 or len(inputs[1].shape) != 2 or first.shape[1] != inputs[1].shape[0]:
            raise ValueError("matmul requires compatible two-dimensional shapes")
        return TensorSpec((first.shape[0], inputs[1].shape[1]), first.dtype)
    if task.op == "sum":
        return TensorSpec((), first.dtype)
    if task.op == "slice":
        start, stop = task.params["start"], task.params["stop"]
        if len(first.shape) != 1 or not isinstance(start, int) or not isinstance(stop, int):
            raise ValueError("slice requires a vector and integer bounds")
        if not 0 <= start <= stop <= first.size:
            raise ValueError("slice bounds outside input")
        return TensorSpec((stop - start,), first.dtype)
    if task.op == "concat":
        if any(len(x.shape) != 1 for x in inputs):
            raise ValueError("concat currently supports vectors only")
        return TensorSpec((sum(x.size for x in inputs),), first.dtype)
    return first


def scalar_execute(task: Task, inputs: list[Tensor]) -> Tensor:
    """Strict reference: Python binary64 operations, left-fold sums, output dtype cast."""
    spec = infer(task, [x.spec for x in inputs])
    arrays = [x.values() for x in inputs]
    a = arrays[0]
    if task.op == "add":
        result = [x + y for x, y in zip(a, arrays[1])]
    elif task.op == "multiply":
        result = [x * y for x, y in zip(a, arrays[1])]
    elif task.op == "scale":
        result = [x * task.params["factor"] for x in a]
    elif task.op == "relu":
        result = [x if x > 0 else 0.0 for x in a]
    elif task.op == "identity":
        return inputs[0]
    elif task.op == "slice":
        result = a[task.params["start"]:task.params["stop"]]
    elif task.op == "concat":
        result = [x for arr in arrays for x in arr]
    elif task.op == "sum":
        total = 0.0
        for x in a:
            total = total + x
        result = [total]
    elif task.op == "matmul":
        m, k = inputs[0].spec.shape
        n = inputs[1].spec.shape[1]
        b, result = arrays[1], []
        for row in range(m):
            for col in range(n):
                total = 0.0
                for inner in range(k):
                    total = total + a[row * k + inner] * b[inner * n + col]
                result.append(total)
    else:
        raise ValueError(task.op)
    return Tensor.from_values(result, spec.shape, spec.dtype)


def array_execute(xp, task: Task, arrays):
    """NumPy/CuPy arithmetic under the explicit relaxed numerical contract."""
    a = arrays[0]
    if task.op == "add":
        return a + arrays[1]
    if task.op == "multiply":
        return a * arrays[1]
    if task.op == "scale":
        return a * task.params["factor"]
    if task.op == "relu":
        return xp.where(a > 0, a, 0.0)
    if task.op == "identity":
        return a.copy()
    if task.op == "matmul":
        return a @ arrays[1]
    if task.op == "sum":
        return xp.asarray(a.sum(), dtype=a.dtype)
    if task.op == "slice":
        return a[task.params["start"]:task.params["stop"]].copy()
    if task.op == "concat":
        return xp.concatenate(arrays)
    raise ValueError(task.op)
