"""Explicit, bounded execution settings. No external dependencies."""
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


def normalize_base_url(value: str) -> str:
    """Validate a service base URL without echoing potentially sensitive input."""
    message = "base_url 必须是有效的 HTTPS 基础地址，不能含账号、密码、查询参数或片段"
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "?" in value or "#" in value
                or "\\" in value or '"' in value):
            raise ValueError(message)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    normalized = value.rstrip("/")
    if parsed.path.rstrip("/").endswith("/responses"):
        raise ValueError("base_url 请填写基础地址（例如 https://api.openai.com/v1），不要包含 /responses")
    return normalized


def validate_api_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("api_key 必须是非空字符串或 null")
    value = value.strip()
    if not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("api_key 必须是非空 ASCII 字符串，不能含空白或控制字符")
    return value


def validate_api_key_env(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("api_key_env 必须是环境变量名，例如 OPENAI_API_KEY")
    return value


@dataclass
class Config:
    provider: str = "codex_cli"
    codex_command: str = "codex"
    analyst_model: str = "gpt-5.6-terra"
    planner_model: str = "gpt-5.6-terra"
    developer_model: str = "gpt-5.6-terra"
    reviewer_model: str = "gpt-5.6-terra"
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    api_key_env: str = DEFAULT_API_KEY_ENV
    max_requirements: int = 2
    max_cycles: int = 1
    max_attempts: int = 2
    max_turns: int = 16
    max_output_tokens: int = 8000
    max_api_calls: int = 100
    max_total_tokens: int = 300000
    request_timeout: int = 600
    test_timeout: int = 120
    test_commands: list[list[str]] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    def validate(self):
        limits = {"max_requirements": 10, "max_cycles": 10, "max_attempts": 5,
                  "max_turns": 100, "max_output_tokens": 64000,
                  "max_api_calls": 10000, "max_total_tokens": 10000000,
                  "request_timeout": 600, "test_timeout": 1800}
        for name, upper in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"{name} 必须为 1–{upper} 的整数")
        if self.provider not in ("codex_cli", "openai_api"):
            raise ValueError("provider 必须是 codex_cli 或 openai_api")
        if not isinstance(self.codex_command, str) or not self.codex_command.strip() or "\x00" in self.codex_command:
            raise ValueError("codex_command 必须是非空命令")
        for name in ("analyst_model", "planner_model", "developer_model", "reviewer_model"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} 不能为空")
        if self.base_url is not None:
            self.base_url = normalize_base_url(self.base_url)
        self.api_key = validate_api_key(self.api_key)
        self.api_key_env = validate_api_key_env(self.api_key_env)
        if not isinstance(self.exclude, list) or not all(isinstance(x, str) for x in self.exclude):
            raise ValueError("exclude 必须是字符串数组")
        if not isinstance(self.test_commands, list) or any(
            not isinstance(cmd, list) or not cmd or any(not isinstance(x, str) or not x or "\x00" in x for x in cmd)
            for cmd in self.test_commands
        ):
            raise ValueError("test_commands 必须是命令参数数组，例如 [[\"python\", \"-m\", \"pytest\"]]")
        return self

    @classmethod
    def load(cls, path: Path | None, *, overrides: dict | None = None):
        data = {} if path is None else json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置文件必须是 JSON 对象")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知配置字段: {', '.join(sorted(unknown))}")
        if overrides:
            unknown = set(overrides) - set(cls.__dataclass_fields__)
            if unknown:
                raise ValueError(f"未知配置字段: {', '.join(sorted(unknown))}")
            data.update(overrides)
        return cls(**data).validate()

    def to_dict(self):
        data = asdict(self)
        if data["api_key"] is not None:
            data["api_key"] = "[已脱敏]"
        return data
