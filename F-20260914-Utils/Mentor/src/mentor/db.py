import time
import uuid
from contextlib import contextmanager
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def uid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Application(Identity, Base):
    __tablename__ = "applications"
    name: Mapped[str] = mapped_column(String(200))
    active_deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Revision(Identity, Base):
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("app_id", "version"),)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    version: Mapped[str] = mapped_column(String(40))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)


class Build(Identity, Base):
    __tablename__ = "builds"
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    digest: Mapped[str] = mapped_column(String(64), index=True)
    bundle: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="built")
    verification: Mapped[dict] = mapped_column(JSON, default=dict)


class Deployment(Identity, Base):
    __tablename__ = "deployments"
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"))
    environment: Mapped[str] = mapped_column(String(40))
    workflow_id: Mapped[str] = mapped_column(String(80))
    webhook_path: Mapped[str] = mapped_column(String(200))
    model_binding: Mapped[dict] = mapped_column(JSON)


class Document(Identity, Base):
    __tablename__ = "documents"
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(80))
    pages: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(Integer)


class Run(Identity, Base):
    __tablename__ = "runs"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"))
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    current_step: Mapped[str] = mapped_column(String(40), default="parse")
    deadline: Mapped[float] = mapped_column(Float)
    result_revision: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    tokens_reserved: Mapped[int] = mapped_column(Integer, default=0)
    cost_reserved: Mapped[float] = mapped_column(Float, default=0)
    cost_actual: Mapped[float] = mapped_column(Float, default=0)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    model_binding: Mapped[dict] = mapped_column(JSON, default=dict)


class Task(Identity, Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "step"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class Result(Identity, Base):
    __tablename__ = "results"
    __table_args__ = (UniqueConstraint("run_id", "revision"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)


class Review(Identity, Base):
    __tablename__ = "reviews"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")


class Evaluation(Identity, Base):
    __tablename__ = "evaluations"
    build_id: Mapped[str] = mapped_column(ForeignKey("builds.id"))
    report: Mapped[dict] = mapped_column(JSON)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelCall(Identity, Base):
    __tablename__ = "model_calls"
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0)
    reservation: Mapped[float] = mapped_column(Float)
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    price_snapshot: Mapped[dict] = mapped_column(JSON)


class Audit(Identity, Base):
    __tablename__ = "audits"
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Outbox(Identity, Base):
    __tablename__ = "outbox"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[float] = mapped_column(Float, default=0)


class Idempotency(Base):
    __tablename__ = "idempotency"
    scope: Mapped[str] = mapped_column(String(250), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(64))


class Database:
    def __init__(self, url: str):
        options: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.engine = create_engine(url, **options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            yield session

