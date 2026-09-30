from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MENTOR_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://mentor:mentor_local@localhost:5432/mentor"
    storage_root: Path = Path(".data")
    teacher_endpoint: str = ""
    teacher_model: str = ""
    teacher_mode: str = "assisted"
    teacher_protocol: str = "responses"
    teacher_api_key: SecretStr = SecretStr("")
    student_endpoint: str = ""
    student_model: str = ""
    student_api_key: SecretStr = SecretStr("")
    model_mode: str = "live"
    n8n_url: str = "http://localhost:5678"
    n8n_api_key: SecretStr = SecretStr("")
    n8n_webhook_secret: SecretStr = SecretStr("")
    service_token: SecretStr = SecretStr("")
    admin_token: SecretStr = SecretStr("")
    reviewer_token: SecretStr = SecretStr("")
    operator_token: SecretStr = SecretStr("")
    organization_id: str = "default"
    dataset_dir: str = ""
    runtime_url: str = "http://mentor-api:8000"
    max_queued_runs: int = 100
    lease_seconds: int = 120
    task_timeout_seconds: int = 300
    allow_local_demo: bool = False
    retention_days: int = 30
    student_input_usd_per_million: float = 0.30
    student_output_usd_per_million: float = 1.20
    teacher_input_usd_per_million: float = 10.0
    teacher_output_usd_per_million: float = 50.0

    def role_config(self, role: str) -> tuple[str, str, str]:
        if role not in {"teacher", "student"}:
            raise ValueError("Unknown model role")
        return (
            getattr(self, f"{role}_endpoint").rstrip("/"),
            getattr(self, f"{role}_model"),
            getattr(self, f"{role}_api_key").get_secret_value(),
        )


@lru_cache
def settings() -> Settings:
    return Settings()
