"""章节要点与事实回流（P0 批次 4：A1 / U8）。

## 这一层解决什么

写完一章之后，真正对下一章有用的是「这一章确认了什么」：谁受了伤、谁漏了面、
哪条伏笔被埋下。原来的前情只有卷表里的 `gist`，那是**写之前**的计划；
正文里实际发生的事、角色实际变成什么样，没人记。

这里把正文抽成三类结构化要点：

    fact       已经发生、后续必须当成事实的事
    state      角色/势力当前的状态（位置、伤势、关系、处境）
    foreshadow 新埋下或推进了的伏笔

## 三条硬规矩

1. **抽取结果只是提案**。它落在 `<章节>.notes.json`，永远不会写进
   `60-setting/setting.yaml`。作者的三层真源只有作者能改。
2. **只有作者确认过的要点**才会进下一章的上下文。`pending` 不算，
   `discarded` 绝不算。这样「接受一条 / 丢弃一条」在下一章的预算账本里
   是能直接看见的。
3. **每一条都能追溯**：哪一章、哪次生成（prose_sha / 模型）、谁在什么时候
   确认的、改过什么，都留在条目自己的 `history` 里。

## 冲突不静默覆盖

新要点若和前面已确认的 `state` 要点指向同一个人且说法不同，会被标成
`conflict`（带 `conflicts_with` 与旧说法原文），由作者裁决。作者接受新条目时，
旧条目只被标 `superseded_by` 并保留原文与历史，不删除。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOTES_SCHEMA = "chapter-notes-v1"
NOTE_TYPES = ("fact", "state", "foreshadow")
NOTE_STATUSES = ("pending", "accepted", "edited", "discarded")
MAX_NOTES = 12
MIN_TEXT_CHARS = 6
MAX_TEXT_CHARS = 180
MIN_EVIDENCE_CHARS = 6

TYPE_LABELS = {"fact": "事实", "state": "状态", "foreshadow": "伏笔"}
STATUS_LABELS = {
    "pending": "待确认", "accepted": "已接受", "edited": "已编辑", "discarded": "已丢弃",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def notes_path(work_dir: str | Path, chapter_id: str) -> Path:
    return Path(work_dir) / "90-draft" / f"{chapter_id}.notes.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_notes(work_dir: str | Path, chapter_id: str) -> dict[str, Any]:
    path = notes_path(work_dir, chapter_id)
    empty = {
        "schema_version": NOTES_SCHEMA, "chapter_id": chapter_id, "notes": [],
        "source": {}, "generated_at": "", "updated_at": "",
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("notes", [])
    data.setdefault("source", {})
    return data


def save_notes(work_dir: str | Path, chapter_id: str, payload: dict[str, Any]) -> Path:
    path = notes_path(work_dir, chapter_id)
    payload = {
        **payload, "schema_version": NOTES_SCHEMA, "chapter_id": chapter_id,
        "updated_at": now_iso(),
    }
    _write_json(path, payload)
    return path


def has_notes(work_dir: str | Path, chapter_id: str) -> bool:
    return bool(load_notes(work_dir, chapter_id).get("notes"))


# ── 关联对象（章节 / 卷 / 角色）─────────────────────────────


def known_refs(
    setting: dict[str, Any] | None,
    brief: dict[str, Any] | None,
    *,
    extra_foreshadow_ids: list[str] | None = None,
) -> list[dict[str, str]]:
    """可被要点引用的人/势力/术语/伏笔。

    `主角` 这类角色标签会映射到设定集里的真实姓名——正文里写的是名字，
    要点里再写「主角」，下一章就对不上具体是谁了。
    """
    setting = setting or {}
    brief = brief or {}
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str, kind: str, note: str = "") -> None:
        name = str(name or "").strip()
        if not name or name in seen:
            return
        seen.add(name)
        out.append({"ref": name, "kind": kind, "note": note})

    chars = [c for c in (setting.get("characters") or []) if isinstance(c, dict)]
    lead = next((c for c in chars if str(c.get("role") or "").strip() == "主角"), None)
    for c in chars:
        add(str(c.get("name") or ""), "character", str(c.get("role") or ""))
        for alias in c.get("aliases") or []:
            add(str(alias), "character", f"{c.get('name')} 的别名")
    for f in setting.get("factions") or []:
        if isinstance(f, dict):
            add(str(f.get("name") or ""), "faction", "势力")
    for t in setting.get("terms") or []:
        if isinstance(t, dict):
            add(str(t.get("term") or t.get("name") or ""), "term", "术语")
        elif str(t).strip():
            add(str(t), "term", "术语")
    for an in setting.get("abilities") or []:
        if isinstance(an, dict):
            add(str(an.get("name") or ""), "ability", "能力")
    # 章节指令里的出场人物可能还没进设定集（新角色第一次登场）。
    # 它们仍要能和「本章」对上号，否则这一章的状态就无处可挂。
    for name in (brief.get("settings") or {}).get("characters") or []:
        add(str(name), "character", "本章指令里的出场人物")
    if lead and "主角" in seen:
        # 标签与真名并存时，把标签指向真名，抽取输出仍写标签也能对上。
        out.append({"ref": "主角", "kind": "character",
                    "note": f"等同 {lead.get('name')}", "alias_of": str(lead.get("name"))})
    for fid in extra_foreshadow_ids or []:
        add(str(fid), "foreshadow", "已登记的伏笔编号")
    for row in brief.get("foreshadow") or []:
        if isinstance(row, dict):
            add(str(row.get("id") or ""), "foreshadow", str(row.get("desc") or "本章伏笔"))
    return out


def resolve_ref(ref: str, refs: list[dict[str, str]]) -> dict[str, str] | None:
    ref = str(ref or "").strip()
    if not ref:
        return None
    for row in refs:
        if row["ref"] == ref:
            if row.get("alias_of"):
                target = next((r for r in refs if r["ref"] == row["alias_of"]), None)
                return target or row
            return row
    return None


# ── 解析模型输出 ────────────────────────────────────────────


class NotesPayloadError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_notes", detail: str = "") -> None:
        self.code = code
        self.detail = detail or message
        super().__init__(message)


def extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise NotesPayloadError("要点抽取模型返回了空内容", code="empty_response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        first, last = raw.find("{"), raw.rfind("}")
        if first >= 0 and last > first:
            raw = raw[first:last + 1]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise NotesPayloadError(
            f"要点输出不是合法 JSON：{exc}", code="invalid_json", detail=str(exc),
        ) from exc
    if not isinstance(data, dict):
        raise NotesPayloadError("要点输出顶层不是对象", code="invalid_json")
    return data


def _clean_type(value: Any) -> str:
    kind = str(value or "").strip().lower()
    for alias, key in (("事实", "fact"), ("设定", "fact"), ("状态", "state"),
                       ("伏笔", "foreshadow"), ("埋设", "foreshadow")):
        if alias in kind:
            return key
    return kind if kind in NOTE_TYPES else ""


def _note_id(chapter_id: str, ref: str, text: str, kind: str) -> str:
    digest = hashlib.sha1(
        f"{chapter_id}|{kind}|{ref}|{text}".encode("utf-8")
    ).hexdigest()
    return f"nt-{digest[:10]}"


def normalize_notes(
    raw_items: Any,
    prose: str,
    *,
    chapter_id: str,
    vol: int,
    chapter_no: int,
    refs: list[dict[str, str]],
    prior_notes: list[dict[str, Any]] | None = None,
    source: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把模型给的要点收成可入库条目；收不了的写明原因，不静默丢。

    这里的校验是**入库门槛**：不合格的条目不进 notes.json，
    更不能进下一章的上下文。
    """
    prior_notes = prior_notes or []
    source = source or {}
    # 同一个关联对象的最新已确认状态：用来标冲突，绝不自动覆盖。
    prior_state: dict[str, dict[str, Any]] = {}
    for note in prior_notes:
        if str(note.get("type") or "") != "state":
            continue
        if str(note.get("status") or "") not in ("accepted", "edited"):
            continue
        if note.get("superseded_by"):
            continue
        ref = str(note.get("ref") or "")
        old = prior_state.get(ref)
        if old is None or int(note.get("chapter_no") or 0) > int(old.get("chapter_no") or 0):
            prior_state[ref] = note

    items: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items if isinstance(raw_items, list) else []):
        row = raw if isinstance(raw, dict) else {}
        kind = _clean_type(row.get("type"))
        ref_raw = str(row.get("ref") or row.get("character") or "").strip()
        text = str(row.get("text") or row.get("fact") or "").strip()
        evidence = str(row.get("evidence") or "").strip()
        if not kind:
            invalid.append({"index": index, "reason": "type 必须是 fact/state/foreshadow",
                            "ref": ref_raw, "text": text[:40]})
            continue
        if not ref_raw:
            invalid.append({"index": index, "reason": f"{TYPE_LABELS[kind]}要点缺少 ref（关联角色/伏笔）",
                            "text": text[:40]})
            continue
        target = resolve_ref(ref_raw, refs)
        if not target:
            invalid.append({"index": index, "reason": f"ref「{ref_raw}」对不上本章/设定集里的人和伏笔",
                            "ref": ref_raw, "text": text[:40]})
            continue
        if kind == "foreshadow" and target["kind"] != "foreshadow":
            invalid.append({"index": index, "reason": f"伏笔要点的 ref「{ref_raw}」不是伏笔编号",
                            "ref": ref_raw})
            continue
        if len(text) < MIN_TEXT_CHARS:
            invalid.append({"index": index, "reason": "要点太短，后续无法据此写作",
                            "ref": ref_raw, "text": text})
            continue
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS].rstrip()
        if evidence and evidence not in (prose or ""):
            invalid.append({"index": index,
                            "reason": "evidence 在正文里找不到（模型可能改过字）",
                            "ref": ref_raw, "text": text[:40], "evidence": evidence})
            continue
        nid = _note_id(chapter_id, target["ref"], text, kind)
        if nid in seen:
            invalid.append({"index": index, "reason": "与本批另一条完全重复",
                            "ref": ref_raw, "text": text[:40]})
            continue
        seen.add(nid)
        conflict = None
        if kind == "state":
            old = prior_state.get(target["ref"])
            if old and str(old.get("text") or "").strip() != text:
                conflict = {
                    "with": str(old.get("id") or ""),
                    "chapter": str(old.get("chapter") or ""),
                    "chapter_no": int(old.get("chapter_no") or 0),
                    "text": str(old.get("text") or ""),
                    "reason": "与之前已确认的状态说法不同，需要作者裁决（不会自动覆盖）",
                }
        items.append({
            "id": nid,
            "chapter": chapter_id,
            "vol": int(vol),
            "chapter_no": int(chapter_no),
            "ref": target["ref"],
            "ref_kind": target["kind"],
            "type": kind,
            "type_label": TYPE_LABELS[kind],
            "text": text,
            "evidence": evidence,
            "confidence": _clean_confidence(row.get("confidence")),
            "status": "pending",
            "conflict": conflict,
            "superseded_by": "",
            "confirmed_by": "",
            "confirmed_at": "",
            "discard_reason": "",
            "source_generation": {
                "prose_sha": str(source.get("prose_sha") or ""),
                "task_id": str(source.get("task_id") or ""),
                "model": str(source.get("model") or ""),
                "provider": str(source.get("provider") or ""),
            },
            "created_at": now_iso(),
            "history": [],
        })
        if len(items) >= MAX_NOTES:
            break
    return items, invalid


def _clean_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


# ── 提示词 ──────────────────────────────────────────────────


EXTRACT_SYSTEM = """你是长篇小说的连续性编辑。读完这一章正文，只做一件事：
把「下一章写作时必须记得的、这一章已经确认的」东西抽成结构化要点。

# 三类要点
- fact：已经发生、后续必须当成事实的事（谁做了什么、什么东西被拿走/毁掉、谁死了）。
- state：角色/势力此刻的状态（位置、伤势、体压/等级、掌握的东西、关系变化）。
  必须能在后续章节里被直接当作「当前状态」使用。
- foreshadow：这一章新埋下或推进了的伏笔。

# 铁律
1. 只写**正文里能证实**的内容。正文没写的、靠推测补的一律不要。
2. ref 必须从给定的「可关联对象」里原样选一个；不要自造人名。
3. 一条只说一件事。不要合并两个事实，也不要写成剧情梗概。
4. 每条 text 控制在 6–80 字；写具体状态，不写「气氛紧张」这种空话。
5. 不要输出对本章的点评、建议、总结，只要 JSON。
6. 最多 8 条，宁少勿凑。

# 输出格式
{"items":[{"type":"fact|state|foreshadow","ref":"关联对象","text":"要点","evidence":"正文里逐字出现的短句（可选）","confidence":0.0-1.0}]}
"""


def build_extract_messages(
    *, prose: str, brief: dict[str, Any], setting: dict[str, Any],
    refs: list[dict[str, str]],
) -> list[dict[str, str]]:
    ref_lines = []
    for row in refs:
        note = row.get("note") or ""
        ref_lines.append(f"- {row['ref']}（{row['kind']}{'：' + note if note else ''}）")
    command = json.dumps(
        {
            "chapter": (brief or {}).get("chapter_id") or "",
            "one_line": (brief or {}).get("one_line") or "",
            "core_plot": (brief or {}).get("core_plot") or [],
        },
        ensure_ascii=False,
    )
    user = (
        (f"【作品】{(setting or {}).get('work') or ''}\n\n" if setting else "")
        + (f"【本章任务】{command}\n\n" if command else "")
        + "【可关联对象】\n" + "\n".join(ref_lines) + "\n\n"
        + "【正文】\n" + (prose or "")
    )
    return [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": user},
    ]


# ── 调用 ────────────────────────────────────────────────────


def _call_usage(result: Any) -> dict[str, int]:
    usage = getattr(result, "usage", None)
    return usage.to_dict() if usage is not None and getattr(usage, "present", False) else {}


def run_extract(
    *,
    client: Any,
    model_id: str,
    provider_id: str,
    prose: str,
    brief: dict[str, Any],
    setting: dict[str, Any],
    refs: list[dict[str, str]],
    chapter_id: str,
    vol: int,
    chapter_no: int,
    prior_notes: list[dict[str, Any]] | None = None,
    max_tokens: int = 3000,
) -> dict[str, Any]:
    """调一次要点抽取。JSON 坏掉重试一次；两次都不行就明确报错。"""
    import time

    from .errors import ErrorKind
    from .llm import build_thinking_extra

    messages = build_extract_messages(prose=prose, brief=brief, setting=setting, refs=refs)
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for attempt in range(2):
        start = time.perf_counter()
        try:
            result = client.chat(
                model=model_id, messages=messages,
                max_tokens=max_tokens, temperature=0.2,
                extra=build_thinking_extra("disabled"),
            )
        except Exception as exc:  # noqa: BLE001
            code = str(getattr(getattr(exc, "kind", None), "value", "") or
                       ErrorKind.UNKNOWN.value)
            attempts.append({"attempt": attempt + 1, "ok": False, "error_code": code,
                             "error": str(exc)})
            raise NotesPayloadError(f"要点抽取调用失败（{code}）：{exc}", code=code) from exc
        elapsed_ms = (time.perf_counter() - start) * 1000
        usage = _call_usage(result)
        finish_reason = str(getattr(result, "finish_reason", "") or "")
        row = {
            "attempt": attempt + 1, "elapsed_ms": round(elapsed_ms, 1),
            "usage": usage, "finish_reason": finish_reason,
            "chars": len(getattr(result, "text", "") or ""),
        }
        attempts.append(row)
        try:
            data = extract_json_object(getattr(result, "text", "") or "")
            items, invalid = normalize_notes(
                data.get("items"), prose, chapter_id=chapter_id, vol=vol,
                chapter_no=chapter_no, refs=refs, prior_notes=prior_notes,
                source={
                    "prose_sha": sha256(prose), "model": model_id,
                    "provider": provider_id,
                    "task_id": str(data.get("task_id") or ""),
                },
            )
        except NotesPayloadError as exc:
            last_error = exc
            if finish_reason == "length":
                raise NotesPayloadError(
                    "要点输出被 max_tokens 截断，JSON 不完整。把 max_tokens 调大再试。",
                    code="truncated", detail=str(exc),
                ) from exc
            row["error"] = str(exc)
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": (getattr(result, "text", "") or "")[:400]},
                    {"role": "user", "content":
                        "上一条不是合法 JSON。只输出一个 JSON 对象，不要围栏、不要解释；"
                        "ref 必须从「可关联对象」里原样选。"},
                ]
                continue
            raise exc
        if not items and invalid:
            if attempt == 0:
                row["error"] = "没有任何一条要点通过校验"
                messages = messages + [
                    {"role": "assistant", "content": (getattr(result, "text", "") or "")[:400]},
                    {"role": "user", "content":
                        "你给的所有要点都被校验拒了。检查 ref 是否原样来自「可关联对象」、"
                        "text 是否只写正文能证实的内容。重来一次。"},
                ]
                continue
            raise NotesPayloadError(
                f"连续两次抽取都没有合格要点（{len(invalid)} 条被拒："
                f"{invalid[0].get('reason') or '格式不符'}）",
                code="no_valid_notes",
            )
        return {
            "ok": True,
            "items": items,
            "invalid": invalid,
            "attempts": attempts,
            "usage": usage,
            "finish_reason": finish_reason,
            "call": {
                "stage": "notes_extract", "round": 0,
                "chars": len(prose or ""), "new_chars": 0, "total_chars": len(prose or ""),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "finish_reason": finish_reason,
                "elapsed_ms": round(sum(float(a.get("elapsed_ms") or 0) for a in attempts), 1),
                "max_tokens": max_tokens,
                "attempt_count": len(attempts),
                "error_code": "", "ok": True,
            },
        }
    if last_error:
        raise last_error
    raise NotesPayloadError("要点抽取没有产出结果", code="empty_response")


# ── 落库与作者裁决 ──────────────────────────────────────────


def save_extraction(
    work_dir: str | Path,
    chapter_id: str,
    *,
    items: list[dict[str, Any]],
    invalid: list[dict[str, Any]],
    prose: str,
    source: dict[str, Any],
    replace: bool = True,
    call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一次抽取结果落盘。

    `replace=True` 只替换**待确认**的提案：作者已经接受/编辑/丢弃的条目
    一律保留（连同 history）。否则「换个模型再抽一次」会把作者的裁决悄悄冲掉，
    这比抽不出要点更糟。
    """
    previous = load_notes(work_dir, chapter_id)
    if replace:
        payload = {
            "schema_version": NOTES_SCHEMA, "chapter_id": chapter_id,
            "notes": [n for n in previous.get("notes") or []
                      if str(n.get("status") or "pending") != "pending"],
            "history": list(previous.get("history") or []),
            "source": {}, "generated_at": "",
        }
    else:
        payload = previous
    kept: list[dict[str, Any]] = list(payload.get("notes") or [])
    added = 0
    for item in items:
        old = next((n for n in kept if n.get("id") == item["id"]), None)
        if old is not None:
            # 同一章、同一句、同一关联对象：不重复入库，也不覆盖作者已做的裁决。
            continue
        kept.append(item)
        added += 1
    payload["notes"] = kept
    payload["source"] = {
        **source, "prose_sha": sha256(prose), "chars": len(prose or ""),
        "call": call or {},
    }
    payload["generated_at"] = now_iso()
    payload["last_invalid"] = invalid
    payload.setdefault("history", []).append({
        "at": now_iso(), "action": "extract",
        "added": added, "kept": len(kept) - added, "invalid": len(invalid),
        "prose_sha": sha256(prose),
    })
    save_notes(work_dir, chapter_id, payload)
    return payload


def find_note(payload: dict[str, Any], note_id: str) -> dict[str, Any] | None:
    return next((n for n in payload.get("notes") or [] if str(n.get("id")) == str(note_id)), None)


def update_note(
    work_dir: str | Path,
    chapter_id: str,
    note_id: str,
    *,
    action: str,
    text: str = "",
    by: str = "作者",
    reason: str = "",
) -> dict[str, Any]:
    """作者对单条要点的裁决：accept / edit / discard / reset。

    · accept：确认原文本（或作者同时改过的文本）进下一章上下文；
    · edit：确认改写后的文本；旧文本留在 history；
    · discard：丢弃，绝不进下一章上下文；
    · reset：退回待确认（给「反悔」留门）。
    """
    action = str(action or "").strip().lower()
    if action not in ("accept", "edit", "discard", "reset"):
        raise ValueError("action 只能是 accept / edit / discard / reset")
    payload = load_notes(work_dir, chapter_id)
    note = find_note(payload, note_id)
    if not note:
        raise ValueError(f"找不到要点「{note_id}」")
    before_status = str(note.get("status") or "pending")
    before_text = str(note.get("text") or "")
    new_text = str(text or "").strip() if action == "edit" else before_text
    if action == "edit" and (len(new_text) < MIN_TEXT_CHARS or len(new_text) > MAX_TEXT_CHARS):
        raise ValueError(f"要点长度要在 {MIN_TEXT_CHARS}–{MAX_TEXT_CHARS} 字之间")

    now = now_iso()
    if action == "accept":
        note["status"] = "accepted"
    elif action == "edit":
        note["status"] = "edited"
    elif action == "discard":
        note["status"] = "discarded"
        note["discard_reason"] = reason or "作者丢弃"
    else:
        note["status"] = "pending"
        note["confirmed_by"] = ""
        note["confirmed_at"] = ""
        note["discard_reason"] = ""
    if action in ("accept", "edit"):
        note["confirmed_by"] = by
        note["confirmed_at"] = now
        note["discard_reason"] = ""
    note["text"] = new_text
    note.setdefault("history", []).append({
        "at": now, "action": action, "by": by,
        "from": before_status, "to": str(note["status"]),
        "text_before": before_text, "text_after": new_text,
        "reason": reason,
    })

    # 冲突：作者确认新状态时，旧状态只标「已被取代」，原文与历史都保留。
    if action in ("accept", "edit") and isinstance(note.get("conflict"), dict):
        old_id = str(note["conflict"].get("with") or "")
        old_chapter = str(note["conflict"].get("chapter") or "")
        if old_id and old_chapter:
            old_payload = load_notes(work_dir, old_chapter)
            old = find_note(old_payload, old_id)
            if old:
                old["superseded_by"] = note_id
                old["superseded_at"] = now
                old.setdefault("history", []).append({
                    "at": now, "action": "superseded", "by": by,
                    "reason": f"被 {chapter_id}/{note_id} 的已确认状态取代（原文保留）",
                })
                save_notes(work_dir, old_chapter, old_payload)
        note["conflict"]["resolved_at"] = now
        note["conflict"]["resolution"] = "作者确认新状态"

    payload.setdefault("history", []).append({
        "at": now, "action": action, "note_id": note_id, "by": by,
        "from": before_status, "to": str(note["status"]),
    })
    save_notes(work_dir, chapter_id, payload)
    return {"ok": True, "note": note, "payload": payload}


def public_notes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """界面用的条目：带标签、按类型与章号排序，并明确哪些会进下一章。"""
    rows: list[dict[str, Any]] = []
    for note in payload.get("notes") or []:
        row = dict(note)
        row["type_label"] = TYPE_LABELS.get(str(note.get("type")), "其他")
        row["status_label"] = STATUS_LABELS.get(str(note.get("status")), "未知")
        row["in_context"] = (
            str(note.get("status")) in ("accepted", "edited")
            and not note.get("superseded_by")
        )
        row["has_conflict"] = bool(note.get("conflict"))
        rows.append(row)
    order = {"state": 0, "fact": 1, "foreshadow": 2}
    rows.sort(key=lambda r: (order.get(str(r.get("type")), 9), r.get("created_at") or ""))
    return rows


def notes_summary(payload: dict[str, Any]) -> dict[str, Any]:
    notes = payload.get("notes") or []
    counts = {status: 0 for status in NOTE_STATUSES}
    for n in notes:
        counts[str(n.get("status") or "pending")] = counts.get(
            str(n.get("status") or "pending"), 0
        ) + 1
    return {
        "chapter_id": payload.get("chapter_id"),
        "total": len(notes),
        "counts": counts,
        "in_context": sum(1 for n in notes
                          if str(n.get("status")) in ("accepted", "edited")
                          and not n.get("superseded_by")),
        "conflicts": sum(1 for n in notes if n.get("conflict")),
        "source": payload.get("source") or {},
        "generated_at": payload.get("generated_at") or "",
        "path": "",
    }


# ── 回流：下一章装配读这里 ──────────────────────────────────


def iter_work_notes(work_dir: str | Path) -> list[dict[str, Any]]:
    """全部章节里已经确认、且没有被取代的要点，按章号排序。"""
    out: list[dict[str, Any]] = []
    draft_dir = Path(work_dir) / "90-draft"
    if not draft_dir.exists():
        return out
    for path in sorted(draft_dir.glob("*.notes.json")):
        chapter_id = path.name[: -len(".notes.json")]
        payload = load_notes(work_dir, chapter_id)
        for note in payload.get("notes") or []:
            if str(note.get("status")) not in ("accepted", "edited"):
                continue
            if note.get("superseded_by"):
                continue
            out.append(note)
    out.sort(key=lambda n: (int(n.get("chapter_no") or 0), int(n.get("vol") or 0),
                            str(n.get("id") or "")))
    return out


def render_notes(
    work_dir: str | Path,
    *,
    before_chapter_no: int = 0,
    limit: int = 20,
) -> str:
    """下一章的「已确认要点」块。

    **独立成块**，不混进前情：作者在预算账本里要能单独看见它占了多少字。
    """
    if not before_chapter_no:
        return ""
    rows = [n for n in iter_work_notes(work_dir)
            if int(n.get("chapter_no") or 0) < before_chapter_no]
    if not rows:
        return ""
    # 最近的章优先；同一章里状态 > 事实 > 伏笔
    order = {"state": 0, "fact": 1, "foreshadow": 2}
    rows.sort(key=lambda n: (-int(n.get("chapter_no") or 0), order.get(str(n.get("type")), 9)))
    kept = rows[:limit]
    lines = [
        "【已确认章节要点】这些是作者逐条确认过的、前面章节已经发生的事与当前状态。"
        "写作时把它们当作事实；与设定集冲突时以作者的设定集为准，不要自行改写。"
    ]
    for n in kept:
        cno = int(n.get("chapter_no") or 0)
        label = TYPE_LABELS.get(str(n.get("type")), "要点")
        ref = str(n.get("ref") or "")
        if str(n.get("type")) == "foreshadow":
            line = f"· 第 {cno} 章｜伏笔 {ref}：{n.get('text')}"
        elif str(n.get("type")) == "state":
            line = f"· 第 {cno} 章｜{ref} 当前状态：{n.get('text')}"
        else:
            line = f"· 第 {cno} 章｜事实（{ref}）：{n.get('text')}"
        lines.append(line)
    if len(rows) > len(kept):
        lines.append(f"· …另有 {len(rows) - len(kept)} 条已确认要点未注入（按最近章优先）")
    return "\n".join(lines)


def context_source(work_dir: str | Path, *, before_chapter_no: int = 0) -> str:
    """装配报告里用的来源：具体哪几章的 notes、共几条。"""
    rows = [n for n in iter_work_notes(work_dir)
            if int(n.get("chapter_no") or 0) < int(before_chapter_no or 0)]
    if not rows:
        return ""
    chapters = sorted({str(n.get("chapter") or "") for n in rows if n.get("chapter")})
    files = "、".join(f"90-draft/{cid}.notes.json" for cid in chapters)
    return f"{files}（{len(rows)} 条已确认）"


def latest_states(work_dir: str | Path) -> dict[str, dict[str, Any]]:
    """每个角色最新一条已确认状态。「角色状态优先读要点」的抓手。"""
    out: dict[str, dict[str, Any]] = {}
    for note in iter_work_notes(work_dir):
        if str(note.get("type")) != "state":
            continue
        ref = str(note.get("ref") or "")
        if not ref:
            continue
        old = out.get(ref)
        if old is None or int(note.get("chapter_no") or 0) > int(old.get("chapter_no") or 0):
            out[ref] = note
    return out


def character_state_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / "90-draft" / ".state" / "character-state.json"


def rebuild_character_state(work_dir: str | Path) -> dict[str, Any]:
    """把「已确认状态要点」聚合成一份派生索引。**不是真源**，可随时重建。"""
    states = latest_states(work_dir)
    payload = {
        "schema_version": "character-state-v1",
        "generated_at": now_iso(),
        "note": "由已确认的 state 要点聚合而来，不是作者手写的真源；改设定请改 60-setting/setting.yaml",
        "characters": [
            {
                "ref": ref, "text": n.get("text"), "chapter": n.get("chapter"),
                "chapter_no": int(n.get("chapter_no") or 0), "note_id": n.get("id"),
                "confirmed_by": n.get("confirmed_by"), "confirmed_at": n.get("confirmed_at"),
            }
            for ref, n in sorted(states.items())
        ],
    }
    path = character_state_path(work_dir)
    _write_json(path, payload)
    return payload
