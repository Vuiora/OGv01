import httpx
import pytest
from fastapi.testclient import TestClient

from concept_relation.api import create_app
from concept_relation.config import Settings


@pytest.mark.parametrize("failure, expected", [
    ("connection", "无法连接 n8n"), (404, "Webhook 未注册"),
    (401, "n8n 拒绝认证"), (500, "HTTP 500"),
])
def test_dispatch_errors_identify_the_cause(tmp_path, failure, expected):
    def handler(request):
        if failure == "connection":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(failure, text="private provider response")
    settings = Settings(data_root=tmp_path, app_key="test", llm_url="https://model.test", llm_model="test")
    app = create_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(app, headers={"X-API-Key": "test"}) as client:
        job_id = client.post("/api/jobs", files={"file": ("test.md", b"content")}).json()["job_id"]
        response = client.post(f"/api/jobs/{job_id}/launch", json={"mode": "n8n"})
        assert response.status_code == 502
        assert expected in response.json()["detail"]
        status = client.get(f"/api/jobs/{job_id}").json()
        assert status["status"] == "failed" and status["error"] == response.json()["detail"]
        assert "private provider response" not in status["error"]


def test_dispatch_timeout_keeps_polling_without_resubmitting(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)
    app = create_app(Settings(data_root=tmp_path, llm_url="https://model.test", llm_model="test"),
                     httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", files={"file": ("test.md", b"content")}).json()["job_id"]
        response = client.post(f"/api/jobs/{job_id}/launch", json={"mode": "n8n"})
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert "请勿重复提交" in response.json()["message"]
        assert client.post(f"/api/jobs/{job_id}/launch", json={"mode": "n8n"}).status_code == 409


@pytest.mark.parametrize("body", [{"accepted": True, "job_id": "wrong"}, "<html>not n8n</html>"])
def test_dispatch_rejects_invalid_success_responses(tmp_path, body):
    def handler(request):
        return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(200, text=body)
    app = create_app(Settings(data_root=tmp_path, llm_url="https://model.test", llm_model="test"),
                     httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", files={"file": ("test.md", b"content")}).json()["job_id"]
        assert client.post(f"/api/jobs/{job_id}/launch", json={"mode": "n8n"}).status_code == 502
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "failed"


def test_retry_preserves_completed_stages_and_failure_history(tmp_path):
    import json
    def handler(request):
        return httpx.Response(202, json={"accepted": True, "job_id": json.loads(request.content)["job_id"]})
    app = create_app(Settings(data_root=tmp_path, llm_url="https://model.test", llm_model="test"),
                     httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", files={"file": ("test.md", b"content")}).json()["job_id"]
        job = app.state.store.get(job_id)
        job.update(status="failed", error="output limit", completed_stages=["parse", "extract"])
        app.state.store.save(job)
        response = client.post(f"/api/jobs/{job_id}/retry", json={"mode": "n8n"})
        assert response.status_code == 202
        result = response.json()
        assert result["completed_stages"] == ["parse", "extract"]
        assert result["previous_failures"][0]["error"] == "output limit"
        assert client.post(f"/api/jobs/{job_id}/retry", json={"mode": "n8n"}).status_code == 409
