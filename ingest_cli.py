#!/usr/bin/env python
"""作品录入命令行入口。

用法示例：

  # 演示：用合成文件跑一遍切分与清洗，不涉及任何真实作品
  python ingest_cli.py --demo

  # 预览：只切分不落盘。导入流程的第 2 步必须让人看到这个再确认
  python ingest_cli.py --file 关山灯.txt --preview

  # 正式录入
  python ingest_cli.py --file 关山灯.txt --out workspaces/关山灯/00-ingest --work 关山灯

  # 打开页眉页脚删除（默认只报告不删）
  python ingest_cli.py --file 关山灯.txt --out ... --strip-repeated
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.ingest import format_preview, ingest, save_ingest  # noqa: E402
from workshop.samples import build_synthetic_novel  # noqa: E402


def load_extra_patterns(work_config_path: str | None) -> list[str]:
    """从 work.yaml 读这本书特有的章节标题写法。

    读不到就返回空列表——内置模板能满足大多数作品；
    读到了但格式不对则显式报错，不静默忽略。
    """
    if not work_config_path:
        return []
    path = Path(work_config_path)
    if not path.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"警告：{path} 读取失败，本次只用内置模板（{exc}）", file=sys.stderr)
        return []
    patterns = ((data.get("ingest") or {}).get("chapter_patterns")) or []
    return [str(p) for p in patterns if str(p).strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest_cli",
        description="作品录入：切分、清洗、完整性核验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--file", help="作品文件路径（txt）")
    p.add_argument("--out", help="输出目录；省略则只预览不落盘")
    p.add_argument("--work", default="", help="作品名，默认取文件名")
    p.add_argument(
        "--work-config",
        default=str(ROOT / "work.yaml"),
        help="作品配置路径，用于读取这本书特有的章节标题写法",
    )
    p.add_argument("--preview", action="store_true", help="只预览切分结果，不落盘")
    p.add_argument("--preview-limit", type=int, default=20, help="预览里列出多少章")
    p.add_argument(
        "--strip-repeated",
        action="store_true",
        help="删除反复出现的短行（页眉页脚类）。默认只报告不删",
    )
    p.add_argument(
        "--strip-ads",
        action="store_true",
        help="删除盗版站广告与引流行。默认只报告不删",
    )
    p.add_argument("--demo", action="store_true", help="用合成文件跑演示")
    return p


def run(args: argparse.Namespace) -> int:
    if args.demo:
        print("演示模式：使用合成文件，不涉及任何真实作品。")
        print()
        tmp_dir = Path(tempfile.mkdtemp(prefix="ingest-demo-"))
        source = tmp_dir / "夜行录.txt"
        source.write_text(build_synthetic_novel(), encoding="utf-8")
        out_dir = tmp_dir / "00-ingest"
        work_name = args.work or "演示作品"
    else:
        if not args.file:
            print("请用 --file 指定作品文件，或用 --demo 跑演示。", file=sys.stderr)
            return 2
        source = Path(args.file)
        if not source.exists():
            print(f"找不到文件：{source}", file=sys.stderr)
            return 2
        out_dir = Path(args.out) if args.out else None
        work_name = args.work

    extra_patterns = load_extra_patterns(args.work_config)
    if extra_patterns:
        print(f"已加载 {len(extra_patterns)} 条作品自定义章节模板（来自 {Path(args.work_config).name}）")
        print()

    result = ingest(
        source,
        work_name=work_name,
        strip_repeated=args.strip_repeated,
        strip_ads=args.strip_ads,
        extra_patterns=extra_patterns,
    )
    print(format_preview(result, limit=args.preview_limit))

    integrity = result.get("integrity") or {}
    should_save = out_dir is not None and not args.preview

    if should_save:
        paths = save_ingest(result, out_dir)
        print()
        print("── 已落盘 ──────────────────────────────────")
        print(f"章节文件      {paths['chapters_dir']}")
        print(f"清洗前原文    {paths['raw_dir']}")
        print(f"清单          {paths['manifest']}")
        print(f"清洗日志      {paths['cleaning_log']}")
        if "review" in paths:
            print(f"复核队列      {paths['review']}")
        print()
        print(f"下一步：用 python annotate_cli.py --chapter-file "
              f"{paths['chapters_dir'] / 'v001-c0001.md'} --provider deepseek 标注第一章")
    elif args.preview:
        print()
        print("（预览模式，未落盘。确认无误后加 --out 指定输出目录）")

    return 0 if integrity.get("passed") else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
