"""Run each project's existing offline suite in its own working directory."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = {
    "sdl": ("F-20260919-StatisticalDiscoveryLearning", "unittest"),
    "plda": ("F-20260914-Utils/ParallelLogicDeterminationAlgorithModule", "unittest"),
    "renew": ("F-260908-GPTSelf-renew", "unittest"),
    "clc": ("F-20260914-Utils/ConceptLayerConstructor", "pytest"),
    "crd": ("F-20260914-Utils/ConceptRelationDraw", "pytest"),
    "architecture": ("F-20260914-Utils/ThePicWorkingFlow", "pytest"),
    "mtbmt": ("MTBMT", "pytest"),
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdlib", action="store_true", help="Only SDL, PLDA and Self Renew")
    parser.add_argument("--project", choices=list(PROJECTS), action="append")
    parser.add_argument("--python", default=sys.executable, help="Interpreter with project test dependencies")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = []
    for name in args.project or list(PROJECTS):
        path, framework = PROJECTS[name]
        if args.stdlib and framework != "unittest":
            continue
        command = [args.python, "-m", "unittest", "discover", "-s", "tests", "-v"] if framework == "unittest" else [args.python, "-m", "pytest", "-q"]
        print(f"Running {name}: {path}", flush=True)
        started = time.monotonic()
        environment = os.environ.copy()
        source = ROOT / path / "src"
        if source.is_dir():
            environment["PYTHONPATH"] = str(source) + (os.pathsep + environment["PYTHONPATH"]
                                                      if environment.get("PYTHONPATH") else "")
        result = subprocess.run(command, cwd=ROOT / path, env=environment)
        results.append({"project": name, "path": path, "exit_code": result.returncode,
                        "seconds": round(time.monotonic() - started, 2)})
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if all(x["exit_code"] == 0 for x in results) else 1)

if __name__ == "__main__":
    main()
