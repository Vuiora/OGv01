"""Generate importable n8n workflows; no API keys are embedded."""
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]


def node(name, kind, version, parameters, x, y=0, **extra):
    return {"id": str(uuid5(NAMESPACE_URL, name + kind)), "name": name, "type": f"n8n-nodes-base.{kind}",
            "typeVersion": version, "parameters": parameters, "position": [x, y], **extra}


def workflow(file_mode=False):
    mode = "file" if file_mode else "text"
    auth = {"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"}
    submit = {"method": "POST", "url": "http://api:8000/v1/jobs" + ("" if file_mode else "/text"),
              **auth, "sendBody": True, "options": {"timeout": 60000}}
    if file_mode:
        submit.update({"contentType": "multipart-form-data", "bodyParameters": {"parameters": [
            {"parameterType": "formBinaryData", "name": "files", "inputDataFieldName": "={{ Object.keys($binary)[0] }}"},
            {"name": "title", "value": "={{ $json.body.title || '知识层级文档' }}"},
            {"name": "formats", "value": "md,docx,pdf"},
        ]}})
    else:
        submit.update({"specifyBody": "json", "jsonBody": "={{ JSON.stringify({title: $json.body.title || '知识层级文档', text: $json.body.text, formats: $json.body.formats || ['md','docx','pdf']}) }}"})

    def condition(expression):
        return {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{"id": "check", "leftValue": expression, "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "options": {}}

    nodes = [
        node("Receive", "webhook", 2, {"httpMethod": "POST", "path": f"clc-{mode}", "authentication": "headerAuth",
             "responseMode": "responseNode", "options": {}}, 0, webhookId=str(uuid5(NAMESPACE_URL, f"clc-{mode}"))),
        node("Submit", "httpRequest", 4.2, submit, 240),
        node("Accepted", "respondToWebhook", 1.4, {"respondWith": "firstIncomingItem", "options": {"responseCode": 202}}, 480),
        node("Wait", "wait", 1.1, {"resume": "timeInterval", "amount": 10, "unit": "seconds"}, 720,
             webhookId=str(uuid5(NAMESPACE_URL, f"clc-wait-{mode}"))),
        node("Status", "httpRequest", 4.2, {**auth, "url": "={{ 'http://api:8000/v1/jobs/' + $('Submit').first().json.id }}",
             "options": {"timeout": 30000}}, 960),
        node("Terminal", "if", 2.2, condition("={{ ['succeeded','failed'].includes($json.status) }}"), 1200),
        node("Succeeded", "if", 2.2, condition("={{ $json.status === 'succeeded' }}"), 1440, -80),
        node("Download", "httpRequest", 4.2, {**auth, "url": "={{ 'http://api:8000/v1/jobs/' + $('Submit').first().json.id + '/bundle' }}",
             "options": {"timeout": 60000, "response": {"response": {"responseFormat": "file", "outputPropertyName": "data"}}}}, 1680, -160),
        node("Failed", "stopAndError", 1, {"errorMessage": "={{ $json.error || 'CLC task failed' }}"}, 1680, 80),
        node("Setup notes", "stickyNote", 1, {"content": "## 使用前配置\nReceive 选择 Header Auth 凭据保护入口。\nSubmit / Status / Download 选择同一份 Header Auth：X-API-Key = .env 中 CLC_API_KEY。\n默认 Docker 内网 api:8000；宿主机服务请改地址。\n入口立刻返回 202；任务结束后 Download 保存 ZIP 到执行记录。\n不自动发送消息，不携带任何真实密钥。", "height": 290, "width": 580}, 0, -360),
    ]
    connections = {}

    def connect(source, target, branch=0):
        outputs = connections.setdefault(source, {"main": []})["main"]
        while len(outputs) <= branch:
            outputs.append([])
        outputs[branch].append({"node": target, "type": "main", "index": 0})

    for first, second in [("Receive", "Submit"), ("Submit", "Accepted"), ("Accepted", "Wait"),
                          ("Wait", "Status"), ("Status", "Terminal"), ("Terminal", "Succeeded"),
                          ("Succeeded", "Download")]:
        connect(first, second)
    connect("Terminal", "Wait", 1)
    connect("Succeeded", "Failed", 1)
    return {"name": f"CLC {mode} to knowledge", "nodes": nodes, "connections": connections, "active": False,
            "settings": {"executionOrder": "v1", "executionTimeout": 7200}, "pinData": {}, "tags": []}


if __name__ == "__main__":
    folder = ROOT / "n8n"
    folder.mkdir(exist_ok=True)
    for is_file in [False, True]:
        path = folder / ("file-to-knowledge.json" if is_file else "text-to-knowledge.json")
        path.write_text(json.dumps(workflow(is_file), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path.name)
