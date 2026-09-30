import time
from dataclasses import asdict, dataclass
from pathlib import Path

from clc.config import Settings


@dataclass
class Chunk:
    id: str
    source_id: str
    start: int
    end: int
    text: str

    def to_dict(self):
        return asdict(self)


def parse_document(path: Path, settings: Settings) -> str:
    if path.suffix.lower() in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8-sig")
    else:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise RuntimeError('PDF/Word 解析需要安装 pip install -e ".[docling]"') from exc
        from docling.datamodel.base_models import ConversionStatus, InputFormat
        from huggingface_hub.errors import LocalEntryNotFoundError

        for attempt in range(3):
            try:
                converter = DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.DOCX])
                result = converter.convert(
                    path, raises_on_error=True, max_num_pages=settings.max_pages,
                    max_file_size=settings.max_upload_mb * 1024 * 1024,
                )
                break
            except LocalEntryNotFoundError as exc:
                # Includes IncompleteSnapshotError. Keep partial downloads so the Hub can resume.
                if attempt == 2:
                    raise RuntimeError(
                        "PDF/Word 解析模型尚未下载完整，重试后仍无法获取。"
                        "请检查 Worker 到 huggingface.co 的网络或代理，补齐模型后重新提交；"
                        "Docker 模型缓存保存在 docling_models 卷中。"
                    ) from exc
                time.sleep(2 ** attempt)
        if result.status != ConversionStatus.SUCCESS:
            raise ValueError("Docling 未完整解析文档，请检查文件或页数上限")
        text = result.document.export_to_markdown()
    if not text.strip():
        raise ValueError("文档没有可提取的文字")
    if len(text) > settings.max_source_chars:
        raise ValueError("解析后的文字超限，请拆分文档后提交")
    return text


def chunk_text(text: str, source_id: str, settings: Settings) -> list[Chunk]:
    chunks, start = [], 0
    while start < len(text):
        end = min(start + settings.chunk_chars, len(text))
        if end < len(text):
            # A paragraph boundary must leave room for overlap and forward progress.
            boundary = text.rfind("\n\n", start + max(settings.chunk_chars // 2, settings.chunk_overlap), end)
            if boundary >= 0:
                end = boundary + 2
        chunks.append(Chunk(f"{source_id}-c{len(chunks)+1:04d}", source_id, start, end, text[start:end]))
        if end == len(text):
            break
        start = max(start + 1, end - settings.chunk_overlap)
    return chunks


def validate_evidence(citations, chunks: dict[str, Chunk]):
    for citation in citations:
        chunk = chunks.get(citation.chunk_id)
        if chunk is None or "".join(citation.quote.split()) not in "".join(chunk.text.split()):
            raise ValueError(f"原文引用无法核对: {citation.chunk_id}")
