import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from ogflow.api import create_app
from ogflow.architecture import produce_architecture
from ogflow.bootstrap import ROOT
from ogflow.config import LLMConfig
from ogflow.llm import JSONClient, LLMError
from ogflow.renew_adapter import ChatProvider, chat_messages

CONFIG = LLMConfig(api_key="private-test-value")

def test_chat_client_uses_canonical_endpoint_and_does_not_expose_errors():
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(401, text="private-test-value provider failure")
    client = JSONClient(CONFIG, transport=httpx.MockTransport(transport))
    with pytest.raises(LLMError) as error:
        client.complete("JSON", {})
    assert str(requests[0].url) == "https://llm.ujn.edu.cn/v1/chat/completions"
    assert json.loads(requests[0].content)["model"] == "deepseek-v41-flash"
    assert "private-test-value" not in str(error.value)

def test_json_budget_and_incomplete_response():
    def transport(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
    client = JSONClient(CONFIG, max_calls=1, transport=httpx.MockTransport(transport))
    with pytest.raises(LLMError, match="incomplete"):
        client.complete("JSON", {})
    with pytest.raises(LLMError, match="budget"):
        client.complete("JSON", {})

def test_self_renew_tool_transport_round_trip():
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "call-1", "function": {"name": "final_result", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    provider = ChatProvider(CONFIG, transport=httpx.MockTransport(transport))
    result = provider.respond(model=CONFIG.model, instructions="scope", input=[{"role": "user", "content": "audit"}],
        tools=[{"type": "function", "name": "final_result", "parameters": {"type": "object"}}], max_output_tokens=100)
    assert result["output"][0]["type"] == "function_call"
    history = [{"role": "user", "content": "audit"}] + result["output"] + [{"type": "function_call_output", "call_id": "call-1", "output": "ok"}]
    messages = chat_messages("scope", history)
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"]
    assert provider.snapshot()["total_tokens"] == 15

def test_api_auth_and_private_vault_not_downloadable(tmp_path):
    app = create_app(output=tmp_path, config=CONFIG, token="test-token")
    client = TestClient(app)
    assert client.get("/v1/application").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert client.get("/v1/application", headers=headers).status_code == 200
    folder = tmp_path / ("a" * 32)
    folder.mkdir()
    (folder / "evidence.sqlite3").write_text("private C rows")
    assert client.get(f"/v1/runs/{folder.name}/artifacts/evidence.sqlite3", headers=headers).status_code == 404
    spec = json.loads((ROOT / "examples/business/demo-task.json").read_text(encoding="utf-8"))
    spec["observations"] = "../outside.csv"
    assert client.post("/v1/runs", json=spec, headers=headers).status_code == 400

def test_tpwf_produces_valid_editable_pages_from_implemented_sources(tmp_path):
    result = produce_architecture(tmp_path, CONFIG, live=False)
    assert result["quality_passed"] is True
    assert result["page_count"] == result["module_count"] + 1
    architecture = json.loads((tmp_path / "architecture.json").read_text(encoding="utf-8"))
    assert all(c["evidence"] for c in architecture["components"])
    assert "Wiki" not in json.dumps(architecture)
    assert "private-test-value" not in (tmp_path / "architecture.drawio").read_text(encoding="utf-8")


def test_api_runs_real_pipeline_and_serves_archive(tmp_path):
    app = create_app(output=tmp_path, config=CONFIG, token="test-token")
    headers = {"Authorization": "Bearer test-token"}
    spec = json.loads((ROOT / "examples/business/demo-task.json").read_text(encoding="utf-8"))
    spec.update(observations="examples/business/demo-observations.csv", llm="offline",
                architecture=False, feedback=False, n_resamples=2, max_candidates=24)
    with TestClient(app) as client:
        response = client.post("/v1/runs", json=spec, headers=headers)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        status = client.get(f"/v1/runs/{run_id}", headers=headers).json()
        assert status["state"] == "SUCCEEDED"
        artifact = client.get(f"/v1/runs/{run_id}/artifacts/discovery-archive.json", headers=headers)
        assert artifact.status_code == 200
        assert artifact.json()["rounds"][0]["status"] == "confirmed"
