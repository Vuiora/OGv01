import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import exports
from .config import Settings
from .llm import PipelineError
from .layout import graph_layout
from .models import LaunchRequest, ReviewRequest, ModelConfig
from .pipeline import Pipeline, StageConflict
from .store import Store, now

PACKAGE = Path(__file__).parent
ALLOWED = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}


def create_app(settings=None, client=None):
    settings = settings or Settings.from_env()
    runtime_path = settings.data_root / "model-config.json"
    if runtime_path.exists():
        saved = ModelConfig.model_validate_json(runtime_path.read_text(encoding="utf-8"))
        settings = replace(settings, llm_url=saved.base_url, llm_model=saved.model,
                           llm_key=saved.api_key or "", api_style=saved.api_style)
    store = Store(settings.data_root)
    client = client or httpx.AsyncClient()
    pipeline = Pipeline(settings, store, client)
    tasks = set()

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()

    app = FastAPI(title="ConceptRelationDraw", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.pipeline = store, pipeline

    @app.middleware("http")
    async def bounds(request, call_next):
        if request.url.path == "/api/jobs" and request.method == "POST":
            limit = settings.max_upload_mb * 1024 * 1024 + 65536
            received = 0
            original_receive = request._receive
            async def limited_receive():
                nonlocal received
                message = await original_receive()
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "上传文件超过大小限制")
                return message
            request._receive = limited_receive
            try:
                if int(request.headers.get("content-length", "0")) > limit:
                    return JSONResponse({"detail": "上传文件超过大小限制"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效的 Content-Length"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    async def auth(request: Request):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "仅允许同源请求")
        if settings.app_key and not hmac.compare_digest(request.headers.get("X-API-Key", ""), settings.app_key):
            raise HTTPException(401, "请在连接设置中输入 APP_API_KEY")

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "任务不存在"}, status_code=404)

    @app.exception_handler(StageConflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PipelineError)
    async def failure(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/config")
    async def config():
        return dict(auth_required=bool(settings.app_key), model_configured=settings.model_ready(),
                    max_upload_mb=settings.max_upload_mb, max_document_chars=settings.max_document_chars)

    @app.get("/api/settings/model", dependencies=[Depends(auth)])
    async def model_settings():
        return dict(base_url=settings.llm_url, model=settings.llm_model, api_style=settings.api_style,
                    key_configured=bool(settings.llm_key), configured=settings.model_ready())

    @app.put("/api/settings/model", dependencies=[Depends(auth)])
    async def save_model(body: ModelConfig):
        nonlocal settings
        if not settings.app_key:
            raise HTTPException(403, "服务必须配置 APP_API_KEY 才能从网页修改模型设置。")
        if store.has_active_jobs():
            raise HTTPException(409, "有任务正在处理，完成后再修改模型设置。")
        if settings.llm_key and urlparse(body.base_url).netloc != urlparse(settings.llm_url).netloc and not body.api_key:
            raise HTTPException(400, "更换模型服务地址时，请重新输入该服务的 API Key。")
        new_key = body.api_key if body.api_key else settings.llm_key
        new_settings = replace(settings, llm_url=body.base_url, llm_model=body.model, llm_key=new_key, api_style=body.api_style)
        if not new_settings.model_ready():
            raise HTTPException(400, "OpenAI 官方接口需要填写 API Key。")
        saved = body.model_copy(update={"api_key": new_key})
        temp = runtime_path.with_suffix(".tmp")
        temp.write_text(saved.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(runtime_path)
        settings = new_settings
        pipeline.settings = settings
        pipeline.llm.settings = settings
        return await model_settings()

    @app.get("/api/services", dependencies=[Depends(auth)])
    async def services():
        async def check(name, url):
            try:
                response = await client.get(url, timeout=3)
                return {"name": name, "status": "online" if response.is_success else "unavailable"}
            except httpx.HTTPError:
                return {"name": name, "status": "unavailable"}
        n8n = urlparse(settings.n8n_url)
        results = await asyncio.gather(check("n8n", f"{n8n.scheme}://{n8n.netloc}/healthz/readiness"),
                                       check("Docling", settings.docling_url + "/health"))
        return {"services": [{"name": "Web / API", "status": "online"}, *results], "model_configured": settings.model_ready()}

    @app.get("/api/demo")
    async def demo():
        graph = json.loads((PACKAGE / "fixtures/demo.json").read_text(encoding="utf-8"))
        graph["layout"] = graph_layout(graph)
        return graph

    @app.get("/api/demo/document")
    async def demo_document():
        return Response((PACKAGE / "fixtures/demo.md").read_text(encoding="utf-8"), media_type="text/plain")

    @app.get("/api/demo/export/{format}")
    async def demo_export(format: str, view: Literal["backbone", "all"] = "backbone"):
        graph = json.loads((PACKAGE / "fixtures/demo.json").read_text(encoding="utf-8"))
        return export_response(graph, format, view)

    @app.get("/api/jobs", dependencies=[Depends(auth)])
    async def jobs():
        return store.list()

    @app.post("/api/jobs", status_code=201, dependencies=[Depends(auth)])
    async def upload(file: UploadFile = File(...)):
        filename = Path((file.filename or "document").replace("\\", "/")).name
        if Path(filename).suffix.lower() not in ALLOWED:
            raise HTTPException(415, "不支持此文件类型；请使用 PDF、DOCX、PPTX、XLSX、HTML、Markdown、TXT 或图片。")
        data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        await file.close()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "上传文件超过大小限制")
        if not data:
            raise HTTPException(400, "文档为空")
        return store.create(filename, data)

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    async def job(job_id: str):
        return store.get(job_id)

    @app.post("/api/jobs/{job_id}/reanalyze", status_code=201, dependencies=[Depends(auth)])
    async def reanalyze(job_id: str):
        original = store.get(job_id)
        if original["status"] != "completed":
            raise StageConflict("请等待原任务完成后重新提取；失败任务可使用重试。")
        document = store.path(job_id, "document.md").read_text(encoding="utf-8")
        if len(document) > settings.max_document_chars:
            raise HTTPException(413, "文档超过当前完整上下文预算；没有截断或提交模型。")
        created = store.create(original["filename"], store.path(job_id, "source").read_bytes())
        store.write(created["job_id"], "document.md", document)
        store.write(created["job_id"], "parsed.json", store.read(job_id, "parsed.json"))
        created.update(completed_stages=["parse"], progress=15, parent_job_id=job_id,
                       message="复用原文解析，等待重新提取重要概念")
        store.save(created)
        return created

    @app.post("/api/jobs/{job_id}/launch", status_code=202, dependencies=[Depends(auth)])
    async def launch(job_id: str, body: LaunchRequest):
        job = store.get(job_id)
        if job["status"] != "uploaded":
            raise StageConflict("任务已启动；重复请求不会再次计费执行")
        if not settings.model_ready():
            raise HTTPException(503, "请先在“服务配置”中保存模型接口和 API Key。")
        job.update(status="queued", message="等待工作流调度", mode=body.mode)
        store.save(job)
        if body.mode == "direct":
            task = asyncio.create_task(pipeline.run(job_id))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        else:
            try:
                response = await client.post(settings.n8n_url, headers={"X-API-Key": settings.app_key}, json={"job_id": job_id}, timeout=20)
                response.raise_for_status()
                try:
                    acknowledgement = response.json()
                except ValueError as exc:
                    raise PipelineError("n8n 未返回有效的任务确认，请检查“立即确认任务”节点的 JSON 响应设置。") from exc
                if not isinstance(acknowledgement, dict) or acknowledgement.get("accepted") is not True or acknowledgement.get("job_id") != job_id:
                    raise PipelineError("n8n 返回的任务确认不匹配，请重新导入并发布本项目工作流。")
            except httpx.TimeoutException:
                current = store.get(job_id)
                if current["status"] == "queued":
                    current.update(message="n8n 响应超时，任务可能已接收；正在继续查询进度，请勿重复提交。")
                    store.save(current)
                return current
            except (httpx.HTTPError, PipelineError) as exc:
                if isinstance(exc, PipelineError):
                    message = str(exc)
                elif isinstance(exc, httpx.ConnectError):
                    message = "无法连接 n8n 服务。请运行 start-service.ps1 启动全部服务，并在“服务配置”中确认 n8n 在线。"
                elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                    message = "n8n 已连接，但工作流 Webhook 未注册。请导入并发布 ConceptRelationDraw 工作流，使用生产 Webhook 地址。"
                elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                    message = "n8n 拒绝认证。请将 Webhook 的 Header Auth 设置为 X-API-Key，并使用本项目的 APP_API_KEY。"
                elif isinstance(exc, httpx.HTTPStatusError):
                    message = f"n8n 返回 HTTP {exc.response.status_code}，请检查 n8n 执行记录与服务日志。"
                else:
                    message = "n8n 网络请求失败，请检查服务地址及网络连接。"
                current = store.get(job_id)
                if current["status"] == "queued":
                    current.update(status="failed", error=message, message="调度失败")
                    store.save(current)
                raise PipelineError(message) from exc
        return store.get(job_id)

    @app.post("/api/jobs/{job_id}/stages/{stage}", dependencies=[Depends(auth)])
    async def stage(job_id: str, stage: str):
        return await pipeline.stage(job_id, stage)

    @app.post("/api/jobs/{job_id}/retry", status_code=202, dependencies=[Depends(auth)])
    async def retry(job_id: str, body: LaunchRequest):
        job = store.get(job_id)
        if job["status"] != "failed":
            raise StageConflict("只有失败任务可以重试，正在运行的任务不会重复提交。")
        job.setdefault("previous_failures", []).append({"error": job["error"], "at": job["updated_at"], "retried_at": now()})
        job.update(status="uploaded", error=None, message="准备重试，保留已完成阶段")
        store.save(job)
        return await launch(job_id, body)

    def ready(job_id):
        if store.get(job_id)["status"] != "completed":
            raise HTTPException(409, "任务尚未完成")
        graph = store.read(job_id, "graph.json")
        graph["layout"] = graph_layout(graph)
        return graph

    @app.get("/api/jobs/{job_id}/graph", dependencies=[Depends(auth)])
    async def graph(job_id: str):
        return ready(job_id)

    @app.get("/api/jobs/{job_id}/document", dependencies=[Depends(auth)])
    async def document(job_id: str):
        path = store.path(job_id, "document.md")
        if not path.exists():
            raise HTTPException(409, "文档尚未解析")
        return Response(path.read_text(encoding="utf-8"), media_type="text/plain")

    @app.patch("/api/jobs/{job_id}/relations/{relation_id}", dependencies=[Depends(auth)])
    async def review(job_id: str, relation_id: str, body: ReviewRequest):
        graph = ready(job_id)
        relation = next((r for r in graph["relations"] if r["id"] == relation_id), None)
        if relation is None:
            raise HTTPException(404, "关系不存在")
        relation["review"] = body.status
        graph["layout"] = graph_layout(graph)
        store.write(job_id, "graph.json", graph)
        return relation

    @app.get("/api/jobs/{job_id}/export/{format}", dependencies=[Depends(auth)])
    async def export(job_id: str, format: str, view: Literal["backbone", "all"] = "backbone"):
        return export_response(ready(job_id), format, view)

    def export_response(graph, format, view):
        graph["layout"] = graph_layout(graph)
        if format == "json":
            content, mime = json.dumps(graph, ensure_ascii=False, indent=2), "application/json"
        elif format in ("svg", "drawio", "mmd"):
            content = {"svg": exports.svg, "drawio": exports.drawio, "mmd": exports.mermaid}[format](graph, view)
            mime = {"svg": "image/svg+xml", "drawio": "application/xml", "mmd": "text/plain"}[format]
        else:
            raise HTTPException(404, "支持 json / svg / drawio / mmd")
        return Response(content, media_type=mime, headers={"Content-Disposition": f'attachment; filename="concept-map.{format}"'})

    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    @app.get("/")
    async def index():
        return FileResponse(PACKAGE / "static/index.html")

    return app
