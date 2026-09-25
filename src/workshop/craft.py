"""技法库：给正文生成与审稿提供写作技法储备。

## 为什么不整包塞进提示词

技法库 83 KB（13 篇）。全塞进去的话，光技法就比设定集 + 卷表 + 本章指令加起来还大好几倍——
输入预算被技法吃掉，模型反而看不清"这一章到底要写什么"。所以这里做的是**按需检索**：
开篇章给黄金开篇，有对话给对话规范，章末永远给悬念钩子。

## 这些内容不是本项目原创

`craft/` 里是从外部技能包原样收录的写作技法参考，来源与版权说明见 `craft/SOURCES.md`。
本模块只负责**检索与预算控制**，不改写原文——改写了就没法跟原出处对照，
将来要换、要删、要核许可都无从下手。

⚠️ 因此这些文件**不入库**（`.gitignore` 里的 `src/workshop/craft/*`）：那份技能声明
内容版权归原作者所有，本项目只在本机自用范围内参考。公开仓库里只有
`craft/README.md`，说明怎么自己补一份。文件不在时 `pick()` / `read()` 全部返回空，
生成与审稿少一段技法参考，其余功能不受影响。

## 为什么预算超了要点名

和参照素材同一个规矩：技法少给一篇，作者不会知道，而他会以为"模型读过那篇了"。
`pick()` 返回的 `dropped` 就是干这个的。
"""

from __future__ import annotations

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

# 技法库不能喧宾夺主：它最多占输入的这个量级。设定集与本章指令加起来才几百到几千字，
# 技法给到 6000 字已经能覆盖几篇；再多就是拿技法压住了"这一章要写什么"。
DEFAULT_CRAFT_BUDGET = 6000


def available() -> list[str]:
    """仓库里实际有的技法篇目（按 CATALOG 排序）。"""
    return [name for name in sorted(CATALOG, key=lambda k: CATALOG[k]["order"])
            if (CRAFT_DIR / f"{name}.md").exists()]


def read(name: str) -> str:
    path = CRAFT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def pick(
    *,
    chapter_no: int = 0,
    is_first_of_volume: bool = False,
    brief: dict[str, Any] | None = None,
    review: bool = False,
) -> tuple[list[str], list[str]]:
    """这一章该带哪几篇技法。返回 (要带的, 没带的)——没带的要点名，不能静默少给。

    判据全部来自本章指令与章号，不问模型：问模型"你需要什么技法"它一定说全都要。
    """
    brief = brief or {}
    focus = " ".join(str(x) for x in (brief.get("narrative") or {}).get("focus") or [])
    plot = " ".join(str(x) for x in brief.get("core_plot") or [])
    has_carry = bool(brief.get("carry_over"))
    blob = focus + " " + plot + " " + str(brief.get("one_line") or "")
    dialogue_hint = any(k in blob for k in ("对话", "说", "问", "谈", "喊", "骂", "开口", "回话"))

    wanted: list[str] = []
    for name in available():
        spec = CATALOG[name]
        if spec.get("review_only") and not review:
            continue
        if review and not spec.get("review_only") and name not in ("quality-checklist", "review-dimensions"):
            # 审稿只吃审查那两篇：把生成用的技法一起喂进去，它会去评"开头够不够黄金"
            # 这种只有生成阶段才该管的事。
            continue
        if review:
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


def craft_block(
    *,
    chapter_no: int = 0,
    is_first_of_volume: bool = False,
    brief: dict[str, Any] | None = None,
    budget: int = DEFAULT_CRAFT_BUDGET,
    review: bool = False,
) -> str:
    """一步到位：挑 + 渲染。

    `review=True` 时只给审查那两篇——审稿拿着生成用的技法，会去评
    「开头够不够黄金」这种只有生成阶段才该管的事。
    """
    names, _not_selected = pick(
        chapter_no=chapter_no, is_first_of_volume=is_first_of_volume,
        brief=brief, review=review,
    )
    return render(names, budget=budget)
