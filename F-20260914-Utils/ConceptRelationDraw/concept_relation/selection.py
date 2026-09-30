from copy import deepcopy

from .grounding import normalize
from .llm import PipelineError

RELATION_PRIORITY = {"part_of": 8, "prerequisite": 7, "depends_on": 6, "causes": 5,
                     "explains": 4, "applies_to": 3, "contrasts": 2, "related": 1}


def choose_key_concepts(candidates, selection, limit):
    if len(selection.concepts) > limit:
        raise PipelineError(f"模型的重要概念筛选超过 {limit} 个上限，请重试。")
    index = {concept["id"]: concept for concept in candidates}
    used, selected = set(), []
    for choice in selection.concepts:
        members = [choice.id, *choice.duplicate_ids]
        if len(set(members)) != len(members) or any(member not in index or member in used for member in members):
            raise PipelineError("重要概念筛选包含不存在或重复使用的候选 ID，结果未发布。")
        if any(normalize(index[member]["label"]) != normalize(index[choice.id]["label"]) for member in choice.duplicate_ids):
            raise PipelineError("重复概念合并只能使用名称一致的候选，结果未发布。")
        used.update(members)
        concept = deepcopy(index[choice.id])
        concept["importance_reason"] = choice.importance_reason
        concept["merged_from"] = members
        concept["evidence"] = list({(evidence["start"], evidence["end"]): evidence
                                    for member in members for evidence in index[member]["evidence"]}.values())
        concept["section_ids"] = sorted({section for member in members for section in index[member]["section_ids"]})
        selected.append(concept)
    omitted = [dict(stage="select", id=concept["id"], label=concept["label"], reason="全文重要性筛选未保留该候选")
               for concept in candidates if concept["id"] not in used]
    return selected, omitted


def sparse_relations(relations, limit):
    counts, selected, omitted, pairs = {}, [], [], set()
    ranked = sorted(relations, key=lambda relation: (-RELATION_PRIORITY[relation["type"]], -relation["confidence"], relation["id"]))
    for relation in ranked:
        source = relation["source"]
        pair = tuple(sorted((source, relation["target"])))
        if relation["type"] == "related" and pair in pairs:
            omitted.append(dict(stage="sparsify", source=source, target=relation["target"], type=relation["type"],
                                reason="同一对概念已有更具体的关系，不重复保留泛泛关联"))
            continue
        if counts.get(source, 0) >= limit:
            omitted.append(dict(stage="sparsify", source=source, target=relation["target"], type=relation["type"],
                                reason="超过每个源概念的重要关系预算，优先保留具体类型及较高置信度关系"))
            continue
        counts[source] = counts.get(source, 0) + 1
        pairs.add(pair)
        selected.append(relation)
    return selected, omitted


def choose_key_relations(relations, selection):
    selected_ids = set(selection.relation_ids)
    existing_ids = {relation["id"] for relation in relations}
    if len(selected_ids) != len(selection.relation_ids) or not selected_ids <= existing_ids:
        raise PipelineError("全文关系复核包含不存在或重复的关系 ID，结果未发布。")
    selected = [relation for relation in relations if relation["id"] in selected_ids]
    omitted = [dict(stage="relation_review", id=relation["id"], source=relation["source"], target=relation["target"],
                    reason="全文关系复核未保留：联系不够直接或重要、语义方向不符、或存在冗余")
               for relation in relations if relation["id"] not in selected_ids]
    return selected, omitted
