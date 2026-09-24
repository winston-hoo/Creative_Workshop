"""实体统计：人物、势力、能力、地点、人物关系。

## 与其他模块的关系

标注解决的是**节奏与结构**（钩子、情绪、冲突、伏笔），这里解决**实体层**：
谁出场了、属于哪个势力、会什么能力、和谁是什么关系。

原料是**正文**而不是梗概——梗概一句话 60 字，装不下人物关系的细节。

## 为什么分块

和大纲一样，一次性塞 545 章进去细节会被忽略，而且一处失败整本重跑。
所以每 N 章一块，逐块抽取，块间独立、可断点续跑，最后归并。

## 三条纪律（与全项目一致）

1. **只抽原文里出现的**。模型自己脑补的设定一律不要——宁可少，不能编。
2. **首次出现章号要给**，否则没法回答「这个人物什么时候登场的」。
3. **失败的块显式标出来**，不伪装成「没有实体」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .batch import ChapterTask
from .llm import ApiError, ChatResult, OpenAICompatProvider, build_thinking_extra, extract_json_object
from .secrets import redact

ENTITIES_SCHEMA_VERSION = "entities-v1"
DEFAULT_BLOCK_SIZE = 50

ENTITY_PROMPT = """你是长篇小说的设定整理员。下面是连续若干章的正文，请整理出其中出现的设定实体。

严格按 JSON 输出，不要任何前后说明，不要用代码围栏：

{
  "characters": [
    {"name": "人物名", "role": "主角|重要配角|配角|路人", "identity": "身份/职业/处境（不超过 30 字）",
     "abilities": ["这个人会的能力名"], "factions": ["这个人所属的势力名"],
     "first_chapter": 首次出现的章号(int), "relations": [{"to": "另一个人物名", "type": "关系类型（亲属/同学/师徒/敌对/上下级等，不超过 8 字）", "note": "关系说明（不超过 20 字）"}]}
  ],
  "factions": [{"name": "势力名", "stance": "立场/性质（不超过 20 字）", "members": ["成员名"], "first_chapter": 首次出现的章号(int)}],
  "abilities": [{"name": "能力名", "holder": "会这个能力的人物名", "effect": "效果（不超过 30 字）", "first_chapter": 首次出现的章号(int)}],
  "locations": [{"name": "地点名", "note": "是什么地方（不超过 20 字）", "first_chapter": 首次出现的章号(int)}]
}

铁律：
- **只整理原文里明确出现的东西**。名字、身份、能力、关系，原文没写就是没有，不要推断、不要补全、不要起名
- 原文没给名字的人物（如「老爹」「屈老二」这类称呼）就用原文里的称呼，不要给正式名字
- relations 只记**双向都成立的关系**（甲是乙的父亲、甲与乙是同学），单方面的认知不算
- 同一个人物在多章出现，只记一条，first_chapter 取最早
- 本章没有的内容就给空数组，不要为了填满而编造"""


@dataclass
class EntitiesOptions:
    block_size: int = DEFAULT_BLOCK_SIZE
    temperature: float = 0.2
    thinking: str = "disabled"
    max_tokens: int = 4000
    max_attempts: int = 3
    timeout_sec: float = 120.0


@dataclass
class EntitiesPlan:
    work: str
    provider_id: str
    model_id: str
    total_chapters: int
    blocks: int
    block_size: int
    body_chars: int
    est_input_tokens: int
    est_output_tokens: int
    est_cost_cny: float | None
    price_note: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work": self.work,
            "provider": self.provider_id,
            "model": self.model_id,
            "total_chapters": self.total_chapters,
            "blocks": self.blocks,
            "block_size": self.block_size,
            "body_chars": self.body_chars,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "est_cost_cny": self.est_cost_cny,
            "price_note": self.price_note,
            "warnings": self.warnings,
        }


def build_entities_plan(
    *,
    work: str,
    provider_id: str,
    model_id: str,
    tasks: list[ChapterTask],
    block_size: int = DEFAULT_BLOCK_SIZE,
    price_input_per_mtok: float | None = None,
    price_output_per_mtok: float | None = None,
    bucket: str | None = None,
) -> EntitiesPlan:
    """算出「要跑几块、大概花多少」。**不发起任何调用。**"""
    size = max(1, block_size)
    blocks = max(1, -(-len(tasks) // size))
    body_chars = sum(t.body_chars_for_prompt for t in tasks)

    est_in = int(body_chars * 0.675) + int(len(ENTITY_PROMPT) * 0.5) * blocks
    est_out = 2000 * blocks

    cost = None
    note = "未填写单价，费用无法计算。"
    if price_input_per_mtok is not None and price_output_per_mtok is not None:
        cost = round(
            est_in / 1_000_000 * price_input_per_mtok + est_out / 1_000_000 * price_output_per_mtok, 4
        )
        bucket_text = {"peak": "高峰", "offpeak": "空闲"}.get(bucket or "", "不分时段")
        note = f"按{bucket_text}单价估算：输入 {price_input_per_mtok} 元/百万、输出 {price_output_per_mtok} 元/百万。"

    return EntitiesPlan(
        work=work,
        provider_id=provider_id,
        model_id=model_id,
        total_chapters=len(tasks),
        blocks=blocks,
        block_size=size,
        body_chars=body_chars,
        est_input_tokens=est_in,
        est_output_tokens=est_out,
        est_cost_cny=cost,
        price_note=note,
    )


def _block_label(task_range: list[ChapterTask]) -> str:
    if not task_range:
        return ""
    first, last = task_range[0], task_range[-1]
    a, b = first.chapter_no, last.chapter_no
    if a is not None and b is not None and b > a:
        return f"第 {a}-{b} 章"
    if a is not None and b is not None and a == b and len(task_range) == 1:
        return f"第 {a} 章"
    return f"{first.chapter_id} → {last.chapter_id}"


def _block_text(task_range: list[ChapterTask]) -> str:
    parts: list[str] = []
    for task in task_range:
        try:
            raw = task.path.read_text(encoding="utf-8")
        except OSError:
            continue
        body = raw.split("---", 2)[2].strip() if raw.startswith("---") else raw
        label = f"第{task.chapter_no}章" if task.chapter_no is not None else task.chapter_id
        title = f" {task.title}" if task.title else ""
        parts.append(f"【{label}{title}】\n{body}")
    return "\n\n".join(parts)


def _call(
    client: OpenAICompatProvider,
    model_id: str,
    user: str,
    opts: EntitiesOptions,
) -> tuple[dict[str, Any] | None, str, dict[str, int]]:
    extra = build_thinking_extra(opts.thinking, None)
    last_error = ""
    usage_total: dict[str, int] = {}
    for _attempt in range(1, max(1, opts.max_attempts) + 1):
        try:
            result: ChatResult = client.chat(
                model_id,
                [
                    {"role": "system", "content": "你是长篇小说的设定整理员。只输出 JSON。"},
                    {"role": "user", "content": user},
                ],
                max_tokens=opts.max_tokens,
                temperature=opts.temperature,
                response_format={"type": "json_object"},
                extra=extra,
            )
        except ApiError as exc:
            last_error = f"{exc.kind.value}：{exc.safe_body(client.secrets)}"
            continue
        if result.usage:
            for key, value in result.usage.to_dict().items():
                if isinstance(value, int):
                    usage_total[key] = usage_total.get(key, 0) + value
        payload = extract_json_object(result.text)
        if payload is None:
            last_error = f"返回内容不是 JSON：{result.text[:120]}"
            continue
        return payload, "", usage_total
    return None, last_error, usage_total


def _empty_block(label: str, error: str, chapters: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "range": label,
        "chapters": chapters,
        "error": error,
        "characters": [],
        "factions": [],
        "abilities": [],
        "locations": [],
    }


def generate_entities(
    *,
    plan: EntitiesPlan,
    tasks: list[ChapterTask],
    client: OpenAICompatProvider,
    opts: EntitiesOptions | None = None,
    secrets: list[str] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """逐块抽取实体，最后归并。失败的块显式留痕，不伪装成「没有」。"""
    opts = opts or EntitiesOptions(block_size=plan.block_size)
    size = max(1, plan.block_size)
    blocks = [tasks[i : i + size] for i in range(0, len(tasks), size)]

    result: dict[str, Any] = {
        "schema_version": ENTITIES_SCHEMA_VERSION,
        "work": plan.work,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "model": plan.model_id,
        "blocks": [],
        "errors": [],
        "usage": {},
        "merged": {},
    }

    done_blocks: dict[str, dict[str, Any]] = {}
    if state_path and Path(state_path).exists():
        try:
            cached = json.loads(Path(state_path).read_text(encoding="utf-8-sig"))
            for item in cached.get("blocks") or []:
                if isinstance(item, dict) and item.get("range") and not item.get("error"):
                    done_blocks[str(item["range"])] = item
        except (ValueError, OSError):
            done_blocks = {}

    total = len(blocks)
    for index, block in enumerate(blocks, start=1):
        label = _block_label(block)
        if on_progress:
            on_progress(index, total, label)

        chapter_refs = [
            {"id": t.chapter_id, "chapter_no": t.chapter_no, "title": t.title} for t in block
        ]

        if label in done_blocks:
            result["blocks"].append(done_blocks[label])
            continue

        text = _block_text(block)
        if not text.strip():
            result["blocks"].append(_empty_block(label, "这一块没有可读的正文", chapter_refs))
            continue

        payload, error, usage = _call(
            client,
            plan.model_id,
            f"{ENTITY_PROMPT}\n\n以下是第 {index}/{total} 块（{label}）：\n\n{text}",
            opts,
        )
        for key, value in usage.items():
            result["usage"][key] = result["usage"].get(key, 0) + value

        if payload is None:
            result["errors"].append(f"{label}：{error or '返回为空'}")
            result["blocks"].append(_empty_block(label, error or "返回为空", chapter_refs))
        else:
            result["blocks"].append(
                {
                    "range": label,
                    "chapters": chapter_refs,
                    "error": "",
                    "characters": payload.get("characters") or [],
                    "factions": payload.get("factions") or [],
                    "abilities": payload.get("abilities") or [],
                    "locations": payload.get("locations") or [],
                }
            )

        _save_state(state_path, result, secrets)

    result["merged"] = _merge(result["blocks"])
    _save_state(state_path, result, secrets)
    return result


def _save_state(state_path: Path | None, result: dict[str, Any], secrets: list[str] | None) -> None:
    if not state_path:
        return
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        redact(json.dumps(result, ensure_ascii=False, indent=2), secrets), encoding="utf-8"
    )


# ── 归并 ──────────────────────────────────────────────────
#
# 名字归并是这一步的全部难点：同一人物在不同块里写法可能略异。
# 规则归并只做到「完全一致」与「互相包含」两级，剩下的留给人工核对——
# 假装合并成功比不合并更糟。


def _norm(name: Any) -> str:
    return str(name or "").strip()


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _merge(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    characters: dict[str, dict[str, Any]] = {}
    factions: dict[str, dict[str, Any]] = {}
    abilities: dict[str, dict[str, Any]] = {}
    locations: dict[str, dict[str, Any]] = {}
    relations: list[dict[str, Any]] = []

    def key_of(name: str, table: dict) -> str:
        low = name.lower()
        if low in table:
            return low
        for existing in table:
            if existing and (existing in low or low in existing):
                return existing
        return low

    def merge_one(table: dict, name: str, payload: dict, chapter: int | None, fields: tuple) -> None:
        name = _norm(name)
        if not name:
            return
        key = key_of(name, table)
        if key not in table:
            entry = {"name": name, "first_chapter": chapter}
            for f in fields:
                entry[f] = payload.get(f)
            table[key] = entry
            return
        entry = table[key]
        entry["first_chapter"] = _first_int(entry.get("first_chapter"), chapter)
        for f in fields:
            new = _norm(payload.get(f))
            old = _norm(entry.get(f))
            if new and len(new) >= len(old):
                entry[f] = new

    for block in blocks:
        if block.get("error"):
            continue
        for ch in block.get("characters") or []:
            if not isinstance(ch, dict):
                continue
            name = _norm(ch.get("name"))
            if not name:
                continue
            merge_one(characters, name, ch, _first_int(ch.get("first_chapter")), ("role", "identity"))
            for ability in ch.get("abilities") or []:
                ability = _norm(ability)
                if not ability:
                    continue
                merge_one(abilities, ability, {"holder": name}, None, ("effect",))
                entry = abilities[key_of(ability, abilities)]
                holders = entry.setdefault("holders", [])
                if name not in holders:
                    holders.append(name)
                entry["holder"] = "、".join(holders)
            for faction in ch.get("factions") or []:
                faction = _norm(faction)
                if not faction:
                    continue
                merge_one(factions, faction, {}, None, ("stance",))
                entry = factions[key_of(faction, factions)]
                members = entry.setdefault("members", [])
                if name not in members:
                    members.append(name)
            for rel in ch.get("relations") or []:
                if not isinstance(rel, dict):
                    continue
                other = _norm(rel.get("to"))
                if not other or other == name:
                    continue
                row = {
                    "from": name,
                    "to": other,
                    "type": _norm(rel.get("type")),
                    "note": _norm(rel.get("note")),
                }
                if row not in relations:
                    relations.append(row)
        for fa in block.get("factions") or []:
            if not isinstance(fa, dict):
                continue
            merge_one(factions, fa.get("name"), fa, _first_int(fa.get("first_chapter")), ("stance",))
        for ab in block.get("abilities") or []:
            if not isinstance(ab, dict):
                continue
            merge_one(abilities, ab.get("name"), ab, _first_int(ab.get("first_chapter")), ("effect",))
        for lo in block.get("locations") or []:
            if not isinstance(lo, dict):
                continue
            merge_one(locations, lo.get("name"), lo, _first_int(lo.get("first_chapter")), ("note",))

    def sort_key(entry: dict[str, Any]) -> tuple:
        return (entry.get("first_chapter") or 10**9, entry["name"])

    chars = sorted(characters.values(), key=sort_key)
    facs = sorted(factions.values(), key=sort_key)
    abis = sorted(abilities.values(), key=sort_key)
    locs = sorted(locations.values(), key=sort_key)

    return {
        "characters": chars,
        "factions": facs,
        "abilities": abis,
        "locations": locs,
        "relations": relations,
        "counts": {
            "characters": len(chars),
            "factions": len(facs),
            "abilities": len(abis),
            "locations": len(locs),
            "relations": len(relations),
        },
    }


# ── 渲染 ──────────────────────────────────────────────────


def render_markdown(result: dict[str, Any]) -> str:
    merged = result.get("merged") or {}
    counts = merged.get("counts") or {}
    failed = [b for b in result.get("blocks") or [] if b.get("error")]

    lines = [
        f"# 设定实体 · {result.get('work')}",
        "",
        f"生成于 {result.get('generated_at', '')}",
        "",
        f"人物 {counts.get('characters', 0)}　势力 {counts.get('factions', 0)}　"
        f"能力 {counts.get('abilities', 0)}　地点 {counts.get('locations', 0)}　关系 {counts.get('relations', 0)}",
        "",
    ]
    if failed:
        lines.append(f"> ⚠️ 有 {len(failed)} 块抽取失败，这些段里的实体**不在下面的表里**。")
        lines.append("")

    lines.append("## 人物")
    chars = merged.get("characters") or []
    if chars:
        lines.append("| 人物 | 定位 | 身份 | 首次出现 |")
        lines.append("|---|---|---|---|")
        for c in chars:
            lines.append(
                f"| {c['name']} | {c.get('role') or '—'} | {c.get('identity') or '—'} | "
                f"第{c.get('first_chapter') or '—'}章 |"
            )
    else:
        lines.append("无数据。")
    lines.append("")

    relations = merged.get("relations") or []
    lines.append("## 人物关系")
    if relations:
        lines.append("| 人物 | 关系 | 对象 | 说明 |")
        lines.append("|---|---|---|---|")
        for r in relations:
            lines.append(f"| {r['from']} | {r['type'] or '—'} | {r['to']} | {r['note'] or '—'} |")
    else:
        lines.append("无数据。")
    lines.append("")

    for title, key, cols in (
        ("势力", "factions", [("name", "势力"), ("stance", "立场"), ("first_chapter", "首次出现")]),
        ("能力", "abilities", [("name", "能力"), ("holder", "会的人"), ("effect", "效果"), ("first_chapter", "首次出现")]),
        ("地点", "locations", [("name", "地点"), ("note", "说明"), ("first_chapter", "首次出现")]),
    ):
        items = merged.get(key) or []
        lines.append(f"## {title}")
        if items:
            lines.append("| " + " | ".join(c[1] for c in cols) + " |")
            lines.append("|" + "---|" * len(cols))
            for item in items:
                lines.append("| " + " | ".join(str(item.get(c[0]) or "—") for c in cols) + " |")
        else:
            lines.append("无数据。")
        lines.append("")

    if failed:
        lines.append("## 未完成的块")
        for b in failed:
            lines.append(f"- {b['range']}：{b['error']}")
        lines.append("")

    lines.append("> 实体由模型从正文抽取，名字与关系可能有遗漏或归并错误，重要设定请以原文为准。")
    return "\n".join(lines)


def save_entities(work_dir: Path, result: dict[str, Any], *, stamp: str | None = None) -> dict[str, Path]:
    out = Path(work_dir) / "50-entities"
    out.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%dT%H%M%S")
    json_path = out / f"entities-{stamp}.json"
    md_path = out / f"entities-{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    (out / "latest.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (out / "latest.md").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"json": json_path, "markdown": md_path, "latest_json": out / "latest.json", "latest_md": out / "latest.md"}
