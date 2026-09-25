#!/usr/bin/env python
"""技法库检查：内置 12 篇齐不齐、外部包是不是真能覆盖内置、预算会不会砍掉该给的篇目。

    python tests/test_craft.py

只查三件容易静默坏掉的事：
① 内置篇目缺了文件 —— 程序不报错，只是那篇技法静默不给（作者会以为模型读过）；
② 外部覆盖没生效 —— 放进去的包没被读到，还以为接上了；
③ 预算不够 —— 「always」的篇目被静默砍掉，提示里只有一句「超预算」。

本机可能放着外部技法包（`craft/`，见 .gitignore）。检验内置篇目时一律把数据根指到空目录，
不然量的是外部包，内置版坏了也看不出来。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop import craft  # noqa: E402

# 临时目录放仓库的 .tmp/（已在 .gitignore 里），不放系统 %TEMP%——仓库里其余测试同一个规矩。
WORK = ROOT / ".tmp" / "craft-test"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


@contextmanager
def data_root(path: Path):
    """把数据根指到别处。`craft/` 落在哪个根下，技法库就优先读哪一份。"""
    previous = os.environ.get("WORKSHOP_ROOT")
    os.environ["WORKSHOP_ROOT"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WORKSHOP_ROOT", None)
        else:
            os.environ["WORKSHOP_ROOT"] = previous


@contextmanager
def builtin_only():
    """数据根下没有 craft/ —— 读到的一定是内置那 12 篇。"""
    empty = WORK / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    with data_root(empty):
        yield


def test_builtin_complete() -> None:
    """内置篇目必须齐：少一篇没人知道，只是这一章少一段参考。"""
    print("内置技法库完整")
    with builtin_only():
        names = craft.available()
        missing = [n for n in craft.CATALOG if n not in names]
        check(not missing, f"CATALOG 里每篇都有文件（缺：{'、'.join(missing) or '无'}）")
        check(len(craft.CATALOG) == 12, f"CATALOG 是 12 篇（实际 {len(craft.CATALOG)}）")
        for name in names:
            check(len(craft.read(name).strip()) > 200, f"{name}.md 不是空壳")


def test_external_overrides_builtin() -> None:
    """数据根下的 craft/ 优先于内置，删掉就退回内置。"""
    print("外部包覆盖内置")
    root = WORK / "external"
    external = root / "craft"
    external.mkdir(parents=True, exist_ok=True)
    for stale in external.glob("*.md"):
        stale.unlink()
    (external / "chapter-guide.md").write_text("外部包的内容", encoding="utf-8")
    (external / "not-in-catalog.md").write_text("不该被认", encoding="utf-8")

    with data_root(root):
        check(craft.read("chapter-guide") == "外部包的内容", "同名篇目读的是外部包")
        check("not-in-catalog" not in craft.available(), "CATALOG 之外的篇目不进库")
        # 外部包没有的篇目仍然走内置，不是整库失效
        check(len(craft.read("hook-techniques").strip()) > 200, "外部包缺的篇目回落内置")

        (external / "chapter-guide.md").unlink()
        check(len(craft.read("chapter-guide").strip()) > 200, "外部包删掉后回落内置")


def test_pick_and_render() -> None:
    """挑选与预算：审稿只吃审查两篇，超预算的必须点名。"""
    print("挑选与预算")
    with builtin_only():
        names, _dropped = craft.pick(chapter_no=1)
        always = {n for n in craft.CATALOG if craft.CATALOG[n].get("always")}
        check(always <= set(names), "always 的四篇都在")
        check("golden-opening" in names, "第 1 章带上黄金开篇")
        check("review-dimensions" not in names, "生成时不给审查维度")

        review_names, _ = craft.pick(chapter_no=1, review=True)
        check(set(review_names) == {"quality-checklist", "review-dimensions"},
              f"审稿只给审查两篇（实际 {review_names}）")

        out = craft.render(["chapter-guide", "hook-techniques"], budget=100)
        check("超出 100 字预算" in out, "超预算时说明超了多少")
        check(craft.CATALOG["hook-techniques"]["label"] in out, "没给的篇目被点名")

        # 最长的一次选择必须整整齐齐装得下：不然「always」的篇目会被静默砍掉，
        # 而作者只看到一句超预算提示，不会知道少的是每章都该给的那一篇。
        longest = ["golden-opening", "chapter-guide", "emotion-curve", "hook-techniques",
                   "plot-structures", "dialogue-writing", "continuity"]
        full = craft.render(longest)
        check("超出" not in full, f"最长一次选择不超过默认预算（实际 {len(full)} 字）")
        check(all(craft.read(n).strip() in full for n in longest), "最长一次选择一篇都没少")


def main() -> int:
    print("=" * 58)
    for fn in (test_builtin_complete, test_external_overrides_builtin, test_pick_and_render):
        fn()
        print()

    print("=" * 58)
    if _failures:
        print(f"未通过 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
