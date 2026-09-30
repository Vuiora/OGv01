from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from .backends import discover_devices
from .io import dumps, load_program, report_dict
from .model import Device
from .runtime import PLDAModule


def main() -> int:
    parser = argparse.ArgumentParser(description="PLDA parallel feasibility and execution module")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("devices", help="probe actual installed CPU/GPU/NPU backends")
    for command in ("analyze", "run"):
        sub = subs.add_parser(command)
        sub.add_argument("request", type=Path)
        sub.add_argument("--output", type=Path)
        sub.add_argument("--cpu-only", action="store_true")
    args = parser.parse_args()
    try:
        code = 0
        if args.command == "devices":
            devices, diagnostics = discover_devices()
            value = {"devices": [asdict(d) for d in devices], "diagnostics": diagnostics}
        else:
            program = load_program(args.request)
            devices = [Device("cpu:0", "CPU", "cpu", "host", slots=2)] if args.cpu_only else None
            with PLDAModule(devices) as module:
                if args.command == "analyze":
                    value = report_dict(module.analyze(program), module.devices)
                else:
                    result = module.run(program)
                    value = result.to_dict()
                    code = 0 if result.state == "SUCCEEDED" else 2
        rendered = dumps(value)
        if getattr(args, "output", None):
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return code
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        print(dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
