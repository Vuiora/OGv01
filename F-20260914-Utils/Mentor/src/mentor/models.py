import json
import time

import httpx
from sqlalchemy import select

from mentor.config import Settings
from mentor.db import Database, ModelCall, Run
from mentor.domain import TERMINAL, Policy, canonical


class ModelError(Exception):
    pass


class ModelGateway:
    def __init__(self, db: Database, config: Settings, transport=None):
        self.db, self.config, self.transport = db, config, transport

    def generate(self, role: str, prompt: str, payload: dict, policy: Policy, run_id=None) -> dict:
        endpoint, model, key = self.config.role_config(role)
        if not endpoint or not model or not key:
            raise ModelError("MODEL_NOT_CONFIGURED")
        if role == "teacher" and self.config.teacher_mode == "assisted":
            raise ModelError("TEACHER_ASSISTED_MODE_REQUIRES_CANDIDATE_IMPORT")
        text = canonical(payload)
        # UTF-8 byte count is a conservative upper bound for these byte-tokenized providers.
        input_bound = len((prompt + text).encode()) + 256
        reserved_tokens = input_bound + policy.max_output_tokens
        input_rate = getattr(self.config, f"{role}_input_usd_per_million")
        output_rate = getattr(self.config, f"{role}_output_usd_per_million")
        reserve = (input_bound * input_rate + policy.max_output_tokens * output_rate) / 1e6
        body = {"model": model, "messages": [{"role": "system", "content": prompt},
                 {"role": "user", "content": text}], "max_tokens": policy.max_output_tokens,
                "response_format": {"type": "json_object"}, "stream": False}
        path = "/chat/completions"
        if "deepseek.com" in endpoint:
            body["thinking"] = {"type": "disabled"}
        if role == "teacher" and self.config.teacher_protocol == "responses":
            path = "/responses"
            body = {"model": model, "instructions": prompt, "input": text, "store": False,
                    "max_output_tokens": policy.max_output_tokens, "reasoning": {"effort": "low"},
                    "text": {"format": {"type": "json_object"}}}
        for attempt in range(3):
            with self.db.transaction() as session:
                if run_id:
                    run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
                    if (not run or run.status in TERMINAL or run.deadline <= time.time()):
                        raise ModelError("RUN_NOT_ACTIVE")
                    if (run.requests >= policy.max_model_requests_per_run
                            or run.tokens_reserved + reserved_tokens > policy.max_total_tokens
                            or run.cost_actual + run.cost_reserved + reserve > policy.max_cost_usd):
                        raise ModelError("BUDGET_EXHAUSTED")
                    run.requests += 1
                    run.tokens_reserved += reserved_tokens
                    run.cost_reserved += reserve
                call = ModelCall(run_id=run_id, role=role, model=model, reservation=reserve,
                                 reserved_tokens=reserved_tokens,
                                 price_snapshot={"input": input_rate, "output": output_rate,
                                                 "basis": "configured_upper_bound", "currency": "USD"})
                session.add(call)
                session.flush()
                call_id = call.id
            try:
                with httpx.Client(timeout=90, transport=self.transport, follow_redirects=False) as client:
                    response = client.post(endpoint + path, headers={"Authorization": "Bearer " + key}, json=body)
                if response.status_code >= 400:
                    raise ModelError(f"MODEL_HTTP_{response.status_code}")
                data = response.json()
                usage = data.get("usage") or {}
                incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
                outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
                known = incoming is not None and outgoing is not None
                cost = (incoming * input_rate + outgoing * output_rate) / 1e6 if known else reserve
                with self.db.transaction() as session:
                    call = session.get(ModelCall, call_id)
                    call.status, call.usage, call.cost = "estimated" if not known else "succeeded", usage, cost
                    if run_id:
                        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
                        run.cost_reserved = max(0, run.cost_reserved - reserve)
                        run.cost_actual += cost
                        if known:
                            run.tokens_reserved += incoming + outgoing - reserved_tokens
                if path == "/responses":
                    if data.get("status") != "completed":
                        raise ModelError("MODEL_INCOMPLETE_OUTPUT")
                    content = "".join(c.get("text", "") for item in data.get("output", [])
                                      for c in item.get("content", []) if c.get("type") == "output_text")
                else:
                    choice = data["choices"][0]
                    if choice.get("finish_reason") != "stop":
                        raise ModelError("MODEL_INCOMPLETE_OUTPUT")
                    content = choice["message"]["content"]
                return json.loads(content)
            except (httpx.HTTPError, ModelError, ValueError, KeyError, IndexError) as error:
                with self.db.transaction() as session:
                    call = session.get(ModelCall, call_id)
                    if call.status == "reserved":
                        call.status = "uncertain"
                        call.cost = reserve
                code = str(error) if isinstance(error, ModelError) else "MODEL_TRANSPORT_OR_FORMAT_ERROR"
                retryable = code.startswith(("MODEL_HTTP_429", "MODEL_HTTP_5")) or isinstance(error, httpx.TransportError)
                if not retryable or attempt == 2:
                    raise ModelError(code) from None
                time.sleep(min(2 ** attempt, 4))
        raise ModelError("MODEL_RETRY_EXHAUSTED")
