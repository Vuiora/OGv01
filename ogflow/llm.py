"""Bounded JSON chat client. Provider bodies and credentials never enter errors."""
import json
import httpx
from .config import LLMConfig

class LLMError(RuntimeError):
    pass

class JSONClient:
    def __init__(self, config: LLMConfig, *, max_calls=12, transport=None):
        self.config, self.max_calls, self.calls = config, max_calls, 0
        self.transport = transport
        self.events = []

    def complete(self, system, payload, *, max_tokens=4096):
        if not self.config.api_key:
            raise LLMError("OG_LLM_API_KEY is not configured")
        if self.calls >= self.max_calls:
            raise LLMError("LLM request budget exhausted")
        self.calls += 1
        try:
            with httpx.Client(timeout=self.config.timeout, transport=self.transport, follow_redirects=False) as client:
                response = client.post(self.config.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.config.api_key},
                    json={"model": self.config.model, "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)}],
                        "response_format": {"type": "json_object"}, "max_tokens": max_tokens,
                        "temperature": 0, "stream": False})
            if response.status_code != 200:
                raise LLMError(f"LLM HTTP {response.status_code}")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise LLMError("LLM output is incomplete")
            content = choice["message"]["content"].strip()
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            result = json.loads(content)
            if not isinstance(result, dict):
                raise LLMError("LLM output must be a JSON object")
            self.events.append({"call": self.calls, "model": self.config.model, "usage": body.get("usage", {})})
            return result
        except LLMError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("LLM transport or response format failed") from None

