from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from plda import Device, PLDAModule, Program, RuntimeConfig, Task, Tensor, load_program, shard_elementwise
from plda.kernels import scalar_execute


def crash_on_primary_worker(task, inputs, device, destination):
    """Fault injection: real OS process exits without publishing a result."""
    if device.id == "cpu:crash":
        os._exit(23)
    from plda.backends import worker
    worker(task, inputs, device, destination)


def cpu(slots=2, memory=1024**2):
    return Device("cpu:0", "CPU", "cpu", "host", slots=slots, memory_bytes=memory, launch_ms=10)


def diamond():
    return Program({"x": Tensor.from_values([1, 2, 3]), "y": Tensor.from_values([10, 20, 30])}, (
        Task("a", "scale", ("x",), {"factor": 2}),
        Task("b", "scale", ("y",), {"factor": 3}),
        Task("out", "add", ("a", "b"))), ("out",))


class RuntimeTests(unittest.TestCase):
    def test_worker_crash_releases_resources_before_retry(self):
        primary = Device("cpu:crash", "CPU", "cpu", "host", launch_ms=0)
        fallback = replace(cpu(), launch_ms=100)
        program = Program({"x": Tensor.from_values([2])},
                          (Task("out", "identity", ("x",), max_retries=1),), ("out",))
        with patch("plda.runtime.worker", crash_on_primary_worker):
            with PLDAModule([primary, fallback]) as module:
                result = module.run(program)
        self.assertEqual(result.state, "SUCCEEDED", result.errors)
        self.assertEqual(result.outputs["out"].values(), (2,))
        self.assertEqual(result.attempts["out"], 2)
        failures = [e for e in result.events if e.kind == "ATTEMPT_FAILED"]
        self.assertEqual(failures[0].detail["code"], "WORKER_CRASH")
        release = next(e.time_s for e in result.events if e.kind == "RELEASE" and e.device == primary.id)
        retry = next(e.time_s for e in result.events if e.kind == "RESERVED" and e.device == fallback.id)
        self.assertLessEqual(release, retry)

    def test_job_deadline_cancels_without_commit(self):
        with PLDAModule([cpu()], RuntimeConfig(job_timeout_s=0.00001)) as module:
            result = module.run(diamond())
        self.assertEqual(result.state, "TIMED_OUT")
        self.assertEqual(result.outputs, {})

    def test_cancel_queued_job_never_starts_it(self):
        x = Tensor.from_values([1.0] * (200 * 200), (200, 200))
        slow = Program({"x": x}, (Task("out", "matmul", ("x", "x")),), ("out",))
        with PLDAModule([cpu(memory=10**7)]) as module:
            first = module.submit(slow)
            queued = module.submit(diamond())
            self.assertTrue(queued.cancel())
            first.cancel()
            result = queued.result(10)
        self.assertEqual(result.state, "CANCELLED")
        self.assertEqual(sum(result.attempts.values()), 0)

    def test_real_process_parallelism_and_dependency_commit(self):
        with PLDAModule([cpu()]) as module:
            result = module.run(diamond())
        self.assertEqual(result.state, "SUCCEEDED", result.errors)
        self.assertEqual(result.outputs["out"].values(), (32, 64, 96))
        reserved = {e.task: e.time_s for e in result.events if e.kind == "RESERVED"}
        committed = {e.task: e.time_s for e in result.events if e.kind == "COMMITTED"}
        self.assertLess(reserved["a"], committed["b"])
        self.assertLess(reserved["b"], committed["a"])
        self.assertGreater(reserved["out"], max(committed["a"], committed["b"]))

    def test_slot_and_memory_backpressure(self):
        # Each scale needs 96 bytes, so a 96-byte device allows only one at a time.
        with PLDAModule([cpu(slots=4, memory=144)]) as module:
            result = module.run(diamond())
        self.assertEqual(result.state, "SUCCEEDED", result.errors)
        reservations = [e for e in result.events if e.kind == "RESERVED"]
        self.assertTrue(all(e.detail["used_slots"] == 1 for e in reservations))
        self.assertTrue(all(e.detail["used_bytes"] <= 144 for e in reservations))
        self.assertEqual(len(reservations), len([e for e in result.events if e.kind == "RELEASE"]))

    def test_no_device_does_not_hang_and_dependents_skip(self):
        with PLDAModule([cpu(memory=1)]) as module:
            result = module.run(diamond())
        self.assertEqual(result.state, "FAILED")
        self.assertEqual(result.task_states["out"], "SKIPPED")
        self.assertEqual(result.errors["a"]["code"], "NO_ELIGIBLE_DEVICE")

    def test_host_budget_and_invalid_graph_rejected(self):
        with PLDAModule([cpu()], RuntimeConfig(host_buffer_bytes=1)) as module:
            result = module.run(diamond())
            self.assertEqual(result.errors["__job__"]["code"], "HOST_BUDGET_EXCEEDED")
            self.assertEqual(sum(result.attempts.values()), 0)
            with self.assertRaises(ValueError):
                module.submit(replace(diamond(), outputs=("unknown",)))

    def test_missing_accelerator_falls_back_without_fake_acceleration(self):
        gpu = Device("npu:missing", "NPU", "openvino", "NPU.PLDATEST_MISSING", launch_ms=0)
        program = Program({"x": Tensor.from_values([2, 3], dtype="float32")},
            (Task("out", "scale", ("x",), {"factor": 2}, numerical_mode="relaxed",
                  estimated_ms={"NPU": 0.01, "CPU": 100}, max_retries=1),), ("out",))
        with PLDAModule([gpu, cpu()]) as module:
            result = module.run(program)
            self.assertIn(gpu.id, module.capabilities()["quarantined_devices"])
        self.assertEqual(result.state, "SUCCEEDED", result.errors)
        self.assertEqual(result.attempts["out"], 2)
        self.assertEqual(result.outputs["out"].values(), (4, 6))
        commits = [e for e in result.events if e.kind == "COMMITTED"]
        self.assertEqual([e.device for e in commits], ["cpu:0"])

    def test_strict_arithmetic_does_not_route_to_accelerator(self):
        gpu = Device("gpu", "GPU", "cupy", "0")
        with PLDAModule([gpu]) as module:
            result = module.run(diamond())
        self.assertEqual(sum(result.attempts.values()), 0)
        self.assertEqual(result.state, "FAILED")

    def test_real_timeout_stops_worker_and_discards_output(self):
        program = Program({"x": Tensor.from_values([1])},
                          (Task("out", "identity", ("x",), timeout_s=0.0001, max_retries=0),), ("out",))
        with PLDAModule([cpu()]) as module:
            result = module.run(program)
            self.assertEqual(result.errors["out"]["code"], "TASK_TIMEOUT")
            self.assertEqual(result.outputs, {})
            self.assertEqual(len([e for e in result.events if e.kind == "RELEASE"]), 1)
            self.assertEqual(module.run(diamond()).state, "SUCCEEDED")

    def test_cancellation_and_queue_backpressure(self):
        # A large scalar matmul ensures there is an active process to cancel.
        x = Tensor.from_values([1.0] * (250 * 250), (250, 250))
        program = Program({"x": x}, (Task("out", "matmul", ("x", "x")),), ("out",))
        with PLDAModule([cpu(memory=10**7)], RuntimeConfig(max_jobs=1)) as module:
            handle = module.submit(program)
            deadline = time.monotonic() + 5
            while not any(e["kind"] == "RESERVED" for e in handle.status()["events"]) and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaisesRegex(RuntimeError, "queue is full"):
                module.submit(diamond())
            self.assertTrue(handle.cancel())
            result = handle.result(10)
            self.assertEqual(result.state, "CANCELLED")
            self.assertNotIn("out", result.outputs)
            self.assertFalse(handle.cancel())

    def test_exception_skips_dependents_preserves_independent_branch(self):
        # Float32 output cast overflows; invalid execution is nonretryable.
        program = Program({"x": Tensor.from_values([3e38], dtype="float32")}, (
            Task("bad", "scale", ("x",), {"factor": 1e30}),
            Task("good", "identity", ("x",)),
            Task("blocked", "identity", ("bad",))), ("good", "blocked"))
        with PLDAModule([cpu()]) as module:
            result = module.run(program)
        self.assertEqual(result.task_states, {"bad": "FAILED", "good": "SUCCEEDED", "blocked": "SKIPPED"})
        self.assertEqual(result.attempts["bad"], 1)
        self.assertIn("good", result.outputs)

    def test_shards_tail_and_empty_vector(self):
        with PLDAModule([cpu()]) as module:
            for values in (list(range(10)), []):
                program = shard_elementwise("scale", [Tensor.from_values(values)], 4, params={"factor": 2})
                result = module.run(program)
                self.assertEqual(result.state, "SUCCEEDED", result.errors)
                self.assertEqual(result.outputs["result"].values(), tuple(2 * x for x in values))

    def test_snapshot_and_sequential_jobs_share_quota(self):
        program = diamond()
        with PLDAModule([cpu(slots=1)]) as module:
            first = module.submit(program)
            program.tasks[0].params["factor"] = 1000
            second = module.submit(diamond())
            self.assertEqual(first.result(20).outputs["out"].values(), (32, 64, 96))
            self.assertEqual(second.result(20).outputs["out"].values(), (32, 64, 96))

    def test_numpy_matches_scalar_on_builtin_examples(self):
        if importlib.util.find_spec("numpy") is None:
            self.skipTest("NumPy optional")
        from plda.backends import execute
        a = Tensor.from_values([1, -2, 3, 4], (2, 2), "float32")
        b = Tensor.from_values([5, 6, 7, 8], (2, 2), "float32")
        for op, inputs, params in [("add", [a, b], {}), ("multiply", [a, b], {}),
                                   ("matmul", [a, b], {}), ("sum", [a], {}),
                                   ("relu", [a], {}), ("scale", [a], {"factor": 2})]:
            task = Task("out", op, tuple(str(i) for i in range(len(inputs))), params, numerical_mode="relaxed")
            result, mode = execute(task, inputs, cpu())
            self.assertEqual(mode, "numpy-native")
            self.assertEqual(result.values(), scalar_execute(task, inputs).values())

    def test_example_json_end_to_end(self):
        program = load_program(Path(__file__).parents[1] / "examples" / "request.json")
        with PLDAModule([cpu()]) as module:
            result = module.run(program)
        self.assertEqual(result.state, "SUCCEEDED", result.errors)
        self.assertEqual(result.outputs["result"].values(), (32, 64, 96, 128, 160))


if __name__ == "__main__":
    unittest.main()
