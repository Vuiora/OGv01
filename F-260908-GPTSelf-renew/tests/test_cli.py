"""CLI connection selection without live services or real credentials."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from renew import cli
from renew.demo import DemoProvider
from renew.repository import create_snapshot
from renew.runtime import CodexCLIProvider, OpenAIProvider


class ConnectionCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "project"
        self.source.mkdir()
        (self.source / "main.py").write_text("print('hello')\n", encoding="utf-8")
        self.output = self.base / "run"
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        # main reconfigures real streams; StringIO leaves the test runner alone.
        self.start_patch(patch("renew.cli.sys.stdout", self.stdout))
        self.start_patch(patch("renew.cli.sys.stderr", self.stderr))
        self.start_patch(patch.dict(os.environ, {}, clear=True))
        self.http = self.start_patch(patch(
            "renew.runtime.urlopen",
            side_effect=AssertionError("live network forbidden in CLI tests"),
        ))
        self.pipeline = Mock()
        self.pipeline.output = self.output
        self.pipeline.run.return_value = {
            "status": "completed", "workspace": str(self.output / "workspace"),
        }
        self.pipeline_factory = self.start_patch(patch(
            "renew.cli.Pipeline", side_effect=self.capture_pipeline,
        ))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def capture_pipeline(self, config, provider, output, **options):
        self.config, self.provider = config, provider
        self.options = options
        self.assertEqual(output, self.output)
        return self.pipeline

    def write_config(self, data, path=None):
        path = self.base / "settings.json" if path is None else path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_cli(self, *arguments):
        return cli.main([
            "run", str(self.source), "--output", str(self.output), *arguments,
        ])

    def send_mock_request(self):
        self.http.side_effect = None
        self.http.return_value = io.BytesIO(json.dumps({
            "status": "completed", "output": [],
        }).encode("utf-8"))
        self.provider.respond(
            model=self.config.analyst_model, instructions="Inspect the project.",
            input=[], tools=[], max_output_tokens=self.config.max_output_tokens,
        )
        return self.http.call_args.args[0]

    def test_provider_flag_overrides_json_in_both_directions(self):
        cases = (
            ("codex_cli", "openai_api", OpenAIProvider),
            ("openai_api", "codex_cli", CodexCLIProvider),
        )
        for configured, selected, expected_class in cases:
            with self.subTest(provider=selected):
                path = self.write_config({
                    "provider": configured, "api_key": "test-config-key",
                })
                self.assertEqual(self.run_cli(
                    "--config", str(path), "--provider", selected,
                ), 0)
                self.assertIsInstance(self.provider, expected_class)
                self.assertEqual(self.config.provider, selected)
        self.http.assert_not_called()

    def test_endpoint_aliases_and_cli_key_model_override_json(self):
        path = self.write_config({
            "provider": "codex_cli", "base_url": "https://old.example/v1",
            "api_key": "test-old-config-key", "api_key_env": "OLD_SERVICE_KEY",
            "analyst_model": "old-analyst", "planner_model": "old-planner",
            "developer_model": "old-developer", "reviewer_model": "old-reviewer",
        })
        for alias in ("--endpoint", "--base-url"):
            with self.subTest(alias=alias):
                self.assertEqual(self.run_cli(
                    "--config", str(path), "--provider", "openai_api",
                    alias, "https://new.example/gateway/v1/",
                    "--api-key", "test-cli-key", "--model", "new-model",
                ), 0)
                request = self.send_mock_request()
                self.assertEqual(request.full_url, "https://new.example/gateway/v1/responses")
                self.assertEqual(request.get_header("Authorization"), "Bearer test-cli-key")
                self.assertEqual(json.loads(request.data)["model"], "new-model")
                for role in ("analyst", "planner", "developer", "reviewer"):
                    self.assertEqual(getattr(self.config, role + "_model"), "new-model")
                self.assertEqual(self.config.base_url, "https://new.example/gateway/v1")
        self.assertNotIn("test-cli-key", self.stdout.getvalue() + self.stderr.getvalue())
        self.assertNotIn("test-old-config-key", self.stdout.getvalue() + self.stderr.getvalue())

    def test_api_key_env_clears_literal_json_key_and_uses_selected_environment(self):
        path = self.write_config({
            "provider": "openai_api", "api_key": "test-old-config-key",
            "api_key_env": "OLD_SERVICE_KEY",
        })
        with patch.dict(os.environ, {
            "RENEW_SERVICE_KEY": "test-selected-key",
            "OLD_SERVICE_KEY": "test-old-env-key", "OPENAI_API_KEY": "test-default-key",
        }):
            self.assertEqual(self.run_cli(
                "--config", str(path), "--api-key-env", "RENEW_SERVICE_KEY",
            ), 0)
        self.assertIsNone(self.config.api_key)
        self.assertEqual(self.config.api_key_env, "RENEW_SERVICE_KEY")
        request = self.send_mock_request()
        self.assertEqual(request.get_header("Authorization"), "Bearer test-selected-key")

    def test_missing_selected_environment_does_not_fall_back_to_literal_or_openai_key(self):
        path = self.write_config({"api_key": "test-old-config-key"})
        for provider in ("openai_api", "codex_cli"):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                "OPENAI_API_KEY": "test-default-key",
            }):
                self.assertEqual(self.run_cli(
                    "--config", str(path), "--provider", provider,
                    "--api-key-env", "MISSING_RENEW_KEY",
                ), 1)
        self.pipeline_factory.assert_not_called()
        self.http.assert_not_called()
        error = self.stderr.getvalue()
        self.assertIn("MISSING_RENEW_KEY", error)
        self.assertNotIn("test-old-config-key", error)
        self.assertNotIn("test-default-key", error)

    def test_internal_config_is_excluded_from_snapshot_even_when_key_is_overridden(self):
        for filename in ("profiles/local.json", "profiles/[local].json"):
            with self.subTest(filename=filename):
                path = self.write_config({
                    "api_key": "test-private-config-key", "exclude": ["scratch.txt"],
                }, self.source / filename)
                self.assertEqual(self.run_cli(
                    "--config", str(path), "--api-key", "test-override-key",
                ), 0)
                self.assertIn("scratch.txt", self.config.exclude)
                destination = self.base / ("snapshot-" + path.stem)
                with patch("renew.repository._git_files", return_value=None):
                    snapshot = create_snapshot(self.source, destination, tuple(self.config.exclude))
                self.assertNotIn(filename, snapshot["files"])
                self.assertFalse((destination / filename).exists())
                self.assertTrue((destination / "main.py").is_file())
                path.unlink()

    def test_external_config_does_not_exclude_same_named_project_file(self):
        path = self.write_config({"exclude": ["scratch.txt"]})
        (self.source / path.name).write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_cli("--config", str(path)), 0)
        self.assertEqual(self.config.exclude, ["scratch.txt"])

    def test_default_codex_cli_preserves_login_without_reading_ambient_api_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "invalid unused key\n"}):
            self.assertEqual(self.run_cli("--analyze-only"), 0)
        self.assertIsInstance(self.provider, CodexCLIProvider)
        self.assertIsNone(self.provider.base_url)
        self.assertIsNone(self.provider._api_key)
        self.assertFalse(self.options["execute_tests"])
        self.assertTrue(self.options["analyze_only"])
        self.assertFalse(self.options["demo"])
        self.assertIn("本机 codex 登录", self.stdout.getvalue())
        self.http.assert_not_called()

    def test_explicit_default_key_environment_selects_api_auth_for_codex(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-explicit-default-key"}):
            self.assertEqual(self.run_cli("--api-key-env", "OPENAI_API_KEY"), 0)
        self.assertIsInstance(self.provider, CodexCLIProvider)
        self.assertEqual(self.provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(self.provider._api_key, "test-explicit-default-key")
        self.assertIn("本次配置的 endpoint 与 API key", self.stdout.getvalue())
        self.http.assert_not_called()

    def test_explicit_default_key_environment_requires_key_instead_of_login_fallback(self):
        self.assertEqual(self.run_cli("--api-key-env", "OPENAI_API_KEY"), 1)
        self.pipeline_factory.assert_not_called()
        self.assertIn("OPENAI_API_KEY", self.stderr.getvalue())
        self.http.assert_not_called()

    def test_demo_stays_offline_and_enables_its_test_commands(self):
        with patch("renew.cli.CodexCLIProvider") as codex, patch("renew.cli.OpenAIProvider") as api:
            self.assertEqual(cli.main(["demo", "--output", str(self.output)]), 0)
        codex.assert_not_called()
        api.assert_not_called()
        self.http.assert_not_called()
        self.assertIsInstance(self.provider, DemoProvider)
        self.assertTrue(self.config.test_commands)
        self.assertTrue(self.options["demo"])
        self.assertTrue(self.options["execute_tests"])
        self.assertFalse(self.options["analyze_only"])

    def test_literal_and_environment_key_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli("--api-key", "test-key", "--api-key-env", "RENEW_SERVICE_KEY")
        self.assertEqual(raised.exception.code, 2)
        self.pipeline_factory.assert_not_called()
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
