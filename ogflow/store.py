"""Immutable stage artifacts and an ordered event journal."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()

def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)

class RunStore:
    def __init__(self, root: Path, run_id: str):
        self.path = root.resolve() / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.run_id, self.events = run_id, []

    def artifact(self, name, value):
        write_json(self.path / name, value)
        return {"path": name, "sha256": digest(value)}

    def event(self, stage, state, **details):
        self.events.append({"seq": len(self.events) + 1, "time": datetime.now(timezone.utc).isoformat(),
                            "stage": stage, "state": state, **details})
        write_json(self.path / "events.json", self.events)

