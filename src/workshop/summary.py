"""按章摘要快照（P0 批次 4：A5）。

## 为什么快照

前情（`render_recap`）每次都现拼卷表里的 `gist`。那是**大纲阶段的计划**，
正文写完后实际情况可能已经变了；而正文要点（`notes.py`）又只有作者确认后
才可信。于是需要一层「把已确认要点压成一句可读摘要」的缓存：

    已确认要点 → 摘要快照 → 前情块

## 关键不变式

· 快照只是**派生缓存**，可以随时重建、删掉；真源仍是作者的三层真源。
· 快照记录它来自哪份要点（`source_sha` + `note_ids`）。要点一改，
  `stale` 立刻为真；`record = load_snapshot()` 会把 `stale` 一起返回。
· `source_sha` 与当前要点集合不一致时，前情**回退卷表摘要**，绝不拿旧快照
  假装是新的。重建后 `source_sha` 必然与当前来源一致（测试会验这条）。

## 为什么不做模型调用

摘要只做「把合格要点排成一句」，不需要再花一次钱，也不会引入第二套口径。
模型只在抽取要点时用一次。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SUMMARY_SCHEMA = "chapter-summary-v1"
TYPE_ORDER = {"state": 0, "fact": 1, "foreshadow": 2}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def summary_dir(work_dir: str | Path) -> Path:
    return Path(work_dir) / "90-draft" / ".summary"


def summary_path(work_dir: str | Path, chapter_id: str) -> Path:
    return summary_dir(work_dir) / f"{chapter_id}.json"


def _notes_of_chapter(work_dir: str | Path, chapter_id: str) -> list[dict[str, Any]]:
    from .notes import load_notes

    payload = load_notes(work_dir, chapter_id)
    return [
        n for n in payload.get("notes") or []
        if str(n.get("status")) in ("accepted", "edited") and not n.get("superseded_by")
    ]


def source_digest(work_dir: str | Path, chapter_id: str) -> str:
    """当前「已确认要点」的指纹。空集合用空串，表示没有可用的要点来源。"""
    rows = _notes_of_chapter(work_dir, chapter_id)
    if not rows:
        return ""
    canonical = [
        {
            "id": str(n.get("id") or ""),
            "type": str(n.get("type") or ""),
            "ref": str(n.get("ref") or ""),
            "text": str(n.get("text") or ""),
            "status": str(n.get("status") or ""),
            "superseded_by": str(n.get("superseded_by") or ""),
        }
        for n in sorted(rows, key=lambda n: (TYPE_ORDER.get(str(n.get("type")), 9),
                                             str(n.get("id") or "")))
    ]
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compose(chapter_id: str, vol: int, chapter_no: int, notes: list[dict[str, Any]]) -> str:
    """把要点压成一句/几句摘要。不用模型，排序固定，保证可复现。"""
    ordered = sorted(notes, key=lambda n: (TYPE_ORDER.get(str(n.get("type")), 9),
                                           str(n.get("id") or "")))
    states = [n for n in ordered if str(n.get("type")) == "state"]
    facts = [n for n in ordered if str(n.get("type")) == "fact"]
    fores = [n for n in ordered if str(n.get("type")) == "foreshadow"]
    bits: list[str] = []
    if states:
        bits.append("状态：" + "；".join(
            f"{n.get('ref')}——{n.get('text')}" for n in states))
    if facts:
        bits.append("事实：" + "；".join(
            (f"{n.get('ref')}：" if n.get("ref") else "") + str(n.get("text")) for n in facts))
    if fores:
        bits.append("伏笔：" + "；".join(
            f"{n.get('ref')}：{n.get('text')}" for n in fores))
    body = "｜".join(bits)
    return body


def build_snapshot(
    work_dir: str | Path, chapter_id: str, *, vol: int, chapter_no: int
) -> dict[str, Any]:
    """按当前已确认要点重建快照并落盘。没有要点时仍写一份「空快照」，
    但 `available=False`——前情会据此回退卷摘要。"""
    notes = _notes_of_chapter(work_dir, chapter_id)
    digest = source_digest(work_dir, chapter_id)
    payload = {
        "schema_version": SUMMARY_SCHEMA,
        "chapter_id": chapter_id,
        "vol": int(vol),
        "chapter_no": int(chapter_no),
        "available": bool(notes),
        "source_sha": digest,
        "note_ids": [str(n.get("id") or "") for n in notes],
        "note_count": len(notes),
        "summary": _compose(chapter_id, int(vol), int(chapter_no), notes),
        "label": f"第 {chapter_no} 章 {chapter_id}："
                 + (_compose(chapter_id, int(vol), int(chapter_no), notes) or "（没有已确认要点）"),
        "built_at": now_iso(),
    }
    path = summary_path(work_dir, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def load_snapshot(work_dir: str | Path, chapter_id: str) -> dict[str, Any] | None:
    path = summary_path(work_dir, chapter_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data = dict(data)
    current = source_digest(work_dir, chapter_id)
    data["current_source_sha"] = current
    data["stale"] = str(data.get("source_sha") or "") != current
    data["snapshot_sha"] = hashlib.sha256(
        json.dumps({k: data.get(k) for k in (
            "chapter_id", "source_sha", "note_ids", "summary",
        )}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return data


def usable_snapshot(work_dir: str | Path, chapter_id: str) -> dict[str, Any] | None:
    """只有「有要点 + 未过期」的快照才能进前情。"""
    snap = load_snapshot(work_dir, chapter_id)
    if not snap or snap.get("stale") or not snap.get("available"):
        return None
    return snap


def recap_line(
    work_dir: str | Path, chapter_id: str, *,
    vol: int, chapter_no: int, fallback_gist: str,
) -> tuple[str, str]:
    """给前情用的单章摘要：优先已确认要点快照，否则回退卷表 gist。

    返回 (文本, 来源标签)。来源标签会写进前情块，作者能看出这一章
    用的是「要点」还是「卷摘要」。
    """
    snap = usable_snapshot(work_dir, chapter_id)
    if snap:
        return str(snap.get("summary") or ""), "notes"
    gist = str(fallback_gist or "").strip()
    if not gist:
        return "", ""
    return gist, "volume"


def rebuild(work_dir: str | Path, chapters: Iterable[tuple[str, int, int]] | None = None) -> list[dict[str, Any]]:
    """批量重建。`chapters` 是 (chapter_id, vol, chapter_no) 列表；不给则扫 notes 文件。"""
    if chapters is None:
        found: list[tuple[str, int, int]] = []
        draft_dir = Path(work_dir) / "90-draft"
        if draft_dir.exists():
            for path in sorted(draft_dir.glob("*.notes.json")):
                chapter_id = path.name[: -len(".notes.json")]
                from .notes import load_notes

                payload = load_notes(work_dir, chapter_id)
                if not payload.get("notes"):
                    continue
                first = payload["notes"][0]
                found.append((chapter_id, int(first.get("vol") or 1),
                              int(first.get("chapter_no") or 0)))
        chapters = found
    out: list[dict[str, Any]] = []
    for chapter_id, vol, chapter_no in chapters:
        out.append(build_snapshot(
            work_dir, chapter_id, vol=int(vol or 1), chapter_no=int(chapter_no or 0)
        ))
    return out


def status(work_dir: str | Path) -> list[dict[str, Any]]:
    """界面上「哪些快照过期了」用。"""
    rows: list[dict[str, Any]] = []
    draft_dir = Path(work_dir) / "90-draft"
    if not draft_dir.exists():
        return rows
    for path in sorted(draft_dir.glob("*.notes.json")):
        chapter_id = path.name[: -len(".notes.json")]
        snap = load_snapshot(work_dir, chapter_id)
        rows.append({
            "chapter_id": chapter_id,
            "exists": snap is not None,
            "available": bool(snap and snap.get("available")),
            "stale": bool(snap and snap.get("stale")),
            "note_count": int((snap or {}).get("note_count") or 0),
            "source_sha": str((snap or {}).get("source_sha") or ""),
            "summary": str((snap or {}).get("summary") or ""),
        })
    return rows
