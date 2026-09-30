"""Real n8n -> HTTP API -> real Docling smoke, with an explicitly fake LLM.

prepare writes isolated n8n imports; serve starts only the smoke API/provider;
verify calls the published webhook through /v1/launch and checks the artifact.
Never loads production .env or sends source to an external model.
"""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import time
import xml.etree.ElementTree as ET

import httpx
import uvicorn
from architecture_flow.api import create_app
from architecture_flow.config import Settings
from architecture_flow.ingest import convert_document

BASE = Path(__file__).resolve().parents[1]
STATE = BASE / ".n8n-test"


def prepare():
    STATE.mkdir(exist_ok=True)
    config_path = STATE / "smoke-config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps({"api_key": secrets.token_hex(32)}), encoding="utf-8")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    credentials = [{"id": "ArchitectureSmokeAuth", "name": "Architecture smoke only", "type": "httpHeaderAuth",
                    "data": {"name": "X-API-Key", "value": config["api_key"]}}]
    (STATE / "smoke-credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
    flow = json.loads((BASE / "n8n/workflow.json").read_text(encoding="utf-8"))
    flow["id"] = "ArchitectureFlowSmoke"
    flow["name"] = "LOCAL SMOKE - fake LLM, real n8n and Docling"
    for node in flow["nodes"]:
        if "url" in node["parameters"]:
            node["parameters"]["url"] = node["parameters"]["url"].replace("architecture-api:8080", "127.0.0.1:18080")
        if "credentials" in node:
            node["credentials"] = {"httpHeaderAuth": {"id": "ArchitectureSmokeAuth", "name": "Architecture smoke only"}}
    (STATE / "smoke-workflow.json").write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")
    print("Prepared isolated smoke imports (credentials are not printed).")


def settings():
    config = json.loads((STATE / "smoke-config.json").read_text(encoding="utf-8"))
    return Settings(input_root=BASE.parent, template=BASE.parent / "TheStructure.drawio", data_root=BASE / "data/integration",
                    api_key=config["api_key"], analysis_url="http://127.0.0.1:18080/smoke/v1", analysis_model="FAKE-analysis",
                    analysis_key="fake", design_url="http://127.0.0.1:18080/smoke/v1", design_model="FAKE-design", design_key="fake",
                    docling_url="http://127.0.0.1:15001", n8n_url="http://127.0.0.1:15678/webhook/drawio-architecture")


def serve():
    app = create_app(settings(), httpx.AsyncClient(trust_env=False))
    @app.post("/smoke/v1/chat/completions")
    async def fake_model(body: dict):
        if body["model"] == "FAKE-analysis":
            answer = {"summary": "SIMULATED analysis for plumbing validation only.", "responsibilities": [], "unknowns": ["No real model used."]}
        else:
            answer = json.loads((BASE / "examples/workflow-architecture.json").read_text(encoding="utf-8"))
        return {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}, "finish_reason": "stop"}], "usage": {"total_tokens": 0}}
    uvicorn.run(app, host="127.0.0.1", port=18080)


async def docling():
    async with httpx.AsyncClient(trust_env=False) as client:
        converted = await convert_document(client, settings(), "ParallelLogicDeterminationAlgorithModule/docs/DESIGN.md")
    (BASE / "data/docling-smoke.md").write_text(converted["markdown"], encoding="utf-8")
    print(json.dumps({"converter": converted["converter"], "path": converted["path"], "characters": len(converted["markdown"])}))


def verify():
    cfg = settings()
    with httpx.Client(base_url="http://127.0.0.1:18080", headers={"X-API-Key": cfg.api_key}, trust_env=False, timeout=40) as client:
        request = {"repository": "ThePicWorkingFlow/architecture_flow", "documents": ["ParallelLogicDeterminationAlgorithModule/docs/DESIGN.md"],
                   "instructions": "Integration plumbing smoke; model output is deliberately a proposed fixture, not a real analysis.", "preserve_page_ids": []}
        start = time.monotonic()
        created = client.post("/v1/launch", json=request)
        created.raise_for_status()
        ident = created.json()["job_id"]
        accepted_s = time.monotonic() - start
        deadline = start + 240
        while time.monotonic() < deadline:
            response = client.get("/v1/jobs/" + ident)
            response.raise_for_status()
            status = response.json()
            if status["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "CANCELLED"}:
                break
            time.sleep(1)
        assert status["state"] == "SUCCEEDED", status
        result = client.get(status["artifacts"]["drawio"])
        result.raise_for_status()
        downloads = ["drawio"]
        for name, path in status["artifacts"].items():
            if name in {"drawio", "sha256"}: continue
            artifact = client.get(path)
            artifact.raise_for_status()
            assert isinstance(artifact.json(), dict)
            downloads.append(name)
        pages = [p.get("name") for p in ET.fromstring(result.content).findall("diagram")]
        assert len(pages) == 5, pages
        (BASE / "examples/integration-smoke.drawio").write_bytes(result.content)
        report = {"status": "passed", "job_id": ident, "n8n": "2.38.7", "docling": "1.32.0", "llm": "FAKE local compatible HTTP provider",
                  "webhook_accepted_seconds": round(accepted_s, 3), "total_seconds": round(time.monotonic() - start, 3),
                  "pages": pages, "downloads": downloads, "quality_passed": status["quality_passed"], "stages": list(json.loads((cfg.data_root / "jobs" / ident / "job.json").read_text(encoding="utf-8"))["completed"])}
        (BASE / "examples/integration-smoke-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "serve", "docling", "verify"])
    action = parser.parse_args().command
    if action == "prepare": prepare()
    elif action == "serve": serve()
    elif action == "docling": asyncio.run(docling())
    else: verify()
