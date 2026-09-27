"""UTF-8 JSON command-line interface for the Module 01 evidence vault."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .errors import AccessDenied, IntegrityError, StateError, ValidationError


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return json.load(stream, parse_constant=lambda value: _invalid_json_number(value))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}") from None


def _invalid_json_number(value: str):
    raise ValidationError(f"Non-finite JSON number is not permitted: {value}")


def _write_json(value, path: str | None = None):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path:
        Path(path).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)


def _token(args) -> str:
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8-sig").strip()
    else:
        token = os.environ.get("SDL_M01_TOKEN", "").strip()
    if not token or any(character.isspace() for character in token):
        raise AccessDenied("Provide exactly one token in --token-file or SDL_M01_TOKEN")
    return token


def save_credentials(tokens: dict[str, str], directory: Path) -> dict[str, str]:
    """Save local administrator credentials separately; never return token contents."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {role: directory / f"{role}.token" for role in tokens}
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("Refusing to overwrite credential files")
    for role, path in paths.items():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(tokens[role] + "\n")
    return {role: str(path.resolve()) for role, path in paths.items()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SDL 模块01：数据与证据协议（UTF-8 JSON）")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="创建新证据库并将角色令牌写入独立文件")
    init.add_argument("--db", required=True)
    init.add_argument("--credentials-dir", required=True)
    init.add_argument("--output", help="JSON 输出文件；默认标准输出")

    demo = commands.add_parser("demo", help="运行120批次×8条记录的合成协议演示")
    demo.add_argument("--output", required=True, help="必须不存在的新目录")

    definitions = {
        "build": ("构建协议", ("spec", "records")),
        "describe": ("读取公开协议", ("protocol",)),
        "read": ("读取探索、开发或历史视图", ("ref",)),
        "quality": ("按角色读取质量报告", ("ref",)),
        "bind": ("冻结计划并绑定封存确证资源", ("ref", "plan")),
        "consume": ("在返回确证数据前原子标记used", ("binding",)),
        "record": ("记录外部评估结果", ("binding", "result")),
        "release": ("发布已记录结果", ("binding",)),
        "results": ("读取已发布结果", ("binding",)),
        "archive": ("建立已发布确证数据的历史视图", ("binding",)),
        "add-confirmation": ("追加新的确证批次", ("protocol", "records", "purpose")),
        "compromise": ("不可逆地标记未用确证数据已暴露", ("ref", "reason")),
        "audit": ("读取证据账本", ()),
        "verify": ("核对快照摘要和账本链", ()),
    }
    for name, (help_text, parameters) in definitions.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--db", required=True)
        command.add_argument("--token-file", help="仅包含一个角色令牌的UTF-8文件；否则用SDL_M01_TOKEN")
        command.add_argument("--output", help="JSON 输出文件；默认标准输出")
        for parameter in parameters:
            command.add_argument(f"--{parameter}", required=True)
        if name == "consume":
            command.add_argument("--replay", action="store_true", help="仅复算同一冻结绑定，不产生新证据")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        from . import Module01, initialize

        if args.command == "demo":
            from .demo import run_demo

            _write_json(run_demo(args.output))
            return 0
        if args.command == "init":
            credentials = Path(args.credentials_dir)
            if credentials.exists() and any(credentials.iterdir()):
                raise FileExistsError("Credentials directory must be new or empty")
            credentials.mkdir(parents=True, exist_ok=True)
            tokens = initialize(args.db)
            result = {"database": str(Path(args.db).resolve()), "credentials": save_credentials(tokens, credentials)}
        else:
            module = Module01(args.db, _token(args))
            actions = {
                "build": lambda: module.build(_read_json(args.spec), _read_json(args.records)),
                "describe": lambda: module.describe(args.protocol),
                "read": lambda: module.read_dataset(args.ref),
                "quality": lambda: module.quality(args.ref),
                "bind": lambda: module.bind_confirmation(args.ref, _read_json(args.plan)),
                "consume": lambda: module.consume_confirmation(args.binding, replay=args.replay),
                "record": lambda: module.record_evaluation(args.binding, _read_json(args.result)),
                "release": lambda: module.release_results(args.binding),
                "results": lambda: module.results(args.binding),
                "archive": lambda: module.archive_confirmation(args.binding),
                "add-confirmation": lambda: module.add_confirmation(args.protocol, _read_json(args.records), args.purpose),
                "compromise": lambda: module.mark_compromised(args.ref, args.reason),
                "audit": module.ledger,
                "verify": module.verify_integrity,
            }
            result = actions[args.command]()
        _write_json(result, args.output)
        return 0
    except (ValidationError, AccessDenied, StateError, IntegrityError, OSError) as exc:
        # Do not include input payloads, stack traces, or role-token values.
        sys.stderr.write(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
