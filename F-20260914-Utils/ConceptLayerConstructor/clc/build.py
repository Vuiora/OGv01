import hashlib
import json
import unicodedata
from collections.abc import Callable
from pathlib import Path

from clc.config import Settings
from clc.llm import LLM
from clc.models import Extraction, Graph, Node, Outline
from clc.parse import Chunk, chunk_text, parse_document, validate_evidence


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def concept_id(concept) -> str:
    # Version/scope is part of identity: conflicting historical claims must not be silently collapsed.
    key = "|".join(unicodedata.normalize("NFKC", v).casefold().strip()
                   for v in (concept.title, concept.kind, concept.scope))
    return "k-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def assemble(title: str, nodes: list[Node], outline: Outline, chunks: dict[str, Chunk]) -> Graph:
    ids = {n.id for n in nodes}
    if len(outline.placements) != len(ids) or {p.id for p in outline.placements} != ids:
        raise ValueError("目录必须包含每个知识点，恰好一次")
    parents = {p.id: p.parent_id for p in outline.placements}
    assigned = [n.model_copy(update={"parent_id": parents[n.id]}) for n in nodes]
    assigned += [Node(id=g.id, title=g.title, kind="category", summary=g.summary,
                      parent_id=g.parent_id, synthetic=True, scope="自动组织的阅读目录")
                 for g in outline.categories]
    for relation in outline.relations:
        validate_evidence(relation.evidence, chunks)
    return Graph(title=title, nodes=assigned, relations=outline.relations)


def build_graph(title: str, sources: list[dict], job_dir: Path, settings: Settings,
                llm=None, progress: Callable = lambda stage: None) -> Graph:
    llm = llm or LLM(settings)
    parsed_dir = job_dir / "output" / "sources"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    all_chunks, source_records, char_count = [], [], 0
    for source in sources:
        progress("parsing")
        text = parse_document(job_dir / "input" / source["stored_name"], settings)
        char_count += len(text)
        if char_count > settings.max_source_chars:
            raise ValueError("本任务总文字数超限，请分批提交")
        parsed_dir.joinpath(source["id"] + ".md").write_text(text, encoding="utf-8")
        chunks = chunk_text(text, source["id"], settings)
        all_chunks.extend(chunks)
        if len(all_chunks) > settings.max_chunks:
            raise ValueError("分块数超限，请拆分任务或调整 CLC_MAX_CHUNKS")
        source_records.append({**source, "chars": len(text), "chunks": len(chunks),
                               "sha256": hashlib.sha256(text.encode()).hexdigest()})
    write_json(parsed_dir / "manifest.json", source_records)
    write_json(parsed_dir / "chunks.json", [c.to_dict() for c in all_chunks])
    chunk_map = {c.id: c for c in all_chunks}
    by_id: dict[str, Node] = {}
    for i, chunk in enumerate(all_chunks):
        progress(f"extracting {i+1}/{len(all_chunks)}")

        def check(extraction, chunk=chunk):
            for concept in extraction.concepts:
                validate_evidence(concept.evidence, {chunk.id: chunk})

        result = llm.complete(Extraction,
            "识别当前分块中值得单独学习的知识点，每个知识点将成为一个独立 Markdown 文件。"
            "给出定义、解释、原文支持的易混点与示例；没有信息时不要编造。非知识内容可返回空列表。",
            chunk.to_dict(), check)
        for concept in result.concepts:
            key = concept_id(concept)
            node = Node(id=key, **concept.model_dump())
            if key in by_id:
                existing = by_id[key]
                if node.explanation not in existing.explanation:
                    existing.explanation += "\n\n" + node.explanation
                for field in ("evidence", "examples", "pitfalls"):
                    values = getattr(existing, field)
                    values.extend(v for v in getattr(node, field) if v not in values)
            else:
                by_id[key] = node
            if len(by_id) > settings.max_concepts:
                raise ValueError("知识点数量超过 CLC_MAX_CONCEPTS，请拆分资料")
    if not by_id:
        raise ValueError("资料中没有识别出有原文依据的知识点")
    nodes = list(by_id.values())
    # Planning sees only grounded summaries/evidence; full prose stays in atomic Markdown nodes.
    payload = {"title": title, "concepts": [n.model_dump(exclude={"explanation", "examples", "pitfalls"}) for n in nodes]}
    if len(json.dumps(payload, ensure_ascii=False)) > settings.plan_chars:
        raise ValueError("层级规划上下文超限，请减少资料或提高 CLC_PLAN_CHARS")
    progress("organizing")
    outline = llm.complete(Outline,
        "把全部知识点组织成适合循序学习的层级目录。必要时生成少量 category 分组，明确它只是阅读组织。"
        "placements 必须覆盖每个已有知识点 ID 恰好一次，不得修改或遗漏 ID。目录深度不超过 12，无环。"
        "在 relations 中标明原文支持的跨层关系，特别区分策略与实现，类别与机制。"
        "source 是关系主语，例如 CFS implements SCHED_NORMAL；evidence 必须取自提供的逐字引文。"
        "不同版本或语境的信息要保留差异，不能擅自推断一个统一的事实。",
        payload, lambda result: assemble(title, nodes, result, chunk_map))
    graph = assemble(title, nodes, outline, chunk_map)
    write_json(job_dir / "output" / "graph.json", graph.model_dump())
    return graph
