#!/usr/bin/env python
"""批量标注调度命令行入口。

单章链路跑通之后，批量要解决的不是「怎么调模型」，而是四件事：

  1. 这次要跑哪些章（幂等：跑过且原文没变的跳过）
  2. 大概花多少（token 我能算，单价你填，没有单价就不给数字）
  3. 跑的时候怎么控（并发、预算闸门、失败不落盘）
  4. 中断之后怎么接（重跑只补缺的）

用法：

  # 先看计划。不调模型，不花钱
  python batch_cli.py --work 示例作品

  # 试跑 20 章（强烈建议先做这一步）
  python batch_cli.py --work 示例作品 --run --limit 20 --yes

  # 全量
  python batch_cli.py --work 示例作品 --run --yes

  # 本地模拟跑一遍调度逻辑，验证并发顺序与台账，不花钱
  python batch_cli.py --demo --run

  # 看最近一次跑到哪了
  python batch_cli.py --work 示例作品 --status

**不提供「一键跑完所有模型任务」。** 每次跑都要你主动发起，并且先看到范围与成本。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import AnnotateOptions  # noqa: E402
from workshop.batch import (  # noqa: E402
    BatchRunner,
    build_plan,
    format_plan,
    format_state,
    load_chapter_tasks,
    pick_model_id,
    resolve_annotation_options,
    resolve_concurrency,
)
from workshop.config import load_config  # noqa: E402
from workshop.ledger import Ledger  # noqa: E402
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.primitives import load_primitives, load_work  # noqa: E402
from workshop.samples import VALID_ANNOTATION, build_synthetic_novel  # noqa: E402
from workshop.secrets import SecretStore, setup_logging  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402

DEMO_WORK_NAME = "批量演示"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="batch_cli",
        description="批量标注调度：筛选、估算、并发、续跑",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=str(ROOT / "providers.yaml"))
    p.add_argument("--primitives", default=str(ROOT / "primitives.yaml"))
    p.add_argument(
        "--work",
        help="作品名。对应 workspaces/{作品名}/，产物各归各位",
    )
    p.add_argument("--work-config", default=None, help="作品配置，默认 workspaces/{作品名}/work.yaml")
    p.add_argument("--ingest-dir", default=None, help="录入目录，默认 workspaces/{作品名}/00-ingest")
    p.add_argument("--annotations-dir", default=None, help="标注目录，默认 workspaces/{作品名}/10-annotations")
    p.add_argument("--ledger", default=None, help="伏笔台账路径")
    p.add_argument("--runs-dir", default=None, help="运行记录目录，默认 {标注目录}/_runs")

    p.add_argument("--provider", help="服务商 id，默认取任务绑定")
    p.add_argument("--model", help="模型 id，默认取任务绑定")
    p.add_argument("--concurrency", type=int, default=None, help="并发数。默认 1（台账要求有序，见模块说明）")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 章待跑章节（试跑用）")
    p.add_argument("--max-tokens", type=int, default=2000)
    p.add_argument("--timeout", type=float, default=30.0)

    p.add_argument("--run", action="store_true", help="真的执行。不加则只出计划")
    p.add_argument("--yes", action="store_true", help="跳过交互确认（脚本里用）")
    p.add_argument(
        "--allow-over-budget",
        action="store_true",
        help="估算已超预算上限时仍然执行（会在运行中再次拦闸）",
    )
    p.add_argument("--no-ledger", action="store_true", help="不使用伏笔台账")
    p.add_argument("--status", action="store_true", help="只看最近一次运行状态")
    p.add_argument("--demo", action="store_true", help="本地模拟服务跑一遍，不花钱")
    return p


# ── 路径 ──────────────────────────────────────────────────


def _resolve_dirs(args) -> dict[str, Path]:
    if args.demo:
        base = ROOT / ".tmp" / "batch-demo"
        work_dir = base / "workspaces" / DEMO_WORK_NAME
    else:
        if not args.work:
            raise SystemExit("请用 --work 指定作品名，或用 --demo 跑本地演示。")
        work_dir = ROOT / "workspaces" / args.work
    ingest = Path(args.ingest_dir) if args.ingest_dir else work_dir / "00-ingest"
    annotations = Path(args.annotations_dir) if args.annotations_dir else work_dir / "10-annotations"
    ledger_path = (
        Path(args.ledger) if args.ledger else work_dir / "20-kb" / "k2-material" / "foreshadow-ledger.json"
    )
    work_config = Path(args.work_config) if args.work_config else work_dir / "work.yaml"
    runs_dir = Path(args.runs_dir) if args.runs_dir else annotations / "_runs"
    return {
        "work_dir": work_dir,
        "ingest": ingest,
        "annotations": annotations,
        "ledger": ledger_path,
        "work_config": work_config,
        "runs": runs_dir,
    }


def _ensure_work_config(path: Path, work_name: str, *, protagonist: str = "", motive: str = "") -> None:
    """作品配置缺失时补一份最小可用版本。

    不能拿根目录模板顶替——那里写的是别的作品的主角，套上去会让 P21
    度量错对象，而且这种错误不会报错，只会安静地产出一列错数据。
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"work: {work_name}",
                f'protagonist: "{protagonist}"',
                f'core_motive: "{motive}"',
                "",
                "# core_motive 决定 P21「主角核心动机呈现强度」在度量什么。",
                "# 留空时模型会自己猜一个动机来打分，那一列数据不可信。",
                "style_checks: []",
                "",
            ]
        ),
        encoding="utf-8",
    )


# ── 演示 ──────────────────────────────────────────────────


def _demo_responder(messages: list) -> dict:
    """按章序给不同答案：第 1 章埋设，之后推进第一章埋下的 F-001。

    这样能验证一件关键的事——**第 2 章的提示词里必须已经含 F-001**。
    若并发破坏了顺序，第 2 章就看不到它，台账会把它当成新的埋设再分配一个编号。
    """
    tail = " ".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
    payload = copy.deepcopy(VALID_ANNOTATION)
    if "F-001" in tail:
        payload["foreshadows"] = [
            {"动作": "推进", "编号": "F-001", "描述": "旧伤再次发作"},
        ]
    else:
        payload["foreshadows"] = [
            {"动作": "埋设", "编号": "", "描述": "墙缝里的旧刀痕"},
        ]
    return payload


def _prepare_demo(dirs: dict[str, Path]) -> None:
    """造一份合成作品并录入，作为批量调度的验证场。"""
    from workshop.ingest import ingest, save_ingest

    base = dirs["work_dir"].parent.parent
    if base.exists():
        import shutil

        shutil.rmtree(base, ignore_errors=True)
    source = base / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(build_synthetic_novel(), encoding="utf-8")

    result = ingest(source, work_name=DEMO_WORK_NAME)
    save_ingest(result, dirs["ingest"])
    _ensure_work_config(dirs["work_config"], DEMO_WORK_NAME, protagonist="演示主角", motive="活下去")


# ── 命令 ──────────────────────────────────────────────────


def cmd_status(dirs: dict[str, Path]) -> int:
    latest = dirs["runs"] / "latest.json"
    if not latest.exists():
        print("还没有跑过批量任务。")
        return 0
    try:
        data = json.loads(latest.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        print(f"读不了运行记录：{exc}", file=sys.stderr)
        return 1
    print(format_state(_dict_to_state(data)))
    return 0


def _dict_to_state(data: dict) -> "object":
    from workshop.batch import BatchState

    known = {k: v for k, v in data.items() if k in BatchState.__dataclass_fields__}
    return BatchState(**known)


def cmd_plan_or_run(args, dirs: dict[str, Path]) -> int:
    primitives = load_primitives(args.primitives)
    _ensure_work_config(dirs["work_config"], args.work or DEMO_WORK_NAME)
    work = load_work(dirs["work_config"])

    tasks = load_chapter_tasks(dirs["ingest"])
    if not tasks:
        print(f"在 {dirs['ingest']} 里没读到章节清单，先跑录入。", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    if args.demo:
        provider = cfg.get_provider("mock") or _mock_provider_config(cfg)
        model_id = args.model or "mock-flash"
    else:
        provider_id = args.provider or ((cfg.task_bindings or {}).get("chapter_annotation") or {}).get("provider")
        if not provider_id:
            print("配置里没有任务绑定，请用 --provider 指定服务商。", file=sys.stderr)
            return 2
        provider = cfg.get_provider(str(provider_id))
        if provider is None:
            print(f"配置里找不到服务商 {provider_id}", file=sys.stderr)
            return 2
        model_id = pick_model_id(cfg, provider, args.model)

    concurrency = resolve_concurrency(cfg, args.concurrency)
    plan = build_plan(
        cfg=cfg,
        provider=provider,
        model_id=model_id,
        primitives=primitives,
        work=work,
        tasks=tasks,
        annotations_dir=dirs["annotations"],
        concurrency=concurrency,
        ledger_enabled=not args.no_ledger,
        limit=args.limit,
    )
    print(format_plan(plan))
    if not args.run:
        print()
        print("这只是计划，没有发起任何调用。加 --run 才会真的跑。")
        return 0

    if plan.over_budget and not args.allow_over_budget:
        print()
        print("已停止：估算超过预算上限。要继续请加 --allow-over-budget，")
        print("或调小 --limit 分批跑，或调高配置里的预算上限。")
        return 1

    if not args.yes and not _confirm(plan):
        print("已取消。")
        return 0

    return _execute(args, dirs, cfg, provider, model_id, primitives, work, plan)


def _mock_provider_config(cfg):
    """演示模式下配置里没有 mock 服务商，就地造一个。"""
    from workshop.config import ProviderConfig

    return ProviderConfig(
        id="mock",
        raw={
            "id": "mock",
            "name": "本地模拟",
            "enabled": True,
            "base_url": "",
            "auth": {"scheme": "bearer", "api_key_ref": None},
            "models": [{"id": "mock-flash", "role": "main"}],
        },
    )


def _confirm(plan) -> bool:
    est = plan.estimate
    print()
    print(f"即将对 {len(plan.pending)} 章发起模型调用。")
    if est.cost_cny is None:
        print(f"预估 token 合计 {est.total_tokens:,}。{est.price_note}")
    else:
        print(f"预估 token 合计 {est.total_tokens:,}，费用 ≈ ¥{est.cost_cny}。")
    try:
        answer = input("确认开始？输入 y 继续：").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _execute(args, dirs, cfg, provider, model_id, primitives, work, plan) -> int:
    secrets_store = SecretStore(cfg.reports_dir.parent)
    setup_logging(secrets_store.known_values)

    if args.demo:
        state_holder: dict = {}
        mock_state = MockState(responder=_demo_responder, per_token_ms=0)
        server = MockServer(mock_state)
        server.__enter__()
        state_holder["server"] = server
        api_key = "mock-key"
        base_url = server.base_url
        pricing: dict = {}
    else:
        api_key = secrets_store.get(provider.api_key_ref) if provider.api_key_ref else ""
        if provider.api_key_ref and not api_key:
            print(f"读不到密钥，请设置环境变量 {provider.api_key_ref}", file=sys.stderr)
            return 2
        base_url = provider.base_url
        pricing = (provider.model(model_id) or {}).get("pricing") or {}
        state_holder = {}
        server = None
        if not provider.enabled:
            print(f"⚠️  配置里 {provider.id} 的 enabled 是 false，但仍按你的指令执行。")

    opts = resolve_annotation_options(
        cfg,
        AnnotateOptions(max_tokens=args.max_tokens, timeout_sec=args.timeout),
    )
    client = OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        timeout_sec=opts.timeout_sec,
        auth_scheme=provider.auth_scheme,
        secrets=secrets_store.known_values,
    )

    ledger = None
    if plan.ledger_enabled:
        ledger = Ledger.load(dirs["ledger"])
        ledger.work = work.name

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    dirs["runs"].mkdir(parents=True, exist_ok=True)
    state_path = dirs["runs"] / f"{run_id}.json"
    latest_path = dirs["runs"] / "latest.json"

    last_print = [0.0]

    def on_progress(state) -> None:
        now = datetime.now().timestamp()
        if now - last_print[0] < 1.0 and state.committed != state.total:
            return
        last_print[0] = now
        print(
            f"  {state.committed}/{state.total}  "
            f"正常 {state.ok}  待复核 {state.needs_review}  失败 {state.failed}"
            f"  已用 token {state.total_tokens:,}",
            flush=True,
        )

    runner = BatchRunner(
        plan=plan,
        client=client,
        primitives=primitives,
        work=work,
        opts=opts,
        annotations_dir=dirs["annotations"],
        ledger=ledger,
        ledger_path=dirs["ledger"] if ledger is not None else None,
        state_path=state_path,
        run_id=run_id,
        secrets=secrets_store.known_values,
        pricing=pricing,
        price_bucket_now=plan.estimate.price_bucket,
        on_progress=on_progress,
    )

    print()
    print(f"开始执行  run_id={run_id}  并发 {plan.concurrency}")
    print("-" * 62)
    try:
        state = runner.run()
    finally:
        if server is not None:
            server.__exit__(None, None, None)

    try:
        latest_path.write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass

    print()
    print(format_state(state))
    if ledger is not None:
        print()
        print(ledger.summary())
    print()
    print(f"运行记录：{state_path}")
    return 0 if state.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    dirs = _resolve_dirs(args)

    if args.demo:
        _prepare_demo(dirs)
        print(f"演示模式：本地模拟服务 + 合成作品（{dirs['work_dir']}），不产生真实调用与费用。")
        print()

    if args.status:
        return cmd_status(dirs)
    return cmd_plan_or_run(args, dirs)


if __name__ == "__main__":
    raise SystemExit(main())
