import hashlib
import json
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputPolicy(Contract):
    formats: list[Literal["pdf", "png", "jpeg"]] = ["pdf", "png", "jpeg"]
    languages: list[str] = ["zh", "en"]
    max_pages: int = Field(20, ge=1, le=100)
    max_file_bytes: int = Field(20971520, ge=1, le=104857600)


class Policy(Contract):
    max_student_repairs_per_step: int = Field(1, ge=0, le=1)
    max_teacher_escalations_per_run: int = Field(0, ge=0, le=1)
    max_model_requests_per_run: int = Field(24, ge=1, le=100)
    max_run_seconds: int = Field(600, ge=10, le=3600)
    max_input_chars_per_chunk: int = Field(12000, ge=1000, le=50000)
    max_output_tokens: int = Field(4000, ge=100, le=16000)
    max_total_tokens: int = Field(100000, ge=1000, le=1000000)
    max_cost_usd: float = Field(1.0, gt=0, le=100)


class Rule(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: Literal["required", "line_amounts", "order_total"]
    fields: list[str] = []
    tolerance: str = "0.01"
    severity: Literal["error", "warning"] = "error"


class AppSpec(Contract):
    schema_version: Literal["0.1"] = "0.1"
    app_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    app_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(max_length=4000)
    target: Literal["n8n"] = "n8n"
    input: InputPolicy = InputPolicy()
    policy: Policy = Policy()
    output_schema: dict[str, Any]
    prompt: str = Field(min_length=10, max_length=20000)
    rules: list[Rule]
    unresolved_questions: list[str] = []
    critical_fields: list[str] = ["order_number", "currency", "total_amount"]
    total_formula: Literal["sum_lines", "sum_lines_plus_tax_shipping_minus_discount", "unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_contract(self):
        Draft202012Validator.check_schema(self.output_schema)
        if self.output_schema.get("type") != "object":
            raise ValueError("Output schema must be an object")
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Rule IDs must be unique")
        # Remote references could cause network access during validation.
        def check(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and not str(value).startswith("#"):
                        raise ValueError("Remote schema references are prohibited")
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(self.output_schema)
        return self


class Evidence(Contract):
    document_id: str
    parse_id: str
    block_id: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=1)


class FieldMeta(Contract):
    state: Literal["extracted", "not_found", "unreadable", "conflict", "derived", "human_corrected"]
    evidence: list[Evidence] = []


class Extraction(Contract):
    data: dict[str, Any]
    field_meta: dict[str, FieldMeta]


class Block(Contract):
    id: str
    page: int = Field(ge=1)
    text: str
    kind: Literal["text", "table"] = "text"
    bbox: list[float] | None = None


class DocumentIR(Contract):
    document_id: str
    parse_id: str
    parser: str
    source_sha256: str
    pages: int
    blocks: list[Block]
    warnings: list[str] = []
    complete: bool = True


STEPS = ("parse", "select", "extract", "validate", "finalize")
TERMINAL = {"succeeded", "failed", "rejected", "cancelled"}
