from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .config import FIXED_INSTRUCTIONS

FlowKind = Literal["raw", "standard", "analysis", "dispatch", "result", "control"]
Role = Literal["component", "group", "plda", "core", "data"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(Strict):
    path: str = Field(min_length=1, max_length=240)
    line: int = Field(ge=1)


class Region(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    label: str = Field(min_length=1, max_length=60)
    kind: Literal["device", "infrastructure", "data"]


class Component(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    label: str = Field(min_length=1, max_length=100)
    module: str
    parent: str | None = None
    role: Role = "component"
    evidence: list[SourceRef] = Field(default_factory=list, max_length=10)
    status: Literal["observed", "proposed", "external"] = "observed"


class Connection(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    source: str
    target: str
    kind: FlowKind
    label: str = Field(default="", max_length=60)


class Architecture(Strict):
    version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=80)
    modules: list[Region] = Field(min_length=1, max_length=8)
    components: list[Component] = Field(min_length=1, max_length=80)
    connections: list[Connection] = Field(default_factory=list, max_length=120)
    assumptions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def graph(self):
        modules = {m.id: m for m in self.modules}
        nodes = {n.id: n for n in self.components}
        all_ids = [m.id for m in self.modules] + [n.id for n in self.components] + [e.id for e in self.connections]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("all semantic IDs must be globally unique")
        if len({m.label for m in self.modules}) != len(self.modules):
            raise ValueError("module labels must be unique")
        for n in self.components:
            if n.module not in modules:
                raise ValueError(f"unassigned component: {n.id}")
            if n.status == "observed" and not n.evidence:
                raise ValueError(f"observed component requires source evidence: {n.id}")
            seen = {n.id}
            parent = n.parent
            while parent:
                if parent in seen or parent not in nodes:
                    raise ValueError(f"invalid parent/cycle: {n.id}")
                p = nodes[parent]
                if p.module != n.module or p.role not in {"group", "plda"}:
                    raise ValueError(f"parent must be a group within the same module: {n.id}")
                seen.add(parent)
                parent = p.parent
        for m in modules:
            if not any(n.module == m for n in nodes.values()):
                raise ValueError(f"empty module: {m}")
        for e in self.connections:
            if e.source not in nodes or e.target not in nodes or e.source == e.target:
                raise ValueError(f"invalid edge endpoints: {e.id}")
        return self


class JobRequest(Strict):
    repository: str = Field(min_length=1, max_length=500)
    documents: list[str] = Field(default_factory=list, max_length=10)
    instructions: str = Field(default=FIXED_INSTRUCTIONS, max_length=8000)
    module_names: list[str] = Field(default_factory=list, max_length=8)
    preserve_page_ids: list[str] = Field(default_factory=list, max_length=10)


class Finding(Strict):
    code: str
    location: str
    message: str


class Review(Strict):
    passed: bool
    errors: list[Finding]
    page_count: int
    module_count: int
    warnings: list[str] = Field(default_factory=list)
