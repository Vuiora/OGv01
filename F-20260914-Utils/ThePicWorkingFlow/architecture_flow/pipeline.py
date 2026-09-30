import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import uuid

import httpx
from .config import Settings
from .drawio import render, lint, profile, parse
from .ingest import collect, chunks, convert_document
from .llm import LLM, ANALYZE, DESIGN
from .models import Architecture, JobRequest

STAGES = ("ingest", "analyze", "design", "render", "check", "repair", "publish", "reject")


class Pipeline:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self.llm = LLM(settings, self.client)
        self.root = settings.data_root / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks = {}
        self.capacity = asyncio.Semaphore(4)
        # One API process owns this local store. In-progress remote calls cannot be
        # claimed successful across a process restart.
        for path in self.root.glob("*/job.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("running_stage"):
                record.update(state="FAILED", running_stage=None, error="PROCESS_RESTARTED")
                self.save(record)

    def directory(self, ident):
        if len(ident)!=32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("invalid job ID")
        return self.root / ident

    def read(self, ident):
        return json.loads((self.directory(ident)/"job.json").read_text(encoding="utf-8"))

    def save(self, record):
        record["updated_at"]=datetime.now(timezone.utc).isoformat()
        self.write(record["job_id"],"job.json",record)

    def write(self, ident, name, value):
        file=self.directory(ident)/name
        temp=file.with_suffix(file.suffix+".tmp")
        if isinstance(value,bytes):temp.write_bytes(value)
        else:temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        temp.replace(file)

    def load(self, ident,name):
        return json.loads((self.directory(ident)/name).read_text(encoding="utf-8"))

    def create(self, request: JobRequest):
        template=self.settings.template.read_bytes()
        available={p.get("id") for p in parse(template).findall("diagram")}
        if set(request.preserve_page_ids)-available:raise ValueError("preserve_page_ids missing from template")
        style=profile(template)
        ident=uuid.uuid4().hex
        self.directory(ident).mkdir()
        record={"job_id":ident,"state":"CREATED","revision":0,"repairs":0,"completed":{},"running_stage":None,
                "cancelled":False,"quality_passed":False,"can_repair":True,"request":request.model_dump(),"events":[]}
        self.write(ident,"template.drawio",template)
        self.write(ident,"style-profile.json",style)
        self.save(record)
        return self.public(record)

    def public(self, r):
        return {k:r.get(k) for k in ("job_id","state","revision","repairs","running_stage","quality_passed","can_repair","error","updated_at","events","artifacts")}

    def cancelled(self, ident):
        if self.read(ident).get("cancelled"):raise ValueError("JOB_CANCELLED")

    async def cancel(self, ident):
        # No lock needed here: stage completion re-reads this flag before saving.
        r=self.read(ident)
        if r["state"] in {"SUCCEEDED","REJECTED"}:return self.public(r)
        r.update(cancelled=True,state="CANCELLED")
        self.save(r)
        return self.public(r)

    def evidence_check(self, architecture, context):
        known={s["path"]:len(s["content"].splitlines()) for s in context["sources"]}
        known.update({"doc:"+d["path"]:len(d["markdown"].splitlines()) for d in context["documents"]})
        for n in architecture.components:
            for ref in n.evidence:
                if ref.path not in known or ref.line>known[ref.path]:
                    raise ValueError(f"invalid evidence reference {ref.path}:{ref.line}")

    async def design(self, ident, record, repair=False):
        context=self.load(ident,"context.json")
        data={"analysis":self.load(ident,"analysis.json"),"style":self.load(ident,"style-profile.json"),
              "required_module_names":record["request"]["module_names"],"instructions":record["request"]["instructions"],
              "schema":Architecture.model_json_schema(),"allowed_evidence":{s["path"]:len(s["content"].splitlines()) for s in context["sources"]}}
        data["allowed_evidence"].update({"doc:"+d["path"]:len(d["markdown"].splitlines()) for d in context["documents"]})
        if context.get("design_contract"):
            data["source_derived_outline"] = context["design_contract"]
        if repair:
            data["previous_design"]=self.load(ident,"architecture.json")
            data["validation_errors"]=self.load(ident,"quality.json")
        for attempt in range(3):
            self.cancelled(ident)
            candidate,usage=await self.llm.call("design",DESIGN,data)
            record["events"].append({"stage":"design-repair" if repair else "design","attempt":attempt+1,**usage})
            self.write(ident,f"design-response-r{record['revision']}-a{attempt}.json",candidate)
            try:
                architecture=Architecture.model_validate(candidate)
                required=record["request"]["module_names"]
                if required and [m.label for m in architecture.modules]!=required:
                    raise ValueError("一级模块名称/顺序必须与 required_module_names 完全一致")
                self.evidence_check(architecture,context)
                contract = context.get("design_contract")
                if contract:
                    expected = {n["id"] for n in contract["components"]}
                    if {n.id for n in architecture.components} != expected:
                        raise ValueError("retain exactly the source_derived_outline component IDs")
                    flows = {(e["source"], e["target"], e["kind"]) for e in contract["connections"]}
                    if {(e.source, e.target, e.kind) for e in architecture.connections} != flows:
                        raise ValueError("retain exactly the source_derived_outline data/control flows")
                self.write(ident,"architecture.json",architecture.model_dump())
                return
            except ValueError as exc:
                if attempt==2:raise ValueError("design failed schema/evidence/module constraints after 3 attempts") from exc
                data.update(previous_design=candidate,validation_errors=str(exc)[:8000])

    async def stage(self, ident, stage):
        if stage not in STAGES:raise ValueError("unknown stage")
        lock=self.locks.setdefault(ident,asyncio.Lock())
        async with lock, self.capacity:
            r=self.read(ident)
            rev=r["revision"]
            key=f"{stage}:{rev if stage in {'render','check','publish'} else 0}"
            if stage!="repair" and key in r["completed"]:return self.public(r)
            if r["state"] in {"SUCCEEDED","REJECTED","CANCELLED"}:raise ValueError("job is terminal")
            self.cancelled(ident)
            prerequisites={"ingest":None,"analyze":"context.json","design":"analysis.json","render":"architecture.json",
                           "check":"candidate.drawio","repair":"quality.json","publish":"quality.json","reject":"quality.json"}
            required=prerequisites[stage]
            if required and not (self.directory(ident)/required).exists():raise ValueError("stage prerequisite missing: "+required)
            r.update(state="RUNNING",running_stage=stage,error=None)
            self.save(r)
            try:
                if stage=="ingest":
                    context=collect(self.settings.input_root,r["request"]["repository"],self.settings.max_source_chars)
                    context["documents"]=[]
                    for document in r["request"]["documents"]:
                        self.cancelled(ident)
                        converted=await convert_document(self.client,self.settings,document)
                        context["documents"].append(converted)
                    if sum(len(d["markdown"]) for d in context["documents"])+context["characters"]>self.settings.max_source_chars:
                        raise ValueError("combined code/document budget exceeded")
                    self.write(ident,"context.json",context)
                elif stage=="analyze":
                    context=self.load(ident,"context.json")
                    all_sources=context["sources"]+[{"path":"doc:"+d["path"],"content":d["markdown"]} for d in context["documents"]]
                    reports=[]
                    # Integrators may provide a source-derived AST index while retaining
                    # full source snapshots for evidence validation and audit.
                    analysis_chunks = [context["analysis_index"]] if context.get("analysis_index") else chunks(all_sources,self.settings.chunk_chars)
                    for i,chunk in enumerate(analysis_chunks):
                        self.cancelled(ident)
                        report,usage=await self.llm.call("analysis",ANALYZE,{"chunk":i+1,"source_data":chunk})
                        if not isinstance(report.get("summary"),str) or not isinstance(report.get("responsibilities"),list):
                            raise ValueError("invalid architecture analysis response")
                        reports.append(report);r["events"].append({"stage":"analyze","chunk":i+1,**usage})
                    analysis={"reports":reports,"omissions":context["omissions"],"evidence_format":"source file line; doc: prefix refers to Docling Markdown lines"}
                    if len(json.dumps(analysis))>160_000:raise ValueError("analysis synthesis input too large; narrow repository")
                    self.write(ident,"analysis.json",analysis)
                elif stage=="design":await self.design(ident,r)
                elif stage=="render":
                    arch=Architecture.model_validate(self.load(ident,"architecture.json"))
                    xml,style=render(arch,(self.directory(ident)/"template.drawio").read_bytes(),r["request"]["preserve_page_ids"])
                    self.write(ident,"candidate.drawio",xml);self.write(ident,"style-profile.json",style)
                elif stage=="check":
                    if f"render:{rev}" not in r["completed"]:raise ValueError("current revision has not been rendered")
                    review=self.review(ident,r)
                    self.write(ident,"quality.json",review.model_dump())
                    r.update(quality_passed=review.passed,can_repair=r["repairs"]<self.settings.max_repairs)
                elif stage=="repair":
                    if f"check:{rev}" not in r["completed"] or r["quality_passed"] or r["repairs"]>=self.settings.max_repairs:
                        raise ValueError("repair requires failed current review and remaining repair budget")
                    await self.design(ident,r,repair=True)
                    r.update(revision=rev+1,repairs=r["repairs"]+1,quality_passed=False)
                elif stage=="publish":
                    if f"check:{rev}" not in r["completed"]:raise ValueError("current revision has not passed review")
                    # Recheck actual bytes, not a stale pass flag, before publication.
                    review=self.review(ident,r)
                    if not review.passed:raise ValueError("quality gate rejected publication")
                    self.cancelled(ident)
                    content=(self.directory(ident)/"candidate.drawio").read_bytes()
                    self.write(ident,"architecture.drawio",content)
                    r.update(state="SUCCEEDED",quality_passed=True,artifacts={"drawio":f"/v1/jobs/{ident}/artifacts/architecture.drawio",
                              "architecture":f"/v1/jobs/{ident}/artifacts/architecture.json","quality":f"/v1/jobs/{ident}/artifacts/quality.json",
                              "analysis":f"/v1/jobs/{ident}/artifacts/analysis.json","style":f"/v1/jobs/{ident}/artifacts/style-profile.json",
                              "sha256":sha256(content).hexdigest()})
                elif stage=="reject":
                    if r["quality_passed"]:raise ValueError("cannot reject a passing review")
                    r.update(state="REJECTED",error="QUALITY_GATE_FAILED")
                self.cancelled(ident)
                r["completed"][key]=True
                r["running_stage"]=None
                if r["state"]=="RUNNING":r["state"]="WAITING"
                self.save(r)
                return self.public(r)
            except Exception as exc:
                was_cancelled=self.read(ident).get("cancelled",False)
                r.update(state="CANCELLED" if was_cancelled else "FAILED",cancelled=was_cancelled,running_stage=None)
                # Do not put authorization headers or provider response bodies into public status.
                r["error"]=f"{type(exc).__name__}: {str(exc)[:500]}" if not isinstance(exc,httpx.HTTPStatusError) else f"Upstream HTTP {exc.response.status_code} during {stage}"
                self.save(r)
                raise

    def review(self,ident,r):
        return lint((self.directory(ident)/"candidate.drawio").read_bytes(),Architecture.model_validate(self.load(ident,"architecture.json")),
                    self.load(ident,"style-profile.json"),r["request"]["preserve_page_ids"],(self.directory(ident)/"template.drawio").read_bytes())

    async def run(self, ident):
        try:
            for stage in ("ingest","analyze","design","render","check"):await self.stage(ident,stage)
            while not self.read(ident)["quality_passed"] and self.read(ident)["can_repair"]:
                for stage in ("repair","render","check"):await self.stage(ident,stage)
            await self.stage(ident,"publish" if self.read(ident)["quality_passed"] else "reject")
        except Exception:
            # stage() already recorded a safe public failure.
            return
