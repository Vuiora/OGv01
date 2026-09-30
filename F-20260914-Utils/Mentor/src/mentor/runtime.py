import json
import time
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import or_, select

from mentor.compiler import compile_spec
from mentor.config import Settings
from mentor.db import (
    Application,
    Audit,
    Build,
    Database,
    Document,
    Idempotency,
    Outbox,
    Result,
    Review,
    Revision,
    Run,
    Task,
)
from mentor.documents import inspect_file, parse_document, select_chunks
from mentor.domain import STEPS, TERMINAL, AppSpec, DocumentIR, Extraction, canonical, digest
from mentor.models import ModelError, ModelGateway
from mentor.rules import validate
from mentor.storage import ArtifactStore


class Conflict(ValueError):
    pass


class Runtime:
    def __init__(self, config: Settings, db=None, gateway=None):
        self.config = config
        self.db = db or Database(config.database_url)
        self.store = ArtifactStore(config.storage_root)
        self.gateway = gateway or ModelGateway(self.db, config)

    def audit(self, session, action, resource_id, actor="system", details=None):
        session.add(Audit(action=action, resource_id=resource_id, actor=actor,
                          organization_id=self.config.organization_id, details=details or {}))

    def own(self, session, cls, resource_id, lock=False):
        query = select(cls).where(cls.id == resource_id, cls.organization_id == self.config.organization_id)
        if lock:
            query = query.with_for_update()
        value = session.scalar(query)
        if value is None:
            raise LookupError("RESOURCE_NOT_FOUND")
        return value

    def remember(self, session, scope, key, body_hash, resource_id=None):
        if not key or len(key) > 128:
            raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
        scope = self.config.organization_id + ":" + scope
        entry = session.get(Idempotency, (scope, key))
        if entry:
            if entry.body_hash != body_hash:
                raise Conflict("IDEMPOTENCY_BODY_CONFLICT")
            return entry.resource_id
        if resource_id:
            session.add(Idempotency(scope=scope, key=key, body_hash=body_hash, resource_id=resource_id))
        return None

    def upload(self, content: bytes, filename: str, key: str):
        mime, pages = inspect_file(content, filename)
        storage_key, sha = self.store.put(content, "documents")
        with self.db.transaction() as session:
            old = self.remember(session, "documents", key, digest([filename, sha]))
            if old:
                return self.own(session, Document, old)
            doc = Document(filename=Path(filename).name, sha256=sha, storage_key=storage_key,
                           mime=mime, pages=pages, size=len(content), organization_id=self.config.organization_id)
            session.add(doc)
            session.flush()
            self.remember(session, "documents", key, digest([filename, sha]), doc.id)
            self.audit(session, "document.uploaded", doc.id)
            return doc

    def register(self, spec: AppSpec, provenance=None) -> Build:
        bundle = compile_spec(spec)
        with self.db.transaction() as session:
            app = session.get(Application, spec.app_id)
            if app and app.organization_id != self.config.organization_id:
                raise Conflict("APPLICATION_ID_CONFLICT")
            if not app:
                app = Application(id=spec.app_id, name=spec.description[:200],
                                  organization_id=self.config.organization_id)
                session.add(app)
                session.flush()
            revision = session.scalar(select(Revision).where(Revision.app_id == app.id,
                                                              Revision.version == spec.app_version))
            if revision:
                if revision.hash != digest(spec.model_dump()):
                    raise Conflict("IMMUTABLE_VERSION_CONFLICT")
                old = session.scalar(select(Build).where(Build.revision_id == revision.id,
                                                         Build.digest == bundle["build_digest"]))
                if old:
                    return old
            else:
                revision = Revision(app_id=app.id, version=spec.app_version, spec=spec.model_dump(),
                                    hash=digest(spec.model_dump()), provenance=provenance or {},
                                    organization_id=self.config.organization_id)
                session.add(revision)
                session.flush()
            build = Build(revision_id=revision.id, digest=bundle["build_digest"], bundle=bundle,
                          organization_id=self.config.organization_id)
            session.add(build)
            session.flush()
            self.audit(session, "build.created", build.id, details={"digest": build.digest})
            return build

    def binding(self):
        return {"student_endpoint": self.config.student_endpoint, "student_model": self.config.student_model,
                "mode": self.config.model_mode, "teacher_mode": self.config.teacher_mode}

    def create_run(self, document_id, build_id, key, deployment_id=None):
        with self.db.transaction() as session:
            body_hash = digest([document_id, build_id, deployment_id])
            old = self.remember(session, "runs", key, body_hash)
            if old:
                return self.own(session, Run, old)
            doc = self.own(session, Document, document_id)
            build = self.own(session, Build, build_id)
            spec = AppSpec.model_validate(build.bundle["files"]["app-spec.json"])
            if doc.pages > spec.input.max_pages or doc.size > spec.input.max_file_bytes:
                raise ValueError("APP_INPUT_LIMIT")
            run = Run(document_id=doc.id, build_id=build.id, deployment_id=deployment_id,
                      deadline=time.time() + spec.policy.max_run_seconds, model_binding=self.binding(),
                      organization_id=self.config.organization_id)
            session.add(run)
            session.flush()
            self.remember(session, "runs", key, body_hash, run.id)
            if deployment_id:
                session.add(Outbox(run_id=run.id, organization_id=self.config.organization_id))
            self.audit(session, "run.created", run.id)
            return run

    def request_task(self, run_id, step, spec_hash=None):
        if step not in STEPS:
            raise ValueError("UNSUPPORTED_STEP")
        with self.db.transaction() as session:
            run = self.own(session, Run, run_id, lock=True)
            build = self.own(session, Build, run.build_id)
            if spec_hash and build.bundle["files"]["execution-plan.json"]["spec_hash"] != spec_hash:
                raise Conflict("WORKFLOW_SPEC_MISMATCH")
            task = session.scalar(select(Task).where(Task.run_id == run_id, Task.step == step))
            if task:
                return task
            if run.status in TERMINAL or run.status == "needs_review" or run.deadline < time.time():
                raise Conflict("RUN_NOT_ACTIVE")
            index = STEPS.index(step)
            if index:
                previous = session.scalar(select(Task).where(Task.run_id == run_id, Task.step == STEPS[index - 1]))
                if not previous or previous.status != "succeeded":
                    raise Conflict("STEP_PRECONDITION_FAILED")
            run.status, run.current_step = "running", step
            task = Task(run_id=run_id, step=step, organization_id=self.config.organization_id)
            session.add(task)
            session.flush()
            return task

    def claim(self):
        with self.db.transaction() as session:
            now = time.time()
            task = session.scalar(select(Task).join(Run).where(
                Task.organization_id == self.config.organization_id,
                Run.status.in_(["queued", "running"]), Run.deadline > now,
                or_(Task.status == "queued", (Task.status == "running") & (Task.lease_until < now))
            ).order_by(Task.created_at).with_for_update(skip_locked=True, of=Task).limit(1))
            if not task:
                return None
            if task.attempts >= 3:
                task.status, task.error = "failed", "TASK_ATTEMPTS_EXHAUSTED"
                run = self.own(session, Run, task.run_id, lock=True)
                run.status, run.diagnostics = "failed", {"code": task.error}
                return None
            task.status = "running"
            task.fence += 1
            task.attempts += 1
            task.lease_until = now + self.config.lease_seconds
            return task

    def heartbeat(self, task_id, fence):
        with self.db.transaction() as session:
            task = self.own(session, Task, task_id, lock=True)
            if task.fence == fence and task.status == "running":
                task.lease_until = time.time() + self.config.lease_seconds

    def output(self, run_id, step):
        with self.db.transaction() as session:
            task = session.scalar(select(Task).where(Task.run_id == run_id, Task.step == step,
                                                      Task.organization_id == self.config.organization_id))
            if not task or task.status != "succeeded":
                raise Conflict("MISSING_PREREQUISITE")
            return json.loads(self.store.path(task.output["artifact_key"]).read_text(encoding="utf-8"))

    def execute(self, task):
        with self.db.transaction() as session:
            run = self.own(session, Run, task.run_id)
            doc = self.own(session, Document, run.document_id)
            build = self.own(session, Build, run.build_id)
        spec = AppSpec.model_validate(build.bundle["files"]["app-spec.json"])
        if task.step == "parse":
            return parse_document(doc, self.store, demo=self.config.allow_local_demo).model_dump()
        ir = DocumentIR.model_validate(self.output(run.id, "parse"))
        if task.step == "select":
            return {"chunks": select_chunks(ir, spec.policy.max_input_chars_per_chunk)}
        if task.step == "extract":
            chunks = self.output(run.id, "select")["chunks"]
            data, metadata, warnings = {}, {}, []
            for chunk in chunks:
                payload = {"document_id": ir.document_id, "parse_id": ir.parse_id,
                           "blocks": chunk["blocks"], "output_schema": spec.output_schema,
                           "response_schema": Extraction.model_json_schema()}
                extracted = None
                for repair in range(spec.policy.max_student_repairs_per_step + 1):
                    try:
                        response = self.gateway.generate("student", spec.prompt, payload, spec.policy, run.id)
                        extracted = Extraction.model_validate(response)
                        break
                    except (ModelError, ValidationError) as exc:
                        warnings.append(type(exc).__name__)
                        payload["repair_instruction"] = "Previous response failed JSON/contract validation. Return valid JSON matching response_schema."
                        if isinstance(exc, ModelError) and str(exc) in {"BUDGET_EXHAUSTED", "RUN_NOT_ACTIVE"}:
                            break
                if extracted is None:
                    warnings.append("INCOMPLETE_EXTRACTION")
                    continue
                offset = len(data.get("items", []))
                for field, value in extracted.data.items():
                    if field == "items" and isinstance(value, list):
                        data.setdefault("items", []).extend(value)
                    elif value is not None:
                        if field in data and data[field] is not None and data[field] != value:
                            data[field] = None
                            warnings.append("FIELD_CONFLICT:" + field)
                        elif "FIELD_CONFLICT:" + field not in warnings:
                            data[field] = value
                    else:
                        data.setdefault(field, None)
                for path, meta in extracted.field_meta.items():
                    if path.startswith("/items/"):
                        parts = path.split("/")
                        try:
                            parts[2] = str(int(parts[2]) + offset)
                        except ValueError:
                            continue
                        path = "/".join(parts)
                    metadata[path] = meta.model_dump()
            for field in spec.output_schema.get("properties", {}):
                data.setdefault(field, [] if field == "items" else None)
            return {"extraction": {"data": data, "field_meta": metadata}, "warnings": warnings}
        if task.step == "validate":
            extracted = self.output(run.id, "extract")
            if extracted["warnings"]:
                ir.complete = False
                ir.warnings.extend(extracted["warnings"])
            return validate(Extraction.model_validate(extracted["extraction"]), ir, spec)
        if task.step == "finalize":
            return self.output(run.id, "validate")
        raise ValueError("UNSUPPORTED_STEP")

    def commit_task(self, task_id, fence, output=None, error=None):
        artifact = None
        if output is not None:
            key, sha = self.store.put(canonical(output).encode(), "task-results")
            artifact = {"artifact_key": key, "sha256": sha}
        with self.db.transaction() as session:
            task = self.own(session, Task, task_id, lock=True)
            run = self.own(session, Run, task.run_id, lock=True)
            if task.fence != fence or task.status != "running" or task.lease_until < time.time():
                return False
            if run.status in TERMINAL or run.deadline <= time.time():
                task.status, task.error = "failed", "RUN_NOT_ACTIVE"
                return False
            task.status = "failed" if error else "succeeded"
            task.error, task.output = error, artifact or {}
            if error:
                run.status, run.diagnostics = "failed", {"code": error, "step": task.step}
            elif task.step == "finalize":
                run.result_revision = 1
                run.status = output["status"]
                session.add(Result(run_id=run.id, revision=1, content=output,
                                   organization_id=self.config.organization_id))
                if run.status == "needs_review":
                    session.add(Review(run_id=run.id, organization_id=self.config.organization_id))
            self.audit(session, "task." + task.status, task.id, details={"fence": fence})
            return True

    def work_once(self):
        task = self.claim()
        if not task:
            return False
        import threading
        stop = threading.Event()
        def pulse():
            while not stop.wait(max(1, self.config.lease_seconds / 3)):
                self.heartbeat(task.id, task.fence)
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            output = self.execute(task)
            self.commit_task(task.id, task.fence, output=output)
        except Exception as exc:
            self.commit_task(task.id, task.fence, error=type(exc).__name__)
        finally:
            stop.set()
            thread.join(timeout=1)
        return True

    def cancel(self, run_id, actor):
        with self.db.transaction() as session:
            run = self.own(session, Run, run_id, lock=True)
            if run.status not in TERMINAL:
                run.status = "cancelled"
                self.audit(session, "run.cancelled", run.id, actor)
            return run

    def result(self, run_id):
        with self.db.transaction() as session:
            run = self.own(session, Run, run_id)
            result = session.scalar(select(Result).where(Result.run_id == run_id,
                                                         Result.revision == run.result_revision))
            if not result:
                raise Conflict("RESULT_NOT_READY")
            build = self.own(session, Build, run.build_id)
            return {**result.content, "run_id": run.id, "status": run.status,
                    "result_revision": result.revision, "build_digest": build.digest,
                    "cost_usd": run.cost_actual, "uncertain_cost_usd": run.cost_reserved}

    def review(self, review_id, expected_revision, decision, extraction, actor, reason):
        with self.db.transaction() as session:
            review = self.own(session, Review, review_id, lock=True)
            run = self.own(session, Run, review.run_id, lock=True)
            if run.result_revision != expected_revision or review.status != "pending" or run.status != "needs_review":
                raise Conflict("REVIEW_REVISION_CONFLICT")
            old = session.scalar(select(Result).where(Result.run_id == run.id, Result.revision == expected_revision))
            content = old.content
            if decision == "accept":
                build = self.own(session, Build, run.build_id)
                spec = AppSpec.model_validate(build.bundle["files"]["app-spec.json"])
                ir = DocumentIR.model_validate(self.output(run.id, "parse"))
                content = validate(extraction or Extraction.model_validate(
                    {"data": content["data"], "field_meta": content["field_meta"]}), ir, spec)
                if content["status"] != "succeeded":
                    raise Conflict("CRITICAL_VALIDATION_FAILED")
            run.result_revision += 1
            run.status = "succeeded" if decision == "accept" else "rejected"
            content = {**content, "human_review": {"actor": actor, "reason": reason, "decision": decision}}
            session.add(Result(run_id=run.id, revision=run.result_revision, content=content,
                               organization_id=self.config.organization_id))
            review.status = "resolved"
            self.audit(session, "review." + decision, review.id, actor,
                       {"reason": reason, "result_revision": run.result_revision})
            return run

    def expire_runs(self):
        with self.db.transaction() as session:
            for run in session.scalars(select(Run).where(Run.status.in_(["queued", "running"]),
                    Run.deadline < time.time(), Run.organization_id == self.config.organization_id).with_for_update()):
                run.status, run.diagnostics = "failed", {"code": "RUN_DEADLINE_EXCEEDED"}
                self.audit(session, "run.expired", run.id)
