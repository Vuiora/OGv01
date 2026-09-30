"""CLC hierarchy and CRD relations share one parsed document representation."""
import asyncio
import hashlib
import shutil
import json
import subprocess
from pathlib import Path
import httpx
from clc.build import build_graph
from clc.config import Settings as CLCSettings
from clc.parse import parse_document
from concept_relation.config import Settings as CRDSettings
from concept_relation.pipeline import Pipeline as CRDPipeline
from concept_relation.store import Store as CRDStore
from .bootstrap import ROOT

def validate_document_structure(text):
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Document input requires Node.js for the n8n-Docling structure bridge")
    script = ROOT / "F-20260914-Utils/n8n-Docling/normalize-document.js"
    result = subprocess.run([node, str(script)], input=json.dumps({"text": text}, ensure_ascii=False),
        encoding="utf-8", capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("n8n-Docling document structure validation failed")
    return json.loads(result.stdout)

def collect_knowledge(paths, directory: Path, config, progress=lambda stage: None):
    if not paths:
        return {"sources": [], "concepts": [], "relations": [], "note": "No document context supplied"}
    directory.mkdir(parents=True, exist_ok=True)
    job = directory / "clc"
    (job / "input").mkdir(parents=True, exist_ok=True)
    sources, texts = [], []
    settings = CLCSettings(llm_base_url=config.base_url, llm_model=config.model,
        llm_api_key=config.api_key, llm_timeout=config.timeout, max_concepts=60)
    for index, path in enumerate(paths, 1):
        if path.suffix.lower() not in {".md", ".txt", ".pdf", ".docx"}:
            raise ValueError("Knowledge input supports Markdown, TXT, PDF and DOCX")
        # Parsing is shared. Docling is used by CLC for PDF/DOCX; downstream CRD consumes Markdown.
        text = parse_document(path, settings)
        structure = validate_document_structure(text)
        source_id = f"source-{index}"
        stored = source_id + ".md"
        (job / "input" / stored).write_text(text, encoding="utf-8")
        sources.append({"id": source_id, "name": path.name, "stored_name": stored,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "structure": structure})
        texts.append(text)
    graph = build_graph("Business observation context", sources, job, settings, progress=progress)
    progress("relations")
    async def relations():
        crd_store = CRDStore(directory / "crd")
        crd_settings = CRDSettings(data_root=directory / "crd", llm_url=config.base_url,
            llm_model=config.model, llm_key=config.api_key, api_style="chat",
            llm_timeout=int(config.timeout), max_key_concepts=10, relation_batch_size=3, max_relations_per_concept=2)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            pipeline = CRDPipeline(crd_settings, crd_store, client)
            outputs = []
            for source, text in zip(sources, texts):
                task = crd_store.create(source["name"] + ".md", text.encode())
                await pipeline.run(task["job_id"])
                state = crd_store.get(task["job_id"])
                if state["status"] != "completed":
                    raise RuntimeError("CRD knowledge processing failed")
                outputs.append(crd_store.read(task["job_id"], "graph.json"))
            return outputs
    relation_graphs = asyncio.run(relations())
    return {"sources": sources, "hierarchy": graph.model_dump(), "relation_graphs": relation_graphs,
        "evidence_purpose": "context_only", "note": "Document claims provide exploration context, not independent statistical confirmation"}

