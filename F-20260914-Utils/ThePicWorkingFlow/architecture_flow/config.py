from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[1]
REFERENCE_TEMPLATE = Path(r"C:\Users\Lenovo\Desktop\OGv01\F-20260914-Utils\TheStructure.drawio")
FIXED_INSTRUCTIONS = "分析代码职责与数据流。遵循 C:\\Users\\Lenovo\\Desktop\\OGv01\\F-20260914-Utils\\TheStructure.drawio 所体现的架构表达思路、层次组织、配色、连线语义、页面布局和可读性原则。根据待分析代码和文档确定模块名称、数量、边界、组件与连接；总图展示全局关系，各一级模块独立成页，子区域体现内部职责。模板仅供设计参考，不要求复制或保留其原有页面、模块、节点、文字或业务内容。区分已有实现、外部系统和提议设计。"


@dataclass
class Settings:
    input_root: Path
    template: Path
    data_root: Path
    api_key: str
    analysis_url: str
    analysis_model: str
    analysis_key: str
    design_url: str
    design_model: str
    design_key: str
    docling_url: str = "http://127.0.0.1:5001"
    docling_key: str = ""
    n8n_url: str = "http://127.0.0.1:5678/webhook/drawio-architecture"
    max_repairs: int = 2
    llm_timeout: float = 180
    max_source_chars: int = 240_000
    chunk_chars: int = 20_000
    json_mode: bool = True

    @classmethod
    def from_env(cls):
        load_dotenv(BASE / ".env", encoding="utf-8-sig")
        common_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        common_key = os.getenv("LLM_API_KEY", "")
        return cls(
            input_root=Path(os.getenv("INPUT_ROOT", str(BASE.parent))).resolve(),
            template=REFERENCE_TEMPLATE if REFERENCE_TEMPLATE.is_file() else Path(os.getenv("DRAWIO_TEMPLATE", str(BASE.parent / "TheStructure.drawio"))).resolve(),
            data_root=Path(os.getenv("DATA_ROOT", str(BASE / "data"))).resolve(),
            api_key=os.getenv("APP_API_KEY", ""),
            analysis_url=os.getenv("ANALYSIS_BASE_URL", "") or common_url,
            analysis_model=os.getenv("ANALYSIS_MODEL", ""),
            analysis_key=os.getenv("ANALYSIS_API_KEY", "") or common_key,
            design_url=os.getenv("DESIGN_BASE_URL", "") or common_url,
            design_model=os.getenv("DESIGN_MODEL", ""),
            design_key=os.getenv("DESIGN_API_KEY", "") or common_key,
            docling_url=os.getenv("DOCLING_BASE_URL", "http://127.0.0.1:5001").rstrip("/"),
            docling_key=os.getenv("DOCLING_API_KEY", ""),
            n8n_url=os.getenv("N8N_WEBHOOK_URL", "http://127.0.0.1:5678/webhook/drawio-architecture"),
            json_mode=os.getenv("LLM_JSON_MODE", "true").lower() == "true",
        )
