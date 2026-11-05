"""技法库：给正文生成与审稿提供写作技法储备。

## 为什么不整包塞进提示词

技法库 12 篇，全塞进去仍然比设定集 + 卷表 + 本章指令加起来大——输入预算被技法吃掉，
模型反而看不清"这一章到底要写什么"。所以这里做的是**按需检索**：
开篇章给黄金开篇，有对话给对话规范，章末永远给悬念钩子。

## 篇目从哪来

- **内置**：`craft/*.md`，本项目自己写的，随仓库与 exe 一起走。
- **外部**：数据根下的 `craft/`（源码方式是项目根目录，exe 是
  `%LOCALAPPDATA%\\创作工坊\\craft`）。放在这里的同名篇目**覆盖内置**——
  想接别处的技法库，把文件放进去就行，不用改代码。外部包不进 git，也不进 exe。

两处都没有这份篇目时 `pick()` / `read()` 返回空，生成与审稿少一段技法参考，
其余功能不受影响。

## 为什么预算超了要点名

和参照素材同一个规矩：技法少给一篇，作者不会知道，而他会以为"模型读过那篇了"。
`pick()` 返回的 `dropped` 就是干这个的。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

CRAFT_DIR = Path(__file__).resolve().parent / "craft"

# 每篇：什么时候该用、给模型看的定位。`always` 的每章都给。
CATALOG: dict[str, dict[str, Any]] = {
    "chapter-guide": {
        "label": "章节写作指南",
        "when": "每一章（前 20% 决定生死，开头写不好后面白写）",
        "always": True,
        "order": 10,
    },
    "hook-techniques": {
        "label": "悬念钩子十法",
        "when": "每一章（章末必须留钩子）",
        "always": True,
        "order": 20,
    },
    "golden-opening": {
        "label": "黄金开篇",
        "when": "第 1 章，以及每卷的第 1 章",
        "first_chapter": True,
        "first_of_volume": True,
        "order": 5,
    },
    "emotion-curve": {
        "label": "情绪曲线",
        "when": "每一章（每 3 章一个「压-小扬-压-爆」循环）",
        "always": True,
        "order": 15,
    },
    "plot-structures": {
        "label": "爽文情节结构",
        "when": "每一章（单章节奏 + 卷级节奏）",
        "always": True,
        "order": 25,
    },
    "dialogue-writing": {
        "label": "对话写作规范",
        "when": "本章指令里出现对话迹象时",
        "on_dialogue": True,
        "order": 30,
    },
    "content-expansion": {
        "label": "内容扩充技巧",
        "when": "目标字数写不够的时候",
        # ponytail: pick() 还没读 on_short / on_new_character（要字数缺口与「新人物」信号），
        # 这两篇目前只在 available() 里有份，谁也选不到。接上信号时补这两条分支。
        "on_short": True,
        "order": 40,
    },
    "continuity": {
        "label": "连贯性机制",
        "when": "有「需承接」内容时",
        "on_carry_over": True,
        "order": 35,
    },
    "character-building": {
        "label": "人物塑造原则",
        "when": "本章有新人物登场时（指令里有人物、设定集里还没有）",
        "on_new_character": True,
        "order": 45,
    },
    "prompt-guide": {"label": "提示词完善指南", "when": "不起草正文时（给助手备用）", "order": 90},
    "quality-checklist": {"label": "质量检查清单", "when": "审稿时（不给生成用）", "review_only": True, "order": 95},
    "review-dimensions": {"label": "章节审查维度", "when": "审稿时（不给生成用）", "review_only": True, "order": 96},
}

# 预算是「别让技法压住这一章要写什么」的闸门，但也不该把该给的篇目砍掉：
# 一章最多会选到 4 篇 always + 黄金开篇 + 对话规范 + 承接，加起来约 9200 字。
# 上限取 10000 —— 最长的一次选择装得下，其余情况照旧按预算裁。
# `tests/test_craft.py` 有断言兜住这条：哪天加篇目把最长选择顶出去，测试会红。
DEFAULT_CRAFT_BUDGET = 10000

# C6：同一份技法库按**阶段**分发。细纲要结构，正文要行文，审稿要审查。
# 三组集合不重叠的部分是刻意的：把「开头够不够黄金」喂给审稿，它会去评一件
# 只有生成阶段才该管的事。
STAGE_TAGS: dict[str, set[str]] = {
    "chapter-guide": {"draft"},
    "hook-techniques": {"draft"},
    "golden-opening": {"draft", "brief"},
    "emotion-curve": {"draft", "brief"},
    "plot-structures": {"draft", "brief"},
    "dialogue-writing": {"draft"},
    "content-expansion": {"draft"},
    "continuity": {"draft"},
    "character-building": {"draft"},
    "prompt-guide": set(),
    "quality-checklist": {"review"},
    "review-dimensions": {"review"},
}

STAGE_LABELS = {
    "draft": "正文（行文技法）",
    "brief": "细纲（结构技法）",
    "review": "审稿（审查技法）",
}

# 细纲要给的是「按什么结构拆」，三篇结构篇目合计约 11.8k 字，上限放宽到 12k；
# 正文沿用 10k；审稿只有两篇，用不到上限。
STAGE_BUDGETS = {"draft": DEFAULT_CRAFT_BUDGET, "brief": 12000, "review": DEFAULT_CRAFT_BUDGET}


def _external_dir() -> Path | None:
    """外部技法包目录：数据根下的 `craft/`，与 `server.main` 认的根同一个来源。"""
    root = Path(os.environ.get("WORKSHOP_ROOT") or Path(__file__).resolve().parent.parent.parent)
    path = (root / "craft").resolve()
    return None if path == CRAFT_DIR else path


def _path(name: str) -> Path | None:
    """这份篇目实际该读哪个文件：外部包优先，内置兜底。"""
    external = _external_dir()
    for candidate in ((external / f"{name}.md") if external else None, CRAFT_DIR / f"{name}.md"):
        if candidate is not None and candidate.exists():
            return candidate
    return None


def available() -> list[str]:
    """实际有的技法篇目（按 CATALOG 排序）。"""
    return sorted((name for name in CATALOG if _path(name)),
                  key=lambda k: CATALOG[k]["order"])


def read(name: str) -> str:
    path = _path(name)
    return path.read_text(encoding="utf-8") if path else ""


def pick(
    *,
    chapter_no: int = 0,
    is_first_of_volume: bool = False,
    brief: dict[str, Any] | None = None,
    review: bool = False,
    stage: str | None = None,
) -> tuple[list[str], list[str]]:
    """这一章该带哪几篇技法。返回 (要带的, 没带的)——没带的要点名，不能静默少给。

    判据全部来自本章指令与章号，不问模型：问模型"你需要什么技法"它一定说全都要。

    `stage` 决定走哪一组（`brief` 细纲 / `draft` 正文 / `review` 审稿）。
    `review=True` 是旧入口，等价于 `stage="review"`，保留给既有调用与测试。
    """
    if stage is None:
        stage = "review" if review else "draft"
    if stage not in STAGE_LABELS:
        raise ValueError(f"未知技法阶段「{stage}」，可选：{', '.join(STAGE_LABELS)}")
    brief = brief or {}
    focus = " ".join(str(x) for x in (brief.get("narrative") or {}).get("focus") or [])
    plot = " ".join(str(x) for x in brief.get("core_plot") or [])
    has_carry = bool(brief.get("carry_over"))
    blob = focus + " " + plot + " " + str(brief.get("one_line") or "")
    dialogue_hint = any(k in blob for k in ("对话", "说", "问", "谈", "喊", "骂", "开口", "回话"))

    wanted: list[str] = []
    for name in available():
        spec = CATALOG[name]
        if stage not in STAGE_TAGS.get(name, set()):
            continue
        if stage == "review":
            wanted.append(name)
            continue
        if spec.get("always"):
            wanted.append(name)
            continue
        if spec.get("first_chapter") and chapter_no == 1:
            wanted.append(name)
            continue
        if spec.get("first_of_volume") and is_first_of_volume:
            wanted.append(name)
            continue
        if spec.get("on_dialogue") and dialogue_hint:
            wanted.append(name)
            continue
        if spec.get("on_carry_over") and has_carry:
            wanted.append(name)
            continue
    return wanted, [n for n in available() if n not in wanted]


def render(names: list[str], *, budget: int = DEFAULT_CRAFT_BUDGET) -> str:
    """把选中的技法拼成一段。**只对"预算不够"点名**。

    「这一章不需要对话规范」和「技法库超预算没给」是两回事。混成一句"超出预算"，
    作者会去调高上限——而调多高都改变不了"这一章没有对话"这个事实。
    """
    parts: list[str] = []
    used = 0
    lost: list[str] = []
    for name in names:
        text = read(name).strip()
        if not text:
            lost.append(name)
            continue
        block = f"【技法·{CATALOG[name]['label']}】\n{text}"
        if used + len(block) > budget and parts:
            lost.append(name)
            continue
        parts.append(block)
        used += len(block) + 1
    out = "\n\n".join(parts)
    if lost:
        labels = "、".join(CATALOG.get(n, {}).get("label") or n for n in lost)
        out += (
            f"\n（⚠️ 技法库超出 {budget} 字预算，以下篇目这次没有给：{labels}。"
            "调高上限可以带上它们）"
        )
    return out


def plan_block(
    *,
    stage: str = "draft",
    chapter_no: int = 0,
    is_first_of_volume: bool = False,
    brief: dict[str, Any] | None = None,
    budget: int | None = None,
) -> dict[str, Any]:
    """挑 + 渲染，并把「给了哪几篇、占了多少字、多少钱」一起返回。

    plan 里要能解释注入量（C6 验收）：`chars` 是实际拼进去的字数，
    正文的 token / 费用估算用的就是它；`labels` 与 `dropped` 让作者知道
    「这次按结构/行文/审查给了哪几篇」，而不是只看到一段来历不明的参考。
    """
    size = STAGE_BUDGETS.get(stage, DEFAULT_CRAFT_BUDGET) if budget is None else int(budget)
    names, not_selected = pick(
        chapter_no=chapter_no, is_first_of_volume=is_first_of_volume,
        brief=brief, stage=stage,
    )
    text = render(names, budget=size)
    given = [name for name in names if read(name).strip()]
    dropped = [name for name in names if name not in given]
    if "⚠️" in text:  # render() 已把超预算的篇目写进正文说明里
        dropped = [name for name in names if f"【技法·{CATALOG[name]['label']}】" not in text]
    return {
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "names": given,
        "labels": [CATALOG[n]["label"] for n in given],
        "chars": len(text),
        "budget": size,
        "dropped": dropped,
        "not_selected": not_selected,
        "text": text,
    }


def craft_block(
    *,
    chapter_no: int = 0,
    is_first_of_volume: bool = False,
    brief: dict[str, Any] | None = None,
    budget: int | None = None,
    review: bool = False,
    stage: str | None = None,
) -> str:
    """一步到位：挑 + 渲染。

    `review=True` 时只给审查那两篇——审稿拿着生成用的技法，会去评
    「开头够不够黄金」这种只有生成阶段才该管的事。新代码用 `stage=`。
    """
    chosen = stage or ("review" if review else "draft")
    return plan_block(
        stage=chosen, chapter_no=chapter_no, is_first_of_volume=is_first_of_volume,
        brief=brief, budget=budget,
    )["text"]
