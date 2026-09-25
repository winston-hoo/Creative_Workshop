"""M9 创作台 · 规划层命令入口。

    python create_cli.py --new 关山灯 --genre 玄幻 --protagonist 李默 --core-motive "活着回去"
    python create_cli.py --check 关山灯                      # 校验设定集
    python create_cli.py --show 关山灯 [--inject 4000]        # 渲染设定集 / 看注入块
    python create_cli.py --sync 关山灯                       # 主角/动机同步进 work.yaml

    python create_cli.py --check-volume 关山灯 [--vol 1]      # 校验分卷目录
    python create_cli.py --show-volume 关山灯 [--vol 1]       # 渲染分卷目录

    python create_cli.py --seed-briefs 关山灯 [--vol 1]       # 按卷表播种逐章指令骨架
    python create_cli.py --check-brief 关山灯 [--chapter v001-c0001]
    python create_cli.py --show-brief 关山灯 --chapter v001-c0001

    python create_cli.py --chain 关山灯                       # 三层链路体检（先看这个）
    python create_cli.py --list                              # 列出所有原创工作区

本阶段**不调模型**：三层都是人写的真源，模型只是将来可选的起草手段。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from workshop import chapter_brief as cb  # noqa: E402
from workshop import volume as vo  # noqa: E402
from workshop.creation import (  # noqa: E402
    BRIEF_DIR,
    ORIGINAL_KIND,
    SETTING_BASENAME,
    SETTING_DIR,
    VOLUME_DIR,
    chain_report,
    create_original_work,
    load_setting,
    load_work_setting,
    render_injection_block,
    render_setting_markdown,
    seed_briefs,
    setting_path_of,
    sync_work_config,
    validate_setting,
)

WORKSPACES = ROOT / "workspaces"


def _workspaces(args: argparse.Namespace) -> Path:
    """工作区根目录。exe 模式下数据在 %LOCALAPPDATA%\\创作工坊，不是项目目录。"""
    return Path(args.root) if args.root else WORKSPACES


def _work_dir(args: argparse.Namespace, name: str) -> Path:
    return _workspaces(args) / name


def _report(name: str, errors: list[str], warnings: list[str], ok_line: str) -> int:
    if errors:
        print(f"\n阻断 {len(errors)} 项（不改就不能往下走）：")
        for item in errors[:40]:
            print(f"  ✗ {item}")
        if len(errors) > 40:
            print(f"  …还有 {len(errors) - 40} 项")
    if warnings:
        print(f"\n提示 {len(warnings)} 项（能往下走，但值得看一眼）：")
        for item in warnings[:20]:
            print(f"  · {item}")
        if len(warnings) > 20:
            print(f"  …还有 {len(warnings) - 20} 项")
    if not errors:
        print(f"\n{ok_line}")
    return 1 if errors else 0


# ── 设定集 ──────────────────────────────────────────────────


def cmd_new(args: argparse.Namespace) -> int:
    try:
        info = create_original_work(
            _workspaces(args),
            args.new,
            args.genre or "",
            logline=args.logline or "",
            protagonist=args.protagonist or "",
            core_motive=args.core_motive or "",
        )
    except FileExistsError as exc:
        print(f"建不了：{exc}", file=sys.stderr)
        return 2
    print(f"已建原创工作区：{info['work_dir']}")
    for key in (SETTING_DIR, VOLUME_DIR, BRIEF_DIR, "90-draft"):
        print(f"  {key}/")
    print(f"  work.yaml            kind={info['kind']}")
    print(f"  {SETTING_DIR}/{SETTING_BASENAME}")
    print(f"  {VOLUME_DIR}/{info['first_volume'].name}")
    print()
    print("接着做：① 填设定集 ② 填第一卷 ③ 播种逐章指令 ④ 体检")
    print(f"  python create_cli.py --check {args.new}")
    print(f"  python create_cli.py --check-volume {args.new} --vol 1")
    print(f"  python create_cli.py --seed-briefs {args.new}")
    print(f"  python create_cli.py --chain {args.new}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = setting_path_of(_work_dir(args, args.check))
    if not path.exists():
        print(f"找不到设定集：{path}", file=sys.stderr)
        return 2
    data = load_setting(path)
    errors, warnings = validate_setting(data)
    print(f"设定集 · {path}")
    print(f"  人物 {len(data.get('characters') or [])}　势力 {len(data.get('factions') or [])}"
          f"　术语 {len(data.get('terms') or [])}")
    return _report(args.check, errors, warnings, "设定集没问题。")


def cmd_show(args: argparse.Namespace) -> int:
    path = setting_path_of(_work_dir(args, args.show))
    if not path.exists():
        print(f"找不到设定集：{path}", file=sys.stderr)
        return 2
    data = load_setting(path)
    if args.inject:
        print(render_injection_block(data, max_chars=args.inject))
    else:
        print(render_setting_markdown(data), end="")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        result = sync_work_config(_work_dir(args, args.sync))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    flag = "已更新" if result["changed"] else "无变化"
    print(f"{flag}：protagonist={result['protagonist'] or '—'}  core_motive={result['core_motive'] or '—'}")
    return 0


# ── 分卷目录 ────────────────────────────────────────────────


def cmd_check_volume(args: argparse.Namespace) -> int:
    work_dir = _work_dir(args, args.check_volume)
    setting = load_work_setting(work_dir) if setting_path_of(work_dir).exists() else None
    volumes, broken = vo.load_all_volumes(work_dir / VOLUME_DIR)
    errors: list[str] = list(broken)
    warnings: list[str] = []

    if args.vol:
        if args.vol not in volumes:
            print(f"第 {args.vol} 卷没找到（现有：{sorted(volumes) or '无'}）", file=sys.stderr)
            return 2
        e, w = vo.validate_volume(volumes[args.vol], setting=setting, file_vol_no=args.vol)
        errors += e
        warnings += w
    else:
        for no in sorted(volumes):
            e, w = vo.validate_volume(volumes[no], setting=setting, file_vol_no=no)
            errors += [f"第{no}卷：{x}" for x in e]
            warnings += [f"第{no}卷：{x}" for x in w]
        e, w = vo.validate_chain(volumes, setting=setting) if volumes else (["一卷都还没填"], [])
        errors += e
        warnings += w

    total = sum(len((v.get("vol") or {}).get("chapters") or []) for v in volumes.values())
    print(f"分卷目录 · {work_dir / VOLUME_DIR}")
    print(f"  已填 {len(volumes)} 卷，共 {total} 章"
          + (f"（本次只看第 {args.vol} 卷）" if args.vol else ""))
    return _report(args.check_volume, errors, warnings, "分卷目录没问题。")


def cmd_show_volume(args: argparse.Namespace) -> int:
    work_dir = _work_dir(args, args.show_volume)
    volumes, broken = vo.load_all_volumes(work_dir / VOLUME_DIR)
    if broken:
        for item in broken:
            print(f"⚠️ {item}", file=sys.stderr)
    if args.vol:
        data = volumes.get(args.vol)
        if data is None:
            print(f"第 {args.vol} 卷没找到（现有：{sorted(volumes) or '无'}）", file=sys.stderr)
            return 2
        print(vo.render_volume_markdown(data), end="")
        return 0
    if not volumes:
        print("一卷都还没填。")
        return 0
    print(vo.render_volumes_markdown(volumes), end="")
    return 0


# ── 逐章创作任务指令 ────────────────────────────────────────


def cmd_seed_briefs(args: argparse.Namespace) -> int:
    # args.vol 默认是 0（「不限」），不能直接当卷号传下去——传 0 会把每一卷都跳过。
    result = seed_briefs(_work_dir(args, args.seed_briefs), vol_no=args.vol or None)
    print(f"播种逐章指令：新建 {len(result['created'])} 份，已存在跳过 {len(result['skipped'])} 份")
    for item in result["broken_volumes"]:
        print(f"  ⚠️ 卷文件读不了：{item}", file=sys.stderr)
    if result["created"]:
        print("  例如：" + "、".join(result["created"][:5]))
    print("  已有的文件一律不动——改过的指令不会被骨架覆盖。")
    return 1 if result["broken_volumes"] else 0


def cmd_check_brief(args: argparse.Namespace) -> int:
    work_dir = _work_dir(args, args.check_brief)
    setting = load_work_setting(work_dir) if setting_path_of(work_dir).exists() else None
    volumes, _broken = vo.load_all_volumes(work_dir / VOLUME_DIR)
    briefs, broken = cb.load_all_briefs(work_dir / BRIEF_DIR)
    errors: list[str] = list(broken)
    warnings: list[str] = []

    if args.chapter:
        if args.chapter not in briefs:
            print(f"没找到指令：{args.chapter}（现有 {len(briefs)} 份）", file=sys.stderr)
            return 2
        parsed = cb.parse_chapter_id(args.chapter)
        e, w = cb.validate_brief(
            briefs[args.chapter], setting=setting,
            volume=volumes.get(parsed[0]) if parsed else None,
        )
        errors += e
        warnings += w
    else:
        for cid in sorted(briefs):
            parsed = cb.parse_chapter_id(cid)
            e, w = cb.validate_brief(
                briefs[cid], setting=setting,
                volume=volumes.get(parsed[0]) if parsed else None,
            )
            errors += [f"{cid}：{x}" for x in e]
            warnings += [f"{cid}：{x}" for x in w]
        e, w = cb.validate_brief_chain(briefs) if briefs else ([], [])
        errors += e
        warnings += w

    print(f"创作任务指令 · {work_dir / BRIEF_DIR}")
    if not briefs:
        print("  一份都还没有——先跑 --seed-briefs 播种骨架，再逐份填。")
        return 0
    print(f"  已填 {len(briefs)} 份" + (f"（本次只看 {args.chapter}）" if args.chapter else ""))
    return _report(args.check_brief, errors, warnings, "创作任务指令没问题。")


def cmd_show_brief(args: argparse.Namespace) -> int:
    if not args.chapter:
        print("--show-brief 需要 --chapter", file=sys.stderr)
        return 2
    path = cb.brief_path(_work_dir(args, args.show_brief) / BRIEF_DIR, args.chapter)
    if not path.exists():
        print(f"没找到指令：{path}", file=sys.stderr)
        return 2
    print(cb.render_brief_markdown(cb.load_brief(path)), end="")
    return 0


# ── 链路与列表 ──────────────────────────────────────────────


def cmd_chain(args: argparse.Namespace) -> int:
    report = chain_report(_work_dir(args, args.chain))
    layers = report["layers"]
    s = layers["setting"]
    v = layers["volume"]
    b = layers["brief"]

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    def suffix(errors: list[str]) -> str:
        return f" · {len(errors)} 项阻断" if errors else ""

    print(f"三层链路 · {report['work']}")
    print(f"  ① 设定集      {mark(bool(s.get('done')))}  "
          f"人物 {s.get('characters', 0)}{suffix(s.get('errors') or [])}")
    print(f"  ② 分卷目录    {mark(v['exists'] and not v['errors'])}  "
          f"{v['volumes']} 卷 / 铺到第 {v['covered_chapters']} 章{suffix(v['errors'])}")
    print(f"  ③ 逐章指令    {mark(b['done'] and not b['errors'])}  "
          f"{b['done']} / {b['planned']} 章{suffix(b['errors'])}")
    return _report(args.chain, report["errors"], report["warnings"],
                   "三层链路通，可以开始写正文了。")


def cmd_list(args: argparse.Namespace) -> int:
    root = _workspaces(args)
    if not root.exists():
        print("还没有 workspaces 目录")
        return 0
    rows = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if not (entry / SETTING_DIR / SETTING_BASENAME).exists():
            continue
        report = chain_report(entry)
        layers = report["layers"]
        rows.append((
            entry.name,
            f"{layers['setting'].get('characters', 0)} 人",
            f"{layers['volume']['volumes']} 卷",
            f"{layers['brief']['done']}/{layers['brief']['planned']} 章",
            "通" if report["ok"] else f"{len(report['errors'])} 项阻断",
        ))
    if not rows:
        print("还没有原创工作区。用 --new 建一个。")
        return 0
    widths = [max(len(str(r[i])) for r in rows) for i in range(4)]
    header = ("工作区", "人物", "卷", "指令", "链路")
    print(f"{header[0].ljust(widths[0])}  {header[1].ljust(widths[1])}  "
          f"{header[2].ljust(widths[2])}  {header[3].ljust(widths[3])}  链路")
    for r in rows:
        print(f"{r[0].ljust(widths[0])}  {r[1].ljust(widths[1])}  "
              f"{r[2].ljust(widths[2])}  {r[3].ljust(widths[3])}  {r[4]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="create_cli",
        description="M9 创作台 · 规划层：原创工作区 / 设定集 / 分卷目录 / 逐章创作任务指令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--new", metavar="书名", help="新建原创工作区")
    g.add_argument("--check", metavar="书名", help="校验设定集")
    g.add_argument("--show", metavar="书名", help="渲染设定集")
    g.add_argument("--sync", metavar="书名", help="把设定集的主角/动机同步进 work.yaml")
    g.add_argument("--check-volume", dest="check_volume", metavar="书名", help="校验分卷目录")
    g.add_argument("--show-volume", dest="show_volume", metavar="书名", help="渲染分卷目录")
    g.add_argument("--seed-briefs", dest="seed_briefs", metavar="书名", help="按卷表播种逐章指令骨架")
    g.add_argument("--check-brief", dest="check_brief", metavar="书名", help="校验逐章创作任务指令")
    g.add_argument("--show-brief", dest="show_brief", metavar="书名", help="渲染一份创作任务指令")
    g.add_argument("--chain", metavar="书名", help="三层链路体检")
    g.add_argument("--list", action="store_true", help="列出所有原创工作区")

    p.add_argument("--genre", default="", help="题材")
    p.add_argument("--logline", default="", help="一句话前提")
    p.add_argument("--protagonist", default="", help="主角名")
    p.add_argument("--core-motive", dest="core_motive", default="", help="主角核心动机")
    p.add_argument("--vol", type=int, default=0, help="只处理某一卷")
    p.add_argument("--chapter", default="", metavar="v001-c0001", help="只处理某一章")
    p.add_argument("--root", default=None, help="工作区根目录，默认项目下的 workspaces/")
    p.add_argument("--inject", type=int, default=0, metavar="字数上限",
                   help="--show 时改为打印注入块（生成时真正喂进去的那段）")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    if args.new:
        return cmd_new(args)
    if args.check:
        return cmd_check(args)
    if args.show:
        return cmd_show(args)
    if args.sync:
        return cmd_sync(args)
    if args.check_volume:
        return cmd_check_volume(args)
    if args.show_volume:
        return cmd_show_volume(args)
    if args.seed_briefs:
        return cmd_seed_briefs(args)
    if args.check_brief:
        return cmd_check_brief(args)
    if args.show_brief:
        return cmd_show_brief(args)
    if args.chain:
        return cmd_chain(args)
    return cmd_list(args)


if __name__ == "__main__":
    raise SystemExit(main())
