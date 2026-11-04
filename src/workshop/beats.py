"""写前细纲（beat sheet）。

## 为什么要多这一次廉价调用

直接让模型写 3000 字正文，它要同时决定「这一章发生什么」和「每件事怎么落笔」。
后者它擅长，前者经常糊在一起：核心情节漏一段、顺序反了、写到一半才想起要出场的人。
花一次短调用先把 5–8 个节拍定下来，正文就只需照着走，跑偏的概率明显低。

## 不是第二套真源

细纲由本章指令**派生**，不是作者要维护的新文档。它落盘只为两件事：
  · 复用：同一份指令重写时不必再花一次调用；
  · 可见：作者能看见模型打算怎么拆这一章，不满意可以先改指令。

指令一改，`brief_sha` 对不上，旧细纲自动作废，不会拿旧计划写新章。

## 失败怎么办

解析失败、模型返空、超时——一律**回退到没有细纲**继续写正文。
多花一次调用不能变成多一道失败点。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .draft import count_chars, draft_dir
from .errors import ErrorKind, GenerationCancelled
from .llm import ApiError, OpenAICompatProvider, build_thinking_extra, extract_json_object

BEATS_SCHEMA_VERSION = "draft-beats-v1"
BEATS_MAX_TOKENS = 900
BEATS_MIN = 5
BEATS_MAX = 8


def beats_path(work_dir: str | Path, chapter_id: str) -> Path:
    return draft_dir(Path(work_dir)) / f"{chapter_id}.beats.json"


def brief_fingerprint(brief: dict[str, Any] | None) -> str:
    """只把**会影响节拍**的字段算进指纹。

    改标题、改语气不该让细纲作废；改一句话概要、核心情节、出场人物才该。
    """
    brief = brief or {}
    picked = {
        "chapter_id": brief.get("chapter_id"),
        "one_line": brief.get("one_line"),
        "core_plot": brief.get("core_plot"),
        "timeline": brief.get("timeline"),
        "settings": brief.get("settings"),
        "carry_over": brief.get("carry_over"),
        "must_not": brief.get("must_not"),
        "word_target": brief.get("word_target"),
    }
    raw = json.dumps(picked, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_beats(raw: Any) -> list[dict[str, Any]]:
    """把模型可能给出的几种形状统一成 [{no,title,goal,must}]。"""
    if isinstance(raw, dict):
        raw = raw.get("beats") or raw.get("items") or raw.get("list") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"no": index, "title": "", "goal": text, "must": []})
            continue
        if not isinstance(item, dict):
            continue
        goal = str(
            item.get("goal") or item.get("beat") or item.get("content")
            or item.get("summary") or item.get("desc") or ""
        ).strip()
        if not goal:
            continue
        must = item.get("must") or item.get("must_include") or []
        if isinstance(must, str):
            must = [must]
        must = [str(x).strip() for x in must if str(x).strip()] if isinstance(must, list) else []
        out.append(
            {
                "no": int(item.get("no") or item.get("index") or index),
                "title": str(item.get("title") or item.get("name") or "").strip(),
                "goal": goal,
                "must": must,
            }
        )
    return out


def parse_beats(text: str) -> list[dict[str, Any]]:
    """先按 JSON 解析；失败时退化到「1. xxx」逐行。两者都不成就返回空。"""
    if not (text or "").strip():
        return []
    data = extract_json_object(text)
    beats = normalize_beats(data)
    if beats:
        return beats
    # 模型偶尔会直接输出数组而不是对象
    candidate = (text or "").strip()
    start, end = candidate.find("["), candidate.rfind("]")
    if start != -1 and end > start:
        try:
            beats = normalize_beats(json.loads(candidate[start : end + 1]))
            if beats:
                return beats
        except ValueError:
            pass
    # 最后兜底：逐行编号
    lines = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?:[-*·]|\d+[.、)])\s*(.+)$", line)
        if m and len(m.group(1).strip()) >= 4:
            lines.append(m.group(1).strip())
    return normalize_beats(lines)[:BEATS_MAX]


def render_beats_for_prompt(beats: list[dict[str, Any]]) -> str:
    """把节拍渲染成正文提示词里的一段。**顺序就是写作顺序**。"""
    if not beats:
        return ""
    lines = [
        "【写前细纲】这一章按下面的节拍顺序写。每个节拍都要落到正文里，"
        "不要跳、不要换顺序；某一段写不动时宁可压短，也不要略过。",
    ]
    for beat in beats:
        head = f"{int(beat.get('no') or 0)}."
        if beat.get("title"):
            head += f" {beat['title']}"
        lines.append(head)
        lines.append(f"　　{beat.get('goal') or ''}")
        must = beat.get("must") or []
        if must:
            lines.append(f"　　必须写到：{'、'.join(str(x) for x in must)}")
    return "\n".join(lines)


def build_beat_messages(
    *,
    setting: dict[str, Any],
    volume: dict[str, Any],
    brief: dict[str, Any],
    word_range: list[int] | None = None,
    prev_tail: str = "",
) -> list[dict[str, str]]:
    """细纲调用的消息。刻意比正文短很多——这次调用是「策划」，不是「写作」。"""
    setting = setting or {}
    volume = volume or {}
    brief = brief or {}
    rng = word_range or brief.get("word_target") or [2800, 4000]
    if not isinstance(rng, list) or len(rng) != 2:
        rng = [2800, 4000]

    style = setting.get("style") or {}
    lead = ""
    for c in setting.get("characters") or []:
        if isinstance(c, dict) and str(c.get("role") or "").strip() == "主角":
            lead = str(c.get("name") or "")
            break
    vol = volume.get("vol") or {}
    settings = brief.get("settings") or {}
    parts = [
        "【作品】" + str(setting.get("work") or "（未名）")
        + (f"；主角：{lead}" if lead else "")
        + (f"；一句话前提：{setting['logline']}" if setting.get("logline") else ""),
        f"【本卷】第 {vol.get('vol') or '?'} 卷 {vol.get('title') or ''}"
        + (f"；卷目标：{vol['goal']}" if vol.get("goal") else "")
        + (f"；核心冲突：{vol['core_conflict']}" if vol.get("core_conflict") else ""),
        f"【本章】{brief.get('chapter_no') or ''} {brief.get('title') or ''}"
        + (f"；时间：{brief['timeline']}" if brief.get("timeline") else ""),
        f"【一句话概要】{brief.get('one_line') or '（没写）'}",
    ]
    core = brief.get("core_plot") or []
    if core:
        parts.append("【核心情节（必须按顺序覆盖）】")
        parts += [f"{i}. {x}" for i, x in enumerate(core, start=1)]
    if settings.get("characters"):
        parts.append("【必须出场】" + "、".join(str(x) for x in settings["characters"]))
    if brief.get("carry_over"):
        parts.append("【需承接】" + "、".join(str(x) for x in brief["carry_over"]))
    if brief.get("must_not"):
        parts.append("【禁止出现】" + "、".join(str(x) for x in brief["must_not"]))
    if style.get("perspective") or style.get("tone"):
        parts.append("【文风】" + "，".join(
            x for x in (f"视角 {style.get('perspective')}" if style.get("perspective") else "",
                        f"基调 {style.get('tone')}" if style.get("tone") else "") if x))
    if prev_tail:
        parts.append("【上一章末尾（只为接得上，不是本章内容）】\n…" + prev_tail[-500:])
    parts.append(f"【目标字数】{rng[0]}–{rng[1]} 字。节拍数量按这个篇幅定，5–8 个。")

    system = (
        "你是网文写手的写前策划。你只做一件事：把作者给的本章任务拆成连续节拍。\n"
        "硬约束：\n"
        "- 只输出 JSON，不要解释、不要代码围栏\n"
        "- 严格输出 {\"beats\": [...]} 结构\n"
        f"- beats 里放 {BEATS_MIN}–{BEATS_MAX} 个对象，顺序就是正文顺序\n"
        "- 每个对象字段：no（从 1 递增）、title（不超过 8 字的短标签）、"
        "goal（这个节拍要发生什么，不超过 40 字）、must（可选，必须点到的人/物/信息数组）\n"
        "- 必须覆盖【核心情节】里的每一项，不许漏，不许改顺序\n"
        "- 不要写台词，不要展开描写，不要替正文起草\n"
        "- 不要新增设定集里没有的人物、势力、物品"
    )
    user = "\n".join(parts) + "\n\n现在输出这一章的节拍 JSON。"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def save_beats(
    work_dir: str | Path,
    chapter_id: str,
    *,
    beats: list[dict[str, Any]],
    brief: dict[str, Any],
    model: str = "",
    source: str = "model",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    path = beats_path(work_dir, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": BEATS_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "brief_sha": brief_fingerprint(brief),
        "generated_at": _now_iso(),
        "model": model,
        "source": source,
        "count": len(beats),
        "usage": usage or {},
        "beats": beats,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {k: v for k, v in payload.items() if k != "beats"} | {"path": str(path)}


def load_beats(work_dir: str | Path, chapter_id: str) -> dict[str, Any] | None:
    path = beats_path(work_dir, chapter_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["beats"] = normalize_beats(data.get("beats") or [])
    return data


def load_reusable_beats(
    work_dir: str | Path, chapter_id: str, brief: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """指令没变就复用旧细纲。变了就返回空，让调用方重新生成。"""
    saved = load_beats(work_dir, chapter_id)
    if not saved or not saved.get("beats"):
        return [], saved
    if saved.get("brief_sha") != brief_fingerprint(brief):
        return [], saved
    return saved["beats"], saved


def _call_record(
    *,
    text: str,
    usage: dict[str, int],
    finish_reason: str,
    elapsed_ms: float,
    max_tokens: int,
    error_code: str = "",
) -> dict[str, Any]:
    return {
        "stage": "beats",
        "round": 0,
        "chars": count_chars(text),
        "raw_chars": len(text or ""),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "finish_reason": finish_reason,
        "elapsed_ms": round(float(elapsed_ms), 1),
        "max_tokens": max_tokens,
        "error_code": error_code,
        "ok": not error_code,
    }


def generate_beats(
    *,
    client: OpenAICompatProvider,
    model_id: str,
    setting: dict[str, Any],
    volume: dict[str, Any],
    brief: dict[str, Any],
    word_range: list[int] | None = None,
    prev_tail: str = "",
    max_tokens: int = BEATS_MAX_TOKENS,
    cancel_check=None,
    cancel_event=None,
) -> dict[str, Any]:
    """调用一次模型产出细纲。**不抛业务错误**：失败就返回 ok=False，由调用方回退。

    唯一会抛的是 `GenerationCancelled`——取消必须立刻穿透，不能被当成「细纲失败」吞掉。
    """
    if cancel_check and cancel_check():
        raise GenerationCancelled("已取消")
    messages = build_beat_messages(
        setting=setting, volume=volume, brief=brief, word_range=word_range, prev_tail=prev_tail
    )
    extra = build_thinking_extra("disabled", None)
    try:
        result = client.chat_stream(
            model_id, messages, max_tokens=max_tokens,
            temperature=0.3, extra=extra, cancel_event=cancel_event,
        )
    except GenerationCancelled:
        raise
    except ApiError as exc:
        return {
            "ok": False,
            "beats": [],
            "error_code": exc.kind.value,
            "error": f"{exc.kind.value}：{exc.safe_body(client.secrets)}",
            "usage": {},
            "call": _call_record(
                text="", usage={}, finish_reason="", elapsed_ms=0,
                max_tokens=max_tokens, error_code=exc.kind.value,
            ),
        }
    usage = result.usage.to_dict() if result.usage else {
        "prompt_tokens": 0,
        "completion_tokens": result.output_tokens,
        "total_tokens": result.output_tokens,
    }
    beats = parse_beats(result.text)
    ok = bool(beats)
    error_code = "" if ok else ErrorKind.RESPONSE_UNPARSABLE.value
    return {
        "ok": ok,
        "beats": beats,
        "error_code": error_code,
        "error": "" if ok else "细纲没有解析出可用的节拍，已回退为不注入细纲继续写正文",
        "raw": result.text,
        "usage": usage,
        "finish_reason": result.finish_reason,
        "call": _call_record(
            text=result.text, usage=usage, finish_reason=result.finish_reason,
            elapsed_ms=result.total_ms, max_tokens=max_tokens, error_code=error_code,
        ),
    }
