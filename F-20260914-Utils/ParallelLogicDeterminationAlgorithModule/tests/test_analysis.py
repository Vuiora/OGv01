from dataclasses import replace
import random
import unittest

from plda import (Access, AffineAccess, Instruction, Loop, MachineModel, Program, Task, Tensor,
                  analyze_ilp, analyze_simd, analyze_tlp, prepare, program_from_dict)


class AnalysisTests(unittest.TestCase):
    def test_three_dependency_kinds_and_disjoint_slices(self):
        nodes = (
            Instruction("a", (Access("x", "write", 0, 4),)),
            Instruction("b", (Access("x", "read", 0, 4),)),
            Instruction("c", (Access("x", "write", 0, 4),)),
            Instruction("d", (Access("x", "write", 4, 8),)),
        )
        result = analyze_tlp(nodes)
        edges = {(e.source, e.target): e.reasons for e in result.edges}
        self.assertEqual(set(edges), {("a", "b"), ("a", "c"), ("b", "c")})
        self.assertTrue(edges["a", "b"][0].startswith("RAW"))
        self.assertTrue(edges["a", "c"][0].startswith("WAW"))
        self.assertTrue(edges["b", "c"][0].startswith("WAR"))
        self.assertIn("d", result.levels[0])

    def test_unknown_alias_does_not_use_relative_offsets(self):
        result = analyze_tlp((Instruction("a", (Access(None, "write", 0, 4),)),
                              Instruction("b", (Access("x", "read", 100, 104),))))
        self.assertEqual(len(result.edges), 1)

    def test_unknown_effect_and_barrier_serialize(self):
        nodes = (Instruction("a"), Instruction("b", effects_complete=False), Instruction("c"))
        self.assertEqual(analyze_tlp(nodes).levels, [["a"], ["b"], ["c"]])
        nodes = (Instruction("a"), Instruction("b", barrier=True), Instruction("c"))
        self.assertEqual(analyze_tlp(nodes).levels, [["a"], ["b"], ["c"]])

    def test_cycle_and_invalid_cost_rejected(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            analyze_tlp((Instruction("a", depends_on=("b",)), Instruction("b", depends_on=("a",))))
        with self.assertRaises(ValueError):
            analyze_tlp((Instruction("a"),), {"a": -1})

    def test_ilp_latency_ports_issue_width_and_critical_path(self):
        instructions = (Instruction("a", latency=3), Instruction("b", latency=2),
                        Instruction("c", depends_on=("a", "b")))
        result = analyze_ilp(instructions, MachineModel(2, {"alu": 2}))
        self.assertEqual(result["cycles"], 4)
        self.assertEqual(result["same_cycle_groups"][0], ["a", "b"])
        self.assertEqual(result["graph"].span, 4)
        limited = analyze_ilp((Instruction("a", latency=1, occupancy=3), Instruction("b")),
                              MachineModel(2, {"alu": 1}))
        self.assertEqual(limited["schedule"][1]["issue"], 3)

    def test_ilp_keeps_war_and_waw(self):
        result = analyze_ilp((Instruction("a", (Access("r1", "read"),)),
                              Instruction("b", (Access("r1", "write"),)),
                              Instruction("c", (Access("r1", "write"),))))
        self.assertEqual(result["cycles"], 3)

    def test_simd_safe_large_loop_and_tail(self):
        loop = Loop("inplace", 1_000_003, (AffineAccess("a", "read", 4), AffineAccess("a", "write", 4)))
        result = analyze_simd(loop)
        self.assertEqual(result["verdict"], "safe")
        self.assertEqual(result["scalar_tail"], 3)

    def test_simd_recurrence_witness(self):
        loop = Loop("recurrence", 10, (AffineAccess("a", "read", 4), AffineAccess("a", "write", 4, 4)))
        result = analyze_simd(loop)
        self.assertEqual(result["verdict"], "unsafe")
        self.assertNotEqual(*result["witness"]["iterations"])

    def test_simd_unknown_and_budget(self):
        loop = Loop("indirect", 10, (AffineAccess(None, "write", 4),))
        self.assertEqual(analyze_simd(loop)["verdict"], "unknown")
        loop = Loop("large", 1_000_000, (AffineAccess("a", "read", 5), AffineAccess("a", "write", 7)))
        self.assertEqual(analyze_simd(loop)["verdict"], "unknown")

    def test_simd_reduction_requires_algebra_and_float_permission(self):
        loop = Loop("reduce", 16, (AffineAccess("a", "read", 4),), reduction="sum")
        self.assertEqual(analyze_simd(loop)["verdict"], "unsafe")
        self.assertEqual(analyze_simd(replace(loop, allow_reassociation=True))["verdict"], "safe")
        self.assertEqual(analyze_simd(replace(loop, dtype="uint64_mod"))["verdict"], "safe")
        self.assertEqual(analyze_simd(replace(loop, reduction="custom"))["verdict"], "unknown")

    def test_simd_matches_exhaustive_oracle_for_300_affine_loops(self):
        rng = random.Random(83)
        for case in range(300):
            n = rng.randint(2, 8)
            accesses = tuple(AffineAccess(rng.choice(["a", "b"]), rng.choice(["read", "write"]),
                                          rng.randint(-4, 6), 32 + rng.randint(0, 5), rng.randint(1, 4))
                             for _ in range(rng.randint(1, 4)))
            conflict = False
            for i in range(n):
                for j in range(i + 1, n):
                    for a in accesses:
                        for b in accesses:
                            if a.resource != b.resource or "write" not in {a.mode, b.mode}:
                                continue
                            start_a, start_b = a.stride * i + a.offset, b.stride * j + b.offset
                            conflict |= start_a < start_b + b.width and start_b < start_a + a.width
            result = analyze_simd(Loop(str(case), n, accesses))
            self.assertEqual(result["verdict"], "unsafe" if conflict else "safe", (case, accesses, result))

    def test_task_graph_shapes_and_ssa(self):
        program = Program({"x": Tensor.from_values([1, 2])}, (
            Task("a", "scale", ("x",), {"factor": 2}),
            Task("b", "scale", ("x",), {"factor": 3}),
            Task("out", "add", ("a", "b"))), ("out",))
        self.assertEqual(prepare(program).graph.levels, [["a", "b"], ["out"]])
        with self.assertRaises(ValueError):
            prepare(replace(program, tasks=(Task("x", "identity", ("x",)),)))
        with self.assertRaises(ValueError):
            prepare(replace(program, tasks=(Task("a", "identity", ("missing",)),)))

    def test_request_boundary_and_raw_normalization(self):
        value = {"version": 1, "source_kind": "raw", "inputs": {"x": [1, 2]}, "tasks": [], "outputs": ["x"]}
        self.assertEqual(prepare(program_from_dict(value)).host_buffer_bytes, 16)
        with self.assertRaises(ValueError):
            program_from_dict({**value, "script": "arbitrary code"})


if __name__ == "__main__":
    unittest.main()
