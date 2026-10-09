"""Command-line entry point for configuration checks and test generation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import __version__
from .agent import run_agent
from .config import ConfigError, load_config
from .llm_client import DeepSeekClient
from .models import ModelInfo, OracleMode, TaskInput, TaskStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testgen-agent",
        description="读取 Python 源码和行为规格，调用模型生成并执行 pytest 测试。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--check-config", action="store_true", help="仅校验并显示脱敏配置")
    parser.add_argument("--source", type=Path, help="待测试的 Python 源文件")
    parser.add_argument("--spec", type=Path, help="UTF-8 行为规格文件")
    parser.add_argument("--output", type=Path, help="新的运行目录；默认写入 outputs/cli/")
    parser.add_argument("--env-file", type=Path, help="指定 UTF-8 .env；默认读取当前目录的 .env")
    parser.add_argument("--provider", help="LLM 服务商，当前支持 deepseek")
    parser.add_argument("--model", help="DeepSeek 模型标识")
    parser.add_argument("--base-url", help="DeepSeek 服务地址")
    parser.add_argument("--proxy-url", help="可选 HTTP(S) 代理地址；建议通过 .env 配置")
    parser.add_argument("--api-key-env", help="保存 API Key 的环境变量名；不接受明文 Key")
    parser.add_argument("--request-timeout", type=int, dest="request_timeout_seconds", help="模型请求超时，1..600 秒")
    parser.add_argument("--max-tokens", type=int, help="单次模型输出上限，1024..32768；默认 8192")
    parser.add_argument("--max-rounds", type=int, help="候选执行轮数，1..3（含首轮）")
    parser.add_argument("--timeout", type=int, dest="execution_timeout_seconds", help="单轮测试执行超时，1..300 秒")
    return parser


def _default_output() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("outputs/cli") / f"{stamp}-{uuid4().hex[:8]}"


def _read_spec(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("无法读取行为规格，请检查路径、权限和 UTF-8 编码。") from None
    if not value.strip():
        raise ValueError("行为规格不能为空。")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure) and not stream.isatty():
            reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.check_config and (args.source is None or args.spec is None):
        parser.error("生成测试需要同时提供 --source 和 --spec；仅检查配置可使用 --check-config。")
    overrides = {
        field: getattr(args, field)
        for field in (
            "provider", "model", "base_url", "proxy_url", "api_key_env", "request_timeout_seconds", "max_tokens",
            "max_rounds", "execution_timeout_seconds",
        )
    }
    try:
        config = load_config(env_file=args.env_file, overrides=overrides)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.check_config:
        print(json.dumps(config.safe_summary(), ensure_ascii=False, indent=2))
        print("配置检查通过（未发送模型请求）。")
        return 0

    try:
        specification = _read_spec(args.spec)
        output = args.output or _default_output()
        request = TaskInput(
            source_path=args.source,
            spec_path=args.spec,
            output_dir=output,
            max_rounds=config.max_rounds,
            execution_timeout_seconds=config.execution_timeout_seconds,
            model=ModelInfo.from_config(config),
        )
        result = run_agent(
            request,
            client=DeepSeekClient(config),
            oracle_mode=OracleMode.SPECIFICATION,
            behavior_specification=specification,
        )
    except (OSError, ValueError) as error:
        print(str(error) or type(error).__name__, file=sys.stderr)
        return 2

    summary = {
        "status": result.decision.status,
        "stop_reason": result.state.stop_reason,
        "conclusion": json.loads(result.summary_file.read_text(encoding="utf-8"))["conclusion"],
        "output_dir": str(output.resolve()),
        "test_file": str(result.final_test_file.resolve()) if result.final_test_file else None,
        "summary_file": str(result.summary_file.resolve()),
        "report_file": str(result.report_file.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.decision.status in {TaskStatus.COMPLETED, TaskStatus.PARTIAL} else 1
