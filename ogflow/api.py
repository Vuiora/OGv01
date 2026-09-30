"""Local authenticated task API and downloadable public artifacts."""
import hmac
import json
import os
import re
import threading
import uuid
from pathlib import Path
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from .bootstrap import ROOT
from .config import LLMConfig
from .mentor import build_application
from .models import TaskSpec

PUBLIC_FILES = {"run.json", "task.json", "application.json", "events.json", "protocol.json", "knowledge.json",
    "recommendation.json", "proposals.json", "validation.json", "frozen-plan.json", "confirmation-statistics.json",
    "discovery-archive.json", "knowledge-version.json", "open-questions.json", "feedback.json", "acquisition-plan.json"}
ARCH_FILES = {"architecture.drawio", "architecture.json", "quality.json", "production.json"}

def create_app(*, output=None, config=None, token=None, pipeline_factory=None):
    from .pipeline import BusinessPipeline
    settings = (config or LLMConfig.load()).apply()
    root = Path(output or ROOT / "runs" / "business").resolve()
    root.mkdir(parents=True, exist_ok=True)
    secret = token if token is not None else os.getenv("OG_API_TOKEN", "")
    pipeline = (pipeline_factory or BusinessPipeline)(root, config=settings)
    pending, capacity = {}, threading.Semaphore(1)
    application = FastAPI(title="OGv01 business closed loop", version="0.2.0")
    def auth(authorization: str = Header(default="")):
        if not secret:
            raise HTTPException(503, "Configure OG_API_TOKEN")
        if not hmac.compare_digest(authorization, "Bearer " + secret):
            raise HTTPException(401, "Unauthorized")
    def directory(run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise HTTPException(404, "Run not found")
        return root / run_id
    def work(spec, ident):
        with capacity:
            try:
                pipeline.run(spec, run_id=ident)
            except Exception:
                pass  # Pipeline has already persisted a safe failure record.
            finally:
                pending.pop(ident, None)
    @application.get("/health")
    def health():
        return {"status": "ready", "llm": settings.public(), "authenticated": bool(secret)}
    @application.get("/v1/application", dependencies=[Depends(auth)])
    def application_contract():
        return build_application()
    @application.post("/v1/runs", status_code=202, dependencies=[Depends(auth)])
    def submit(spec: TaskSpec, background: BackgroundTasks):
        for filename in [spec.observations] + spec.documents:
            path = (ROOT / filename).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():
                raise HTTPException(400, "Task inputs must be existing files inside this checkout")
        if len(pending) >= 10:
            raise HTTPException(429, "Task queue is full")
        ident = uuid.uuid4().hex
        pending[ident] = {"run_id": ident, "state": "QUEUED"}
        background.add_task(work, spec, ident)
        return {"run_id": ident, "state": "QUEUED", "status_url": f"/v1/runs/{ident}"}
    @application.get("/v1/runs/{run_id}", dependencies=[Depends(auth)])
    def status(run_id: str):
        path = directory(run_id) / "run.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        if run_id in pending:
            return pending[run_id]
        raise HTTPException(404, "Run not found")
    @application.get("/v1/runs/{run_id}/artifacts/{filename}", dependencies=[Depends(auth)])
    def artifact(run_id: str, filename: str):
        folder = directory(run_id)
        if filename in PUBLIC_FILES:
            path = folder / filename
        elif filename in ARCH_FILES:
            path = folder / "architecture" / filename
        else:
            raise HTTPException(404, "Artifact not found")
        if not path.is_file():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, filename=filename)
    return application

app = create_app()
