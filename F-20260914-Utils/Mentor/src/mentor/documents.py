import io
from importlib.metadata import version
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from mentor.domain import Block, DocumentIR, digest
from mentor.storage import ArtifactStore


def inspect_file(content: bytes, filename: str, max_pages=20, max_bytes=20971520) -> tuple[str, int]:
    if not content or len(content) > max_bytes:
        raise ValueError("FILE_SIZE_LIMIT")
    suffix = Path(filename).suffix.lower()
    if content.startswith(b"%PDF-") and suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("ENCRYPTED_PDF")
        pages = len(reader.pages)
        mime = "application/pdf"
    else:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("UNSUPPORTED_FORMAT")
            if (image.format == "PNG" and suffix != ".png") or (
                image.format == "JPEG" and suffix not in {".jpg", ".jpeg"}
            ):
                raise ValueError("FILE_TYPE_MISMATCH")
            if image.width * image.height > 40_000_000:
                raise ValueError("IMAGE_PIXEL_LIMIT")
            mime = "image/png" if image.format == "PNG" else "image/jpeg"
            image.verify()
        pages = 1
    if not 1 <= pages <= max_pages:
        raise ValueError("PAGE_LIMIT")
    return mime, pages


def parse_document(document, store: ArtifactStore, demo=False) -> DocumentIR:
    content = store.path(document.storage_key).read_bytes()
    if demo:
        if document.mime != "application/pdf":
            raise ValueError("DEMO_PARSER_REQUIRES_DIGITAL_PDF")
        blocks = [Block(id=f"p{i + 1}", page=i + 1, text=page.extract_text() or "")
                  for i, page in enumerate(PdfReader(io.BytesIO(content)).pages)]
        return DocumentIR(document_id=document.id, parse_id=digest([document.sha256, "demo"]),
                          parser="pypdf-demo", source_sha256=document.sha256, pages=document.pages,
                          blocks=blocks, warnings=["DEMO_PARSER_NOT_RELEASE_QUALIFIED"], complete=True)
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = True
    options.do_table_structure = True
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(DocumentStream(name=document.filename, stream=io.BytesIO(content)),
                               max_num_pages=20, max_file_size=20971520)
    raw = result.document.export_to_dict()
    from mentor.domain import canonical
    raw_key, _ = store.put(canonical(raw).encode(), "parses/raw")
    blocks = []
    for item, _level in result.document.iterate_items():
        provenance = getattr(item, "prov", [])
        if not provenance:
            continue
        prov = provenance[0]
        text = getattr(item, "text", "")
        kind = "text"
        if hasattr(item, "export_to_dataframe"):
            text = item.export_to_dataframe(doc=result.document).to_csv(index=False)
            kind = "table"
        if not text.strip():
            continue
        bbox = None
        page = result.document.pages.get(prov.page_no)
        if page is not None and page.size and getattr(prov, "bbox", None):
            box = prov.bbox.to_top_left_origin(page.size.height)
            bbox = [box.l / page.size.width, box.t / page.size.height,
                    box.r / page.size.width, box.b / page.size.height]
        blocks.append(Block(id=item.self_ref, page=prov.page_no, text=text, kind=kind, bbox=bbox))
    complete = str(result.status.value) == "success" and bool(blocks)
    return DocumentIR(document_id=document.id, parse_id=digest([document.sha256, raw_key, version("docling")]),
                      parser=f"docling-{version('docling')}", source_sha256=document.sha256,
                      pages=document.pages, blocks=blocks, complete=complete,
                      warnings=[] if complete else ["PARTIAL_OR_EMPTY_PARSE"])


def select_chunks(ir: DocumentIR, limit: int) -> list[dict]:
    chunks, current, length = [], [], 0
    for block in ir.blocks:
        # Keep source IDs and complete line boundaries; never silently truncate a table.
        lines = block.text.splitlines(keepends=True)
        segment = ""
        for line in lines:
            if len(line) > limit:
                raise ValueError("BLOCK_EXCEEDS_CONTEXT_BUDGET")
            if len(segment) + len(line) > limit:
                if current:
                    chunks.append({"blocks": current})
                    current, length = [], 0
                chunks.append({"blocks": [block.model_copy(update={"text": segment}).model_dump()]})
                segment = ""
            segment += line
        if length + len(segment) > limit and current:
            chunks.append({"blocks": current})
            current, length = [], 0
        current.append(block.model_copy(update={"text": segment}).model_dump())
        length += len(segment)
    if current:
        chunks.append({"blocks": current})
    return chunks

