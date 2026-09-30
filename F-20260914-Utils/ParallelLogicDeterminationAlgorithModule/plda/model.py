"""Public, pickle-safe IR. Resource names identify canonical allocations, not pointers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import struct
from typing import Any


def positive(value: float, name: str, *, zero: bool = False) -> None:
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")


@dataclass(frozen=True)
class Access:
    resource: str | None
    mode: str
    start: int | None = None
    end: int | None = None  # Exclusive byte offset; None means the whole allocation.

    def __post_init__(self) -> None:
        if self.mode not in {"read", "write"}:
            raise ValueError("access mode must be read or write")
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must both be set or both omitted")
        if self.start is not None and (self.start < 0 or self.end <= self.start):
            raise ValueError("access requires 0 <= start < end")


@dataclass(frozen=True)
class Instruction:
    id: str
    accesses: tuple[Access, ...] = ()
    depends_on: tuple[str, ...] = ()
    latency: int = 1
    unit: str = "alu"
    occupancy: int = 1
    effects_complete: bool = True
    barrier: bool = False  # Includes non-speculatable control, volatile IO and traps.

    def __post_init__(self) -> None:
        if not self.id or self.latency < 1 or self.occupancy < 1:
            raise ValueError("instruction needs an id and positive latency/occupancy")
        if not isinstance(self.latency, int) or not isinstance(self.occupancy, int):
            raise ValueError("latency and occupancy are integer cycles")


@dataclass(frozen=True)
class MachineModel:
    issue_width: int = 2
    units: dict[str, int] = field(default_factory=lambda: {"alu": 2, "load": 1, "store": 1})

    def __post_init__(self) -> None:
        if not isinstance(self.issue_width, int) or self.issue_width < 1:
            raise ValueError("issue_width must be a positive integer")
        if not self.units or any(not isinstance(v, int) or v < 1 for v in self.units.values()):
            raise ValueError("unit counts must be positive integers")


@dataclass(frozen=True)
class AffineAccess:
    resource: str | None
    mode: str
    stride: int | None  # Byte address = stride * logical iteration + offset.
    offset: int = 0
    width: int = 4

    def __post_init__(self) -> None:
        if self.mode not in {"read", "write"} or self.width < 1:
            raise ValueError("invalid affine access")
        if any(not isinstance(x, int) for x in (self.offset, self.width)):
            raise ValueError("offset/width must be integers")
        if self.stride is not None and not isinstance(self.stride, int):
            raise ValueError("stride must be an integer or None")


@dataclass(frozen=True)
class Loop:
    id: str
    trip_count: int | None
    accesses: tuple[AffineAccess, ...]
    vector_width: int = 4
    effects_complete: bool = True
    control: str = "uniform"  # uniform, masked, unknown
    target_supports_masks: bool = False
    reduction: str | None = None  # A separate final reduction of lane results.
    dtype: str = "float32"
    allow_reassociation: bool = False

    def __post_init__(self) -> None:
        if self.trip_count is not None and (not isinstance(self.trip_count, int) or self.trip_count < 0):
            raise ValueError("trip_count must be a nonnegative integer or None")
        if not isinstance(self.vector_width, int) or self.vector_width < 2:
            raise ValueError("vector_width must be at least two")
        if self.control not in {"uniform", "masked", "unknown"}:
            raise ValueError("invalid loop control")
        if self.dtype not in {"float32", "float64", "uint64_mod"}:
            raise ValueError("unsupported loop dtype")


@dataclass(frozen=True)
class TensorSpec:
    shape: tuple[int, ...]
    dtype: str = "float64"

    def __post_init__(self) -> None:
        if self.dtype not in {"float32", "float64"}:
            raise ValueError("tensor dtype must be float32 or float64")
        if any(not isinstance(x, int) or x < 0 for x in self.shape):
            raise ValueError("shape dimensions must be nonnegative integers")

    @property
    def size(self) -> int:
        return math.prod(self.shape)

    @property
    def itemsize(self) -> int:
        return 4 if self.dtype == "float32" else 8

    @property
    def nbytes(self) -> int:
        return self.size * self.itemsize


@dataclass(frozen=True)
class Tensor:
    spec: TensorSpec
    data: bytes  # Immutable little-endian contiguous host buffer.

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or len(self.data) != self.spec.nbytes:
            raise ValueError("tensor buffer size does not match its spec")

    @classmethod
    def from_values(cls, values, shape=None, dtype="float64") -> Tensor:
        values = tuple(values)
        spec = TensorSpec(tuple(shape) if shape is not None else (len(values),), dtype)
        if len(values) != spec.size:
            raise ValueError("value count does not match shape")
        fmt = "f" if dtype == "float32" else "d"
        return cls(spec, struct.pack(f"<{len(values)}{fmt}", *values))

    def values(self) -> tuple[float, ...]:
        fmt = "f" if self.spec.dtype == "float32" else "d"
        return struct.unpack(f"<{self.spec.size}{fmt}", self.data)

    def to_dict(self) -> dict:
        return {"shape": list(self.spec.shape), "dtype": self.spec.dtype, "values": list(self.values())}


@dataclass(frozen=True)
class Task:
    id: str  # Also its single-assignment output buffer id.
    op: str
    inputs: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    accesses: tuple[Access, ...] = ()  # Additional external resources for analysis.
    effects_complete: bool = True
    barrier: bool = False
    devices: tuple[str, ...] = ("CPU", "GPU", "NPU")
    numerical_mode: str = "strict"  # strict = scalar CPU reference; relaxed = backend arithmetic.
    estimated_ms: dict[str, float] = field(default_factory=lambda: {"CPU": 1.0})
    timeout_s: float = 30.0
    max_retries: int = 1
    workspace_bytes: int = 0

    def __post_init__(self) -> None:
        if not self.id or not self.op:
            raise ValueError("task needs id and op")
        if not self.devices or any(x not in {"CPU", "GPU", "NPU"} for x in self.devices):
            raise ValueError("devices must be a nonempty subset of CPU/GPU/NPU")
        if self.numerical_mode not in {"strict", "relaxed"}:
            raise ValueError("invalid numerical_mode")
        positive(self.timeout_s, "timeout_s")
        if not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if not isinstance(self.workspace_bytes, int) or self.workspace_bytes < 0:
            raise ValueError("workspace_bytes must be a nonnegative integer")
        for kind, cost in self.estimated_ms.items():
            if kind not in {"CPU", "GPU", "NPU"}:
                raise ValueError("unknown cost device kind")
            positive(cost, "estimated_ms")


@dataclass(frozen=True)
class Program:
    inputs: dict[str, Tensor]
    tasks: tuple[Task, ...]
    outputs: tuple[str, ...]
    instructions: tuple[Instruction, ...] = ()
    loops: tuple[Loop, ...] = ()


@dataclass(frozen=True)
class Device:
    id: str
    kind: str
    backend: str
    target: str
    slots: int = 1
    memory_bytes: int = 512 * 1024 * 1024
    bandwidth_bytes_per_s: float = 5e9
    launch_ms: float = 100.0  # Includes process/context startup; calibrate for deployment.
    name: str = ""

    def __post_init__(self) -> None:
        if not self.id or self.kind not in {"CPU", "GPU", "NPU"}:
            raise ValueError("invalid device")
        if self.backend not in {"cpu", "cupy", "openvino"}:
            raise ValueError("invalid backend")
        if self.backend == "cpu" and self.kind != "CPU":
            raise ValueError("CPU backend cannot advertise accelerator execution")
        if self.backend == "cupy" and self.kind != "GPU":
            raise ValueError("CuPy backend requires GPU")
        if self.backend == "openvino" and not self.target.startswith(self.kind):
            raise ValueError("OpenVINO target must explicitly match device kind")
        if not isinstance(self.slots, int) or self.slots < 1 or self.memory_bytes < 1:
            raise ValueError("positive slots and memory budget required")
        positive(self.bandwidth_bytes_per_s, "bandwidth")
        positive(self.launch_ms, "launch_ms", zero=True)


def plain(value):
    """Convert dataclass reports to JSON-compatible objects."""
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value
