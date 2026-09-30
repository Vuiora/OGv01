"""TPWF analysis/design/render/check/publication for the implemented business system."""
import asyncio
import ast
import hashlib
import json
from pathlib import Path
import shutil
import httpx
from architecture_flow.config import Settings
from architecture_flow.ingest import collect
from architecture_flow.models import Architecture, JobRequest
from architecture_flow.pipeline import Pipeline
from architecture_flow.drawio import render, lint
from .bootstrap import ROOT
from .store import write_json

VENDOR_EVIDENCE = [
    "MTBMT/src/mtbmt/meta_features.py", "MTBMT/src/mtbmt/experience_store.py",
    "MTBMT/src/mtbmt/meta_learner.py",
    "F-20260919-StatisticalDiscoveryLearning/sdl_m01/__init__.py",
    "F-20260914-Utils/ParallelLogicDeterminationAlgorithModule/plda/__init__.py",
    "F-20260914-Utils/ConceptLayerConstructor/clc/build.py",
    "F-20260914-Utils/ConceptRelationDraw/concept_relation/pipeline.py",
    "F-20260914-Utils/Mentor/src/mentor/domain.py",
    "F-20260914-Utils/Mentor/src/mentor/compiler.py",
    "F-260908-GPTSelf-renew/renew/prompts.py",
    "F-20260914-Utils/ThePicWorkingFlow/architecture_flow/pipeline.py",
]

def source_context():
    context = collect(ROOT, "ogflow", 240000)
    for relative in VENDOR_EVIDENCE:
        content = (ROOT / relative).read_text(encoding="utf-8-sig")
        context["sources"].append({"path": relative, "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(), "symbols": []})
        context["characters"] += len(content)
    context["documents"] = []
    index = []
    for source in context["sources"]:
        tree = ast.parse(source["content"])
        imports = 0
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if imports < 4:
                    index.append(f"{source['path']}:{node.lineno}: {ast.unparse(node)}")
                imports += 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                members = node.body if isinstance(node, ast.ClassDef) else [node]
                index.append(f"{source['path']}:{node.lineno}: {type(node).__name__} {node.name}: {(ast.get_docstring(node) or '')[:180]}")
                if not source["path"].startswith("ogflow/"):
                    continue
                for member in members:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        calls = sorted({ast.unparse(c.func) for c in ast.walk(member) if isinstance(c, ast.Call)})
                        index.append(f"{source['path']}:{member.lineno}: {member.name} -> {', '.join(calls)[:180]}")
    context["analysis_index"] = "Source-derived AST import/call index; inspect unknown dynamic semantics cautiously.\n" + "\n".join(index)
    if context["characters"] > 240000:
        raise ValueError("Business architecture source budget exceeded")
    return context

def reference(relative, symbol):
    content = (ROOT / relative).read_text(encoding="utf-8-sig")
    tree = ast.parse(content)
    matches = [node.lineno for node in ast.walk(tree)
               if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol]
    if not matches:
        raise ValueError(f"Architecture source symbol missing: {symbol}")
    return {"path": relative, "line": min(matches)}

def deterministic_architecture():
    # A source-grounded offline view is useful in CI; production also runs TPWF's live LLM stages.
    modules = [("input", "观测与知识输入"), ("strategy", "MTBMT 探索策略"),
               ("discovery", "SDL 发现与确证"), ("compute", "PLDA 确定性计算"),
               ("archive", "归档与经验反馈"), ("engineering", "应用与架构生产")]
    definitions = [
        ("csv", "带来源的观测数据", "input", "ogflow/observations.py", "load_observations"),
        ("knowledge", "Docling → CLC / CRD", "input", "ogflow/documents.py", "collect_knowledge"),
        ("recommender", "元学习选择 / 分组冷启动", "strategy", "ogflow/recommendation.py", "recommend"),
        ("proposal", "统一 LLM 提出表达式", "strategy", "ogflow/sdl_adapter.py", "propose"),
        ("evidence", "M1 证据与权限分区", "input", "ogflow/pipeline.py", "BusinessPipeline"),
        ("exploration", "E / V 探索与验证", "input", "ogflow/sdl_adapter.py", "run_discovery"),
        ("sealed_c", "封存 C · 冻结后一次消费", "input", "ogflow/pipeline.py", "run"),
        ("hypotheses", "M2–M5 候选与 V 门禁", "discovery", "ogflow/sdl_adapter.py", "run_discovery"),
        ("freeze", "冻结方法 / 参数 / 阈值", "discovery", "ogflow/statistics.py", "make_evaluator"),
        ("sign_test", "独立组符号检验", "discovery", "ogflow/statistics.py", "make_evaluator"),
        ("plda", "任务 DAG / CPU 损失计算", "compute", "ogflow/statistics.py", "loss_difference"),
        ("archive_store", "M8 发现档案与审计", "archive", "ogflow/pipeline.py", "run"),
        ("experience", "E 经验库 → 下一任务推荐", "archive", "MTBMT/src/mtbmt/experience_store.py", "ExperienceStore"),
        ("mentor", "Mentor AppSpec / n8n 入口", "engineering", "ogflow/mentor.py", "build_application"),
        ("renew", "Self Renew 研发建议", "engineering", "ogflow/feedback.py", "build_feedback"),
        ("tpwf", "TPWF 分析 / 设计 / 校验", "engineering", "ogflow/architecture.py", "produce_architecture"),
        ("ujn_llm", "UJN 统一 LLM 服务", "engineering", "ogflow/llm.py", "JSONClient"),
    ]
    components = [{"id": ident, "label": label, "module": module, "status": "observed",
                   "evidence": [reference(path, symbol)]} for ident, label, module, path, symbol in definitions]
    next(n for n in components if n["id"] == "ujn_llm")["status"] = "external"
    connections = [("csv", "evidence", "raw"), ("evidence", "exploration", "standard"),
        ("evidence", "sealed_c", "raw"), ("exploration", "recommender", "analysis"),
        ("exploration", "hypotheses", "standard"), ("sealed_c", "sign_test", "standard"),
        ("knowledge", "proposal", "analysis"), ("recommender", "proposal", "analysis"),
        ("proposal", "hypotheses", "analysis"), ("hypotheses", "freeze", "standard"),
        ("freeze", "sign_test", "dispatch"), ("sign_test", "plda", "dispatch"),
        ("plda", "sign_test", "result"), ("sign_test", "archive_store", "result"),
        ("archive_store", "experience", "standard"), ("experience", "recommender", "analysis"),
        ("archive_store", "renew", "analysis"), ("mentor", "evidence", "control"),
        ("archive_store", "tpwf", "control"), ("proposal", "ujn_llm", "dispatch"),
        ("renew", "ujn_llm", "dispatch"), ("tpwf", "ujn_llm", "dispatch")]
    return Architecture.model_validate({"version": 1, "title": "OGv01 整体业务闭环",
        "modules": [{"id": ident, "label": label, "kind": "data" if ident in {"input", "archive"} else "infrastructure"} for ident, label in modules],
        "components": components, "connections": [{"id": f"edge-{index}", "source": a, "target": b, "kind": kind}
            for index, (a, b, kind) in enumerate(connections)],
        "assumptions": ["文档知识只用于探索；C 分区不发送给 LLM。", "Self Renew 输出待审查建议。",
            "组独立性由任务所有者声明。", "PDF/DOCX 输入需要安装 Docling；n8n 编排需要配置 API 凭据。"]})

def produce_architecture(directory: Path, config, *, live=True):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    template = ROOT / "F-20260914-Utils" / "TheStructure.drawio"
    context = source_context()
    outline = deterministic_architecture()
    context["design_contract"] = outline.model_dump()
    settings = Settings(input_root=ROOT, template=template, data_root=directory,
        api_key="", analysis_url=config.base_url, analysis_model=config.model, analysis_key=config.api_key,
        design_url=config.base_url, design_model=config.model, design_key=config.api_key,
        chunk_chars=24000, max_source_chars=240000, llm_timeout=config.timeout, output_tokens=16384)
    async def produce():
        async with httpx.AsyncClient(follow_redirects=False) as client:
            pipeline = Pipeline(settings, client)
            job = pipeline.create(JobRequest(repository="ogflow", module_names=[m.label for m in outline.modules], instructions=(
                "只描述提供源码中已有的 OGv01 整体业务闭环。包括知识输入、MTBMT策略、SDL发现与确证、"
                "PLDA计算、归档和经验反馈、Mentor规格与n8n入口、Self Renew建议、TPWF生产。"
                "排除 Wiki、Anki、PRT。把统一 UJN LLM 作为外部服务。最多6个一级模块、总共最多18个组件、24条连接。"
                "每组件只引用1处最相关证据；标签最多30字；assumptions最多6条。紧凑JSON总长不超过12000字符。"
                "清楚画出探索数据与封存确证数据的权限边界。当前 Mentor 适配器使用AppSpec并生成OGflow工作流；"
                "Self Renew只生成建议，不自动改代码。所有 observed 节点必须引用提供的源码行号。"
                "source_derived_outline 已依据真实代码给出业务结构。必须保留其组件ID、模块归属、连接端点与类型；"
                "根据分析结果改善标签和连接说明，但不删除冻结、封存C或确认计算节点。")))
            ident = job["job_id"]
            pipeline.write(ident, "context.json", context)
            record = pipeline.read(ident)
            record["completed"]["ingest:0"] = True
            pipeline.save(record)
            if live:
                for stage in ("analyze", "design", "render", "check"):
                    await pipeline.stage(ident, stage)
                while not pipeline.read(ident)["quality_passed"] and pipeline.read(ident)["can_repair"]:
                    for stage in ("repair", "render", "check"):
                        await pipeline.stage(ident, stage)
            else:
                architecture = deterministic_architecture()
                pipeline.evidence_check(architecture, context)
                pipeline.write(ident, "architecture.json", architecture.model_dump())
                for stage in ("render", "check"):
                    await pipeline.stage(ident, stage)
            await pipeline.stage(ident, "publish")
            for name in ("architecture.drawio", "architecture.json", "quality.json", "style-profile.json"):
                shutil.copy2(pipeline.directory(ident) / name, directory / name)
            quality = pipeline.load(ident, "quality.json")
            write_json(directory / "source-manifest.json", {"sources": [{"path": s["path"], "sha256": s["sha256"]}
                for s in context["sources"]], "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest()})
            metadata = {"mode": "live_tpwf" if live else "offline_source_grounded", "quality_passed": quality["passed"],
                "page_count": quality["page_count"], "module_count": quality["module_count"],
                "llm": config.public(), "source_count": len(context["sources"]),
                "drawio": str(directory / "architecture.drawio"), "job_id": ident,
                "usage": pipeline.read(ident)["events"],
                "sha256": hashlib.sha256((directory / "architecture.drawio").read_bytes()).hexdigest()}
            write_json(directory / "production.json", metadata)
            return metadata
    return asyncio.run(produce())
