import asyncio
from copy import deepcopy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from architecture_flow.api import create_app
from architecture_flow.config import FIXED_INSTRUCTIONS, Settings
from architecture_flow.drawio import render, lint, entries
from architecture_flow.ingest import collect, within
from architecture_flow.models import Architecture

ROOT=Path(__file__).resolve().parents[2]
TEMPLATE=(ROOT/"TheStructure.drawio").read_bytes()


@pytest.fixture
def architecture():
    return {"title":"测试架构","modules":[{"id":"device","label":"DEVICE_1","kind":"device"},
        {"id":"comm","label":"通信基础设施","kind":"infrastructure"},{"id":"data","label":"数据处理模块","kind":"data"}],
        "components":[{"id":"cpu","label":"CPU CORE","module":"device","status":"external","role":"core"},
        {"id":"plda","label":"PLDA","module":"comm","status":"observed","role":"plda","evidence":[{"path":"repo/app.py","line":1}]},
        {"id":"input","label":"输入接口","module":"comm","parent":"plda","status":"observed","evidence":[{"path":"repo/app.py","line":1}]},
        {"id":"dispatch","label":"任务分发","module":"comm","parent":"plda","status":"observed","evidence":[{"path":"repo/app.py","line":2}]},
        {"id":"payload","label":"原始数据","module":"data","status":"external","role":"data"}],
        "connections":[{"id":"raw-in","source":"payload","target":"input","kind":"raw"},
        {"id":"task","source":"input","target":"dispatch","kind":"dispatch"},{"id":"execute","source":"dispatch","target":"cpu","kind":"dispatch"},
        {"id":"return","source":"cpu","target":"input","kind":"result"}]}


@pytest.fixture
def settings(tmp_path):
    repo=tmp_path/"repo";repo.mkdir();(repo/"app.py").write_text("def run():\n    return 1\n",encoding="utf-8")
    (repo/"design.md").write_text("# Architecture\nA simple system.\n",encoding="utf-8")
    template=tmp_path/"style.drawio";template.write_bytes(TEMPLATE)
    return Settings(input_root=tmp_path,template=template,data_root=tmp_path/"state",api_key="test-key",
        analysis_url="http://llm.test/v1",analysis_model="analysis-model",analysis_key="private-key",
        design_url="http://llm.test/v1",design_model="design-model",design_key="private-key",docling_url="http://docling.test")


def fake_client(architecture, invalid_first=False):
    calls=[];designs=0
    def respond(request):
        nonlocal designs
        payload=json.loads(request.content);calls.append((request.url.path,payload))
        if request.url.path=="/v1/convert/source":
            assert payload["sources"][0]["kind"]=="file"
            return httpx.Response(200,json={"status":"success","document":{"md_content":"# Design\nInput to compute."}})
        if payload["model"]=="analysis-model":
            result={"summary":"Input is executed by compute.","responsibilities":[{"name":"run","evidence":[{"path":"repo/app.py","line":1}]}],"unknowns":[]}
        else:
            designs+=1;result=deepcopy(architecture)
            if invalid_first and designs==1:result["components"][1]["module"]="missing"
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(result)},"finish_reason":"stop"}],"usage":{"total_tokens":17}})
    return httpx.AsyncClient(transport=httpx.MockTransport(respond)),calls


def test_api_pipeline_docling_llm_and_gate(settings,architecture):
    upstream,calls=fake_client(architecture,True)
    with TestClient(create_app(settings,upstream)) as api:
        headers={"X-API-Key":"test-key"}
        body={"repository":"repo","documents":["repo/design.md"],"module_names":["DEVICE_1","通信基础设施","数据处理模块"]}
        assert api.post("/v1/jobs",json=body).status_code==401
        created=api.post("/v1/jobs",json=body,headers=headers)
        assert created.status_code==202;ident=created.json()["job_id"]
        for stage in ("ingest","analyze","design","render","check","publish"):
            result=api.post(f"/v1/jobs/{ident}/stages/{stage}",headers=headers)
            assert result.status_code==200,result.text
        assert result.json()["state"]=="SUCCEEDED"
        download=api.get(f"/v1/jobs/{ident}/artifacts/architecture.drawio",headers=headers)
        assert download.status_code==200
        assert len(ET.fromstring(download.content).findall("diagram"))==4
        assert len([c for c in calls if c[0].endswith("chat/completions") and c[1]["model"]=="design-model"])==2
        # Completed stage retries do not bill the model again.
        count=len(calls);api.post(f"/v1/jobs/{ident}/stages/analyze",headers=headers);assert len(calls)==count
        assert api.get(f"/v1/jobs/{ident}/artifacts/template.drawio",headers=headers).status_code==404


@pytest.mark.parametrize("specify_modules", [False, True])
def test_design_reference_does_not_impose_template_content(settings,specify_modules):
    candidate={"title":"任务服务","modules":[
        {"id":"gateway","label":"请求接入","kind":"infrastructure"},
        {"id":"worker","label":"任务处理","kind":"data"}],
        "components":[
            {"id":"receive","label":"接收请求","module":"gateway","evidence":[{"path":"repo/app.py","line":1}]},
            {"id":"execute","label":"执行任务","module":"worker","evidence":[{"path":"repo/app.py","line":2}]}],
        "connections":[{"id":"submit","source":"receive","target":"execute","kind":"dispatch"}]}
    names=[module["label"] for module in candidate["modules"]] if specify_modules else []
    upstream,calls=fake_client(candidate)
    with TestClient(create_app(settings,upstream)) as api:
        headers={"X-API-Key":"test-key"}
        config=api.get("/v1/client-config").json()
        assert config["module_names"]==[]
        assert config["instructions"]==FIXED_INSTRUCTIONS
        assert Path(config["default_repository"]).is_dir()
        created=api.post("/v1/jobs",headers=headers,json={"repository":"repo","module_names":names})
        assert created.status_code==202,created.text
        ident=created.json()["job_id"]
        record=api.app.state.pipeline.read(ident)
        assert record["request"]["module_names"]==names
        assert record["request"]["preserve_page_ids"]==[]
        for stage in ("ingest","analyze","design","render","check","publish"):
            response=api.post(f"/v1/jobs/{ident}/stages/{stage}",headers=headers)
            assert response.status_code==200,response.text
        assert response.json()["state"]=="SUCCEEDED"
        download=api.get(f"/v1/jobs/{ident}/artifacts/architecture.drawio",headers=headers)
        assert download.status_code==200
        pages=ET.fromstring(download.content).findall("diagram")
        assert [page.get("name") for page in pages]==["系统总览","请求接入","任务处理"]
        assert all(page.get("id")!="plda-architecture-dataflow-v1" for page in pages)
        assert "PLDA" not in download.text and "DEVICE_1" not in download.text
        design=next(payload for path,payload in calls if payload.get("model")=="design-model")
        design_data=json.loads(design["messages"][1]["content"])
        assert design_data["required_module_names"]==names
        assert design_data["instructions"]==FIXED_INSTRUCTIONS
        assert "本项目 DEVICE_1" not in design["messages"][0]["content"]
        styles=entries(pages[0])
        for module in candidate["modules"]:
            fill=design_data["style"]["palette"][module["kind"]]["fillColor"]
            assert f"fillColor={fill}" in styles["m-"+module["id"]][1].get("style")


@pytest.mark.parametrize("names", [[], ["请求接入","任务处理"]])
def test_n8n_launch_keeps_content_choices_and_design_principles(settings,names):
    forwarded=[]
    def respond(request):
        assert str(request.url)==settings.n8n_url
        forwarded.append(json.loads(request.content))
        return httpx.Response(202,json={"job_id":"test-job"})
    upstream=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with TestClient(create_app(settings,upstream)) as api:
        response=api.post("/v1/launch",headers={"X-API-Key":"test-key"},
            json={"repository":"repo","module_names":names,"instructions":"旧版设计要求"})
        assert response.status_code==202,response.text
    assert forwarded[0]["module_names"]==names
    assert forwarded[0]["preserve_page_ids"]==[]
    assert forwarded[0]["instructions"]==FIXED_INSTRUCTIONS


@pytest.mark.parametrize("mutation,expected",[("missing-page","PAGES"),("wrong-color","EDGE_STYLE"),("orphan","OWNERSHIP"),("overflow","CONTAINMENT"),("bad-link","LINK"),("overlap","OVERLAP")])
def test_linter_rejects_faults(architecture,mutation,expected):
    arch=Architecture.model_validate(architecture);xml,style=render(arch,TEMPLATE,[])
    tree=ET.fromstring(xml);overview=tree[0];items=entries(overview)
    if mutation=="missing-page":tree.remove(tree[-1])
    elif mutation=="wrong-color":
        cell=items["e-raw-in"][1];cell.set("style",cell.get("style").replace("#0000FF","#abcdef"))
    elif mutation=="orphan":items["n-input"][1].set("parent","1")
    elif mutation=="overflow":items["n-input"][1].find("mxGeometry").set("x","8000")
    elif mutation=="bad-link":items["m-comm"][0].set("link","data:page/id,missing")
    elif mutation=="overlap":items["n-dispatch"][1].find("mxGeometry").set("y",items["n-input"][1].find("mxGeometry").get("y"))
    review=lint(ET.tostring(tree),arch,style,[],TEMPLATE)
    assert not review.passed and expected in {e.code for e in review.errors}


def test_preservation_and_correct_module_count(architecture):
    arch=Architecture.model_validate(architecture);ids=["plda-architecture-dataflow-v1"]
    xml,style=render(arch,TEMPLATE,ids);review=lint(xml,arch,style,ids,TEMPLATE)
    assert review.passed and review.page_count==5 and review.module_count==3


def test_hierarchy_and_evidence_not_invented(architecture):
    candidate=deepcopy(architecture);candidate["components"][2]["module"]="data"
    with pytest.raises(ValueError):Architecture.model_validate(candidate)
    candidate=deepcopy(architecture);candidate["components"][1]["evidence"]=[]
    with pytest.raises(ValueError):Architecture.model_validate(candidate)


def test_budget_path_and_hidden_files(settings):
    with pytest.raises(ValueError):within(settings.input_root,"../outside")
    (settings.input_root/"repo/.env").write_text("SECRET=private")
    context=collect(settings.input_root,"repo",1000)
    assert [s["path"] for s in context["sources"]]==["repo/app.py"]
    with pytest.raises(ValueError):collect(settings.input_root,"repo",1)


def test_cancel_and_no_publish_before_review(settings,architecture):
    upstream,_=fake_client(architecture)
    with TestClient(create_app(settings,upstream)) as api:
        headers={"X-API-Key":"test-key"};ident=api.post("/v1/jobs",json={"repository":"repo"},headers=headers).json()["job_id"]
        assert api.post(f"/v1/jobs/{ident}/stages/publish",headers=headers).status_code==422
        api.post(f"/v1/jobs/{ident}/cancel",headers=headers)
        assert api.post(f"/v1/jobs/{ident}/stages/ingest",headers=headers).status_code==422
        assert api.get(f"/v1/jobs/{ident}",headers=headers).json()["state"]=="CANCELLED"


def test_publication_rechecks_actual_xml(settings,architecture):
    upstream,_=fake_client(architecture)
    with TestClient(create_app(settings,upstream)) as api:
        h={"X-API-Key":"test-key"};ident=api.post("/v1/jobs",json={"repository":"repo"},headers=h).json()["job_id"]
        for stage in ("ingest","analyze","design","render","check"):
            assert api.post(f"/v1/jobs/{ident}/stages/{stage}",headers=h).status_code==200
        file=settings.data_root/"jobs"/ident/"candidate.drawio"
        file.write_bytes(file.read_bytes().replace(b"#0000FF",b"#123456",1))
        assert api.post(f"/v1/jobs/{ident}/stages/publish",headers=h).status_code==422
        assert api.get(f"/v1/jobs/{ident}/artifacts/architecture.drawio",headers=h).status_code==409


@pytest.mark.parametrize("exhaust_budget", [False, True])
def test_quality_repair_and_budget(settings,architecture,exhaust_budget):
    upstream,calls=fake_client(architecture)
    with TestClient(create_app(settings,upstream)) as api:
        h={"X-API-Key":"test-key"};ident=api.post("/v1/jobs",json={"repository":"repo"},headers=h).json()["job_id"]
        def stage(name):
            response=api.post(f"/v1/jobs/{ident}/stages/{name}",headers=h)
            assert response.status_code==200,response.text
            return response.json()
        for name in ("ingest","analyze","design","render"):stage(name)
        def spoil():
            file=settings.data_root/"jobs"/ident/"candidate.drawio"
            tree=ET.fromstring(file.read_bytes());tree.remove(tree[-1]);file.write_bytes(ET.tostring(tree))
        spoil();assert stage("check")["quality_passed"] is False
        rounds=2 if exhaust_budget else 1
        for attempt in range(rounds):
            assert stage("repair")["revision"]==attempt+1
            stage("render")
            if exhaust_budget:spoil()
            checked=stage("check")
        if exhaust_budget:
            assert checked["can_repair"] is False
            assert api.post(f"/v1/jobs/{ident}/stages/repair",headers=h).status_code==422
            assert stage("reject")["state"]=="REJECTED"
            assert api.get(f"/v1/jobs/{ident}/artifacts/architecture.drawio",headers=h).status_code==409
        else:
            assert checked["quality_passed"] is True
            assert stage("publish")["state"]=="SUCCEEDED"


def test_long_nested_labels_keep_children_below_headers(architecture):
    from architecture_flow.drawio import lines
    architecture["components"][1]["label"]="并行可行性分析与调度"*10
    architecture["components"][2]["role"]="group"
    architecture["components"][2]["label"]="长名称内部子模块"*12
    architecture["components"][3]["parent"]="input"
    architecture["components"][4]["label"]="外部数据接口"*16
    arch=Architecture.model_validate(architecture)
    xml,style=render(arch,TEMPLATE,[])
    assert lint(xml,arch,style,[],TEMPLATE).passed
    items=entries(ET.fromstring(xml)[0])
    parent=items["n-input"][1].find("mxGeometry")
    child=items["n-dispatch"][1].find("mxGeometry")
    label_height=24*len(lines(architecture["components"][2]["label"],int(float(parent.get("width"))/10)))+24
    assert float(child.get("y"))>=label_height
