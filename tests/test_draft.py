"""写正文的自检。不联网、不花钱。

这里查的是机器**查得准**的三件事：字数、禁令字面命中、该出场的人漏了谁。
钩子够不够、情节顺不顺、伏笔漂不漂亮——查不了，所以不假装查。

以及 `strip_wrapping`：模型永远会加上「第 1 章 数值」这种壳和 ``` 围栏。
它加壳是提示词里的要求，但要求归要求，回来必须洗一遍——
正文文件里混进标题行，后面所有字数统计和拼接都跟着脏。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.draft import (  # noqa: E402
    build_draft_messages,
    build_draft_plan,
    check_draft,
    count_chars,
    load_draft,
    save_draft,
    strip_wrapping,
)

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def _setting() -> dict:
    return {
        "work": "关山灯", "logline": "零层废柴靠数值之眼活下来", "core_motive": "活着回去",
        "style": {"perspective": "第三限知", "taboo": ["不用破折号"]},
        "world": {"era": "天元三百年", "places": [{"name": "青云宗", "note": "第一大宗"}]},
        "power": {"system": "修为", "tiers": ["炼气", "筑基"]},
        "factions": [{"name": "青云宗", "stance": "表面中立"}],
        "characters": [{"name": "李默", "role": "主角", "motive": "活着回去"}],
    }


def _volume() -> dict:
    return {"vol": {"vol": 1, "title": "灯下黑", "start_chapter": 1, "end_chapter": 6,
                    "core_conflict": "零层怎么活", "goal": "站住脚",
                    "chapters": [{"chapter_no": 1, "title": "数值", "gist": "看到修为数值"}]}}


def _brief() -> dict:
    return {
        "chapter_id": "v001-c0001", "title": "数值", "one_line": "李默看到别人的修为数值",
        "timeline": "入宗第二天清晨", "core_plot": ["醒来看见数值", "试着确认不是幻觉"],
        "narrative": {"perspective": "第三限知", "tone": "冷幽默", "focus": ["第一次看见数值的震撼"]},
        "settings": {"characters": ["李默", "玄阳真人"], "factions": ["青云宗"], "terms": ["数值之眼"]},
        "foreshadow": [{"action": "埋设", "id": "v001-c0001-f01", "desc": "眼睛能看数值"}],
        "carry_over": ["李默刚被塞进柴房"], "must_not": ["穿越前的现代生活"],
        "word_target": [2800, 4000],
    }


def test_strip_wrapping() -> None:
    print("洗掉模型加的壳")

    cases = [
        ("第 1 章 数值\n\n李默睁开眼。", "李默睁开眼。"),
        ("## 数值\n\n李默睁开眼。", "李默睁开眼。"),
        ("第一章 数值\n李默睁开眼。", "李默睁开眼。"),
        ("第十二章　数值\n李默睁开眼。", "李默睁开眼。"),
        ("```\n李默睁开眼。\n```", "李默睁开眼。"),
        ("```markdown\n李默睁开眼。\n```", "李默睁开眼。"),
        ("“李默睁开眼。”", "李默睁开眼。"),
        ("李默睁开眼。", "李默睁开眼。"),
    ]
    for raw, want in cases:
        got = strip_wrapping(raw)
        check(got == want, f"{raw[:14]!r} → {got[:14]!r}")

    # 正文里本来就有「第 X 章」这种字样时不能误伤中间的内容
    body = "李默翻到第三章那一页，上面写着：「第 3 章 试炼」。"
    check(strip_wrapping(body) == body, "正文中间的「第 3 章」字样不被误删")


def test_count_chars() -> None:
    print("字数统计")

    check(count_chars("你好 世界\n\n再来") == 6, f"空白不计入（实际 {count_chars('你好 世界\n\n再来')}）")
    check(count_chars("") == 0, "空串是 0")
    check(count_chars("   \n\t ") == 0, "纯空白是 0")


def test_check_draft_word_count() -> None:
    """阈值：目标 2800–4000 → 不到一半（1400）是阻断，短于下限或超出上限都是提示。

    **没有宽容带**：实测真写第一章时，4784 字对 2800–4000 的目标，
    因为在 1.3 倍宽容带内而静默通过——统计说「不在区间内」，却一条提示都没有。
    这是假绿灯，比不查更坏。
    """
    print("自检 · 字数")

    brief = _brief()
    body = "李默" * 1500  # 3000 字，落在 2800–4000 里
    errors, warnings, stats = check_draft(body, brief=brief)
    check(not errors, f"落在区间内不报错（{errors}）")
    check(not any("目标" in w for w in warnings), f"落在区间内不报字数（{warnings}）")
    check(stats["in_range"], "统计里标了 in_range")
    check(stats["chars"] == 3000, f"字数是 3000（实际 {stats['chars']}）")

    errors, _, _ = check_draft("李默" * 600, brief=brief)  # 1200 字 < 1400
    check(any("不到目标下限" in e for e in errors), f"不到一半是阻断（{errors}）")

    errors, warnings, _ = check_draft("李默" * 800, brief=brief)  # 1600 字，过半但没到下限
    check(not errors and any("短于目标下限" in w for w in warnings),
          f"偏短是提示不是阻断（{warnings}）")

    # 关键回归：刚过上限一点点也必须报出来
    errors, warnings, _ = check_draft("李默" * 2100, brief=brief)  # 4200 字，刚过 4000
    check(not errors and any("超出目标上限" in w for w in warnings),
          f"刚过上限就提示，不给宽容带（{warnings}）")

    errors, warnings, _ = check_draft("李默" * 3000, brief=brief)  # 6000 字
    check(not errors and any("超出目标上限" in w for w in warnings), f"远超上限也是提示（{warnings}）")

    errors, _, _ = check_draft("", brief=brief)
    check(any("是空的" in e for e in errors), "空正文是阻断")


def test_check_draft_detects_truncation() -> None:
    """被截断的结尾要看出来，而且不依赖供应商的 finish_reason。

    实测这家 API 的 finish_reason 返回 None，光靠它等于没查。
    末尾是不是句末标点，是独立于供应商的信号。
    """
    print("自检 · 结尾像不像被截断")

    brief = _brief()
    done = ("李默睁开眼。" + "他看了一会儿。" * 300) + "于是他决定活下去。"
    errors, warnings, _ = check_draft(done, brief=brief)
    check(not any("截断" in w for w in warnings), f"正常结尾不报（{warnings}）")

    cut = ("李默睁开眼。" + "他看了一会儿。" * 300) + "于是他决定"
    errors, warnings, _ = check_draft(cut, brief=brief)
    check(any("截断" in w for w in warnings), f"结尾断开要报（{warnings}）")

    for end in ("。", "！", "？", "…", "”"):
        _, warns, _ = check_draft("正文内容真的够长啊" * 300 + end, brief=brief)
        check(not any("截断" in w for w in warns), f"末尾是「{end}」不算截断")


def test_check_draft_must_not() -> None:
    """禁令是硬红线：命中了就是阻断，不是提示。"""
    print("自检 · 禁止出现")

    body = "李默睁开眼。" + "他想起穿越前的现代生活。" + "李默" * 1500
    errors, warnings, _ = check_draft(body, brief=_brief())
    check(any("禁止出现" in e for e in errors), f"命中禁令是阻断（{errors}）")

    clean = "李默睁开眼。" + "李默" * 1500
    errors, _, _ = check_draft(clean, brief=_brief())
    check(not any("禁止出现" in e for e in errors), "没命中就不报")


def test_check_draft_missing_characters() -> None:
    """指令里写了要出场却没出现的人，必须点出来。

    这是作者最容易漏看的一件事：他检查情节，忘了检查人。
    """
    print("自检 · 该出场的人漏了谁")

    body = "李默睁开眼。" + "李默" * 1500
    errors, warnings, stats = check_draft(body, brief=_brief())
    check(stats["characters_missing"] == ["玄阳真人"], f"抓出漏掉的人（{stats['characters_missing']}）")
    check(any("玄阳真人" in w for w in warnings), "提示里点了名字")
    check(stats["characters_appeared"] == ["李默"], "出现过的人也在统计里")

    body2 = "李默和玄阳真人都出现了。" + "李默" * 1500
    _, _, stats2 = check_draft(body2, brief=_brief())
    check(not stats2["characters_missing"], "都出场了就不报")


def test_draft_plan_blocks_without_brief() -> None:
    """没有指令就不该开写——那等于让模型替你决定这一章要干什么。"""
    print("计划 · 没指令不开工")

    plan = build_draft_plan(
        chapter_id="v001-c0007", title="登门", setting=_setting(), volume=_volume(),
        brief={}, provider_id="deepseek", model_id="deepseek-flash",
        brief_errors=[], brief_exists=False,
    )
    d = plan.to_dict()
    check(not d["ready"], "没指令时 ready=False")
    check("还没有" in d["blocker"] and "指令" in d["blocker"], f"说清了原因（{d['blocker']}）")

    plan2 = build_draft_plan(
        chapter_id="v001-c0001", title="数值", setting=_setting(), volume=_volume(),
        brief=_brief(), provider_id="deepseek", model_id="deepseek-flash",
        brief_errors=["core_plot 是空的"], brief_exists=True,
    )
    check(not plan2.to_dict()["ready"], "指令有阻断时也不开工")
    check("先修指令" in plan2.blocker, f"指向先修指令（{plan2.blocker}）")


def test_draft_plan_counts_and_cost() -> None:
    print("计划 · 字数与费用")

    plan = build_draft_plan(
        chapter_id="v001-c0001", title="数值", setting=_setting(), volume=_volume(),
        brief=_brief(), refs_block="x" * 261, provider_id="deepseek", model_id="deepseek-flash",
        price_input_per_mtok=2.0, price_output_per_mtok=8.0, bucket="peak",
    )
    d = plan.to_dict()
    check(d["ready"], "齐了就 ready")
    check(d["setting_chars"] > 0 and d["volume_chars"] > 0 and d["brief_chars"] > 0,
          f"三层都算进了输入（{d['setting_chars']}/{d['volume_chars']}/{d['brief_chars']}）")
    check(d["word_range"] == [2800, 4000], f"目标字数取到了（{d['word_range']}）")
    check(d["est_cost_cny"] and d["est_cost_cny"] > 0, f"算出了费用（{d['est_cost_cny']}）")
    check(d["est_output_tokens"] > 3000, f"输出按上限估（{d['est_output_tokens']}）")
    check("高峰" in d["price_note"], "单价说明写了时段")


def test_messages_carry_three_layers() -> None:
    """提示词必须把三层都带上，而且顺序是先设定、再本卷、再本章。"""
    print("提示词 · 三层都在")

    msgs = build_draft_messages(setting=_setting(), volume=_volume(), brief=_brief(),
                                refs_block="【参照素材】某书的结构指纹", title="数值")
    check(len(msgs) == 2 and msgs[0]["role"] == "system", "一条 system + 一条 user")
    user = msgs[1]["content"]
    for needle in ("设定集", "李默", "本卷目录", "灯下黑", "本章创作任务指令",
                   "李默看到别人的修为数值", "参照素材", "2800–4000"):
        check(needle in user, f"用户消息里有「{needle}」")
    check(user.index("设定集") < user.index("本卷目录") < user.index("本章创作任务指令"),
          "顺序是设定集 → 本卷 → 本章指令")

    system = msgs[0]["content"]
    for needle in ("只输出正文", "不要标题", "章末留钩子", "禁止出现", "不要替作者新增大段设定"):
        check(needle in system, f"系统提示里有硬约束「{needle}」")


def test_save_and_load(tmp_dir: Path) -> None:
    print("落盘与读回")

    work = tmp_dir / "作品"
    saved = save_draft(work, "v001-c0001", text="李默睁开眼。", meta={"model": "m", "chars": 6})
    check(Path(saved["text_path"]).exists(), "正文写成了 .md")
    check(Path(saved["meta_path"]).exists(), "元数据写成了 .json")

    loaded = load_draft(work, "v001-c0001")
    check(loaded["exists"] and loaded["text"] == "李默睁开眼。", "读回来内容一致")
    check(loaded["meta"]["model"] == "m", "元数据读得回来")

    missing = load_draft(work, "v001-c9999")
    check(not missing["exists"] and missing["text"] == "", "没写过的章节返回 exists=False，不报错")


def main() -> int:
    import tempfile

    print("=" * 58)
    print("写正文自检（不联网、不花钱）")
    print("=" * 58)

    for fn in (
        test_strip_wrapping,
        test_count_chars,
        test_check_draft_word_count,
        test_check_draft_detects_truncation,
        test_check_draft_must_not,
        test_check_draft_missing_characters,
        test_draft_plan_blocks_without_brief,
        test_draft_plan_counts_and_cost,
        test_messages_carry_three_layers,
    ):
        fn()
        print()

    with tempfile.TemporaryDirectory() as tmp:
        test_save_and_load(Path(tmp))
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
