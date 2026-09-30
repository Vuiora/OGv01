import argparse
from datetime import datetime
from glob import escape
from pathlib import Path
import sys
import uuid

from .config import Config, DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL
from .demo import DemoProvider
from .pipeline import Pipeline
from .report import LABELS
from .runtime import CodexCLIProvider, OpenAIProvider


def parser():
    p = argparse.ArgumentParser(description="Self Renew · 自主发现代码不足与创新机会，生成需求并委派开发智能体")
    commands = p.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="无需 API 密钥的离线流程演示，文件修改和测试真实执行")
    demo.add_argument("--output", type=Path, help="尚不存在的输出目录")
    run = commands.add_parser("run", help="对指定本地项目运行 Codex 多智能体研发流程")
    run.add_argument("repo", type=Path, help="任意已准备好的本地项目目录")
    run.add_argument("--config", type=Path, help="JSON 配置，参考 renew.example.json")
    run.add_argument("--output", type=Path, help="项目之外、尚不存在的输出目录")
    run.add_argument("--model", help="覆盖所有角色模型；也可在 JSON 中分别配置")
    run.add_argument("--provider", choices=("codex_cli", "openai_api"), help="覆盖配置中的调用方式")
    run.add_argument("--endpoint", "--base-url", dest="base_url", help="覆盖 HTTPS API 基础地址，例如 https://api.openai.com/v1")
    key = run.add_mutually_exclusive_group()
    key.add_argument("--api-key", help="本次运行使用的 API key；推荐改用 --api-key-env 避免写入命令历史")
    key.add_argument("--api-key-env", help="读取此环境变量作为 API key，并覆盖配置文件中的 api_key")
    run.add_argument("--analyze-only", action="store_true", help="仅分析并生成需求")
    run.add_argument("--run-tests", action="store_true", help="在本地主机的项目副本中执行配置中的测试命令（用于可信项目）")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    pipeline = None
    try:
        demo = args.command == "demo"
        source = (Path(__file__).resolve().parent.parent / "examples" / "tiny_project") if demo else args.repo.expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"项目目录不存在: {source}")
        overrides = {}
        if not demo:
            for field in ("provider", "base_url", "api_key", "api_key_env"):
                value = getattr(args, field)
                if value is not None:
                    overrides[field] = value
            if args.api_key_env is not None:
                overrides["api_key"] = None
            if args.model is not None:
                overrides.update({field: args.model for field in (
                    "analyst_model", "planner_model", "developer_model", "reviewer_model")})
        config = Config() if demo else Config.load(args.config, overrides=overrides)
        if not demo and args.api_key_env is not None and config.base_url is None:
            config.base_url = DEFAULT_BASE_URL
        if demo:
            config.test_commands = [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]]
            provider = DemoProvider()
            print("离线演示：模型决策为固定模拟数据；不会调用 OpenAI API。", flush=True)
        else:
            # A local run configuration may contain a literal key. Keep it out of
            # project snapshots and model context, including when CLI overrides it.
            if args.config is not None:
                config_path = args.config.expanduser().resolve()
                if config_path.is_relative_to(source):
                    relative_config = escape(config_path.relative_to(source).as_posix())
                    if relative_config not in config.exclude:
                        config.exclude.append(relative_config)
            connection = {"base_url": config.base_url, "api_key": config.api_key,
                          "api_key_env": config.api_key_env}
            if config.provider == "codex_cli":
                provider = CodexCLIProvider(command=config.codex_command, timeout=config.request_timeout,
                                            max_calls=config.max_api_calls, max_total_tokens=config.max_total_tokens,
                                            **connection)
                custom = config.base_url is not None or config.api_key is not None or config.api_key_env != DEFAULT_API_KEY_ENV
                print("标准 Codex CLI 模式：使用本次配置的 endpoint 与 API key。" if custom
                      else "标准 Codex CLI 模式：使用本机 codex 登录与服务配置。", flush=True)
            else:
                provider = OpenAIProvider(timeout=config.request_timeout,
                                          max_calls=config.max_api_calls, max_total_tokens=config.max_total_tokens,
                                          **connection)
                print("兼容 API 模式：直接调用配置的 Responses API 端点。", flush=True)
            if not args.run_tests or not config.test_commands:
                print("本次未配置并启用测试命令，开发成果将标记为待验证。", flush=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        output = args.output or source.parent / ".renew-runs" / name
        pipeline = Pipeline(config, provider, output,
                            execute_tests=demo or args.run_tests,
                            analyze_only=False if demo else args.analyze_only,
                            demo=demo, progress=lambda message: print(message, flush=True))
        state = pipeline.run(source)
        print(f"\n状态：{LABELS.get(state['status'], state['status'])}")
        print(f"报告：{pipeline.output / 'report.md'}")
        print(f"交付副本：{state['workspace']}")
        print(f"代码差异：{pipeline.output / 'changes.patch'}")
        return 2 if state["status"] in ("needs_attention", "integration_failed") else 0
    except KeyboardInterrupt:
        print("\n已中断，已产生的候选代码和日志会保留。", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"运行未完成：{exc}", file=sys.stderr)
        if pipeline is not None and (pipeline.output / "report.md").is_file():
            print(f"运行记录：{pipeline.output / 'report.md'}", file=sys.stderr)
        return 1
