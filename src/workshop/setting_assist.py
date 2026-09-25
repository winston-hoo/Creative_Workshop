"""M9 创作台 · 设定集助手：用对话快速起草，但**改什么由作者逐条点头**。

## 为什么不是「一说就把整份设定集重写一遍」

设定集是作者自己的东西：他的设定、他的创意。模型整份重写一遍，作者看完说「不是我想要的」，
那一次调用连同他的阅读时间全是无用功——而且他刚才手填的部分也被覆盖了，这是更坏的后果。

所以这里的形态是**提案制**：

    作者说一句话（"给我三个配角：一个导师、一个对手、一个损友"）
      → 模型只返回「建议新增/修改哪几条」，不是整份设定集
      → 界面把每条摊开，作者逐条勾选
      → 只有勾中的那几条进表单草稿
      → 作者再自己点「保存」，才落到 setting.yaml

`apply_proposals` 是这条链上唯一会写数据的地方，**模型说什么都不直接信**：
不认识的小节、缺主键的条目、重名的角色，一律拒收并写明原因，而不是塞进设定集里当既成事实。
它能脱离模型单测，这是刻意的——最容易出错的一段必须能不花钱地反复验。

## 依据从哪来

`refs` 是作者选定的已入库作品（走 K1 实体 / K3 风格指纹）。K1 里是别的人物名与专属设定，
按作者本人的决定可以作参照；不选就是纯原创，一切从零。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .llm import (
    ApiError,
    ChatResult,
    ErrorKind,
    OpenAICompatProvider,
    build_thinking_extra,
    extract_json_object,
)

ASSIST_SCHEMA_VERSION = "setting-assist-v1"

# 素材注入的三档。
#
# ⚠️ 默认是 `full`，这里推翻了早先一次实测的结论（那次同一问题各跑一次，全素材 2648
# 输入 token、不注入 636，产出「同等可用」，所以当时定了中间档 k3）。
# 推翻的理由：那次测的是**写三个配角**，而 k3 只给统计量、零人名零势力零能力。
# 设定集助手的主业恰恰是生成势力/能力/世界观——不给素材，它只能凭空编，
# 「知识库没起作用」就成了一句字面属实的话。样本各一次的结论，撑不住这种代价。
# 真要省钱的作者，界面上把档位调回 k3 就是。
REFS_LEVELS: dict[str, str] = {
    "none": "不注入",
    "k3": "只注入结构指纹",
    "full": "注入全部素材",
}
DEFAULT_REFS_LEVEL = "full"
REFS_LEVEL_HINTS: dict[str, str] = {
    "none": "纯原创。不听任何已入库作品的节奏，模型全凭设定集发挥。",
    "k3": "借节奏不借设定：钩子强度/情绪/冲突/主线推进均值、主导钩子类型、视角人称分布。零人名零情节。",
    "full": "借格局：再加上人物定位与身份、势力立场、能力、地点、关系格局。素材量大，成本约为中间档的 5 倍。",
}

# 每一层各自的白名单。**白名单而不是黑名单**：黑名单永远漏，而漏掉的那一项
# 就是模型往里塞东西的口子。路径都是**相对于该层自己的文档**。
#
# 三层共用同一个提案引擎（提 → 逐条勾选 → 并入草稿 → 作者点保存），
# 差别只在这张表和提示词的框架句。分卷目录和逐章指令一开始没有助手，
# 是因为「章表」这种结构化程度高的东西，模型乱塞的破坏力比人设更大——
# 白名单 + 主键 + int 字段三重限制之后才可以放它进来。
LAYER_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "setting": {
        "characters": {
            "kind": "objlist", "path": ("characters",), "key": "name", "label": "人物",
            "fields": ["name", "role", "identity", "personality", "motive", "arc", "faction"],
            "list_fields": ["abilities"], "required": ["name", "role", "motive"],
        },
        "factions": {
            "kind": "objlist", "path": ("factions",), "key": "name", "label": "势力",
            "fields": ["name", "stance", "leader"], "required": ["name"],
        },
        "terms": {
            "kind": "objlist", "path": ("terms",), "key": "term", "label": "术语",
            "fields": ["term", "meaning"], "required": ["term"],
        },
        "places": {
            "kind": "objlist", "path": ("world", "places"), "key": "name", "label": "地点",
            "fields": ["name", "note"],
        },
        "rules": {"kind": "strlist", "path": ("world", "rules"), "label": "世界硬规则"},
        "tiers": {"kind": "strlist", "path": ("power", "tiers"), "label": "力量体系等级"},
        # 能力原本只是人物身上一个逗号分隔的文本框，没效果、没等级、没归属。
        # 升成一等小节：知识库里 400 多条能力要有地方落，作者也要能单独查和改。
        "abilities": {
            "kind": "objlist", "path": ("abilities",), "key": "name", "label": "能力",
            "fields": ["name", "effect", "tier", "holder"], "required": ["name"],
        },
        "themes": {"kind": "strlist", "path": ("themes",), "label": "主题与象征"},
        "taboo": {"kind": "strlist", "path": ("style", "taboo"), "label": "文风禁忌"},
        "logline": {"kind": "scalar", "path": ("logline",), "label": "一句话前提"},
        "core_motive": {"kind": "scalar", "path": ("core_motive",), "label": "主角核心动机"},
        "ultimate_hook": {"kind": "scalar", "path": ("ultimate_hook",), "label": "终极钩子"},
        "era": {"kind": "scalar", "path": ("world", "era"), "label": "时代"},
        "power_system": {"kind": "scalar", "path": ("power", "system"), "label": "力量体系名"},
        "tone": {"kind": "scalar", "path": ("style", "tone"), "label": "基调"},
    },
    "volume": {
        "title": {"kind": "scalar", "path": ("vol", "title"), "label": "卷名"},
        "era": {"kind": "scalar", "path": ("vol", "era"), "label": "时间跨度"},
        "core_conflict": {"kind": "scalar", "path": ("vol", "core_conflict"), "label": "本卷核心冲突"},
        "goal": {"kind": "scalar", "path": ("vol", "goal"), "label": "本卷目标"},
        "closing": {"kind": "scalar", "path": ("vol", "closing"), "label": "卷末总结"},
        "parts": {
            "kind": "objlist", "path": ("vol", "parts"), "key": "title", "label": "部分划分",
            "fields": ["title", "gist", "ending_demand"],
            "list_fields": ["must_complete"],
            "int_fields": ["start_chapter", "end_chapter"],
            "aliases": {"summary": "gist", "desc": "gist", "note": "gist"},
        },
        "chapters": {
            "kind": "objlist", "path": ("vol", "chapters"), "key": "chapter_no", "label": "章节",
            "fields": ["title", "gist", "foreshadow"],
            "int_fields": ["chapter_no"],
            "list_fields": ["characters"],
            "required": ["chapter_no", "title"],
            # 实测模型反复用 summary 表示「本章概要」。不收它的话，
            # 卷表回填每次都「没有任何字段被改到」——闸是对的，但功能等于没有。
            "aliases": {"summary": "gist", "desc": "gist", "note": "gist", "brief": "gist"},
        },
    },
    "brief": {
        "title": {"kind": "scalar", "path": ("title",), "label": "本章标题"},
        "one_line": {"kind": "scalar", "path": ("one_line",), "label": "一句话概要"},
        "timeline": {"kind": "scalar", "path": ("timeline",), "label": "时间位置"},
        # 章末钩子类型。存下来是为了**和上一章轮换**——同一个手法每章用，读者第三次就腻了。
        "hook_type": {"kind": "scalar", "path": ("hook_type",), "label": "章末钩子类型"},
        "core_plot": {"kind": "strlist", "path": ("core_plot",), "label": "核心情节"},
        "carry_over": {"kind": "strlist", "path": ("carry_over",), "label": "需承接"},
        "must_not": {"kind": "strlist", "path": ("must_not",), "label": "禁止出现"},
        "narrative_focus": {"kind": "strlist", "path": ("narrative", "focus"), "label": "叙事重点"},
        "narrative_tone": {"kind": "scalar", "path": ("narrative", "tone"), "label": "本章基调"},
        "characters": {"kind": "strlist", "path": ("settings", "characters"), "label": "出场人物"},
        "factions": {"kind": "strlist", "path": ("settings", "factions"), "label": "涉及势力"},
        "terms": {"kind": "strlist", "path": ("settings", "terms"), "label": "涉及术语"},
        "foreshadow": {
            "kind": "objlist", "path": ("foreshadow",), "key": "id", "label": "伏笔动作",
            "fields": ["action", "id", "desc"],
            "enum_fields": {"action": ["埋设", "推进", "回收"]},
        },
    },
}

# 兼容旧名：设定集那一层单独用时就是它
SECTION_SPECS = LAYER_SPECS["setting"]

LAYERS = tuple(LAYER_SPECS.keys())
LAYER_LABELS = {"setting": "设定集", "volume": "分卷目录", "brief": "逐章创作任务指令"}

OPS = ("add", "update", "remove", "set")


# ── 计划（免费） ────────────────────────────────────────────


@dataclass
class AssistPlan:
    work: str
    provider_id: str
    model_id: str
    setting_chars: int
    refs: list[str]
    ref_chars: int
    est_input_tokens: int
    est_output_tokens: int
    est_cost_cny: float | None
    price_note: str
    warnings: list[str] = field(default_factory=list)
    # 这次真正会喂进去的参照素材。给界面用，让作者能**看见**知识库到底贡献了什么，
    # 而不是点一个「问助手」然后猜。
    refs_preview: str = ""
    level: str = DEFAULT_REFS_LEVEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "work": self.work,
            "provider": self.provider_id,
            "model": self.model_id,
            "setting_chars": self.setting_chars,
            "refs": self.refs,
            "ref_chars": self.ref_chars,
            "refs_level": self.level,
            "refs_levels": REFS_LEVELS,
            "refs_level_hints": REFS_LEVEL_HINTS,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "est_cost_cny": self.est_cost_cny,
            "price_note": self.price_note,
            "warnings": self.warnings,
            "refs_preview": self.refs_preview,
            "ready": bool(self.model_id),
        }


def build_assist_plan(
    *,
    work: str,
    setting: dict[str, Any],
    refs_block: str,
    refs: list[str],
    provider_id: str,
    model_id: str,
    level: str = DEFAULT_REFS_LEVEL,
    price_input_per_mtok: float | None = None,
    price_output_per_mtok: float | None = None,
    bucket: str | None = None,
    layer: str = "setting",
) -> AssistPlan:
    """算出这一次对话大概多少 token、多少钱。**不发起任何调用。**"""
    setting_chars = len(_render_current(setting, layer=layer))
    ref_chars = len(refs_block)
    # 真实散文的系数是 0.675（见 providers.yaml 的 measured_batch），别用探测那个 0.968
    est_in = int((setting_chars + ref_chars + 900) * 0.675)
    est_out = 700
    cost = None
    note = "未填写单价，费用无法计算。"
    if price_input_per_mtok is not None and price_output_per_mtok is not None:
        cost = round(
            est_in / 1_000_000 * price_input_per_mtok + est_out / 1_000_000 * price_output_per_mtok, 4
        )
        bucket_text = {"peak": "高峰", "offpeak": "空闲"}.get(bucket or "", "不分时段")
        note = f"按{bucket_text}单价估算：输入 {price_input_per_mtok} 元/百万、输出 {price_output_per_mtok} 元/百万。"

    warnings: list[str] = []
    if not refs or level == "none":
        warnings.append(
            "这次不注入任何已入库作品的素材：模型全凭当前设定集发挥"
            if not refs else "你选了「不注入」，参照作品虽然勾了但不会进提示词"
        )
    elif level == "k3":
        warnings.append("只注入结构指纹（统计量，零人名零情节）：借节奏，不借设定")
    if setting_chars > 12000:
        warnings.append(f"当前设定集 {setting_chars} 字，输入偏大；可以考虑先精简再对话")
    return AssistPlan(
        work=work,
        provider_id=provider_id,
        model_id=model_id,
        setting_chars=setting_chars,
        refs=refs,
        ref_chars=ref_chars,
        est_input_tokens=est_in,
        est_output_tokens=est_out,
        est_cost_cny=cost,
        price_note=note,
        warnings=warnings,
        refs_preview=refs_block,
        level=level if level in REFS_LEVELS else DEFAULT_REFS_LEVEL,
    )


# ── 提示词 ──────────────────────────────────────────────────


def _sections_help(layer: str) -> str:
    lines: list[str] = []
    for name, spec in LAYER_SPECS.get(layer, {}).items():
        line = f"  · {name}（{spec['label']}）"
        if spec["kind"] == "objlist":
            line += f"，每种条目字段：{'、'.join(spec.get('fields') or [])}"
            if spec.get("list_fields"):
                line += f"，另有数组字段 {'、'.join(spec['list_fields'])}（用、分隔的字符串）"
            if spec.get("int_fields"):
                line += f"，其中 {'、'.join(spec['int_fields'])} 必须是整数"
            if spec.get("enum_fields"):
                for key, values in spec["enum_fields"].items():
                    line += f"，{key} 只能是 {' | '.join(values)}"
            if spec.get("required"):
                line += f"，必填：{'、'.join(spec['required'])}"
            line += f"，主键 {spec['key']}"
        elif spec["kind"] == "strlist":
            line += "，字符串数组"
        else:
            line += "，单个字符串"
        lines.append(line)
    return "\n".join(lines)


_LAYER_FRAMING = {
    "setting": "长篇小说的设定集",
    "volume": "长篇小说的分卷目录（卷头 + 部分划分 + 逐章表）",
    "brief": "长篇小说某一章的创作任务指令",
}

_LAYER_RULES = {
    "setting": """- 人物必须有 name、role、motive；术语必须有 term、meaning。""",
    "volume": """- 章节条目必须有 chapter_no（整数）和 title；章号要与本卷已有的连号，不要跳号、不要重复。
- 卷头类字段（title / era / core_conflict / goal / closing）用 set。
- 一次不要重排整卷章表。只提作者点名要动的那几章。""",
    "brief": """- foreshadow 的 action 只能是「埋设」「推进」「回收」；「推进」「回收」必须引用前面已经埋设过的 id，不能凭空回收。
- 埋设新伏笔时才给新 id（形如 v001-c0007-f01）。
- 一句话概要、核心情节、禁止出现，都不要与已有的重复。""",
}


def assist_system(layer: str = "setting", task: str = "") -> str:
    """这一层的系统提示。三层共用同一个引擎，差别只在这张表与那几条规则。

    `task="writeback"` 时前置一段回填铁律：回填比起草严得多，
    因为起草提错了顶多白勾一次，回填提错了会把编出来的东西写进真源。
    """
    if layer not in LAYER_SPECS:
        layer = "setting"
    preamble = ""
    if task == "writeback":
        preamble = _WRITEBACK_PREAMBLE + "\n"
    elif task == "review":
        preamble = _REVIEW_PREAMBLE + "\n"
    return preamble + f"""你是{_LAYER_FRAMING[layer]}的起草助手。作者会告诉你要什么，你只负责**提出建议条目**。

# 你能动的位置（只能动这些，别的一律不要碰）
{_sections_help(layer)}

# 输出格式（只输出 JSON，不要任何前后说明）
{{
  "reply": "先用一两句话回应作者，说清你打算加什么、为什么",
  "proposals": [
    {{"section": "<位置名>", "op": "add", "entry": {{"<主键>": "...", "<其他字段>": "..."}}}},
    {{"section": "<位置名>", "op": "update", "entry": {{"<主键>": "已有条目的主键", "<要改的字段>": "..."}}}},
    {{"section": "brief:foreshadow", "op": "add", "entry": {{"action": "埋设", "id": "v001-c0001-f01", "desc": "他照水面，自己头顶什么都看不到"}}}},
    {{"section": "<字符串数组>", "op": "add", "value": "..."}},
    {{"section": "<单个字段>", "op": "set", "value": "..."}}
  ]
}}

# 硬约束
- op 只能是 add / update / remove / set。字符串数组用 add / remove + value；单个字段用 set + value。
- **每条提案都必须带 entry 或 value**（哪个由 op 和目标决定）。两样都没有的空壳提案不要提——
  它只会被拒，还会让作者以为你漏了东西。没东西可提就少提一条，别拿空条占位。
- update 必须带主键（objlist 的主键见上），只写要改的字段，不写的别补。
- **作者要多少就给多少，一次给足**。他说「20 个势力」就提 20 条，说「把世界观配齐」就把该有的都提出来。
  他明确要的那一类，别挤牙膏。
- 但他**没点名**的类别不要顺手加。他要势力，你别自作主张附赠一整套力量体系。
- 不要重复已有的条目。已有的名字不要换个写法再加一遍。
- 把握不准的宁可少提一条，也别凑数——凑数的只会让作者多勾掉几次。
{_LAYER_RULES.get(layer, "")}
"""


# 兼容旧名
ASSIST_SYSTEM = assist_system("setting")


def render_document(document: dict[str, Any], *, layer: str = "setting") -> str:
    """把一层文档渲染成给模型看的紧凑视图。公开入口（写正文那边也要用）。"""
    return _render_current(document, layer=layer)


def build_refs_block(
    refs: list[dict[str, Any]], *, level: str = DEFAULT_REFS_LEVEL, max_chars: int = 6000
) -> str:
    """把选定的参照作品拼成一段素材。`level` 决定给多少。

    - `none`：什么都不给
    - `k3`  ：只给结构指纹（统计量，零人名零情节）
    - `full`：再加人物/势力/能力/地点/关系格局

    实测全素材的性价比并不高（同一问题各跑一次，产出同等可用，输入却是 4 倍），
    所以默认走 `k3`：借节奏不借设定。

    超预算**点名丢了什么**，不静默截断：素材少给一半，模型会自己补，
    补出来的东西跟参照毫无关系，而作者以为它是照着知识库写的。
    """
    if not refs or level not in REFS_LEVELS or level == "none":
        return ""
    want_full = level == "full"

    def _rows(rows: Any, limit: int, fmt) -> list[str]:
        out: list[str] = []
        for row in (rows or [])[:limit]:
            if not isinstance(row, dict):
                continue
            line = fmt(row)
            if line:
                out.append(line)
        return out

    role_rank = {"主角": 0, "重要配角": 1, "配角": 2, "反派": 1, "导师": 1, "路人": 3}

    sections: list[str] = []
    dropped: list[str] = []
    used = 0
    for ref in refs:
        name = str(ref.get("work") or "")
        k1 = ref.get("k1") or {}
        blocks: list[tuple[str, list[str]]] = []

        k3_lines = [f"  - {line}" for line in (ref.get("k3") or [])[:12]]
        if k3_lines:
            blocks.append((f"{name} · 结构指纹（统计量）", k3_lines))

        if want_full:
            chars = sorted(
                (c for c in (k1.get("characters") or []) if isinstance(c, dict)),
                key=lambda c: (role_rank.get(str(c.get("role") or ""), 9), c.get("first_chapter") or 9999),
            )
            char_lines = _rows(
                chars, 40,
                lambda c: f"  - {c.get('name')}（{c.get('role') or '—'}）"
                          + (f"：{c.get('identity')}" if c.get("identity") else ""),
            )
            if char_lines:
                blocks.append((f"{name} · 人物", char_lines))

            fac_lines = _rows(
                k1.get("factions"), 15,
                lambda f: f"  - {f.get('name')}：{f.get('stance') or '—'}",
            )
            if fac_lines:
                blocks.append((f"{name} · 势力", fac_lines))

            ab_lines = _rows(
                k1.get("abilities"), 30,
                lambda a: f"  - {a.get('name')}" + (f"：{a.get('effect')}" if a.get("effect") else ""),
            )
            if ab_lines:
                blocks.append((f"{name} · 能力", ab_lines))

            loc_lines = _rows(k1.get("locations"), 20, lambda l: f"  - {l.get('name')}")
            if loc_lines:
                blocks.append((f"{name} · 地点", loc_lines))

            rel_counts: dict[str, int] = {}
            for row in k1.get("relations") or []:
                if isinstance(row, dict) and row.get("type"):
                    rel_counts[str(row["type"])] = rel_counts.get(str(row["type"]), 0) + 1
            if rel_counts:
                top = sorted(rel_counts.items(), key=lambda kv: -kv[1])[:10]
                blocks.append((f"{name} · 关系格局（类型 ↦ 条数）",
                               ["  - " + "、".join(f"{k} {v}" for k, v in top)]))

        for title, lines in blocks:
            text = f"【{title}】\n" + "\n".join(lines)
            if used + len(text) > max_chars and sections:
                dropped.append(title)
                continue
            sections.append(text)
            used += len(text) + 1

    block = "\n\n".join(sections)
    if dropped:
        block += (
            f"\n（⚠️ 参照素材超出 {max_chars} 字上限，以下小节没有注入：{'、'.join(dropped)}。"
            "要更全就少选几本参照作品，或提高上限）"
        )
    return block


REFS_USAGE_HINT = (
    "这些是**素材参照**：借它们的角色功能、势力格局、能力分层与节奏统计。"
    "不要把原作的人名、专属名词、独有设定直接搬进这部作品——那是洗稿，"
    "而且写出来一眼就认得出出处。作者要的是自己的书。"
)


def _render_current(document: dict[str, Any], *, layer: str = "setting") -> str:
    """当前文档的紧凑视图，给模型看「现在已经有什么」。三层共用。"""
    if layer == "setting":
        from .creation import render_injection_block

        return render_injection_block(document)

    if layer == "volume":
        vol = document.get("vol") or {}
        lines = [
            f"卷 {vol.get('vol')}：{vol.get('title') or '（没写卷名）'}"
            f"（第 {vol.get('start_chapter')}–{vol.get('end_chapter')} 章）",
            f"时间跨度：{vol.get('era') or '—'}",
            f"本卷核心冲突：{vol.get('core_conflict') or '—'}",
            f"本卷目标：{vol.get('goal') or '—'}",
        ]
        if vol.get("parts"):
            lines.append("部分划分：")
            for part in vol["parts"]:
                if isinstance(part, dict):
                    lines.append(
                        f"  · {part.get('title') or '—'}"
                        f"（第 {part.get('start_chapter')}–{part.get('end_chapter')} 章）"
                        f"{'：' + str(part['gist']) if part.get('gist') else ''}"
                    )
        lines.append("章表：")
        for ch in vol.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            line = f"  · 第 {ch.get('chapter_no')} 章 {ch.get('title') or '—'}"
            if ch.get("gist"):
                line += f"：{ch['gist']}"
            if ch.get("characters"):
                line += f"｜出场：{'、'.join(str(c) for c in ch['characters'])}"
            if ch.get("foreshadow"):
                line += f"｜伏笔：{ch['foreshadow']}"
            lines.append(line)
        return "\n".join(lines)

    # brief
    lines = [
        f"章节：{document.get('chapter_id') or '—'}",
        f"本章标题：{document.get('title') or '—'}",
        f"一句话概要：{document.get('one_line') or '—'}",
        f"时间位置：{document.get('timeline') or '—'}",
    ]
    if document.get("core_plot"):
        lines.append("核心情节：" + "；".join(str(x) for x in document["core_plot"]))
    narrative = document.get("narrative") or {}
    if narrative:
        lines.append(
            f"叙事：视角 {narrative.get('perspective') or '—'}"
            f"｜基调 {narrative.get('tone') or '—'}"
            + (f"｜重点 {'、'.join(str(x) for x in narrative.get('focus') or [])}"
               if narrative.get("focus") else "")
        )
    settings = document.get("settings") or {}
    for key, label in (("characters", "出场人物"), ("factions", "涉及势力"), ("terms", "涉及术语")):
        if settings.get(key):
            lines.append(f"{label}：" + "、".join(str(x) for x in settings[key]))
    for ch in document.get("foreshadow") or []:
        if isinstance(ch, dict):
            lines.append(f"伏笔·{ch.get('action') or '—'} {ch.get('id') or '—'}：{ch.get('desc') or '—'}")
    for key, label in (("carry_over", "需承接"), ("must_not", "禁止出现")):
        if document.get(key):
            lines.append(f"{label}：" + "；".join(str(x) for x in document[key]))
    return "\n".join(lines)


def build_user_prompt(
    *,
    setting: dict[str, Any],
    message: str,
    history: list[dict[str, str]] | None = None,
    refs_block: str = "",
    layer: str = "setting",
    context_block: str = "",
) -> str:
    title = f"【当前{LAYER_LABELS.get(layer, '设定集')}】"
    parts: list[str] = []
    # 写前必读放**最前面**：锚点要压在所有内容之上。放在当前文档之后就成了"事后补充"，
    # 模型先看完几百字本章指令再看到它，钉不住。
    if context_block:
        parts += [context_block, ""]
    parts += [title, _render_current(setting, layer=layer) or "（还是空的）"]
    if refs_block:
        parts += ["", "【可借的素材参照（来自已入库作品的知识库）】", refs_block, "", REFS_USAGE_HINT]
    if history:
        parts += ["", "【刚才的对话】"]
        for turn in history[-6:]:
            role = "作者" if turn.get("role") == "user" else "你"
            parts.append(f"{role}：{str(turn.get('content') or '')[:500]}")
    parts += ["", "【作者这次的要求】", message.strip()]
    return "\n".join(parts)


# ── 调用（会花钱） ──────────────────────────────────────────


@dataclass
class AssistResult:
    reply: str = ""
    proposals: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    attempts: int = 0


def run_assist(
    *,
    client: OpenAICompatProvider,
    model_id: str,
    setting: dict[str, Any],
    message: str,
    history: list[dict[str, str]] | None = None,
    refs_block: str = "",
    layer: str = "setting",
    task: str = "",
    prose: str = "",
    volume: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    title: str = "",
    temperature: float = 0.6,
    max_tokens: int = 2000,
    max_attempts: int = 2,
    thinking: str | None = "disabled",
    context_block: str = "",
) -> AssistResult:
    """要一份提案。**只返回提案，不写任何文件**——落盘是作者点了保存之后的事。

    `task="writeback"` 时走回填提示词：正文是事实来源，三层真源只是对照。
    起草是「从无到有提建议」，回填是「从既有正文里提取事实」——两条路混在一起必然出错，
    所以这里不是加几句要求，是换一整套提示词。
    """
    from .craft import craft_block

    if task == "writeback" and str(prose or "").strip():
        messages = build_writeback_prompt(
            setting=setting, volume=volume or {}, brief=brief or {},
            prose=prose, title=title,
        )
    elif task == "review" and str(prose or "").strip():
        messages = build_review_prompt(
            setting=setting, brief=brief or {}, prose=prose, title=title,
            craft=craft_block(review=True),
        )
    else:
        messages = [
            {"role": "system", "content": assist_system(layer)},
            {"role": "user", "content": build_user_prompt(
                setting=setting, message=message, history=history,
                refs_block=refs_block, layer=layer)},
        ]
    extra = build_thinking_extra(thinking, None)
    usage_total: dict[str, int] = {}
    last_error = ""
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            result: ChatResult = client.chat(
                model_id,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                extra=extra,
            )
        except ApiError as exc:
            last_error = f"{exc.kind.value}：{exc.safe_body(client.secrets)}"
            if exc.kind in (ErrorKind.BAD_REQUEST, ErrorKind.RESPONSE_UNPARSABLE):
                # 有的中转站不认 json_object，去掉再试一次
                try:
                    result = client.chat(
                        model_id,
                        messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        response_format=None,
                        extra=extra,
                    )
                except ApiError as exc2:  # noqa: PERF203
                    last_error = f"{exc2.kind.value}：{exc2.safe_body(client.secrets)}"
                    continue
            else:
                continue
        if result.usage:
            for key, value in result.usage.to_dict().items():
                if isinstance(value, int):
                    usage_total[key] = usage_total.get(key, 0) + value
        payload = extract_json_object(result.text)
        if payload is None:
            last_error = f"返回内容不是 JSON：{result.text[:120]}"
            continue
        proposals = payload.get("proposals")
        return AssistResult(
            reply=str(payload.get("reply") or ""),
            proposals=proposals if isinstance(proposals, list) else [],
            usage=usage_total,
            attempts=attempt,
        )
    return AssistResult(error=last_error, usage=usage_total, attempts=max_attempts)


# ── 应用提案（纯脚本，可脱机单测） ──────────────────────────


def _get_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _ensure_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    last = path[-1]
    if not isinstance(node.get(last), list):
        node[last] = []
    return node[last]


def describe_proposal(proposal: dict[str, Any], layer: str = "setting") -> str:
    """给界面用的一行摘要。认不出来的小节也要能显示，不能显示成空白。"""
    section = str(proposal.get("section") or "")
    spec = (LAYER_SPECS.get(layer) or LAYER_SPECS["setting"]).get(section)
    label = spec["label"] if spec else f"未知小节「{section}」"
    op = str(proposal.get("op") or "")
    if spec and spec["kind"] == "objlist":
        entry = proposal.get("entry") if isinstance(proposal.get("entry"), dict) else {}
        key = str(entry.get(spec["key"]) or "") or "（没写主键）"
        return f"{label} · {op} · {key}"
    value = str(proposal.get("value") or "")
    return f"{label} · {op} · {value[:40]}"


def apply_proposals(
    document: dict[str, Any],
    proposals: list[dict[str, Any]],
    accepted: list[int] | None = None,
    layer: str = "setting",
    allow_partial: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把作者勾中的提案并进这一层的草稿。返回 (新草稿, 逐条结果)。

    逐条结果里每条都带 ok / reason，**拒收的原因必须写出来**——
    「我勾了 5 条，只进来 3 条」而不说为什么，作者只能怀疑是自己看错了。

    `allow_partial=True` 时不查必填字段。**只给从知识库导入用**：
    知识库里的人物有名字和身份、但没有「动机」——动机是作者的事，
    拿一个占位符顶上比留空更坏。模型提案仍然走严格检查。
    """
    import copy

    specs = LAYER_SPECS.get(layer) or LAYER_SPECS["setting"]
    out = copy.deepcopy(document)
    results: list[dict[str, Any]] = []
    wanted = None if accepted is None else {int(i) for i in accepted}

    for index, raw in enumerate(proposals):
        row: dict[str, Any] = {
            "index": index,
            "section": str((raw or {}).get("section") or ""),
            "op": str((raw or {}).get("op") or ""),
            "label": describe_proposal(raw if isinstance(raw, dict) else {}, layer),
            "ok": False,
            "reason": "",
        }
        if wanted is not None and index not in wanted:
            row["reason"] = "作者没勾选这一条"
            results.append(row)
            continue
        if not isinstance(raw, dict):
            row["reason"] = "提案不是一条对象"
            results.append(row)
            continue
        spec = specs.get(row["section"])
        if spec is None:
            row["reason"] = f"不认识的小节「{row['section']}」，已拒收（模型只能动白名单里的位置）"
            results.append(row)
            continue
        op = row["op"]
        if op not in OPS:
            row["reason"] = f"不认识的动作「{op}」"
            results.append(row)
            continue

        if spec["kind"] == "scalar":
            if op != "set":
                row["reason"] = "单个字段只能用 set"
                results.append(row)
                continue
            value = str(raw.get("value") or "").strip()
            if not value:
                row["reason"] = "value 是空的"
                results.append(row)
                continue
            node = out
            for key in spec["path"][:-1]:
                node = node.setdefault(key, {})
            node[spec["path"][-1]] = value
            row["ok"] = True
            results.append(row)
            continue

        if spec["kind"] == "strlist":
            bucket = _ensure_path(out, spec["path"])
            value = str(raw.get("value") or "").strip()
            if not value:
                row["reason"] = "value 是空的"
                results.append(row)
                continue
            if op == "add":
                if value in bucket:
                    row["reason"] = f"「{value}」已经在里面了"
                else:
                    bucket.append(value)
                    row["ok"] = True
                results.append(row)
                continue
            if op == "remove":
                if value not in bucket:
                    row["reason"] = f"「{value}」本来就不在里面"
                else:
                    bucket.remove(value)
                    row["ok"] = True
                results.append(row)
                continue
            row["reason"] = "字符串数组只能用 add / remove"
            results.append(row)
            continue

        # objlist
        bucket = _ensure_path(out, spec["path"])
        if not isinstance(bucket, list):
            row["reason"] = "目标位置不是列表"
            results.append(row)
            continue
        entry = raw.get("entry")
        if op == "add":
            if not isinstance(entry, dict):
                # 把「模型实际给了什么」打出来。只说「必须带 entry 对象」的话，
                # 作者和我都看不出它到底是没给、还是给成了别的形状——
                # 上一条同类问题就是靠这句才查出来的。
                given = "、".join(str(k) for k in raw if k not in ("section", "op")) or "什么都没给"
                row["reason"] = f"add 必须带 entry 对象（模型给的是：{given}）"
                results.append(row)
                continue
            key_field = spec["key"]
            key_value = str(entry.get(key_field) or "").strip()
            if not key_value:
                row["reason"] = f"缺主键 {key_field}"
                results.append(row)
                continue
            if any(isinstance(x, dict) and str(x.get(key_field) or "") == key_value for x in bucket):
                row["reason"] = f"「{key_value}」已经存在（要改就用 update）"
                results.append(row)
                continue
            cleaned = _clean_entry(entry, spec)
            missing = [
                f for f in (spec.get("required") or [])
                if cleaned.get(f) is None or str(cleaned.get(f)).strip() == ""
            ]
            if missing and not allow_partial:
                row["reason"] = f"{spec['label']}缺必填字段：{'、'.join(missing)}"
                results.append(row)
                continue
            if missing:
                row["warning"] = f"必填字段还没写：{'、'.join(missing)}（导入的条目要你自己补）"
            bad_enum = _enum_problem(cleaned, spec)
            if bad_enum:
                row["reason"] = bad_enum
                results.append(row)
                continue
            bucket.append(cleaned)
            row["ok"] = True
            results.append(row)
            continue

        if op == "update":
            if not isinstance(entry, dict):
                row["reason"] = "update 必须带 entry 对象"
                results.append(row)
                continue
            key_field = spec["key"]
            key_value = str(entry.get(key_field) or "").strip()
            if not key_value:
                row["reason"] = f"update 必须带主键 {key_field}"
                results.append(row)
                continue
            target = next(
                (x for x in bucket if isinstance(x, dict) and str(x.get(key_field) or "") == key_value),
                None,
            )
            if target is None:
                row["reason"] = f"找不到「{key_value}」，要先 add"
                results.append(row)
                continue
            # ⚠️ update 必须**真的改到了什么**才算成功。模型用了一个不在白名单里的字段名时
            # （实测：卷表的「本章概要」字段叫 gist，它写了 summary），字段被丢掉，
            # 结果是什么都没改却报「进 1 条、已保存」——这是最坏的一种失败：看着成功、实际空转，
            # 作者以为概要已经对齐了。
            updates = {k: v for k, v in _clean_entry(entry, spec).items() if k != key_field}
            if not updates:
                given = "、".join(str(k) for k in entry if k != key_field)
                row["reason"] = (
                    "没有任何字段被改到——模型给的字段名都不在这个小节的允许值里"
                    + (f"（它给的是：{given}；允许的是：{'、'.join(spec.get('fields') or [])}）"
                       if given else "")
                )
                results.append(row)
                continue
            for field_name, field_value in updates.items():
                target[field_name] = field_value
            row["ok"] = True
            results.append(row)
            continue

        if op == "remove":
            key_value = str((entry or {}).get(spec["key"]) or "").strip() if isinstance(entry, dict) else ""
            if not key_value:
                row["reason"] = f"remove 必须带主键 {spec['key']}"
                results.append(row)
                continue
            kept = [x for x in bucket
                    if not (isinstance(x, dict) and str(x.get(spec["key"]) or "") == key_value)]
            if len(kept) == len(bucket):
                row["reason"] = f"找不到「{key_value}」"
                results.append(row)
                continue
            bucket[:] = kept
            row["ok"] = True
            results.append(row)
            continue

        row["reason"] = f"objlist 不支持动作「{op}」"
        results.append(row)

    return out, results


def _enum_problem(entry: dict[str, Any], spec: dict[str, Any]) -> str:
    """枚举字段（比如伏笔动作只能是 埋设/推进/回收）的快检。违反就整条拒收。"""
    for field_name, allowed in (spec.get("enum_fields") or {}).items():
        value = entry.get(field_name)
        if value is None:
            continue
        if str(value).strip() not in allowed:
            return f"{field_name}「{value}」不在允许值里（{' | '.join(allowed)}）"
    return ""


# ── 从知识库导入（纯脚本，零成本、可审计） ──────────────────
#
# 知识库导入不做成「一键全收」，而是**每条都由作者点**。
# 它复用的就是模型提案那套：白名单、主键去重、逐条结果与原因。
# 区别只有一个——提案不是模型生成的，是从 K1 实体表里读出来的。
# 好处是不用为导入再写一遍校验，也不会出现「导入路径比提案路径松」这种口子。

# 知识库小节的 K1 字段 ↦ 设定集小节的字段。只映射对得上的，对不上的留给作者。
KB_SECTION_MAP: dict[str, str] = {
    "characters": "characters",
    "factions": "factions",
    "abilities": "abilities",
    "locations": "places",
    "terms": "terms",
}


def kb_proposals(
    k1: dict[str, Any], *, section: str, keys: list[str] | None = None
) -> list[dict[str, Any]]:
    """把 K1 实体表里选中的条目转成提案。

    `keys` 是作者勾中的主键（人物名 / 势力名 / 能力名 / 地点名）；
    不传就是全部。K1 里没有的字段（比如人物的「动机」）**不编**——
    留空由作者填，编一个假的比留空坏得多。
    """
    target = KB_SECTION_MAP.get(section)
    if target is None:
        return []
    spec = LAYER_SPECS["setting"][target]
    key_field = spec["key"]
    wanted = {str(k) for k in keys} if keys else None

    proposals: list[dict[str, Any]] = []
    for row in k1.get(section) or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get(key_field) or row.get("name") or row.get("term") or "").strip()
        if not name or (wanted is not None and name not in wanted):
            continue
        entry: dict[str, Any] = {key_field: name}
        for field_name in spec.get("fields") or []:
            if field_name == key_field:
                continue
            value = row.get(field_name)
            if value is None and field_name == "effect":
                value = row.get("note") or row.get("desc")
            if value is None and field_name == "tier":
                value = row.get("level")
            if value is not None and str(value).strip():
                entry[field_name] = str(value).strip()
        proposals.append({"section": target, "op": "add", "entry": entry})
    return proposals


def kb_relations_proposals(
    k1: dict[str, Any], *, subject: str, existing: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """把某个人的关系整条带过来。

    关系是嵌在人物里的数组，所以语义是 `update` 那一个人的 relations 字段——
    **整条替换**，不是往后面追加。追加的话，同一个关系导两次就多一条，
    而且和作者手改过的版本混在一起分不出哪个是哪个。

    K1 的关系表是按章累积的：同一个人跟同一个对象的关系会出现很多次
    （实测张老师有 3 条「王铁柱/师生」、5 条「王铁柱/师徒」）。
    这里按 (对象, 关系) 去重——不去的话导进来是一堆重复行，作者还得自己删。
    """
    rows = [
        r for r in (k1.get("relations") or [])
        if isinstance(r, dict) and str(r.get("from") or "").strip() == subject
    ]
    if not rows:
        return []
    seen: set[tuple[str, str]] = set()
    relations: list[dict[str, str]] = []
    for r in rows:
        to = str(r.get("to") or "").strip()
        kind = str(r.get("type") or "").strip()
        if not to or (to, kind) in seen:
            continue
        seen.add((to, kind))
        relations.append({"to": to, "type": kind})
    if not relations:
        return []
    return [{
        "section": "characters", "op": "update",
        "entry": {"name": subject, "relations": relations},
    }]


def _clean_entry(entry: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """只留白名单里的字段。模型多给的字段一律丢掉。

    ⚠️ 整数字段必须转回 int，不能跟着别的字段一起被 str() 掉。
    章号变成字符串「7」，卷表的连号校验当场就崩——而且报出来的错
    会是「缺 start_chapter」这类看着毫不相干的话，很难查回这里。

    ⚠️ `allowed` 要把 int_fields / enum_fields / 主键也算进去。只按 fields 算的话，
    章号会在转 int **之前**就被过滤掉，报出来的错是「缺必填 chapter_no」——
    明明作者给了章号。这个坑实测踩过一次。
    """
    allowed = set(spec.get("fields") or []) | set(spec.get("list_fields") or [])
    allowed |= set(spec.get("int_fields") or []) | set(spec.get("enum_fields") or {})
    allowed.add("relations")
    if spec.get("key"):
        allowed.add(spec["key"])
    # 别名：模型爱用的同义词映射到白名单里的真名（实测「本章概要」它反复写 summary）。
    # **这不放松白名单**——别名映射之后仍然要落在 allowed 里，映射到列表外照样丢。
    aliases = {str(k): str(v) for k, v in (spec.get("aliases") or {}).items()}
    for k, v in aliases.items():
        if v not in allowed:
            raise KeyError(f"别名 {k}→{v} 的目标不在白名单里，配置写错了")
    int_fields = set(spec.get("int_fields") or [])
    list_fields = set(spec.get("list_fields") or [])
    cleaned: dict[str, Any] = {}
    for raw_name, value in entry.items():
        field_name = aliases.get(raw_name, raw_name)
        if field_name not in allowed:
            continue
        if field_name in int_fields:
            try:
                cleaned[field_name] = int(str(value).strip())
            except (TypeError, ValueError):
                # 非整数就丢掉，让这一层的校验去报错，别在这里编一个值出来
                continue
        elif field_name in list_fields and isinstance(value, list):
            cleaned[field_name] = [str(v).strip() for v in value if str(v).strip()]
        elif field_name == "relations" and isinstance(value, list):
            cleaned[field_name] = [
                {"to": str(r.get("to") or "").strip(), "type": str(r.get("type") or "").strip()}
                for r in value
                if isinstance(r, dict) and str(r.get("to") or "").strip()
            ]
        elif isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                cleaned[field_name] = text
    return cleaned


# ── 回填：把正文里实际发生的提回真源 ────────────────────────
#
# 为什么需要它：三层真源是作者手填的，正文一写出来真源就落后了。
# 写到第 10 章，模型不知道第 3 章里谁死了、哪个伏笔已经回收了——
# 不闭合的话，作者迟早要手工回溯，而那正是这套东西该替他省掉的活。
#
# 与「起草」的根本差别：起草是从无到有地提建议，回填是**从既有正文里提取事实**。
# 所以铁律只有一条：正文里没有的，一个字都不许提。

_WRITEBACK_PREAMBLE = """你是长篇小说的**回填**助手。作者刚写完一章正文，你要把正文里**实际发生过的**东西提回设定集/卷表。

# 回填的铁律（比起草严得多）
- **只提正文里真的出现过的**。正文没写的人、没用过的能力、没埋的伏笔，一律不许提。
- 人名、术法名要与正文**完全一致**，不要顺手改名、不要补全称、不要统一格式。
- 只有**有名字**的人物、能力、地点、术语才值得提。正文里的一次性过场角色
  （「那个少年」「摊主」）不要提——没有名字，提了也没法用。
- **回填写回三层，小节名要带层前缀**：`setting:abilities`、`volume:chapters`、`brief:foreshadow`
  这种。不带前缀的一律按 setting 处理。
  前缀是必须的：`title` 在卷层和指令层都有、`characters` 在设定集是条目表而在指令层是字符串数组，
  光看裸名根本分不出你指的是哪一个。
- `volume:chapters` 只用来改**本章那一行**的概要（`op: update`，主键是 `chapter_no`），
  让它与正文实际发生的事对齐。不要动别的章，也不要重排章表。
- `brief:foreshadow` 只在正文里真的埋了或收了伏笔时才提。
- 宁可不提，也不要凑数。凑数的只会让作者多勾几次，然后开始不信任这份清单。
"""


def build_writeback_prompt(
    *,
    setting: dict[str, Any] | None = None,
    volume: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    prose: str = "",
    title: str = "",
) -> list[dict[str, str]]:
    """回填的提示词：正文是唯一事实来源，三层真源只是「现在记着什么」的对照。"""
    parts: list[str] = []
    if title:
        parts.append(f"（这一章是「{title}」。）")
    parts += ["【这一章的正文 · 唯一事实来源】", str(prose).strip(), ""]
    if setting:
        parts += ["【设定集现在记着什么】", render_document(setting, layer="setting"), ""]
    if volume:
        parts += ["【本卷目录现在记着什么】", render_document(volume, layer="volume"), ""]
    if brief:
        parts += ["【这一章的创作任务指令】", render_document(brief, layer="brief"), ""]
    parts += [
        "【要你做的事】",
        "逐条对照上面的正文，往三个地方回填（小节名**必须带层前缀**）：",
        "· `setting:*` —— 正文里真出现了、而设定集还没有的**有名字的**人物、能力、地点、术语，用 add 补上；",
        "  设定集已有的条目被正文补充了信息（动机、性格、所属势力、表现形式的细节），用 update 补上。",
        "· `volume:chapters` —— 用 update 把**本章那一行**的概要改成正文实际发生的事。",
        "· `brief:foreshadow` —— 正文里真的埋了或收了伏笔时才提。",
        "**正文里没有的一律不要提。** 提之前先在心里过一遍正文，对不上就删掉那一条。",
    ]
    return [
        {"role": "system", "content": assist_system("setting", task="writeback")},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ── 技法审稿 ────────────────────────────────────────────────
#
# 机械自检查得了「字数/禁令/漏人/截断」，查不了「开头抓不抓人」「爽点结构完不完整」
# 「有没有穿帮」。这一块靠技法库里的审查维度，产出**对本章指令的修正提案**。
#
# 为什么不直接改正文：正文是成品，模型改一遍等于重写，作者还得逐句对比。
# 而指令是"写这一章之前该定死的东西"——审稿意见落回指令，再重写一章，
# 作者既看得见改了什么，也能否决。这是闭环，不是返工。

_REVIEW_PREAMBLE = """你是长篇小说的**技法审稿**。作者刚写完一章，你要按审查维度挑问题，并给出**对本章指令的修正**。

# 你要交什么
两样：
1. `reply` 里给**审查结论**：按维度过一遍，说清这一章哪几项达标、哪几项不达标。要具体到章节里的位置（「第三段」「结尾」），不要说不痛不痒的场面话。
2. `proposals` 里给**对本章创作任务指令的修正**：把审查发现的问题，翻译成「下次重写这一章时指令该怎么改」。

# 审稿铁律
- **只提真问题。** 没问题的维度就说没问题，不要为了凑数硬挑。
  一次审稿提三五条真问题，比提十五条套话有用得多——套话会让作者不再看审查结论。
- 提的每一条都要**指得出在这一章的哪里**。指不出来就说明你没读到，那就别提。
- 修正要落在**指令的小节**上（`one_line` / `core_plot` / `carry_over` / `must_not` /
  `narrative_focus` / `narrative_tone` / `settings.*` / `foreshadow`），
  因为这些是下次重写时真正会被执行的东西。
- 不要提「多加点细节」「增强张力」这种没法执行的。要提「开头三段都是环境描写，
  改成从动作切入」这种能直接落成指令的。
"""


def build_review_prompt(
    *,
    setting: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    prose: str = "",
    title: str = "",
    craft: str = "",
) -> list[dict[str, str]]:
    """审稿的提示词：正文 + 本章指令 + 技法审查维度。"""
    parts: list[str] = []
    if title:
        parts.append(f"（这一章是「{title}」。）")
    parts += ["【这一章的正文 · 要审的东西】", str(prose).strip(), ""]
    if brief:
        parts += ["【本章创作任务指令 · 修正就落在它身上】",
                  render_document(brief, layer="brief"), ""]
    if setting:
        parts += ["【设定集 · 判断有没有穿帮的依据】",
                  render_document(setting, layer="setting"), ""]
    if craft:
        parts += ["【审查维度与质量清单 · 按它过一遍】", craft, ""]
    parts += [
        "【要你做的事】",
        "按上面的审查维度逐项过一遍这一章。",
        "`reply` 里给结论：哪几项达标、哪几项不达标，不达标的要指出在正文的什么位置。",
        "`proposals` 里给**对本章指令的修正**：审出来的问题，翻成下次重写时指令该怎么改。",
        "没有问题的维度不要硬挑；宁可只提三条真的。",
    ]
    return [
        {"role": "system", "content": assist_system("brief", task="review")},
        {"role": "user", "content": "\n".join(parts)},
    ]
