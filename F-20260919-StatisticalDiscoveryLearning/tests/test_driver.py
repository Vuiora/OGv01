"""M11 LLM 驱动层（``sdl_m11/driver.py``）的验收测试。

覆盖：

* 边界常量与 AST 自检（不替 LLM 决策、不新增算法、不跑统计、不授予等级、不绕过 M10）；
* 凭据纪律：**密钥不入 to_dict / 不入 canonical_json / 不入 content_digest / 不入提示词**；
* 工具 schema 组装（15 个工具、参数类型映射、必填项）；
* 驱动循环：脚本化假 client 编排真实 M10 工具箱 → 回喂 → 停止原因；
* 预算（rounds / tool_calls）、endpoint 报错降级；
* 可选代码沙箱工具：**默认关闭**；开启时走 M12 且被拒代码不执行；
* 「不绕过 M10」的硬证据：非法参数经 ``Toolbox.call`` 得到 ``rejected``。

**离线可测**：本例不触网——用 :class:`ScriptedClient` 注入脚本化回复序列。
"""

from __future__ import annotations

import ast
import json
import pathlib
import unittest

from sdl_m10.toolbox import build_default_toolbox, STATUS_OK, STATUS_REJECTED
from sdl_m11 import driver as D
from sdl_m11.driver import (
    ENV_API_KEY, ENV_BASE_URL, ENV_MODEL, ENV_TIMEOUT,
    DriverInputError, DriverLimits, DriverSession, EndpointConfig,
    EndpointConfigError, LLMReply, LLMTransportError, NOT_PROVIDED_BY_P19,
    STOP_BUDGET_EXHAUSTED, STOP_LLM_ERROR, STOP_MAX_ROUNDS, STOP_NO_TOOL_CALL,
    ToolCall, _parse_openai_reply, build_system_prompt, run_driver, self_check,
    tool_schema,
)

MODULE_PATH = pathlib.Path(D.__file__)


def _module_source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


class ScriptedClient:
    """脚本化假 client：按顺序弹出预设回复，用于离线驱动整条编排链。

    依赖倒置：M11 只要求 ``complete(messages, tools) -> LLMReply``，
    因此真实 HTTP 客户端与这个假对象在驱动层看来毫无区别。
    """

    def __init__(self, replies: list[LLMReply]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, messages, tools):  # noqa: ANN001
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        if not self._replies:
            return LLMReply(text="(脚本已空)")
        return self._replies.pop(0)


class RaisingClient:
    """一调就抛的 client，用于验证 llm_error 降级。"""

    def complete(self, messages, tools):  # noqa: ANN001
        raise LLMTransportError("模拟 endpoint 掉线")


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

class BoundaryTests(unittest.TestCase):

    def test_self_check_passes_on_real_source(self):
        report = self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["defined_other_stages"], ())
        self.assertEqual(report["accessed_forbidden"], ())
        self.assertFalse(report["imports_m01"])

    def test_boundary_flags_are_all_false(self):
        self.assertFalse(D.DRIVER_DECIDES_FOR_LLM)
        self.assertFalse(D.DRIVER_ADDS_ALGORITHMS)
        self.assertFalse(D.DRIVER_RUNS_STATISTICS)
        self.assertFalse(D.DRIVER_GRANTS_EVIDENCE_GRADE)
        self.assertFalse(D.DRIVER_BYPASSES_TOOLBOX)
        self.assertFalse(D.DRIVER_REQUIRES_FREE_CODE)

    def test_source_has_no_m01_import_statement(self):
        tree = ast.parse(_module_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotEqual(a.name.split(".")[0], "sdl_m01")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual((node.module or "").split(".")[0], "sdl_m01")

    def test_all_tool_calls_go_through_toolbox_call(self):
        """硬证据：源码里必须出现 ``toolbox.call(``，且**不得**直接调用算法层函数。"""
        src = _module_source()
        self.assertIn("toolbox.call(", src)
        # 不得直接调用这些算法层函数（它们只能经 M10 的封装到达）。
        tree = ast.parse(src)
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        for banned in ("fit_relation", "enumerate_candidates", "pareto_front",
                       "compute_p_value", "grade_for"):
            self.assertNotIn(banned, called, f"不得直接调用 {banned}")

    def test_detector_catches_planted_definition(self):
        planted = "def compute_p_value(x):\n    return 0.05\n"
        tree = ast.parse(planted)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertIn("compute_p_value", defined & set(NOT_PROVIDED_BY_P19))


# ---------------------------------------------------------------------------
# 凭据纪律
# ---------------------------------------------------------------------------

class CredentialDisciplineTests(unittest.TestCase):
    """密钥只能存在于内存：不入 to_dict / canonical_json / digest / 提示词。"""

    KEY = "sk-SUPER-SECRET-DO-NOT-LEAK-0123456789"

    def test_api_key_is_masked_in_to_dict(self):
        cfg = EndpointConfig(base_url="https://x/v1", model="m", api_key=self.KEY)
        d = cfg.to_dict()
        self.assertNotIn(self.KEY, json.dumps(d))
        self.assertEqual(d["api_key"], "***")
        self.assertTrue(d["has_api_key"])

    def test_api_key_absent_from_canonical_json_and_digest(self):
        cfg = EndpointConfig(base_url="https://x/v1", model="m", api_key=self.KEY)
        self.assertNotIn(self.KEY, cfg.canonical_json())
        # 换密钥不应改变配置指纹（指纹只反映「连哪、用哪个模型」）。
        other = EndpointConfig(base_url="https://x/v1", model="m", api_key="other")
        self.assertEqual(cfg.content_digest(), other.content_digest())

    def test_api_key_absent_from_system_prompt(self):
        cfg = EndpointConfig(base_url="https://x/v1", model="m", api_key=self.KEY)
        toolbox = build_default_toolbox(seed=0)
        prompt = build_system_prompt(catalogue_summary=toolbox.catalogue())
        self.assertNotIn(self.KEY, prompt)
        self.assertNotIn("api_key", prompt.lower())

    def test_from_env_reads_and_requires_core_fields(self):
        env = {
            ENV_BASE_URL: "https://api.example/v1",
            ENV_MODEL: "gpt-x",
            ENV_API_KEY: self.KEY,
            ENV_TIMEOUT: "12.5",
        }
        cfg = EndpointConfig.from_env(env)
        self.assertEqual(cfg.base_url, "https://api.example/v1")
        self.assertEqual(cfg.model, "gpt-x")
        self.assertEqual(cfg.timeout, 12.5)
        self.assertEqual(cfg.api_key, self.KEY)
        with self.assertRaises(EndpointConfigError):
            EndpointConfig.from_env({ENV_MODEL: "gpt-x"})  # 缺 base_url
        with self.assertRaises(EndpointConfigError):
            EndpointConfig.from_env({ENV_BASE_URL: "u", ENV_TIMEOUT: "abc",
                                     ENV_MODEL: "m"})

    def test_invalid_config_rejected(self):
        with self.assertRaises(EndpointConfigError):
            EndpointConfig(base_url="", model="m")
        with self.assertRaises(EndpointConfigError):
            EndpointConfig(base_url="u", model="")
        with self.assertRaises(EndpointConfigError):
            EndpointConfig(base_url="u", model="m", timeout=0)

    def test_session_never_contains_key(self):
        cfg = EndpointConfig(base_url="https://x/v1", model="m", api_key=self.KEY)
        # run_driver 不接触密钥；会话对象里也不该有它的任何痕迹。
        session = DriverSession(steps=(), final_text="", stop_reason=STOP_NO_TOOL_CALL,
                                toolbox_digest="d", endpoint=cfg.base_url, model=cfg.model)
        blob = session.canonical_json()
        self.assertNotIn(self.KEY, blob)


# ---------------------------------------------------------------------------
# 工具 schema
# ---------------------------------------------------------------------------

class ToolSchemaTests(unittest.TestCase):

    def setUp(self):
        self.toolbox = build_default_toolbox(seed=0)

    def test_schema_has_one_entry_per_tool(self):
        schema = tool_schema(self.toolbox)
        self.assertEqual(len(schema), len(self.toolbox.names()))
        self.assertEqual(len(schema), 15)

    def test_schema_shape_is_openai_style(self):
        schema = tool_schema(self.toolbox)
        for item in schema:
            self.assertEqual(item["type"], "function")
            fn = item["function"]
            self.assertIn("name", fn)
            self.assertIn("description", fn)
            self.assertEqual(fn["parameters"]["type"], "object")
            self.assertIn("required", fn["parameters"])

    def test_required_params_match_signature(self):
        """必填项须与真实签名一致——参数名一律查 describe()，不凭直觉。"""
        spec = self.toolbox.describe("m02.parse_expression")
        by_name = {t["function"]["name"]: t for t in tool_schema(self.toolbox)}
        params = by_name["m02.parse_expression"]["function"]["parameters"]
        self.assertIn("text", params["properties"])       # 真名是 text，不是 expression
        self.assertNotIn("expression", params["properties"])
        self.assertEqual(params["required"],
                         [p.name for p in spec.parameters if p.required])

    def test_type_mapping(self):
        by_name = {t["function"]["name"]: t for t in tool_schema(self.toolbox)}
        props = by_name["m02.parse_expression"]["function"]["parameters"]["properties"]
        self.assertEqual(props["text"]["type"], "string")
        self.assertEqual(props["max_depth"]["type"], "integer")

    def test_generic_sequence_params_map_to_array(self):
        """``Sequence[str]`` / ``Iterable[float]`` 必须映射为 JSON array，不能退化成 string。

        回归缺陷：``ParamSpec.type`` 是整段注解文本，精确匹配表查不到就掉进默认
        ``"string"``——LLM 会把列表参数当字符串传，``prepare_sample`` /
        ``enumerate_candidates`` / ``knowledge_base`` 全部不可用。
        **此缺陷由真实 endpoint 的端到端演示暴露，离线假 client 测不到。**
        """
        by_name = {t["function"]["name"]: t for t in tool_schema(self.toolbox)}
        ps = by_name["m03.prepare_sample"]["function"]["parameters"]["properties"]
        self.assertEqual(ps["variables"]["type"], "array")
        self.assertEqual(ps["variables"]["items"]["type"], "string")
        self.assertEqual(ps["protocol_id"]["type"], "string")
        self.assertEqual(ps["target"]["type"], "string")

        ec = by_name["m02.enumerate_candidates"]["function"]["parameters"]["properties"]
        self.assertEqual(ec["variables"]["type"], "array")
        self.assertEqual(ec["constants"]["type"], "array")
        self.assertEqual(ec["constants"]["items"]["type"], "number")
        self.assertEqual(ec["powers"]["items"]["type"], "integer")

    def test_generic_split_helper(self):
        from sdl_m11.driver import _json_type_for
        self.assertEqual(_json_type_for("Sequence[str]"), ("array", "string"))
        self.assertEqual(_json_type_for("Iterable[float]"), ("array", "number"))
        self.assertEqual(_json_type_for("Mapping[str,int]"), ("object", None))
        self.assertEqual(_json_type_for("int"), ("integer", None))
        self.assertEqual(_json_type_for("str"), ("string", None))


# ---------------------------------------------------------------------------
# 驱动循环（离线）
# ---------------------------------------------------------------------------

class DriverLoopTests(unittest.TestCase):

    def setUp(self):
        self.toolbox = build_default_toolbox(seed=0)

    def test_no_tool_call_means_done(self):
        client = ScriptedClient([LLMReply(text="分析完成，无需更多调用。")])
        session = run_driver(client=client, toolbox=self.toolbox,
                             user_task="随便看看")
        self.assertEqual(session.stop_reason, STOP_NO_TOOL_CALL)
        self.assertIn("分析完成", session.final_text)
        self.assertEqual(session.tool_call_count, 0)
        self.assertEqual(len(client.calls), 1)

    def test_tool_call_is_executed_and_fed_back(self):
        """LLM 请求一次真实工具调用 → 经 M10 执行 → 结果回喂 → 下一轮收尾。"""
        client = ScriptedClient([
            LLMReply(text="先解析表达式。", tool_calls=(
                ToolCall(name="m02.parse_expression", arguments={"text": "2*X + 1"}),
            )),
            LLMReply(text="得到 AST，完成。"),
        ])
        session = run_driver(client=client, toolbox=self.toolbox,
                             user_task="解析 2*X + 1")
        self.assertEqual(session.stop_reason, STOP_NO_TOOL_CALL)
        self.assertEqual(session.tool_call_count, 1)
        # 第一次调用的结果必须回喂（第二次调用的 messages 里出现 role=tool）。
        second_msgs = client.calls[1]["messages"]
        roles = [m["role"] for m in second_msgs]
        self.assertIn("tool", roles)
        tool_msg = next(m for m in second_msgs if m["role"] == "tool")
        payload = json.loads(tool_msg["content"])
        self.assertEqual(payload["status"], STATUS_OK)
        # assistant 的 tool_calls 应记入历史。
        self.assertIn("assistant", roles)

    def test_tools_are_passed_to_client(self):
        client = ScriptedClient([LLMReply(text="done")])
        run_driver(client=client, toolbox=self.toolbox, user_task="x")
        self.assertEqual(len(client.calls[0]["tools"]), 15)

    def test_invalid_arguments_yield_rejected_not_bypass(self):
        """非法参数必须经 ``Toolbox.call`` 得到 rejected——证明没有绕过护栏。

        用 M10 的**真名**（``text``）反过来的错名（``expression``）触发
        ``unexpected_argument``；若 M11 绕过 toolbox 直调算法层，就得不到这个码。
        """
        client = ScriptedClient([
            LLMReply(tool_calls=(
                ToolCall(name="m02.parse_expression",
                         arguments={"expression": "X"}),  # 错名
            )),
            LLMReply(text="ok"),
        ])
        session = run_driver(client=client, toolbox=self.toolbox, user_task="x")
        step = session.steps[0]
        self.assertEqual(len(step.results), 1)
        self.assertEqual(step.results[0].status, STATUS_REJECTED)
        self.assertEqual(step.results[0].reason, "unexpected_argument")

    def test_unknown_tool_is_rejected(self):
        client = ScriptedClient([
            LLMReply(tool_calls=(ToolCall(name="m99.nope", arguments={}),)),
            LLMReply(text="ok"),
        ])
        session = run_driver(client=client, toolbox=self.toolbox, user_task="x")
        self.assertEqual(session.steps[0].results[0].status, STATUS_REJECTED)

    def test_max_rounds_stops_loop(self):
        # 每轮都请求工具、永不收尾 → 应由 max_rounds 截断。
        replies = [
            LLMReply(tool_calls=(ToolCall(name="m02.parse_expression",
                                          arguments={"text": "X"}),))
            for _ in range(10)
        ]
        client = ScriptedClient(replies)
        session = run_driver(client=client, toolbox=self.toolbox, user_task="x",
                             limits=DriverLimits(max_rounds=3, max_tool_calls=99))
        self.assertEqual(session.stop_reason, STOP_MAX_ROUNDS)
        self.assertEqual(len(session.steps), 3)

    def test_tool_call_budget_stops_loop(self):
        client = ScriptedClient([
            LLMReply(tool_calls=(
                ToolCall(name="m02.parse_expression", arguments={"text": "X"}),
                ToolCall(name="m02.parse_expression", arguments={"text": "Z"}),
                ToolCall(name="m02.parse_expression", arguments={"text": "W"}),
            )),
        ])
        session = run_driver(client=client, toolbox=self.toolbox, user_task="x",
                             limits=DriverLimits(max_rounds=5, max_tool_calls=2))
        self.assertEqual(session.stop_reason, STOP_BUDGET_EXHAUSTED)
        self.assertEqual(session.tool_call_count, 2)

    def test_llm_error_degrades_gracefully(self):
        session = run_driver(client=RaisingClient(), toolbox=self.toolbox,
                             user_task="x")
        self.assertEqual(session.stop_reason, STOP_LLM_ERROR)
        self.assertIn("驱动中断", session.final_text)

    def test_empty_user_task_rejected(self):
        client = ScriptedClient([LLMReply(text="x")])
        with self.assertRaises(DriverInputError):
            run_driver(client=client, toolbox=self.toolbox, user_task="   ")

    def test_limits_must_be_positive(self):
        with self.assertRaises(DriverInputError):
            DriverLimits(max_rounds=0)
        with self.assertRaises(DriverInputError):
            DriverLimits(max_tool_calls=0)


# ---------------------------------------------------------------------------
# 可选代码沙箱
# ---------------------------------------------------------------------------

class CodeSubmissionTests(unittest.TestCase):

    def setUp(self):
        self.toolbox = build_default_toolbox(seed=0)

    def test_sandbox_tool_absent_by_default(self):
        client = ScriptedClient([LLMReply(text="done")])
        run_driver(client=client, toolbox=self.toolbox, user_task="x")
        names = [t["function"]["name"] for t in client.calls[0]["tools"]]
        self.assertNotIn("m12.run_code", names)

    def test_sandbox_tool_present_when_enabled(self):
        client = ScriptedClient([LLMReply(text="done")])
        run_driver(client=client, toolbox=self.toolbox, user_task="x",
                   allow_code_submission=True)
        names = [t["function"]["name"] for t in client.calls[0]["tools"]]
        self.assertIn("m12.run_code", names)

    def test_enabled_sandbox_executes_pure_computation(self):
        client = ScriptedClient([
            LLMReply(tool_calls=(ToolCall(
                name="m12.run_code",
                arguments={"source": "def main():\n    return {'n': sum([1,2,3])}\n",
                           "entry": "main"}),)),
            LLMReply(text="完成"),
        ])
        session = run_driver(client=client, toolbox=self.toolbox, user_task="x",
                             allow_code_submission=True)
        res = session.steps[0].results[0]
        self.assertEqual(res.status, STATUS_OK)
        self.assertEqual(res.summary["value"], {"n": 6})

    def test_enabled_sandbox_rejects_escape_and_never_executes(self):
        """开启代码提交时，越狱代码仍被 M12 拦下——driving 层不是绕过口。"""
        client = ScriptedClient([
            LLMReply(tool_calls=(ToolCall(
                name="m12.run_code",
                arguments={"source": "import os\ndef main():\n    return os.getcwd()\n",
                           "entry": "main"}),)),
            LLMReply(text="ok"),
        ])
        session = run_driver(client=client, toolbox=self.toolbox, user_task="x",
                             allow_code_submission=True)
        res = session.steps[0].results[0]
        self.assertEqual(res.status, STATUS_REJECTED)
        self.assertFalse(res.summary["verdict"]["allowed"])


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

class ReplyParsingTests(unittest.TestCase):

    def test_parses_content_and_tool_calls(self):
        data = {
            "choices": [{"message": {
                "content": "好的",
                "tool_calls": [{
                    "function": {"name": "m02.parse_expression",
                                 "arguments": '{"text": "X"}'},
                }],
            }}],
        }
        reply = _parse_openai_reply(data)
        self.assertEqual(reply.text, "好的")
        self.assertEqual(len(reply.tool_calls), 1)
        self.assertEqual(reply.tool_calls[0].name, "m02.parse_expression")
        self.assertEqual(dict(reply.tool_calls[0].arguments), {"text": "X"})

    def test_malformed_arguments_become_empty(self):
        data = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "t", "arguments": "{not json"}}]}}]}
        reply = _parse_openai_reply(data)
        self.assertEqual(dict(reply.tool_calls[0].arguments), {})

    def test_bad_envelope_raises_transport_error(self):
        with self.assertRaises(LLMTransportError):
            _parse_openai_reply({"nope": 1})

    def test_tool_call_requires_name(self):
        with self.assertRaises(DriverInputError):
            ToolCall(name="")


# ---------------------------------------------------------------------------
# 双向自检（防空洞门禁）
# ---------------------------------------------------------------------------

class SelfCheckTwoWayTests(unittest.TestCase):

    def test_planted_definition_is_caught(self):
        src = "def fit_relation(x):\n    return x\n"
        tree = ast.parse(src)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertIn("fit_relation", defined & set(NOT_PROVIDED_BY_P19))

    def test_boundary_declaration_is_not_flagged(self):
        """禁用名出现在规则表常量里，不得被自检判成违规（AST 而非文本）。"""
        report = self_check()
        self.assertNotIn("grade_for", report["defined_other_stages"])
        self.assertNotIn("sqlite3", report["accessed_forbidden"])


if __name__ == "__main__":
    unittest.main()
