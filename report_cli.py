#!/usr/bin/env python
"""结构体检报告命令行入口。

纯本地聚合，不调模型：读录入清单、已落盘的标注、伏笔台账，算出一本报告。

  python report_cli.py --work 示例作品            # 出报告并落盘
  python report_cli.py --work 示例作品 --stdout    # 只打印，不落盘
  python report_cli.py --demo                            # 用演示数据看一次

**零标注也能出报告**：章节长度、分卷统计、风格违规这几节来自脚本轨，
不需要模型。模型轨小节在没数据时会明写「无数据」，不会用空图假装正常。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.analysis import build_report, render_markdown, save_report  # noqa: E402
from workshop.batch import load_chapter_tasks  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="report_cli",
        description="结构体检报告：把标注数据聚成能看出问题的报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--work", help="作品名，对应 workspaces/{作品名}/")
    p.add_argument("--ingest-dir", default=None)
    p.add_argument("--annotations-dir", default=None)
    p.add_argument("--ledger", default=None)
    p.add_argument("--out", default=None, help="报告目录，默认 workspaces/{作品名}/30-reports")
    p.add_argument("--stale-threshold", type=int, default=30, help="多少章未推进算疑似断点")
    p.add_argument("--stdout", action="store_true", help="只打印 Markdown，不落盘")
    p.add_argument("--json", action="store_true", help="打印 JSON 而不是 Markdown")
    p.add_argument("--demo", action="store_true", help="用 .tmp/batch-demo 里的演示数据")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.demo:
        work_dir = ROOT / ".tmp" / "batch-demo" / "workspaces" / "批量演示"
        work_name = "批量演示"
    else:
        if not args.work:
            print("请用 --work 指定作品名，或用 --demo。", file=sys.stderr)
            return 2
        work_dir = ROOT / "workspaces" / args.work
        work_name = args.work

    ingest = Path(args.ingest_dir) if args.ingest_dir else work_dir / "00-ingest"
    annotations = Path(args.annotations_dir) if args.annotations_dir else work_dir / "10-annotations"
    ledger = Path(args.ledger) if args.ledger else work_dir / "20-kb" / "k2-material" / "foreshadow-ledger.json"
    out_dir = Path(args.out) if args.out else work_dir / "30-reports"

    if not load_chapter_tasks(ingest):
        print(f"在 {ingest} 里没有读到章节清单。请先导入作品。", file=sys.stderr)
        return 2

    report = build_report(
        work_name=work_name,
        ingest_dir=ingest,
        annotations_dir=annotations,
        ledger_path=ledger,
        stale_threshold=args.stale_threshold,
    )

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0

    markdown = render_markdown(report)
    print(markdown)
    if not args.stdout:
        paths = save_report(report, out_dir)
        print()
        print(f"已落盘：{paths['markdown']}")
        print(f"        {paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
