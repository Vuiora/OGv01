import argparse
import json
from pathlib import Path
from .bootstrap import ROOT
from .config import LLMConfig
from .models import TaskSpec

def main(argv=None):
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="OGv01 business closed loop")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("task", type=Path)
    run.add_argument("--output", type=Path)
    run.add_argument("--offline", action="store_true")
    run.add_argument("--no-architecture", action="store_true")
    arch = sub.add_parser("architecture")
    arch.add_argument("--output", type=Path, default=ROOT / "runs" / "architecture")
    arch.add_argument("--offline", action="store_true")
    sub.add_parser("check-llm")
    replay = sub.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("--output", type=Path, default=ROOT / "runs" / "business")
    finish = sub.add_parser("finish")
    finish.add_argument("run_id")
    finish.add_argument("--output", type=Path, default=ROOT / "runs" / "business")
    finish.add_argument("--architecture", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8095)
    app = sub.add_parser("application")
    app.add_argument("--output", type=Path, default=ROOT / "artifacts" / "business" / "application.json")
    args = parser.parse_args(argv)
    config = LLMConfig.load().apply()
    try:
        if args.command == "run":
            from .pipeline import BusinessPipeline
            values = json.loads(args.task.read_text(encoding="utf-8-sig"))
            if args.offline:
                values["llm"] = "offline"
            if args.no_architecture:
                values["architecture"] = False
            result = BusinessPipeline(args.output, config=config).run(TaskSpec.model_validate(values), base_dir=args.task.resolve().parent)
        elif args.command == "architecture":
            from .architecture import produce_architecture
            result = produce_architecture(args.output, config, live=not args.offline)
        elif args.command == "check-llm":
            from .llm import JSONClient
            response = JSONClient(config).complete('Return JSON {"ok":true}.', {"task": "connection check"}, max_tokens=64)
            result = {**config.public(), "ok": response.get("ok") is True}
        elif args.command == "replay":
            import re
            if not re.fullmatch(r"[a-f0-9]{32}", args.run_id):
                raise ValueError("Invalid run ID")
            result = json.loads((args.output / args.run_id / "run.json").read_text(encoding="utf-8"))
            result["replay"] = True
            result["new_confirmation_evidence"] = False
        elif args.command == "finish":
            from .pipeline import BusinessPipeline
            result = BusinessPipeline(args.output, config=config).finish(args.run_id, architecture=args.architecture)
        elif args.command == "application":
            from .mentor import build_application
            from .store import write_json
            result = build_application()
            write_json(args.output, result)
            write_json(args.output.with_name("ogflow.n8n.json"), result["workflow"])
            result = {"application": str(args.output), "workflow": str(args.output.with_name("ogflow.n8n.json"))}
        elif args.command == "serve":
            import uvicorn
            uvicorn.run("ogflow.api:app", host="127.0.0.1", port=args.port)
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "FAILED", "error": type(exc).__name__,
            "message": "Inspect local run artifacts; credentials and upstream bodies are not printed."}))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
