from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLC_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    api_key: str = ""
    web_auto_auth: bool = True
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_json_mode: bool = True
    llm_timeout: int = Field(120, ge=1)
    llm_output_tokens: int = Field(8192, ge=512, le=32768)
    llm_retries: int = Field(2, ge=0, le=5)
    max_upload_mb: int = Field(40, ge=1, le=500)
    max_files: int = Field(10, ge=1, le=50)
    max_pages: int = Field(300, ge=1)
    max_source_chars: int = Field(400_000, ge=1000)
    chunk_chars: int = Field(7000, ge=500, le=30000)
    chunk_overlap: int = Field(300, ge=0, le=400)
    max_chunks: int = Field(80, ge=1)
    max_concepts: int = Field(300, ge=1, le=1000)
    plan_chars: int = Field(120_000, ge=1000)
    job_timeout: int = Field(3600, ge=30)
    lease_seconds: int = Field(120, ge=30)
    poll_seconds: float = Field(2, ge=0.1)
    pdf_font: Path | None = None
