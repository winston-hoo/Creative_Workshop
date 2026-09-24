#!/usr/bin/env python
"""单章标注命令行入口。

用法示例：

  # 演示：用本地模拟服务跑通整条链路，不需要密钥、不花钱
  python annotate_cli.py --demo

  # 只算脚本轨字段，完全不调模型（免费）
  python annotate_cli.py --chapter-file chapters/v001-c0001.md --script-only

  # 真实标注一章
  python annotate_cli.py --chapter-file chapters/v001-c0001.md --provider deepseek

  # 带上上一章的标注，让模型判断连贯性
  python annotate_cli.py --chapter-file chapters/v001-c0002.md \
      --prev annotations/v001-c0001.json --provider deepseek

  # 演示「模型没按要求输出 JSON」时会发生什么
  python annotate_cli.py --demo --bad-output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import (  # noqa: E402
    AnnotateOptions,
    annotate_chapter,
    is_up_to_date,
    load_annotation,
    save_annotation,
    strip_front_matter,
    text_sha256,
)
from workshop.config import load_config, resolve_default_model  # noqa: E402
from workshop.ledger import Ledger  # noqa: E402
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.primitives import load_primitives, load_work  # noqa: E402
from workshop.samples import BAD_MODEL_OUTPUT, INVALID_ENUM_ANNOTATION, SAMPLE_CHAPTER  # noqa: E402
from workshop.script_fields import compute_script_fields  # noqa: E402
from workshop.secrets import SecretStore, setup_logging  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="annotate_cli",
        description="单章标注：结构原语的表转化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=str(ROOT / "providers.yaml"), help="服务商配置")
    p.add_argument(
        "--work-config",
        "--work",
        dest="work_config",
        default=str(ROOT / "work.yaml"),
        help="作品配置。每本作品一份，应在 workspaces/{作品名}/work.yaml",
    )
    p.add_argument("--primitives", default=str(ROOT / "primitives.yaml"), help="原语定义")

    p.add_argument("--chapter-file", help="章节文件路径")
    p.add_argument("--chapter-no", type=int, default=None, help="章序，默认取 front-matter 或 1")
    p.add_argument("--vol-no", type=int, default=None, help="卷序，默认取 front-matter 或 1")
    p.add_argument("--title", default=None, help="章节标题，默认取 front-matter")
    p.add_argument("--prev", default=None, help="上一章标注 JSON 路径，用于连贯性判断")

    p.add_argument(
        "--annotations-dir",
        default=None,
        help="标注输出目录。默认放到作品自己的工作区：{作品目录}/10-annotations",
    )
    p.add_argument(
        "--archive-dir", default=None, help="重跑时旧标注的归档目录"
    )
    p.add_argument(
        "--ledger",
        default=None,
        help="伏笔台账路径。默认 {作品目录}/20-kb/k2-material/foreshadow-ledger.json",
    )
    p.add_argument(
        "--no-ledger",
        action="store_true",
        help="本次不使用伏笔台账（编号由模型自填，跨章会不一致，一般不要用）",
    )
    p.add_argument("--provider", help="服务商 id")
    p.add_argument("--model", help="模型 id")
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--max-tokens", type=int, default=2000)
    p.add_argument("--max-attempts", type=int, default=3, help="含首次在内最多尝试几次")
    p.add_argument("--timeout", type=float, default=30.0)

    p.add_argument("--script-only", action="store_true", help="只算脚本轨字段，不调模型")
    p.add_argument("--force", action="store_true", help="忽略幂等，强制重跑")
    p.add_argument("--no-save", action="store_true", help="只打印，不落盘")
    p.add_argument("--demo", action="store_true", help="用本地模拟服务跑演示")
    p.add_argument("--bad-output", action="store_true", help="演示模型返回非 JSON 时的处理")
    p.add_argument("--invalid-output", action="store_true", help="演示模型返回越界取值时的处理")
    return p


def _resolve_chapter_meta(args, meta: dict) -> tuple[int, int, str]:
    chapter_no = args.chapter_no or int(meta.get("chapter_no") or meta.get("章序") or 1)
    vol_no = args.vol_no or int(meta.get("vol_no") or meta.get("卷序") or 1)
    title = args.title or str(meta.get("title") or meta.get("标题") or "")
    return chapter_no, vol_no, title


def cmd_script_only(args) -> int:
    primitives = load_primitives(args.primitives)
    work = load_work(args.work_config)
    raw = Path(args.chapter_file).read_text(encoding="utf-8")
    meta, body = strip_front_matter(raw)
    chapter_no, vol_no, title = _resolve_chapter_meta(args, meta)

    fields, issues = compute_script_fields(
        body, vol_no=vol_no, chapter_no=chapter_no, work=work, primitives=primitives
    )

    print("脚本轨字段（零模型成本）")
    print("=" * 50)
    for key, value in fields.items():
        if key == "style_violations":
            continue
        print(f"{key:24} {value}")
    violations = fields.get("style_violations") or []
    print()
    print(f"风格违规：{len(violations)} 类")
    for item in violations:
        print(f"  · {item['rule_id']} {item['desc']}（{item['count']} 处）")
        for sample in item.get("samples") or []:
            excerpt = sample.get("excerpt") or sample.get("matched") or ""
            print(f"      offset {sample.get('offset')}  {excerpt}")
    if issues:
        print()
        print("提示：")
        for item in issues:
            print(f"  · {item}")
    return 0


def cmd_demo(args) -> int:
    if args.bad_output:
        state = MockState(annotation_raw_text=BAD_MODEL_OUTPUT, per_token_ms=0)
        label = "模型返回非 JSON"
    elif args.invalid_output:
        state = MockState(annotation_payload=INVALID_ENUM_ANNOTATION, per_token_ms=0)
        label = "模型返回值越界"
    else:
        from workshop.samples import VALID_ANNOTATION

        state = MockState(annotation_payload=VALID_ANNOTATION, per_token_ms=0)
        label = "正常返回"

    primitives = load_primitives(args.primitives)
    work = load_work(args.work_config)
    opts = AnnotateOptions(
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        max_attempts=args.max_attempts,
        timeout_sec=15,
    )

    print(f"演示模式：本地模拟服务，场景「{label}」，不产生真实调用与费用。")
    print()

    with MockServer(state) as server:
        client = OpenAICompatProvider(
            base_url=server.base_url, api_key="mock-key", timeout_sec=15
        )
        result = annotate_chapter(
            client=client,
            primitives=primitives,
            work=work,
            chapter_id="demo-c0001",
            chapter_no=1,
            vol_no=1,
            title="夜行（演示）",
            text=SAMPLE_CHAPTER,
            provider_id="mock",
            model_id="mock-flash",
            opts=opts,
        )

    _print_result(result)
    return 0 if result.record["status"] == "ok" or args.bad_output or args.invalid_output else 1


def cmd_annotate(args) -> int:
    if not args.chapter_file:
        print("请用 --chapter-file 指定章节文件。", file=sys.stderr)
        return 2

    primitives = load_primitives(args.primitives)
    work = load_work(args.work_config)
    if not work.core_motive:
        # 这条不是随便提醒：core_motive 决定 P21 在度量什么。
        # 提示词会让模型对 motive_strength 显式填 null（而不是猜一个分数），
        # 所以跑是能跑的，但这一列会是空列——想让它有数据，先填这里。
        print("⚠️  作品配置里 core_motive 为空。")
        print("   P21「主角核心动机呈现强度」的度量对象就是它。")
        print("   本轮标注里 motive_strength 会一律为 null（模型被显式禁止猜动机）。")
        print(f"   想让这一列有数据，先在 {Path(args.work_config).name} 里填好再跑。")
        print()
    raw = Path(args.chapter_file).read_text(encoding="utf-8")
    meta, body = strip_front_matter(raw)
    chapter_no, vol_no, title = _resolve_chapter_meta(args, meta)
    chapter_id = str(meta.get("id") or f"v{vol_no:03d}-c{chapter_no:04d}")

    # 产物归属作品自己的工作区，而不是全局 config 目录。
    # 否则接入第二本书之后，两本书的标注会混在一个目录里。
    work_dir = Path(args.work_config).resolve().parent
    annotations_dir = Path(args.annotations_dir) if args.annotations_dir else work_dir / "10-annotations"
    archive_dir = (
        Path(args.archive_dir) if args.archive_dir else annotations_dir / "_archive"
    )
    ledger_path = (
        Path(args.ledger)
        if args.ledger
        else work_dir / "20-kb" / "k2-material" / "foreshadow-ledger.json"
    )

    target = annotations_dir / f"{chapter_id}.json"
    if not args.force and is_up_to_date(target, text_sha256(body)):
        print(f"跳过：{chapter_id} 已有标注且原文未变（幂等生效）。")
        print(f"       如需强制重跑，加 --force。文件：{target}")
        return 0

    cfg = load_config(args.config)
    if not args.provider:
        print("请用 --provider 指定服务商（或用 --demo 跑模拟）。", file=sys.stderr)
        return 2
    provider = cfg.get_provider(args.provider)
    if provider is None:
        print(f"配置里找不到服务商 {args.provider}", file=sys.stderr)
        return 2
    model_id = args.model or resolve_default_model(cfg, provider)

    secrets = SecretStore(cfg.reports_dir.parent)
    setup_logging(secrets.known_values)
    api_key = secrets.get(provider.api_key_ref)
    if provider.api_key_ref and not api_key:
        print(f"读不到密钥，请设置环境变量 {provider.api_key_ref}", file=sys.stderr)
        return 2

    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    opts = AnnotateOptions(
        temperature=args.temperature,
        thinking=str(binding.get("thinking") or "disabled"),
        max_tokens=args.max_tokens,
        max_attempts=args.max_attempts,
        timeout_sec=args.timeout,
    )

    prev_summary = load_annotation(args.prev) if args.prev else None
    if prev_summary:
        prev_summary = prev_summary.get("fields") or {}

    ledger: Ledger | None = None
    if not args.no_ledger:
        ledger = Ledger.load(ledger_path)
        ledger.work = work.name
        print(f"伏笔台账：{ledger_path.relative_to(ROOT) if ledger_path.is_relative_to(ROOT) else ledger_path}")
        print(f"  当前未回收 {len(ledger.open_items)} 条 / 共 {len(ledger.items)} 条")
    else:
        print("伏笔台账：本次未启用（编号由模型自填，跨章会不一致）")
    print()

    print(f"标注 {chapter_id}  ·  {provider.name} / {model_id}")
    print(f"温度 {opts.temperature}  思考模式 {opts.thinking}  最多尝试 {opts.max_attempts} 次")
    print()

    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=opts.timeout_sec,
        auth_scheme=provider.auth_scheme,
        secrets=secrets.known_values,
    )

    result = annotate_chapter(
        client=client,
        primitives=primitives,
        work=work,
        chapter_id=chapter_id,
        chapter_no=chapter_no,
        vol_no=vol_no,
        title=title,
        text=body,
        provider_id=provider.id,
        model_id=model_id,
        opts=opts,
        source_file=str(args.chapter_file),
        prev_summary=prev_summary,
        ledger=ledger,
    )

    if not args.no_save:
        saved = save_annotation(
            result.record,
            annotations_dir,
            secrets=secrets.known_values,
            archive_dir=archive_dir,
        )
        result.saved_path = saved
        # 台账在标注成功落盘之后才保存。顺序反了的话，
        # 一次落盘失败就会留下「编号已分配但标注不存在」的幽灵条目。
        if ledger is not None:
            ledger.save(ledger_path)
            result.saved_path = saved

    _print_result(result)
    return 0 if result.record["status"] == "ok" else 1


def _print_result(result) -> None:
    record = result.record
    fields = record["fields"]
    prov = record["provenance"]

    print("=" * 58)
    print(f"标注结果  {record['chapter_id']}  第{record['chapter_no']}章 {record['title']}")
    print("=" * 58)
    print(f"状态          {record['status']}    复核动作 {record['review_action']}")
    print(f"尝试次数      {result.attempts}")
    print(f"延迟          {prov.get('latency_ms')} ms")
    print(
        f"字段填充      {prov.get('model_fields_filled')}/{prov.get('model_fields_total')}"
        f"（模型轨）"
    )
    print(f"前缀指纹      {prov.get('prefix_hash')}   原语指纹 {prov.get('primitives_hash')}")
    print()

    print("── 脚本轨 ──────────────────────────────────")
    for key in (
        "char_count",
        "para_count",
        "avg_para_chars",
        "dialogue_ratio",
        "perspective_detected",
    ):
        print(f"{key:24} {fields.get(key)}")
    violations = fields.get("style_violations") or []
    print(f"{'style_violations':24} {len(violations)} 类")
    for item in violations:
        print(f"{'':24} · {item['rule_id']} {item['desc']}（{item['count']} 处）")

    print()
    print("── 模型轨 ──────────────────────────────────")
    for key, value in fields.items():
        if key in (
            "vol_no",
            "chapter_no",
            "char_count",
            "para_count",
            "avg_para_chars",
            "dialogue_ratio",
            "perspective_detected",
            "style_violations",
        ):
            continue
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
        print(f"{key:24} {rendered}")

    report = record.get("self_report") or {}
    print()
    print(f"自评置信度    {report.get('confidence')}")
    if report.get("uncertain_fields"):
        print(f"存疑字段      {', '.join(str(x) for x in report['uncertain_fields'])}")

    led = record.get("foreshadow_ledger")
    if led:
        print()
        print("── 伏笔台账动作 ────────────────────────────")
        if led.get("added"):
            print(f"新埋设（编号由台账分配）  {', '.join(led['added'])}")
        if led.get("advanced"):
            print(f"推进                      {', '.join(led['advanced'])}")
        if led.get("recovered"):
            print(f"回收                      {', '.join(led['recovered'])}")
        if led.get("ignored"):
            for item in led["ignored"]:
                print(f"忽略                      {item.get('id')}（{item.get('reason')}）")
        if led.get("anomalies"):
            print("异常：")
            for item in led["anomalies"]:
                print(f"  · {item}")

    if record.get("issues"):
        print()
        print("── 提示 ────────────────────────────────────")
        for item in record["issues"]:
            print(f"  · {item}")

    if record.get("validation_issues"):
        print()
        print("── 校验问题 ────────────────────────────────")
        for item in record["validation_issues"]:
            print(f"  · {item}")

    if record.get("errors"):
        print()
        print("── 错误 ────────────────────────────────────")
        for item in record["errors"]:
            print(f"  · 第{item.get('attempt')}次 {item.get('kind')}：{item.get('detail')}")

    if result.saved_path:
        print()
        print(f"已落盘：{result.saved_path}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.demo:
        return cmd_demo(args)
    if args.script_only:
        return cmd_script_only(args)
    return cmd_annotate(args)


if __name__ == "__main__":
    raise SystemExit(main())
