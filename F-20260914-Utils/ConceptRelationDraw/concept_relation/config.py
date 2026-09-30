import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_root: Path = Path("data")
    app_key: str = ""
    docling_url: str = "http://127.0.0.1:5002"
    docling_key: str = ""
    n8n_url: str = "http://127.0.0.1:5679/webhook/concept-relation"
    llm_url: str = ""
    llm_key: str = ""
    llm_model: str = ""
    api_style: str = "responses"
    json_mode: bool = True
    llm_timeout: int = 180
    output_tokens: int = 8192
    reasoning_effort: str = ""
    max_document_chars: int = 60000
    max_prompt_chars: int = 100000
    max_concepts: int = 250
    max_key_concepts: int = 20
    max_relations_per_concept: int = 3
    chunk_chars: int = 5000
    relation_batch_size: int = 12
    max_upload_mb: int = 30

    def model_ready(self):
        from urllib.parse import urlparse
        return bool(self.llm_url and self.llm_model and (self.llm_key or urlparse(self.llm_url).hostname != "api.openai.com"))

    @classmethod
    def from_env(cls):
        load_dotenv()
        numbers = {field: int(os.getenv(env, default)) for field, env, default in [
            ("llm_timeout", "LLM_TIMEOUT", "180"), ("output_tokens", "LLM_OUTPUT_TOKENS", "8192"),
            ("max_document_chars", "MAX_DOCUMENT_CHARS", "60000"), ("max_prompt_chars", "MAX_PROMPT_CHARS", "100000"),
            ("max_concepts", "MAX_CONCEPTS", "250"), ("chunk_chars", "CHUNK_CHARS", "5000"),
            ("max_key_concepts", "MAX_KEY_CONCEPTS", "20"), ("max_relations_per_concept", "MAX_RELATIONS_PER_CONCEPT", "3"),
            ("relation_batch_size", "RELATION_BATCH_SIZE", "12"), ("max_upload_mb", "MAX_UPLOAD_MB", "30"),
        ]}
        if any(value <= 0 for value in numbers.values()) or numbers["chunk_chars"] < 500:
            raise ValueError("配置预算必须为正数，CHUNK_CHARS 至少为 500。")
        if numbers["max_key_concepts"] > 100:
            raise ValueError("MAX_KEY_CONCEPTS 不能超过 100。")
        api_style = os.getenv("LLM_API_STYLE", "responses")
        if api_style not in ("responses", "chat"):
            raise ValueError("LLM_API_STYLE 必须为 responses 或 chat")
        reasoning_effort = os.getenv("LLM_REASONING_EFFORT", "")
        if reasoning_effort not in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("LLM_REASONING_EFFORT 不受支持")
        return cls(data_root=Path(os.getenv("DATA_ROOT", "data")).resolve(),
                   app_key=os.getenv("APP_API_KEY", ""), docling_url=os.getenv("DOCLING_BASE_URL", cls.docling_url).rstrip("/"),
                   docling_key=os.getenv("DOCLING_API_KEY", ""), n8n_url=os.getenv("N8N_WEBHOOK_URL", cls.n8n_url),
                   llm_url=os.getenv("LLM_BASE_URL", "").rstrip("/"), llm_key=os.getenv("LLM_API_KEY", ""),
                   llm_model=os.getenv("LLM_MODEL", ""), api_style=api_style, reasoning_effort=reasoning_effort,
                   json_mode=os.getenv("LLM_JSON_MODE", "true").lower() == "true", **numbers)
