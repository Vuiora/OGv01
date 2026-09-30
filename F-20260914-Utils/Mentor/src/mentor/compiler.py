import copy
import hashlib
import uuid

from mentor.domain import STEPS, AppSpec, canonical, digest

N8N_VERSION = "2.39.8"


def compile_spec(spec: AppSpec) -> dict:
    spec_hash = digest(spec.model_dump())
    nodes, connections = [], {}
    webhook_path = f"mentor/{spec.app_id}/{spec.app_version}/{spec_hash[:12]}"

    def node(name, kind, params, version, x, credentials=None):
        item = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{spec_hash}/{name}")), "name": name,
                "type": f"n8n-nodes-base.{kind}", "typeVersion": version,
                "position": [x, 0], "parameters": params}
        if credentials:
            item["credentials"] = credentials
        nodes.append(item)

    def link(source, target, branch=0):
        outputs = connections.setdefault(source, {"main": []})["main"]
        while len(outputs) <= branch:
            outputs.append([])
        outputs[branch].append({"node": target, "type": "main", "index": 0})

    service = {"httpHeaderAuth": {"id": "__MENTOR_SERVICE_CREDENTIAL__", "name": "Mentor Runtime"}}
    trigger = {"httpHeaderAuth": {"id": "__MENTOR_WEBHOOK_CREDENTIAL__", "name": "Mentor Trigger"}}
    node("Start", "webhook", {"httpMethod": "POST", "path": webhook_path,
                              "authentication": "headerAuth", "responseMode": "onReceived",
                              "options": {}}, 2.1, 0, trigger)
    nodes[-1]["webhookId"] = nodes[-1]["id"]
    node("Failed", "stopAndError", {"errorMessage": "Mentor task failed. Inspect run diagnostics."}, 1, 2000)
    previous = "Start"
    for index, step in enumerate(STEPS):
        create, query, ready, pending, wait = [f"{part}_{step}" for part in ("Create", "Query", "Ready", "Pending", "Wait")]
        x = 300 + index * 600
        node(create, "httpRequest", {
            "method": "POST",
            "url": "={{ '__MENTOR_RUNTIME_URL__/internal/v1/runs/' + $('Start').first().json.body.run_id + '/steps/" + step + "/tasks' }}",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "sendBody": True, "specifyBody": "json", "jsonBody": canonical({"spec_hash": spec_hash}),
            "options": {"timeout": 15000}}, 4.2, x, service)
        node(query, "httpRequest", {
            "url": "={{ '__MENTOR_RUNTIME_URL__/internal/v1/tasks/' + $('" + create + "').first().json.task_id }}",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "options": {"timeout": 15000}}, 4.2, x + 100, service)
        for name, expected in ((ready, "succeeded"), (pending, "pending")):
            node(name, "if", {"conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{"id": name, "leftValue": "={{ $json.state }}", "rightValue": expected,
                                "operator": {"type": "string", "operation": "equals"}}],
                "combinator": "and"}, "options": {}}, 2.2, x + 200)
        node(wait, "wait", {"resume": "timeInterval", "amount": 2, "unit": "seconds"}, 1.1, x + 300)
        link(previous, create)
        link(create, query)
        link(query, ready)
        link(ready, pending, 1)
        link(pending, wait)
        link(pending, "Failed", 1)
        link(wait, query)
        previous = ready
    workflow = {"name": f"Mentor {spec.app_id} {spec.app_version}", "nodes": nodes,
                "connections": connections, "settings": {"executionOrder": "v1",
                "executionTimeout": spec.policy.max_run_seconds + 30,
                "saveDataSuccessExecution": "none", "saveDataErrorExecution": "none",
                "saveManualExecutions": False}}
    files = {"app-spec.json": spec.model_dump(), "workflow.n8n.json": workflow,
             "execution-plan.json": {"steps": list(STEPS), "spec_hash": spec_hash},
             "runtime-config.json": {"schema_version": "0.1", "spec_hash": spec_hash}}
    manifest = {"compiler_version": "0.1.0", "target": "n8n", "target_version": N8N_VERSION,
                "files": {name: hashlib.sha256(canonical(content).encode()).hexdigest()
                          for name, content in files.items()}}
    return {"build_digest": digest(manifest), "manifest": manifest, "files": files,
            "webhook_path": webhook_path}


def bind_workflow(bundle: dict, runtime_url: str, service_credential: str, webhook_credential: str) -> dict:
    from urllib.parse import urlparse
    parsed = urlparse(runtime_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "'" in runtime_url:
        raise ValueError("Invalid runtime URL")
    value = canonical(copy.deepcopy(bundle["files"]["workflow.n8n.json"]))
    replacements = {"__MENTOR_RUNTIME_URL__": runtime_url.rstrip("/"),
                    "__MENTOR_SERVICE_CREDENTIAL__": service_credential,
                    "__MENTOR_WEBHOOK_CREDENTIAL__": webhook_credential}
    import json
    # Replace values recursively, preserving JSON escaping for supplied IDs and URLs.
    def replace(item):
        if isinstance(item, str):
            for key, replacement in replacements.items():
                item = item.replace(key, replacement)
            return item
        if isinstance(item, dict):
            return {k: replace(v) for k, v in item.items()}
        if isinstance(item, list):
            return [replace(v) for v in item]
        return item
    return replace(json.loads(value))
