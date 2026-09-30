import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from jsonschema import Draft202012Validator

from mentor.domain import AppSpec, DocumentIR, Extraction


def pointer(data, path):
    value = data
    for part in path.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def normal(value) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def money(value) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ValueError("Amount must be an unambiguous decimal string")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Amount must be finite")
    return result


def validate(extraction: Extraction, ir: DocumentIR, spec: AppSpec) -> dict:
    checks = []

    def record(rule_id, status, paths, message, severity="error"):
        checks.append(dict(rule_id=rule_id, status=status, field_paths=paths,
                           message=message, severity=severity))

    schema_errors = list(Draft202012Validator(spec.output_schema).iter_errors(extraction.data))
    record("output_schema", "fail" if schema_errors else "pass", [],
           "; ".join(e.message for e in schema_errors) or "Output matches schema")
    record("document_complete", "pass" if ir.complete else "fail", [], "Document completeness")
    blocks = {block.id: block for block in ir.blocks}
    leaves = {}

    def walk(value, prefix=""):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, prefix + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, prefix + f"/{i}")
        else:
            leaves[prefix] = value
    walk(extraction.data)
    for path, value in leaves.items():
        meta = extraction.field_meta.get(path)
        if value is None:
            continue
        valid = bool(meta and meta.state in {"extracted", "human_corrected"} and meta.evidence)
        if valid:
            valid = False
            for ev in meta.evidence:
                block = blocks.get(ev.block_id)
                if (block and ev.document_id == ir.document_id and ev.parse_id == ir.parse_id
                        and ev.page == block.page and normal(ev.quote) in normal(block.text)
                        and normal(value) in normal(ev.quote)):
                    valid = True
                    break
        record("evidence" + path, "pass" if valid else "fail", [path], "Source evidence supports value")
    for rule in spec.rules:
        status, paths = "pass", []
        try:
            if rule.type == "required":
                paths = ["/" + f for f in rule.fields if extraction.data.get(f) in (None, "")]
                status = "fail" if paths else "pass"
            elif rule.type == "line_amounts":
                items = extraction.data.get("items")
                if not items:
                    status = "unknown"
                else:
                    for i, item in enumerate(items):
                        expected = (money(item["quantity"]) * money(item["unit_price"])).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP)
                        if abs(expected - money(item["line_amount"])) > money(rule.tolerance):
                            paths.append(f"/items/{i}/line_amount")
                    status = "fail" if paths else "pass"
            elif rule.type == "order_total":
                items = extraction.data.get("items")
                if not items or spec.total_formula == "unknown":
                    status = "unknown"
                else:
                    total = sum((money(i["line_amount"]) for i in items), Decimal(0))
                    if spec.total_formula == "sum_lines_plus_tax_shipping_minus_discount":
                        total += money(extraction.data["tax_amount"]) + money(extraction.data["shipping_amount"])
                        total -= money(extraction.data["discount_amount"])
                    status = "pass" if abs(total - money(extraction.data["total_amount"])) <= money(rule.tolerance) else "fail"
                    paths = ["/total_amount"]
        except (KeyError, ValueError, TypeError, InvalidOperation):
            status = "unknown"
        record(rule.id, status, paths, f"{rule.type}: {status}", rule.severity)
    accepted = all(c["status"] == "pass" for c in checks if c["severity"] == "error")
    return {"status": "succeeded" if accepted else "needs_review", "data": extraction.data,
            "field_meta": {key: value.model_dump() for key, value in extraction.field_meta.items()},
            "validation": checks, "parser": ir.parser, "warnings": ir.warnings}
