import sys
import zipfile

from clc.build import build_graph, write_json
from clc.config import Settings
from clc.export import export_graph
from clc.store import Store


def main():
    settings = Settings()
    store = Store(settings.data_dir)
    job = store.get(sys.argv[1])
    if not job or job["status"] != "running":
        return 1
    folder = store.job_dir(job["id"])
    try:
        request = job["request"]
        graph = build_graph(request["title"], request["sources"], folder, settings,
                            progress=lambda stage: store.stage(job["id"], stage))
        store.stage(job["id"], "exporting")
        export_graph(graph, folder / "output", request["formats"], settings)
        with zipfile.ZipFile(folder / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted((folder / "output").rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(folder / "output"))
        write_json(folder / "result.json", {"error": None})
        return 0
    except Exception as exc:  # noqa: BLE001 - process boundary must persist a terminal failure
        # ValidationError can include source contents; don't persist those in externally visible status.
        safe = str(exc) if type(exc) in {ValueError, RuntimeError, TimeoutError} else f"处理失败 ({type(exc).__name__})"
        write_json(folder / "result.json", {"error": safe[:500]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
