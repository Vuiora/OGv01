"""Deterministic provenance checks; these do not prove semantic entailment."""
import re


def normalize(text):
    return re.sub(r"\s+", "", text).casefold()


def split_document(markdown, chunk_chars=5000):
    sections, chunks = [], []
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", markdown))
    boundaries = [(0, "文档开头")]
    for heading in headings:
        if heading.start() == 0:
            boundaries[0] = (0, heading.group(1))
        else:
            boundaries.append((heading.start(), heading.group(1)))
    for index, (start, title) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(markdown)
        if not markdown[start:end].strip():
            continue
        sid = f"s{len(sections) + 1}"
        sections.append(dict(id=sid, title=title, start=start, end=end))
        cursor = start
        while cursor < end:
            stop = min(cursor + chunk_chars, end)
            if stop < end:
                newline = markdown.rfind("\n", cursor + chunk_chars // 2, stop)
                if newline > cursor:
                    stop = newline + 1
            chunks.append(dict(id=f"b{len(chunks) + 1}", section_id=sid, title=title,
                               start=cursor, end=stop, text=markdown[cursor:stop]))
            if stop == end:
                break
            cursor = max(cursor + 1, stop - min(300, chunk_chars // 10))
    return sections, chunks


def evidence_for(quotes, document, sections, start=0, end=None):
    evidence = []
    limit = len(document) if end is None else end
    compact_document, offsets = None, None
    for quote in quotes:
        quote = quote.strip()
        compact_quote = re.sub(r"\s+", "", quote)
        if len(compact_quote) < 2:
            return None
        position = document.find(quote, start, limit)
        if position < 0:
            if compact_document is None:
                offsets = [index for index in range(start, limit) if not document[index].isspace()]
                compact_document = "".join(document[index] for index in offsets)
            compact_position = compact_document.find(compact_quote)
            if compact_position < 0:
                return None
            position = offsets[compact_position]
            quote = document[position:offsets[compact_position + len(compact_quote) - 1] + 1]
        section = next((s for s in sections if s["start"] <= position < s["end"]), None)
        item = dict(quote=quote, start=position, end=position + len(quote),
                    line_start=document.count("\n", 0, position) + 1,
                    line_end=document.count("\n", 0, position + len(quote)) + 1,
                    section_id=section["id"] if section else "", section=section["title"] if section else "")
        if item not in evidence:
            evidence.append(item)
    return evidence


def validate_concepts(candidates, document, sections, chunk, concepts, rejected):
    for candidate in candidates:
        evidence = evidence_for(candidate.quotes, document, sections, chunk["start"], chunk["end"])
        if not evidence or not any(normalize(candidate.label) in normalize(e["quote"]) for e in evidence):
            rejected.append(dict(stage="extract", label=candidate.label, reason="名称或引用未在当前文档块的原文证据中出现"))
            continue
        existing = next((c for c in concepts if normalize(c["label"]) == normalize(candidate.label)
                         and normalize(c["definition"]) == normalize(candidate.definition)), None)
        if existing:
            existing["evidence"] = list({(e["start"], e["end"]): e for e in existing["evidence"] + evidence}.values())
            existing["section_ids"] = sorted(set(existing["section_ids"] + [chunk["section_id"]]))
            continue
        concepts.append(dict(id=f"c{len(concepts) + 1}", label=candidate.label, definition=candidate.definition,
                             section_ids=[chunk["section_id"]], evidence=evidence))


def validate_relations(candidates, document, sections, concepts, allowed_sources, relations, rejected):
    nodes = {c["id"]: c for c in concepts}
    for candidate in candidates:
        reason = None
        evidence = evidence_for(candidate.quotes, document, sections)
        if candidate.source not in allowed_sources or candidate.source not in nodes or candidate.target not in nodes:
            reason = "端点不是提取出的知识点，或超出本批源节点"
        elif candidate.source == candidate.target:
            reason = "自连接"
        elif not evidence:
            reason = "引用无法在文档原文中精确定位"
        elif not all(any(normalize(nodes[cid]["label"]) in normalize(e["quote"]) for e in evidence)
                     for cid in [candidate.source, candidate.target]):
            reason = "关系证据未覆盖两个概念名称"
        if reason:
            rejected.append(dict(stage="relate", source=candidate.source, target=candidate.target, reason=reason))
            continue
        source, target = candidate.source, candidate.target
        if candidate.type in ("related", "contrasts"):
            source, target = sorted((source, target))
        if any(r["source"] == source and r["target"] == target and r["type"] == candidate.type for r in relations):
            continue
        relations.append(dict(id=f"r{len(relations) + 1}", source=source, target=target, type=candidate.type,
                              explanation=candidate.explanation, evidence=evidence, confidence=candidate.confidence,
                              review="unreviewed", evidence_verified=True))
