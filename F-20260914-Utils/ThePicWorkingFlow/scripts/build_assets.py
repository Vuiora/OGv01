"""Generate importable n8n workflow, schemas, and an editable workflow diagram."""
from pathlib import Path
import json
import shutil
import uuid
from architecture_flow.models import Architecture
from architecture_flow.drawio import render, lint

BASE=Path(__file__).resolve().parents[1]


def workflow():
    nodes=[];connections={}
    credential={"httpHeaderAuth":{"id":"CONFIGURE_HEADER_AUTH","name":"Architecture Flow API"}}
    def add(name,typ,parameters,x,y=240,version=1,**extra):
        node={"id":str(uuid.uuid5(uuid.NAMESPACE_URL,"architecture-flow/"+name)),"name":name,"type":"n8n-nodes-base."+typ,
              "typeVersion":version,"parameters":parameters,"position":[x,y],**extra}
        nodes.append(node);return name
    def connect(a,b,branch=0):
        branches=connections.setdefault(a,{"main":[]})["main"]
        while len(branches)<=branch:branches.append([])
        branches[branch].append({"node":b,"type":"main","index":0})
    add("架构任务 Webhook","webhook",{"httpMethod":"POST","path":"drawio-architecture","authentication":"headerAuth","responseMode":"responseNode","options":{}},0,version=2.1,credentials=credential,webhookId="architecture-flow-v1")
    def http(name,stage,x,y=240):
        url="http://architecture-api:8080/v1/jobs" if stage is None else "={{ 'http://architecture-api:8080/v1/jobs/' + $json.job_id + '/stages/"+stage+"' }}"
        params={"method":"POST","url":url,"authentication":"genericCredentialType","genericAuthType":"httpHeaderAuth","options":{"timeout":600000}}
        if stage is None:params.update(sendBody=True,specifyBody="json",jsonBody="={{ $json.body }}")
        add(name,"httpRequest",params,x,y,4.2,credentials=credential,retryOnFail=stage not in {None,"repair"},maxTries=2,waitBetweenTries=1000)
    http("创建持久化任务",None,240)
    add("立即返回任务 ID","respondToWebhook",{"respondWith":"json","responseBody":"={{ $json }}","options":{"responseCode":202}},480,version=1.4)
    steps=[("代码与 Docling 文档接入","ingest"),("大模型分析代码架构","analyze"),("大模型设计模块与连接","design"),("应用项目样式并生成分页","render"),("校验总图 模块与风格","check")]
    previous="立即返回任务 ID"
    connect("架构任务 Webhook","创建持久化任务");connect("创建持久化任务",previous)
    for i,(name,stage) in enumerate(steps):
        http(name,stage,720+i*260);connect(previous,name);previous=name
    def condition(name,field,x,y):
        add(name,"if",{"conditions":{"options":{"caseSensitive":True,"leftValue":"","typeValidation":"strict","version":2},
            "conditions":[{"id":str(uuid.uuid5(uuid.NAMESPACE_URL,name)),"leftValue":"={{ $json."+field+" }}","rightValue":True,"operator":{"type":"boolean","operation":"true","singleValue":True}}],"combinator":"and"},"options":{}},x,y,2.2)
    condition("风格检查通过？","quality_passed",2020,240);connect(previous,"风格检查通过？")
    http("发布 drawio 与报告","publish",2320,120);connect("风格检查通过？","发布 drawio 与报告",0)
    condition("仍可修复？","can_repair",2320,400);connect("风格检查通过？","仍可修复？",1)
    http("模型按检查反馈修复","repair",2030,590);connect("仍可修复？","模型按检查反馈修复",0);connect("模型按检查反馈修复","应用项目样式并生成分页")
    http("拒绝发布并保留报告","reject",2620,480);connect("仍可修复？","拒绝发布并保留报告",1)
    add("使用说明","stickyNote",{"content":"## n8n + Docling 架构设计\n1. 所有认证节点选择同一 Header Auth 凭据：X-API-Key = APP_API_KEY。\n2. Compose 中 API 地址为 architecture-api:8080；本机 npm 测试请替换为 127.0.0.1:8080。\n3. 发布工作流后 POST /webhook/drawio-architecture。立即返回 job_id，随后查询 API 任务状态。\n4. 修复最多两轮；只有通过 XML、模块归属与样式校验才发布。\n5. 厂商 LLM Key 仅配置在架构服务 .env。", "width":680,"height":310},0,-130,1)
    return {"id":"ArchitectureFlowV1","name":"Architecture Flow · n8n + Docling · 项目风格 drawio","nodes":nodes,"connections":connections,
            "settings":{"executionOrder":"v1","executionTimeout":3600,"saveExecutionProgress":True},"active":False,
            "versionId":str(uuid.uuid5(uuid.NAMESPACE_URL,"architecture-flow/workflow/v1")),"pinData":{},"tags":[]}


def workflow_architecture():
    modules=[{"id":"orchestration","label":"n8n 编排模块","kind":"infrastructure"},
             {"id":"architecture-service","label":"架构服务模块","kind":"infrastructure"},
             {"id":"documents","label":"Docling 文档模块","kind":"data"},
             {"id":"models","label":"大模型 API 模块","kind":"data"}]
    components=[]
    for module,items in [("orchestration",[("webhook","Webhook / 立即返回任务 ID"),("stages","阶段编排 / 有界修复循环")]),
                         ("architecture-service",[("collector","代码快照 / AST 符号 / 来源行"),("analysis","架构分析 / 证据聚合"),("design","模块与数据线结构化设计"),("style","项目风格 / 总图 / 模块分页"),("quality","独立风格与结构检查"),("publish","校验通过后发布"),("delivery","工作台 / 状态查询 / 下载文件")]),
                         ("documents",[("docling","文档转换 / OCR / Markdown")]),
                         ("models",[("analysis-model","分析模型 / 兼容 API"),("design-model","设计与修复模型 / 兼容 API")])]:
        for ident,label in items:components.append({"id":ident,"label":label,"module":module,"status":"proposed"})
    triples=[("webhook","stages","control"),("stages","collector","dispatch"),("collector","docling","raw"),("docling","analysis","standard"),
             ("collector","analysis","standard"),("analysis","analysis-model","analysis"),("analysis-model","design","analysis"),
             ("design","design-model","analysis"),("design-model","style","analysis"),("style","quality","standard"),
             ("quality","design","control"),("quality","publish","control"),("publish","delivery","result")]
    return Architecture.model_validate({"title":"代码架构分析与 draw.io 设计工作流","modules":modules,"components":components,
        "connections":[{"id":f"flow-{i}","source":a,"target":b,"kind":k} for i,(a,b,k) in enumerate(triples)],
        "assumptions":["流程设计图；运行实现以 architecture_flow 与 n8n/workflow.json 为准。"]})


def main():
    (BASE/"n8n").mkdir(exist_ok=True)
    (BASE/"examples").mkdir(exist_ok=True)
    (BASE/"schemas").mkdir(exist_ok=True)
    (BASE/"n8n/workflow.json").write_text(json.dumps(workflow(),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (BASE/"schemas/architecture.schema.json").write_text(json.dumps(Architecture.model_json_schema(),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    request={"repository":"ParallelLogicDeterminationAlgorithModule","documents":["ParallelLogicDeterminationAlgorithModule/docs/DESIGN.md"],
             "module_names":[],"preserve_page_ids":[]}
    (BASE/"examples/request.json").write_text(json.dumps(request,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    template=(BASE.parent/"TheStructure.drawio").read_bytes()
    arch=workflow_architecture()
    xml,style=render(arch,template,[])
    report=lint(xml,arch,style,[],template)
    if not report.passed:raise ValueError(report.model_dump_json(indent=2))
    target=BASE/"TheWorkingflow.drawio"
    backup=BASE/"examples/TheWorkingflow.before.drawio"
    if target.exists() and not backup.exists():shutil.copy2(target,backup)
    target.write_bytes(xml)
    (BASE/"examples/workflow-architecture.json").write_text(arch.model_dump_json(indent=2)+"\n",encoding="utf-8")
    (BASE/"examples/workflow-quality.json").write_text(report.model_dump_json(indent=2)+"\n",encoding="utf-8")
    print(f"Generated workflow JSON and {report.page_count} editable draw.io pages")


if __name__=="__main__":main()
