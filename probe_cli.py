#!/usr/bin/env python
"""探测脚本命令行入口。

用法示例：

  # 列出配置里有哪些服务商与模型
  python probe_cli.py --list

  # 探测 DeepSeek（手动「测试连接」）
  python probe_cli.py --provider deepseek

  # 指定模型与采样次数
  python probe_cli.py --provider deepseek --model deepseek-v4-pro --samples 5

  # 带上全量任务的规模，顺便推导全量耗时
  python probe_cli.py --provider deepseek --chapters 1000 --concurrency 4

  # 不落盘（只在屏幕上看看）
  python probe_cli.py --provider deepseek --no-archive

  # 自检：跑本地模拟服务，不花钱、不需要密钥
  python probe_cli.py --self-test
  python probe_cli.py --self-test --mock-no-json
  python probe_cli.py --self-test --mock-no-usage
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop import __version__  # noqa: E402
from workshop.config import DEFAULT_CONFIG_NAME, load_config, resolve_default_model  # noqa: E402
from workshop.errors import ErrorKind  # noqa: E402
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.probe import ProbeOptions, format_report_text, run_probe  # noqa: E402
from workshop.report import (  # noqa: E402
    compare_reports,
    find_previous,
    format_comparison,
    save_report,
)
from workshop.secrets import SecretStore, setup_logging  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_cli",
        description="模型服务商探测与基准测试（手动「测试连接」）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"workshop {__version__}")
    parser.add_argument(
        "--config",
        default=str(ROOT / DEFAULT_CONFIG_NAME),
        help="配置文件路径，默认同目录下的 providers.yaml",
    )
    parser.add_argument("--list", action="store_true", help="列出配置里的服务商与模型后退出")
    parser.add_argument("--provider", help="服务商 id，如 deepseek")
    parser.add_argument("--model", help="模型 id，省略时按 probe_model > main 角色 > 首个模型 选择")

    parser.add_argument("--text-chars", type=int, default=None, help="测试文本的中文字符数")
    parser.add_argument("--samples", type=int, default=None, help="采样次数，取中位数")
    parser.add_argument("--max-tokens", type=int, default=None, help="探测请求的最大输出长度")
    parser.add_argument("--timeout", type=float, default=None, help="单请求读取超时（秒）")
    parser.add_argument("--no-stream", action="store_true", help="关闭流式（将测不出首字延迟）")
    parser.add_argument(
        "--thinking",
        choices=["disabled", "enabled", "auto"],
        default=None,
        help="思考模式，默认 disabled。探测短输出任务应关闭，否则思维链会占满输出预算",
    )
    parser.add_argument("--reasoning-effort", default=None, help="思考强度：low / high / max")
    parser.add_argument("--chapters", type=int, default=None, help="待处理章数，用于推导全量耗时")
    parser.add_argument("--concurrency", type=int, default=None, help="并发数，用于推导全量耗时")

    parser.add_argument("--no-archive", action="store_true", help="不落盘存档")
    parser.add_argument("--no-compare", action="store_true", help="跳过与上一次的对比")

    parser.add_argument("--self-test", action="store_true", help="跑本地模拟服务自检")
    parser.add_argument("--mock-no-json", action="store_true", help="自检：模拟不支持结构化输出")
    parser.add_argument("--mock-no-usage", action="store_true", help="自检：模拟不返回用量字段")
    parser.add_argument(
        "--mock-thinking",
        action="store_true",
        help="自检：模拟默认开启思考模式（思维链吃满输出预算，正文为空）",
    )
    return parser


def cmd_list(config_path: str) -> int:
    cfg = load_config(config_path)
    print(f"配置文件：{cfg.path}")
    print(f"计价货币：{cfg.settings.get('currency')}   闸门模式：{cfg.settings.get('budget_gate_mode')}")
    print(f"报告目录：{cfg.reports_dir}")
    print()
    providers = cfg.providers
    if not providers:
        print("配置里还没有任何服务商。")
        return 1
    for p in providers:
        status = "已启用" if p.enabled else "未启用"
        print(f"[{p.id}] {p.name}  ({p.type} / {p.protocol})  {status}")
        print(f"    base_url: {p.base_url or '未填'}")
        for m in p.models:
            role = m.get("role") or "-"
            print(f"    · {m.get('id')}  role={role}  alias={m.get('alias')}")
        if not p.models:
            print("    （未配置模型）")
        print()
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    state = MockState(
        supports_json=not args.mock_no_json,
        supports_usage=not args.mock_no_usage,
        thinking_default=args.mock_thinking,
    )
    opts = ProbeOptions(
        text_chars=args.text_chars or 1000,
        samples=args.samples or 3,
        streaming=not args.no_stream,
        max_tokens=args.max_tokens or 8,
        timeout_sec=args.timeout or 30.0,
        triggered_by="self_test",
        # 模拟真实事故现场：不传 thinking 字段，由服务端按默认值决定
        thinking="auto" if args.mock_thinking else "disabled",
    )

    print("自检模式：启动本地模拟服务，不产生任何真实调用与费用。")
    print(
        f"模拟开关：structured_output={state.supports_json}  "
        f"usage={state.supports_usage}  thinking_default={state.thinking_default}"
    )
    print()

    with MockServer(state) as server:
        client = OpenAICompatProvider(
            base_url=server.base_url, api_key="mock-key", timeout_sec=opts.timeout_sec
        )
        report = run_probe(
            client=client,
            provider_id="mock",
            provider_name="本地模拟服务",
            model_id="mock-flash",
            opts=opts,
            pricing=None,
            base_url=server.base_url,
            protocol="openai_compatible",
        )

    print(format_report_text(report))
    print()

    reports_dir = ROOT / "config" / "probe-reports" / "_selftest"
    paths = save_report(report, reports_dir)
    print(f"报告已落盘：{paths['yaml'].relative_to(ROOT)}")

    failures = verify_self_test(report, args)

    # 对比链路：第二次及以后跑自检时会与上一次对比，
    # 顺带验证「可比性校验」是否按预期工作——这一条比「能不能对比」更重要。
    previous = find_previous(reports_dir, "mock", "mock-flash", report.get("report_id"))
    print()
    if previous:
        print("── 与上一次对比 ──────────────────────────────")
        notes = compare_reports(previous, report, 50.0)
        print(format_comparison(notes))

        prev_hash = (previous.get("config_snapshot") or {}).get("prompt_hash")
        curr_hash = (report.get("config_snapshot") or {}).get("prompt_hash")
        flagged = any(n.get("level") == "incomparable" for n in notes)

        if prev_hash == curr_hash and flagged:
            failures.append("同一测试配方下不应判定为不可比")
        if prev_hash != curr_hash and not flagged:
            failures.append("测试配方不同时应判定为不可比，实际未拦下")
    else:
        print("（没有历史报告，本次作为基线）")

    print()
    if failures:
        print("自检结果：未通过")
        for item in failures:
            print(f"  · {item}")
        return 1
    print("自检结果：全部通过")
    return 0


def verify_self_test(report: dict, args: argparse.Namespace) -> list[str]:
    """自检的断言。这一步是自检真正的价值所在——它检验的是判定逻辑，不只是能不能跑通。"""
    failures: list[str] = []
    results = report.get("results") or {}
    verdict = report.get("verdict") or {}
    benchmark = report.get("benchmark") or {}
    errs = report.get("errors") or []
    kinds = {e.get("kind") for e in errs}

    if not results.get("reachable"):
        failures.append("P1 预期可达，实际不可达")

    discovered = results.get("models_discovered") or []
    if "mock-flash" not in discovered:
        failures.append(f"P1 预期模型列表含 mock-flash，实际为 {discovered}")

    expect_json: bool | None = None if args.mock_thinking else (not args.mock_no_json)
    if results.get("structured_output") is not expect_json:
        failures.append(
            f"P3 预期 structured_output={expect_json}，实际 {results.get('structured_output')}"
        )

    expect_usage = not args.mock_no_usage
    if results.get("usage_in_response") is not expect_usage:
        failures.append(
            f"P4 预期 usage_in_response={expect_usage}，实际 {results.get('usage_in_response')}"
        )

    if expect_usage:
        coefficient = results.get("tokens_per_cjk_char")
        if results.get("tokens_per_cjk_char_estimated"):
            failures.append("P5 预期拿到实测换算系数，实际回退成了粗估")
        elif coefficient is None or abs(float(coefficient) - 0.65) > 0.08:
            failures.append(f"P5 预期换算系数接近 0.65，实际 {coefficient}")
    else:
        if not results.get("tokens_per_cjk_char_estimated"):
            failures.append("P5 无用量时应回退为粗估系数，实际标记为实测")

    # 降级场景下服务商仍应可启用
    expect_enabled = True
    if verdict.get("enabled_ok") is not expect_enabled:
        failures.append(f"结论预期 enabled_ok={expect_enabled}，实际 {verdict.get('enabled_ok')}")

    ttft = (benchmark.get("ttft_ms") or {}).get("median")
    first_content = (benchmark.get("first_content_ms") or {}).get("median")

    if not args.no_stream:
        if ttft is None:
            failures.append("基准测预期拿到首字延迟，实际未测到")

    if args.mock_thinking:
        # 思考模式的回归断言：思维链也是输出，首字延迟必须测得到；
        # 但正文始终为空，正文首字不该出现。这两条一起验证了 reasoning_content 的处理是否正确。
        if not args.no_stream and first_content is not None:
            failures.append("思考模式下正文为空，不应测到正文首字")
        if not benchmark.get("thinking_used"):
            failures.append("预期检测到思维链，实际未检测到")
        if not (report.get("derived") or {}).get("note"):
            failures.append("思考模式下推导值应附带警告说明，实际没有")
        if ErrorKind.OUTPUT_BUDGET_CONSUMED_BY_REASONING.value not in kinds:
            failures.append("预期记录「输出被思维链占满」的提示，实际未记录")
    elif not args.no_stream:
        if first_content is None:
            failures.append("非思考模式下应能测到正文首字，实际未测到")

    if args.mock_no_json and ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED.value not in kinds:
        failures.append("预期记录「不支持结构化输出」的提示，实际未记录")
    if args.mock_no_usage and ErrorKind.USAGE_MISSING.value not in kinds:
        failures.append("预期记录「用量缺失」的提示，实际未记录")

    for e in errs:
        if e.get("blocking"):
            failures.append(f"出现阻断性错误：{e.get('step')} {e.get('kind')}")

    return failures


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not args.provider:
        print("请用 --provider 指定服务商，或先用 --list 查看可选项。", file=sys.stderr)
        return 2

    provider = cfg.get_provider(args.provider)
    if provider is None:
        available = ", ".join(p.id for p in cfg.providers) or "（无）"
        print(f"配置里找不到服务商 {args.provider}。可选：{available}", file=sys.stderr)
        return 2

    try:
        model_id = args.model or resolve_default_model(cfg, provider)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    secrets = SecretStore(cfg.reports_dir.parent)
    setup_logging(secrets.known_values)

    api_key = secrets.get(provider.api_key_ref)
    if provider.api_key_ref and not api_key:
        print(
            f"读不到密钥。请设置环境变量 {provider.api_key_ref}，"
            f"或写入 {cfg.reports_dir.parent / 'secrets.json'}。",
            file=sys.stderr,
        )
        return 2

    probe_cfg = cfg.probe_settings
    opts = ProbeOptions(
        text_chars=args.text_chars or int(probe_cfg.get("probe_text_chars") or 1000),
        samples=args.samples or int(probe_cfg.get("samples") or 3),
        streaming=(not args.no_stream) and bool(probe_cfg.get("streaming", True)),
        max_tokens=args.max_tokens or 8,
        timeout_sec=args.timeout or 60.0,
        chapters=args.chapters,
        concurrency=args.concurrency,
        triggered_by="manual_test_connection",
        thinking=args.thinking or cfg.probe_thinking,
        reasoning_effort=args.reasoning_effort,
    )

    print(f"开始探测  {provider.name} / {model_id}")
    print(f"采样 {opts.samples} 次 · 测试文本 {opts.text_chars} 字 · "
          f"流式 {'开' if opts.streaming else '关'} · 思考模式 {opts.thinking}")
    print()

    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=opts.timeout_sec,
        auth_scheme=provider.auth_scheme,
        secrets=secrets.known_values,
        rate_limit=provider.rate_limit,
    )

    report = run_probe(
        client=client,
        provider_id=provider.id,
        provider_name=provider.name,
        model_id=model_id,
        opts=opts,
        pricing=provider.pricing,
        base_url=provider.base_url,
        protocol=provider.protocol,
        anthropic_base_url=provider.base_url_anthropic,
    )

    print(format_report_text(report))

    if not args.no_archive and cfg.archive_enabled:
        paths = save_report(report, cfg.reports_dir, secrets=secrets.known_values)
        print()
        print(f"报告已存档：{paths['yaml']}")
        if "markdown" in paths:
            print(f"人可读版本：{paths['markdown']}")
    elif args.no_archive:
        print()
        print("（按 --no-archive 要求，本次未落盘）")

    if not args.no_compare:
        previous = find_previous(cfg.reports_dir, provider.id, model_id, report.get("report_id"))
        if previous:
            print()
            print("── 与上一次对比 ──────────────────────────────")
            notes = compare_reports(previous, report, cfg.alert_threshold_pct)
            print(format_comparison(notes))
        else:
            print()
            print("（没有历史报告，本次作为基线）")

    return 0 if (report.get("verdict") or {}).get("enabled_ok") else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    if args.self_test:
        return cmd_self_test(args)
    if args.list:
        return cmd_list(args.config)
    return cmd_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
