"""Configuration validation needs neither credentials nor network access."""
import json
from pathlib import Path
import tempfile
import unittest

from renew.config import Config


class ConfigTests(unittest.TestCase):
    def test_defaults_and_missing_config_are_valid(self):
        self.assertIsInstance(Config.load(None), Config)
        config = Config()
        self.assertIs(config.validate(), config)
        self.assertEqual(config.provider, "codex_cli")
        self.assertEqual(config.codex_command, "codex")
        self.assertEqual(config.test_commands, [])
        self.assertIsNone(config.base_url)
        self.assertIsNone(config.api_key)
        self.assertEqual(config.api_key_env, "OPENAI_API_KEY")

    def test_provider_and_codex_command_are_validated(self):
        for provider in (None, "", "openai", 1):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                Config(provider=provider).validate()
        for command in (None, "", " \t", "codex\x00exec"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                Config(codex_command=command).validate()
        Config(provider="openai_api", codex_command="codex.cmd").validate()

    def test_integer_limits_reject_wrong_types_and_out_of_range_values(self):
        limits = {
            "max_requirements": 10, "max_cycles": 10, "max_attempts": 5,
            "max_turns": 100, "max_output_tokens": 64000,
            "max_api_calls": 10000, "max_total_tokens": 10000000,
            "request_timeout": 600, "test_timeout": 1800,
        }
        for field, upper in limits.items():
            for invalid in (None, True, False, "2", 1.5, 0, -1, upper + 1):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        Config(**{field: invalid}).validate()
            for valid in (1, upper):
                with self.subTest(field=field, valid=valid):
                    Config(**{field: valid}).validate()

    def test_model_names_require_nonempty_strings(self):
        for field in ("analyst_model", "planner_model", "developer_model", "reviewer_model"):
            for invalid in (None, False, 1, "", " \t"):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        Config(**{field: invalid}).validate()

    def test_endpoint_requires_https(self):
        for invalid in (3, "", "http://localhost/v1", "file:///tmp/api",
                        "https://", "https:///v1", "https://user:secret@example.invalid/v1",
                        "https://example.invalid/v1?key=secret", "https://example.invalid/v1#secret",
                        "https://example.invalid:bad/v1", "https://example.invalid:65536/v1",
                        "https://example.invalid:0/v1", "https://example.invalid/v1\n",
                        "https://example.invalid/\x00v1", "https://example.invalid/\x7fv1",
                        "https://example.invalid\\v1", "https://example.invalid/v1/responses"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Config(base_url=invalid).validate()
        self.assertIsNone(Config(base_url=None).validate().base_url)
        self.assertEqual(Config(base_url="https://example.invalid/v1/").validate().base_url,
                         "https://example.invalid/v1")

    def test_credentials_validate_without_echoing_secret(self):
        for key in ("", " \t", 1, True, "secret\x00key", "secret\nkey", "secret key", "密钥"):
            with self.subTest(key=key), self.assertRaises(ValueError) as error:
                Config(api_key=key).validate()
            if isinstance(key, str) and key.strip():
                self.assertNotIn(key, str(error.exception))
        for name in (None, "", 1, "1_KEY", "API-KEY", "API=KEY", "API KEY"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Config(api_key_env=name).validate()
        config = Config(api_key="  test-secret-value  ", api_key_env="PROXY_KEY_2").validate()
        self.assertEqual(config.api_key, "test-secret-value")
        self.assertEqual(config.api_key_env, "PROXY_KEY_2")

    def test_secret_is_redacted_from_serialized_config_and_repr(self):
        config = Config(api_key="test-secret-value", base_url="https://example.invalid/v1").validate()
        serialized = config.to_dict()
        self.assertEqual(serialized["api_key"], "[已脱敏]")
        self.assertNotIn("test-secret-value", json.dumps(serialized))
        self.assertNotIn("test-secret-value", repr(config))
        self.assertEqual(config.api_key, "test-secret-value")
        self.assertEqual(serialized["base_url"], config.base_url)

    def test_load_overrides_are_applied_before_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"base_url": "old-invalid-url", "api_key": "old-test-secret",
                                        "api_key_env": "OLD_KEY", "max_cycles": 3}), encoding="utf-8")
            config = Config.load(path, overrides={"base_url": "https://new.invalid/v1/",
                                                  "api_key": None, "api_key_env": "NEW_KEY"})
            self.assertEqual(config.base_url, "https://new.invalid/v1")
            self.assertIsNone(config.api_key)
            self.assertEqual(config.api_key_env, "NEW_KEY")
            self.assertEqual(config.max_cycles, 3)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["api_key"], "old-test-secret")
        with self.assertRaises(ValueError):
            Config.load(None, overrides={"endpoint_typo": "https://example.invalid/v1"})

    def test_commands_are_argv_lists_and_never_shell_strings(self):
        invalid_commands = (
            None, "python -m unittest", ["python -m unittest"],
            [[]], [["python", 3]], [["python", None]],
            [["", "-m", "unittest"]], [["python", "bad\x00argument"]],
            [("python", "-m", "unittest")],
        )
        for invalid in invalid_commands:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Config(test_commands=invalid).validate()
        config = Config(test_commands=[["python", "-c", "print('literal & value')"]]).validate()
        self.assertEqual(config.test_commands[0][2], "print('literal & value')")

    def test_exclusions_require_string_array(self):
        for invalid in (None, "node_modules", [1], [None]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Config(exclude=invalid).validate()
        Config(exclude=["generated", "vendor/**"]).validate()

    def test_load_rejects_unknown_fields_and_nonobjects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for invalid in ({"max_cycle": 2}, [], None, 1, "config"):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        Config.load(path)

    def test_load_accepts_utf8_bom_and_validates_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"max_cycles": 3, "exclude": ["生成文件"]}), encoding="utf-8-sig")
            config = Config.load(path)
            self.assertEqual(config.max_cycles, 3)
            self.assertEqual(config.exclude, ["生成文件"])
            path.write_text('{"max_cycles": true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                Config.load(path)

    def test_mutable_defaults_and_serialized_values_are_independent(self):
        first, second = Config(), Config()
        first.exclude.append("vendor")
        first.test_commands.append(["python", "-m", "unittest"])
        serialized = first.to_dict()
        serialized["test_commands"][0].append("bad")
        serialized["exclude"].append("cache")
        self.assertEqual(second.exclude, [])
        self.assertEqual(second.test_commands, [])
        self.assertEqual(first.exclude, ["vendor"])
        self.assertEqual(first.test_commands, [["python", "-m", "unittest"]])


if __name__ == "__main__":
    unittest.main()
