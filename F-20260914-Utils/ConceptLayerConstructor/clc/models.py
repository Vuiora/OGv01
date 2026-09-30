from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal["domain", "category", "policy", "implementation", "mechanism", "concept", "example"]
RelationKind = Literal["is_a", "part_of", "implements", "handled_by", "depends_on", "contrasts_with", "uses"]
Format = Literal["md", "docx", "pdf"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Citation(StrictModel):
    chunk_id: str
    quote: str = Field(min_length=4, max_length=2000)


class Concept(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    kind: Kind
    summary: str = Field(min_length=1, max_length=600)
    explanation: str = Field(min_length=1, max_length=5000)
    scope: str = Field(default="原文未明确", max_length=500)
    pitfalls: list[str] = Field(default_factory=list, max_length=8)
    examples: list[str] = Field(default_factory=list, max_length=5)
    evidence: list[Citation] = Field(min_length=1, max_length=30)


class Extraction(StrictModel):
    concepts: list[Concept] = Field(max_length=20)


class Category(StrictModel):
    id: str = Field(pattern=r"^g-[a-z0-9-]{1,50}$")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=600)
    parent_id: str | None = None


class Placement(StrictModel):
    id: str
    parent_id: str | None


class Relation(StrictModel):
    source: str
    target: str
    kind: RelationKind
    explanation: str = Field(min_length=1, max_length=1000)
    evidence: list[Citation] = Field(min_length=1, max_length=10)


class Outline(StrictModel):
    categories: list[Category] = Field(default_factory=list, max_length=40)
    placements: list[Placement]
    relations: list[Relation] = Field(default_factory=list, max_length=1000)


class Node(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    title: str
    kind: Kind
    parent_id: str | None = None
    summary: str
    explanation: str = ""
    scope: str = "原文未明确"
    pitfalls: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    evidence: list[Citation] = Field(default_factory=list)
    synthetic: bool = False


class Graph(StrictModel):
    schema_version: str = "1.0"
    title: str
    nodes: list[Node]
    relations: list[Relation] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_tree(self):
        by_id = {n.id: n for n in self.nodes}
        if not by_id or len(by_id) != len(self.nodes):
            raise ValueError("知识点为空或 ID 重复")
        for node in self.nodes:
            if not node.synthetic and not node.evidence:
                raise ValueError(f"知识点缺少引用: {node.id}")
            seen = {node.id}
            parent = node.parent_id
            while parent is not None:
                if parent not in by_id:
                    raise ValueError(f"父节点不存在: {parent}")
                if parent in seen:
                    raise ValueError("层级存在环")
                seen.add(parent)
                if len(seen) > 12:
                    raise ValueError("层级深度超过 12")
                parent = by_id[parent].parent_id
        for edge in self.relations:
            if edge.source not in by_id or edge.target not in by_id or edge.source == edge.target:
                raise ValueError("关系引用无效节点")
        return self


class TextRequest(StrictModel):
    # Source text must retain its original whitespace and character offsets.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    title: str = Field(default="知识层级文档", min_length=1, max_length=160)
    text: str = Field(min_length=1)
    formats: list[Format] = Field(default_factory=lambda: ["md", "docx", "pdf"], min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_title(cls, value):
        if isinstance(value, dict) and isinstance(value.get("title"), str):
            value = {**value, "title": value["title"].strip()}
        return value
