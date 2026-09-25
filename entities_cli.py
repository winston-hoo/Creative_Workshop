#!/usr/bin/env python
"""实体统计命令行入口：人物、势力、能力、地点、人物关系。

  python entities_cli.py --work 示例作品            # 只看计划，不调模型
  python entities_cli.py --work 示例作品 --run --yes  # 真跑
  python entities_cli.py --work 示例作品 --run --yes --limit 3   # 先跑 3 块，再跑一次接着跑
  python entities_cli.py --demo --run                     # 本地模拟
  python entities_cli.py --work 示例作品 --status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.batch import _pick_price, load_chapter_tasks, pick_model_id, price_bucket  # noqa: E402
from workshop.config import load_config  # noqa: E402
from workshop.entities import (  # noqa: E402
    ALIASES_BASENAME,
    DEFAULT_BLOCK_SIZE,
    EntitiesOptions,
    EntitiesPlan,
    build_entities_plan,
    generate_entities,
    load_aliases,
    save_entities,
)
from workshop.llm import MAX_TOKENS_CAP, OpenAICompatProvider, default_max_tokens  # noqa: E402
from workshop.secrets import SecretStore, setup_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="entities_cli",
        description="实体统计：人物、势力、能力、地点、关系",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=str(ROOT / "providers.yaml"))
    p.add_argument("--work", help="作品名")
    p.add_argument("--ingest-dir", default=None)
    p.add_argument("--out-dir", default=None, help="默认 workspaces/{作品}/50-entities")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--max-tokens", type=int, default=None,
                   help="单块输出 token 预算起点；不填则取模型声明的 max_output（封顶 32000）")
    p.add_argument("--limit", type=int, default=None,
                   help="本次最多跑几块。没轮到的块标成「还没跑」，再跑一次接着跑，已完成的块不重复花钱")
    p.add_argument("--run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--status", action="store_true")
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
        "out": Path(args.out_dir) if args.out_dir else work_dir / "50-entities",
    }


def _resolve(cfg, args):
    from workshop.config import ProviderConfig

    if args.demo:
        return (
            ProviderConfig(
                id="mock",
                raw={
                    "id": "mock",
                    "name": "本地模拟",
                    "enabled": True,
                    "base_url": "",
                    "auth": {"scheme": "bearer"},
                    "models": [{"id": "mock-flash", "role": "heavy", "pricing": {}, "measured": {}}],
                },
            ),
            args.model or "mock-flash",
        )
    binding = (cfg.task_bindings or {}).get("entity_extraction") or {}
    provider_id = args.provider or binding.get("provider")
    if not provider_id:
        raise SystemExit("配置里没有 entity_extraction 的任务绑定，请用 --provider 指定。")
    provider = cfg.get_provider(str(provider_id))
    if provider is None:
        raise SystemExit(f"配置里找不到服务商 {provider_id}")
    return provider, pick_model_id(cfg, provider, args.model)


def _demo_responder(messages: list) -> dict:
    return {
        "characters": [
            {
                "name": "演示主角",
                "role": "主角",
                "identity": "活下去的人",
                "abilities": ["演示能力"],
                "factions": [],
                "first_chapter": 1,
                "relations": [{"to": "演示配角", "type": "同伴", "note": "一起行动"}],
            },
            {"name": "演示配角", "role": "配角", "identity": "同行者", "first_chapter": 2},
        ],
        "factions": [{"name": "演示势力", "stance": "不明", "members": ["演示主角"], "first_chapter": 1}],
        "abilities": [{"name": "演示能力", "holder": "演示主角", "effect": "演示效果", "first_chapter": 1}],
        "locations": [{"name": "演示地点", "note": "故事发生地", "first_chapter": 1}],
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    dirs = _dirs(args)

    if args.status:
        latest = dirs["out"] / "latest.md"
        print(latest.read_text(encoding="utf-8") if latest.exists() else "还没有生成过实体统计。")
        return 0

    cfg = load_config(args.config)
    provider, model_id = _resolve(cfg, args)
    tasks = load_chapter_tasks(dirs["ingest"])
    if not tasks:
        print(f"在 {dirs['ingest']} 里没读到章节清单。", file=sys.stderr)
        return 2

    model_cfg = provider.model(model_id) or {}
    budget = args.max_tokens or default_max_tokens(model_cfg)
    bucket = price_bucket(provider)
    plan: EntitiesPlan = build_entities_plan(
        work=args.work or "批量演示",
        provider_id=provider.id,
        model_id=model_id,
        tasks=tasks,
        block_size=args.block_size,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
    )

    print(f"实体统计计划  {plan.work}   {provider.name} / {plan.model_id}")
    print("=" * 62)
    print(f"章节       共 {plan.total_chapters} 章   分块 每块 {plan.block_size} 章 × {plan.blocks} 块")
    print(f"正文       {plan.body_chars:,} 字（含空白与缩进）")
    print(f"输入 token ≈ {plan.est_input_tokens:,}   输出 ≈ {plan.est_output_tokens:,}")
    print(f"费用       {'≈ ¥' + str(plan.est_cost_cny) if plan.est_cost_cny is not None else plan.price_note}")
    print(
        f"输出预算   每块 {budget:,} token"
        f"（被 max_tokens 截断时自动翻倍，封顶 {MAX_TOKENS_CAP:,}）"
    )
    if args.limit:
        print(f"本次上限   只跑 {args.limit} 块，剩下的标成「还没跑」，再跑一次接着跑")
    print()
    if not args.run:
        print("这只是计划，没有发起任何调用。加 --run 才会真的跑。")
        return 0
    if not args.yes:
        try:
            if input("确认开始？输入 y 继续：").strip().lower() not in ("y", "yes"):
                print("已取消。")
                return 0
        except EOFError:
            return 0

    store = SecretStore(cfg.reports_dir.parent)
    setup_logging(store.known_values)

    if args.demo:
        from workshop.selftest import MockServer, MockState

        server = MockServer(MockState(responder=lambda m: _demo_responder(m), per_token_ms=0))
        server.__enter__()
        client = OpenAICompatProvider(base_url=server.base_url, api_key="k", timeout_sec=args.timeout)
    else:
        api_key = store.get(provider.api_key_ref) if provider.api_key_ref else ""
        if provider.api_key_ref and not api_key:
            print(f"读不到密钥，请设置环境变量 {provider.api_key_ref}", file=sys.stderr)
            return 2
        client = OpenAICompatProvider(
            base_url=provider.base_url,
            api_key=api_key,
            timeout_sec=args.timeout,
            auth_scheme=provider.auth_scheme,
            secrets=store.known_values,
            rate_limit=provider.rate_limit,
        )
        server = None

    print()
    try:
        result = generate_entities(
            plan=plan,
            tasks=tasks,
            client=client,
            opts=EntitiesOptions(block_size=args.block_size, max_tokens=budget, timeout_sec=args.timeout),
            secrets=store.known_values,
            on_progress=lambda i, t, l: print(f"  [{i}/{t}] 抽取 {l}", flush=True),
            state_path=dirs["out"] / "_state.json",
            aliases=load_aliases(dirs["work_dir"] / ALIASES_BASENAME),
            limit=args.limit,
        )
    finally:
        if server is not None:
            server.__exit__(None, None, None)

    paths = save_entities(dirs["work_dir"], result)
    merged = result.get("merged") or {}
    print()
    print(f"实体：人物 {merged['counts']['characters']}  势力 {merged['counts']['factions']}  "
          f"能力 {merged['counts']['abilities']}  地点 {merged['counts']['locations']}  "
          f"关系 {merged['counts']['relations']}")
    if result.get("errors"):
        print(f"未完成 {len(result['errors'])} 块")
    salvaged = [b for b in result.get("blocks") or [] if b.get("salvaged")]
    if salvaged:
        print(
            f"有 {len(salvaged)} 块输出被截断，只抢救出截断前的部分实体（可能不完整）："
            f"{'、'.join(str(b.get('range')) for b in salvaged)}"
        )
    leftover = result.get("pending") or []
    if leftover:
        shown = "、".join(leftover[:5]) + ("…" if len(leftover) > 5 else "")
        print(f"还有 {len(leftover)} 块没跑（{shown}）。再跑一次会接着跑，已完成的块不会重复花钱。")
    print(f"实测 usage：{result.get('usage')}")
    print(f"已落盘：{paths['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
