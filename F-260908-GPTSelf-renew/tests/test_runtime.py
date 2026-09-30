from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from renew.runtime import (
    APIError,
    AgentRunner,
    BudgetExceeded,
    OpenAIProvider,
    CodexCLIProvider,
    ProtocolError,
    validate_schema,
)


FINAL_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "minLength": 1}},
    "required": ["summary"],
    "additionalProperties": False,
}
READ_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}
TOOLS = [{"type": "function", "name": "read_file", "parameters": READ_SCHEMA, "strict": True}]


def call(name="final_result", args=None, call_id="call-1"):
    return {
        "type": "function_call", "id": "fc-" + call_id, "call_id": call_id,
        "name": name, "arguments": json.dumps(args if args is not None else {"summary": "done"}),
        "status": "completed",
    }


def response(*output, status="completed", usage=None):
    result = {"id": "resp-test", "status": status, "output": list(output)}
    if usage is not None:
        result["usage"] = usage
    return result


class FakeProvider:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def respond(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return next(self.responses)


class AgentRunnerTests(unittest.TestCase):
    def run_agent(self, provider, dispatch=None, **kwargs):
        return AgentRunner(provider, model="test-model", **kwargs).run(
            "developer", "Inspect and improve this repository.", TOOLS,
            dispatch if dispatch is not None else Mock(return_value={"content": "hello"}),
            FINAL_SCHEMA,
        )

    def test_multiple_turns_keep_all_reasoning_and_match_call_output(self):
        reasoning = {
            "type": "reasoning", "id": "rs-test", "summary": [],
            "encrypted_content": "opaque-reasoning",
        }
        read = call("read_file", {"path": "main.py"})
        provider = FakeProvider(response(reasoning, read), response(call(call_id="call-2")))
        dispatch = Mock(return_value={"content": "print('hello')"})
        self.assertEqual(self.run_agent(provider, dispatch), {"summary": "done"})
        dispatch.assert_called_once_with("read_file", {"path": "main.py"})
        next_input = provider.requests[1]["input"]
        self.assertEqual(next_input[1], reasoning)
        self.assertEqual(next_input[2], read)
        self.assertEqual(next_input[3]["call_id"], "call-1")
        self.assertEqual(json.loads(next_input[3]["output"]), {"content": "print('hello')"})
        self.assertEqual(provider.requests[0]["tools"][-1]["parameters"], FINAL_SCHEMA)
        self.assertTrue(provider.requests[0]["tools"][-1]["strict"])

    def test_each_run_has_independent_context(self):
        provider = FakeProvider(response(call()), response(call()))
        runner = AgentRunner(provider, model="test-model")
        for prompt in ("first", "second"):
            runner.run("analyst", prompt, [], Mock(), FINAL_SCHEMA)
        self.assertEqual(provider.requests[1]["input"], [{"role": "user", "content": "second"}])

    def test_tool_exception_is_returned_and_model_can_recover(self):
        provider = FakeProvider(
            response(call("read_file", {"path": "gone.py"})),
            response(call(call_id="call-2")),
        )
        self.run_agent(provider, Mock(side_effect=FileNotFoundError("file does not exist")))
        feedback = json.loads(provider.requests[1]["input"][-1]["output"])
        self.assertFalse(feedback["ok"])
        self.assertIn("file does not exist", feedback["error"])

    def test_keyboard_interrupt_is_not_swallowed(self):
        provider = FakeProvider(response(call("read_file", {"path": "main.py"})))
        with self.assertRaises(KeyboardInterrupt):
            self.run_agent(provider, Mock(side_effect=KeyboardInterrupt()))

    def test_invalid_final_schema_retries_instead_of_succeeding(self):
        provider = FakeProvider(response(call(args={"wrong": "field"})), response(call(call_id="call-2")))
        self.assertEqual(self.run_agent(provider), {"summary": "done"})
        self.assertFalse(json.loads(provider.requests[1]["input"][-1]["output"])["ok"])

    def test_unknown_tool_and_bad_arguments_do_not_dispatch(self):
        for tool_call in (
            call("delete_everything", {}),
            call("read_file", {"path": "a", "escape": True}),
            {**call("read_file"), "arguments": "{invalid"},
            {**call("read_file"), "arguments": '{"path": NaN}'},
        ):
            with self.subTest(tool_call=tool_call):
                provider = FakeProvider(response(tool_call), response(call(call_id="call-2")))
                dispatch = Mock()
                self.run_agent(provider, dispatch)
                dispatch.assert_not_called()
                self.assertFalse(json.loads(provider.requests[1]["input"][-1]["output"])["ok"])

    def test_text_only_and_empty_responses_exhaust_turn_limit(self):
        for item in (None, {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}):
            with self.subTest(item=item):
                outputs = () if item is None else (item,)
                provider = FakeProvider(response(*outputs), response(*outputs))
                with self.assertRaisesRegex(ProtocolError, "2 轮上限"):
                    self.run_agent(provider, max_turns=2)
                self.assertEqual(len(provider.requests), 2)

    def test_incomplete_refusal_and_failed_responses_are_not_success(self):
        for result in (
            response(call(), status="incomplete"),
            response(call(), status="failed"),
            response({"type": "message", "content": [{"type": "refusal", "refusal": "Cannot comply"}]}),
        ):
            with self.subTest(result=result):
                with self.assertRaises(ProtocolError):
                    self.run_agent(FakeProvider(result))

    def test_replayed_call_id_is_blocked_before_dispatch(self):
        provider = FakeProvider(
            response(call("read_file", {"path": "main.py"})),
            response(call("read_file", {"path": "main.py"})),
        )
        dispatch = Mock(return_value={})
        with self.assertRaisesRegex(ProtocolError, "重复"):
            self.run_agent(provider, dispatch)
        dispatch.assert_called_once()

    def test_final_result_must_be_only_call_in_response(self):
        provider = FakeProvider(
            response(call(), call("read_file", {"path": "main.py"}, "call-2")),
            response(call(call_id="call-3")),
        )
        dispatch = Mock()
        self.run_agent(provider, dispatch)
        dispatch.assert_not_called()
        feedback = [item for item in provider.requests[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(len(feedback), 2)
        self.assertTrue(all(not json.loads(item["output"])["ok"] for item in feedback))

    def test_events_do_not_include_tool_arguments_or_contents(self):
        events = Mock()
        provider = FakeProvider(
            response(call("read_file", {"path": "private-value"})),
            response(call(call_id="call-2")),
        )
        self.run_agent(provider, event=events)
        self.assertNotIn("private-value", str(events.call_args_list))
        self.assertEqual(events.call_count, 2)
        self.assertEqual(events.call_args_list[0].args, (
            "developer", {"type": "tool_call", "name": "read_file", "turn": 1},
        ))


class SchemaTests(unittest.TestCase):
    def test_nested_objects_arrays_enum_and_bounds(self):
        schema = {
            "type": "object", "required": ["items"], "additionalProperties": False,
            "properties": {"items": {
                "type": "array", "minItems": 1, "maxItems": 2,
                "items": {"type": "integer", "minimum": 1, "maximum": 3, "enum": [1, 2, 3]},
            }},
        }
        validate_schema({"items": [1, 3]}, schema)
        for value in ({"items": []}, {"items": [4]}, {"items": [True]}, {"items": [1], "extra": 2}, {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_schema(value, schema)

    def test_nullable_and_combinator_support(self):
        validate_schema(None, {"type": ["string", "null"]})
        validate_schema(3, {"anyOf": [{"type": "string"}, {"type": "integer"}]})
        with self.assertRaises(ValueError):
            validate_schema(True, {"anyOf": [{"type": "string"}, {"type": "integer"}]})
        with self.assertRaises(ValueError):
            validate_schema(float("inf"), {"type": "number"})


class ProviderTests(unittest.TestCase):
    @staticmethod
    def send(provider):
        return provider.respond(model="test-model", instructions="test", input=[], tools=[], max_output_tokens=100)

    @staticmethod
    def raw_response(usage=None, status="completed"):
        return io.BytesIO(json.dumps(response(call(), usage=usage, status=status)).encode())

    def test_missing_key_has_clear_error(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}), self.assertRaisesRegex(APIError, "OPENAI_API_KEY"):
            OpenAIProvider()

    def test_api_connections_use_the_selected_endpoint_and_key_without_crossing(self):
        environment = {"OPENAI_API_KEY": "default-test-key", "FIRST_KEY": "first-test-key",
                       "SECOND_KEY": "second-test-key"}
        with patch.dict("os.environ", environment, clear=True):
            providers = [
                OpenAIProvider(base_url="https://first.example.test/v1///", api_key_env="FIRST_KEY"),
                OpenAIProvider(base_url="https://second.example.test/api/v1", api_key_env="SECOND_KEY"),
                OpenAIProvider(api_key="explicit-test-key", api_key_env="FIRST_KEY"),
            ]
            original_environment = dict(os.environ)
            with patch("renew.runtime.urlopen", side_effect=lambda *a, **kw: self.raw_response()) as opener:
                for provider in providers:
                    self.send(provider)
            requests = [entry.args[0] for entry in opener.call_args_list]
            self.assertEqual([request.full_url for request in requests], [
                "https://first.example.test/v1/responses",
                "https://second.example.test/api/v1/responses",
                "https://api.openai.com/v1/responses",
            ])
            self.assertEqual([request.get_header("Authorization") for request in requests], [
                "Bearer first-test-key", "Bearer second-test-key", "Bearer explicit-test-key",
            ])
            self.assertEqual(dict(os.environ), original_environment)

    def test_selected_key_environment_never_falls_back_to_another_key(self):
        environment = {"OPENAI_API_KEY": "wrong-default-test-key", "SELF_RENEW_API_KEY": "stale-test-key"}
        with patch.dict("os.environ", environment, clear=True), patch("renew.runtime.subprocess.run") as run:
            for provider_type in (OpenAIProvider, CodexCLIProvider):
                with self.subTest(provider=provider_type.__name__):
                    with self.assertRaisesRegex(APIError, "MISSING_KEY"):
                        provider_type(api_key_env="MISSING_KEY")
            run.assert_not_called()

    def test_custom_endpoint_without_key_fails_before_starting_codex(self):
        with patch.dict("os.environ", {}, clear=True), patch("renew.runtime.subprocess.run") as run:
            with self.assertRaisesRegex(APIError, "OPENAI_API_KEY"):
                CodexCLIProvider(base_url="https://endpoint.example.test/v1")
            run.assert_not_called()

    def test_both_providers_reject_unsafe_connection_values(self):
        for provider_type in (OpenAIProvider, CodexCLIProvider):
            for options in (
                {"base_url": "http://endpoint.example.test/v1", "api_key": "test-key"},
                {"base_url": "https://user:password@endpoint.example.test/v1", "api_key": "test-key"},
                {"base_url": "https://endpoint.example.test/v1?key=secret", "api_key": "test-key"},
                {"api_key": "test-key\nAuthorization: injected"},
                {"api_key": "test-key", "api_key_env": "INVALID=KEY"},
            ):
                with self.subTest(provider=provider_type.__name__, options=options), self.assertRaises(ValueError):
                    provider_type(**options)

    def test_request_uses_stateless_responses_and_usage_snapshot_is_copy(self):
        provider = OpenAIProvider(api_key="fake-test-key")
        usage = {"input_tokens": 5, "output_tokens": 8, "total_tokens": 13}
        with patch("renew.runtime.urlopen", return_value=self.raw_response(usage)) as opener:
            self.send(provider)
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertIn("reasoning.encrypted_content", payload["include"])
        self.assertEqual(provider.usage, {"calls": 1, **usage})
        snapshot = provider.snapshot()
        snapshot["calls"] = 100
        self.assertEqual(provider.usage["calls"], 1)

    def test_request_budget_blocks_following_call(self):
        provider = OpenAIProvider(api_key="fake-test-key", max_calls=1)
        with patch("renew.runtime.urlopen", return_value=self.raw_response()) as opener:
            self.send(provider)
            with self.assertRaises(BudgetExceeded):
                self.send(provider)
        self.assertEqual(opener.call_count, 1)

    def test_retry_attempts_count_toward_budget(self):
        provider = OpenAIProvider(api_key="fake-test-key", max_calls=2)
        errors = [HTTPError("https://example.invalid", 429, "secret response", {}, io.BytesIO()) for _ in range(2)]
        with patch("renew.runtime.urlopen", side_effect=errors) as opener, patch("renew.runtime.time.sleep"):
            with self.assertRaises(BudgetExceeded):
                self.send(provider)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(provider.usage["calls"], 2)

    def test_retry_is_bounded_and_does_not_echo_response_body(self):
        provider = OpenAIProvider(api_key="fake-test-key")
        errors = [HTTPError("https://example.invalid", 503, "secret-body", {}, io.BytesIO(b"secret-body")) for _ in range(3)]
        with patch("renew.runtime.urlopen", side_effect=errors) as opener, patch("renew.runtime.time.sleep"):
            with self.assertRaises(APIError) as captured:
                self.send(provider)
        self.assertEqual(opener.call_count, 3)
        self.assertNotIn("secret", str(captured.exception))

    def test_structured_http_error_includes_sanitized_provider_details(self):
        body = json.dumps({"error": {
            "message": "No available channel for model test-model; Bearer fake-test-key; sk-private12345678",
            "code": "model_channel_unavailable",
            "type": "provider_error",
        }}).encode()
        headers = {"x-oneapi-request-id": "request-123"}
        errors = [HTTPError("https://example.invalid", 503, "private", headers, io.BytesIO(body)) for _ in range(3)]
        provider = OpenAIProvider(api_key="fake-test-key")
        with patch("renew.runtime.urlopen", side_effect=errors), patch("renew.runtime.time.sleep"):
            with self.assertRaises(APIError) as captured:
                self.send(provider)
        message = str(captured.exception)
        self.assertIn("No available channel for model test-model", message)
        self.assertIn("model_channel_unavailable", message)
        self.assertIn("provider_error", message)
        self.assertIn("request-123", message)
        self.assertNotIn("fake-test-key", message)
        self.assertNotIn("sk-private12345678", message)

    def test_http_request_ids_redact_the_actual_selected_key(self):
        key = "selected-test-secret"
        for header in ("x-request-id", "x-oneapi-request-id", "request-id"):
            for request_id in (key, "req-" + key + "-123"):
                with self.subTest(header=header, request_id=request_id):
                    provider = OpenAIProvider(api_key=key)
                    error = HTTPError("https://example.invalid", 401, "unauthorized",
                                      {header: request_id}, io.BytesIO())
                    with patch("renew.runtime.urlopen", side_effect=error):
                        with self.assertRaises(APIError) as captured:
                            self.send(provider)
                    message = str(captured.exception)
                    self.assertIn("请求 ID：", message)
                    self.assertIn("[已脱敏]", message)
                    self.assertNotIn(key, message)

    def test_non_retryable_http_and_transport_errors_are_private(self):
        for error in (
            HTTPError("https://example.invalid", 401, "private-value", {}, io.BytesIO(b"private-value")),
            URLError("private-value"),
            TimeoutError("private-value"),
        ):
            with self.subTest(error=type(error).__name__):
                provider = OpenAIProvider(api_key="fake-test-key")
                with patch("renew.runtime.urlopen", side_effect=error) as opener:
                    with self.assertRaises(APIError) as captured:
                        self.send(provider)
                self.assertNotIn("private-value", str(captured.exception))
                self.assertEqual(opener.call_count, 1)

    def test_transport_errors_report_safe_categories(self):
        cases = (
            (TimeoutError("private"), "请求超时"),
            (URLError(socket.gaierror("private")), "DNS 解析失败"),
            (URLError(ssl.SSLError("private")), "TLS 连接失败"),
            (URLError(ConnectionRefusedError("private")), "连接被拒绝"),
            (URLError(ConnectionResetError("private")), "连接被重置"),
        )
        for error, category in cases:
            with self.subTest(category=category):
                provider = OpenAIProvider(api_key="fake-test-key")
                with patch("renew.runtime.urlopen", side_effect=error):
                    with self.assertRaisesRegex(APIError, category) as captured:
                        self.send(provider)
                self.assertNotIn("private", str(captured.exception))

    def test_token_budget_counts_actual_usage_and_stops_after_overshoot(self):
        provider = OpenAIProvider(api_key="fake-test-key", max_total_tokens=10)
        with patch("renew.runtime.urlopen", return_value=self.raw_response({"input_tokens": 8, "output_tokens": 5, "total_tokens": 13})) as opener:
            with self.assertRaises(BudgetExceeded):
                self.send(provider)
            with self.assertRaises(BudgetExceeded):
                self.send(provider)
        self.assertEqual(provider.usage["total_tokens"], 13)
        self.assertEqual(opener.call_count, 1)

    def test_incomplete_response_still_counts_consumed_tokens(self):
        provider = OpenAIProvider(api_key="fake-test-key")
        with patch("renew.runtime.urlopen", return_value=self.raw_response({"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}, "incomplete")):
            with self.assertRaises(ProtocolError):
                self.send(provider)
        self.assertEqual(provider.usage["total_tokens"], 5)

    def test_shared_call_budget_is_thread_safe(self):
        provider = OpenAIProvider(api_key="fake-test-key", max_calls=4)

        def attempt(_):
            try:
                self.send(provider)
                return True
            except BudgetExceeded:
                return False

        with patch("renew.runtime.urlopen", side_effect=lambda *a, **kw: self.raw_response({"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})):
            with ThreadPoolExecutor(max_workers=8) as pool:
                succeeded = list(pool.map(attempt, range(12)))
        self.assertEqual(sum(succeeded), 4)
        self.assertEqual(provider.usage, {"calls": 4, "input_tokens": 4, "output_tokens": 8, "total_tokens": 12})

    def test_codex_cli_provider_uses_structured_output_and_workspace_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            provider = CodexCLIProvider(command="codex-test", workspace=workspace)
            final_schema = {"type": "object", "properties": {"summary": {"type": "string"}},
                            "required": ["summary"], "additionalProperties": False}
            captured = {}

            def fake_run(argv, **kwargs):
                captured["argv"] = argv
                captured["input"] = kwargs["input"]
                output_path = Path(argv[argv.index("-o") + 1])
                output_path.write_text(json.dumps({"summary": "完成"}), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch("renew.runtime.subprocess.run", side_effect=fake_run) as run:
                result = provider.respond(model="test-model", instructions="inspect", input=[],
                                          tools=[{"name": "final_result", "parameters": final_schema}],
                                          max_output_tokens=100)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(json.loads(result["output"][0]["arguments"]), {"summary": "完成"})
            self.assertIn("--ephemeral", captured["argv"])
            self.assertIn("--json", captured["argv"])
            self.assertIn("--output-schema", captured["argv"])
            self.assertIn("-s", captured["argv"])
            self.assertIn("read-only", captured["argv"])
            self.assertIn("standard Codex CLI", captured["input"])
            self.assertIn("Project test execution is disabled", captured["input"])
            self.assertIn("Do not run project tests", captured["input"])
            self.assertIn("static repository inspection", captured["input"])
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["errors"], "replace")
            self.assertNotIn("-c", captured["argv"])
            self.assertNotIn("env", run.call_args.kwargs)
            self.assertEqual(provider.usage["calls"], 1)

    @staticmethod
    def send_codex(provider):
        return provider.respond(model="test-model", instructions="inspect", input=[],
                                tools=[{"name": "final_result", "parameters": FINAL_SCHEMA}],
                                max_output_tokens=100)

    def test_codex_default_keeps_login_even_when_api_key_is_in_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "OPENAI_API_KEY": "unused-test-key", "SELF_RENEW_API_KEY": "unused-private-test-key",
        }, clear=True):
            provider = CodexCLIProvider(command="codex-test").for_workspace(directory)

            def fake_run(argv, **kwargs):
                self.assertNotIn("-c", argv)
                self.assertNotIn("env", kwargs)
                Path(argv[argv.index("-o") + 1]).write_text('{"summary":"done"}', encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch("renew.runtime.subprocess.run", side_effect=fake_run):
                self.send_codex(provider)
            self.assertIsNone(provider.base_url)

    def test_codex_connections_override_provider_and_isolate_keys_to_child_environment(self):
        environment = {"OPENAI_API_KEY": "wrong-default-test-key", "FIRST_KEY": "first-test-key",
                       "SECOND_KEY": "second-test-key", "SELF_RENEW_API_KEY": "stale-test-key"}
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", environment, clear=True):
            providers = [
                CodexCLIProvider(workspace=directory, base_url="https://first.example.test/v1///",
                                 api_key_env="FIRST_KEY"),
                CodexCLIProvider(workspace=directory, base_url="https://second.example.test/api/v1",
                                 api_key_env="SECOND_KEY"),
                CodexCLIProvider(workspace=directory, api_key="explicit-test-key", api_key_env="FIRST_KEY"),
                CodexCLIProvider(workspace=directory, api_key_env="SECOND_KEY"),
            ]
            expected = [
                ("https://first.example.test/v1", "first-test-key"),
                ("https://second.example.test/api/v1", "second-test-key"),
                ("https://api.openai.com/v1", "explicit-test-key"),
                ("https://api.openai.com/v1", "second-test-key"),
            ]
            original_environment = dict(os.environ)
            for provider, (base_url, key) in zip(providers, expected):
                with self.subTest(base_url=base_url, key=key):
                    def fake_run(argv, **kwargs):
                        settings = tomllib.loads("\n".join(
                            argv[index + 1] for index, argument in enumerate(argv) if argument == "-c"
                        ))
                        self.assertEqual(settings, {
                            "model_provider": "self_renew",
                            "model_providers": {"self_renew": {
                                "name": "Self Renew", "base_url": base_url,
                                "env_key": "SELF_RENEW_API_KEY", "wire_api": "responses",
                                "requires_openai_auth": False,
                            }},
                        })
                        self.assertEqual(kwargs["env"]["SELF_RENEW_API_KEY"], key)
                        self.assertIsNot(kwargs["env"], os.environ)
                        self.assertNotIn(key, " ".join(argv))
                        self.assertNotIn(key, kwargs["input"])
                        output_path = Path(argv[argv.index("-o") + 1])
                        for path in output_path.parent.iterdir():
                            self.assertNotIn(key, path.read_text(encoding="utf-8"))
                        output_path.write_text('{"summary":"done"}', encoding="utf-8")
                        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

                    with patch("renew.runtime.subprocess.run", side_effect=fake_run):
                        self.send_codex(provider)
                    self.assertEqual(dict(os.environ), original_environment)

    def test_codex_workspace_clones_keep_selected_key_and_share_budget(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CUSTOM_KEY": "original-test-key"}):
            original = CodexCLIProvider(base_url="https://endpoint.example.test/v1", api_key_env="CUSTOM_KEY",
                                        workspace=directory, max_calls=1, execute_tests=True)
            os.environ["CUSTOM_KEY"] = "replacement-test-key"
            clone = original.for_workspace(directory, writable=True)
            self.assertEqual(clone.base_url, original.base_url)
            self.assertEqual(clone.api_key_env, "CUSTOM_KEY")
            self.assertTrue(clone.execute_tests)
            self.assertTrue(clone.writable)

            def fake_run(argv, **kwargs):
                self.assertEqual(kwargs["env"]["SELF_RENEW_API_KEY"], "original-test-key")
                Path(argv[argv.index("-o") + 1]).write_text('{"summary":"done"}', encoding="utf-8")
                stdout = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}})
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            with patch("renew.runtime.subprocess.run", side_effect=fake_run) as run:
                self.send_codex(clone)
                with self.assertRaises(BudgetExceeded):
                    self.send_codex(original)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(original.usage, {"calls": 1, "input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
            self.assertEqual(clone.usage, original.usage)

    def test_codex_failure_redacts_the_actual_selected_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "CUSTOM_KEY": "selected-test-secret", "OPENAI_API_KEY": "default-test-secret",
        }, clear=True):
            provider = CodexCLIProvider(workspace=directory, api_key_env="CUSTOM_KEY")
            failed = subprocess.CompletedProcess([], 1, stdout="", stderr=(
                "Authentication failed: selected-test-secret; default-test-secret; model unavailable"
            ))
            with patch("renew.runtime.subprocess.run", return_value=failed):
                with self.assertRaises(APIError) as captured:
                    self.send_codex(provider)
            message = str(captured.exception)
            self.assertIn("Authentication failed", message)
            self.assertIn("model unavailable", message)
            self.assertIn("[已脱敏]", message)
            self.assertNotIn("selected-test-secret", message)
            self.assertNotIn("default-test-secret", message)

    def test_codex_cli_provider_requests_workspace_write_for_developer(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = CodexCLIProvider(command="codex-test", workspace=directory, writable=True)
            final_schema = {"type": "object", "properties": {"summary": {"type": "string"}},
                            "required": ["summary"], "additionalProperties": False}

            def fake_run(argv, **kwargs):
                Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"summary": "完成"}), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch("renew.runtime.subprocess.run", side_effect=fake_run) as run:
                provider.respond(model="test-model", instructions="implement", input=[],
                                 tools=[{"name": "final_result", "parameters": final_schema}],
                                 max_output_tokens=100)
            argv = run.call_args.args[0]
            self.assertIn("--approve-for-me", argv)
            self.assertNotIn("workspace-write", argv)

    def test_codex_cli_provider_marks_external_test_execution_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = CodexCLIProvider(command="codex-test", workspace=directory, execute_tests=True)
            final_schema = {"type": "object", "properties": {"summary": {"type": "string"}},
                            "required": ["summary"], "additionalProperties": False}
            captured = {}

            def fake_run(argv, **kwargs):
                captured["input"] = kwargs["input"]
                Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"summary": "done"}), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with patch("renew.runtime.subprocess.run", side_effect=fake_run):
                provider.respond(model="test-model", instructions="inspect", input=[],
                                 tools=[{"name": "final_result", "parameters": final_schema}],
                                 max_output_tokens=100)
            self.assertIn("explicitly enabled external test execution", captured["input"])
            self.assertIn("Do not run project tests", captured["input"])

    def test_codex_cli_usage_is_aggregated_from_json_events(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = CodexCLIProvider(command="codex-test", workspace=directory)
            final_schema = {"type": "object", "properties": {"summary": {"type": "string"}},
                            "required": ["summary"], "additionalProperties": False}

            def fake_run(argv, **kwargs):
                Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"summary": "完成"}), encoding="utf-8")
                stdout = json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 12, "output_tokens": 3,
                }})
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            with patch("renew.runtime.subprocess.run", side_effect=fake_run):
                provider.respond(model="test-model", instructions="inspect", input=[],
                                 tools=[{"name": "final_result", "parameters": final_schema}],
                                 max_output_tokens=100)
            self.assertEqual(provider.usage, {"calls": 1, "input_tokens": 12,
                                               "output_tokens": 3, "total_tokens": 15})


if __name__ == "__main__":
    unittest.main()
