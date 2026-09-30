"""Bounded asynchronous module with process isolation and atomic host-buffer commit."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import multiprocessing as mp
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .backends import discover_devices, worker
from .model import Device, Program, Tensor, positive
from .planner import Plan, eligibility, placement_cost_ms, prepare, reservation_bytes

TERMINAL = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED"}
RETRYABLE = {"BACKEND_UNAVAILABLE", "OUT_OF_MEMORY", "WORKER_CRASH", "TASK_TIMEOUT", "KERNEL_ERROR"}


@dataclass(frozen=True)
class RuntimeConfig:
    max_jobs: int = 16
    max_tasks: int = 1024
    host_buffer_bytes: int = 1024**3
    job_timeout_s: float = 300.0
    poll_s: float = 0.01

    def __post_init__(self):
        if min(self.max_jobs, self.max_tasks, self.host_buffer_bytes) < 1:
            raise ValueError("admission limits must be positive")
        positive(self.job_timeout_s, "job_timeout_s")
        positive(self.poll_s, "poll_s")


@dataclass
class Event:
    time_s: float
    kind: str
    task: str | None = None
    device: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class JobResult:
    job_id: str
    state: str
    outputs: dict[str, Tensor]
    task_states: dict[str, str]
    attempts: dict[str, int]
    errors: dict[str, dict]
    events: list[Event]
    elapsed_s: float

    def to_dict(self):
        return {"job_id": self.job_id, "state": self.state,
                "outputs": {k: v.to_dict() for k, v in self.outputs.items()},
                "task_states": self.task_states, "attempts": self.attempts,
                "errors": self.errors, "events": [asdict(x) for x in self.events],
                "elapsed_s": self.elapsed_s}


class JobHandle:
    def __init__(self, plan: Plan):
        self.id = uuid.uuid4().hex
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._state = "QUEUED"
        self._task_states = {t.id: "PENDING" for t in plan.program.tasks}
        self._attempts = {t.id: 0 for t in plan.program.tasks}
        self._events: list[Event] = []
        self._future: Future | None = None

    def cancel(self) -> bool:
        with self._lock:
            if self._state in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                return False
            self._cancel.set()
            return True

    def result(self, timeout: float | None = None) -> JobResult:
        """Waiting timeout does not cancel work. Use cancel() for cancellation."""
        return self._future.result(timeout)

    def done(self) -> bool:
        return self._future.done()

    def status(self) -> dict:
        with self._lock:
            return {"job_id": self.id, "state": self._state, "task_states": dict(self._task_states),
                    "attempts": dict(self._attempts), "events": [asdict(e) for e in self._events[-20:]]}


@dataclass
class _Attempt:
    process: mp.Process
    device: Device
    started: float
    reserved: int
    directory: Path


def _stop(process: mp.Process) -> None:
    """Never reclaim a slot before its process is confirmed dead."""
    if process.is_alive():
        process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    if process.is_alive():
        raise RuntimeError("worker could not be stopped; resource reuse is forbidden")


class PLDAModule:
    """One bounded FIFO job queue; ready tasks within a job run concurrently.

    FIFO ownership makes all module device/memory limits apply across submissions.
    Separate module instances must receive separate physical resource quotas.
    """

    def __init__(self, devices: list[Device] | None = None, config: RuntimeConfig | None = None):
        self.config = config or RuntimeConfig()
        self.devices, self.diagnostics = discover_devices() if devices is None else (list(devices), [])
        if not self.devices or len({d.id for d in self.devices}) != len(self.devices):
            raise ValueError("distinct nonempty device inventory required")
        self._coordinator = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plda")
        self._admission = threading.BoundedSemaphore(self.config.max_jobs)
        self._lock = threading.Lock()
        self._handles: dict[str, JobHandle] = {}
        self._closed = False
        self._quarantined: set[str] = set()
        self._context = mp.get_context("spawn")

    def capabilities(self) -> dict:
        with self._lock:
            quarantined = sorted(self._quarantined)
        return {"module": "PLDA", "version": "0.1.0", "analyses": ["ILP", "TLP", "SIMD"],
                "devices": [asdict(d) for d in self.devices], "diagnostics": self.diagnostics,
                "quarantined_devices": quarantined,
                "job_policy": "bounded FIFO; parallel tasks within one active job"}

    def analyze(self, program: Program, machine=None) -> Plan:
        if len(program.tasks) > self.config.max_tasks:
            raise ValueError("task analysis/admission budget exceeded")
        if len(program.instructions) > 4096 or len(program.loops) > 1024:
            raise ValueError("instruction/loop analysis budget exceeded")
        return prepare(deepcopy(program), machine)

    def submit(self, program: Program) -> JobHandle:
        if not self._admission.acquire(blocking=False):
            raise RuntimeError("PLDA queue is full; apply upstream backpressure")
        try:
            plan = self.analyze(program)  # Snapshot protects against caller mutation.
            with self._lock:
                if self._closed:
                    raise RuntimeError("PLDA module is closed")
                handle = JobHandle(plan)
                self._handles[handle.id] = handle
                handle._future = self._coordinator.submit(self._run_guarded, plan, handle)
            handle._future.add_done_callback(lambda _: self._finished(handle.id))
            return handle
        except BaseException:
            self._admission.release()
            raise

    def _finished(self, job_id):
        with self._lock:
            self._handles.pop(job_id, None)
        self._admission.release()

    def run(self, program: Program) -> JobResult:
        return self.submit(program).result()

    def close(self, *, cancel: bool = False):
        with self._lock:
            self._closed = True
            if cancel:
                for handle in self._handles.values():
                    handle.cancel()
        self._coordinator.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close(cancel=exc_type is not None)

    def _run_guarded(self, plan, handle):
        try:
            return self._run(plan, handle)
        except Exception as exc:
            # _run's finally stops workers first. An unexpected supervisor failure
            # trips the whole inventory so later jobs cannot reuse uncertain devices.
            with self._lock:
                self._quarantined.update(d.id for d in self.devices)
            with handle._lock:
                handle._state = "FAILED"
                for ident, status in handle._task_states.items():
                    if status not in TERMINAL:
                        handle._task_states[ident] = "FAILED"
                return JobResult(handle.id, "FAILED", {}, dict(handle._task_states), dict(handle._attempts),
                    {"__job__": {"code": "SUPERVISOR_ERROR", "message": f"{type(exc).__name__}: {exc}"}},
                    list(handle._events), 0.0)

    def _run(self, plan: Plan, handle: JobHandle) -> JobResult:
        started = time.monotonic()
        tasks = {t.id: t for t in plan.program.tasks}
        pred = plan.graph.predecessors()
        states, attempts, events = handle._task_states, handle._attempts, handle._events
        store = dict(plan.program.inputs)
        errors: dict[str, dict] = {}
        active: dict[str, _Attempt] = {}
        slots = {d.id: 0 for d in self.devices}
        memory = {d.id: 0 for d in self.devices}
        excluded = {k: set() for k in tasks}
        end_state = None

        def event(kind, task=None, device=None, **detail):
            with handle._lock:
                events.append(Event(time.monotonic() - started, kind, task, device, detail))

        def state(task, value):
            with handle._lock:
                states[task] = value

        def release(ident):
            attempt = active.pop(ident)
            if attempt.process.is_alive():
                raise RuntimeError("attempt still alive at resource release")
            slots[attempt.device.id] -= 1
            memory[attempt.device.id] -= attempt.reserved
            event("RELEASE", ident, attempt.device.id, reserved_bytes=attempt.reserved)
            attempt.process.close()
            return attempt

        def fail(ident, code, message, device):
            task = tasks[ident]
            errors[ident] = {"code": code, "message": message, "device": device.id if device else None}
            event("ATTEMPT_FAILED", ident, device.id if device else None, code=code, message=message)
            if device:
                excluded[ident].add(device.id)
                if code == "BACKEND_UNAVAILABLE" or (device.kind != "CPU" and code in {"WORKER_CRASH", "TASK_TIMEOUT"}):
                    with self._lock:
                        self._quarantined.add(device.id)
                    event("DEVICE_QUARANTINED", ident, device.id, code=code)
            # Only audited pure kernels enter this runtime. No external effect is replayed.
            if code in RETRYABLE and attempts[ident] <= task.max_retries:
                state(ident, "PENDING")
                event("RETRY_PENDING", ident, remaining=task.max_retries + 1 - attempts[ident])
            else:
                state(ident, "FAILED")

        with handle._lock:
            handle._state = "RUNNING"
        event("JOB_STARTED")
        with tempfile.TemporaryDirectory(prefix=f"plda-{handle.id[:8]}-") as temp:
            try:
                if handle._cancel.is_set():
                    for ident in tasks:
                        state(ident, "CANCELLED")
                    end_state = "CANCELLED"
                elif plan.host_buffer_bytes > self.config.host_buffer_bytes:
                    for ident in tasks:
                        state(ident, "FAILED")
                    errors["__job__"] = {"code": "HOST_BUDGET_EXCEEDED", "message": "immutable host buffer admission failed"}
                    end_state = "FAILED"
                while end_state is None and any(s not in TERMINAL for s in states.values()):
                    now = time.monotonic()
                    if handle._cancel.is_set() or now - started >= self.config.job_timeout_s:
                        end_state = "CANCELLED" if handle._cancel.is_set() else "TIMED_OUT"
                        for ident in list(active):
                            _stop(active[ident].process)
                            release(ident)
                        for ident, value in list(states.items()):
                            if value not in TERMINAL:
                                state(ident, "CANCELLED")
                        event(end_state)
                        break

                    # Reap before allocating: completed outputs become visible atomically.
                    for ident, attempt in list(active.items()):
                        if attempt.process.is_alive():
                            if now - attempt.started < tasks[ident].timeout_s:
                                continue
                            _stop(attempt.process)
                            release(ident)
                            fail(ident, "TASK_TIMEOUT", "attempt exceeded its wall-clock deadline", attempt.device)
                            continue
                        attempt.process.join()
                        exitcode = attempt.process.exitcode
                        release(ident)
                        if exitcode != 0:
                            fail(ident, "WORKER_CRASH", f"worker exit code {exitcode}", attempt.device)
                            continue
                        try:
                            status = json.loads((attempt.directory / "status.json").read_text(encoding="utf-8"))
                            if status["ok"]:
                                if status["finished_at"] - attempt.started > tasks[ident].timeout_s:
                                    fail(ident, "TASK_TIMEOUT", "result completed after its deadline", attempt.device)
                                    continue
                                output = attempt.directory / "output.bin"
                                if output.stat().st_size != plan.specs[ident].nbytes:
                                    raise ValueError("invalid worker output byte count")
                                value = Tensor(plan.specs[ident], output.read_bytes())
                                store[ident] = value  # One immutable commit after process termination.
                                state(ident, "SUCCEEDED")
                                errors.pop(ident, None)
                                event("COMMITTED", ident, attempt.device.id, **status)
                            else:
                                fail(ident, status["code"], status["message"], attempt.device)
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            fail(ident, "INVALID_RESULT", str(exc), attempt.device)

                    for ident in plan.graph.order:
                        if states[ident] == "PENDING" and any(states[p] in {"FAILED", "SKIPPED", "CANCELLED"} for p in pred[ident]):
                            state(ident, "SKIPPED")
                            errors[ident] = {"code": "DEPENDENCY_FAILED", "message": "required predecessor failed"}
                            event("SKIPPED", ident)

                    ready = [k for k in plan.graph.order if states[k] == "PENDING" and
                             all(states[p] == "SUCCEEDED" for p in pred[k])]
                    ready.sort(key=lambda k: (-plan.priorities[k], plan.graph.order.index(k)))
                    for ident in ready:
                        if handle._cancel.is_set():
                            break
                        task = tasks[ident]
                        needed = reservation_bytes(task, plan.specs)
                        candidates, reasons = [], {}
                        for device in self.devices:
                            eligible, reason = eligibility(task, device, plan.specs)
                            if device.id in self._quarantined:
                                eligible, reason = False, "device quarantined after uncertain execution"
                            if device.id in excluded[ident]:
                                eligible, reason = False, "previous attempt on this device failed"
                            if needed > device.memory_bytes:
                                eligible, reason = False, "task exceeds logical device memory budget"
                            if eligible:
                                candidates.append(device)
                            else:
                                reasons[device.id] = reason
                        if not candidates:
                            state(ident, "FAILED")
                            previous_error = errors.get(ident)
                            errors[ident] = ({**previous_error, "placement_failure": reasons} if previous_error else
                                             {"code": "NO_ELIGIBLE_DEVICE", "message": reasons})
                            event("PLACEMENT_FAILED", ident, reasons=reasons)
                            continue
                        available = [d for d in candidates if slots[d.id] < d.slots and
                                     memory[d.id] + needed <= d.memory_bytes]
                        if not available:
                            continue  # Backpressure; never reinterpret temporary pressure as failure.
                        # Compare an idle placement with waiting for a cheaper busy device.
                        forecasts = {}
                        for d in candidates:
                            delay = 0.0
                            if d not in available:
                                running = [a for a in active.values() if a.device.id == d.id]
                                delay = max((max(0.0, placement_cost_ms(tasks[k], d, plan.specs) -
                                                  (now - a.started) * 1000)
                                             for k, a in active.items() if a in running), default=0)
                            forecasts[d.id] = delay + placement_cost_ms(task, d, plan.specs)
                        best = min(candidates, key=lambda d: (forecasts[d.id], d.id))
                        if best not in available:
                            continue
                        device = best
                        directory = Path(temp) / f"attempt-{uuid.uuid4().hex}"
                        directory.mkdir()
                        process = self._context.Process(target=worker,
                            args=(task, [store[k] for k in task.inputs], device, str(directory)),
                            name=f"plda-{ident[:32]}")
                        slots[device.id] += 1
                        memory[device.id] += needed
                        with handle._lock:
                            attempts[ident] += 1
                        state(ident, "RUNNING")
                        event("RESERVED", ident, device.id, reserved_bytes=needed,
                              used_slots=slots[device.id], used_bytes=memory[device.id],
                              estimated_ms=forecasts[device.id], attempt=attempts[ident])
                        try:
                            launch = time.monotonic()
                            process.start()
                            active[ident] = _Attempt(process, device, launch, needed, directory)
                        except Exception as exc:
                            if process.pid is not None:
                                _stop(process)
                            process.close()
                            slots[device.id] -= 1
                            memory[device.id] -= needed
                            event("RELEASE", ident, device.id, reserved_bytes=needed)
                            fail(ident, "WORKER_CRASH", str(exc), device)
                    if active:
                        handle._cancel.wait(self.config.poll_s)
                    elif any(s == "PENDING" for s in states.values()):
                        # Placement failures may have just made descendants skippable.
                        continue
            finally:
                for ident in list(active):
                    _stop(active[ident].process)
                    release(ident)
        if end_state is None:
            end_state = "FAILED" if any(s in {"FAILED", "SKIPPED"} for s in states.values()) else "SUCCEEDED"
        # Also respect cancellation of a queued zero-task job.
        if handle._cancel.is_set() and end_state == "SUCCEEDED":
            end_state = "CANCELLED"
        with handle._lock:
            handle._state = end_state
        event("JOB_FINISHED", state=end_state)
        return JobResult(handle.id, end_state, {k: store[k] for k in plan.program.outputs if k in store},
                         dict(states), dict(attempts), errors, list(events), time.monotonic() - started)
