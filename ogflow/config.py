"""One business LLM endpoint, with aliases for existing subproject entrypoints."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from dotenv import load_dotenv
from .bootstrap import ROOT

@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "https://llm.ujn.edu.cn/v1"
    model: str = "deepseek-v41-flash"
    api_key: str = field(default="", repr=False)
    timeout: float = 180

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Business LLM base URL must be an HTTPS base address")
        if not self.model or self.timeout <= 0:
            raise ValueError("LLM model and positive timeout are required")

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env", override=False)
        return cls(os.getenv("OG_LLM_BASE_URL", cls.base_url).rstrip("/"),
                   os.getenv("OG_LLM_MODEL", cls.model), os.getenv("OG_LLM_API_KEY", ""),
                   float(os.getenv("OG_LLM_TIMEOUT", "180")))

    def public(self):
        return {"base_url": self.base_url, "model": self.model, "protocol": "chat/completions"}

    def apply(self):
        # Explicit canonical settings take precedence over old per-project model selections.
        values = {
            "LLM_BASE_URL": self.base_url, "LLM_API_KEY": self.api_key, "LLM_MODEL": self.model,
            "LLM_API_STYLE": "chat", "LLM_JSON_MODE": "true",
            "ANALYSIS_BASE_URL": self.base_url, "DESIGN_BASE_URL": self.base_url,
            "ANALYSIS_MODEL": self.model, "DESIGN_MODEL": self.model,
            "ANALYSIS_API_KEY": self.api_key, "DESIGN_API_KEY": self.api_key,
            "CLC_LLM_BASE_URL": self.base_url, "CLC_LLM_MODEL": self.model, "CLC_LLM_API_KEY": self.api_key,
            "SDL_LLM_BASE_URL": self.base_url, "SDL_LLM_MODEL": self.model, "SDL_LLM_API_KEY": self.api_key,
            "MENTOR_TEACHER_ENDPOINT": self.base_url, "MENTOR_STUDENT_ENDPOINT": self.base_url,
            "MENTOR_TEACHER_MODEL": self.model, "MENTOR_STUDENT_MODEL": self.model,
            "MENTOR_TEACHER_API_KEY": self.api_key, "MENTOR_STUDENT_API_KEY": self.api_key,
            "MENTOR_TEACHER_PROTOCOL": "chat", "SELF_RENEW_API_KEY": self.api_key,
        }
        os.environ.update(values)
        return self

