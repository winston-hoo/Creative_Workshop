"""伏笔台账自检。不需要密钥、不调模型。

直接跑：python tests/test_ledger.py

验证的是台账语义，每一条都对应一个真实会踩的坑：
  · 编号由台账分配，模型只用不改
  · 模型引用不存在的编号时不能丢信息
  · 对已回收的伏笔再做动作要拦下来
  · 未回收清单必须进**变量部分**，不能污染固定前缀（否则缓存全失效）
  · 长期未推进的伏笔要能被识别为疑似断点
  · **重跑同一章必须幂等**（一部 545 章的连载真实踩到：
    重跑 20 章就让台账从 794 条涨到 825 条，全量重跑会让它翻倍）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.ledger import Ledger  # noqa: E402
from workshop.primitives import load_primitives, load_work  # noqa: E402
from workshop.prompts import build_stable_prefix, build_variable_tail  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


# ── 分配与写回 ──────────────────────────────────────────────


def test_plan_assigns_id_and_writes_back() -> None:
    """埋设时编号留空，由台账分配，并**写回标注结果**。

    写回很关键：落盘的标注文件必须带台账的规范编号，
    否则标注文件自身不自洽，之后按编号回溯就对不上。
    """
    print("编号由台账分配并写回")

    led = Ledger(work="测试作品")
    foreshadows = [
        {"动作": "埋设", "编号": "", "描述": "主角身世之谜"},
        {"动作": "埋设", "编号": "模型瞎编的编号", "描述": "神秘信物"},
    ]
    report = led.apply(chapter_id="c1", chapter_no=1, foreshadows=foreshadows)

    check(len(report.added) == 2, "两条埋设都登记了")
    check(foreshadows[0]["编号"] == "F-001", f"第一条写回 F-001（实际 {foreshadows[0]['编号']!r}）")
    check(
        foreshadows[1]["编号"] == "F-002",
        "第二条即使模型填了编号，也按埋设重新分配（不让模型主导编号）",
    )
    check(report.anomalies == [], "埋设不该产生异常")
    check(led.find("F-001") is not None and led.find("F-001").desc == "主角身世之谜", "描述入库")


def test_advance_and_recover() -> None:
    print("推进与回收")

    led = Ledger()
    led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[{"动作": "埋设", "编号": "", "描述": "神秘信物"}, {"动作": "埋设", "编号": "", "描述": "旧敌"}],
    )
    report = led.apply(
        chapter_id="c3",
        chapter_no=3,
        foreshadows=[
            {"动作": "推进", "编号": "F-001", "描述": "信物出现裂痕"},
            {"动作": "回收", "编号": "F-002", "描述": "旧敌现身"},
        ],
    )

    check(report.advanced == ["F-001"], "推进被记录")
    check(report.recovered == ["F-002"], "回收被记录")
    check(led.find("F-001").last_touched_chapter_no == 3, "最后推进章被更新")
    check(not led.find("F-002").is_open, "回收后状态变为已回收")
    check(len(led.open_items) == 1, "未回收清单只剩一条")


def test_unknown_reference_not_lost() -> None:
    """模型引用不存在的编号时，**不能丢信息**。

    真实场景：模型一口咬定它在推进某条伏笔，但编号对不上（上一轮编号变过、
    或它自己编的）。直接丢弃等于把这一章的伏笔线索抹掉。
    """
    print("引用不存在的编号不丢信息")

    led = Ledger()
    report = led.apply(
        chapter_id="c5",
        chapter_no=5,
        foreshadows=[{"动作": "推进", "编号": "F-999", "描述": "某条线索"}],
    )

    check(len(report.anomalies) == 1, "记录了异常")
    check("不存在" in report.anomalies[0], f"异常说明了原因（{report.anomalies[0]}）")
    check(len(report.added) == 1, "按埋设重新登记，信息没丢")
    check(len(led.items) == 1, "台账里确实多了一条")
    check(led.items[0].desc == "某条线索", "描述保留了")

    # 未填编号的推进同样处理
    led2 = Ledger()
    report2 = led2.apply(
        chapter_id="c6", chapter_no=6, foreshadows=[{"动作": "推进", "编号": "", "描述": "另一条"}]
    )
    check(len(report2.anomalies) == 1 and len(led2.items) == 1, "未填编号的推进也重新登记")


def test_action_on_recovered_blocked() -> None:
    print("对已回收的伏笔再做动作要拦下来")

    led = Ledger()
    led.apply(chapter_id="c1", chapter_no=1, foreshadows=[{"动作": "埋设", "编号": "", "描述": "旧敌"}])
    led.apply(chapter_id="c2", chapter_no=2, foreshadows=[{"动作": "回收", "编号": "F-001", "描述": "旧敌现身"}])
    report = led.apply(
        chapter_id="c3", chapter_no=3, foreshadows=[{"动作": "推进", "编号": "F-001", "描述": "又提了一次"}]
    )

    check(len(report.ignored) == 1, "动作被忽略")
    check(bool(report.anomalies), "记录了异常（作者该看一眼是不是真回收了）")
    check(led.find("F-001").status == "recovered", "状态没有被改回未回收")
    check(led.find("F-001").last_touched_chapter_no == 2, "最后推进章没有被污染")


def test_meaningless_foreshadow_ignored() -> None:
    print("无意义条目被忽略而不是塞进台账")

    led = Ledger()
    report = led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[
            {"动作": "", "编号": "", "描述": ""},
            {"动作": "随便写的", "编号": "", "描述": "???"},
        ],
    )
    check(len(led.items) == 0, f"识别不了的动作不入库（实际 {len(led.items)} 条）")
    check(len(report.anomalies) == 2, f"两条都被记为异常（实际 {len(report.anomalies)}）")
    check(all("无法识别" in a for a in report.anomalies), "异常说明了原因")


def test_partial_bad_items_do_not_kill_whole_list() -> None:
    """回归：数组字段里一个坏条目曾让整列作废。

    对伏笔来说，那等于丢掉整章的线索——只因为某个「动作」写错了一个字。
    现在只丢坏的那一项，好的留下，并把丢掉的记进校验问题。
    """
    print("数组字段部分失败不牵连整体（回归）")

    primitives = load_primitives(ROOT / "primitives.yaml")
    payload = {
        "perspective": "第三限知",
        "scene_switches": 1,
        "time_span": "即时",
        "hook_strength": 4,
        "hook_type": "悬念",
        "emotion": 4,
        "conflict": 3,
        "info_release": 4,
        "mainline_progress": 3,
        "subplot_count": 1,
        "payoffs": [{"锚点": "甲", "类型": "反转", "强度": 4}],
        "foreshadows": [
            {"动作": "埋设", "编号": "", "描述": "好的那条"},
            {"动作": "写错了", "编号": "", "描述": "坏的那条"},
            {"动作": "回收", "编号": "F-001", "描述": "也是好的"},
        ],
        "motive_strength": 3,
        "confidence": "高",
        "uncertain_fields": [],
    }
    cleaned, issues = primitives.validate(payload)

    kept = cleaned.get("foreshadows") or []
    check(len(kept) == 2, f"保住了 2 条好条目（实际 {len(kept)}）")
    check(any(e.get("描述") == "好的那条" for e in kept), "第一条好条目保留")
    check(any(e.get("描述") == "也是好的" for e in kept), "第三条好条目也保留")
    check(
        any("foreshadows" in i and "取值非法" in i for i in issues),
        f"被丢掉的条目记进了校验问题（{issues}）",
    )
    check(cleaned.get("payoffs") and len(cleaned["payoffs"]) == 1, "其他数组字段不受影响")


# ── 上下文与断点 ────────────────────────────────────────────


def test_context_excludes_recovered_and_shows_gap() -> None:
    print("未回收清单：排除已回收、带上间隔章数")

    led = Ledger()
    led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[{"动作": "埋设", "编号": "", "描述": "甲"}, {"动作": "埋设", "编号": "", "描述": "乙"}],
    )
    led.apply(chapter_id="c2", chapter_no=2, foreshadows=[{"动作": "回收", "编号": "F-002", "描述": "乙被解决"}])

    ctx = led.context_for_prompt(chapter_no=11)
    check(len(ctx) == 1, f"只返回未回收的（实际 {len(ctx)}）")
    check(ctx[0]["id"] == "F-001", "返回的是未回收那条")
    check(ctx[0]["gap"] == 10, f"间隔章数正确（实际 {ctx[0]['gap']}）")
    check(ctx[0]["planted_chapter_no"] == 1, "带上了埋设章")


def test_stale_detection() -> None:
    print("疑似断点识别")

    led = Ledger()
    led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[{"动作": "埋设", "编号": "", "描述": "长期没动的"}, {"动作": "埋设", "编号": "", "描述": "刚推过的"}],
    )
    led.apply(chapter_id="c40", chapter_no=40, foreshadows=[{"动作": "推进", "编号": "F-002", "描述": "刚提过"}])

    stale = led.stale_items(current_chapter_no=50, threshold=30)
    check(len(stale) == 1, f"识别出 1 条断点（实际 {len(stale)}）")
    check(stale[0].id == "F-001", "断点是长期未动的那条")

    led.apply(chapter_id="c41", chapter_no=41, foreshadows=[{"动作": "推进", "编号": "F-001", "描述": "动了"}])
    check(len(led.stale_items(current_chapter_no=50, threshold=30)) == 0, "推进后不再是断点")


# ── 重跑幂等 ────────────────────────────────────────────────


def _plants_in(led: Ledger, fid: str, chapter_id: str) -> list:
    """某条目里由指定章产生的「埋设」事件。

    不能直接数 events 总数：别的章的推进/回收事件也会留在同一条目上，
    那是应该保留的（重跑只撤销本章痕迹）。
    """
    item = led.find(fid)
    if item is None:
        return []
    return [e for e in item.events if e.chapter_id == chapter_id and e.action == "埋设"]


def _book_ledger() -> Ledger:
    """造一本小书的台账：c1 埋两条，c2 推进一条，c3 回收一条。"""
    led = Ledger(work="演示作品")
    led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[
            {"动作": "埋设", "编号": "", "描述": "主角身世有隐情"},
            {"动作": "埋设", "编号": "", "描述": "反派的真面目未揭晓"},
        ],
    )
    led.apply(
        chapter_id="c2", chapter_no=2, foreshadows=[{"动作": "推进", "编号": "F-001", "描述": "身世线索浮现"}]
    )
    led.apply(
        chapter_id="c3", chapter_no=3, foreshadows=[{"动作": "回收", "编号": "F-002", "描述": "真面目揭晓"}]
    )
    return led


def test_rerun_same_chapter_is_idempotent() -> None:
    """回归：重跑一章会把「推进」追加成两条 events。

    同一章跑两次，台账必须和跑一次完全一样——条目数、events 数、
    编号、状态全都一样。这是全量重跑 540 章能不能做的前提。
    """
    print("重跑同一章：结果不变")

    once = _book_ledger()
    twice = _book_ledger()
    twice.apply(
        chapter_id="c2", chapter_no=2, foreshadows=[{"动作": "推进", "编号": "F-001", "描述": "身世线索浮现"}]
    )

    check(len(twice.items) == len(once.items), f"条目数不变（{len(once.items)} → {len(twice.items)}）")
    check(
        sum(len(i.events) for i in twice.items) == sum(len(i.events) for i in once.items),
        f"events 总数不变（{sum(len(i.events) for i in once.items)} → "
        f"{sum(len(i.events) for i in twice.items)}）",
    )
    check([i.id for i in twice.items] == [i.id for i in once.items], "编号集合不变")
    check(
        [i.status for i in twice.items] == [i.status for i in once.items],
        "回收状态不变（c3 的回收没有被误撤销）",
    )

    # 反复跑同一个动作也不能涨
    for _ in range(3):
        twice.apply(
            chapter_id="c2",
            chapter_no=2,
            foreshadows=[{"动作": "推进", "编号": "F-001", "描述": "身世线索浮现"}],
        )
    check(
        len(twice.find("F-001").events) == len(once.find("F-001").events),
        f"反复重跑同一章 events 不累积（{len(once.find('F-001').events)} → "
        f"{len(twice.find('F-001').events)}）",
    )


def test_rerun_reuses_planted_id() -> None:
    """回归：重跑一章，上次埋的伏笔被当成新伏笔又发一个编号。

    描述措辞漂一点也要认出来是同一条，复用原编号。
    """
    print("重跑埋设：复用旧编号而不是发新号")

    led = _book_ledger()
    before = set(i.id for i in led.items)
    seq_before = led.next_seq

    report = led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[
            {"动作": "埋设", "编号": "", "描述": "主角身世藏着隐情"},  # 措辞变了
            {"动作": "埋设", "编号": "", "描述": "反派真面目还没揭晓"},  # 措辞变了
        ],
    )

    check(set(i.id for i in led.items) == before, f"编号集合没变（{sorted(before)}）")
    check(led.next_seq == seq_before, f"没有消耗新编号（next_seq {seq_before} → {led.next_seq}）")
    check(report.added == ["F-001", "F-002"], f"复用的正是原来的编号（实际 {report.added}）")
    check(
        all(len(_plants_in(led, fid, "c1")) == 1 for fid in ("F-001", "F-002")),
        "每条各自只有一个 c1 的埋设事件（不重复堆积）",
    )


def test_rerun_distinguishes_different_foreshadow() -> None:
    """反向：真的是新伏笔时不能硬并到旧条目上。"""
    print("重跑埋设：真·新伏笔要发新号")

    led = _book_ledger()
    report = led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[{"动作": "埋设", "编号": "", "描述": "主角身世有隐情"}, {"动作": "埋设", "编号": "", "描述": "外星舰队已在路上"}],
    )

    check("F-003" in report.added, f"全新的伏笔拿到新编号（实际 {report.added}）")
    check(len(_plants_in(led, "F-001", "c1")) == 1, "旧条目被复用而不是被并掉")
    # 这一轮没有重新埋 F-002，它就不该有新的埋设事件；
    # 但条目本身要留着（宁可多留一条也不丢信息），别的章的回收记录也还在。
    check(len(_plants_in(led, "F-002", "c1")) == 0, "没重新埋的不会凭空长出埋设事件")
    check(led.find("F-002") is not None, "没重新埋的旧条目仍然保留，没有被删掉")
    check(not led.find("F-002").is_open, "它身上别的章做的回收记录还在")
    check(led.find("F-003").desc == "外星舰队已在路上", "新条目描述正确")


def test_rerun_undoes_this_chapters_recovery() -> None:
    """回收动作若正是这一章做的，重跑时应先撤销——否则状态会卡在「已回收」。

    反过来，别的章做的回收不能被这一章的重跑误撤销。
    """
    print("重跑撤销本章的回收，但不动别章的")

    led = _book_ledger()
    check(not led.find("F-002").is_open, "前置：F-002 已被 c3 回收")

    # c3 重跑，但这次只推进不回收
    led.apply(
        chapter_id="c3", chapter_no=3, foreshadows=[{"动作": "推进", "编号": "F-002", "描述": "真面目仍未揭晓"}]
    )
    check(led.find("F-002").is_open, "c3 不再回收它，状态退回未回收")
    check(len(led.find("F-002").events) == 2, f"只留下埋设+推进两条（实际 {len(led.find('F-002').events)}）")

    # c1 重跑，不能影响 c3 做的回收判定
    led2 = _book_ledger()
    led2.apply(
        chapter_id="c1", chapter_no=1, foreshadows=[{"动作": "埋设", "编号": "", "描述": "主角身世有隐情"}]
    )
    check(not led2.find("F-002").is_open, "别的章重跑不会误撤销 c3 的回收")


def test_rerun_prunes_unconfirmed_plants() -> None:
    """回归：只撤销不清理，台账会只增不减。

    上一轮埋的、这一轮没再确认的条目是孤儿，必须清掉，
    否则每重跑一轮就攒一批，全量重跑后台账会明显膨胀。
    """
    print("重跑清理：本章埋过但这一轮没再埋的要清掉")

    led = Ledger()
    led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[
            {"动作": "埋设", "编号": "", "描述": "甲的旧伏笔"},
            {"动作": "埋设", "编号": "", "描述": "乙的旧伏笔"},
        ],
    )
    # 乙后来被第 2 章推进过，它身上带着别的章的痕迹
    led.apply(
        chapter_id="c2", chapter_no=2, foreshadows=[{"动作": "推进", "编号": "F-002", "描述": "乙的线索又出现"}]
    )
    check(len(led.items) == 2, "前置：两条都在")

    led.apply(
        chapter_id="c1", chapter_no=1, foreshadows=[{"动作": "埋设", "编号": "", "描述": "甲的旧伏笔"}]
    )

    check(led.find("F-001") is not None, "这一轮仍确认的甲保留")
    check(led.find("F-002") is not None, "被别的章推进过的乙不会被误删")
    check(len(led.items) == 2, f"没有凭空多出条目（实际 {len(led.items)}）")
    check(len(_plants_in(led, "F-002", "c1")) == 0, "乙身上 c1 的埋设痕迹已撤销")
    check(
        len([e for e in led.find("F-002").events if e.chapter_id == "c2"]) == 1,
        "但 c2 的推进痕迹还在（别的章的记录不受这一章重跑影响）",
    )

    # 这一章一个伏笔都不报了：它自己埋的、且没人碰过的条目要清干净
    led2 = Ledger()
    led2.apply(chapter_id="c1", chapter_no=1, foreshadows=[{"动作": "埋设", "编号": "", "描述": "丙"}])
    led2.apply(chapter_id="c1", chapter_no=1, foreshadows=[])
    check(len(led2.items) == 0, f"本章不再确认的条目被清掉（实际 {len(led2.items)}）")


# ── 持久化 ──────────────────────────────────────────────────


def test_roundtrip() -> None:
    print("台账持久化")

    led = Ledger(work="测试作品")
    led.apply(
        chapter_id="c1",
        chapter_no=1,
        foreshadows=[{"动作": "埋设", "编号": "", "描述": "甲"}, {"动作": "埋设", "编号": "", "描述": "乙"}],
    )
    led.apply(chapter_id="c2", chapter_no=2, foreshadows=[{"动作": "回收", "编号": "F-002", "描述": "乙解决"}])

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "20-kb" / "k2-material" / "foreshadow-ledger.json"
        led.save(path, stale_threshold=30)
        check(path.exists(), "文件写出")

        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        check(raw["next_seq"] == 3, "next_seq 被持久化（下次不会重号）")
        check(raw["statistics"]["open"] == 1, "统计写入了未回收数")
        check("stale_items" in raw, "断点清单写入了")

        back = Ledger.load(path)
        check(len(back.items) == 2, "读回条数一致")
        check(back.next_seq == 3, "读回 next_seq")
        check(back.find("F-002").status == "recovered", "读回回收状态")
        check(len(back.find("F-001").events) == 1, "读回事件链")

        # 继续用读回的台账分配，编号不能撞
        report = back.apply(
            chapter_id="c3", chapter_no=3, foreshadows=[{"动作": "埋设", "编号": "", "描述": "丙"}]
        )
        check(report.added == ["F-003"], f"新编号接着排（实际 {report.added}）")


def test_missing_file_is_empty_ledger() -> None:
    print("台账不存在时按空台账起步")

    with tempfile.TemporaryDirectory() as tmp:
        led = Ledger.load(Path(tmp) / "不存在.json")
        check(len(led.items) == 0, "返回空台账而不是报错")
        check(led.next_seq == 1, "编号从 1 开始")


# ── 与提示词的配合（缓存关键） ──────────────────────────────


def test_ledger_context_goes_to_variable_tail() -> None:
    """未回收清单必须进**变量部分**。

    它每章都在变，放进固定前缀会让 prompt 缓存全部失效——
    这会直接抬高整本书的输入成本。
    """
    print("台账上下文属于变量部分（缓存关键）")

    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(ROOT / "work.yaml")
    prefix = build_stable_prefix(primitives, work)

    led = Ledger()
    led.apply(chapter_id="c1", chapter_no=1, foreshadows=[{"动作": "埋设", "编号": "", "描述": "神秘信物"}])
    ctx = led.context_for_prompt(chapter_no=5)

    tail_with = build_variable_tail(
        chapter_label="第5章", chapter_text="正文", open_foreshadows=ctx
    )
    tail_without = build_variable_tail(
        chapter_label="第5章", chapter_text="正文", open_foreshadows=[]
    )

    check("F-001" in tail_with, "编号出现在变量尾巴里")
    check("神秘信物" in tail_with, "描述出现在变量尾巴里")
    check("未回收" in tail_with, "有明确的段落标题")
    check("F-001" not in prefix, "固定前缀里没有伏笔编号")
    check("神秘信物" not in prefix, "固定前缀里没有伏笔内容")
    check("第1章埋设" not in prefix, "固定前缀里没有台账的元信息")

    # 不同章节的上下文不同，但固定前缀必须完全一致
    ctx6 = led.context_for_prompt(chapter_no=6)
    tail6 = build_variable_tail(
        chapter_label="第6章", chapter_text="另一段正文", open_foreshadows=ctx6
    )
    check(tail_with != tail6, "变量部分确实随章变化")
    check(
        build_stable_prefix(primitives, work) == prefix,
        "固定前缀不受台账内容影响（缓存不会失效）",
    )

    # 空台账也要有明确提示，否则模型可能以为清单就是空的
    check("没有未回收的伏笔" in tail_without, "空台账给出明确提示")


def main() -> int:
    print("=" * 58)
    print("伏笔台账自检")
    print("=" * 58)

    for fn in (
        test_plan_assigns_id_and_writes_back,
        test_advance_and_recover,
        test_unknown_reference_not_lost,
        test_action_on_recovered_blocked,
        test_meaningless_foreshadow_ignored,
        test_partial_bad_items_do_not_kill_whole_list,
        test_context_excludes_recovered_and_shows_gap,
        test_stale_detection,
        test_rerun_same_chapter_is_idempotent,
        test_rerun_reuses_planted_id,
        test_rerun_distinguishes_different_foreshadow,
        test_rerun_undoes_this_chapters_recovery,
        test_rerun_prunes_unconfirmed_plants,
        test_roundtrip,
        test_missing_file_is_empty_ledger,
        test_ledger_context_goes_to_variable_tail,
    ):
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
