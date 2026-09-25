"""M9 创作台 · 规划层：原创工作区 + 设定集。

设计出处：`小说创作工坊-通用设计方案.md`（v2）定义了 M0–M8，覆盖「接入 → 标注 →
知识库 → 题材聚合 → 通用规则 → 对比 → 重写 → 校验 → 融合骨架」。**从零写一本新书
不在那份文档的范围里**，所以这里新开一个模块号 M9，沿用它既有的架构惯例：
仍是 L1 工作区层里的一条流水线，仍然走「计划免费 → 确认 → 执行 → 状态文件续跑」，
产物仍然落文件、人可读可改。

M9 分两半，本文件是后半的**数据层**，也是整个 M9 的输入：

  人工真源  ──▶  设定集（60-setting/setting.yaml）        ← 本文件
                  ↓
  模型产出  ──▶  分卷目录（70-volume/）→ 逐章创作任务指令（80-brief/）
                  ↓
  下一阶段  ──▶  正文成稿（90-draft/，M9 之外）

## 为什么设定集是 YAML 而不是让模型自己记

预案（`02_策划蓝图/`）里的《核心设定术语表》《出场人物设定》《详细目录》都是**人写出来
再反复改版本**的东西（v1.0→v2.2）。设定集必须满足同样的用法：人可以直接手改、
能 diff、改错了能看出来。所以它是注释友好的 YAML，模型只负责起草，落盘之后归人所有。

## 字段基准

字段形状照抄预案的实际产物，不另发明：
  · 《核心设定术语表》→ world / power / factions / terms / themes
  · 《出场人物设定》  → characters（基本信息 / 性格 / 能力 / 在本卷中的作用 / 人物关系）
  · 《项目创意策划书》→ 项目基本信息 + style + target
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .primitives import yaml_scalar

SETTING_SCHEMA_VERSION = "setting-v1"

# 原创工作区的目录编号从 60 起：00-50 是「分析已入库作品」那条链的，
# 留着不动，这样同一部作品将来既可以是原创的，也可以走标注/体检/知识库。
SETTING_DIR = "60-setting"
VOLUME_DIR = "70-volume"
BRIEF_DIR = "80-brief"
DRAFT_DIR = "90-draft"

SETTING_BASENAME = "setting.yaml"
ORIGINAL_KIND = "original"

_ROLES = ("主角", "重要配角", "配角", "反派", "导师", "路人")
_PERSPECTIVES = ("第一人称", "第三限知", "第三全知")


# ── 骨架 ────────────────────────────────────────────────────


def empty_setting(work: str = "", genre: str = "") -> dict[str, Any]:
    return {
        "schema_version": SETTING_SCHEMA_VERSION,
        "work": work,
        "genre": genre,
        "logline": "",
        "core_motive": "",
        "target": {"chapters": 0, "chars_per_chapter": [2800, 4000]},
        "style": {"perspective": "第三限知", "tone": "", "taboo": []},
        "world": {"era": "", "places": [], "rules": []},
        "power": {"system": "", "tiers": []},
        "factions": [],
        "characters": [],
        "terms": [],
        "themes": [],
    }


def setting_template_text(
    work: str,
    genre: str,
    *,
    logline: str = "",
    protagonist: str = "",
    core_motive: str = "",
) -> str:
    """给人手填的带注释骨架。

    注释是给写作者看的说明，不是机器读的——第一版必须能「打开就填」，
    否则写作者会退回去用 Word 写设定，设定集就又一次变成摆设。

    已经知道的值（书名/题材/前提/主角/动机）直接填进去，不要留空让人再抄一遍。
    """
    if protagonist:
        lead_block = f"""characters:
  - name: {yaml_scalar(protagonist)}
    role: 主角              # {' | '.join(_ROLES)}
    identity: ""
    personality: ""
    motive: {yaml_scalar(core_motive)}
    arc: ""
    abilities: []
    faction: ""
    relations: []
# 再加人物就照上面的形状往下写：
#  - name: 苏清月
#    role: 重要配角
#    identity: 青云宗大师姐
#    motive: 守住宗门
#    faction: 青云宗
#    relations:
#      - {{to: {protagonist}, type: 师姐}}
"""
    else:
        lead_block = f"""characters: []
#  - name: 李默
#    role: 主角              # {' | '.join(_ROLES)}
#    identity: 穿越者，外门弟子
#    personality: 吐槽役，嘴上认怂心里有数
#    motive: 活着回去
#    arc: 从只想自保到愿意扛事
#    abilities: [数值之眼]
#    faction: 青云宗
#    relations:
#      - {{to: 苏清月, type: 师姐}}
"""

    return f"""# 设定集 · {work}
#
# 这是 M9 创作台的真源：分卷目录、逐章创作任务指令、正文都从这里取约束。
# 直接手改，不要靠模型猜。改完跑 `python create_cli.py --check {work}` 自查。
#
# 只写「必须稳定」的东西：改一次就要连累几十章的那些。
# 临时想法写在别处，别写进来。

schema_version: {SETTING_SCHEMA_VERSION}

work: {yaml_scalar(work)}
genre: {yaml_scalar(genre)}

# 一句话前提：谁，在什么处境下，要做什么，代价是什么。不超过 80 字。
logline: {yaml_scalar(logline)}

# 主角核心动机（一句话）。它同时是标注原语 P21 的度量对象，写不清那一列数据就不可信。
core_motive: {yaml_scalar(core_motive)}

# 篇幅目标。分卷目录的估费用它算。
target:
  chapters: 0
  chars_per_chapter: [2800, 4000]

# 文风硬约束。perspective 会被逐章校验，写错方向会满篇假警。
style:
  perspective: 第三限知      # 第一人称 | 第三限知 | 第三全知
  tone: ""                   # 例：轻松搞笑、冷峻克制
  taboo: []                  # 例：["不用破折号", "不写第一人称内心独白"]

world:
  era: ""                    # 时代/纪年
  places: []                 # - {{name: 地名, note: 一句话说明}}
  rules: []                  # 世界硬规则，一条一句：- "查克拉用尽会昏迷"

power:
  system: ""                 # 力量体系名
  tiers: []                  # 由低到高，一行一个：- 炼气 / - 筑基

# 势力。leader 与 characters 里的名字要对得上（校验会查）。
factions: []
#  - name: 青云宗
#    stance: 修真界第一大宗，表面中立
#    leader: 玄阳真人

# 人物。name / role / motive 必填；relations.to 指向另一个人物名，外部人物允许但会提示。
{lead_block}
# 术语表：名字一旦定了就别再漂。写正文时模型最容易在这里跑偏。
terms: []
#  - {{term: 数值之眼, meaning: 能看到目标的修为数值}}

themes: []                   # 主题与象征
"""


# ── 读写 ────────────────────────────────────────────────────


def load_setting(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"设定集格式异常（顶层不是映射）：{p}")
    return data


# ── 校验 ────────────────────────────────────────────────────


def _names(rows: Any) -> list[str]:
    out: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            name = str(row.get("name") or row.get("term") or "").strip()
            if name:
                out.append(name)
    return out


def validate_setting(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """返回 (阻断项, 提示项)。

    阻断 = 缺了它就没法生成（会产出自相矛盾的目录或指令）。
    提示 = 能生成，但多半是漏了或者前后不一致，值得看一眼。

    规矩和项目其他地方一致：**不猜、不填默认值**——宁可报出来让人补。
    """
    errors: list[str] = []
    warnings: list[str] = []

    for key, label in (("work", "作品名"), ("genre", "题材"), ("logline", "一句话前提"),
                       ("core_motive", "主角核心动机")):
        if not str(data.get(key) or "").strip():
            errors.append(f"缺 {label}（{key}）")

    chars = [c for c in (data.get("characters") or []) if isinstance(c, dict)]
    if not chars:
        errors.append("characters 是空的——没有人物就没法生成分卷目录")

    seen: dict[str, int] = {}
    for c in chars:
        name = str(c.get("name") or "").strip()
        if not name:
            errors.append("有一个人物没写 name")
            continue
        seen[name] = seen.get(name, 0) + 1
        for key, label in (("role", "定位"), ("motive", "动机")):
            if not str(c.get(key) or "").strip():
                errors.append(f"人物「{name}」缺 {label}（{key}）")
        role = str(c.get("role") or "").strip()
        if role and role not in _ROLES:
            warnings.append(f"人物「{name}」的 role「{role}」不在常用取值里：{' | '.join(_ROLES)}")

    for name, count in seen.items():
        if count > 1:
            errors.append(f"人物「{name}」出现了 {count} 次")

    leads = [str(c.get("name") or "").strip() for c in chars
             if str(c.get("role") or "").strip() == "主角"]
    if len(leads) != 1:
        errors.append(f"role=主角 的人物应当恰好 1 个，实际 {len(leads)} 个：{leads or '无'}")

    faction_names = set(_names(data.get("factions")))
    for c in chars:
        name = str(c.get("name") or "").strip()
        faction = str(c.get("faction") or "").strip()
        if faction and faction_names and faction not in faction_names:
            warnings.append(f"人物「{name}」的 faction「{faction}」不在 factions 里")
        for rel in c.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            other = str(rel.get("to") or "").strip()
            if other and other not in seen:
                # 原作人物、只被提及的人都会走到这里，所以是提示不是阻断。
                warnings.append(f"人物「{name}」的关系指向「{other}」，但它不在 characters 里")

    for row in data.get("factions") or []:
        if not isinstance(row, dict):
            continue
        leader = str(row.get("leader") or "").strip()
        fname = str(row.get("name") or "").strip()
        if leader and leader not in seen:
            warnings.append(f"势力「{fname}」的首领「{leader}」不在 characters 里")

    style = data.get("style") or {}
    perspective = str(style.get("perspective") or "").strip()
    if not perspective:
        warnings.append("style.perspective 没写，逐章的人称校验会失效")
    elif perspective not in _PERSPECTIVES:
        errors.append(f"style.perspective「{perspective}」不合法：{' | '.join(_PERSPECTIVES)}")

    target = data.get("target") or {}
    if not isinstance(target.get("chapters"), int) or target.get("chapters", 0) <= 0:
        warnings.append("target.chapters 没填，分卷目录的费用估算会缺一项")

    if not _names(data.get("terms")):
        warnings.append("terms 是空的——术语表是防「名字漂移」最省事的一道闸")
    if not data.get("themes"):
        warnings.append("themes 是空的")

    return errors, warnings


# ── 渲染 ────────────────────────────────────────────────────


def render_setting_markdown(data: dict[str, Any]) -> str:
    """人读版。对标预案里的《核心设定术语表》。"""
    lines = [f"# 设定集 · {data.get('work') or '(未命名)'}", ""]
    meta = [f"题材：{data.get('genre') or '—'}"]
    target = data.get("target") or {}
    if target.get("chapters"):
        span = target.get("chars_per_chapter") or []
        each = f"，每章 {span[0]}-{span[1]} 字" if len(span) == 2 else ""
        meta.append(f"目标：{target['chapters']} 章{each}")
    lines.append("　".join(meta))
    lines.append("")
    if data.get("logline"):
        lines += [f"> {data['logline']}", ""]
    if data.get("core_motive"):
        lines += [f"**主角核心动机**：{data['core_motive']}", ""]

    style = data.get("style") or {}
    if style:
        lines.append("## 文风硬约束")
        lines.append(f"- 视角：{style.get('perspective') or '—'}")
        if style.get("tone"):
            lines.append(f"- 基调：{style['tone']}")
        for t in style.get("taboo") or []:
            lines.append(f"- 禁止：{t}")
        lines.append("")

    world = data.get("world") or {}
    if any(world.get(k) for k in ("era", "places", "rules")):
        lines.append("## 世界观")
        if world.get("era"):
            lines.append(f"- 时代：{world['era']}")
        for p in world.get("places") or []:
            if isinstance(p, dict):
                lines.append(f"- 地点 **{p.get('name')}**：{p.get('note') or ''}")
        for r in world.get("rules") or []:
            lines.append(f"- 硬规则：{r}")
        lines.append("")

    power = data.get("power") or {}
    if power.get("system") or power.get("tiers"):
        lines.append("## 力量体系")
        if power.get("system"):
            lines.append(f"- 体系：{power['system']}")
        if power.get("tiers"):
            lines.append("- 等级：" + " → ".join(str(t) for t in power["tiers"]))
        lines.append("")

    if data.get("factions"):
        lines.append("## 势力")
        lines.append("| 势力 | 立场 | 首领 |")
        lines.append("|---|---|---|")
        for f in data["factions"]:
            if isinstance(f, dict):
                lines.append(f"| {f.get('name')} | {f.get('stance') or '—'} | {f.get('leader') or '—'} |")
        lines.append("")

    if data.get("characters"):
        lines.append("## 人物")
        for c in data["characters"]:
            if not isinstance(c, dict):
                continue
            head = f"### {c.get('name')}（{c.get('role') or '—'}）"
            lines.append(head)
            for key, label in (("identity", "身份"), ("personality", "性格"), ("motive", "动机"),
                               ("arc", "弧光"), ("faction", "所属")):
                if c.get(key):
                    lines.append(f"- {label}：{c[key]}")
            if c.get("abilities"):
                lines.append("- 能力：" + "、".join(str(a) for a in c["abilities"]))
            for rel in c.get("relations") or []:
                if isinstance(rel, dict):
                    lines.append(f"- 关系：{rel.get('type') or '—'} → {rel.get('to')}")
            lines.append("")

    if data.get("terms"):
        lines.append("## 术语表")
        lines.append("| 术语 | 含义 |")
        lines.append("|---|---|")
        for t in data["terms"]:
            if isinstance(t, dict):
                lines.append(f"| {t.get('term')} | {t.get('meaning') or '—'} |")
        lines.append("")

    if data.get("themes"):
        lines.append("## 主题与象征")
        lines.append("、".join(str(t) for t in data["themes"]))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# 注入时按这个顺序填，超预算就从后往前丢。
_INJECTION_SECTIONS = (
    "basis", "style", "characters", "world", "factions", "power", "abilities", "terms",
)
# 告警里要报中文小节名——报英文 key 的话，人看到「characters 没注入」还得回去查代码。
_SECTION_LABELS = {
    "basis": "基本信息",
    "style": "文风硬约束",
    "characters": "人物",
    "world": "世界观",
    "factions": "势力",
    "power": "力量体系",
    "abilities": "能力体系",
    "terms": "术语",
}


def render_injection_block(data: dict[str, Any], *, max_chars: int | None = None) -> str:
    """给模型看的紧凑设定卡。分卷目录、逐章指令、正文都用它。

    超出 max_chars 时**不会静默截断**：丢掉的小节会在末尾点名。
    设定被悄悄吃掉一半，生成出来的东西看着合理却处处拧着——那是最难查的一类问题。
    """
    sections: dict[str, list[str]] = {}

    basis = [f"【作品】{data.get('work') or '—'}", f"【题材】{data.get('genre') or '—'}"]
    if data.get("logline"):
        basis.append(f"【一句话前提】{data['logline']}")
    if data.get("core_motive"):
        basis.append(f"【主角核心动机】{data['core_motive']}")
    target = data.get("target") or {}
    span = target.get("chars_per_chapter") or []
    if target.get("chapters"):
        each = f"，每章 {span[0]}-{span[1]} 字" if len(span) == 2 else ""
        basis.append(f"【篇幅】约 {target['chapters']} 章{each}")
    sections["basis"] = basis

    style = data.get("style") or {}
    style_lines = ["【文风硬约束】"]
    if style.get("perspective"):
        style_lines.append(f"视角：{style['perspective']}")
    if style.get("tone"):
        style_lines.append(f"基调：{style['tone']}")
    for t in style.get("taboo") or []:
        style_lines.append(f"禁止：{t}")
    sections["style"] = style_lines if len(style_lines) > 1 else []

    char_lines = ["【人物】"]
    for c in data.get("characters") or []:
        if not isinstance(c, dict):
            continue
        bits = [f"{c.get('name')}（{c.get('role') or '—'}"]
        if c.get("faction"):
            bits.append(f"，{c['faction']}")
        bits.append("）")
        line = "".join(bits)
        for key, label in (("identity", "身份"), ("personality", "性格"), ("motive", "动机"),
                           ("arc", "弧光")):
            if c.get(key):
                line += f" {label}：{c[key]}；"
        if c.get("abilities"):
            line += f" 能力：{'、'.join(str(a) for a in c['abilities'])}；"
        rels = [f"{r.get('type') or '—'}→{r.get('to')}"
                for r in (c.get("relations") or []) if isinstance(r, dict)]
        if rels:
            line += " 关系：" + "、".join(rels)
        char_lines.append("- " + line.rstrip("；"))
    sections["characters"] = char_lines if len(char_lines) > 1 else []

    world = data.get("world") or {}
    world_lines = ["【世界观】"]
    if world.get("era"):
        world_lines.append(f"时代：{world['era']}")
    for p in world.get("places") or []:
        if isinstance(p, dict):
            world_lines.append(f"地点 {p.get('name')}：{p.get('note') or ''}")
    for r in world.get("rules") or []:
        world_lines.append(f"硬规则：{r}")
    sections["world"] = world_lines if len(world_lines) > 1 else []

    faction_lines = ["【势力】"]
    for f in data.get("factions") or []:
        if isinstance(f, dict):
            faction_lines.append(
                f"{f.get('name')}：{f.get('stance') or '—'}"
                + (f"（首领 {f['leader']}）" if f.get("leader") else "")
            )
    sections["factions"] = faction_lines if len(faction_lines) > 1 else []

    power = data.get("power") or {}
    power_lines = []
    if power.get("system") or power.get("tiers"):
        power_lines.append("【力量体系】" + str(power.get("system") or ""))
        if power.get("tiers"):
            power_lines.append("等级：" + " → ".join(str(t) for t in power["tiers"]))
    sections["power"] = power_lines

    ability_lines = ["【能力体系】"]
    for a in data.get("abilities") or []:
        if not isinstance(a, dict):
            continue
        line = f"{a.get('name')}"
        for key, label in (("tier", "等级"), ("holder", "持有者"), ("effect", "效果")):
            if a.get(key):
                line += f"｜{label}：{a[key]}"
        ability_lines.append(line)
    sections["abilities"] = ability_lines if len(ability_lines) > 1 else []

    term_lines = ["【术语】"]
    for t in data.get("terms") or []:
        if isinstance(t, dict):
            term_lines.append(f"{t.get('term')}={t.get('meaning') or ''}")
    sections["terms"] = term_lines if len(term_lines) > 1 else []

    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for key in _INJECTION_SECTIONS:
        block = sections[key]
        if not block:
            continue
        text = "\n".join(block)
        # 第一条（基本信息）永远保留，哪怕它自己就超了：设定卡没有头部就没有意义。
        # 宁可超预算并**明说**超了，也不把句子拦腰砍断——半截设定比没有设定更坏。
        if max_chars is not None and used + len(text) > max_chars and kept:
            dropped.append(_SECTION_LABELS.get(key, key))
            continue
        kept.append(text)
        used += len(text) + 1

    out = "\n".join(kept)
    if dropped:
        out += (
            f"\n（⚠️ 设定集超出 {max_chars} 字上限，以下小节没有注入：{'、'.join(dropped)}。"
            "改小设定或调高上限，不要当作它们不存在）"
        )
    return out


# ── 建立原创工作区 ──────────────────────────────────────────

_TEMPLATE_WORK_YAML = """# 原创作品配置 · {work}
#
# kind: original —— 这部作品是「从零写」的，不是录入别人的书。
#    没有 00-ingest，因此标注/体检/知识库那条链暂时跑不了（那些阶段要 manifest）。
#    等 90-draft 里攒出定稿章节，再走一次录入就能把自己写的书也纳入结构分析。
#
# protagonist / core_motive 由 60-setting/setting.yaml 同步而来，
# **改主角或动机请改设定集**，别只改这里——标注原语 P21 的度量对象就是这两个字段。

work: {work}
kind: {kind}
genre: {genre}
protagonist: {protagonist}
core_motive: {core_motive}

# 风格硬约束（机器可校验，全部由脚本检测，不花模型成本）。
# 想让逐章校验真的盯住人称/破折号/句式，就在这里写规则；留空等于不检查。
style_checks: []

ingest:
  chapter_patterns: []
  section_markers: [番外, 外传, 前传, 序章, 楔子, 尾声]
"""


def create_original_work(
    workspaces_root: str | Path,
    name: str,
    genre: str = "",
    *,
    logline: str = "",
    protagonist: str = "",
    core_motive: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """建一个原创工作区。已存在则拒绝（除非 overwrite）。

    拒绝而不是覆盖：工作区里可能有几十万字的稿子，一次手滑不该把它抹掉。
    """
    root = Path(workspaces_root)
    work_dir = root / name
    if work_dir.exists() and not overwrite:
        raise FileExistsError(f"工作区已存在：{work_dir}（要重建请先自己移走或删掉）")

    dirs = {
        key: work_dir / key
        for key in (SETTING_DIR, VOLUME_DIR, BRIEF_DIR, DRAFT_DIR)
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    setting = empty_setting(name, genre)
    setting["logline"] = logline
    setting["core_motive"] = core_motive
    if protagonist:
        setting["characters"] = [{"name": protagonist, "role": "主角", "motive": core_motive}]

    # 只有一份 setting.yaml：带注释、人手填、就是真源。
    # 不另存一份机器版——两份就会有一份先过期，而设定集过期是最难发现的一类错。
    setting_path = dirs[SETTING_DIR] / SETTING_BASENAME
    if not setting_path.exists():
        setting_path.write_text(
            setting_template_text(
                name, genre, logline=logline, protagonist=protagonist, core_motive=core_motive
            ),
            encoding="utf-8",
        )

    # 第一卷给个骨架，省得去猜文件名和字段。卷区间先按 1-100 起，填的时候改。
    from . import volume as vo

    first_volume = vo.volume_path(dirs[VOLUME_DIR], 1)
    if not first_volume.exists():
        first_volume.write_text(vo.volume_template_text(name, 1, 1, 100), encoding="utf-8")

    work_yaml = work_dir / "work.yaml"
    work_yaml.write_text(
        _TEMPLATE_WORK_YAML.format(
            work=name, kind=ORIGINAL_KIND, genre=genre,
            protagonist=protagonist, core_motive=core_motive,
        ),
        encoding="utf-8",
        # 只在新建时写；已存在时上面已经 raise 过了，这里不会再覆盖。
    )

    return {
        "work": name,
        "kind": ORIGINAL_KIND,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "work_dir": work_dir,
        "dirs": dirs,
        "work_yaml": work_yaml,
        "setting": setting_path,
        "first_volume": first_volume,
    }


def sync_work_config(work_dir: str | Path) -> dict[str, Any]:
    """把设定集里的主角与核心动机同步进 work.yaml。

    只同步这两个字段：它们是标注原语 P21 的度量对象，两处不一致会让那一列数据不可信。
    其余字段（style_checks / ingest）归 work.yaml 自己管。
    """
    wd = Path(work_dir)
    setting_path = wd / SETTING_DIR / SETTING_BASENAME
    if not setting_path.exists():
        raise FileNotFoundError(f"找不到设定集：{setting_path}")
    setting = load_setting(setting_path)

    leads = [str(c.get("name") or "").strip() for c in (setting.get("characters") or [])
             if isinstance(c, dict) and str(c.get("role") or "").strip() == "主角"]
    work_yaml = wd / "work.yaml"
    data = yaml.safe_load(work_yaml.read_text(encoding="utf-8")) or {} if work_yaml.exists() else {}
    before = (data.get("protagonist"), data.get("core_motive"))
    data["work"] = str(setting.get("work") or data.get("work") or wd.name)
    data["kind"] = str(data.get("kind") or ORIGINAL_KIND)
    data["genre"] = str(setting.get("genre") or data.get("genre") or "")
    data["protagonist"] = leads[0] if leads else ""
    data["core_motive"] = str(setting.get("core_motive") or "")
    data.setdefault("style_checks", [])
    data.setdefault("ingest", {"chapter_patterns": [], "section_markers": []})
    work_yaml.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8"
    )
    return {
        "work_yaml": work_yaml,
        "protagonist": data["protagonist"],
        "core_motive": data["core_motive"],
        "changed": before != (data["protagonist"], data["core_motive"]),
    }


def setting_path_of(work_dir: str | Path) -> Path:
    return Path(work_dir) / SETTING_DIR / SETTING_BASENAME


def load_work_setting(work_dir: str | Path) -> dict[str, Any]:
    return load_setting(setting_path_of(work_dir))


def seed_briefs(work_dir: str | Path, *, vol_no: int | None = None) -> dict[str, Any]:
    """按卷表 + 设定集给缺指令的章播种骨架。

    播下去的 title / one_line / 字数 / 视角都是从上游抄来的——手抄一遍容易漂，
    而「指令和卷表对不上」正是这一层最该避免的错。已有的文件一律不动。
    """
    from . import chapter_brief as cb
    from . import volume as vo

    wd = Path(work_dir)
    setting = load_work_setting(wd)
    span = (setting.get("target") or {}).get("chars_per_chapter") or []
    word_target = (span[0], span[1]) if len(span) == 2 and all(isinstance(x, int) for x in span) else (2800, 4000)
    perspective = str(((setting.get("style") or {}).get("perspective")) or "")
    work = str(setting.get("work") or wd.name)

    volumes, broken = vo.load_all_volumes(wd / VOLUME_DIR)
    brief_dir = wd / BRIEF_DIR
    created: list[str] = []
    skipped: list[str] = []
    for no in sorted(volumes):
        if vol_no is not None and no != vol_no:
            continue
        vol = volumes[no].get("vol") or {}
        for row in vol.get("chapters") or []:
            if not isinstance(row, dict):
                continue
            chapter_no = row.get("chapter_no")
            if not isinstance(chapter_no, int):
                continue
            cid = cb.chapter_id_of(no, chapter_no)
            target = cb.brief_path(brief_dir, cid)
            if target.exists():
                skipped.append(cid)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                cb.brief_template_text(
                    work, no, chapter_no,
                    title=str(row.get("title") or ""),
                    gist=str(row.get("gist") or ""),
                    word_target=word_target,
                    perspective=perspective,
                ),
                encoding="utf-8",
            )
            created.append(cid)
    return {"created": created, "skipped": skipped, "broken_volumes": broken}


def chain_report(work_dir: str | Path) -> dict[str, Any]:
    """三层链路体检：设定集 → 分卷目录 → 逐章创作任务指令。

    每一层都报**完成度**，而不只是"有没有"：一卷填了一半、一百章只写了三章指令，
    「有文件」和「能往下走」是两回事。所有阻断项汇总在最后。
    """
    from . import chapter_brief as cb
    from . import volume as vo

    wd = Path(work_dir)
    report: dict[str, Any] = {"work": wd.name, "layers": {}, "errors": [], "warnings": []}

    # ① 设定集
    setting: dict[str, Any] | None = None
    spath = setting_path_of(wd)
    if not spath.exists():
        report["layers"]["setting"] = {"exists": False, "done": 0, "total": 1}
        report["errors"].append(f"没有设定集：{spath}")
    else:
        setting = load_setting(spath)
        errors, warnings = validate_setting(setting)
        report["layers"]["setting"] = {
            "exists": True,
            "done": 1 if not errors else 0,
            "total": 1,
            "characters": len(setting.get("characters") or []),
            "errors": errors,
            "warnings": warnings,
        }
        report["errors"] += [f"设定集：{e}" for e in errors]
        report["warnings"] += [f"设定集：{w}" for w in warnings]

    # ② 分卷目录
    volumes, broken = vo.load_all_volumes(wd / VOLUME_DIR)
    vol_errors: list[str] = list(broken)
    vol_warnings: list[str] = []
    for no in sorted(volumes):
        e, w = vo.validate_volume(volumes[no], setting=setting, file_vol_no=no)
        vol_errors += [f"第{no}卷：{x}" for x in e]
        vol_warnings += [f"第{no}卷：{x}" for x in w]
    ce, cw = vo.validate_chain(volumes, setting=setting) if volumes else ([], [])
    vol_errors += ce
    vol_warnings += cw
    report["layers"]["volume"] = {
        "exists": bool(volumes),
        "volumes": len(volumes),
        "covered_chapters": max(
            (end for end in ((v.get("vol") or {}).get("end_chapter") for v in volumes.values())
             if isinstance(end, int)),
            default=0,
        ),
        "errors": vol_errors,
        "warnings": vol_warnings,
    }
    report["errors"] += [f"分卷目录：{x}" for x in vol_errors]
    report["warnings"] += [f"分卷目录：{x}" for x in vol_warnings]

    # ③ 逐章创作任务指令
    briefs, brief_broken = cb.load_all_briefs(wd / BRIEF_DIR)
    brief_errors: list[str] = list(brief_broken)
    brief_warnings: list[str] = []
    for cid in sorted(briefs):
        parsed = cb.parse_chapter_id(cid)
        vol = volumes.get(parsed[0]) if parsed else None
        e, w = cb.validate_brief(briefs[cid], setting=setting, volume=vol)
        brief_errors += [f"{cid}：{x}" for x in e]
        brief_warnings += [f"{cid}：{x}" for x in w]
    be, bw = cb.validate_brief_chain(briefs) if briefs else ([], [])
    brief_errors += be
    brief_warnings += bw
    planned_chapters = sum(
        len((v.get("vol") or {}).get("chapters") or []) for v in volumes.values()
    )
    # 覆盖度也要报成阻断。只报「已存在的那些指令有没有错」会给出一个假绿灯：
    # 一卷都填了、指令一份没写，也是「没有阻断项」——而那时根本开不了工。
    if planned_chapters and len(briefs) < planned_chapters:
        brief_errors.append(
            f"逐章指令只填了 {len(briefs)}/{planned_chapters} 章，"
            f"还缺 {planned_chapters - len(briefs)} 章——没填的章写不了正文"
        )
    report["layers"]["brief"] = {
        "exists": bool(briefs),
        "done": len(briefs),
        "planned": planned_chapters,
        "errors": brief_errors,
        "warnings": brief_warnings,
    }
    report["errors"] += [f"创作任务指令：{x}" for x in brief_errors]
    report["warnings"] += [f"创作任务指令：{x}" for x in brief_warnings]

    report["ok"] = not report["errors"]
    return report


# ── 人物动向 ────────────────────────────────────────────────
#
# 长篇最隐蔽的失败不是写错，是**写着写着人不见了**：
# 一个重要配角在第 4 章之后就没再出现，作者自己没察觉，读者更不会说。
# 这里从逐章指令和卷表里把每个人物的出场章列出来，算「最近一次出场」和「连续缺席几章」。


def character_presence(work_dir: str | Path, *, brief_only: bool = False) -> dict[str, Any]:
    """谁在第几章出场、谁已经连着好几章没露面了。纯脚本，零成本。

    `brief_only=True` 只数逐章指令里的出场（真被写出来的），
    默认取「指令 ∪ 卷表」（计划 + 指令的全貌，给卡片和注入用）。
    两者的差别在**闸**上是要命的：卷表里写着主角、指令里没有，用并集就永远不响。
    

    出场来源两处：逐章指令的 `settings.characters`（写这一章之前定的）和
    卷表逐章表的 `characters`（大纲层面定的）。两处取并集——
    只认一处的话，先写大纲后补指令的阶段会漏掉一半人。
    """
    from .chapter_brief import load_all_briefs
    from .volume import load_all_volumes

    wd = Path(work_dir)
    briefs, _broken = load_all_briefs(wd / "80-brief")
    volumes, _bv = load_all_volumes(wd / "70-volume")

    # 章号 → 出场人物
    by_chapter: dict[int, set[str]] = {}
    for b in briefs.values():
        no = int(b.get("chapter_no") or 0)
        if not no:
            continue
        names = ((b.get("settings") or {}).get("characters") or [])
        by_chapter.setdefault(no, set()).update(str(n).strip() for n in names if str(n).strip())
    for vol in ([] if brief_only else volumes.values()):
        for ch in (vol.get("vol") or {}).get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            no = int(ch.get("chapter_no") or 0)
            if not no:
                continue
            by_chapter.setdefault(no, set()).update(
                str(n).strip() for n in (ch.get("characters") or []) if str(n).strip()
            )

    latest = max(by_chapter, default=0)
    charted = sorted(by_chapter)

    rows: list[dict[str, Any]] = []
    for name in sorted({n for names in by_chapter.values() for n in names}):
        appears = [no for no in charted if name in by_chapter[no]]
        if not appears:
            continue
        gaps: list[int] = []
        for prev, nxt in zip(appears, appears[1:]):
            if nxt - prev > 1:
                gaps.append(nxt - prev - 1)
        rows.append({
            "name": name,
            "chapters": appears,
            "count": len(appears),
            "first": appears[0],
            "last": appears[-1],
            # 「连续缺席」是最要紧的那个数：一个人最后一次出场之后，到现在隔了几章
            "absent": max(0, latest - appears[-1]),
            "max_gap": max(gaps) if gaps else 0,
        })

    rows.sort(key=lambda r: (-r["absent"], -r["count"]))
    return {
        "rows": rows,
        "counts": {"characters": len(rows), "chapters": len(charted), "latest_chapter_no": latest},
    }


def consistency_report(work_dir: str | Path) -> list[str]:
    """卷表里点过名、但设定集里没有的人与势力。纯脚本，零成本。

    这是回填时踩过的那类问题：卷表写着某个名字，设定集里却没有
    （手打错字，或者那个名字在设定集里属于别的小节）。
    不报的话，写到那一章模型只能自己编一个——而作者以为设定集里有。
    """
    from .chapter_brief import load_all_briefs
    from .volume import load_all_volumes

    wd = Path(work_dir)
    setting = load_work_setting(wd)
    known_chars = {str(c.get("name") or "").strip()
                   for c in (setting.get("characters") or []) if isinstance(c, dict)}
    known_factions = {str(f.get("name") or "").strip()
                      for f in (setting.get("factions") or []) if isinstance(f, dict)}
    # 别名也算认识：设定集里「李默」别名「阿默」，卷表写阿默不算错
    for c in (setting.get("characters") or []):
        if isinstance(c, dict):
            known_chars.update(str(a).strip() for a in (c.get("aliases") or []))
    known_chars.discard("")
    known_factions.discard("")

    problems: list[str] = []

    def _check(names: Any, known: set[str], what: str, where: str) -> None:
        for raw in names or []:
            name = str(raw).strip()
            if name and name not in known:
                problems.append(f"{where}写着{what}「{name}」，但设定集里没有这个人/势力")

    volumes, _bv = load_all_volumes(wd / "70-volume")
    for vol in volumes.values():
        v = vol.get("vol") or {}
        where = f"第 {v.get('vol')} 卷逐章表"
        for ch in v.get("chapters") or []:
            if isinstance(ch, dict):
                _check(ch.get("characters"), known_chars, "出场人物", f"{where}第 {ch.get('chapter_no')} 章")

    briefs, _br = load_all_briefs(wd / "80-brief")
    for b in briefs.values():
        st = b.get("settings") or {}
        where = f"逐章指令 {b.get('chapter_id')}"
        _check(st.get("characters"), known_chars, "出场人物", where)
        _check(st.get("factions"), known_factions, "涉及势力", where)

    return problems


# ── 全书梗概 ────────────────────────────────────────────────
#
# 刻意**不新增字段**。梗概要讲的东西，三层真源里全都有了：
# 一句话前提、主角动机、各卷核心冲突与目标、卷内部分、逐章概要。
# 再存一份 300-500 字的梗概，就会变成第三个真源——改了一处另两处过期，
# 而「指令和正文对不上」这种问题我们已经吃过一次（技法审稿抓出来的）。
#
# 所以这里是**渲染**，不是存储：永远跟着真源走，不可能过期。


def render_synopsis(work_dir: str | Path) -> str:
    """把三层真源渲染成一份可读的全书梗概。纯脚本，零成本。"""
    from .chapter_brief import chapter_id_of
    from .volume import load_all_volumes

    wd = Path(work_dir)
    setting = load_work_setting(wd)
    volumes, _bv = load_all_volumes(wd / "70-volume")
    draft_dir = wd / "90-draft"
    drafted = {p.stem for p in draft_dir.glob("*.md")} if draft_dir.exists() else set()

    out: list[str] = []
    name = setting.get("work") or "（未命名）"
    out.append(f"# {name}")

    meta: list[str] = []
    if setting.get("genre"):
        meta.append(f"题材：{setting['genre']}")
    target = setting.get("target") or {}
    if target.get("chapters"):
        span = target.get("chars_per_chapter") or []
        each = f"，每章 {span[0]}–{span[1]} 字" if len(span) == 2 else ""
        meta.append(f"篇幅：约 {target['chapters']} 章{each}")
    if meta:
        out.append("　".join(meta))

    if setting.get("logline"):
        out.append(f"\n**一句话前提**：{setting['logline']}")
    lead = [c for c in (setting.get("characters") or [])
            if isinstance(c, dict) and str(c.get("role") or "") == "主角"]
    who = lead[0] if lead else {}
    if who.get("name") or setting.get("core_motive"):
        line = f"**主角**：{who.get('name') or '（未定）'}"
        if setting.get("core_motive"):
            line += f"　核心动机：{setting['core_motive']}"
        out.append(line)
    if setting.get("power", {}).get("system") or setting.get("power", {}).get("tiers"):
        p = setting["power"]
        tiers = " → ".join(str(t) for t in p.get("tiers") or [])
        out.append(f"**力量体系**：{p.get('system') or '（未名）'}" + (f"（{tiers}）" if tiers else ""))
    if setting.get("factions"):
        names = "、".join(str(f.get("name")) for f in setting["factions"] if isinstance(f, dict))
        if names:
            out.append(f"**势力**：{names}")

    total_chapters = 0
    written = 0
    for no in sorted(volumes):
        v = (volumes[no].get("vol") or {})
        start, end = v.get("start_chapter"), v.get("end_chapter")
        head = f"\n## 第 {v.get('vol') or no} 卷　{v.get('title') or '（未名）'}"
        if start and end:
            head += f"（第 {start}–{end} 章）"
        out.append(head)
        if v.get("era"):
            out.append(f"时间跨度：{v['era']}")
        if v.get("core_conflict"):
            out.append(f"核心冲突：{v['core_conflict']}")
        if v.get("goal"):
            out.append(f"卷目标：{v['goal']}")
        for part in v.get("parts") or []:
            if isinstance(part, dict) and part.get("title"):
                span = ""
                if part.get("start_chapter") and part.get("end_chapter"):
                    span = f"（第 {part['start_chapter']}–{part['end_chapter']} 章）"
                out.append(f"- **{part['title']}**{span}"
                           + (f"：{part['gist']}" if part.get("gist") else ""))
        chapters = v.get("chapters") or []
        if chapters:
            out.append("")
            for ch in chapters:
                if not isinstance(ch, dict):
                    continue
                total_chapters += 1
                cno = int(ch.get("chapter_no") or 0)
                cid = chapter_id_of(int(v.get("vol") or no), cno) if cno else ""
                mark = "✅" if cid in drafted else "　"
                if cid in drafted:
                    written += 1
                line = f"{mark} 第 {cno} 章　{ch.get('title') or '（未名）'}"
                if ch.get("gist"):
                    line += f" —— {ch['gist']}"
                out.append(line)
        if v.get("closing"):
            out.append(f"\n> 卷末：{v['closing']}")

    if total_chapters:
        out.insert(2, f"\n进度：已写正文 {written} / {total_chapters} 章\n")
    out.append("\n---\n（这份梗概是从设定集、分卷目录、逐章指令**渲染**出来的，不是单独存的。"
               "改真源它就跟着变，所以不会过期。）")
    return "\n".join(out)


# ── 写前必读 ────────────────────────────────────────────────
#
# 作者的原始问题：「全书梗概是后定的，如何引导全书发展？每一卷会不会无法控制、
# 中间主角都换人了？」
#
# 答案不是再加一份梗概——梗概是视图，视图约束不了任何东西。
# 真正的答案是把已有的真源**每次动笔前渲染成输入**。这一步做之前，
# 起草卷表/指令时模型只看得到那一层自己的文档：看不到设定集、看不到前面各卷、
# 看不到上一章正文。隔着几卷凭空造，主角当然会飘。
#
# 采用的做法参考了外部技能包（番茄小说写作）的「写前必读」流程：
#   作品基因（防跑偏锚点）+ 前情 + 角色当前状态 + 待回收伏笔 + 上一章末段
# 但**不抄它的记忆文件**——那会变成第二套真源。这里全部是渲染，没有存储。

ANCHOR_HEADER = "【全书锚点】这些是这本书不能变的东西。动笔前先钉住，写的过程中一个字都不要偏离。"
WRITE_BRIEF_ORDER = ("anchor", "recap", "part", "cast", "foreshadow", "hook", "prev")


def render_story_anchor(work_dir: str | Path) -> str:
    """防跑偏锚点：一句话前提、终极钩子、主角与动机、体系与势力。纯渲染。"""
    wd = Path(work_dir)
    setting = load_work_setting(wd)
    lines = [ANCHOR_HEADER]
    if setting.get("work"):
        lines.append(f"作品：{setting['work']}"
                     + (f"（{setting['genre']}）" if setting.get("genre") else ""))
    if setting.get("logline"):
        lines.append(f"一句话前提：{setting['logline']}")
    if setting.get("ultimate_hook"):
        lines.append(f"终极钩子（读者追到最后的那个答案）：{setting['ultimate_hook']}")
    lead = [c for c in (setting.get("characters") or [])
            if isinstance(c, dict) and str(c.get("role") or "").strip() == "主角"]
    if lead:
        who = lead[0]
        line = f"主角：{who.get('name')}"
        if who.get("identity"):
            line += f"（{who['identity']}）"
        if setting.get("core_motive"):
            line += f"，核心动机：{setting['core_motive']}"
        lines.append(line)
        if who.get("arc"):
            lines.append(f"主角弧光（全书要走完的路）：{who['arc']}")
    elif setting.get("core_motive"):
        lines.append(f"主角核心动机：{setting['core_motive']}")
    power = setting.get("power") or {}
    if power.get("system") or power.get("tiers"):
        tiers = " → ".join(str(t) for t in power.get("tiers") or [])
        lines.append(f"力量体系：{power.get('system') or '（未名）'}" + (f"（{tiers}）" if tiers else ""))
    factions = [str(f.get("name")) for f in (setting.get("factions") or []) if isinstance(f, dict)]
    if factions:
        lines.append("势力：" + "、".join(factions))
    style = setting.get("style") or {}
    if style.get("perspective") or style.get("tone"):
        lines.append("文风：" + "　".join(
            x for x in (f"视角 {style.get('perspective')}" if style.get("perspective") else "",
                        f"基调 {style.get('tone')}" if style.get("tone") else "") if x))
    # 只有书名时**不输出**：一个什么都没说的「锚点」只会占掉模型的注意力，
    # 而作者会以为"锚点已经有了"。锚点必须有实质内容才有意义。
    substantive = bool(setting.get("logline") or setting.get("ultimate_hook")
                       or setting.get("core_motive") or lead)
    if len(lines) == 1 or not substantive:
        return ""
    return "\n".join(lines)


def render_recap(work_dir: str | Path, *, upto_chapter_no: int = 0, max_chars: int = 2500) -> str:
    """前情：前面各卷讲了什么。只到 `upto_chapter_no` 为止——**不剧透后面**。

    写第 3 卷时把第 5 卷的卷末总结也喂进去，等于提前把结局告诉模型，
    它会忍不住往那儿赶。
    """
    from .chapter_brief import chapter_id_of
    from .volume import load_all_volumes

    wd = Path(work_dir)
    volumes, _broken = load_all_volumes(wd / "70-volume")
    parts: list[str] = []
    used = 0
    for no in sorted(volumes):
        v = volumes[no].get("vol") or {}
        vol_no = int(v.get("vol") or no)
        head = f"第 {vol_no} 卷 {v.get('title') or '（未名）'}"
        bits: list[str] = []
        if v.get("core_conflict"):
            bits.append(f"核心冲突：{v['core_conflict']}")
        if v.get("goal"):
            bits.append(f"卷目标：{v['goal']}")
        chapters: list[str] = []
        for ch in v.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            cno = int(ch.get("chapter_no") or 0)
            if upto_chapter_no and cno > upto_chapter_no:
                break
            if cno:
                chapters.append(f"{cno} {ch.get('title') or '（未名）'}"
                                + (f"（{ch['gist']}）" if ch.get("gist") else ""))
        block = f"· {head}"
        if bits:
            block += "｜" + "｜".join(bits)
        if chapters:
            block += "\n  逐章：" + "；".join(chapters)
        if v.get("closing") and (not upto_chapter_no or (v.get("end_chapter") or 0) <= upto_chapter_no):
            block += f"\n  卷末：{v['closing']}"
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block) + 1
    if not parts:
        return ""
    return "【前情】这本书到目前讲过的东西（按顺序，不要重复交代）\n" + "\n".join(parts)


def render_cast_state(work_dir: str | Path, *, absent_alarm: int = 3) -> str:
    """角色当前状态：谁一直在、谁已经连着几章没露面了。

    「主角会不会写着写着换人」这件事，机械上唯一的抓手就是这个数。
    """
    cast = character_presence(work_dir)
    rows = cast.get("rows") or []
    if not rows:
        return ""
    latest = (cast.get("counts") or {}).get("latest_chapter_no") or 0
    lines = [f"【角色动态】已经写到第 {latest} 章"]
    for r in rows[:12]:
        line = f"· {r['name']}：出场 {r['count']} 章，第 {r['first']}–{r['last']} 章"
        if r["absent"]:
            line += f"，最近一次出场后已隔 {r['absent']} 章"
            if r["absent"] >= absent_alarm:
                line += "（⚠️ 缺席偏久，要么让他回来，要么交代他去哪了）"
        lines.append(line)
    return "\n".join(lines)


def render_open_foreshadows(work_dir: str | Path, *, limit: int = 12) -> str:
    """待回收伏笔。写作时最该记得的就是它们。"""
    from .chapter_brief import foreshadow_ledger

    led = foreshadow_ledger(Path(work_dir) / "80-brief")
    opened = led.get("open") or []
    if not opened:
        return ""
    lines = [f"【待回收伏笔】{len(opened)} 条还挂着"]
    for r in opened[:limit]:
        line = f"· {r['id']}：{r['desc'] or '（没写说明）'}（埋于 {r['planted_at']}，挂了 {r['hanging_chapters']} 章）"
        lines.append(line)
    if len(opened) > limit:
        lines.append(f"· …还有 {len(opened) - limit} 条")
    return "\n".join(lines)


def render_prev_tail(work_dir: str | Path, *, before_chapter_no: int = 0, chars: int = 900) -> str:
    """上一章正文的末段——衔接最实在的依据，比任何摘要都准。"""
    from .chapter_brief import chapter_id_of, load_all_briefs

    if not before_chapter_no:
        return ""
    wd = Path(work_dir)
    briefs, _b = load_all_briefs(wd / "80-brief")
    prev: tuple[int, int, str] | None = None
    for cid, b in briefs.items():
        cno = int(b.get("chapter_no") or 0)
        if cno and cno < before_chapter_no:
            key = (cno, int(b.get("vol") or 1))
            if prev is None or key > (prev[0], prev[1]):
                prev = (cno, int(b.get("vol") or 1), cid)
    if prev is None:
        return ""
    path = wd / "90-draft" / f"{prev[2]}.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    return (f"【上一章末段】（第 {prev[0]} 章 {chapter_id_of(prev[1], prev[0])}，"
            f"只给你末尾 {min(chars, len(text))} 字，保证接得上）\n…{text[-chars:]}")


def render_write_brief(
    work_dir: str | Path, *, chapter_no: int = 0, prev_chapter_no: int = 0
) -> str:
    """把「写前必读」组装成给模型的一段输入。

    起草卷表、起草逐章指令、写正文——三处用的是**同一段**，
    这样它读到的东西不会因为从哪个入口进来而不同。
    """
    wd = Path(work_dir)
    blocks = {
        "anchor": render_story_anchor(wd),
        "recap": render_recap(wd, upto_chapter_no=prev_chapter_no or 0),
        "cast": render_cast_state(wd),
        "foreshadow": render_open_foreshadows(wd),
        "part": render_part_task(wd, chapter_no=chapter_no),
        "hook": render_hook_rotation(wd, chapter_no=chapter_no),
        "prev": render_prev_tail(wd, before_chapter_no=chapter_no),
    }
    kept = [blocks[k] for k in WRITE_BRIEF_ORDER if blocks[k]]
    if not kept:
        return ""
    return "\n\n".join(kept) + (
        "\n\n（以上是动笔前必须读完的东西。写到与它冲突的地方，先停下来——"
        "要么改这一段，要么回头改真源，不要带着矛盾往下写。）"
    )


# ── 全书级硬闸 ──────────────────────────────────────────────
#
# 作者的原话：「每一卷的内容会不会无法控制，中间主角都换人了」。
# 上面那些「写前必读」是**引导**（让模型看得见），这里两道是**拦住**。
# 引导靠提示词，拦住必须靠代码——提示词说一百遍"别忘了主角"，也不如一道闸。


def story_gates(work_dir: str | Path, *, absent_limit: int = 3) -> tuple[list[str], list[str]]:
    """返回 (阻断, 警告)。纯脚本，零成本。

    两道闸：
    1. **主角连续缺席**：一本书的主角连着三章不露面，它已经不是那本书了。
       但群像戏（设定里标了多个主角）不能这么卡——所以两个以上主角时降为警告。
    2. **卷间接不上**：上一卷没有卷末总结、下一卷也没有核心冲突，两卷之间就是空的。
    """
    from .volume import load_all_volumes

    wd = Path(work_dir)
    setting = load_work_setting(wd)
    errors: list[str] = []
    warnings: list[str] = []

    leads = [c for c in (setting.get("characters") or [])
             if isinstance(c, dict) and str(c.get("role") or "").strip() == "主角"]
    # 闸只看**指令**里的出场：真正被写出来的是指令，卷表是计划。
    # 用并集的话，卷表里写着主角、指令里没有，闸就永远不响——实测栽过。
    cast = character_presence(wd, brief_only=True)
    latest = (cast.get("counts") or {}).get("latest_chapter_no") or 0
    if latest and leads:
        names = [str(c.get("name") or "") for c in leads]
        absent = []
        for name in names:
            row = next((r for r in (cast.get("rows") or []) if r["name"] == name), None)
            absent.append((name, row["absent"] if row else latest, row["last"] if row else 0))
        worst = max(absent, key=lambda x: x[1])
        if worst[1] >= absent_limit:
            msg = (f"主角「{worst[0]}」已经连续 {worst[1]} 章没出场"
                   + (f"（最近一次是第 {worst[2]} 章）" if worst[2] else "（一次都没出场）")
                   + f"，而已经规划到第 {latest} 章。这本书的主角是他——"
                   "要么把他写回来，要么交代他去哪了。")
            if len(leads) > 1:
                # 群像戏：多个主角轮着来是正常的，降为提示
                warnings.append(msg + "（设定里标了多个主角，按群像处理，不阻拦）")
            else:
                errors.append(msg)

    volumes, _broken = load_all_volumes(wd / "70-volume")
    ordered = sorted(volumes.values(),
                     key=lambda v: int((v.get("vol") or {}).get("vol") or 0))
    for prev, nxt in zip(ordered, ordered[1:]):
        pv, nv = prev.get("vol") or {}, nxt.get("vol") or {}
        if not str(pv.get("closing") or "").strip() and not str(nv.get("core_conflict") or "").strip():
            errors.append(
                f"第 {pv.get('vol')} 卷没有卷末总结，第 {nv.get('vol')} 卷也没有核心冲突——"
                "这两卷之间是断的，下一卷不知道从哪儿接起。"
            )
    return errors, warnings


def render_part_task(work_dir: str | Path, *, chapter_no: int = 0) -> str:
    """本章属于哪一段、这一段必须完成什么、末尾要落在哪。

    从外部技能包的「节奏蓝图」学的：光有一句「这一段讲什么」，作者写到第 5 章时
    没人告诉他任务达没达成。把「必须完成的功能」与「末尾落点要求」写进输入，
    每一卷才真的可控。
    """
    from .volume import load_all_volumes

    if not chapter_no:
        return ""
    wd = Path(work_dir)
    volumes, _b = load_all_volumes(wd / "70-volume")
    for no in sorted(volumes):
        v = volumes[no].get("vol") or {}
        for part in v.get("parts") or []:
            if not isinstance(part, dict):
                continue
            lo = int(part.get("start_chapter") or 0)
            hi = int(part.get("end_chapter") or 0)
            if not lo or not hi or not (lo <= chapter_no <= hi):
                continue
            title = part.get("title") or "（未名）"
            lines = [f"【本段任务】第 {v.get('vol')} 卷「{title}」（第 {lo}–{hi} 章），"
                     f"本章是其中第 {chapter_no - lo + 1} 章"]
            if part.get("gist"):
                lines.append(f"这一段讲什么：{part['gist']}")
            must = [str(x).strip() for x in (part.get("must_complete") or []) if str(x).strip()]
            if must:
                lines.append("这一段必须完成的功能：")
                lines += [f"  {i}. {x}" for i, x in enumerate(must, 1)]
            else:
                lines.append("（这一段没写「必须完成的功能」——写到这儿没人知道任务达成了没有）")
            if part.get("ending_demand"):
                lines.append(f"本段末尾的落点要求：{part['ending_demand']}")
            return "\n".join(lines)
    return ""


def render_hook_rotation(work_dir: str | Path, *, chapter_no: int = 0, look_back: int = 4) -> str:
    """已用过的章末钩子，以及本章该避开哪一种。

    防的是**写法重复**而不是情节重复：读者说不清哪里腻，只觉得「怎么又是这套」。
    """
    from .chapter_brief import HOOK_TYPES, load_all_briefs

    wd = Path(work_dir)
    briefs, _b = load_all_briefs(wd / "80-brief")
    rows = sorted((b for b in briefs.values() if int(b.get("chapter_no") or 0)),
                  key=lambda b: int(b.get("chapter_no") or 0))
    used = [(int(b.get("chapter_no") or 0), str(b.get("hook_type") or "").strip())
            for b in rows if str(b.get("hook_type") or "").strip()]
    if not used and not chapter_no:
        return ""
    lines = ["【章末钩子】"]
    if used:
        recent = used[-look_back:]
        lines.append("已经用过：" + "；".join(f"第 {n} 章 {h}" for n, h in recent))
        last = used[-1][1]
        others = [h for h in HOOK_TYPES if h != last]
        lines.append(f"本章**不要**再用「{last}」（连着两章同一种手法会腻），"
                     f"从这些里挑：{'、'.join(others[:6])}…")
    else:
        lines.append("还没有记录过任何钩子。十种可选：" + "、".join(HOOK_TYPES))
    lines.append("写完把这一章用的钩子类型填进「章末钩子类型」——下一章要靠它轮换。")
    return "\n".join(lines)
