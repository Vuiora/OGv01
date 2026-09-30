import hashlib
import os
import tempfile
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise ValueError("Invalid artifact path")
        return target

    def put(self, content: bytes, namespace: str = "objects") -> tuple[str, str]:
        sha = hashlib.sha256(content).hexdigest()
        key = f"{namespace}/{sha[:2]}/{sha}"
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return key, sha
