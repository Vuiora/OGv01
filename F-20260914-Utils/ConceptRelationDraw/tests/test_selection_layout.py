import asyncio
import json
import random
import xml.etree.ElementTree as ET
from copy import deepcopy
from itertools import combinations

import httpx
import pytest
from fastapi.testclient import TestClient

from concept_relation import exports
from concept_relation.api import create_app
from concept_relation.grounding import evidence_for, split_document
from concept_relation.layout import graph_layout, segment_clear
from concept_relation.llm import PipelineError
from concept_relation.models import KeyConceptSelection, KeyRelationSelection
from concept_relation.pipeline import Pipeline
from concept_relation.selection import choose_key_concepts, sparse_relations, choose_key_relations
from concept_relation.store import Store
from test_pipeline import DOCUMENT, ModelMock, configured, model_result, run_pipeline


def candidates():
    return [dict(id=identifier, label=label, definition=definition, section_ids=[section],
                 evidence=[dict(start=offset, end=offset+2, quote=label)])
            for identifier, label, definition, section, offset in [
                ("c1", "苹果", "一种水果", "s1", 0), ("c2", "苹果", "水果", "s2", 8),
                ("c3", "苹果", "公司名称", "s3", 16), ("c4", "附件", "提交文件", "s4", 24)]]


def choice(identifier, duplicates=None):
    return dict(id=identifier, importance_reason="原文主题涉及该概念", duplicate_ids=duplicates or [])


def test_selection_merges_evidence_without_merging_same_name_different_sense():
    source = candidates()
    selected, omitted = choose_key_concepts(source, KeyConceptSelection(concepts=[choice("c1", ["c2"]), choice("c3")]), 3)
    assert [concept["id"] for concept in selected] == ["c1", "c3"]
    assert len(selected[0]["evidence"]) == 2 and selected[0]["section_ids"] == ["s1", "s2"]
    assert omitted[0]["id"] == "c4"
    assert len(source[0]["evidence"]) == 1


def test_pdf_whitespace_is_located_as_original_text_without_accepting_rewrites():
    document = "# 原文\n😀钻孔成像技 术\n用于裂隙识别。\n# 其他\n另一段。"
    sections, chunks = split_document(document)
    evidence = evidence_for(["😀钻孔成像技术用于裂隙识别。"], document, sections)
    assert evidence[0]["quote"] == "😀钻孔成像技 术\n用于裂隙识别。"
    assert document[evidence[0]["start"]:evidence[0]["end"]] == evidence[0]["quote"]
    assert evidence[0]["line_start"] == 2 and evidence[0]["line_end"] == 3
    assert evidence_for(["钻孔成像技术提高识别准确率。"], document, sections) is None
    assert evidence_for(["另一段。"], document, sections, chunks[0]["start"], chunks[0]["end"]) is None


@pytest.mark.parametrize("choices,limit", [
    ([choice("external")], 3), ([choice("c1"), choice("c1")], 3),
    ([choice("c1", ["c4"])], 3), ([choice("c1", ["c2"]), choice("c2")], 3),
    ([choice("c1", ["c1"])], 3), ([choice("c1"), choice("c3")], 1),
])
def test_selection_rejects_invalid_ids_merges_and_budgets(choices, limit):
    with pytest.raises(PipelineError):
        choose_key_concepts(candidates(), KeyConceptSelection(concepts=choices), limit)


@pytest.mark.parametrize("keep", [[], ["c1", "c2"]])
def test_only_selected_concepts_reach_full_context_relation_calls(tmp_path, keep):
    class SelectiveModel(ModelMock):
        def __call__(self, request):
            payload = json.loads(json.loads(request.content)["input"].split("\nINPUT_DATA:\n")[1])
            if "candidates" in payload:
                assert payload["full_document"] == DOCUMENT
                return model_result({"concepts": [choice(identifier) for identifier in keep]})
            if "source_ids" in payload:
                assert {concept["id"] for concept in payload["concepts"]} == set(keep)
                assert payload["full_document"] == DOCUMENT
                assert payload["max_relations_per_source"] == 3
            return super().__call__(request)

    async def run():
        store, mock = Store(tmp_path), SelectiveModel()
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            pipeline = Pipeline(configured(tmp_path), store, client)
            job = store.create("test.md", DOCUMENT.encode())
            await pipeline.run(job["job_id"])
            graph = store.read(job["job_id"], "graph.json")
            assert [concept["id"] for concept in graph["concepts"]] == keep
            assert graph["extraction_policy"]["candidate_count"] == 3
            assert len(mock.full_documents) == len(keep) + (1 if keep else 0)
            if not keep:
                assert graph["relations"] == [] and graph_layout(graph)["positions"] == {}
    asyncio.run(run())


def sample_graph(count=18):
    generator = random.Random(42)
    concepts = [dict(id=f"c{index}", label=f"概念{index}") for index in range(count)]
    relations = []
    for source, target in combinations(concepts[:-1], 2):
        if generator.random() < .25:
            relations.append(dict(id=f"r{len(relations)}", source=target["id"], target=source["id"], type="depends_on",
                                  confidence=generator.random(), review="unreviewed", explanation="原文联系"))
    return dict(concepts=concepts, relations=relations)


def test_relation_budget_is_audited_and_prefers_specific_relations():
    graph = sample_graph()
    graph["relations"].append(dict(id="generic", source=graph["relations"][0]["source"], target="c1", type="related", confidence=1, review="unreviewed"))
    selected, omitted = sparse_relations(graph["relations"], 1)
    assert len({edge["source"] for edge in selected}) == len(selected)
    assert len(selected) + len(omitted) == len(graph["relations"])
    assert all(entry["stage"] == "sparsify" for entry in omitted)
    assert "generic" not in {edge["id"] for edge in selected}


def test_relation_review_cannot_invent_edges_and_audits_removals():
    relations = sample_graph()["relations"]
    selected, omitted = choose_key_relations(relations, KeyRelationSelection(relation_ids=[relations[0]["id"]]))
    assert selected == relations[:1] and len(omitted) == len(relations) - 1
    assert all(entry["stage"] == "relation_review" for entry in omitted)
    for identifiers in (["external"], [relations[0]["id"]] * 2):
        with pytest.raises(PipelineError):
            choose_key_relations(relations, KeyRelationSelection(relation_ids=identifiers))


def test_generic_pair_relation_is_removed_when_specific_relation_exists():
    specific = sample_graph()["relations"][0]
    generic = {**specific, "id": "generic", "type": "related", "source": specific["target"], "target": specific["source"]}
    selected, omitted = sparse_relations([generic, specific], 3)
    assert selected == [specific] and len(omitted) == 1


@pytest.mark.parametrize("count", [0, 1, 18, 61])
def test_layout_preserves_nodes_and_routes_around_boxes(count):
    graph = sample_graph(count)
    original = deepcopy(graph)
    layout = graph_layout(graph)
    points = layout["positions"]
    assert graph == original
    assert set(points) == {node["id"] for node in graph["concepts"]}
    assert set(layout["routes"]) == {edge["id"] for edge in graph["relations"]}
    for source, target in combinations(points.values(), 2):
        assert abs(source["x"] - target["x"]) >= 176 or abs(source["y"] - target["y"]) >= 56
    parents = {node: node for node in points}

    def root(node):
        while parents[node] != node:
            node = parents[node]
        return node

    for edge in graph["relations"]:
        route = layout["routes"][edge["id"]]
        source, target = points[edge["source"]], points[edge["target"]]
        assert abs(route[0][0] - source["x"]) == 88 and route[0][1] == source["y"]
        assert abs(route[-1][0] - target["x"]) == 88 and route[-1][1] == target["y"]
        boxes = [(point["x"]-88, point["y"]-28, point["x"]+88, point["y"]+28) for point in points.values()]
        for start, end in zip(route, route[1:]):
            assert start[0] == end[0] or start[1] == end[1]
            assert segment_clear(start, end, boxes)
        if edge["id"] in layout["backbone_ids"]:
            source_root, target_root = root(edge["source"]), root(edge["target"])
            assert source_root != target_root
            parents[source_root] = target_root


def test_exports_share_layout_and_keep_supplementary_relations_in_all_mode():
    graph = sample_graph()
    graph["relations"][0]["review"] = "rejected"
    layout = graph_layout(graph)
    assert graph["relations"][0]["id"] not in layout["routes"]
    for view in ("backbone", "all"):
        expected = exports.visible_edges(graph, view)
        svg = ET.fromstring(exports.svg(graph, view))
        assert len(svg.findall(".//{http://www.w3.org/2000/svg}polyline")) == len(expected)
        drawing = ET.fromstring(exports.drawio(graph, view))
        assert len(drawing.findall(".//mxCell[@edge='1']")) == len(expected)
        for node in graph["concepts"]:
            geometry = drawing.find(f".//mxCell[@id='{node['id']}']/mxGeometry")
            assert float(geometry.attrib["x"]) + 88 == layout["positions"][node["id"]]["x"]
    assert len(exports.visible_edges(graph, "all")) > len(layout["backbone_ids"])


def test_reanalyze_preserves_history_and_reuses_parse(tmp_path):
    store, job_id, _ = run_pipeline(tmp_path, ".pdf")
    original = store.path(job_id, "graph.json").read_bytes()
    mock = ModelMock()
    with TestClient(create_app(configured(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(mock)))) as client:
        client.headers["X-API-Key"] = "service-test-key"
        response = client.post(f"/api/jobs/{job_id}/reanalyze")
        assert response.status_code == 201
        new_job = response.json()
        assert new_job["parent_job_id"] == job_id and new_job["job_id"] != job_id
        assert new_job["completed_stages"] == ["parse"]
        for stage in ("parse", "extract", "relate", "render"):
            assert client.post(f"/api/jobs/{new_job['job_id']}/stages/{stage}").status_code == 200
        assert not mock.docling_called
        assert store.path(job_id, "graph.json").read_bytes() == original
        assert client.get(f"/api/jobs/{new_job['job_id']}/export/svg?view=bad").status_code == 422
