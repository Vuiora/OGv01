import json
from pathlib import Path
import pytest
from ogflow.bootstrap import ROOT
from ogflow.config import LLMConfig
from ogflow.models import TaskSpec
from ogflow.pipeline import BusinessPipeline
from ogflow.store import digest

def task():
    values = json.loads((ROOT / "examples/business/demo-task.json").read_text(encoding="utf-8"))
    values.update(llm="offline", architecture=False, feedback=False, n_resamples=2, max_candidates=24)
    return TaskSpec.model_validate(values)

def test_real_cross_project_loop_and_no_resealing(tmp_path):
    pipeline = BusinessPipeline(tmp_path, config=LLMConfig(api_key="test-key"))
    result = pipeline.run(task(), base_dir=ROOT / "examples/business")
    folder = Path(result["output"])
    archive = json.loads((folder / "discovery-archive.json").read_text(encoding="utf-8"))
    plan = json.loads((folder / "frozen-plan.json").read_text(encoding="utf-8"))
    assert result["state"] == "SUCCEEDED"
    assert archive["rounds"][0]["status"] == "confirmed"
    assert archive["knowledge"]["archives"]
    assert archive["rounds"][0]["freeze_invariance_ok"] is True
    assert plan["effect_threshold"] == 0.1
    policy = plan["preprocessing"]["steps"][0]
    assert policy["selected_features"]
    assert policy["target"] == "Y"
    stats = json.loads((folder / "confirmation-statistics.json").read_text(encoding="utf-8"))
    assert len(stats) >= 2
    assert len({v["p_value"] for v in stats.values()}) >= 2
    ledger = json.loads((folder / "audit-ledger.json").read_text(encoding="utf-8"))
    freeze = next(e["seq"] for e in ledger if e["action"] == "freeze")
    consume = next(e["seq"] for e in ledger if e["action"] == "consume")
    assert freeze < consume
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    confirmation_reads = [e for e in ledger if e["action"] == "read" and e["details"].get("dataset_ref") == protocol["resources"]["C1"]]
    assert not confirmation_reads
    assert not any("test-key" in p.read_text(encoding="utf-8") for p in folder.glob("*.json"))
    assert result["artifacts"]["archive"]["sha256"] == digest(archive)
    before = (folder / "audit-ledger.json").read_bytes()
    evidence_before = (folder / "evidence.sqlite3").read_bytes()
    resumed = pipeline.finish(result["run_id"])
    assert resumed["state"] == "SUCCEEDED"
    assert resumed["new_confirmation_evidence"] is False
    assert (folder / "audit-ledger.json").read_bytes() == before
    assert (folder / "evidence.sqlite3").read_bytes() == evidence_before
    tampered = {**archive, "synthetic": False}
    (folder / "discovery-archive.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="intact"):
        pipeline.finish(result["run_id"])
    (folder / "discovery-archive.json").write_text(json.dumps(archive), encoding="utf-8")
    with pytest.raises(ValueError, match="already registered"):
        pipeline.run(task(), base_dir=ROOT / "examples/business")

def test_live_proposals_receive_only_e_and_reject_target(tmp_path):
    class Client:
        config = LLMConfig(api_key="test-key")
        payloads = []
        def complete(self, system, payload, **kwargs):
            self.payloads.append(payload)
            return {"expressions": ["X1*X2", "Y", "__import__('os')"], "rationale": "test adversarial proposals"}
    client = Client()
    spec = task().model_copy(update={"llm": "live"})
    result = BusinessPipeline(tmp_path, config=client.config, client=client).run(spec, base_dir=ROOT / "examples/business")
    folder = Path(result["output"])
    proposals = json.loads((folder / "proposals.json").read_text(encoding="utf-8"))
    assert proposals["accepted_count"] == 1
    assert len(proposals["rejected"]) == 2
    assert client.payloads[0]["E_sample"]
    assert "confirmation" not in client.payloads[0]
    assert "records" not in client.payloads[0]
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["resources"]["C1"] not in json.dumps(client.payloads)

@pytest.mark.parametrize("change", [
    {"features": ["X1", "X1"]}, {"features": ["X1", "Y"]},
    {"units": {"Y": "1"}}, {"group_column": "Y"}, {"top_k": 8},
    {"documents": ["background.md"], "llm": "offline"},
])
def test_contract_rejects_ambiguous_or_incompatible_inputs(change):
    values = task().model_dump()
    values.update(change)
    with pytest.raises(ValueError):
        TaskSpec.model_validate(values)


def test_meta_selector_uses_only_other_e_datasets_and_matching_objective(tmp_path):
    import copy
    from ogflow.observations import load_observations
    from ogflow.recommendation import recommend
    rows, source_hash = load_observations(task(), ROOT / "examples/business/demo-observations.csv")
    history = tmp_path / "experience.jsonl"
    first = recommend(rows, task().features, "Y", "batch", 3, history, task().dataset_id, source_hash, tmp_path)
    assert first["mode"] == "cold_start_grouped_cv"
    records = []
    for name in ["a", "b", "c", "same-source", "confirmation-only", task().dataset_id]:
        evaluations = copy.deepcopy(first["evaluations"])
        # Runtime must not change labels when the declared objective has cost weight zero.
        for method, ev in evaluations.items():
            ev.update(cv_score_mean=1.0 if method == "mutual_info" else -10.0,
                      stability_jaccard=1.0, runtime_sec=1e50 if method == "mutual_info" else 0.0)
        records.append({"dataset_id": name, "meta_features": first["meta_features"],
            "selected_method": "mutual_info", "evaluations": evaluations,
            "selection_reason": {"source_sha256": source_hash if name == "same-source" else name,
                "evidence_purpose": "C" if name == "confirmation-only" else "E"}})
    history.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    result = recommend(rows, task().features, "Y", "batch", 3, history, task().dataset_id, source_hash, tmp_path)
    assert result["mode"] == "historical_meta_selector"
    assert result["historical_dataset_count"] == 3
    assert result["selected_method"] == "mutual_info"
    training = [json.loads(line)["dataset_id"] for line in (tmp_path / "meta-training.jsonl").read_text().splitlines()]
    assert training == ["a", "b", "c"]


def test_n8n_docling_structure_bridge_preserves_code_formula_and_table():
    from ogflow.documents import validate_document_structure
    text = "# Heading\n\n```python\nx = 3\n```\n\n$$x^2$$\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    result = validate_document_structure(text)
    assert result["round_trip_passed"] is True
    assert result["protected_count"] >= 2  # Code and math are protected; the table survives the complete round trip.
    assert result["segment_count"] >= 1

