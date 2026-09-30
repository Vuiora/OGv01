"""Chat Completions transport for Self Renew's existing tool-based AgentRunner."""
import json
import threading
import httpx
from renew.runtime import APIError, BudgetExceeded, ProtocolError

def chat_messages(instructions, items):
    messages = [{"role": "system", "content": instructions}]
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            call = {"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}}
            if messages[-1]["role"] == "assistant" and "tool_calls" in messages[-1]:
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        elif kind == "message":
            messages.append({"role": item.get("role", "assistant"), "content": "".join(c.get("text", "") for c in item.get("content", []))})
        elif "role" in item:
            messages.append({"role": item["role"], "content": item["content"]})
    return messages

class ChatProvider:
    mode = "compatible_chat"

    def __init__(self, config, *, max_calls=12, max_total_tokens=120000, transport=None, progress=lambda event: None):
        self.config, self.max_calls, self.max_total_tokens, self.transport = config, max_calls, max_total_tokens, transport
        self.progress = progress
        self.lock = threading.Lock()
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def snapshot(self):
        with self.lock:
            return dict(self.usage)

    def respond(self, *, model, instructions, input, tools, max_output_tokens):
        if model != self.config.model or not self.config.api_key:
            raise APIError("Unified business model is missing or mismatched")
        with self.lock:
            if self.usage["calls"] >= self.max_calls or self.usage["total_tokens"] >= self.max_total_tokens:
                raise BudgetExceeded("Business Self Renew budget exhausted")
            self.usage["calls"] += 1
        self.progress({"phase": "llm_request", "call": self.snapshot()["calls"]})
        payload = {"model": model, "messages": chat_messages(instructions, input),
            "tools": [{"type": "function", "function": {k: v for k, v in t.items() if k != "type"}} for t in tools],
            "tool_choice": "auto", "max_tokens": max_output_tokens, "temperature": 0, "stream": False}
        try:
            with httpx.Client(timeout=self.config.timeout, transport=self.transport, follow_redirects=False) as client:
                response = client.post(self.config.base_url + "/chat/completions", json=payload,
                    headers={"Authorization": "Bearer " + self.config.api_key})
            if response.status_code != 200:
                self.progress({"phase": "http_failure", "status": response.status_code})
                raise APIError(f"Business Self Renew HTTP {response.status_code}")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProtocolError("Business Self Renew output incomplete")
            usage = body.get("usage", {})
            incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if type(incoming) is not int or type(outgoing) is not int or min(incoming, outgoing) < 0:
                raise ProtocolError("Business Self Renew usage unavailable")
            with self.lock:
                self.usage["input_tokens"] += incoming
                self.usage["output_tokens"] += outgoing
                self.usage["total_tokens"] += incoming + outgoing
                if self.usage["total_tokens"] > self.max_total_tokens:
                    raise BudgetExceeded("Business Self Renew token budget exceeded")
            self.progress({"phase": "llm_completed", **self.snapshot()})
            message, output = choice["message"], []
            for call in message.get("tool_calls", []):
                output.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"],
                               "arguments": call["function"]["arguments"], "status": "completed"})
            if message.get("content"):
                output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": message["content"]}]})
            return {"status": "completed", "output": output, "usage": {
                "input_tokens": incoming, "output_tokens": outgoing, "total_tokens": incoming + outgoing}}
        except (APIError, BudgetExceeded, ProtocolError):
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            self.progress({"phase": "transport_failure", "error": type(exc).__name__})
            raise APIError("Business Self Renew transport/format failure") from None

