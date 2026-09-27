"""M11 · LLM 驱动层（把工具目录交给 LLM 编排）。

本模块回答的问题是：《SDL算法框架说明》§7 要求「LLM 辅助提出表示、候选解释、
竞争假说、可执行代码草案以及取证建议」。P18 交付的 M10 只是**给 LLM 用的接口**
（工具目录 + 会话句柄 + 四类护栏），其本身并不包含 LLM——**没有一个环节真的去调 LLM**。
M11 就是补上这一环：它读取 M10 的工具目录，组装成 LLM 可见的工具清单，向 endpoint
发起对话，解析 LLM 请求的工具调用，经 ``Toolbox.call()`` 执行，再把结果回喂，循环
直到 LLM 不再请求工具或触达预算。

**分工（与框架 §7 逐条对应）**：

======================  ==================================================
框架 §7 的规定            本模块的做法
======================  ==================================================
LLM 提出表示/解释/假说     提供工具目录 + 对话循环；LLM 自主选择调用哪个工具、传什么参数
统计执行器算指标/效应量     M11 **不计算**任何统计量——全部经 M10 转 M2–M9 执行
符号/数值工具检查公式      同上（M2 的解析器、M3 的拟合器即此类工具）
所有内容须能追溯、可核验    每轮记 ``DriverStep``；会话产出 ``content_digest()``
LLM 不能凭文本授予证据等级  M11 **不授予证据等级**，也不把 LLM 文本当证据
LLM 生成的代码须隔离运行    M11 可把代码交给 M12（:mod:`sdl_m12.sandbox`）检查执行
======================  ==================================================

**边界（可机检，见 :func:`self_check`）**：

① 不新增算法、不跑统计、不授予证据等级；
② 不绕过 M10 直接调用算法层——一切工具调用必须经 ``Toolbox.call()``，
   从而受四类护栏与封存泄漏检查约束；
③ endpoint 凭据只从环境变量或显式注入读取，**绝不落盘、绝不入日志、绝不入提示词**；
④ 不把确证分区内容送给 LLM（M10 的封存泄漏检查已挡住，M11 再核对一遍）。

**关于「离线可测」**：本模块把「与 LLM 的对话」抽象为 :class:`LLMClient` 协议
（仅需 ``complete(messages, tools) -> LLMReply``）。真实实现走 HTTP；测试时注入
**假 client**（脚本化的回复序列）即可在无网络环境下验证整条编排链。这与 M9
「采样源由调用方注入」、M10「explorer 由调用方注入」是同一种依赖倒置手法。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from sdl_m10.toolbox import (
    Toolbox,
    ToolResult,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_FAILED,
    REASON_UNKNOWN_TOOL,
    REASON_UNEXPECTED_ARGUMENT,
    REASON_TOOL_RAISED,
    REASON_FORBIDDEN_ARGUMENT,
)

# --------------------------------------------------------------------------
# 版本与机检常量
# --------------------------------------------------------------------------

DRIVER_VERSION = "P19-v1.0"

#: 本模块是否**替 LLM 决策**。恒为 ``False``——编排主意属于 LLM。
DRIVER_DECIDES_FOR_LLM = False

#: 本模块是否**新增算法/统计能力**。恒为 ``False``。
DRIVER_ADDS_ALGORITHMS = False

#: 本模块是否**执行统计检验**。恒为 ``False``——统计全在 M2–M9。
DRIVER_RUNS_STATISTICS = False

#: 本模块是否**授予证据等级**。恒为 ``False``——证据判定在 M6/M8。
DRIVER_GRANTS_EVIDENCE_GRADE = False

#: 本模块是否**绕过 M10 直接调用算法层**。恒为 ``False``——
#: 一切工具调用必须经 ``Toolbox.call()``，否则护栏与封存检查会被架空。
DRIVER_BYPASSES_TOOLBOX = False

#: 本模块是否**要求 LLM 写自由代码**。缺省 ``False``——工具目录足以覆盖
#: 「调用已有算法」；只有当调用方显式启用 ``allow_code_submission`` 时，
#: 才会把 M12 作为 ``m12.run_code`` 工具暴露给 LLM。**默认关闭是安全的选择**。
DRIVER_REQUIRES_FREE_CODE = False

DRIVER_BOUNDARY_NOTE = (
    "M11 是 LLM 驱动层：它把 M10 的工具目录交给 LLM，由 LLM 决定调用哪个工具、"
    "传什么参数，M11 只负责执行其工具调用并把结果回喂。M11 不新增算法、"
    "不执行统计、不授予证据等级，也不绕过 M10 直接调用算法层。"
    "endpoint 凭据只存在于内存，不落盘、不入日志、不入提示词。"
)

HANDLE_NOTE = (
    "LLM 只能凭会话句柄（形如 h_<hex>）引用上游真实产出；它无法构造算法层内部对象。"
    "句柄由 M10 的 HandleStore 发放，跨会话不可复用。"
)

# --------------------------------------------------------------------------
# 停止原因码
# --------------------------------------------------------------------------

STOP_COMPLETED = "completed"
STOP_MAX_ROUNDS = "max_rounds"
STOP_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_NO_TOOL_CALL = "no_tool_call"
STOP_LLM_ERROR = "llm_error"
STOP_FATAL = "fatal"

DRIVER_STOP_REASONS: tuple[str, ...] = (
    STOP_COMPLETED, STOP_MAX_ROUNDS, STOP_BUDGET_EXHAUSTED,
    STOP_NO_TOOL_CALL, STOP_LLM_ERROR, STOP_FATAL,
)

#: 环境变量名（凭据**只经此读取**，代码里不出现任何字面密钥）。
ENV_BASE_URL = "SDL_LLM_BASE_URL"
ENV_API_KEY = "SDL_LLM_API_KEY"
ENV_MODEL = "SDL_LLM_MODEL"
ENV_TIMEOUT = "SDL_LLM_TIMEOUT"

# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class DriverError(ValueError):
    """M11 的基类异常。"""


class DriverInputError(DriverError):
    """输入不合法。"""


class EndpointConfigError(DriverError):
    """endpoint 配置缺失或不完整。"""


class LLMTransportError(DriverError):
    """与 endpoint 通信失败。"""


# 越界定义的自检黑名单（本模块**不得**定义这些）。
NOT_PROVIDED_BY_P19: tuple[str, ...] = (
    "compute_p_value", "holm_adjust", "classify_result", "grade_for",
    "bind_confirmation", "consume_confirmation", "record_evaluation",
    "archive_round", "update_knowledge_version", "run_loop",
    "enumerate_candidates", "fit_relation", "pareto_front",
)

# 越权访问的自检黑名单（本模块**不得**访问这些）。
FORBIDDEN_ACCESS_NAMES: tuple[str, ...] = (
    "sqlite3", "sdl_m01", "add_confirmation", "consume_confirmation",
    "bind_confirmation", "record_evaluation", "archive_confirmation",
    "release_results", "mark_compromised",
)


# --------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EndpointConfig:
    """LLM 连接配置。

    ``api_key`` **只存在于内存**——``to_dict()`` 一律掩码，也不参与
    ``content_digest()``，以免密钥指纹泄漏到日志或仓库。
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 60.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise EndpointConfigError("base_url 不能为空")
        if not isinstance(self.model, str) or not self.model.strip():
            raise EndpointConfigError("model 不能为空")
        if self.timeout <= 0:
            raise EndpointConfigError("timeout 必须为正")
        if self.max_retries < 0:
            raise EndpointConfigError("max_retries 不能为负")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EndpointConfig":
        """从环境变量读取配置。**这是推荐的凭据来源**——不落盘、不入库。"""
        src = os.environ if env is None else env
        base_url = src.get(ENV_BASE_URL, "").strip()
        model = src.get(ENV_MODEL, "").strip()
        if not base_url or not model:
            raise EndpointConfigError(
                f"缺少环境变量 {ENV_BASE_URL} 或 {ENV_MODEL}；"
                f"密钥请置于 {ENV_API_KEY}（不落盘）"
            )
        timeout_raw = src.get(ENV_TIMEOUT, "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else 60.0
        except ValueError as exc:
            raise EndpointConfigError(f"{ENV_TIMEOUT} 必须是数字") from exc
        return cls(
            base_url=base_url,
            model=model,
            api_key=src.get(ENV_API_KEY, ""),
            timeout=timeout,
        )

    def redacted(self) -> dict[str, Any]:
        """掩码视图：可安全写入日志/报告。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "has_api_key": bool(self.api_key),
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.redacted()
        d["api_key"] = "***" if self.api_key else ""
        return d

    def canonical_json(self) -> str:
        return json.dumps(self.redacted(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def content_digest(self) -> str:
        """**不包含密钥**的配置指纹。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# LLM 交互对象
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    """LLM 请求执行的一次工具调用。"""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise DriverInputError("工具名不能为空")
        object.__setattr__(
            self, "arguments",
            MappingProxyType(dict(self.arguments or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": dict(self.arguments)}


@dataclass(frozen=True)
class LLMReply:
    """一次 LLM 回复（驱动层需要的最小信息）。"""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw or {})))


class LLMClient(Protocol):
    """LLM 客户端协议。

    只需实现 ``complete``：给定对话消息与工具 schema，返回 :class:`LLMReply`。
    真实实现走 HTTP（见 :class:`HttpLLMClient`）；测试注入假 client。
    """

    def complete(self, messages: Sequence[Mapping[str, Any]],
                 tools: Sequence[Mapping[str, Any]]) -> LLMReply:
        ...


@dataclass(frozen=True)
class DriverStep:
    """一轮「LLM 提议 → 工具执行」的记录。"""

    round_index: int
    assistant_text: str | None
    tool_calls: tuple[ToolCall, ...]
    results: tuple[ToolResult, ...]
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "assistant_text": self.assistant_text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "results": [r.to_dict() for r in self.results],
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class DriverSession:
    """一次完整驱动会话的结果。"""

    steps: tuple[DriverStep, ...]
    final_text: str
    stop_reason: str
    toolbox_digest: str
    endpoint: str
    model: str
    boundary: str = DRIVER_BOUNDARY_NOTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        if self.stop_reason not in DRIVER_STOP_REASONS:
            raise DriverInputError(f"未知停止原因码：{self.stop_reason!r}")

    @property
    def tool_call_count(self) -> int:
        return sum(len(s.tool_calls) for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": DRIVER_VERSION,
            "steps": [s.to_dict() for s in self.steps],
            "final_text": self.final_text,
            "stop_reason": self.stop_reason,
            "toolbox_digest": self.toolbox_digest,
            "endpoint": self.endpoint,
            "model": self.model,
            "tool_call_count": self.tool_call_count,
            "boundary": self.boundary,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def content_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 工具 schema 转换
# --------------------------------------------------------------------------

_JSON_TYPE = {
    "str": "string", "int": "integer", "float": "number", "number": "number",
    "bool": "boolean", "list": "array", "tuple": "array",
    "dict": "object", "mapping": "object", "none": "null",
}


def tool_schema(toolbox: Toolbox) -> tuple[dict[str, Any], ...]:
    """把 M10 的工具目录转成 LLM 可读的工具 schema（OpenAI 风格）。"""
    cat = toolbox.catalogue()
    out: list[dict[str, Any]] = []
    for name in toolbox.names():
        spec = toolbox.describe(name)
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in spec.parameters:
            props[p.name] = {
                "type": _JSON_TYPE.get(p.type, "string"),
                "description": (
                    f"句柄参数（{p.type}）" if p.type and p.type.startswith("h_")
                    else f"类型 {p.type}"
                ),
            }
            if p.required:
                required.append(p.name)
        out.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": f"[{spec.module}] {spec.summary}",
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        })
    return tuple(out)


def build_system_prompt(*, catalogue_summary: Mapping[str, Any],
                        extra: str | None = None) -> str:
    """组装系统提示词。

    **提示词里绝不出现 endpoint 凭据、令牌或确证分区内容。**
    """
    lines = [
        "你是 SDL（统计发现学习）系统里的分析主体。框架 §7 规定：你负责提出表示、",
        "候选解释、竞争假说与取证建议，并把结果转成清晰陈述；统计执行器负责算指标、",
        "效应量与误差控制，符号/数值工具负责检查公式与约束。",
        "",
        "你可以通过工具调用来使用系统已有的分析算法。请自行决定调用哪个工具、",
        "以什么顺序调用、传什么参数——编排的主意在你，不在工具。",
        "",
        "重要约束：",
        "1. 引用上游产物必须使用会话句柄（形如 h_<hex>），你不能构造内部对象。",
        "2. 你没有权限授予证据等级，也不能凭文本合理性断言结论成立；",
        "   是否构成证据由系统的确证机制判定。",
        "3. 「不支持」与「证据不足」是两件不同的事，不要混为一谈。",
        "",
        f"当前可用工具共 {len(catalogue_summary.get('tools', []))} 个：",
    ]
    for t in catalogue_summary.get("tools", []):
        lines.append(f"  - {t.get('name')}: {t.get('summary', '')}")
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTTP 客户端（真实实现）
# --------------------------------------------------------------------------

class HttpLLMClient:
    """走 OpenAI 兼容 ``/chat/completions`` 的客户端（仅用标准库）。

    只在被真正调用时才发起网络请求；构造本身不触网，便于测试注入。
    """

    def __init__(self, config: EndpointConfig, *,
                 opener: Callable[..., Any] | None = None) -> None:
        self._config = config
        self._opener = opener or urllib.request.urlopen

    @property
    def config(self) -> EndpointConfig:
        return self._config

    def _url(self) -> str:
        base = self._config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def complete(self, messages: Sequence[Mapping[str, Any]],
                 tools: Sequence[Mapping[str, Any]]) -> LLMReply:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": list(messages),
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            # 密钥只出现在请求头里——不写日志、不入会话轨迹。
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            req = urllib.request.Request(self._url(), data=body, headers=headers,
                                         method="POST")
            try:
                with self._opener(req, timeout=self._config.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                return _parse_openai_reply(json.loads(raw))
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code < 500:  # 4xx 非暂时性错误，不重试
                    raise LLMTransportError(
                        f"endpoint 返回 HTTP {exc.code}") from exc
            except Exception as exc:  # noqa: BLE001 - 网络错误种类多，统一降级重试
                last_exc = exc
        raise LLMTransportError(f"endpoint 通信失败：{last_exc}")


def _parse_openai_reply(data: Mapping[str, Any]) -> LLMReply:
    """把 OpenAI 兼容响应解析为 :class:`LLMReply`。"""
    try:
        choice = data["choices"][0]
        msg = choice.get("message", {})
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMTransportError(f"响应结构无法解析：{exc}") from exc

    text = msg.get("content") or ""
    calls: list[ToolCall] = []
    for item in msg.get("tool_calls") or []:
        fn = item.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, Mapping):
            args = dict(raw_args)
        else:
            args = {}
        calls.append(ToolCall(name=str(name), arguments=args))
    return LLMReply(text=text, tool_calls=tuple(calls), raw=dict(data))


# --------------------------------------------------------------------------
# 驱动循环
# --------------------------------------------------------------------------

@dataclass
class DriverLimits:
    """驱动预算。"""

    max_rounds: int = 12
    max_tool_calls: int = 60

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise DriverInputError("max_rounds 必须为正")
        if self.max_tool_calls <= 0:
            raise DriverInputError("max_tool_calls 必须为正")


def _tool_result_message(call: ToolCall, result: ToolResult) -> dict[str, Any]:
    """把 ToolResult 转成回喂给 LLM 的消息（**不含确证分区内容**）。"""
    return {
        "role": "tool",
        "tool_call_id": f"{call.name}#{result.status}",
        "name": call.name,
        "content": json.dumps({
            "status": result.status,
            "handle": result.handle,
            "summary": result.summary,
            "reason": result.reason,
        }, ensure_ascii=False, default=str),
    }


def run_driver(
    *,
    client: LLMClient,
    toolbox: Toolbox,
    limits: DriverLimits | None = None,
    user_task: str,
    extra_system: str | None = None,
    allow_code_submission: bool = False,
    sandbox_tool: Callable[..., Any] | None = None,
) -> DriverSession:
    """驱动一整段「LLM 编排分析」的会话。

    循环：把 (系统提示 + 工具 schema + 用户任务) 发给 LLM → 解析其工具调用 →
    逐个经 ``toolbox.call()`` 执行 → 把结果作为 tool 消息回喂 → 直到 LLM 不再
    请求工具、或触达预算、或 endpoint 报错。

    :param allow_code_submission: 是否把 M12 沙箱作为 ``m12.run_code`` 工具暴露。
        **缺省关闭**——「调用已有算法」不需要 LLM 写代码；只有当任务确实需要
        自定义计算时才开启，且执行仍受 M12 静态检查约束。
    :param sandbox_tool: 注入的沙箱执行函数（缺省用 :mod:`sdl_m12.sandbox`）。
    """
    if not isinstance(user_task, str) or not user_task.strip():
        raise DriverInputError("user_task 不能为空")
    limits = limits or DriverLimits()

    cat = toolbox.catalogue()
    tools = list(tool_schema(toolbox))

    # 可选的代码沙箱工具：仅当显式启用时才暴露。
    if allow_code_submission:
        runner = sandbox_tool
        if runner is None:
            from sdl_m12.sandbox import run_code as _run_code
            runner = _run_code
        tools.append({
            "type": "function",
            "function": {
                "name": "m12.run_code",
                "description": "[M12] 在隔离沙箱中执行一段纯计算代码（先静态检查，"
                               "不过则不执行）。仅返回 JSON 基础类型结果。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "entry": {"type": "string"},
                    },
                    "required": ["source"],
                    "additionalProperties": False,
                },
            },
        })
    else:
        runner = None

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(
            catalogue_summary=cat, extra=extra_system)},
        {"role": "user", "content": user_task},
    ]

    steps: list[DriverStep] = []
    final_text = ""
    stop_reason = STOP_COMPLETED
    calls_used = 0
    toolbox_digest = toolbox.content_digest()[:12]

    for round_index in range(1, limits.max_rounds + 1):
        try:
            reply = client.complete(messages, tools)
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误一律降级为 llm_error
            steps.append(DriverStep(
                round_index=round_index,
                assistant_text=None,
                tool_calls=(),
                results=(),
                stop_reason=STOP_LLM_ERROR,
            ))
            stop_reason = STOP_LLM_ERROR
            final_text = f"[驱动中断] endpoint 报错：{type(exc).__name__}"
            break

        final_text = reply.text or final_text

        # LLM 不再请求工具 → 视为完成。
        if not reply.tool_calls:
            steps.append(DriverStep(
                round_index=round_index,
                assistant_text=reply.text or None,
                tool_calls=(),
                results=(),
                stop_reason=STOP_NO_TOOL_CALL,
            ))
            stop_reason = STOP_NO_TOOL_CALL
            break

        # 把 assistant 的这次请求记入对话历史。
        messages.append({
            "role": "assistant",
            "content": reply.text or "",
            "tool_calls": [
                {"id": f"{c.name}#{i}", "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(dict(c.arguments),
                                                      ensure_ascii=False)}}
                for i, c in enumerate(reply.tool_calls)
            ],
        })

        results: list[ToolResult] = []
        executed: list[ToolCall] = []
        for call in reply.tool_calls:
            if calls_used >= limits.max_tool_calls:
                stop_reason = STOP_BUDGET_EXHAUSTED
                break
            calls_used += 1
            result = _dispatch(toolbox, call, runner)
            results.append(result)
            executed.append(call)
            messages.append(_tool_result_message(call, result))

        # 只把**真正执行过**的调用记进这一步——预算用尽时被跳过的那些调用
        # 不应计入 tool_call_count（否则「预算=2」却报出 3 次调用，账实不符）。
        steps.append(DriverStep(
            round_index=round_index,
            assistant_text=reply.text or None,
            tool_calls=tuple(executed),
            results=tuple(results),
            stop_reason=stop_reason if stop_reason == STOP_BUDGET_EXHAUSTED else None,
        ))

        if stop_reason == STOP_BUDGET_EXHAUSTED:
            break
    else:
        stop_reason = STOP_MAX_ROUNDS

    return DriverSession(
        steps=tuple(steps),
        final_text=final_text,
        stop_reason=stop_reason,
        toolbox_digest=toolbox_digest,
        endpoint="(injected)",
        model="(injected)",
    )


def _dispatch(toolbox: Toolbox, call: ToolCall,
              sandbox_runner: Callable[..., Any] | None) -> ToolResult:
    """执行一次工具调用。

    ``m12.run_code`` 是本层特有的可选工具（不在 M10 目录里）；其余一律
    经 ``Toolbox.call()``——**不绕过护栏**。
    """
    if call.name == "m12.run_code":
        if sandbox_runner is None:
            return ToolResult(
                tool="m12.run_code", status=STATUS_REJECTED,
                handle=None, summary={"error": "沙箱未启用"},
                reason=REASON_UNKNOWN_TOOL,
            )
        try:
            run = sandbox_runner(**dict(call.arguments))
        except TypeError as exc:
            return ToolResult(
                tool="m12.run_code", status=STATUS_REJECTED,
                handle=None, summary={"error": str(exc)},
                reason=REASON_UNEXPECTED_ARGUMENT,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool="m12.run_code", status=STATUS_FAILED,
                handle=None, summary={"error": f"{type(exc).__name__}: {exc}"},
                reason=REASON_TOOL_RAISED,
            )
        payload = run.to_dict() if hasattr(run, "to_dict") else dict(run)
        # M12 的状态码（ok/rejected/failed/timeout）**不能**直接塞进 M10 的 ToolResult——
        # 后者只接受自己那套原因码，否则 __post_init__ 会抛 ToolboxInputError。
        # 这里做一次显式映射，并把沙箱原始状态留在 summary 里以便追溯。
        sb_status = payload.get("status")
        if sb_status == "ok":
            status, reason = STATUS_OK, None
        elif sb_status == "rejected":
            # 静态检查未过 = 提交了被禁止的代码。
            status, reason = STATUS_REJECTED, REASON_FORBIDDEN_ARGUMENT
        else:
            # failed / timeout 等运行期问题，统一按「工具内抛错」归因。
            status, reason = STATUS_FAILED, REASON_TOOL_RAISED
        summary = dict(payload)
        summary["sandbox_status"] = sb_status
        return ToolResult(
            tool="m12.run_code", status=status, handle=None,
            summary=summary, reason=reason,
        )

    # 其余工具：一律经 M10 的 call()，受四类护栏与封存泄漏检查约束。
    return toolbox.call(call.name, **dict(call.arguments))


# --------------------------------------------------------------------------
# 边界自检
# --------------------------------------------------------------------------

def self_check() -> dict[str, Any]:
    """AST 级边界自检。

    与 M10/M12 同理：用 AST 而非文本匹配，并剔除本地绑定名——
    本模块的 ``NOT_PROVIDED_BY_P19`` 里**必须**写出 ``compute_p_value``
    这类被禁名字（那是规则定义），文本匹配会把规则表本身当违规。
    """
    import ast as _ast
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)

    defined: set[str] = set()
    accessed: set[str] = set()
    local_bindings: set[str] = set()

    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            defined.add(node.name)
            local_bindings.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
                    local_bindings.add(a.arg)
        elif isinstance(node, _ast.Name):
            if isinstance(node.ctx, _ast.Store):
                local_bindings.add(node.id)
            else:
                accessed.add(node.id)
        elif isinstance(node, _ast.Attribute):
            accessed.add(node.attr)

    defined_violations = sorted(defined & set(NOT_PROVIDED_BY_P19))
    accessed_violations = sorted(
        (accessed & set(FORBIDDEN_ACCESS_NAMES)) - local_bindings
    )
    # 是否导入 sdl_m01：用 AST 的 import 节点判定（文本匹配会把边界声明当违规）。
    imports_m01 = any(
        (isinstance(n, _ast.Import) and any(
            a.name.split(".")[0] == "sdl_m01" for a in n.names))
        or (isinstance(n, _ast.ImportFrom)
            and (n.module or "").split(".")[0] == "sdl_m01")
        for n in _ast.walk(tree)
    )

    return {
        "defined_other_stages": tuple(defined_violations),
        "accessed_forbidden": tuple(accessed_violations),
        "imports_m01": imports_m01,
        "decides_for_llm": DRIVER_DECIDES_FOR_LLM,
        "adds_algorithms": DRIVER_ADDS_ALGORITHMS,
        "runs_statistics": DRIVER_RUNS_STATISTICS,
        "grants_evidence_grade": DRIVER_GRANTS_EVIDENCE_GRADE,
        "bypasses_toolbox": DRIVER_BYPASSES_TOOLBOX,
        "checked_names": len(NOT_PROVIDED_BY_P19) + len(FORBIDDEN_ACCESS_NAMES),
        "ok": (not defined_violations and not accessed_violations and not imports_m01
               and not (DRIVER_DECIDES_FOR_LLM or DRIVER_ADDS_ALGORITHMS
                        or DRIVER_RUNS_STATISTICS or DRIVER_GRANTS_EVIDENCE_GRADE
                        or DRIVER_BYPASSES_TOOLBOX)),
    }


__all__ = [
    "DRIVER_VERSION", "DRIVER_DECIDES_FOR_LLM", "DRIVER_ADDS_ALGORITHMS",
    "DRIVER_RUNS_STATISTICS", "DRIVER_GRANTS_EVIDENCE_GRADE",
    "DRIVER_BYPASSES_TOOLBOX", "DRIVER_REQUIRES_FREE_CODE",
    "DRIVER_BOUNDARY_NOTE", "HANDLE_NOTE",
    "STOP_COMPLETED", "STOP_MAX_ROUNDS", "STOP_BUDGET_EXHAUSTED",
    "STOP_NO_TOOL_CALL", "STOP_LLM_ERROR", "STOP_FATAL", "DRIVER_STOP_REASONS",
    "ENV_BASE_URL", "ENV_API_KEY", "ENV_MODEL", "ENV_TIMEOUT",
    "DriverError", "DriverInputError", "EndpointConfigError", "LLMTransportError",
    "NOT_PROVIDED_BY_P19", "FORBIDDEN_ACCESS_NAMES",
    "EndpointConfig", "ToolCall", "LLMReply", "LLMClient", "DriverStep",
    "DriverSession", "DriverLimits", "HttpLLMClient",
    "tool_schema", "build_system_prompt", "run_driver", "self_check",
]
