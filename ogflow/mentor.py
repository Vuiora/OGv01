"""Use Mentor's AppSpec validation for a versioned business application contract."""
import uuid
from mentor.domain import AppSpec
from .models import TaskSpec
from .store import digest

STAGES = ["observations", "knowledge", "recommendation", "hypotheses", "freeze", "confirmation", "archive", "feedback", "architecture"]

def build_application():
    app = AppSpec(app_id="ogv01-discovery", app_version="0.2.0",
        description="Observation to meta-learning recommendation, SDL confirmation, archive and architecture production",
        output_schema={"type": "object", "properties": {"run_id": {"type": "string"},
            "state": {"enum": ["SUCCEEDED", "FAILED"]}, "artifacts": {"type": "object"}},
            "required": ["run_id", "state", "artifacts"]},
        prompt="Propose exploration expressions using declared variables and source evidence only; never assign confirmation grades.",
        rules=[], critical_fields=[], total_formula="unknown")
    spec_hash = digest(app.model_dump())
    # This compiler targets OGflow's implemented API, while the original invoice
    # compiler remains the separate Mentor runtime target.
    webhook = {"id": "start", "name": "Start discovery", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
        "position": [0, 0], "parameters": {"httpMethod": "POST", "path": "ogv01-discovery", "responseMode": "responseNode",
            "authentication": "headerAuth"}, "credentials": {"httpHeaderAuth": {"id": "CONFIGURE_OGFLOW_AUTH", "name": "OGflow API"}}}
    submit = {"id": "submit", "name": "Submit business task", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [260, 0], "parameters": {"method": "POST", "url": "={{ $env.OGFLOW_URL + '/v1/runs' }}",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth", "sendBody": True,
            "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}", "options": {"timeout": 30000}},
        "credentials": {"httpHeaderAuth": {"id": "CONFIGURE_OGFLOW_AUTH", "name": "OGflow API"}}}
    reply = {"id": "reply", "name": "Return task reference", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.4,
        "position": [520, 0], "parameters": {"respondWith": "json", "responseBody": "={{ JSON.stringify($json) }}", "options": {"responseCode": 202}}}
    workflow = {"name": "OGv01 discovery closed loop", "nodes": [webhook, submit, reply],
        "connections": {"Start discovery": {"main": [[{"node": submit["name"], "type": "main", "index": 0}]]},
            submit["name"]: {"main": [[{"node": reply["name"], "type": "main", "index": 0}]]}},
        "settings": {"executionOrder": "v1"}}
    return {"app_spec": app.model_dump(), "app_spec_digest": spec_hash, "task_schema": TaskSpec.model_json_schema(),
        "execution_plan": {"target": "ogflow-v1", "stages": STAGES,
            "student_executor": "ogflow.pipeline.BusinessPipeline", "teacher_contract": "mentor.domain.AppSpec"},
        "workflow": workflow}

