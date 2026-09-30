"""Business orchestration with durable artifacts and explicit failure semantics."""
import json
import re
import threading
import uuid
from pathlib import Path
from mtbmt.experience_store import ExperienceRecord, ExperienceStore, now_utc_iso
from sdl_m01 import Module01, initialize
from .bootstrap import ROOT
from .config import LLMConfig
from .documents import collect_knowledge
from .feedback import build_feedback
from .evidence_registry import reserve_units
from .llm import JSONClient
from .mentor import build_application
from .models import TaskSpec
from .observations import load_observations, protocol_spec
from .recommendation import recommend
from .sdl_adapter import propose, run_discovery
from .store import RunStore, write_json, digest

_HISTORY_LOCK = threading.Lock()

class BusinessPipeline:
    def __init__(self, output=None, *, config=None, client=None):
        self.output = Path(output or ROOT / "runs" / "business").resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.config = (config or LLMConfig.load()).apply()
        self.client = client

    def finish(self, run_id, *, architecture=False):
        """Retry publication/feedback using immutable archived results, never reread C."""
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run ID")
        directory = self.output / run_id
        public = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        spec = TaskSpec.model_validate_json((directory / "task.json").read_text(encoding="utf-8"))
        archive = json.loads((directory / "discovery-archive.json").read_text(encoding="utf-8"))
        strategy = json.loads((directory / "recommendation.json").read_text(encoding="utf-8"))
        audit = json.loads((directory / "audit-ledger.json").read_text(encoding="utf-8"))
        if (digest(archive) != public["artifacts"]["archive"]["sha256"]
            or digest(audit) != public["artifacts"]["audit"]["sha256"]
            or any(r["status"] == "skipped" for r in archive["rounds"])):
            raise ValueError("An intact completed discovery archive is required")
        store = object.__new__(RunStore)
        store.path, store.run_id = directory, run_id
        store.events = json.loads((directory / "events.json").read_text(encoding="utf-8"))
        client = None if spec.llm == "offline" else self.client or JSONClient(self.config)
        public.update(state="RUNNING", new_confirmation_evidence=False)
        public.pop("error", None)
        public.pop("failed_stage", None)
        write_json(directory / "run.json", public)
        stage = "feedback"
        try:
            if spec.feedback and "feedback" not in public["artifacts"]:
                store.event(stage, "running", resumed=True)
                public["artifacts"]["feedback"] = store.artifact("feedback.json", build_feedback(client, archive, spec, strategy,
                    progress=lambda event: store.event("feedback", "running", **event)))
                store.event(stage, "completed", resumed=True)
                write_json(directory / "run.json", public)
            stage = "architecture"
            if architecture or spec.architecture:
                from .architecture import produce_architecture
                store.event(stage, "running", resumed=True)
                public["artifacts"]["architecture"] = produce_architecture(directory / "architecture", self.config, live=spec.llm == "live")
                store.event(stage, "completed", quality_passed=True)
            public.update(state="SUCCEEDED", scientific_status="see individual confirmation results", stop=archive["stop"])
            store.event("business", "completed", resumed=True, new_confirmation_evidence=False)
        except Exception as exc:
            public.update(state="FAILED", failed_stage=stage, error=type(exc).__name__)
            store.event(stage, "failed", error=type(exc).__name__)
            write_json(directory / "run.json", public)
            raise
        write_json(directory / "run.json", public)
        return {**public, "output": str(directory)}

    def run(self, spec: TaskSpec, *, base_dir=None, run_id=None):
        base = Path(base_dir or ROOT).resolve()
        ident = run_id or uuid.uuid4().hex
        store = RunStore(self.output, ident)
        public = {"run_id": ident, "state": "RUNNING", "artifacts": {}, "llm": self.config.public(),
                  "synthetic": spec.synthetic, "observation_semantics": "task-owner-declared"}
        write_json(store.path / "run.json", public)
        stage = "observations"
        try:
            store.artifact("task.json", spec.model_dump())
            store.artifact("application.json", build_application())
            store.event(stage, "running")
            records, source_hash = load_observations(spec, (base / spec.observations).resolve())
            reserve_units(self.output, spec.dataset_id, spec.group_column, records, ident, source_hash)
            tokens = initialize(str(store.path / "evidence.sqlite3"))
            custodian = Module01(str(store.path / "evidence.sqlite3"), tokens["custodian"])
            confirmer = Module01(str(store.path / "evidence.sqlite3"), tokens["confirmer"])
            explorer = Module01(str(store.path / "evidence.sqlite3"), tokens["explorer"])
            protocol = custodian.build(protocol_spec(spec), records)
            # Only E/V public records leave the custodian role. Raw C rows and tokens stay local.
            E = explorer.read_dataset(protocol["resources"]["E"])
            V = explorer.read_dataset(protocol["resources"]["V"])
            if not E or not V:
                raise ValueError("E and V partitions must both contain eligible observations")
            store.artifact("protocol.json", protocol)
            store.event(stage, "completed", source_sha256=source_hash, E_rows=len(E), V_rows=len(V))
            write_json(store.path / "run.json", public)
            del records, tokens
            stage = "knowledge"
            store.event(stage, "running")
            knowledge = collect_knowledge([(base / p).resolve() for p in spec.documents], store.path / "knowledge", self.config,
                progress=lambda phase: store.event("knowledge", "running", phase=phase))
            public["artifacts"]["knowledge"] = store.artifact("knowledge.json", knowledge)
            store.event(stage, "completed", source_count=len(knowledge["sources"]))
            write_json(store.path / "run.json", public)
            client = None if spec.llm == "offline" else self.client or JSONClient(self.config, max_calls=8)
            stage = "recommendation"
            store.event(stage, "running")
            history = self.output / "experience.jsonl"
            strategy = recommend(E, spec.features, spec.target, spec.group_column, spec.top_k,
                history, spec.dataset_id, source_hash, store.path)
            public["artifacts"]["recommendation"] = store.artifact("recommendation.json", strategy)
            store.event(stage, "completed", method=strategy["selected_method"], mode=strategy["mode"])
            write_json(store.path / "run.json", public)
            stage = "hypotheses"
            store.event(stage, "running")
            proposal = propose(client, strategy, E, spec.target, knowledge)
            archive = run_discovery(spec, custodian, confirmer, explorer, protocol, strategy, proposal, store)
            archive.update({"run_id": ident, "source_sha256": source_hash, "synthetic": spec.synthetic,
                "business_method": spec.confirmation.method, "knowledge_context_digest": digest(knowledge)})
            if spec.synthetic:
                archive["notice"] = "Synthetic data validates implementation; this is not a real-world discovery."
            public["artifacts"]["archive"] = store.artifact("discovery-archive.json", archive)
            public["artifacts"]["knowledge_version"] = store.artifact("knowledge-version.json", archive["knowledge"])
            public["artifacts"]["open_questions"] = store.artifact("open-questions.json", archive["open_questions"])
            if any(r["status"] == "skipped" for r in archive["rounds"]):
                raise RuntimeError("SDL execution failed; the failed round is preserved in the archive")
            store.event(stage, "completed", rounds=len(archive["rounds"]))
            stage = "archive"
            store.event(stage, "running")
            custodian.verify_integrity()
            record = ExperienceRecord(spec.dataset_id, strategy["meta_features"], None, strategy["evaluations"],
                strategy["measured_best_method"], {"source_sha256": source_hash, "run_id": ident,
                    "evidence_purpose": "E", "grouped_cv": True, "confirmation_grade": "not_applicable"}, now_utc_iso())
            with _HISTORY_LOCK:
                ExperienceStore(history).append(record)
            public["artifacts"]["audit"] = store.artifact("audit-ledger.json", custodian.ledger())
            store.event(stage, "completed", integrity_verified=True)
            write_json(store.path / "run.json", public)
            stage = "feedback"
            if spec.feedback:
                store.event(stage, "running")
                public["artifacts"]["feedback"] = store.artifact("feedback.json", build_feedback(client, archive, spec, strategy,
                    progress=lambda event: store.event("feedback", "running", **event)))
                store.event(stage, "completed")
                write_json(store.path / "run.json", public)
            stage = "architecture"
            if spec.architecture:
                from .architecture import produce_architecture
                store.event(stage, "running")
                architecture = produce_architecture(store.path / "architecture", self.config, live=spec.llm == "live")
                public["artifacts"]["architecture"] = architecture
                store.event(stage, "completed", quality_passed=True)
            public.update(state="SUCCEEDED", scientific_status="see individual confirmation results", stop=archive["stop"])
            store.event("business", "completed")
        except Exception as exc:
            # Safe error type only; never expose provider responses, prompts, CSV rows or keys.
            public.update(state="FAILED", failed_stage=stage, error=type(exc).__name__)
            store.event(stage, "failed", error=type(exc).__name__)
            write_json(store.path / "run.json", public)
            raise
        write_json(store.path / "run.json", public)
        return {**public, "output": str(store.path)}

