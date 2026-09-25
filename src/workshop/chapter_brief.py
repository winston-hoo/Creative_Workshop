"""M9 创作台 · 第三层：逐章创作任务指令。

一章一个文件（`80-brief/v001-c0001.json`），字段基准来自作者实际用过的
「创作任务指令」（`05_决策指令/...第002章_创作任务指令_[Director]`）：
章节标题 / 核心情节 / 时间线衔接 / 字数要求 / 文笔要求 / 必须遵循的设定 /
上章遗留问题修正。写正文的人（人或模型）只看这一个文件就该能动手。

**这一版是人手填的**，不接模型——先把数据形状和校验定下来，确认形状对了再接生成。

章号沿用录入那边的编号约定（`v{卷:03d}-c{章:04d}`），这样将来把定稿章节走一次
录入，标注/体检/知识库那条链能直接接上，不用另起一套 id。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .primitives import yaml_scalar

BRIEF_SCHEMA_VERSION = "brief-v1"
BRIEF_FILE_RE = re.compile(r"^v(\d{3})-c(\d{4})[a-z]?\.ya?ml$")

_FORESHADOW_ACTIONS = ("埋设", "推进", "回收")


# ── 命名与骨架 ──────────────────────────────────────────────


def chapter_id_of(vol_no: int, chapter_no: int) -> str:
    return f"v{vol_no:03d}-c{chapter_no:04d}"


def parse_chapter_id(chapter_id: str) -> tuple[int, int] | None:
    match = re.match(r"^v(\d{1,4})-c(\d{1,5})[a-z]?$", str(chapter_id or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def brief_path(brief_dir: str | Path, chapter_id: str) -> Path:
    return Path(brief_dir) / f"{chapter_id}.yaml"


def empty_brief(
    work: str,
    vol_no: int,
    chapter_no: int,
    *,
    title: str = "",
    gist: str = "",
    word_target: tuple[int, int] | None = None,
    perspective: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "work": work,
        "chapter_id": chapter_id_of(vol_no, chapter_no),
        "vol": vol_no,
        "chapter_no": chapter_no,
        "title": title,
        "one_line": gist,
        "core_plot": [],
        "timeline": "",
        "word_target": list(word_target or (2800, 4000)),
        "narrative": {
            "perspective": perspective,
            "tone": "",
            "focus": [],
        },
        "settings": {"characters": [], "factions": [], "terms": []},
        "foreshadow": [],
        "carry_over": [],
        "must_not": [],
    }


def brief_template_text(
    work: str,
    vol_no: int,
    chapter_no: int,
    *,
    title: str = "",
    gist: str = "",
    word_target: tuple[int, int] | None = None,
    perspective: str = "",
) -> str:
    """给人手填的带注释骨架。

    标题、概要、字数、视角都从卷表和设定集**播种**下来：这些值手抄一遍就容易漂，
    而漂了之后「指令和卷表对不上」正是最该避免的那类错。
    """
    low, high = word_target or (2800, 4000)
    return f"""# 第 {chapter_no} 章创作任务指令 · {chapter_id_of(vol_no, chapter_no)}
#
# 写正文的人只看这一个文件就该能动手：他不需要回去翻设定集和卷表。
# 这里出现的人物/势力/术语都会跟设定集对账，名字漂了会被抓出来。
# 校验：python create_cli.py --check-brief {work} --chapter {chapter_id_of(vol_no, chapter_no)}

schema_version: {BRIEF_SCHEMA_VERSION}
work: {yaml_scalar(work)}
chapter_id: {chapter_id_of(vol_no, chapter_no)}
vol: {vol_no}
chapter_no: {chapter_no}
title: {yaml_scalar(title)}

# 一句话说清这一章干什么（写正文时它就是准绳）。
one_line: {yaml_scalar(gist)}

# 核心情节，按发生顺序一条一句。这一章要落地的动作/转折都写在这里。
core_plot: []
#  - 朔夜在刻印仪式上觉醒穿越者记忆
#  - 仪式完成后他确认自己成了日向分家的孩子

# 时间线衔接：接哪一章、过了多久、什么季节。断在这里，正文就会自己编时间。
timeline: ""

# 字数区间。跟设定集的 target.chars_per_chapter 比一比，差太多会提示。
word_target: [{low}, {high}]

narrative:
  perspective: {yaml_scalar(perspective)}   # 第一人称 | 第三限知 | 第三全知
  tone: ""                       # 例：轻松搞笑、冷峻克制
  focus: []                      # 这一章要重点写什么（画面/心理/打斗/对话）

# 必须遵循的设定。名字要能在设定集里找到。
settings:
  characters: []                 # 出场人物
  factions: []
  terms: []

# 伏笔动作。埋设**自己编号**（F-001 这种），推进/回收引用前面已经埋过的编号。
foreshadow: []
#  - action: 埋设
#    id: F-001
#    desc: 笼中鸟刻印会在宗家需要时引爆

# 上一章留下的问题，这一章要修的那些（时间对不上、人物状态矛盾、承诺没兑现）。
carry_over: []
#  - 上一章说"三天后"，这一章却写成当天

# 这一章**不能**写什么。越界禁令比正面要求更能防止跑偏。
must_not: []
#  - 不写朔夜的心理独白（本作是第三限知）
"""


# ── 读写 ────────────────────────────────────────────────────


def load_brief(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"创作任务指令格式异常（顶层不是映射）：{p}")
    return data


def save_brief(path: str | Path, data: dict[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )
    return p


def list_brief_files(brief_dir: str | Path) -> list[Path]:
    d = Path(brief_dir)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if BRIEF_FILE_RE.match(p.name))


def load_all_briefs(brief_dir: str | Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读全部指令。返回 (chapter_id → 指令, 读不出来的文件说明)。"""
    briefs: dict[str, dict[str, Any]] = {}
    broken: list[str] = []
    for path in list_brief_files(brief_dir):
        try:
            data = load_brief(path)
        except (ValueError, OSError) as exc:
            broken.append(f"{path.name}：{exc}")
            continue
        data["_file"] = path.name
        cid = str(data.get("chapter_id") or "").strip()
        if cid != path.stem:
            broken.append(f"{path.name}：文件名是 {path.stem}，里面的 chapter_id 写的是 {cid or '(空)'}")
            continue
        briefs[cid] = data
    return briefs, broken


# ── 校验 ────────────────────────────────────────────────────


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def validate_brief(
    data: dict[str, Any],
    *,
    setting: dict[str, Any] | None = None,
    volume: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """单份指令校验。跨章（伏笔编号、章节覆盖）由 validate_brief_chain 管。"""
    errors: list[str] = []
    warnings: list[str] = []

    cid = str(data.get("chapter_id") or "").strip()
    parsed = parse_chapter_id(cid)
    if parsed is None:
        errors.append(f"chapter_id「{cid or '(空)'}」不合约定，应当是 v001-c0001 这种")
    else:
        vol_no, chapter_no = parsed
        if data.get("vol") != vol_no:
            errors.append(f"chapter_id 说第 {vol_no} 卷，vol 字段写的是 {data.get('vol')}")
        if data.get("chapter_no") != chapter_no:
            errors.append(f"chapter_id 说第 {chapter_no} 章，chapter_no 字段写的是 {data.get('chapter_no')}")

    if not str(data.get("title") or "").strip():
        errors.append("缺 title")
    if not str(data.get("one_line") or "").strip():
        warnings.append("没写 one_line（一句话说清这章干什么）")

    core_plot = [str(x).strip() for x in (data.get("core_plot") or []) if str(x).strip()]
    if not core_plot:
        errors.append("core_plot 是空的——没有核心情节，写正文的人只能自己编")

    if not str(data.get("timeline") or "").strip():
        warnings.append("没写 timeline（时间线衔接）——断在这里，正文就会自己编时间")

    low = _int_or_none((data.get("word_target") or [None, None])[0]) if isinstance(data.get("word_target"), list) else None
    high = _int_or_none((data.get("word_target") or [None, None])[1]) if isinstance(data.get("word_target"), list) else None
    if low is None or high is None:
        errors.append("word_target 要写成 [下限, 上限] 两个整数")
    elif high < low:
        errors.append(f"word_target 反了：{low}-{high}")
    elif setting is not None:
        span = (setting.get("target") or {}).get("chars_per_chapter") or []
        if len(span) == 2 and isinstance(span[0], int) and isinstance(span[1], int):
            if low < span[0] // 2 or high > span[1] * 2:
                warnings.append(
                    f"word_target {low}-{high} 跟设定集的每章 {span[0]}-{span[1]} 差得有点远"
                )

    narrative = data.get("narrative") or {}
    perspective = str(narrative.get("perspective") or "").strip()
    if setting is not None:
        want = str(((setting.get("style") or {}).get("perspective")) or "").strip()
        if want and not perspective:
            warnings.append(f"narrative.perspective 没写，设定集约定的是「{want}」")
        elif want and perspective and perspective != want:
            warnings.append(
                f"narrative.perspective「{perspective}」跟设定集的「{want}」不一致"
                "——如果是有意的实验就留着，否则改一处别忘了另一处"
            )

    # 引用的名字要能在设定集里找到。名字漂移是最难回头改的一类错。
    if setting is not None:
        known = {str(c.get("name") or "").strip()
                 for c in (setting.get("characters") or []) if isinstance(c, dict)}
        known.discard("")
        for name in (data.get("settings") or {}).get("characters") or []:
            name = str(name).strip()
            if name and known and name not in known:
                warnings.append(f"出场人物「{name}」不在设定集里")
        faction_names = {str(f.get("name") or "").strip()
                         for f in (setting.get("factions") or []) if isinstance(f, dict)}
        faction_names.discard("")
        for name in (data.get("settings") or {}).get("factions") or []:
            name = str(name).strip()
            if name and faction_names and name not in faction_names:
                warnings.append(f"势力「{name}」不在设定集里")

    # 跟卷表对账：章号必须真的在那一卷里，标题不一致说明有一处改了没同步
    if volume is not None:
        vol = volume.get("vol") or {}
        rows = {c.get("chapter_no"): c for c in (vol.get("chapters") or []) if isinstance(c, dict)}
        row = rows.get(data.get("chapter_no"))
        if row is None:
            errors.append(
                f"第{data.get('chapter_no')}章不在第 {vol.get('vol')} 卷的章表里"
                "——卷表和指令对不上，二者必有一个错"
            )
        else:
            if str(row.get("title") or "").strip() and str(row.get("title")).strip() != str(data.get("title") or "").strip():
                warnings.append(
                    f"标题跟卷表不一致：卷表是「{row.get('title')}」，这里写「{data.get('title')}」"
                )

    for entry in data.get("foreshadow") or []:
        if not isinstance(entry, dict):
            errors.append("foreshadow 里有一条不是映射")
            continue
        action = str(entry.get("action") or "").strip()
        fid = str(entry.get("id") or "").strip()
        desc = str(entry.get("desc") or "").strip()
        if action not in _FORESHADOW_ACTIONS:
            errors.append(f"伏笔动作「{action or '(空)'}」不合法：{' | '.join(_FORESHADOW_ACTIONS)}")
            continue
        if action == "埋设":
            if not fid:
                errors.append(f"埋设「{desc or '(没写描述)'}」没有编号——编号要在埋设时就定下来，后文才引用得到")
            if not desc:
                warnings.append(f"埋设 {fid} 没写 desc")
        else:
            if not fid:
                errors.append(f"「{action}」没有编号——它必须指向前面埋过的那一条")
            if not desc:
                warnings.append(f"「{action}」{fid} 没写 desc（这一章它是怎么被推进的）")

    return errors, warnings


def validate_brief_chain(briefs: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """跨章校验：伏笔引用完整性 + 行文顺序。

    伏笔编号的规矩跟台账一致：**埋设时定号，推进/回收时引用**。
    引用了不存在或还没埋的编号，说明中途改过编号，后文却没跟上——
    这种错只会在写正文时爆发，那时已经写完几十章了，所以必须在这里拦住。
    """
    errors: list[str] = []
    warnings: list[str] = []

    order: list[tuple[int, int, str]] = []
    for cid, brief in briefs.items():
        parsed = parse_chapter_id(cid)
        if parsed:
            order.append((parsed[0], parsed[1], cid))
    order.sort()

    planted: dict[str, str] = {}      # 编号 → 埋设它的 chapter_id
    for _vol, _no, cid in order:
        brief = briefs[cid]
        for entry in brief.get("foreshadow") or []:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action") or "").strip()
            fid = str(entry.get("id") or "").strip()
            if not fid:
                continue
            if action == "埋设":
                if fid in planted:
                    errors.append(f"{cid} 又埋了一次 {fid}（它已经在 {planted[fid]} 埋过）——编号不能重用")
                else:
                    planted[fid] = cid
            elif action in ("推进", "回收"):
                if fid not in planted:
                    errors.append(
                        f"{cid} 对 {fid} 做了「{action}」，但它此前没被埋设过"
                        "——多半是中途改了编号，后文没跟上"
                    )

    # 章末钩子不能连着两章同一种——这是"写法重复"，伏笔链那套查不出来
    warnings = warnings + hook_rotation_problems(list(briefs.values()))
    return errors, warnings


# ── 渲染 ────────────────────────────────────────────────────


def render_brief_markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# 第{data.get('chapter_no')}章创作任务指令 · {data.get('title') or '（未命名）'}",
        "",
        f"`{data.get('chapter_id')}`　第 {data.get('vol')} 卷"
        f"　字数 {((data.get('word_target') or ['—', '—'])[0])}-{((data.get('word_target') or ['—', '—'])[1])}",
        "",
    ]
    if data.get("one_line"):
        lines += [f"> {data['one_line']}", ""]

    lines.append("## 核心情节")
    for item in data.get("core_plot") or []:
        lines.append(f"1. {item}")
    lines.append("")

    lines.append("## 时间线衔接")
    lines.append(str(data.get("timeline") or "—"))
    lines.append("")

    narrative = data.get("narrative") or {}
    lines.append("## 文笔要求")
    lines.append(f"- 视角：{narrative.get('perspective') or '—'}")
    if narrative.get("tone"):
        lines.append(f"- 基调：{narrative['tone']}")
    for item in narrative.get("focus") or []:
        lines.append(f"- 重点：{item}")
    lines.append("")

    settings = data.get("settings") or {}
    lines.append("## 必须遵循的设定")
    for key, label in (("characters", "人物"), ("factions", "势力"), ("terms", "术语")):
        values = settings.get(key) or []
        if values:
            lines.append(f"- {label}：" + "、".join(str(v) for v in values))
    if not any((settings.get(k) or []) for k in ("characters", "factions", "terms")):
        lines.append("无")
    lines.append("")

    if data.get("foreshadow"):
        lines.append("## 伏笔动作")
        for entry in data["foreshadow"]:
            if isinstance(entry, dict):
                lines.append(
                    f"- {entry.get('action')} {entry.get('id') or ''}：{entry.get('desc') or ''}".rstrip()
                )
        lines.append("")

    if data.get("carry_over"):
        lines.append("## 上章遗留问题修正")
        for item in data["carry_over"]:
            lines.append(f"- {item}")
        lines.append("")

    if data.get("must_not"):
        lines.append("## 这一章不能写什么")
        for item in data["must_not"]:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── 伏笔账本 ────────────────────────────────────────────────
#
# 伏笔是长篇唯一「不记就一定会烂尾」的东西。链路校验只能告诉你「这一条回收得对不对」，
# 回答不了真正要紧的那个问题：**现在还有几条挂着没收**。
# 这个函数把散在几十个 brief 里的动作汇成一本账：埋了哪些、推到哪一步、哪些该收还没收。


def foreshadow_ledger(brief_dir: str | Path) -> dict[str, Any]:
    """扫全部逐章指令，算出一本伏笔账。纯脚本，零成本。

    生命周期：埋设 → 开启；推进 → 仍开着（记次数）；回收 → 关闭。
    「推进/回收 引用了不存在的 id」不在这里报——那是 `validate_brief_chain` 的活，
    这里重复报一次只会让同一件事出现两遍，作者还得自己判断是不是两个问题。
    """
    briefs, broken = load_all_briefs(brief_dir)
    rows = sorted(
        briefs.values(),
        key=lambda b: (int(b.get("vol") or 0), int(b.get("chapter_no") or 0)),
    )
    latest_no = max((int(b.get("chapter_no") or 0) for b in rows), default=0)

    open_map: dict[str, dict[str, Any]] = {}
    recovered: list[dict[str, Any]] = []
    problems: list[str] = []

    for b in rows:
        cid = str(b.get("chapter_id") or "")
        no = int(b.get("chapter_no") or 0)
        for item in b.get("foreshadow") or []:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip()
            fid = str(item.get("id") or "").strip()
            desc = str(item.get("desc") or "").strip()
            if not fid:
                problems.append(f"{cid}：有一条伏笔动作没写编号（{action}）")
                continue
            if action == "埋设":
                if fid in open_map:
                    # 同一个 id 埋两次：后面的回收会关掉一个、另一个永远挂着，
                    # 而作者以为它收了。必须点出来，不能静默覆盖。
                    problems.append(f"{cid}：编号 {fid} 之前已经埋过，这里是第二次埋设")
                    continue
                if any(r.get("id") == fid for r in recovered):
                    problems.append(f"{cid}：编号 {fid} 之前已经回收过，这里是重新埋设")
                open_map[fid] = {
                    "id": fid, "desc": desc, "planted_at": cid, "planted_no": no,
                    "last_action": "埋设", "last_at": cid, "advances": 0,
                }
            elif action in ("推进", "回收"):
                rec = open_map.get(fid)
                if rec is None:
                    continue  # 链路校验会报，这里不重复
                rec["last_action"] = action
                rec["last_at"] = cid
                if action == "回收":
                    rec["recovered_at"] = cid
                    rec["recovered_no"] = no
                    recovered.append(rec)
                    del open_map[fid]
                else:
                    rec["advances"] += 1

    open_items = sorted(open_map.values(), key=lambda r: r.get("planted_no") or 0)
    for r in open_items:
        # 「挂了多少章」= 从埋设那一章到现在，中间隔了几章。作者就是靠这个数决定先收哪个。
        r["hanging_chapters"] = max(0, latest_no - (r.get("planted_no") or 0))
    for r in recovered:
        r["closed_after"] = (r.get("recovered_no") or 0) - (r.get("planted_no") or 0)

    return {
        "open": open_items,
        "recovered": recovered,
        "problems": problems,
        "broken_files": broken,
        "counts": {
            "open": len(open_items),
            "recovered": len(recovered),
            "chapters": len(rows),
            "latest_chapter_no": latest_no,
        },
    }


# ── 章末钩子的类型与轮换 ────────────────────────────────────
#
# 十种钩子来自外部技能包（番茄小说写作）的 `hook-techniques.md`，
# 那份参考同时在 `craft/hook-techniques.md` 里。这里的表是**规范**——
# 校验和下拉框都靠它，所以它必须在代码里，不能只躺在文档里。
HOOK_TYPES: tuple[str, ...] = (
    "突然揭示", "紧急危机", "未完成的动作", "身份反转", "两难选择",
    "神秘物品线索", "时间限制", "承诺威胁", "离奇消失", "言外之意",
)


def hook_rotation_problems(briefs: list[dict[str, Any]]) -> list[str]:
    """钩子不能连着两章用同一种。返回问题清单。

    这是「写法重复」而不是「情节重复」——读者说不清哪里腻，但会觉得"怎么又是这套"。
    技法库明确要求章末钩子与上一章**轮换**，这里把它变成可检查的。
    """
    rows = sorted(briefs, key=lambda b: (int(b.get("vol") or 0), int(b.get("chapter_no") or 0)))
    problems: list[str] = []
    prev_hook = ""
    prev_cid = ""
    for b in rows:
        cid = str(b.get("chapter_id") or "")
        hook = str(b.get("hook_type") or "").strip()
        if not hook:
            prev_hook, prev_cid = "", cid
            continue
        if hook not in HOOK_TYPES:
            problems.append(
                f"{cid} 的章末钩子类型「{hook}」不在十种里（{'、'.join(HOOK_TYPES)}）"
            )
        elif hook == prev_hook:
            problems.append(
                f"{cid} 和 {prev_cid} 用了同一种章末钩子「{hook}」——连着两章同一个手法，"
                "读者会觉得腻。换一种。"
            )
        prev_hook, prev_cid = hook, cid
    return problems


def part_task_problems(volume: dict[str, Any]) -> list[str]:
    """卷内每一段都该有「必须完成的功能」。没有的话，写到那一段没人知道任务达成了没有。"""
    v = volume.get("vol") or {}
    problems: list[str] = []
    for part in v.get("parts") or []:
        if not isinstance(part, dict):
            continue
        title = str(part.get("title") or "（未名）")
        if not [x for x in (part.get("must_complete") or []) if str(x).strip()]:
            problems.append(
                f"第 {v.get('vol')} 卷的「{title}」这一段没有写「必须完成的功能」——"
                "写到这一段时没人知道任务达成了没有"
            )
    return problems
