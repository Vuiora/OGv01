"""Read source as source; use Docling for document structure, never execute input."""
import ast
import base64
from hashlib import sha256
from pathlib import Path
import os

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "data", "dist", "build", "archive-plda-functional-pages"}
CODE = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".cs", ".sh", ".ps1", ".toml"}
DOCS = {".pdf", ".docx", ".pptx", ".md", ".html", ".xlsx", ".png", ".jpg"}


def within(root: Path, name: str) -> Path:
    candidate = Path(name).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not path.exists():
        raise ValueError(f"input path does not exist: {name}")
    return path


def collect(root: Path, repository: str, max_chars: int) -> dict:
    repo = within(root, repository)
    if not repo.is_dir():
        raise ValueError("repository must be a directory")
    source_root = repo if Path(repository).expanduser().is_absolute() else root.resolve()
    sources, ignored, total = [], [], 0
    for directory, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".") and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() not in CODE or name.startswith(".") or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(source_root):
                continue
            relative = path.relative_to(source_root).as_posix()
            if Path(repository).expanduser().is_absolute():
                relative = f"{repo.name}/{relative}"
            if path.stat().st_size > 256_000:
                ignored.append({"path": relative, "reason": "file exceeds 256 KB"})
                continue
            try:
                content = path.read_text(encoding="utf-8-sig")
            except (UnicodeError, OSError):
                ignored.append({"path": relative, "reason": "not readable UTF-8"})
                continue
            total += len(content)
            if total > max_chars or len(sources) >= 300:
                raise ValueError("source budget exceeded; submit a narrower repository directory")
            symbols = []
            if path.suffix == ".py":
                try:
                    for item in ast.walk(ast.parse(content)):
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            symbols.append({"name": item.name, "line": item.lineno, "kind": type(item).__name__})
                except SyntaxError:
                    ignored.append({"path": relative, "reason": "AST unavailable; text still included"})
            sources.append({"path": relative, "sha256": sha256(content.encode()).hexdigest(), "content": content, "symbols": symbols})
    if not sources:
        raise ValueError("no supported source files found")
    return {"repository": repository, "sources": sources, "omissions": ignored, "characters": total}


def chunks(sources, limit):
    current = ""
    for source in sources:
        for number, line in enumerate(source["content"].splitlines(), 1):
            item = f"{source['path']}:{number}: {line}\n"
            if len(item) > limit:
                raise ValueError(f"source line exceeds chunk budget: {source['path']}:{number}")
            if current and len(current)+len(item) > limit:
                yield current
                current = ""
            current += item
    if current:
        yield current


async def convert_document(client, settings, relative):
    path = within(settings.input_root, relative)
    if path.suffix.lower() not in DOCS or not path.is_file():
        raise ValueError("unsupported document type")
    if path.stat().st_size > 20*1024*1024:
        raise ValueError("document exceeds 20 MiB")
    data = path.read_bytes()
    # File input avoids sending arbitrary fetched URLs to Docling.
    response = await client.post(settings.docling_url+"/v1/convert/source", headers={"X-Api-Key": settings.docling_key},
        json={"sources": [{"kind": "file", "base64_string": base64.b64encode(data).decode(), "filename": path.name}],
              "options": {"to_formats": ["md"], "image_export_mode": "placeholder", "do_ocr": True}}, timeout=300)
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success":
        raise ValueError("Docling conversion was not fully successful; inspect source document")
    markdown = body.get("document", {}).get("md_content")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("Docling did not return Markdown")
    if len(markdown) > settings.max_source_chars:
        raise ValueError("converted document exceeds text budget")
    return {"path": relative, "sha256": sha256(data).hexdigest(), "markdown": markdown, "converter": "docling-serve"}
