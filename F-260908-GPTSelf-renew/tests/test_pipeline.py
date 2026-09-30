"""Offline integration tests using real snapshots, edits and test subprocesses."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from renew.config import Config
from renew.demo import DemoProvider, finding, requirement
from renew.pipeline import (Pipeline, checks_passed, filter_and_order_requirements,
                            order_requirements, review_passed, validate_findings)
from renew.runtime import APIError


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "tiny_project"
TEST_ARGV = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]


def contents(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


class RecordingProvider(DemoProvider):
    def __init__(self):
        super().__init__()
        self.review_inputs = []

    def respond(self, **kwargs):
        if "角色 REVIEWER" in kwargs["instructions"] and len(kwargs["input"]) == 1:
            self.review_inputs.append(json.loads(kwargs["input"][0]["content"]))
        return super().respond(**kwargs)


class IncompleteReviewProvider(DemoProvider):
    def respond(self, **kwargs):
        response = super().respond(**kwargs)
        call = response["output"][0]
        if "角色 REVIEWER" in kwargs["instructions"] and call["name"] == "final_result":
            result = json.loads(call["arguments"])
            result["criteria"].pop()
            call["arguments"] = json.dumps(result)
        return response


class OutOfScopeProvider(DemoProvider):
    """Try to make every developer write target an unassigned file."""
    def __init__(self):
        super().__init__()
        self.tool_errors = []

    def respond(self, **kwargs):
        response = super().respond(**kwargs)
        if "角色 DEVELOPER" in kwargs["instructions"]:
            for item in kwargs["input"]:
                if item.get("type") == "function_call_output":
                    output = json.loads(item["output"])
                    if "error" in output:
                        self.tool_errors.append(output["error"])
            call = response["output"][0]
            if call["name"] == "write_file":
                args = json.loads(call["arguments"])
                args["path"] = "other.py"
                call["arguments"] = json.dumps(args)
        return response


class InterruptedProvider(DemoProvider):
    def respond(self, **kwargs):
        steps = sum(item.get("type") == "function_call" for item in kwargs["input"])
        if "角色 DEVELOPER" in kwargs["instructions"] and steps == 2:
            raise APIError("simulated API interruption")
        return super().respond(**kwargs)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "project"
        shutil.copytree(EXAMPLE, self.source)
        self.original = contents(self.source)
        self.output = self.base / "run"
        # A regression must fail locally instead of making a chargeable request.
        self.no_network = patch("renew.runtime.urlopen", side_effect=AssertionError("network forbidden in offline tests"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def make_pipeline(self, provider=None, *, execute_tests=True, commands=None, **kwargs):
        config = Config(max_attempts=1, test_commands=[TEST_ARGV] if commands is None else commands)
        return Pipeline(config, provider or DemoProvider(), self.output,
                        execute_tests=execute_tests, demo=True, **kwargs)

    def test_demo_full_workflow_verifies_two_requirements_without_changing_source(self):
        provider = RecordingProvider()
        state = self.make_pipeline(provider).run(self.source)
        self.assertEqual(state["status"], "verified")
        self.assertEqual([d["requirement"]["id"] for d in state["deliveries"]], ["R1", "R2"])
        self.assertEqual([d["status"] for d in state["deliveries"]], ["verified", "verified"])
        self.assertEqual(contents(self.source), self.original)
        self.assertEqual(state["usage"]["calls"], 0)
        self.assertGreater(state["usage"]["simulated_calls"], 0)
        for delivery in state["deliveries"]:
            checks = delivery["attempts"][-1]["checks"]
            self.assertTrue(checks_passed(checks))
            self.assertRegex(checks[0]["stdout"] + checks[0]["stderr"], r"Ran [1-9][0-9]* tests")
        workspace = Path(state["workspace"])
        probe = subprocess.run([sys.executable, "-c",
            "from stats import average, summarize; "
            "assert average([-4, 2]) == -1; "
            "assert summarize([-2]) == {'count': 1, 'average': -2, 'min': -2, 'max': -2}; "
            "print(summarize([1, 2, 3]))"], cwd=workspace, capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("'count': 3", probe.stdout)
        self.assertIn("+def summarize", (self.output / "changes.patch").read_text(encoding="utf-8"))
        if shutil.which("git"):
            applied = subprocess.run(["git", "-C", str(self.output / "source"), "apply", "--check",
                                      str(self.output / "changes.patch")], capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue((self.output / "report.md").is_file())
        self.assertTrue((self.output / "events.jsonl").is_file())
        persisted = json.loads((self.output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "verified")
        self.assertEqual(len(persisted["deliveries"]), 2)

    def test_later_review_must_preserve_previously_accepted_criteria(self):
        provider = RecordingProvider()
        state = self.make_pipeline(provider, execute_tests=False).run(self.source)
        self.assertEqual(state["status"], "implemented_unverified")
        self.assertEqual(len(provider.review_inputs), 2)
        expected = set(requirement("fix")["acceptance_criteria"] + requirement("innovation")["acceptance_criteria"])
        second_review = provider.review_inputs[1]
        self.assertEqual(set(second_review["requirement"]["acceptance_criteria"]), expected)
        self.assertTrue(second_review["preserved_requirements"])

    def test_without_execution_approval_implementation_remains_unverified(self):
        with patch("renew.pipeline.run_checks", side_effect=AssertionError("tests were not enabled")):
            state = self.make_pipeline(execute_tests=False).run(self.source)
        self.assertEqual(state["status"], "implemented_unverified")
        self.assertTrue(all(d["status"] == "implemented_unverified" for d in state["deliveries"]))
        self.assertTrue(all(not d["attempts"][-1]["checks"] for d in state["deliveries"]))
        self.assertEqual(contents(self.source), self.original)

    def test_enabled_execution_with_no_commands_is_unverified(self):
        state = self.make_pipeline(commands=[]).run(self.source)
        self.assertEqual(state["status"], "implemented_unverified")
        self.assertEqual([d["status"] for d in state["deliveries"]], ["implemented_unverified"] * 2)

    def test_analyze_only_writes_requirements_without_developing(self):
        state = self.make_pipeline(execute_tests=False, analyze_only=True).run(self.source)
        self.assertEqual(state["status"], "analyzed")
        self.assertEqual(state["deliveries"], [])
        self.assertEqual(len(state["cycles"][0]["plan"]["requirements"]), 2)
        self.assertEqual(contents(Path(state["workspace"])), self.original)

    def test_connection_key_is_not_written_to_run_artifacts(self):
        config = Config(base_url="https://example.invalid/v1", api_key="private-test-key",
                        api_key_env="PROXY_KEY")
        pipeline = Pipeline(config, DemoProvider(), self.output, analyze_only=True, demo=True)
        state = pipeline.run(self.source)
        self.assertEqual(state["status"], "analyzed")
        persisted = json.loads((self.output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["config"]["base_url"], config.base_url)
        self.assertEqual(persisted["config"]["api_key_env"], "PROXY_KEY")
        self.assertEqual(persisted["config"]["api_key"], "[已脱敏]")
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-test-key", path.read_bytes(), str(path))

    def test_missing_review_criterion_rejects_delivery_and_blocks_dependency(self):
        state = self.make_pipeline(IncompleteReviewProvider(), execute_tests=False).run(self.source)
        self.assertEqual(state["status"], "needs_attention")
        self.assertEqual([d["status"] for d in state["deliveries"]], ["rejected", "blocked_dependency"])
        self.assertEqual(contents(Path(state["workspace"])), self.original)

    def test_developer_cannot_write_outside_requirement_scope(self):
        provider = OutOfScopeProvider()
        state = self.make_pipeline(provider, execute_tests=False).run(self.source)
        self.assertTrue(provider.tool_errors)
        self.assertTrue(any("allowed_files" in message for message in provider.tool_errors))
        self.assertEqual(state["deliveries"][0]["status"], "rejected")
        self.assertFalse(any(self.output.rglob("other.py")))
        self.assertEqual(contents(Path(state["workspace"])), self.original)

    def test_codex_candidate_scope_is_checked_before_integration(self):
        class OutOfScopeCandidateProvider(DemoProvider):
            def respond(self, **kwargs):
                response = super().respond(**kwargs)
                if "角色 DEVELOPER" in kwargs["instructions"]:
                    for item in kwargs["input"]:
                        if item.get("type") == "function_call_output":
                            continue
                    call = response["output"][0]
                    if call["name"] == "final_result":
                        return response
                return response

        pipeline = self.make_pipeline(OutOfScopeCandidateProvider(), execute_tests=False)
        original_agent = pipeline.agent

        def fake_agent(name, instructions, prompt, root, schema, **kwargs):
            result = original_agent(name, instructions, prompt, root, schema, **kwargs)
            if name == "developer":
                (Path(root) / "other.py").write_text("out of scope", encoding="utf-8")
            return result

        pipeline.agent = fake_agent
        state = pipeline.run(self.source)
        self.assertEqual(state["deliveries"][0]["status"], "rejected")
        self.assertFalse((self.output / "workspace" / "other.py").exists())

    def test_api_failure_preserves_current_delivery_and_candidate(self):
        pipeline = self.make_pipeline(InterruptedProvider(), execute_tests=False)
        with self.assertRaisesRegex(APIError, "simulated API interruption"):
            pipeline.run(self.source)
        state = json.loads((self.output / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(state["deliveries"]), 1)
        delivery = state["deliveries"][0]
        self.assertEqual(delivery["requirement"]["id"], "R1")
        self.assertIn("raise ValueError", (Path(delivery["candidate"]) / "stats.py").read_text(encoding="utf-8"))
        self.assertTrue((self.output / "report.md").is_file())
        self.assertEqual(contents(Path(state["workspace"])), self.original)
        self.assertEqual(contents(self.source), self.original)

    def test_test_process_side_effects_never_enter_delivery(self):
        script = (
            "import pathlib,subprocess,sys; "
            "result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v']); "
            "pathlib.Path('stats.py').write_text('# test mutated an allowed file\\n'); "
            "pathlib.Path('test-artifact.txt').write_text('test artifact'); "
            "sys.exit(result.returncode)"
        )
        state = self.make_pipeline(commands=[[sys.executable, "-c", script]]).run(self.source)
        self.assertEqual(state["status"], "needs_attention")
        self.assertEqual(state["deliveries"][0]["status"], "rejected")
        checks = state["deliveries"][0]["attempts"][0]["checks"]
        self.assertEqual(checks[0]["exit_code"], 0)
        self.assertFalse(checks_passed(checks))
        for directory in [Path(state["workspace"])] + [Path(d["candidate"]) for d in state["deliveries"] if "candidate" in d]:
            self.assertFalse((directory / "test-artifact.txt").exists())
            self.assertIn("def average", (directory / "stats.py").read_text(encoding="utf-8"))
            self.assertNotIn("test mutated", (directory / "stats.py").read_text(encoding="utf-8"))
        self.assertEqual(contents(self.source), self.original)

    def test_new_test_artifacts_are_discarded_without_blocking_valid_changes(self):
        script = (
            "import pathlib,subprocess,sys; "
            "result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v']); "
            "pathlib.Path('test-artifact.txt').write_text('test artifact'); "
            "sys.exit(result.returncode)"
        )
        state = self.make_pipeline(commands=[[sys.executable, "-c", script]]).run(self.source)
        self.assertEqual(state["status"], "verified")
        for directory in [Path(state["workspace"])] + [Path(d["candidate"]) for d in state["deliveries"]]:
            self.assertFalse((directory / "test-artifact.txt").exists())
        self.assertEqual(contents(self.source), self.original)

    def test_zero_collected_tests_cannot_verify_candidate(self):
        state = self.make_pipeline(commands=[[sys.executable, "-c", "print('Ran 0 tests'); print('OK')"]]).run(self.source)
        self.assertEqual(state["deliveries"][0]["status"], "rejected")
        self.assertEqual(state["status"], "needs_attention")
        self.assertEqual(contents(Path(state["workspace"])), self.original)

    def test_existing_output_and_nested_output_are_rejected(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("existing result", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.make_pipeline().run(self.source)
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "existing result")
        nested = Pipeline(Config(), DemoProvider(), self.source / "results", demo=True)
        with self.assertRaises(ValueError):
            nested.run(self.source)
        self.assertFalse((self.source / "results").exists())


class EvidenceAndRequirementsTests(unittest.TestCase):
    def test_exact_quote_and_line_required(self):
        valid, rejected = validate_findings([finding("fix")], EXAMPLE)
        self.assertEqual(len(valid), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(valid[0]["evidence_status"], "quote_verified_not_behavior_proven")
        for path, line, quote in (
            ("stats.py", 1, "return sum(numbers) / len(numbers)"),
            ("stats.py", 6, "return 42"), ("stats.py", 999, "return"),
            ("stats.py", 0, "return"), ("missing.py", 6, "return"),
            ("../stats.py", 6, "return"), ("stats.py", 6, " "),
        ):
            candidate = finding("fix")
            candidate["evidence"] = [{"path": path, "line": line, "quote": quote}]
            with self.subTest(path=path, line=line, quote=quote):
                valid, rejected = validate_findings([candidate], EXAMPLE)
                self.assertEqual(valid, [])
                self.assertEqual(len(rejected), 1)
                self.assertTrue(rejected[0]["reasons"])

    def test_innovation_without_hypothesis_is_rejected(self):
        candidate = finding("innovation")
        candidate["hypothesis"] = " "
        valid, rejected = validate_findings([candidate], EXAMPLE)
        self.assertFalse(valid)
        self.assertTrue(rejected)

    def test_dependencies_order_before_higher_priority_dependents(self):
        findings, _ = validate_findings([finding("fix"), finding("innovation")], EXAMPLE)
        for item in findings:
            item["priority_score"] = 100 if item["kind"] == "innovation" else 1
        ordered = order_requirements([requirement("innovation"), requirement("fix")], findings)
        self.assertEqual([item["id"] for item in ordered], ["R1", "R2"])

    def test_dependency_cycle_unknown_dependency_and_unknown_finding_rejected(self):
        findings, _ = validate_findings([finding("fix"), finding("innovation")], EXAMPLE)
        cycle = [requirement("fix"), requirement("innovation")]
        cycle[0]["dependencies"] = ["R2"]
        unknown_dependency = [requirement("fix")]
        unknown_dependency[0]["dependencies"] = ["R999"]
        unknown_finding = [requirement("fix")]
        unknown_finding[0]["finding_ids"] = ["A999"]
        duplicate = [requirement("fix"), requirement("fix")]
        for invalid in (cycle, unknown_dependency, unknown_finding, duplicate):
            with self.subTest(requirements=invalid):
                with self.assertRaises(ValueError):
                    order_requirements(invalid, findings)

    def test_unsafe_scope_and_duplicate_acceptance_criteria_rejected(self):
        findings, _ = validate_findings([finding("fix")], EXAMPLE)
        for path in ("../outside.py", "C:/absolute.py", "src/*.py", "src/../file.py"):
            req = requirement("fix")
            req["allowed_files"] = [path]
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    order_requirements([req], findings)
        req = requirement("fix")
        req["acceptance_criteria"] = ["same", "same"]
        with self.assertRaises(ValueError):
            order_requirements([req], findings)

    def test_invalid_planner_requirement_does_not_discard_independent_valid_one(self):
        findings, _ = validate_findings([finding("fix"), finding("innovation")], EXAMPLE)
        invalid = requirement("fix")
        invalid["finding_ids"] = ["A1", "I1"]
        valid = requirement("innovation")
        valid["dependencies"] = []
        ordered, rejected = filter_and_order_requirements([invalid, valid], findings)
        self.assertEqual([req["id"] for req in ordered], ["R2"])
        self.assertEqual(len(rejected), 1)
        self.assertIn("类型", rejected[0]["reason"])

    def test_requirement_depending_on_rejected_requirement_is_also_rejected(self):
        findings, _ = validate_findings([finding("fix"), finding("innovation")], EXAMPLE)
        invalid = requirement("fix")
        invalid["finding_ids"] = ["I1"]
        dependent = requirement("innovation")
        ordered, rejected = filter_and_order_requirements([invalid, dependent], findings)
        self.assertEqual(ordered, [])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(any("依赖" in item["reason"] for item in rejected))

    def test_review_requires_exactly_all_criteria_evidence_and_no_issues(self):
        req = requirement("fix")
        review = {"approved": True, "issues": [], "criteria": [
            {"criterion": text, "passed": True, "evidence": "stats.py has the required behavior"}
            for text in req["acceptance_criteria"]]}
        self.assertTrue(review_passed(review, req))
        variants = []
        missing = copy.deepcopy(review)
        missing["criteria"].pop()
        variants.append(missing)
        duplicated = copy.deepcopy(review)
        duplicated["criteria"].append(copy.deepcopy(duplicated["criteria"][0]))
        variants.append(duplicated)
        no_evidence = copy.deepcopy(review)
        no_evidence["criteria"][0]["evidence"] = " "
        variants.append(no_evidence)
        not_passed = copy.deepcopy(review)
        not_passed["criteria"][0]["passed"] = False
        variants.append(not_passed)
        issues = copy.deepcopy(review)
        issues["issues"] = ["regression found"]
        variants.append(issues)
        for invalid in variants:
            with self.subTest(review=invalid):
                self.assertFalse(review_passed(invalid, req))

    def test_check_results_require_success_and_nonempty_test_collection(self):
        successful = {"exit_code": 0, "timed_out": False, "stdout": "", "stderr": "Ran 6 tests\nOK"}
        self.assertTrue(checks_passed([successful]))
        for invalid in ([], [{**successful, "exit_code": 1}], [{**successful, "timed_out": True}],
                        [{**successful, "stderr": "Ran 0 tests\nOK"}],
                        [{**successful, "stdout": "collected 0 items"}],
                        [{**successful, "stdout": "no tests ran"}]):
            with self.subTest(checks=invalid):
                self.assertFalse(checks_passed(invalid))


if __name__ == "__main__":
    unittest.main()
