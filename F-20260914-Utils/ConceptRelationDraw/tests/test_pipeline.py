import asyncio
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from concept_relation.api import create_app
from concept_relation.config import Settings
from concept_relation.grounding import evidence_for, split_document, validate_concepts, validate_relations
from concept_relation.llm import LLM, PipelineError
from concept_relation.models import ConceptCandidate, Extraction, RelationCandidate
from concept_relation.pipeline import Pipeline, StageConflict
from concept_relation.store import Store

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = "# 基础\n变量用于保存数据。\n常量用于保存固定的数据。\n# 联系\n变量与常量都用于保存数据，但常量的值固定。\n# 独立知识\n注释是代码中的说明文字。\n"


def configured(tmp_path, **kwargs):
    return replace(Settings(data_root=tmp_path, app_key="service-test-key", llm_url="https://model.test/v1", llm_model="test-model",
                            llm_key="model-secret", relation_batch_size=1), **kwargs)


def model_result(value):
    return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value, ensure_ascii=False)}]}]})


class ModelMock:
    def __init__(self):
        self.full_documents = []
        self.requests = []
        self.docling_called = False

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/v1/convert/source"):
            self.docling_called = True
            body = json.loads(request.content)
            assert body["sources"][0]["kind"] == "file"
            assert "base64_string" in body["sources"][0]
            return httpx.Response(200, json={"status": "success", "document": {"md_content": DOCUMENT}})
        if request.url.path.endswith("/healthz/readiness") or request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        if "/webhook/" in request.url.path:
            return httpx.Response(202, json={"accepted": True, "job_id": json.loads(request.content)["job_id"]})
        body = json.loads(request.content)
        assert body["store"] is False
        assert "temperature" not in body
        payload = json.loads(body["input"].split("\nINPUT_DATA:\n")[1])
        if "candidates" in payload:
            assert payload["full_document"] == DOCUMENT
            return model_result({"concepts": [{"id": concept["id"], "importance_reason": "文档主要讨论的概念。"}
                                              for concept in payload["candidates"]]})
        if "full_document" in payload:
            self.full_documents.append(payload["full_document"])
            if "relation_candidates" in payload:
                return model_result({"relation_ids": [relation["id"] for relation in payload["relation_candidates"]]})
            if "c1" in payload["source_ids"]:
                return model_result({"relations": [{"source": "c1", "target": "c2", "type": "contrasts", "explanation": "原文比较变量与常量。",
                                                     "quotes": ["变量与常量都用于保存数据，但常量的值固定。"], "confidence": .91},
                                                    {"source": "c1", "target": "external", "type": "related", "explanation": "伪造端点", "quotes": ["变量用于保存数据。"], "confidence": 1}]})
            return model_result({"relations": []})
        text = payload["text"]
        concepts = []
        for label, quote in [("变量", "变量用于保存数据。"), ("常量", "常量用于保存固定的数据。"), ("注释", "注释是代码中的说明文字。")]:
            if quote in text:
                concepts.append({"label": label, "definition": quote, "quotes": [quote]})
        concepts.append({"label": "外部知识", "definition": "不在文档中", "quotes": ["这句话不在文档中。"]})
        return model_result({"concepts": concepts})


def run_pipeline(tmp_path, suffix=".md"):
    async def run():
        settings = configured(tmp_path)
        store = Store(tmp_path)
        model = ModelMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            pipeline = Pipeline(settings, store, client)
            job = store.create("source" + suffix, DOCUMENT.replace("\n", "\r\n").encode() if suffix == ".md" else b"fake pdf")
            for stage in ("parse", "extract", "relate", "render"):
                await pipeline.stage(job["job_id"], stage)
            return store, job["job_id"], model
    return asyncio.run(run())


def test_full_context_and_isolated_node_with_rejections(tmp_path):
    store, job_id, mock = run_pipeline(tmp_path)
    graph = store.read(job_id, "graph.json")
    assert store.get(job_id)["status"] == "completed"
    assert [c["label"] for c in graph["concepts"]] == ["变量", "常量", "注释"]
    assert len(graph["relations"]) == 1
    assert graph["validation"]["isolated_concepts"] == ["c3"]
    assert len(graph["validation"]["rejected"]) == 4
    assert mock.full_documents == [DOCUMENT] * 4
    for concept in graph["concepts"]:
        for evidence in concept["evidence"]:
            assert DOCUMENT[evidence["start"]:evidence["end"]] == evidence["quote"]


def test_pdf_uses_docling(tmp_path):
    store, job_id, mock = run_pipeline(tmp_path, ".pdf")
    assert mock.docling_called
    assert store.read(job_id, "graph.json")["document"]["converter"] == "docling-serve"


def test_same_label_different_meanings_are_preserved():
    document = "苹果是一种水果。苹果是一家公司的名称。"
    sections, chunks = split_document(document)
    concepts, rejected = [], []
    candidates = [ConceptCandidate(label="苹果", definition=quote, quotes=[quote]) for quote in ["苹果是一种水果。", "苹果是一家公司的名称。"]]
    validate_concepts(candidates + [candidates[0]], document, sections, chunks[0], concepts, rejected)
    assert len(concepts) == 2
    assert not rejected


def test_relation_requires_both_endpoints_in_evidence():
    sections, _ = split_document(DOCUMENT)
    relation = RelationCandidate(source="c1", target="c2", type="related", explanation="无充分证据", quotes=["变量用于保存数据。"], confidence=.9)
    relations, rejected = [], []
    validate_relations([relation], DOCUMENT, sections, [{"id": "c1", "label": "变量"}, {"id": "c2", "label": "常量"}], ["c1"], relations, rejected)
    assert not relations
    assert "两个概念" in rejected[0]["reason"]


def test_large_section_coverage_and_unicode_evidence():
    document = "# 标题\n" + "😀变量保存数据。\n" * 1000
    sections, chunks = split_document(document, 500)
    coverage = set()
    for chunk in chunks:
        assert len(chunk["text"]) <= 500
        coverage.update(range(chunk["start"], chunk["end"]))
    assert len(coverage) == len(document)
    found = evidence_for(["😀变量保存数据。"], document, sections)
    assert found[0]["line_start"] == 2
    assert document[found[0]["start"]:found[0]["end"]] == "😀变量保存数据。"


def test_prompt_budget_fails_before_network(tmp_path):
    async def run():
        mock = ModelMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            with pytest.raises(PipelineError, match="没有截断"):
                await LLM(configured(tmp_path, max_prompt_chars=100), client).ask("提取", {"text": DOCUMENT}, Extraction)
        assert mock.requests == []
    asyncio.run(run())


@pytest.mark.parametrize("response", [
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "refused"}]}]},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "not json"}]}]},
])
def test_incomplete_or_invalid_model_output_fails(tmp_path, response):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
            with pytest.raises(PipelineError):
                await LLM(configured(tmp_path), client).ask("提取", {}, Extraction)
    asyncio.run(run())


def test_chat_completions_request(tmp_path):
    def mock(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert "max_completion_tokens" in body and "max_tokens" not in body
        assert "temperature" not in body
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"concepts": []}'}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            result = await LLM(configured(tmp_path, api_style="chat"), client).ask("提取", {}, Extraction)
            assert result.concepts == []
    asyncio.run(run())


@pytest.mark.parametrize("api_style", ["responses", "chat"])
def test_explicit_reasoning_budget(tmp_path, api_style):
    def mock(request):
        body = json.loads(request.content)
        if api_style == "responses":
            assert body["reasoning"] == {"effort": "low"}
            assert body["max_output_tokens"] == 16384
            return model_result({"concepts": []})
        assert body["reasoning_effort"] == "low"
        assert body["max_completion_tokens"] == 16384
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"concepts": []}'}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            await LLM(configured(tmp_path, api_style=api_style, reasoning_effort="low", output_tokens=16384), client).ask("提取", {}, Extraction)
    asyncio.run(run())


def test_relation_batch_splits_on_output_limit_without_truncating_context(tmp_path):
    class SplittingMock(ModelMock):
        def __call__(self, request):
            body = json.loads(request.content)
            payload = json.loads(body["input"].split("\nINPUT_DATA:\n")[1])
            if "source_ids" in payload and len(payload["source_ids"]) > 1:
                assert payload["full_document"] == DOCUMENT
                return httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []})
            return super().__call__(request)
    async def run():
        store = Store(tmp_path)
        mock = SplittingMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            pipeline = Pipeline(configured(tmp_path, relation_batch_size=3), store, client)
            job = store.create("test.md", DOCUMENT.encode())
            await pipeline.run(job["job_id"])
            assert store.get(job["job_id"])["status"] == "completed"
            graph = store.read(job["job_id"], "graph.json")
            assert len(graph["concepts"]) == 3 and len(graph["relations"]) == 1
            assert graph["validation"]["global_context_calls"] == 6
            assert mock.full_documents == [DOCUMENT] * 4
    asyncio.run(run())


def test_stage_order_and_idempotency(tmp_path):
    async def run():
        store = Store(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(ModelMock())) as client:
            pipeline = Pipeline(configured(tmp_path), store, client)
            job = store.create("a.md", DOCUMENT.encode())
            with pytest.raises(StageConflict):
                await pipeline.stage(job["job_id"], "relate")
            first = await pipeline.stage(job["job_id"], "parse")
            second = await pipeline.stage(job["job_id"], "parse")
            assert first["completed_stages"] == second["completed_stages"] == ["parse"]
    asyncio.run(run())


def test_auth_upload_origin_and_size(tmp_path):
    app = create_app(configured(tmp_path, max_upload_mb=1))
    with TestClient(app) as client:
        assert client.get("/api/jobs").status_code == 401
        client.headers["X-API-Key"] = "service-test-key"
        assert client.get("/api/jobs", headers={"Origin": "https://other.test"}).status_code == 403
        assert client.post("/api/jobs", files={"file": ("empty.md", b"")}).status_code == 400
        assert client.post("/api/jobs", files={"file": ("bad.exe", b"hello")}).status_code == 415
        assert client.post("/api/jobs", files={"file": ("big.md", b"x"*(1024*1024+1))}).status_code == 413
        assert client.post("/api/jobs", files={"file": ("good.md", DOCUMENT.encode())}).status_code == 201
        assert client.get("/api/jobs/missing").status_code == 404


def test_api_stages_exports_review_and_n8n(tmp_path):
    mock = ModelMock()
    app = create_app(configured(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(mock)))
    with TestClient(app) as client:
        client.headers["X-API-Key"] = "service-test-key"
        job_id = client.post("/api/jobs", files={"file": ("sample.md", DOCUMENT.encode())}).json()["job_id"]
        assert client.post(f"/api/jobs/{job_id}/launch", json={"mode": "n8n"}).status_code == 202
        assert client.post(f"/api/jobs/{job_id}/launch", json={"mode": "n8n"}).status_code == 409
        for stage in ("parse", "extract", "relate", "render"):
            assert client.post(f"/api/jobs/{job_id}/stages/{stage}").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/document").text == DOCUMENT
        graph = client.get(f"/api/jobs/{job_id}/graph").json()
        assert len(graph["relations"]) == 1
        for format in ("svg", "drawio", "mmd", "json"):
            assert client.get(f"/api/jobs/{job_id}/export/{format}").status_code == 200
        assert client.patch(f"/api/jobs/{job_id}/relations/r1", json={"status": "rejected"}).status_code == 200
        exported = ET.fromstring(client.get(f"/api/jobs/{job_id}/export/drawio").text)
        assert not exported.findall(".//mxCell[@edge='1']")
        assert len(exported.findall(".//mxCell[@vertex='1']")) == 3
        assert client.get(f"/api/jobs/{job_id}/graph").json()["relations"][0]["review"] == "rejected"


def test_model_settings_persist_without_returning_secrets(tmp_path):
    app = create_app(configured(tmp_path))
    with TestClient(app) as client:
        client.headers["X-API-Key"] = "service-test-key"
        assert "model-secret" not in client.get("/api/settings/model").text
        payload = dict(base_url="https://api.openai.com/v1", model="gpt-test", api_style="responses", api_key="sk-test-only")
        result = client.put("/api/settings/model", json=payload)
        assert result.status_code == 200
        assert "sk-test-only" not in result.text
        payload["api_key"] = None
        payload["model"] = "gpt-updated"
        assert client.put("/api/settings/model", json=payload).status_code == 200
        payload["base_url"] = "https://another.test/v1"
        assert client.put("/api/settings/model", json=payload).status_code == 400
    with TestClient(create_app(configured(tmp_path))) as client:
        client.headers["X-API-Key"] = "service-test-key"
        result = client.get("/api/settings/model").json()
        assert result["model"] == "gpt-updated" and result["key_configured"]


def test_runtime_model_edit_blocked_during_processing(tmp_path):
    app = create_app(configured(tmp_path))
    with TestClient(app) as client:
        job = app.state.store.create("test.md", b"content")
        job["status"] = "relate"
        app.state.store.save(job)
        client.headers["X-API-Key"] = "service-test-key"
        response = client.put("/api/settings/model", json={"base_url":"https://model.test/v1", "model":"new"})
        assert response.status_code == 409


def test_failures_are_persisted_without_leaking_provider_body(tmp_path):
    async def run():
        store = Store(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401, text="sensitive-body"))) as client:
            pipeline = Pipeline(configured(tmp_path), store, client)
            job = store.create("a.md", DOCUMENT.encode())
            await pipeline.run(job["job_id"])
            failed = store.get(job["job_id"])
            assert failed["status"] == "failed"
            assert "HTTP 401" in failed["error"] and "sensitive-body" not in failed["error"]
    asyncio.run(run())


def test_document_budget_and_partial_conversion_fail_closed(tmp_path):
    async def run():
        store = Store(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "partial_success", "document": {"md_content": DOCUMENT}}))) as client:
            pipeline = Pipeline(configured(tmp_path, max_document_chars=5), store, client)
            for name in ("a.md", "a.pdf"):
                job = store.create(name, DOCUMENT.encode())
                with pytest.raises(PipelineError):
                    await pipeline.stage(job["job_id"], "parse")
                assert store.get(job["job_id"])["status"] == "failed"
    asyncio.run(run())


def test_deployment_workflow_and_demo_contract(tmp_path):
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"concept-api", "n8n", "docling"}
    assert all(service["restart"] == "unless-stopped" for service in compose["services"].values())
    workflow = json.loads((ROOT / "n8n/workflow.json").read_text(encoding="utf-8"))
    requests = [node for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
    assert len(requests) == 4
    assert all("http://concept-api:8091/api/jobs/" in node["parameters"]["url"] for node in requests)
    graph = json.loads((ROOT / "concept_relation/fixtures/demo.json").read_text(encoding="utf-8"))
    document = (ROOT / "concept_relation/fixtures/demo.md").read_text(encoding="utf-8")
    for entry in graph["concepts"] + graph["relations"]:
        for evidence in entry["evidence"]:
            assert document[evidence["start"]:evidence["end"]] == evidence["quote"]
    with TestClient(create_app(configured(tmp_path))) as client:
        for format in ("svg", "drawio", "json", "mmd"):
            assert client.get(f"/api/demo/export/{format}").status_code == 200
        assert client.get("/").status_code == 200
