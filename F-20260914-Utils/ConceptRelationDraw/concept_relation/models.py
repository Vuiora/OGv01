from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlparse

RELATION_LABELS = {
    "part_of": "属于", "prerequisite": "是前提", "explains": "解释", "causes": "导致",
    "contrasts": "对比", "applies_to": "应用于", "depends_on": "依赖", "related": "关联",
}
RelationType = Literal["part_of", "prerequisite", "explains", "causes", "contrasts", "applies_to", "depends_on", "related"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConceptCandidate(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1, max_length=1200)
    quotes: list[str] = Field(min_length=1, max_length=5)


class Extraction(StrictModel):
    concepts: list[ConceptCandidate] = Field(max_length=100)


class KeyConceptChoice(StrictModel):
    id: str
    importance_reason: str = Field(min_length=1, max_length=600)
    duplicate_ids: list[str] = Field(default_factory=list, max_length=100)


class KeyConceptSelection(StrictModel):
    concepts: list[KeyConceptChoice] = Field(max_length=100)


class RelationCandidate(StrictModel):
    source: str
    target: str
    type: RelationType
    explanation: str = Field(min_length=1, max_length=1200)
    quotes: list[str] = Field(min_length=1, max_length=5)
    confidence: float = Field(ge=0, le=1)


class Relations(StrictModel):
    relations: list[RelationCandidate] = Field(max_length=500)


class KeyRelationSelection(StrictModel):
    relation_ids: list[str] = Field(max_length=500)


class LaunchRequest(StrictModel):
    mode: Literal["n8n", "direct"] = "n8n"


class ReviewRequest(StrictModel):
    status: Literal["unreviewed", "accepted", "rejected"]


class ModelConfig(StrictModel):
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_style: Literal["responses", "chat"] = "responses"
    api_key: str | None = Field(default=None, max_length=2000)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("请填写完整的 HTTP(S) 基础接口地址，不包含凭据、查询或片段。")
        return value.rstrip("/")
