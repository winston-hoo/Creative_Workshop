"""上下文预算账本自检（P0 批次 1：C1/C2/A4/C3/C4）。

跑：python tests/test_context_budget.py

这里查的是「喂了多少、裁了哪块、为什么裁、账对不对」——
这四件事以前散在各个 render_* 函数里，超了就 break，作者看不到。
现在它们必须是一份可序列化、可落盘、可被界面直接画的报告。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.chapter_brief import chapter_id_of  # noqa: E402
from workshop.context_budget import (  # noqa: E402
    ContextBlock,
    ContextBudgetError,
    build_context,
    load_manifest,
    manifest_path,
    save_manifest,
)
from workshop.creation import render_recap, render_write_brief_report  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def _setting() -> dict:
    return {
        "schema_version": "setting-v1",
        "work": "关山灯",
        "genre": "玄幻",
        "logline": "一个能看到数值的废柴，靠吐槽在修真界活下来。",
        "core_motive": "活着回去",
        "target": {"chapters": 12, "chars_per_chapter": [2800, 4000]},
        "style": {"perspective": "第三限知", "tone": "轻松搞笑", "taboo": ["不用破折号"]},
        "world": {"era": "天元三百年", "places": [{"name": "青云宗", "note": "第一大宗"}],
                  "rules": ["灵力用尽会昏迷"]},
        "power": {"system": "修为", "tiers": ["炼气", "筑基", "金丹"]},
        "factions": [{"name": "青云宗", "stance": "表面中立", "leader": "玄阳真人"}],
        "characters": [
            {"name": "李默", "role": "主角", "identity": "穿越者", "personality": "吐槽役",
             "motive": "活着回去", "arc": "从自保到扛事", "abilities": ["数值之眼"],
             "faction": "青云宗", "relations": [{"to": "苏清月", "type": "师姐"}]},
            {"name": "苏清月", "role": "重要配角", "identity": "大师姐", "motive": "守住宗门",
             "faction": "青云宗"},
        ],
        "terms": [{"term": "数值之眼", "meaning": "能看到目标的修为数值"}],
        "themes": ["活着的意义"],
    }


def _volume(vol_no: int = 1, start: int = 1, end: int = 4) -> dict:
    return {
        "schema_version": "volume-v1",
        "work": "关山灯",
        "vol": {
            "vol": vol_no, "title": f"第{vol_no}卷卷名", "start_chapter": start,
            "end_chapter": end, "era": "天元三百年秋", "core_conflict": "自保 vs 扛事",
            "goal": "主角活下来并找到线索", "closing": "卷末总结：主角站住了脚",
            "parts": [{"title": "觉醒", "start_chapter": start, "end_chapter": end,
                       "gist": "主角认清处境并迈出第一步",
                       "must_complete": ["确认数值之眼不是幻觉"],
                       "ending_demand": "落在第一次主动选择上"}],
            "chapters": [
                {"chapter_no": no, "title": f"第{no}章标题", "gist": f"第{no}章发生了什么",
                 "characters": ["李默"]}
                for no in range(start, end + 1)
            ],
        },
    }


def _brief(chapter_no: int, *, prev_tail=None, future_count=None) -> dict:
    data = {
        "schema_version": "brief-v1",
        "work": "关山灯",
        "chapter_id": chapter_id_of(1, chapter_no),
        "vol": 1,
        "chapter_no": chapter_no,
        "title": f"第{chapter_no}章标题",
        "one_line": f"第{chapter_no}章只干一件事",
        "core_plot": [f"第{chapter_no}章的第一件事", "第二件事"],
        "timeline": "接上一章，隔一天",
        "word_target": [2800, 4000],
        "narrative": {"perspective": "第三限知", "tone": "轻松搞笑", "focus": ["对话"]},
        "settings": {"characters": ["李默"], "factions": ["青云宗"], "terms": ["数值之眼"]},
        "foreshadow": [],
        "carry_over": [],
        "must_not": [],
    }
    if prev_tail is not None:
        data["prev_tail_chars"] = prev_tail
    if future_count is not None:
        data["future_blueprint_count"] = future_count
    return data


def _write_work(root: Path, *, chapter_no: int = 2, prev_tail=None, future_count=None) -> Path:
    wd = root / "关山灯"
    for sub in ("60-setting", "70-volume", "80-brief", "90-draft"):
        (wd / sub).mkdir(parents=True, exist_ok=True)
    (wd / "60-setting" / "setting.yaml").write_text(
        yaml.safe_dump(_setting(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    (wd / "70-volume" / "volume-001.yaml").write_text(
        yaml.safe_dump(_volume(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    for n in range(1, 5):
        kwargs = {}
        if n == chapter_no:
            kwargs = {"prev_tail": prev_tail, "future_count": future_count}
        (wd / "80-brief" / f"v001-c{n:04d}.yaml").write_text(
            yaml.safe_dump(_brief(n, **kwargs), allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    (wd / "90-draft" / "v001-c0001.md").write_text(
        "李默睁开眼。\n\n" + ("他确认了一遍数值，确认不是幻觉。" * 80), encoding="utf-8")
    return wd


# ── C1/C2：预算与裁剪账 ─────────────────────────────────────


def test_required_first_and_trim_receipt() -> None:
    print("预算分配 · 必需优先、可选按比例、裁剪留痕")
    blocks = [
        ContextBlock("anchor", "全书锚点", "required", "甲" * 5000, "60-setting/setting.yaml"),
        ContextBlock("recap", "前情回顾", "optional", "乙" * 3000, "70-volume/*.yaml"),
        ContextBlock("cast", "角色动态", "optional", "丙" * 2000, "80-brief/*.yaml"),
        ContextBlock("tiny", "极小可选块", "optional", "丁" * 130, "80-brief/*.yaml"),
    ]
    text, report = build_context(blocks, budget_chars=8000, footer="尾注" * 50)

    check(report["schema_version"] == "context-budget-v1", "报告有 schema 版本")
    check(len(report["blocks"]) == 4, "空块/被裁块也进报告，不遗漏")
    check(report["used_chars"] == len(text), "used_chars 就是实际注入长度")
    check(report["exact"] is True, "块字数 + 分隔 + 尾注 == 实际长度")
    check(report["block_chars"] + report["separator_chars"] + report["footer_chars"]
          == report["accounted_chars"], "账目三项加起来等于 accounted_chars")
    check(report["used_chars"] <= report["budget_chars"], "总注入不超预算")

    anchor = next(b for b in report["blocks"] if b["id"] == "anchor")
    check(anchor["status"] == "included" and anchor["chars"] == 5000,
          "必需块完整注入，没被裁")
    recap = next(b for b in report["blocks"] if b["id"] == "recap")
    check(recap["status"] == "trimmed", "可选块超预算时按比例截断")
    check(recap["chars"] < recap["original_chars"], "截断块真的少注入了字")
    check("少注入" in recap["reason"], "截断原因写明少喂了多少字")
    tiny = next(b for b in report["blocks"] if b["id"] == "tiny")
    check(tiny["status"] == "omitted", "分到太少的小块整块不注入")
    check(tiny["reason_code"] == "budget_share_too_small", "小块不注入有原因码")
    check(tiny["chars"] == 0 and tiny["original_chars"] == 130, "未注入块保留原始字数")
    check(bool(anchor["sha256"]) and len(anchor["sha256"]) == 64, "每块有原文指纹")
    check("甲" * 5000 in text, "必需块原文确实进了最终文本")
    check("（此块超出预算，后面没有注入）" in text, "截断处在文本里有可见说明")


def test_required_overflow_raises_readable_error() -> None:
    print("预算分配 · 必需材料装不下必须报错，不裁剪")
    blocks = [
        ContextBlock("anchor", "全书锚点", "required", "甲" * 900, "60-setting/setting.yaml"),
        ContextBlock("part", "本段任务", "required", "乙" * 300, "70-volume/*.yaml"),
    ]
    try:
        build_context(blocks, budget_chars=1000, footer="尾注" * 50)
    except ContextBudgetError as exc:
        message = str(exc)
        check(exc.excess_chars == 302, f"算准超出量（实际 {exc.excess_chars}）")
        check(exc.budget_chars == 1000 and exc.required_chars == 1200, "错误里带预算与必需字数")
        check("超出 302 字" in message, "错误文案点名超出多少字")
        check("全书锚点" in message and "本段任务" in message, "错误文案点名是哪几块装不下")
        check("不能靠裁掉必需材料继续写" in message, "错误给了下一步，不是干巴巴的 500")
    else:
        check(False, "必需超预算却没有抛 ContextBudgetError")


def test_empty_blocks_also_reported() -> None:
    print("预算分配 · 没有内容的块也要说清「为什么没喂」")
    blocks = [
        ContextBlock("anchor", "全书锚点", "required", "甲" * 500, "60-setting/setting.yaml"),
        ContextBlock("prev", "上一章正文", "optional", "", "90-draft/<上一章>.md",
                     empty_note="上一章正文还没落盘"),
    ]
    text, report = build_context(blocks, budget_chars=8000, footer="尾注")
    prev = next(b for b in report["blocks"] if b["id"] == "prev")
    check(prev["status"] == "omitted" and prev["reason_code"] == "empty_source",
          "空来源标成 empty_source")
    check("还没落盘" in prev["reason"], "空来源用的是具体原因，不是通用占位")
    check("上一章正文" not in text, "空块没有混进最终文本")


# ── C3/C4/A4：装配、可配置、落盘 ────────────────────────────


def test_write_brief_report_and_config() -> None:
    print("写前必读 · 报告与上下文一致、末段四档、未来蓝图")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp), chapter_no=2, prev_tail=500, future_count=2)
        text, report = render_write_brief_report(
            wd, chapter_no=2, prev_chapter_no=1,
            brief=_brief(2, prev_tail=500, future_count=2), budget_chars=8000,
        )
        ids = [b["id"] for b in report["blocks"]]
        check(ids == ["anchor", "recap", "notes", "part", "cast", "foreshadow", "hook",
                      "future", "prev"], "九块都在报告里，顺序固定（notes 独立成块）")
        check(report["used_chars"] == len(text), "报告字数与真实装配文本一致")
        check(report["exact"] is True, "装配账目自洽")

        anchor = next(b for b in report["blocks"] if b["id"] == "anchor")
        part = next(b for b in report["blocks"] if b["id"] == "part")
        check(anchor["status"] == "included", "锚点是必需块")
        check(part["status"] == "included", "本段任务是必需块")
        check(anchor["chars"] == anchor["original_chars"], "必需块没有少字")

        prev = next(b for b in report["blocks"] if b["id"] == "prev")
        check(prev["original_chars"] < 1200, "prev_tail_chars=500 真的只取末段")
        check("末尾 500 字" in text, "末段档位写进了提示文本")

        future = next(b for b in report["blocks"] if b["id"] == "future")
        check(future["status"] == "included", "未来蓝图进了上下文")
        check("第 3 章" in text and "第 4 章" in text, "未来 2 章都在蓝图里")
        check("第 5 章" not in text, "future_blueprint_count=2 不多给")
        check("本章一个字都不要提前写" in text, "蓝图带防剧透硬约束")

    print("写前必读 · full 档与默认 900 档")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp), chapter_no=2, prev_tail="full")
        text, report = render_write_brief_report(
            wd, chapter_no=2, prev_chapter_no=1,
            brief=_brief(2, prev_tail="full"), budget_chars=8000,
        )
        prev = next(b for b in report["blocks"] if b["id"] == "prev")
        check("上一章全文" in text and "共" in text, "full 档注入全文并标字数")
        check(prev["original_chars"] > 900, "full 档确实比默认 900 字长")

    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp), chapter_no=2)
        text, _report = render_write_brief_report(
            wd, chapter_no=2, prev_chapter_no=1, brief=_brief(2), budget_chars=8000,
        )
        check("末尾 900 字" in text, "不写配置时默认 900 字")


def test_manifest_roundtrip() -> None:
    print("A4 · 上下文清单落盘可读回")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp), chapter_no=2)
        _text, report = render_write_brief_report(
            wd, chapter_no=2, prev_chapter_no=1, brief=_brief(2), budget_chars=8000,
        )
        path = save_manifest(wd, "v001-c0002", report, extra={"phase": "assembled",
                                                             "model": "test-model"})
        check(path == manifest_path(wd, "v001-c0002"), "manifest 路径是 90-draft/<章>.context.json")
        check(path.exists(), "manifest 真的写出来了")
        data = load_manifest(wd, "v001-c0002")
        check(data is not None and data["schema_version"] == "context-manifest-v1",
              "读回来 schema 正确")
        check(data["report"]["used_chars"] == report["used_chars"], "读回来的预算报告不缩水")
        check(data["phase"] == "assembled", "额外字段（阶段）也落了盘")
        check(load_manifest(wd, "v001-c9999") is None, "没写过的章节返回 None，不报错")


def test_recap_no_silent_break() -> None:
    print("C2 · 调用方自设上限时，明说哪几卷没注入")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wd = _write_work(root, chapter_no=2)
        (wd / "70-volume" / "volume-002.yaml").write_text(
            yaml.safe_dump(_volume(2, 5, 8), allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        text = render_recap(wd, upto_chapter_no=7, max_chars=120)
        check("没有注入" in text, "超上限时文本里有明确说明")
        check("第 2 卷" in text, "点名了被跳过的卷")


def main() -> int:
    test_required_first_and_trim_receipt()
    test_required_overflow_raises_readable_error()
    test_empty_blocks_also_reported()
    test_write_brief_report_and_config()
    test_manifest_roundtrip()
    test_recap_no_silent_break()
    print("=" * 58)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
