import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path

import httpx

from .grounding import split_document, validate_concepts, validate_relations
from .llm import LLM, PipelineError, OutputLimitError
from .models import Extraction, Relations, RELATION_LABELS, KeyConceptSelection, KeyRelationSelection
from .selection import choose_key_concepts, sparse_relations, choose_key_relations
from .store import now

STAGES = ["parse", "extract", "relate", "render"]
STAGE_NAMES = {"parse": "解析文档", "extract": "提取知识点", "relate": "分析全局关系", "render": "生成关系图"}


class StageConflict(Exception):
    pass


class Pipeline:
    def __init__(self, settings, store, client):
        self.settings, self.store, self.client = settings, store, client
        self.llm = LLM(settings, client)
        self.locks = {}
        self.capacity = asyncio.Semaphore(2)

    def progress(self, job_id, message, progress):
        job = self.store.get(job_id)
        job.update(message=message, progress=progress)
        self.store.save(job)

    async def stage(self, job_id, stage):
        if stage not in STAGES:
            raise StageConflict("未知处理阶段")
        lock = self.locks.setdefault(job_id, asyncio.Lock())
        if lock.locked():
            raise StageConflict("任务正在处理，请等待当前阶段完成")
        async with lock:
            job = self.store.get(job_id)
            if stage in job["completed_stages"]:
                return job
            if job["status"] == "failed":
                raise StageConflict("任务已失败，请重新上传文档")
            if job["completed_stages"] != STAGES[:STAGES.index(stage)]:
                raise StageConflict("处理阶段顺序错误")
            job.update(status=stage, message=STAGE_NAMES[stage], error=None)
            self.store.save(job)
            try:
                async with self.capacity:
                    await getattr(self, stage)(job_id)
                job = self.store.get(job_id)
                job["completed_stages"].append(stage)
                job["status"] = "completed" if stage == "render" else "waiting"
                job["message"] = "关系图已生成" if stage == "render" else STAGE_NAMES[stage] + "完成"
                self.store.save(job)
                return job
            except Exception as exc:
                message = str(exc) if isinstance(exc, PipelineError) else "处理失败，请检查文档格式及服务配置后重新上传。"
                logging.getLogger(__name__).warning("Job %s stage %s failed (%s)", job_id, stage, type(exc).__name__)
                job = self.store.get(job_id)
                job.update(status="failed", error=message, message="处理失败")
                self.store.save(job)
                raise PipelineError(message) from exc

    async def run(self, job_id):
        try:
            for stage in STAGES:
                await self.stage(job_id, stage)
        except (PipelineError, StageConflict):
            pass

    async def parse(self, job_id):
        job = self.store.get(job_id)
        data = self.store.path(job_id, "source").read_bytes()
        suffix = Path(job["filename"]).suffix.lower()
        if suffix in (".md", ".txt"):
            try:
                markdown = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise PipelineError("Markdown / TXT 文件须采用 UTF-8 编码。") from exc
            converter = "utf-8-text"
        else:
            try:
                response = await self.client.post(self.settings.docling_url + "/v1/convert/source",
                    headers={"X-Api-Key": self.settings.docling_key},
                    json={"sources": [{"kind": "file", "base64_string": base64.b64encode(data).decode(), "filename": job["filename"]}],
                          "options": {"to_formats": ["md"], "image_export_mode": "placeholder", "do_ocr": True}}, timeout=600)
                response.raise_for_status()
                result = response.json()
                if result.get("status") != "success":
                    raise PipelineError("Docling 未完整解析文档，任务已停止；请检查文档或 OCR 配置。")
                markdown = result.get("document", {}).get("md_content")
            except (httpx.HTTPError, ValueError) as exc:
                raise PipelineError("Docling 连接或解析失败，请检查 DOCLING_BASE_URL、密钥及服务状态。") from exc
            converter = "docling-serve"
        if not isinstance(markdown, str) or not markdown.strip():
            raise PipelineError("文档没有可提取的文字。")
        markdown = markdown.replace("\r\n", "\n").replace("\r", "\n")
        if len(markdown) > self.settings.max_document_chars:
            raise PipelineError(f"解析文本为 {len(markdown)} 字符，超过 MAX_DOCUMENT_CHARS={self.settings.max_document_chars}。为保证全局上下文完整，未进行截断。")
        sections, chunks = split_document(markdown, self.settings.chunk_chars)
        self.store.write(job_id, "document.md", markdown)
        self.store.write(job_id, "parsed.json", dict(sections=sections, chunks=chunks, converter=converter,
                         sha256=hashlib.sha256(data).hexdigest(), text_sha256=hashlib.sha256(markdown.encode()).hexdigest()))
        self.progress(job_id, f"已解析 {len(sections)} 个知识部分", 15)

    async def extract(self, job_id):
        parsed = self.store.read(job_id, "parsed.json")
        document = self.store.path(job_id, "document.md").read_text(encoding="utf-8")
        concepts, rejected = [], []
        for index, chunk in enumerate(parsed["chunks"]):
            result = await self.llm.ask(
                "仅提取当前文档块中对理解文档主题、核心方法、关键机制或主要结论重要的概念。"
                "不要把每个名词都当知识点；排除附件命名、文件格式、提交要求、编号、一次性数值、普通描述词及无独立意义的公式符号。"
                "如文档主题本身就是这些事项才可考虑。每块通常 0–6 个，不凑数量。名称须逐字出现在引用中，优先简洁且有独立意义的术语。"
                "不要把整句任务描述、结果呈现要求、数据规格或每个公式参数拆成独立概念。优先核心研究对象、方法、机制、评价指标。"
                "名称用原文中的一个连续术语；不要自行拼接全称和括号缩写，不要删除术语内部的引号。"
                "definition 仅概括原文；quotes 必须逐字引用支持该概念的原文完整短句。只包含行政说明的块返回 concepts: []。",
                {"section": chunk["title"], "text": chunk["text"]}, Extraction)
            validate_concepts(result.concepts, document, parsed["sections"], chunk, concepts, rejected)
            if len(concepts) > self.settings.max_concepts:
                raise PipelineError("知识点数量超过 MAX_CONCEPTS；请调整文档范围或提高预算。未静默丢弃知识点。")
            self.progress(job_id, f"重要概念候选 {index+1}/{len(parsed['chunks'])} 块 · {len(concepts)} 个", 15 + round(25*(index+1)/len(parsed["chunks"])))
        self.store.write(job_id, "candidates.json", dict(concepts=concepts, rejected=rejected))
        if concepts:
            self.progress(job_id, "结合全文筛选重要概念、合并同名同义候选", 43)
            selection = await self.llm.ask(
                "根据完整文档，筛选真正重要的概念。只从 candidates 选择现有 ID，不创造新概念。"
                "重要=理解文档主题、核心方法、关键机制或主要结论不可缺少；排除行政要求、附件、命名、文件规格、"
                "泛泛的‘数学模型/结果/数据’、一次性符号与局部细节，除非它们就是文档主题。"
                "先判断全文主要研究对象，再优先保留主要章节的研究对象、核心方法、关键机制、评价指标或实质结论，避免被附录细节挤占。"
                "仅用于输入描述的单孔/多孔数据规格、分辨率、孔深、文件名、表格列名、图形输出形式不要入选。"
                "振幅/相位等单个公式参数仅在被独立研究或产生主要结论时保留；只用于代入公式不算重要概念。"
                "最多 max_key_concepts 个，这是上限而非目标；宁可只保留少数核心概念，不用次要项凑满。"
                "按重要性排序。每项 importance_reason 指出它在本文主要研究中承担的作用，‘必要输入/输出要求’不能作为入选理由，不引用外部常识。"
                "同一名称且原文含义一致的重复候选，保留一个代表 ID，将其余 ID 放入 duplicate_ids；"
                "同名异义不能合并，不同名称不能放进 duplicate_ids；各 ID 只能出现一次。",
                {"full_document": document, "candidates": concepts, "max_key_concepts": self.settings.max_key_concepts}, KeyConceptSelection)
            selected, omitted = choose_key_concepts(concepts, selection, self.settings.max_key_concepts)
            rejected.extend(omitted)
        else:
            selected = []
        self.store.write(job_id, "concepts.json", dict(concepts=selected, rejected=rejected, candidate_count=len(concepts),
                                                     selection_policy="document_key_concepts", max_key_concepts=self.settings.max_key_concepts))
        self.progress(job_id, f"全文筛选完成 · 保留 {len(selected)} 个重要概念", 50)

    async def relate(self, job_id):
        parsed = self.store.read(job_id, "parsed.json")
        extracted = self.store.read(job_id, "concepts.json")
        document = self.store.path(job_id, "document.md").read_text(encoding="utf-8")
        concepts, rejected, relations = extracted["concepts"], extracted["rejected"], []
        batch_size = self.settings.relation_batch_size
        batches = [concepts[i:i+batch_size] for i in range(0, len(concepts), batch_size)] if len(concepts) > 1 else []
        index = [dict(id=c["id"], label=c["label"], definition=c["definition"], section_ids=c["section_ids"],
                      quotes=[evidence["quote"] for evidence in c["evidence"]]) for c in concepts]
        calls, processed = 0, 0
        while batches:
            batch = batches.pop(0)
            source_ids = [c["id"] for c in batch]
            calls += 1
            try:
                result = await self.llm.ask(
                "结合完整全文分析本批 source_ids 到全部概念的关系。可跨章节连线；target 可为任意其他概念。"
                "每个源概念都必须考虑整个 concepts 索引；没有文档证据就返回空关系，保留孤立点。"
                "只保留理解文档核心内容所必需的直接联系，每个源概念最多 max_relations_per_source 条，不凑数量。"
                "遵循 relationship_policy，不能把传递路径、共享背景或普通共现当直接联系；优先具体类型，避免泛泛 related。"
                "禁止创建新概念。关系必须由原文支持，quotes 合起来必须包含两端概念的原名称，并支持所述关系。"
                "可直接复制 concepts 中的原文 quotes 辅助定位，但必须另有原文直接支持两者的关系，不能凭两个定义硬连。"
                "方向严格为 source → target：part_of=源属于目标；prerequisite=源是目标前提；explains=源解释目标；"
                "causes=源导致目标；applies_to=源应用于目标；depends_on=源依赖目标；contrasts/related 为对称关系。"
                "不能仅因同时出现就关联，不能利用外部常识推导关系。confidence 是模型自评。",
                    {"full_document": document, "sections": parsed["sections"], "concepts": index, "source_ids": source_ids,
                     "relationship_policy": "仅返回直接支持文档核心内容的关键联系；不把共同出现、共享背景或间接传递当作关系。优先具体关系类型，避免泛泛 related。",
                     "max_relations_per_source": self.settings.max_relations_per_concept}, Relations)
            except OutputLimitError:
                if len(batch) == 1:
                    raise
                middle = len(batch) // 2
                batches[0:0] = [batch[:middle], batch[middle:]]
                self.progress(job_id, "模型输出超限，已自动缩小关系批次，保留完整文档上下文", 50 + round(40*processed/len(concepts)))
                continue
            validate_relations(result.relations, document, parsed["sections"], concepts, source_ids, relations, rejected)
            processed += len(batch)
            self.progress(job_id, f"全局关系分析 {processed}/{len(concepts)} 个源概念 · 已保留 {len(relations)} 条", 50 + round(40*processed/len(concepts)))
        relations, omitted = sparse_relations(relations, self.settings.max_relations_per_concept)
        rejected.extend(omitted)
        self.store.write(job_id, "relation-candidates.json", dict(relations=relations, rejected=rejected))
        if relations:
            self.progress(job_id, "结合全文复核重要关系、语义方向与冗余连线", 93)
            calls += 1
            selection = await self.llm.ask(
                "对 relation_candidates 做严格的全文复核，只返回值得保留的现有关系 ID。不得新增、改名或修改关系。"
                "只保留原文明示、对核心主题重要且直接的联系；原文出现两个名词不足以支持关系。"
                "核对 source→target 的真实含义：part_of 是组成或分类归属；depends_on 是依赖；prerequisite 是前提；"
                "applies_to 是方法/技术应用于对象；causes 是导致目标本身。"
                "例如‘干扰使识别更困难’不能写成‘干扰导致识别’；‘图像由方法处理’不能反写成‘图像应用于方法’；"
                "‘指标用于评价对象’不能写成‘指标属于对象’，除非原文确实表达组成。contrasts 需要原文明示的对比，不能凭不同类别。"
                "避免同一对概念保留互为反向的错误边；已有具体关系时不要重复保留 related。"
                "删除只有间接推理、共同背景、任务要求泛化或不确定方向的边；宁可保留孤立概念，不硬连。"
                "relation_ids 可以为空。",
                {"full_document": document, "concepts": index, "relation_candidates": relations}, KeyRelationSelection)
            relations, omitted = choose_key_relations(relations, selection)
            rejected.extend(omitted)
        self.store.write(job_id, "relations.json", dict(relations=relations, rejected=rejected, calls=calls))

    async def render(self, job_id):
        job = self.store.get(job_id)
        parsed, extracted, related = [self.store.read(job_id, filename) for filename in ("parsed.json", "concepts.json", "relations.json")]
        linked = {cid for relation in related["relations"] for cid in (relation["source"], relation["target"])}
        graph = dict(schema_version="1.1", job_id=job_id, title=job["filename"], created_at=now(), demo=False,
                     extraction_policy=dict(mode=extracted.get("selection_policy", "legacy"),
                                            candidate_count=extracted.get("candidate_count", len(extracted["concepts"])),
                                            max_key_concepts=self.settings.max_key_concepts,
                                            relation_review="full_document_key_relations",
                                            max_relations_per_concept=self.settings.max_relations_per_concept),
                     document=dict(filename=job["filename"], sha256=parsed["sha256"], text_sha256=parsed["text_sha256"], converter=parsed["converter"],
                                   offset_unit="Unicode code points in document.md", line_numbers="converted Markdown, not PDF page numbers"),
                     sections=parsed["sections"], concepts=extracted["concepts"], relations=related["relations"], relation_labels=RELATION_LABELS,
                     validation=dict(rejected=related["rejected"], isolated_concepts=[c["id"] for c in extracted["concepts"] if c["id"] not in linked],
                                     global_context="complete_document_every_relation_call", global_context_calls=related["calls"],
                                     note="原文引用及端点经过程序验证；关系含义、方向、摘要准确性及提取完整性仍需人工复核。"))
        self.store.write(job_id, "graph.json", graph)
        job.update(progress=100, counts=dict(sections=len(graph["sections"]), concepts=len(graph["concepts"]), relations=len(graph["relations"]), rejected=len(related["rejected"])))
        self.store.save(job)
