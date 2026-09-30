from contextlib import asynccontextmanager
import hmac
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from .config import FIXED_INSTRUCTIONS, Settings
from .models import JobRequest, Architecture
from .pipeline import Pipeline


def create_app(settings=None,client=None):
    settings=settings or Settings.from_env()
    pipeline=Pipeline(settings,client)
    @asynccontextmanager
    async def lifespan(app):
        yield
        if client is None:await pipeline.client.aclose()
    app=FastAPI(title="Architecture Flow · n8n + Docling",version="0.1.0",lifespan=lifespan)
    app.state.pipeline=pipeline
    def auth(x_api_key: str=Header(default="")):
        if not settings.api_key:raise HTTPException(503,"Set APP_API_KEY in local .env")
        if not hmac.compare_digest(x_api_key,settings.api_key):raise HTTPException(401,"Invalid API key")
    @app.get("/health")
    def health():return {"status":"ok","api_configured":bool(settings.api_key),"llm_configured":bool(settings.analysis_model and settings.design_model and settings.analysis_url and settings.design_url)}
    @app.get("/")
    def ui():return FileResponse(Path(__file__).parent/"static/index.html")
    @app.get("/v1/client-config")
    def client_config():
        repository=settings.template.parent/"ParallelLogicDeterminationAlgorithModule"
        return {"api_key": settings.api_key, "input_root": str(settings.input_root),
                "default_repository": str(repository if repository.is_dir() else settings.input_root),
                "template": str(settings.template), "module_names": [], "instructions": FIXED_INSTRUCTIONS}
    @app.get("/v1/schema",dependencies=[Depends(auth)])
    def schema():return Architecture.model_json_schema()
    @app.post("/v1/jobs",status_code=202,dependencies=[Depends(auth)])
    def create(request:JobRequest,tasks:BackgroundTasks,run:bool=Query(default=False)):
        request=request.model_copy(update={"instructions": FIXED_INSTRUCTIONS})
        try:result=pipeline.create(request)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc
        if run:tasks.add_task(pipeline.run,result["job_id"])
        return result
    @app.post("/v1/launch",status_code=202,dependencies=[Depends(auth)])
    async def launch(request:JobRequest):
        request=request.model_copy(update={"instructions": FIXED_INSTRUCTIONS})
        try:
            response=await pipeline.client.post(settings.n8n_url,json=request.model_dump(),headers={"X-API-Key":settings.api_key},timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:raise HTTPException(502,"n8n webhook unavailable; import/publish workflow and configure Header Auth") from exc
    @app.get("/v1/jobs/{ident}",dependencies=[Depends(auth)])
    def status(ident:str):
        try:return pipeline.public(pipeline.read(ident))
        except (ValueError,FileNotFoundError) as exc:raise HTTPException(404,"Job not found") from exc
    @app.post("/v1/jobs/{ident}/cancel",dependencies=[Depends(auth)])
    async def cancel(ident:str):
        try:return await pipeline.cancel(ident)
        except (ValueError,FileNotFoundError) as exc:raise HTTPException(404,"Job not found") from exc
    @app.post("/v1/jobs/{ident}/stages/{stage}",dependencies=[Depends(auth)])
    async def advance(ident:str,stage:str):
        try:return await pipeline.stage(ident,stage)
        except FileNotFoundError as exc:raise HTTPException(404,"Job or stage input not found") from exc
        except Exception as exc:
            try:detail=pipeline.public(pipeline.read(ident))
            except Exception:detail={"error":"Invalid job or stage"}
            raise HTTPException(422,detail) from exc
    @app.get("/v1/jobs/{ident}/artifacts/{name}",dependencies=[Depends(auth)])
    def artifact(ident:str,name:str):
        allowed={"architecture.drawio","architecture.json","analysis.json","quality.json","style-profile.json"}
        if name not in allowed:raise HTTPException(404,"Artifact not found")
        try:
            r=pipeline.read(ident)
            if name=="architecture.drawio" and r["state"]!="SUCCEEDED":raise HTTPException(409,"Artifact not published")
            path=pipeline.directory(ident)/name
            if not path.is_file():raise HTTPException(404,"Artifact not ready")
            return FileResponse(path,filename=name,media_type="application/xml" if name.endswith(".drawio") else "application/json")
        except (ValueError,FileNotFoundError) as exc:raise HTTPException(404,"Job not found") from exc
    return app


app=create_app()
