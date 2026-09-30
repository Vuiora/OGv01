"""Explicit input contracts; observation semantics are supplied by the task owner."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Confirmation(Strict):
    method: Literal["group_sign_test"] = "group_sign_test"
    alpha: float = Field(0.05, gt=0, lt=1)
    min_effect: float = Field(0.1, gt=0)
    min_groups: int = Field(10, ge=5, le=10000)
    independence_assumption: str = Field(min_length=10)

class TaskSpec(Strict):
    version: Literal[1] = 1
    dataset_id: str = Field(min_length=1, max_length=100)
    observations: str
    target: str = "Y"
    features: list[str] = Field(min_length=1, max_length=8)
    group_column: str = "batch"
    units: dict[str, str]
    environments: list[str] = ["observed"]
    split_seed: int = Field(20260930, ge=0, le=99999999)
    top_k: int = Field(3, ge=1, le=8)
    max_candidates: int = Field(48, ge=4, le=128)
    freeze_cap: int = Field(3, ge=1, le=8)
    n_resamples: int = Field(8, ge=0, le=50)
    llm: Literal["live", "offline"] = "live"
    confirmation: Confirmation
    documents: list[str] = Field(default_factory=list, max_length=10)
    architecture: bool = True
    feedback: bool = True
    synthetic: bool = False

    @model_validator(mode="after")
    def semantics(self):
        import re
        names = self.features + [self.target]
        if len(set(names)) != len(names) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) for n in names):
            raise ValueError("Feature/target names must be unique ASCII identifiers")
        if self.group_column in names or set(self.units) != set(names) or any(not v for v in self.units.values()):
            raise ValueError("Declare units for every numeric field and a separate group column")
        if not self.environments or self.top_k > len(self.features):
            raise ValueError("Environments and top_k must match the declared data")
        if self.llm == "offline" and self.documents:
            raise ValueError("Document knowledge extraction requires live LLM mode")
        return self

