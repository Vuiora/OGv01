"""Safe data partitioning for vector elementwise kernels; unequal tail chunks allowed."""
from .kernels import ELEMENTWISE
from .model import Program, Task, Tensor, TensorSpec


def shard_elementwise(op: str, inputs: list[Tensor], chunk_size: int, *, params=None,
                      numerical_mode="strict", devices=("CPU", "GPU", "NPU"), estimated_ms=None) -> Program:
    if op not in ELEMENTWISE or not isinstance(chunk_size, int) or chunk_size < 1 or not inputs:
        raise ValueError("elementwise op, nonempty inputs and positive integer chunk_size required")
    spec = inputs[0].spec
    if len(spec.shape) != 1 or any(x.spec != spec for x in inputs):
        raise ValueError("sharding requires vectors of identical shape and dtype")
    buffers, tasks, outputs = {}, [], []
    for index, start in enumerate(range(0, max(spec.size, 1), chunk_size)):
        stop = min(spec.size, start + chunk_size)
        refs = []
        for i, tensor in enumerate(inputs):
            name = f"input_{i}_part_{index}"
            buffers[name] = Tensor(TensorSpec((stop - start,), spec.dtype),
                                   tensor.data[start * spec.itemsize:stop * spec.itemsize])
            refs.append(name)
        ident = f"part_{index}"
        tasks.append(Task(ident, op, tuple(refs), params=dict(params or {}),
                          numerical_mode=numerical_mode, devices=tuple(devices),
                          estimated_ms=dict(estimated_ms or {"CPU": 1.0})))
        outputs.append(ident)
    tasks.append(Task("result", "concat", tuple(outputs), devices=("CPU",)))
    return Program(buffers, tuple(tasks), ("result",))
