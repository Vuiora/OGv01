from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ExperienceRecord:
    """
    经验库单条记录（JSONL 一行一条）。

    设计目标：
    - 可追加、可检索、可训练元学习器
    - 兼容多任务（分类/回归）、多相关性算法、多评估设置
    """

    dataset_id: str
    meta_features: Dict[str, Any]
    trajectory_features: Optional[Dict[str, Any]]
    evaluations: Dict[str, Dict[str, Any]]  # method_name -> metrics
    selected_method: str
    selection_reason: Dict[str, Any]
    created_at_utc: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "meta_features": self.meta_features,
            "trajectory_features": self.trajectory_features,
            "evaluations": self.evaluations,
            "selected_method": self.selected_method,
            "selection_reason": self.selection_reason,
            "created_at_utc": self.created_at_utc,
        }


class ExperienceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ExperienceRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
