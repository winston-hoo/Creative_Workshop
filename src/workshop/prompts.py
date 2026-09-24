"""标注提示词构造。

关键约束：**固定前缀必须逐字不变且排在请求最前面**。

所以消息结构刻意分成两段：
  messages[0] system —— 固定前缀（系统提示 + 原语 schema + 约束 + 作品动机设定）
  messages[1] user   —— 变量尾巴（上一章摘要 + 本章正文）

缓存按前缀匹配。把变量插在固定内容之前，缓存会全部失效——这是实现层必须守住的一条。
prefix_hash 会写进标注产物的来源信息，便于日后确认哪些标注用的是同一套前缀。
"""

from __future__ import annotations

import hashlib

from .primitives import Primitives, WorkConfig

PROMPT_VERSION = "annotate-v1"

_ROLE = """你是网文结构标注员。你只做标注，不改写原文，不评价好坏。

你的工作是把一章小说转成结构化数据。你的判断会被用于后续的节奏分析，
所以宁可如实标出「不确定」，也不要为了填满字段而猜测。"""

_CONSTRAINTS = """约束：
- 只输出 JSON，不要任何前后说明，不要用代码围栏
- 不确定的字段填 null，不要猜测
- 描述类字段（伏笔描述、爽点锚点）不超过 20 字
- **本章剧情梗概 chapter_summary 是唯一例外**：一句话写清剧情，不超过 60 字，必须是一句完整的话
- 不要输出原文中的完整句子
- 本章没有出现的内容，强度类字段填 1，数量类字段填 0

伏笔的判定标准（实测最容易标错的一项，请严格按此判断）：
- 伏笔 = **作者埋下、等后文才揭晓**的东西：未解开的疑问、尚未兑现的承诺、
  来历不明的物品或人物、被刻意隐去的动机
- **本章已经交代清楚的信息不是伏笔**（读者当场就知道了，没有悬念）
- **本章发生的动作不是伏笔**（谁打了谁、谁去了哪、谁拿到了什么，这些是情节不是伏笔）
- **主角的异能/身份/金手指本身不是伏笔**，除非本章明确留下了未解之谜
- **同一条线索只登记一次**：后文再提到它，一律填「推进」并引用清单里已有的编号；
  不要为同一条线索另外起一条新的埋设（实测出现过「掉落标志」和「标志未上交」被记成两条）
- 伏笔的「编号」只在判断为「推进」或「回收」时填写，且必须来自下文给出的未回收伏笔清单
- 判断为「埋设」时，伏笔的「编号」留空，系统会自动分配
- 不要自己发明伏笔编号
- 本章确实没有伏笔时，foreshadows 填空数组，不要为了填满而硬凑"""


def build_stable_prefix(primitives: Primitives, work: WorkConfig) -> str:
    """构造固定前缀。由数据文件派生，逐字确定，可安全缓存。"""
    parts = [_ROLE, ""]

    parts.append(f"当前作品：{work.name}")
    if work.protagonist:
        parts.append(f"主角：{work.protagonist}")

    if work.core_motive:
        parts.append(f"主角核心动机：{work.core_motive}")
    else:
        # 关键：core_motive 决定 P21 在度量什么。留空时不给这句话，
        # 模型会自己编一个动机来打分（实测给 3 和 4），那一列数据就废了——
        # 而且「猜出来的分数」和「诚实空值」在数据集里无法区分，前者更糟。
        parts.append(
            "作品未定义主角核心动机。字段 motive_strength 的度量对象不存在，"
            "一律填 null，不要自行猜测任何动机来打分。"
            "本条优先级高于下文「本章没有出现的内容，强度类字段填 1」的通用约束——"
            "没有动机可呈现时填 1 会被误读成「动机存在但很弱」。"
        )
    parts.append("")

    parts.append(f"结构原语版本：{primitives.version}")
    parts.append("")
    parts.append(primitives.build_model_schema_block())
    parts.append("")
    parts.append(_CONSTRAINTS)

    return "\n".join(parts)


def build_variable_tail(
    *,
    chapter_label: str,
    chapter_text: str,
    prev_summary: dict | None = None,
    open_foreshadows: list[dict] | None = None,
) -> str:
    """构造变量尾巴。所有会变的内容都放这里，排在固定前缀之后。

    未回收伏笔清单属于变量部分——它每章都在变，放进固定前缀会让缓存全部失效。
    """
    parts: list[str] = []

    if prev_summary:
        parts.append("上一章的标注结果（供判断连贯性，不需要重复标注）：")
        parts.append(_format_summary(prev_summary))
        parts.append("")

    parts.append("当前未回收的伏笔（判断「推进」或「回收」时必须引用这里的编号）：")
    parts.append(_format_open_foreshadows(open_foreshadows or []))
    parts.append("")

    parts.append(f"请标注以下章节：{chapter_label}")
    parts.append("")
    parts.append(chapter_text)
    return "\n".join(parts)


def _format_open_foreshadows(items: list[dict]) -> str:
    if not items:
        return "- （当前没有未回收的伏笔。本章若出现新的伏笔，动作填「埋设」、编号留空）"
    lines: list[str] = []
    for item in items:
        fid = item.get("id") or "?"
        desc = item.get("desc") or ""
        planted = item.get("planted_chapter_no")
        gap = item.get("gap")
        meta: list[str] = []
        if planted:
            meta.append(f"第{planted}章埋设")
        if gap is not None:
            meta.append(f"已 {gap} 章未推进")
        suffix = f"（{'，'.join(meta)}）" if meta else ""
        lines.append(f"- {fid} {desc}{suffix}")
    return "\n".join(lines)


def _format_summary(summary: dict) -> str:
    """把上一章标注压成很短的摘要。只带结构化结果，不带原文。"""
    keys = [
        ("hook_strength", "章末钩子强度"),
        ("hook_type", "钩子类型"),
        ("emotion", "情绪值"),
        ("conflict", "冲突等级"),
        ("mainline_progress", "主线推进度"),
        ("motive_strength", "动机呈现强度"),
    ]
    lines: list[str] = []
    for key, label in keys:
        value = summary.get(key)
        if value is not None:
            lines.append(f"- {label}: {value}")
    foreshadows = summary.get("foreshadows")
    if isinstance(foreshadows, list) and foreshadows:
        pending = [
            f"{item.get('编号') or '未编号'}（{item.get('动作')}）"
            for item in foreshadows
            if isinstance(item, dict)
        ]
        if pending:
            lines.append(f"- 本章涉及的伏笔: {'、'.join(pending)}")
    return "\n".join(lines) if lines else "- （无）"


def build_messages(
    *,
    stable_prefix: str,
    variable_tail: str,
) -> list[dict[str, str]]:
    """组装消息。顺序不可调换：固定前缀必须在最前。"""
    return [
        {"role": "system", "content": stable_prefix},
        {"role": "user", "content": variable_tail},
    ]


def prefix_hash(stable_prefix: str) -> str:
    return hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest()[:16]
