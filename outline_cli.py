#!/usr/bin/env python
"""大纲生成命令行入口。

把逐章梗概逐层归约成一份能读的大纲。

  python outline_cli.py --work 示例作品                 # 只看计划，不调模型
  python outline_cli.py --work 示例作品 --run --yes      # 真跑
  python outline_cli.py --demo --run                          # 本地模拟，不花钱
  python outline_cli.py --work 示例作品 --status

**原料是标注顺带产出的 chapter_summary（P22）**，所以没跑过标注就没有可归约的东西，
这里会直接说明，而不是凭空编一份大纲出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.batch import (  # noqa: E402
    ChapterTask,
    load_chapter_tasks,
    pick_model_id,
    price_bucket,
    resolve_token_coefficient,
)
from workshop.config import load_config  # noqa: E402
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.outline import (  # noqa: E402
    DEFAULT_BLOCK_SIZE,
    OutlineOptions,
    OutlinePlan,
    build_outline_plan,
    generate_outline,
    save_outline,
)
from workshop.primitives import load_primitives  # noqa: E402
from workshop.secrets import SecretStore, setup_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="outline_cli",
        description="大纲：逐章梗概 → 分段梗概 → 全书大纲",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=str(ROOT / "providers.yaml"))
    p.add_argument("--primitives", default=str(ROOT / "primitives.yaml"))
    p.add_argument("--work", help="作品名")
    p.add_argument("--ingest-dir", default=None)
    p.add_argument("--annotations-dir", default=None)
    p.add_argument("--out-dir", default=None, help="默认 workspaces/{作品}/40-outline")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--max-tokens", type=int, default=1200)
    p.add_argument("--run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--status", action="store_true", help="看最近一次结果")
    p.add_argument("--demo", action="store_true")
    return p


def _dirs(args) -> dict[str, Path]:
    if args.demo:
        work_dir = ROOT / ".tmp" / "batch-demo" / "workspaces" / "批量演示"
    else:
        if not args.work:
            raise SystemExit("请用 --work 指定作品名，或用 --demo。")
        work_dir = ROOT / "workspaces" / args.work
    return {
        "work_dir": work_dir,
        "ingest": Path(args.ingest_dir) if args.ingest_dir else work_dir / "00-ingest",
        "annotations": Path(args.annotations_dir) if args.annotations_dir else work_dir / "10-annotations",
        "out": Path(args.out_dir) if args.out_dir else work_dir / "40-outline",
    }


def _resolve_provider(cfg, args):
    from workshop.config import ProviderConfig

    if args.demo:
        return ProviderConfig(
            id="mock",
            raw={
                "id": "mock",
                "name": "本地模拟",
                "enabled": True,
                "base_url": "",
                "auth": {"scheme": "bearer"},
                "models": [{"id": "mock-flash", "role": "main", "pricing": {}, "measured": {}}],
            },
        ), args.model or "mock-flash"
    binding = (cfg.task_bindings or {}).get("volume_review") or {}
    provider_id = args.provider or binding.get("provider")
    if not provider_id:
        raise SystemExit("配置里没有 volume_review 的任务绑定，请用 --provider 指定。")
    provider = cfg.get_provider(str(provider_id))
    if provider is None:
        raise SystemExit(f"配置里找不到服务商 {provider_id}")
    return provider, pick_model_id(cfg, provider, args.model)


def _demo_summaries(dirs: dict[str, Path]) -> None:
    """给演示作品补上逐章梗概，好让大纲链路能跑通。"""
    index = 0
    for path in sorted(dirs["annotations"].glob("*.json")):
        if path.name.startswith("_"):
            continue
        index += 1
        record = json.loads(path.read_text(encoding="utf-8"))
        record.setdefault("fields", {})["chapter_summary"] = f"演示梗概第 {index} 条"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_status(dirs: dict[str, Path]) -> int:
    latest = dirs["out"] / "latest.md"
    if not latest.exists():
        print("还没有生成过大纲。")
        return 0
    print(latest.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    dirs = _dirs(args)

    if args.status:
        return cmd_status(dirs)

    cfg = load_config(args.config)
    provider, model_id = _resolve_provider(cfg, args)

    if args.demo:
        _demo_summaries(dirs)

    tasks: list[ChapterTask] = load_chapter_tasks(dirs["ingest"])
    if not tasks:
        print(f"在 {dirs['ingest']} 里没读到章节清单。", file=sys.stderr)
        return 2

    model_cfg = provider.model(model_id) or {}
    coeff, _basis = resolve_token_coefficient(model_cfg, provider)
    bucket = price_bucket(provider)
    from workshop.batch import _pick_price

    plan = build_outline_plan(
        work_name=args.work or "批量演示",
        provider_id=provider.id,
        model_id=model_id,
        tasks=tasks,
        annotations_dir=dirs["annotations"],
        block_size=args.block_size,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
        max_output_tokens=args.max_tokens,
    )
    _print_plan(plan, provider.name)

    if not args.run:
        print("\n这只是计划，没有发起任何调用。加 --run 才会真的跑。")
        return 0
    if not plan.blocks:
        print("\n没有可归约的内容，已停止。", file=sys.stderr)
        return 1

    if not args.yes:
        try:
            answer = input("\n确认开始？输入 y 继续：").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消。")
            return 0

    store = SecretStore(cfg.reports_dir.parent)
    setup_logging(store.known_values)

    if args.demo:
        from workshop.selftest import MockServer, MockState

        state = MockState(responder=_demo_responder, per_token_ms=0)
        server = MockServer(state)
        server.__enter__()
        base_url = server.base_url
        api_key = "mock-key"
    else:
        api_key = store.get(provider.api_key_ref) if provider.api_key_ref else ""
        if provider.api_key_ref and not api_key:
            print(f"读不到密钥，请设置环境变量 {provider.api_key_ref}", file=sys.stderr)
            return 2
        base_url = provider.base_url
        server = None

    client = OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        timeout_sec=args.timeout,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
    )
    opts = OutlineOptions(
        block_size=args.block_size,
        max_tokens=args.max_tokens,
        timeout_sec=args.timeout,
    )

    def on_progress(index: int, total: int, label: str) -> None:
        print(f"  [{index}/{total}] 归约 {label}", flush=True)

    print()
    try:
        result = generate_outline(
            plan=plan,
            client=client,
            annotations_dir=dirs["annotations"],
            opts=opts,
            secrets=store.known_values,
            on_progress=on_progress,
            state_path=dirs["out"] / "_state.json",
        )
    finally:
        if server is not None:
            server.__exit__(None, None, None)

    paths = save_outline(dirs["work_dir"], result, plan)
    print()
    print(f"分段梗概 {sum(1 for b in result.blocks if b.get('summary'))}/{len(result.blocks)} 块成功")
    if result.errors:
        print(f"未完成 {len(result.errors)} 项：")
        for item in result.errors:
            print(f"  · {item}")
    print(f"实测 usage：{result.usage}")
    print()
    print(f"已落盘：{paths['markdown']}")
    print(f"        {paths['json']}")
    return 0 if result.outline else 1


def _demo_responder(messages: list) -> dict:
    system = str(messages[0].get("content") or "") if messages else ""
    if "structure" in system:
        return {
            "logline": "少年在生日夜被卷入怪谈游戏，为活下去不断解题。",
            "premise": "普通家庭的孩子在生日当晚收到第一张怪谈任务卡。",
            "structure": [{"part": "第一段", "gist": "起步与适应。", "turn": "第一次真正的生死选择。"}],
            "main_threads": [{"thread": "求生主线", "gist": "从被动接任务到主动找规则漏洞。"}],
            "key_turns": ["接受第一个任务"],
            "ending": "尚未完结。",
            "confidence": "中",
            "uncertain_fields": [],
        }
    return {"summary": "（模拟）这一段里主角接受了任务并活了下来，处境从被动转向试探。"}


def _print_plan(plan: OutlinePlan, provider_name: str) -> None:
    print(f"大纲计划  {plan.work}   {provider_name} / {plan.model_id}")
    print("=" * 62)
    print(f"章节       共 {plan.total_chapters} 章   有梗概 {plan.summarized}   缺梗概 {plan.missing}")
    print(f"分块       每块 {plan.block_size} 章   共 {len(plan.blocks)} 块（+ 1 次全书归约）")
    print("")
    print("── 估算 ────────────────────────────────────────────")
    print(f"输入 token  {plan.est_input_tokens:,}   输出 token {plan.est_output_tokens:,}")
    if plan.est_cost_cny is None:
        print(f"费用        {plan.price_note}")
    else:
        print(f"费用        ≈ ¥{plan.est_cost_cny}")
        print(f"            {plan.price_note}")
    if plan.warnings:
        print("")
        print("── 提醒 ────────────────────────────────────────────")
        for item in plan.warnings:
            print(f"  · {item}")


if __name__ == "__main__":
    raise SystemExit(main())
