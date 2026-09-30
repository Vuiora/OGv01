import hmac
import importlib.util
import json
import shutil
import zipfile
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from clc.config import Settings
from clc.models import TextRequest
from clc.store import Store

ALLOWED = {".md", ".txt", ".pdf", ".docx"}


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="请求体超过上传限制")


class BodyLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.limit:
                raise BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            await JSONResponse({"detail": "请求体超过上传限制"}, status_code=413)(scope, receive, send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    app = FastAPI(title="CLC 知识层级构造服务", version="0.1.0")
    app.state.store, app.state.settings = store, settings
    app.add_middleware(BodyLimit, limit=settings.max_upload_mb * 1024 * 1024 + 1024 * 1024)
    web_dir = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    @app.middleware("http")
    async def prevent_stale_workbench(request: Request, call_next):
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        return result

    browser_token = hmac.new(settings.api_key.encode(), b"clc-browser-session-v1", "sha256").hexdigest()

    def auto_auth(request: Request):
        return settings.web_auto_auth and request.url.hostname in {"localhost", "127.0.0.1", "::1"}

    @app.get("/", include_in_schema=False)
    def home(request: Request):
        page = FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-store"})
        if auto_auth(request) and settings.api_key:
            page.set_cookie("clc_session", browser_token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        return page

    def authorize(request: Request, x_api_key: str = Header(default="")):
        if not settings.api_key or hmac.compare_digest(x_api_key.encode(), settings.api_key.encode()):
            return
        if auto_auth(request) and hmac.compare_digest(
            request.cookies.get("clc_session", "").encode(), browser_token.encode()
        ):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "浏览器请求来源无效")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not origin:
                raise HTTPException(403, "浏览器写入请求缺少来源")
            return
        raise HTTPException(401, "X-API-Key 无效")

    auth = [Depends(authorize)]

    def response(job):
        job_id = job["id"]
        base = f"/v1/jobs/{job_id}"
        files = []
        if job["status"] == "succeeded":
            root = store.job_dir(job_id) / "output"
            files = [{"path": p.relative_to(root).as_posix(),
                      "url": base + "/files/" + p.relative_to(root).as_posix()}
                     for p in sorted(root.rglob("*")) if p.is_file()]
        return {"id": job_id, "status": job["status"], "stage": job["stage"], "error": job["error"],
                "created_at": job["created"], "updated_at": job["updated"],
                "status_url": base, "bundle_url": base + "/bundle", "artifacts": files}

    def get_job(job_id):
        job = store.get(str(job_id))
        if not job:
            raise HTTPException(404, "任务不存在")
        return job

    def require_success(job_id):
        job = get_job(job_id)
        if job["status"] != "succeeded":
            raise HTTPException(409, "任务尚未成功完成")
        return store.job_dir(str(job_id))

    @app.get("/health")
    def health(request: Request):
        return {"status": "ok", "llm_configured": bool(settings.llm_model),
                "docling_installed": importlib.util.find_spec("docling") is not None,
                "auth_required": bool(settings.api_key),
                "web_auto_auth": auto_auth(request),
                "limits": {"files": settings.max_files, "upload_mb": settings.max_upload_mb,
                           "source_chars": settings.max_source_chars}}

    @app.post("/v1/jobs/text", status_code=202, dependencies=auth)
    def submit_text(body: TextRequest):
        if not body.text.strip():
            raise HTTPException(422, "文字不能为空")
        if len(body.text) > settings.max_source_chars:
            raise HTTPException(413, "文字超限")
        job_id = str(uuid4())
        folder = store.job_dir(job_id) / "input"
        folder.mkdir(parents=True)
        (folder / "s001.md").write_text(body.text, encoding="utf-8")
        store.enqueue(job_id, {"title": body.title, "formats": sorted(set(body.formats) | {"md"}),
                               "sources": [{"id": "s001", "name": "input.md", "stored_name": "s001.md"}]})
        return response(store.get(job_id))

    @app.post("/v1/jobs", status_code=202, dependencies=auth)
    async def submit_files(files: Annotated[list[UploadFile], File()], title: str = Form("知识层级文档"),
                           formats: str = Form("md,docx,pdf")):
        selected = set(formats.split(","))
        if not selected or not selected <= {"md", "docx", "pdf"} or not 1 <= len(title.strip()) <= 160:
            raise HTTPException(422, "formats 必须是 md,docx,pdf 的组合，title 长度为 1 至 160")
        if len(files) > settings.max_files:
            raise HTTPException(413, "文件数量超限")
        job_id = str(uuid4())
        folder = store.job_dir(job_id) / "input"
        folder.mkdir(parents=True)
        total, sources = 0, []
        try:
            for index, upload in enumerate(files, 1):
                name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                suffix = Path(name).suffix.lower()
                if suffix not in ALLOWED:
                    raise HTTPException(415, "支持 .md/.txt/.pdf/.docx；旧 .doc 请先另存为 .docx")
                source_id = f"s{index:03d}"
                stored = source_id + suffix
                path = folder / stored
                with path.open("wb") as stream:
                    while block := await upload.read(1024 * 1024):
                        total += len(block)
                        if total > settings.max_upload_mb * 1024 * 1024:
                            raise HTTPException(413, "上传总大小超限")
                        stream.write(block)
                if path.stat().st_size == 0:
                    raise HTTPException(422, "不支持空文件")
                if suffix == ".pdf":
                    with path.open("rb") as stream:
                        if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                            raise HTTPException(422, "PDF 文件头无效")
                if suffix == ".docx":
                    try:
                        with zipfile.ZipFile(path) as archive:
                            infos = archive.infolist()
                            if "word/document.xml" not in archive.namelist():
                                raise ValueError()
                            if len(infos) > 5000 or sum(i.file_size for i in infos) > 200 * 1024 * 1024:
                                raise ValueError()
                    except (ValueError, zipfile.BadZipFile):
                        raise HTTPException(422, "DOCX 结构无效或解压体积超限") from None
                sources.append({"id": source_id, "name": name[:255], "stored_name": stored})
            store.enqueue(job_id, {"title": title.strip(), "formats": sorted(selected | {"md"}), "sources": sources})
        except Exception:
            shutil.rmtree(store.job_dir(job_id))
            raise
        finally:
            for upload in files:
                await upload.close()
        return response(store.get(job_id))

    @app.get("/v1/jobs/{job_id}", dependencies=auth)
    def status(job_id: UUID):
        return response(get_job(job_id))

    @app.get("/v1/jobs/{job_id}/graph", dependencies=auth)
    def graph(job_id: UUID):
        root = require_success(job_id)
        return json.loads((root / "output" / "graph.json").read_text(encoding="utf-8"))

    @app.get("/v1/jobs/{job_id}/bundle", dependencies=auth)
    def bundle(job_id: UUID):
        return FileResponse(require_success(job_id) / "bundle.zip", filename="knowledge.zip")

    @app.get("/v1/jobs/{job_id}/files/{file_path:path}", dependencies=auth)
    def artifact(job_id: UUID, file_path: str):
        root = (require_success(job_id) / "output").resolve()
        path = (root / file_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(path, filename=path.name)

    return app
