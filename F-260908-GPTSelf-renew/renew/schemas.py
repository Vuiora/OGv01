"""The handoff contracts between agents; all outputs use strict function schemas."""


def obj(**properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def arr(items, **kwargs):
    return {"type": "array", "items": items, **kwargs}


def string(**kwargs):
    return {"type": "string", **kwargs}


EVIDENCE = obj(path=string(), line={"type": "integer", "minimum": 1}, quote=string())
FINDING = obj(
    id=string(), kind=string(enum=["fix", "innovation"]), title=string(),
    problem=string(), evidence=arr(EVIDENCE, minItems=1),
    hypothesis=string(), expected_value=string(),
    confidence={"type": "number", "minimum": 0, "maximum": 1},
    impact={"type": "integer", "minimum": 1, "maximum": 5},
    effort={"type": "integer", "minimum": 1, "maximum": 5},
    risk=string(enum=["low", "medium", "high"]),
)
ANALYSIS = obj(project_summary=string(), findings=arr(FINDING, maxItems=8), limitations=arr(string()))
REQUIREMENT = obj(
    id=string(), title=string(), kind=string(enum=["fix", "innovation"]),
    finding_ids=arr(string(), minItems=1), problem=string(), outcome=string(),
    acceptance_criteria=arr(string(), minItems=1),
    allowed_files=arr(string(), minItems=1), dependencies=arr(string()),
)
PLAN = obj(strategy=string(), requirements=arr(REQUIREMENT, maxItems=10), deferred=arr(string()))
DEVELOPMENT = obj(summary=string(), changed_files=arr(string()), notes=arr(string()))
REVIEW = obj(
    approved={"type": "boolean"}, summary=string(),
    criteria=arr(obj(criterion=string(), passed={"type": "boolean"}, evidence=string())),
    issues=arr(string()),
)
