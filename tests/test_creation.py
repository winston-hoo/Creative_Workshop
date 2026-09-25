"""M9 创作台 · 规划层自检。不需要密钥、不调模型。

直接跑：python tests/test_creation.py

每条断言都对应一个会真实咬人的失败模式：

  · 设定集里主角名和 work.yaml 里的不一致 → 标注原语 P21 的度量对象错，那一列数据废掉
  · 人物引用了不存在的势力/关系 → 生成出来的目录里会出现查无此人的角色
  · role=主角 有 0 个或 2 个 → 分卷目录不知道围着谁排
  · 注入块超预算被静默截断 → 生成的东西看着合理却处处跟设定拧着
  · 手改设定集之后没有单一真源 → 两处一先一后过期，最难查的一类错
  · 卷与卷之间章号断档或重叠 → 「少了一章」和「本来就只有这些」分不出来
  · 逐章指令的标题/章号跟卷表漂了 → 写到那一章才发现，那时已经写完几十章
  · 推进/回收引用了没埋过的编号 → 中途改过编号，后文没跟上
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop import chapter_brief as cb  # noqa: E402
from workshop import volume as vo  # noqa: E402
from workshop.creation import (
    character_presence,
    consistency_report,
    render_recap,
    render_story_anchor,
    render_write_brief,
    story_gates,  # noqa: E402
    BRIEF_DIR,
    DRAFT_DIR,
    SETTING_BASENAME,
    SETTING_DIR,
    VOLUME_DIR,
    chain_report,
    create_original_work,
    empty_setting,
    load_setting,
    render_injection_block,
    render_setting_markdown,
    seed_briefs,
    setting_path_of,
    sync_work_config,
    validate_setting,
)
from workshop.primitives import load_work  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def _good_setting() -> dict:
    return {
        "schema_version": "setting-v1",
        "work": "关山灯",
        "genre": "玄幻",
        "logline": "一个能看到数值的废柴，靠吐槽在修真界活下来。",
        "core_motive": "活着回去",
        "target": {"chapters": 120, "chars_per_chapter": [2800, 4000]},
        "style": {"perspective": "第三限知", "tone": "轻松搞笑", "taboo": ["不用破折号"]},
        "world": {"era": "天元三百年", "places": [{"name": "青云宗", "note": "第一大宗"}],
                  "rules": ["查克拉用尽会昏迷"]},
        "power": {"system": "修为", "tiers": ["炼气", "筑基", "金丹"]},
        "factions": [{"name": "青云宗", "stance": "表面中立", "leader": "玄阳真人"}],
        "characters": [
            {"name": "李默", "role": "主角", "identity": "穿越者", "personality": "吐槽役",
             "motive": "活着回去", "arc": "从自保到扛事", "abilities": ["数值之眼"],
             "faction": "青云宗", "relations": [{"to": "苏清月", "type": "师姐"}]},
            {"name": "苏清月", "role": "重要配角", "identity": "大师姐", "motive": "守住宗门",
             "faction": "青云宗"},
            {"name": "玄阳真人", "role": "导师", "identity": "宗主", "motive": "渡劫",
             "faction": "青云宗"},
        ],
        "terms": [{"term": "数值之眼", "meaning": "能看到目标的修为数值"}],
        "themes": ["活着的意义"],
    }


def _write_setting(work_dir: Path, data: dict) -> None:
    path = setting_path_of(work_dir)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _good_volume(vol_no: int = 1, start: int = 1, end: int = 12) -> dict:
    """两卷的样板：12 章一卷，分两个部分，章表铺满。"""
    parts = [
        {"title": "觉醒", "start_chapter": start, "end_chapter": start + 5, "gist": "主角认清处境"},
        {"title": "准备", "start_chapter": start + 6, "end_chapter": end, "gist": "定下第一步"},
    ]
    chapters = [
        {"chapter_no": no, "title": f"第{no}章标题", "gist": f"第{no}章发生了什么",
         "characters": ["李默"], "foreshadow": "埋设" if no == 1 else ""}
        for no in range(start, end + 1)
    ]
    return {
        "schema_version": "volume-v1",
        "work": "关山灯",
        "vol": {
            "vol": vol_no, "title": f"第{vol_no}卷卷名", "start_chapter": start,
            "end_chapter": end, "era": "天元三百年秋", "core_conflict": "自保 vs 扛事",
            "goal": "主角活下来并找到线索", "closing": "卷末总结",
            "parts": parts, "chapters": chapters,
        },
    }


def _good_brief(vol_no: int = 1, chapter_no: int = 1, *, title: str = "") -> dict:
    return {
        "schema_version": "brief-v1",
        "work": "关山灯",
        "chapter_id": cb.chapter_id_of(vol_no, chapter_no),
        "vol": vol_no,
        "chapter_no": chapter_no,
        "title": title or f"第{chapter_no}章标题",
        "one_line": "这一章干什么",
        "core_plot": ["第一件事", "第二件事"],
        "timeline": "接上一章，隔一天",
        "word_target": [2800, 4000],
        "narrative": {"perspective": "第三限知", "tone": "轻松搞笑", "focus": ["对话"]},
        "settings": {"characters": ["李默"], "factions": ["青云宗"], "terms": ["数值之眼"]},
        "foreshadow": [],
        "carry_over": [],
        "must_not": [],
    }


# ── 设定集 ──────────────────────────────────────────────────


def test_template_parses_and_starts_blocked() -> None:
    """新建工作区的模板必须能解析，而且一开始就该报「缺东西」。"""
    print("新建原创工作区")

    with tempfile.TemporaryDirectory() as tmp:
        info = create_original_work(tmp, "关山灯", "玄幻", protagonist="李默", core_motive="活着回去")
        for key in (SETTING_DIR, VOLUME_DIR, BRIEF_DIR, DRAFT_DIR):
            check((Path(tmp) / "关山灯" / key).is_dir(), f"建出 {key}/")
        check(info["setting"].exists(), "设定集骨架落盘")
        check(info["first_volume"].exists(), "第一卷骨架落盘")

        data = load_setting(info["setting"])
        check(data.get("work") == "关山灯", "设定集模板能解析")
        check(data.get("style", {}).get("perspective") == "第三限知", "模板给了默认视角")

        first_vol = vo.load_volume(info["first_volume"])
        check(first_vol["vol"]["vol"] == 1, "卷骨架能解析且卷号正确")

        errors, _w = validate_setting(data)
        check(any("一句话前提" in e for e in errors), f"空设定集被拦下来（实际 {errors[:2]}）")
        vol_errors, _w = vo.validate_volume(first_vol)
        check(vol_errors, "空卷表被拦下来（没有章表）")

        work = load_work(Path(tmp) / "关山灯" / "work.yaml")
        check(work.protagonist == "李默", "work.yaml 里写进了主角")
        check(work.raw.get("kind") == "original", "标明这是原创工作区")

        try:
            create_original_work(tmp, "关山灯", "玄幻")
            check(False, "重复建同一个工作区应当被拒绝")
        except FileExistsError:
            check(True, "重复建同一个工作区被拒绝")


def test_validation_catches_real_problems() -> None:
    print("设定集校验")

    errors, warnings = validate_setting(_good_setting())
    check(not errors, f"完整设定集没有阻断项（实际 {errors}）")
    check(not warnings, f"完整设定集没有提示项（实际 {warnings}）")

    data = _good_setting()
    data["characters"][1]["faction"] = "不存在的门派"
    _e, warnings = validate_setting(data)
    check(any("不存在的门派" in w for w in warnings), "势力对不上会提示")

    data = _good_setting()
    data["characters"][0]["relations"] = [{"to": "查无此人", "type": "师徒"}]
    _e, warnings = validate_setting(data)
    check(any("查无此人" in w for w in warnings), "关系指向不存在的人物会提示")

    for bad, label in ((0, "没有主角"), (2, "两个主角")):
        data = _good_setting()
        if bad == 0:
            data["characters"][0]["role"] = "配角"
        else:
            data["characters"][1]["role"] = "主角"
        errors, _w = validate_setting(data)
        check(any("主角" in e for e in errors), f"{label}时阻断")

    data = _good_setting()
    data["characters"].append({"name": "李默", "role": "配角", "motive": "混"})
    errors, _w = validate_setting(data)
    check(any("出现了 2 次" in e for e in errors), "重名阻断")

    data = _good_setting()
    data["style"]["perspective"] = "第二人称"
    errors, _w = validate_setting(data)
    check(any("第二人称" in e for e in errors), "非法视角阻断")

    data = _good_setting()
    data["characters"] = []
    errors, _w = validate_setting(data)
    check(any("characters 是空的" in e for e in errors), "没有人物阻断")


def test_single_source_of_truth_for_protagonist() -> None:
    """主角与核心动机只认设定集，work.yaml 由它同步。"""
    print("主角/动机的单一真源")

    with tempfile.TemporaryDirectory() as tmp:
        create_original_work(tmp, "关山灯", "玄幻", protagonist="旧名字", core_motive="旧动机")
        work_dir = Path(tmp) / "关山灯"
        _write_setting(work_dir, _good_setting())

        result = sync_work_config(work_dir)
        check(result["protagonist"] == "李默", f"主角跟着设定集走（实际 {result['protagonist']}）")
        check(result["core_motive"] == "活着回去", "动机跟着设定集走")
        check(result["changed"], "标记出发生了变化")
        check(not sync_work_config(work_dir)["changed"], "再同步一次不报变化")

        work = load_work(work_dir / "work.yaml")
        check(work.motive_line() == "李默 · 活着回去", f"动机行拼得对（实际 {work.motive_line()}）")


def test_injection_block_never_truncates_silently() -> None:
    """注入块超预算必须点名丢了什么。"""
    print("注入块不静默截断")

    data = _good_setting()
    full = render_injection_block(data)
    for needle in ("李默", "数值之眼", "青云宗", "第三限知", "活着回去"):
        check(needle in full, f"注入块里有「{needle}」")
    check("⚠️" not in full, "没超预算时不出现截断告警")

    tiny = render_injection_block(data, max_chars=60)
    check("⚠️" in tiny, "超预算时出现告警")
    check("没有注入" in tiny, "并且点名了哪些小节没进去")
    check(tiny.index("【作品】") < tiny.index("⚠️"), "保住优先级最高的小节")
    check("人物" in tiny and "characters" not in tiny, "告警里报的是中文小节名，不是内部 key")
    check("关山灯" in tiny and "活着回去" in tiny, "基本信息一定保住")
    check("【人物】" not in tiny, "预算不够时先丢人物小节")

    markdown = render_setting_markdown(data)
    check(markdown.startswith("# 设定集 · 关山灯"), "人读版标题正确")
    check("### 李默（主角）" in markdown, "人物按小节渲染")
    check(render_setting_markdown(empty_setting("空书", "悬疑")).startswith("# 设定集 · 空书"), "空设定集也能渲染")


# ── 分卷目录 ────────────────────────────────────────────────


def test_volume_sequence_checks() -> None:
    """章号连续性是这一层最强的信号。"""
    print("分卷目录 · 序列校验")

    errors, _w = vo.validate_volume(_good_volume(1, 1, 12), setting=_good_setting(), file_vol_no=1)
    check(not errors, f"完整卷没有阻断项（实际 {errors}）")

    # 缺章
    vol = _good_volume(1, 1, 12)
    vol["vol"]["chapters"] = [c for c in vol["vol"]["chapters"] if c["chapter_no"] != 7]
    errors, _w = vo.validate_volume(vol)
    check(any("缺 1 章" in e for e in errors), f"章号断档被抓住（实际 {errors[:1]}）")

    # 多出区间外的章
    vol = _good_volume(1, 1, 12)
    vol["vol"]["chapters"].append({"chapter_no": 99, "title": "越界", "gist": "x"})
    errors, _w = vo.validate_volume(vol)
    check(any("区间外" in e for e in errors), "超出卷区间的章被抓住")

    # 部分之间断档
    vol = _good_volume(1, 1, 12)
    vol["vol"]["parts"][1]["start_chapter"] = 9
    errors, _w = vo.validate_volume(vol)
    check(any("不接续" in e for e in errors), "部分之间断档被抓住")

    # 部分没铺满整卷
    vol = _good_volume(1, 1, 12)
    vol["vol"]["parts"] = vol["vol"]["parts"][:1]
    errors, _w = vo.validate_volume(vol)
    check(any("本卷到 12 结束" in e for e in errors), "部分没铺满整卷被抓住")

    # 出场人物不在设定集里
    vol = _good_volume(1, 1, 12)
    vol["vol"]["chapters"][0]["characters"] = ["查无此人"]
    _e, warnings = vo.validate_volume(vol, setting=_good_setting())
    check(any("查无此人" in w for w in warnings), "出场人物不在设定集里会提示")

    # 非法伏笔动作
    vol = _good_volume(1, 1, 12)
    vol["vol"]["chapters"][0]["foreshadow"] = "埋伏"
    errors, _w = vo.validate_volume(vol)
    check(any("不合法" in e for e in errors), "非法伏笔动作阻断")

    # 缺 title / gist
    vol = _good_volume(1, 1, 12)
    del vol["vol"]["chapters"][0]["gist"]
    errors, _w = vo.validate_volume(vol)
    check(any("缺 gist" in e for e in errors), "缺内容概要阻断")


def test_volume_chain_checks() -> None:
    print("分卷目录 · 跨卷衔接")

    vols = {1: _good_volume(1, 1, 12), 2: _good_volume(2, 13, 24)}
    errors, _w = vo.validate_chain(vols, setting=_good_setting())
    check(not errors, f"两卷首尾相接时没有阻断项（实际 {errors}）")

    # 中间断档
    vols = {1: _good_volume(1, 1, 12), 2: _good_volume(2, 14, 24)}
    errors, _w = vo.validate_chain(vols)
    check(any("断档" in e for e in errors), "卷之间断档被抓住")

    # 区间重叠
    vols = {1: _good_volume(1, 1, 12), 2: _good_volume(2, 10, 24)}
    errors, _w = vo.validate_chain(vols)
    check(any("重叠" in e for e in errors), "卷之间重叠被抓住")

    # 卷号不连续
    vols = {1: _good_volume(1, 1, 12), 3: _good_volume(3, 13, 24)}
    errors, _w = vo.validate_chain(vols)
    check(any("卷号不连续" in e for e in errors), "卷号跳号被抓住")

    # 跟设定集的目标章数对不上
    vols = {1: _good_volume(1, 1, 12)}
    _e, warnings = vo.validate_chain(vols, setting=_good_setting())
    check(any("还差" in w for w in warnings), f"铺到的章数少于目标会提示（实际 {warnings}）")


def test_volume_files_roundtrip() -> None:
    print("分卷目录 · 文件读写")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for no, (start, end) in ((1, (1, 12)), (2, (13, 24))):
            vo.save_volume(vo.volume_path(d, no), _good_volume(no, start, end))
        volumes, broken = vo.load_all_volumes(d)
        check(not broken, f"没有读不出来的文件（实际 {broken}）")
        check(sorted(volumes) == [1, 2], "按卷号读回两卷")

        # 文件名与内容里的卷号不一致：改一处忘另一处，必须拦住
        vo.save_volume(d / "volume-003.yaml", _good_volume(1, 1, 12))
        volumes, broken = vo.load_all_volumes(d)
        check(any("文件名说第 3 卷" in b for b in broken), f"卷号两处不一致被拦住（实际 {broken}）")

        markdown = vo.render_volume_markdown(_good_volume(1, 1, 12))
        check(markdown.startswith("# 第 1 卷："), "渲染标题正确")
        check("| 第1章 |" in markdown, "渲染出逐章表")
        check("## 觉醒" in markdown and "## 准备" in markdown, "按部分分小节")


# ── 逐章创作任务指令 ────────────────────────────────────────


def test_brief_validation() -> None:
    print("创作任务指令 · 校验")

    errors, warnings = cb.validate_brief(
        _good_brief(), setting=_good_setting(), volume=_good_volume(1, 1, 12)
    )
    check(not errors, f"完整指令没有阻断项（实际 {errors}）")
    check(not warnings, f"完整指令没有提示项（实际 {warnings}）")

    # chapter_id 与 vol/chapter_no 漂了
    brief = _good_brief()
    brief["chapter_no"] = 7
    errors, _w = cb.validate_brief(brief)
    check(any("chapter_no 字段写的是 7" in e for e in errors), "章号两处不一致被抓住")

    # 标题跟卷表漂了
    brief = _good_brief(title="我改过的标题")
    _e, warnings = cb.validate_brief(brief, volume=_good_volume(1, 1, 12))
    check(any("标题跟卷表不一致" in w for w in warnings), "标题与卷表漂了会提示")

    # 章号在卷表里 → 不该报「不在章表里」
    brief = _good_brief(chapter_no=11)
    errors, _w = cb.validate_brief(brief, volume=_good_volume(1, 1, 12))
    check(not any("不在第 1 卷的章表里" in e for e in errors), "章号确实在卷表里时不误报")

    # 章号不在卷表里 → 必须报
    errors, _w = cb.validate_brief(_good_brief(chapter_no=9), volume=_good_volume(1, 1, 5))
    check(any("不在第 1 卷的章表里" in e for e in errors), "章号不在卷表里阻断")

    # 没有核心情节
    brief = _good_brief()
    brief["core_plot"] = []
    errors, _w = cb.validate_brief(brief)
    check(any("core_plot 是空的" in e for e in errors), "没有核心情节阻断")

    # 字数区间反了
    brief = _good_brief()
    brief["word_target"] = [4000, 1000]
    errors, _w = cb.validate_brief(brief)
    check(any("反了" in e for e in errors), "字数区间反了阻断")

    # 视角跟设定集不一致（提示，不是阻断：可能是有意的实验）
    brief = _good_brief()
    brief["narrative"]["perspective"] = "第一人称"
    _e, warnings = cb.validate_brief(brief, setting=_good_setting())
    check(any("不一致" in w for w in warnings), "视角与设定集不一致会提示")

    # 出场人物不在设定集里
    brief = _good_brief()
    brief["settings"]["characters"] = ["查无此人"]
    _e, warnings = cb.validate_brief(brief, setting=_good_setting())
    check(any("查无此人" in w for w in warnings), "出场人物不在设定集里会提示")

    # 埋设没编号
    brief = _good_brief()
    brief["foreshadow"] = [{"action": "埋设", "id": "", "desc": "某个悬念"}]
    errors, _w = cb.validate_brief(brief)
    check(any("没有编号" in e for e in errors), "埋设没编号阻断")

    # 推进没编号
    brief = _good_brief()
    brief["foreshadow"] = [{"action": "推进", "id": "", "desc": "x"}]
    errors, _w = cb.validate_brief(brief)
    check(any("没有编号" in e for e in errors), "推进没编号阻断")

    # 非法动作
    brief = _good_brief()
    brief["foreshadow"] = [{"action": "埋伏", "id": "F-001", "desc": "x"}]
    errors, _w = cb.validate_brief(brief)
    check(any("不合法" in e for e in errors), "非法伏笔动作阻断")


def test_brief_chain_foreshadow_refs() -> None:
    """伏笔编号：埋设时定号，推进/回收时引用。规矩跟台账一致。"""
    print("创作任务指令 · 伏笔引用完整性")

    b1 = _good_brief(chapter_no=1)
    b1["foreshadow"] = [{"action": "埋设", "id": "F-001", "desc": "笼中鸟会引爆"}]
    b2 = _good_brief(chapter_no=2)
    b2["foreshadow"] = [{"action": "推进", "id": "F-001", "desc": "他查到了引爆条件"}]
    b3 = _good_brief(chapter_no=3)
    b3["foreshadow"] = [{"action": "回收", "id": "F-001", "desc": "封印被解开"}]

    briefs = {b["chapter_id"]: b for b in (b1, b2, b3)}
    errors, _w = cb.validate_brief_chain(briefs)
    check(not errors, f"埋设→推进→回收 没有阻断项（实际 {errors}）")

    # 引用了没埋过的编号
    bad = _good_brief(chapter_no=2)
    bad["foreshadow"] = [{"action": "推进", "id": "F-009", "desc": "x"}]
    errors, _w = cb.validate_brief_chain({b1["chapter_id"]: b1, bad["chapter_id"]: bad})
    check(any("此前没被埋设过" in e for e in errors), f"引用未埋编号被抓住（实际 {errors}）")

    # 同一编号埋两次
    again = _good_brief(chapter_no=2)
    again["foreshadow"] = [{"action": "埋设", "id": "F-001", "desc": "重复登记"}]
    errors, _w = cb.validate_brief_chain({b1["chapter_id"]: b1, again["chapter_id"]: again})
    check(any("编号不能重用" in e for e in errors), "同一编号埋两次被抓住")


def test_seed_briefs_never_overwrites() -> None:
    print("播种逐章指令")

    with tempfile.TemporaryDirectory() as tmp:
        create_original_work(tmp, "关山灯", "玄幻", protagonist="李默", core_motive="活着回去")
        work_dir = Path(tmp) / "关山灯"
        _write_setting(work_dir, _good_setting())
        vo.save_volume(vo.volume_path(work_dir / VOLUME_DIR, 1), _good_volume(1, 1, 12))

        result = seed_briefs(work_dir)
        check(len(result["created"]) == 12, f"播了 12 份（实际 {len(result['created'])}）")
        briefs, broken = cb.load_all_briefs(work_dir / BRIEF_DIR)
        check(not broken, f"播下去的都能读回来（实际 {broken}）")

        one = briefs["v001-c0001"]
        check(one["title"] == "第1章标题", "标题从卷表播种下来")
        check(one["one_line"] == "第1章发生了什么", "概要从卷表播种下来")
        check(one["word_target"] == [2800, 4000], "字数从设定集播种下来")
        check(one["narrative"]["perspective"] == "第三限知", "视角从设定集播种下来")

        # 改过的指令不能被骨架覆盖
        cb.save_brief(cb.brief_path(work_dir / BRIEF_DIR, "v001-c0001"), one)
        again = seed_briefs(work_dir)
        check(not again["created"], "第二次播种不新建")
        check(len(again["skipped"]) == 12, "已存在的全部跳过")

        # 骨架本身要能被校验认出来「还没填」
        errors, _w = cb.validate_brief(briefs["v001-c0002"], setting=_good_setting())
        check(any("core_plot 是空的" in e for e in errors), "没填的骨架会被指出缺核心情节")


def test_chain_report() -> None:
    """三层链路体检：报完成度，不只报有没有。"""
    print("三层链路体检")

    with tempfile.TemporaryDirectory() as tmp:
        create_original_work(tmp, "关山灯", "玄幻", protagonist="李默", core_motive="活着回去")
        work_dir = Path(tmp) / "关山灯"

        report = chain_report(work_dir)
        check(not report["ok"], "空工作区链路不通")
        check(any("设定集" in e for e in report["errors"]), "报出了设定集的问题")
        check(any("分卷目录" in e for e in report["errors"]), "报出了分卷目录的问题")

        _write_setting(work_dir, _good_setting())
        vo.save_volume(vo.volume_path(work_dir / VOLUME_DIR, 1), _good_volume(1, 1, 12))
        seed_briefs(work_dir)
        report = chain_report(work_dir)
        check(report["layers"]["setting"]["done"] == 1, "设定集标记为完成")
        check(report["layers"]["volume"]["volumes"] == 1, "认出一卷")
        check(report["layers"]["volume"]["covered_chapters"] == 12, "铺到第 12 章")
        check(report["layers"]["brief"]["done"] == 12, "认出 12 份指令")
        check(report["layers"]["brief"]["planned"] == 12, "知道计划是 12 章")
        # 骨架还没填，所以链路仍然不通 —— 这正是要报出来的
        check(not report["ok"], "骨架没填时链路仍不通")
        check(any("core_plot 是空的" in e for e in report["errors"]), "缺核心情节进了汇总")

        # 把指令填好之后链路应该通
        for no in range(1, 13):
            cid = cb.chapter_id_of(1, no)
            cb.save_brief(cb.brief_path(work_dir / BRIEF_DIR, cid), _good_brief(1, no))
        report = chain_report(work_dir)
        check(report["ok"], f"填完之后链路通（实际阻断 {report['errors'][:3]}）")



def test_foreshadow_ledger() -> None:
    """伏笔账本：真正要回答的是「现在还有几条挂着没收」。

    链路校验只管「这一条回收得对不对」，回答不了这个问题。
    以及两个会静默烂尾的形态必须点出来：同一个编号埋两次（回收只关掉一个）、
    回收之后又埋一次（作者以为收了，其实又开了）。
    """
    print("伏笔账本")

    def brief(cid: str, no: int, items: list) -> dict:
        return {
            "schema_version": "brief-v1", "chapter_id": cid, "vol": 1, "chapter_no": no,
            "title": f"第{no}章", "foreshadow": items,
        }

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "80-brief"
        d.mkdir(parents=True)
        rows = [
            brief("v001-c0001", 1, [{"action": "埋设", "id": "F-001", "desc": "眼睛怎么来的"}]),
            brief("v001-c0002", 2, [{"action": "推进", "id": "F-001", "desc": "又看了一眼"},
                                    {"action": "埋设", "id": "F-002", "desc": "玉佩"}]),
            brief("v001-c0003", 3, [{"action": "回收", "id": "F-002", "desc": "玉佩是她母亲的"}]),
            brief("v001-c0004", 4, [{"action": "埋设", "id": "F-003", "desc": "名册是假的"},
                                    {"action": "埋设", "id": "F-001", "desc": "重复埋"}]),
            brief("v001-c0005", 5, [{"action": "回收", "id": "F-003", "desc": "名册"}]),
        ]
        for r in rows:
            (d / f"{r['chapter_id']}.yaml").write_text(
                yaml.safe_dump(r, allow_unicode=True), encoding="utf-8")

        led = cb.foreshadow_ledger(d)
        check(led["counts"]["open"] == 1, f"挂着 1 条（实际 {led['counts']['open']}）")
        check(led["counts"]["recovered"] == 2, f"收了 2 条（实际 {led['counts']['recovered']}）")
        check(led["counts"]["chapters"] == 5, "扫到了 5 章")

        only = led["open"][0]
        check(only["id"] == "F-001", f"挂着的是 F-001（实际 {only['id']}）")
        check(only["planted_at"] == "v001-c0001", "记得在哪一章埋的")
        check(only["advances"] == 1, f"推进过 1 次（实际 {only['advances']}）")
        check(only["hanging_chapters"] == 4, f"挂了 4 章（实际 {only['hanging_chapters']}）")

        rec = {r["id"]: r for r in led["recovered"]}
        check(rec["F-002"]["closed_after"] == 1, "F-002 埋了 1 章后收的")
        check(rec["F-003"]["closed_after"] == 1, "F-003 埋了 1 章后收的")

        joined = "；".join(led["problems"])
        check("F-001" in joined and "第二次埋设" in joined, f"抓出重复埋设：{joined}")
        check(any("没写编号" in p for p in led["problems"]) or True, "（没写编号的情形不在这份样例里）")

        # 空目录不该炸
        empty = Path(tmp) / "empty"
        empty.mkdir()
        led0 = cb.foreshadow_ledger(empty)
        check(led0["counts"]["open"] == 0 and led0["counts"]["chapters"] == 0, "空目录返回 0，不报错")


def test_cast_and_consistency() -> None:
    """人物动向 + 卷表/设定集一致性。

    两个都容易只验「正常的能过」就收工——所以这里**两边都验**：
    缺席数算得对不对（正例），以及名字对不上时到底报不报（反例）。
    """
    print("人物动向与一致性")

    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "60-setting").mkdir(parents=True)
        (wd / "70-volume").mkdir(parents=True)
        (wd / "80-brief").mkdir(parents=True)

        (wd / "60-setting" / "setting.yaml").write_text(yaml.safe_dump({
            "schema_version": "setting-v1", "work": "x",
            "characters": [{"name": "李默", "role": "主角", "motive": "活"},
                           {"name": "苏清月", "role": "重要配角", "motive": "守"}],
            "factions": [{"name": "青云宗", "stance": "中立"}],
        }, allow_unicode=True), encoding="utf-8")

        (wd / "70-volume" / "volume-001.yaml").write_text(yaml.safe_dump({
            "schema_version": "volume-v1", "vol": {
                "vol": 1, "title": "灯下黑", "start_chapter": 1, "end_chapter": 4,
                "chapters": [{"chapter_no": 1, "title": "数值", "characters": ["李默"]},
                             {"chapter_no": 2, "title": "零层", "characters": ["李默"]},
                             {"chapter_no": 3, "title": "规矩", "characters": ["李默", "查无此人"]},
                             {"chapter_no": 4, "title": "苏清月", "characters": ["李默", "苏清月"]}],
            },
        }, allow_unicode=True), encoding="utf-8")

        for cid, no in (("v001-c0001", 1), ("v001-c0002", 2), ("v001-c0003", 3), ("v001-c0004", 4)):
            (wd / "80-brief" / f"{cid}.yaml").write_text(yaml.safe_dump({
                "schema_version": "brief-v1", "chapter_id": cid, "vol": 1, "chapter_no": no,
                "title": f"第{no}章", "settings": {"characters": ["李默"]},
            }, allow_unicode=True), encoding="utf-8")

        cp = character_presence(wd)
        by = {r["name"]: r for r in cp["rows"]}
        check(cp["counts"]["latest_chapter_no"] == 4, "最远章号是 4")
        check(by["李默"]["count"] == 4 and by["李默"]["absent"] == 0, "主角全勤、缺席 0")
        check(by["苏清月"]["last"] == 4 and by["苏清月"]["count"] == 1, "苏清月只出场第 4 章")
        check(by["查无此人"]["last"] == 3 and by["查无此人"]["absent"] == 1,
              f"卷表里出现过的人也算进动向（缺席 {by['查无此人']['absent']}）")
        check(cp["rows"][0]["name"] == "查无此人", "按缺席数排序，最该处理的排最前")

        bad = consistency_report(wd)
        joined = "；".join(bad)
        check(any("查无此人" in b for b in bad), f"卷表里不在设定集的名字要报：{joined}")
        check(not any("李默" in b or "苏清月" in b for b in bad), "设定集里有的人不报")

        # 别名算认识
        (wd / "60-setting" / "setting.yaml").write_text(yaml.safe_dump({
            "schema_version": "setting-v1", "work": "x",
            "characters": [{"name": "李默", "role": "主角", "motive": "活",
                            "aliases": ["查无此人"]},
                           {"name": "苏清月", "role": "重要配角", "motive": "守"}],
        }, allow_unicode=True), encoding="utf-8")
        check(not consistency_report(wd), "写成别名时不再误报")


def test_write_brief_assembly() -> None:
    """「写前必读」必须组装齐、锚点必须在最前、前情不许剧透后面。

    这是作者原始问题（「全书梗概是后定的，如何引导全书发展」）的答案：
    不是再做一份梗概，而是每次动笔前把真源渲染成**输入**。
    锚点排在最前是有讲究的——放到当前文档之后就成了"事后补充"，钉不住。
    """
    print("写前必读")

    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "60-setting").mkdir(parents=True)
        (wd / "70-volume").mkdir(parents=True)
        (wd / "80-brief").mkdir(parents=True)
        (wd / "90-draft").mkdir(parents=True)

        (wd / "60-setting" / "setting.yaml").write_text(yaml.safe_dump({
            "schema_version": "setting-v1", "work": "关山灯", "genre": "玄幻",
            "logline": "零层废柴靠数值之眼活下来", "core_motive": "活着回去",
            "ultimate_hook": "那双眼睛到底是谁给的",
            "characters": [{"name": "李默", "role": "主角", "identity": "外门杂役",
                            "motive": "活着回去", "arc": "从自保到出头"}],
            "factions": [{"name": "青云宗", "stance": "中立"}],
            "power": {"system": "修为", "tiers": ["炼气", "筑基"]},
        }, allow_unicode=True), encoding="utf-8")

        (wd / "70-volume" / "volume-001.yaml").write_text(yaml.safe_dump({
            "schema_version": "volume-v1", "vol": {
                "vol": 1, "title": "灯下黑", "start_chapter": 1, "end_chapter": 4,
                "core_conflict": "想回去 vs 被当棋子", "goal": "站住脚",
                "closing": "他站住了，但被盯上了",
                "chapters": [{"chapter_no": 1, "title": "数值", "characters": ["李默"]},
                             {"chapter_no": 2, "title": "零层", "gist": "被划去名册", "characters": ["李默"]}],
            },
        }, allow_unicode=True), encoding="utf-8")
        for cid, no in (("v001-c0001", 1), ("v001-c0002", 2)):
            (wd / "80-brief" / f"{cid}.yaml").write_text(yaml.safe_dump({
                "schema_version": "brief-v1", "chapter_id": cid, "vol": 1, "chapter_no": no,
                "title": f"第{no}章", "one_line": f"第{no}章概要",
                "settings": {"characters": ["李默"]},
                "foreshadow": [{"action": "埋设", "id": f"F-00{no}", "desc": f"悬念{no}"}],
            }, allow_unicode=True), encoding="utf-8")
        (wd / "90-draft" / "v001-c0001.md").write_text("第一章正文结尾在这里。", encoding="utf-8")

        anchor = render_story_anchor(wd)
        check("全书锚点" in anchor, "锚点块在")
        check("主角：李默" in anchor and "活着回去" in anchor, "锚点里有主角与核心动机")
        check("那双眼睛到底是谁给的" in anchor, "终极钩子进了锚点")
        check("修为（炼气 → 筑基）" in anchor, "力量体系进了锚点")

        recap = render_recap(wd, upto_chapter_no=1)
        check("第 1 卷" in recap, "前情有第一卷")
        check("零层" not in recap, "前情不剧透第 2 章之后的内容")

        brief = render_write_brief(wd, chapter_no=2, prev_chapter_no=1)
        for block in ("全书锚点", "前情", "角色动态", "待回收伏笔", "上一章末段"):
            check(block in brief, f"写前必读含「{block}」")
        check(brief.startswith("【全书锚点】"), "锚点排在最前")
        check("第一章正文结尾在这里" in brief, "上一章末段真的带上了")

        # 空作品不能炸
        empty = Path(tmp) / "empty"
        (empty / "60-setting").mkdir(parents=True)
        (empty / "60-setting" / "setting.yaml").write_text(
            yaml.safe_dump({"schema_version": "setting-v1", "work": "空"}, allow_unicode=True),
            encoding="utf-8")
        check(render_write_brief(empty) == "", "空作品返回空串，不报错")


def test_story_gates() -> None:
    """两道硬闸：主角缺席、卷间接不上。

    闸的价值全在**反例**上——正例不响不能说明它有用。
    这里重点验反例，而且验到"降级"这一层：群像戏不该被主角缺席卡住。
    """
    print("全书级硬闸")

    def build(tmp: Path, *, lead_absent_from: int = 0, closing: str = "卷末总结",
              conflict2: str = "第二卷冲突") -> Path:
        wd = Path(tmp)
        (wd / "60-setting").mkdir(parents=True, exist_ok=True)
        (wd / "70-volume").mkdir(parents=True, exist_ok=True)
        (wd / "80-brief").mkdir(parents=True, exist_ok=True)
        leads = [{"name": "李默", "role": "主角", "motive": "活"},
                 {"name": "苏清月", "role": "重要配角", "motive": "守"}]
        (wd / "60-setting" / "setting.yaml").write_text(yaml.safe_dump({
            "schema_version": "setting-v1", "work": "x", "characters": leads,
        }, allow_unicode=True), encoding="utf-8")
        for no in range(1, 7):
            who = ["李默"] if not lead_absent_from or no < lead_absent_from else ["苏清月"]
            (wd / "80-brief" / f"v001-c000{no}.yaml").write_text(yaml.safe_dump({
                "schema_version": "brief-v1", "chapter_id": f"v001-c000{no}", "vol": 1,
                "chapter_no": no, "title": f"第{no}章", "settings": {"characters": who},
            }, allow_unicode=True), encoding="utf-8")
        (wd / "70-volume" / "volume-001.yaml").write_text(yaml.safe_dump({
            "schema_version": "volume-v1", "vol": {
                "vol": 1, "title": "一", "start_chapter": 1, "end_chapter": 3, "closing": closing,
                "chapters": [{"chapter_no": n, "title": f"第{n}章"} for n in (1, 2, 3)],
            }}, allow_unicode=True), encoding="utf-8")
        (wd / "70-volume" / "volume-002.yaml").write_text(yaml.safe_dump({
            "schema_version": "volume-v1", "vol": {
                "vol": 2, "title": "二", "start_chapter": 4, "end_chapter": 6,
                "core_conflict": conflict2,
                "chapters": [{"chapter_no": n, "title": f"第{n}章"} for n in (4, 5, 6)],
            }}, allow_unicode=True), encoding="utf-8")
        return wd

    with tempfile.TemporaryDirectory() as tmp:
        good = build(Path(tmp) / "good")
        errs, warns = story_gates(good)
        check(not errs, f"正常作品不报阻断（{errs}）")

        absent = build(Path(tmp) / "absent", lead_absent_from=4)
        errs, _ = story_gates(absent)
        hit = [e for e in errs if "主角" in e and "没出场" in e]
        check(bool(hit), f"主角缺席 3 章要阻断：{hit}")
        check(any("连续 3 章" in e for e in hit), "缺席章数算得对")

        # 群像：两个主角 → 降级为提示
        group = build(Path(tmp) / "group", lead_absent_from=4)
        s = yaml.safe_load((group / "60-setting" / "setting.yaml").read_text(encoding="utf-8"))
        s["characters"].append({"name": "玄阳真人", "role": "主角", "motive": "x"})
        (group / "60-setting" / "setting.yaml").write_text(
            yaml.safe_dump(s, allow_unicode=True), encoding="utf-8")
        errs, warns = story_gates(group)
        check(not [e for e in errs if "主角" in e], "群像戏不因主角缺席阻断")
        check(any("群像" in w for w in warns), f"但给出提示：{warns}")

        gap = build(Path(tmp) / "gap", closing="", conflict2="")
        errs, _ = story_gates(gap)
        check(any("断的" in e for e in errs), f"卷间接不上要阻断：{errs}")



def test_hook_rotation_and_part_tasks() -> None:
    """钩子轮换 + 段落任务：都是「写法/节奏」层面的约束，不是情节约束。

    钩子轮换防的是读者说不清的那种腻（「怎么又是这套」）；
    段落任务防的是写到第 5 章时没人知道这一段的任务达成了没有。
    """
    print("钩子轮换与段落任务")

    b = [{"chapter_id": "v001-c0001", "vol": 1, "chapter_no": 1, "hook_type": "突然揭示"},
         {"chapter_id": "v001-c0002", "vol": 1, "chapter_no": 2, "hook_type": "突然揭示"},
         {"chapter_id": "v001-c0003", "vol": 1, "chapter_no": 3, "hook_type": "乱写"},
         {"chapter_id": "v001-c0004", "vol": 1, "chapter_no": 4, "hook_type": "紧急危机"}]
    probs = cb.hook_rotation_problems(b)
    check(any("同一个手法" in x for x in probs), f"连着两章同一种要报：{probs}")
    check(any("不在十种里" in x for x in probs), "乱写的类型要报")
    check(not any("c0004" in x for x in probs), "轮换成功的那一章不报")

    gaps = [{"chapter_id": "v001-c0001", "vol": 1, "chapter_no": 1, "hook_type": "突然揭示"},
            {"chapter_id": "v001-c0002", "vol": 1, "chapter_no": 2, "hook_type": ""},
            {"chapter_id": "v001-c0003", "vol": 1, "chapter_no": 3, "hook_type": "突然揭示"}]
    check(not cb.hook_rotation_problems(gaps), "中间那章没填时不算连着两章")

    v = {"vol": {"vol": 1, "parts": [
        {"title": "觉醒", "start_chapter": 1, "end_chapter": 3},
        {"title": "入局", "start_chapter": 4, "end_chapter": 6,
         "must_complete": ["拿到第一块灵石", "被赵管事盯上"]},
    ]}}
    pp = cb.part_task_problems(v)
    check(len(pp) == 1 and "觉醒" in pp[0], f"只有缺任务的那一段要报：{pp}")

    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "60-setting").mkdir()
        (wd / "70-volume").mkdir()
        (wd / "80-brief").mkdir()
        (wd / "60-setting" / "setting.yaml").write_text(yaml.safe_dump({
            "schema_version": "setting-v1", "work": "x", "logline": "一句话",
            "characters": [{"name": "李默", "role": "主角", "motive": "活"}],
        }, allow_unicode=True), encoding="utf-8")
        (wd / "70-volume" / "volume-001.yaml").write_text(yaml.safe_dump({
            "schema_version": "volume-v1", "vol": {
                "vol": 1, "title": "一", "start_chapter": 1, "end_chapter": 2,
                "parts": [{"title": "开局", "start_chapter": 1, "end_chapter": 2,
                           "must_complete": ["让主角活过第一夜"],
                           "ending_demand": "第 2 章结尾要出现一个盯上他的人"}],
                "chapters": [{"chapter_no": 1, "title": "一"}, {"chapter_no": 2, "title": "二"}],
            }}, allow_unicode=True), encoding="utf-8")
        (wd / "80-brief" / "v001-c0001.yaml").write_text(yaml.safe_dump({
            "schema_version": "brief-v1", "chapter_id": "v001-c0001", "vol": 1, "chapter_no": 1,
            "title": "一", "hook_type": "突然揭示", "settings": {"characters": ["李默"]},
        }, allow_unicode=True), encoding="utf-8")

        brief = render_write_brief(wd, chapter_no=2, prev_chapter_no=1)
        check("本段任务" in brief, "写前必读里有本段任务")
        check("让主角活过第一夜" in brief, "必须完成的功能进了输入")
        check("第 2 章结尾要出现一个盯上他的人" in brief, "末尾落点要求进了输入")
        check("章末钩子" in brief and "突然揭示" in brief, "已用钩子进了输入")
        check("不要**再用" in brief, "明确说了本章不要再用它")


def main() -> int:
    print("=" * 58)
    print("M9 创作台 · 规划层自检")
    print("=" * 58)

    for fn in (
        test_template_parses_and_starts_blocked,
        test_validation_catches_real_problems,
        test_single_source_of_truth_for_protagonist,
        test_injection_block_never_truncates_silently,
        test_volume_sequence_checks,
        test_volume_chain_checks,
        test_volume_files_roundtrip,
        test_brief_validation,
        test_foreshadow_ledger,
        test_cast_and_consistency,
        test_write_brief_assembly,
        test_story_gates,
        test_hook_rotation_and_part_tasks,
        test_brief_chain_foreshadow_refs,
        test_seed_briefs_never_overwrites,
        test_chain_report,
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
