"""草稿版本与恢复候选。

## 为什么必须有

写正文是**覆盖式**的：每写一次，`90-draft/<章节>.md` 就被新内容替换。
模型失败、被截断、服务重启、作者误点重写——任何一种都会让上一份正文消失，
而作者往往要过一会儿才意识到「原来那版更好」。

所以这里把「已经正式落盘的版本」和「这次没写成、但写出了半截的候选」分开存：

  · `.versions/<章节>/` —— 每次**成功覆盖前**先快照一份，可回看、可恢复；
  · `.candidate/<章节>/` —— 每次失败/中断/取消留下的半截正文，可转正、可从断点续写。

两处都在 `90-draft/` 下，跟着作品走，换机器也一起搬。
不引入数据库：候选就是普通 JSON，作者能直接打开看。

保留策略是**有限保留**：版本留最近 5 份、候选留最近 8 份。
无限留会把一个章节的目录堆到几十份，作者反而找不到想要的那份。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .draft import count_chars, draft_dir, load_draft, save_draft

VERSIONS_KEEP = 5
CANDIDATES_KEEP = 8

VERSION_SCHEMA = "draft-version-v1"
CANDIDATE_SCHEMA = "draft-candidate-v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _safe_id(value: str) -> str:
    """版本/候选 id 来自磁盘，恢复接口会把它拼进路径。挡掉穿越。"""
    value = str(value or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value):
        raise ValueError(f"版本编号「{value}」不合法")
    return value


def versions_dir(work_dir: str | Path, chapter_id: str) -> Path:
    return draft_dir(Path(work_dir)) / ".versions" / chapter_id


def candidates_dir(work_dir: str | Path, chapter_id: str) -> Path:
    return draft_dir(Path(work_dir)) / ".candidate" / chapter_id


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _prune(directory: Path, keep: int, pattern: str) -> None:
    files = sorted(directory.glob(pattern), key=lambda p: p.name)
    for path in files[:-keep] if len(files) > keep else []:
        try:
            path.unlink()
        except OSError:
            pass


def snapshot_version(
    work_dir: str | Path,
    chapter_id: str,
    *,
    text: str,
    meta: dict[str, Any] | None = None,
    reason: str = "覆盖前自动备份",
) -> dict[str, Any] | None:
    """把现有正文存成一份历史版本。空文本不存——没有内容可回退。"""
    if not (text or "").strip():
        return None
    directory = versions_dir(work_dir, chapter_id)
    directory.mkdir(parents=True, exist_ok=True)
    sha = _sha(text)
    version_id = f"{_stamp()}-{sha[:8]}"
    while (directory / f"{version_id}.md").exists():
        version_id = f"{_stamp()}-{uuid.uuid4().hex[:6]}"
    (directory / f"{version_id}.md").write_text(text, encoding="utf-8")
    payload = {
        "schema_version": VERSION_SCHEMA,
        "version_id": version_id,
        "chapter_id": chapter_id,
        "created_at": _now_iso(),
        "chars": count_chars(text),
        "sha256": sha,
        "reason": reason,
        "meta": meta or {},
    }
    _write_json(directory / f"{version_id}.json", payload)
    _prune(directory, VERSIONS_KEEP, "*.md")
    _prune(directory, VERSIONS_KEEP, "*.json")
    return payload


def list_versions(work_dir: str | Path, chapter_id: str) -> list[dict[str, Any]]:
    directory = versions_dir(work_dir, chapter_id)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        data = _read_json(path)
        if not data:
            continue
        data["exists"] = (directory / f"{data.get('version_id')}.md").exists()
        rows.append(data)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows


def load_version(work_dir: str | Path, chapter_id: str, version_id: str) -> dict[str, Any] | None:
    directory = versions_dir(work_dir, chapter_id)
    vid = _safe_id(version_id)
    meta = _read_json(directory / f"{vid}.json")
    text_path = directory / f"{vid}.md"
    if not meta or not text_path.exists():
        return None
    return {**meta, "text": text_path.read_text(encoding="utf-8")}


def save_candidate(
    work_dir: str | Path,
    chapter_id: str,
    *,
    text: str,
    meta: dict[str, Any] | None = None,
    reason: str = "",
    task_id: str = "",
) -> dict[str, Any] | None:
    """把失败/中断时写出来的半截正文留成候选。空文本不留。"""
    if not (text or "").strip():
        return None
    directory = candidates_dir(work_dir, chapter_id)
    directory.mkdir(parents=True, exist_ok=True)
    sha = _sha(text)
    candidate_id = f"{_stamp()}-{sha[:8]}"
    if (directory / f"{candidate_id}.json").exists():
        candidate_id = f"{_stamp()}-{uuid.uuid4().hex[:6]}"
    payload = {
        "schema_version": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "chapter_id": chapter_id,
        "created_at": _now_iso(),
        "chars": count_chars(text),
        "sha256": sha,
        "reason": reason or "生成失败/中断时保留的半截正文",
        "task_id": task_id,
        "meta": meta or {},
        "text": text,
    }
    _write_json(directory / f"{candidate_id}.json", payload)
    _prune(directory, CANDIDATES_KEEP, "*.json")
    return {k: v for k, v in payload.items() if k != "text"}


def list_candidates(work_dir: str | Path, chapter_id: str) -> list[dict[str, Any]]:
    directory = candidates_dir(work_dir, chapter_id)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        data = _read_json(path)
        if not data:
            continue
        rows.append({k: v for k, v in data.items() if k != "text"})
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows


def load_candidate(work_dir: str | Path, chapter_id: str, candidate_id: str) -> dict[str, Any] | None:
    directory = candidates_dir(work_dir, chapter_id)
    cid = _safe_id(candidate_id)
    return _read_json(directory / f"{cid}.json")


def promote_candidate(
    work_dir: str | Path,
    chapter_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    """候选转正：先备份当前正文，再把候选写成正式稿。"""
    candidate = load_candidate(work_dir, chapter_id, candidate_id)
    if not candidate:
        raise ValueError(f"找不到候选「{candidate_id}」")
    text = str(candidate.get("text") or "")
    if not text.strip():
        raise ValueError("这条候选没有正文，不能转正")

    existing = load_draft(work_dir, chapter_id)
    if existing.get("exists"):
        snapshot_version(
            work_dir, chapter_id, text=existing["text"],
            meta=existing.get("meta") or {}, reason="候选转正前备份",
        )
    saved = save_draft(
        work_dir, chapter_id, text=text,
        meta={
            **(candidate.get("meta") or {}),
            "promoted_from_candidate": candidate_id,
            "promoted_at": _now_iso(),
            "chars": count_chars(text),
            "candidate_reason": candidate.get("reason") or "",
        },
    )
    return {"saved": True, "candidate_id": candidate_id, "chars": count_chars(text), **saved}


def restore_version(
    work_dir: str | Path,
    chapter_id: str,
    version_id: str,
) -> dict[str, Any]:
    """恢复到某个历史版本。恢复前先把**当前**正文存一份，避免这次恢复又变成不可逆。"""
    version = load_version(work_dir, chapter_id, version_id)
    if not version:
        raise ValueError(f"找不到版本「{version_id}」")
    text = str(version.get("text") or "")
    if not text.strip():
        raise ValueError("这个版本没有正文，不能恢复")

    existing = load_draft(work_dir, chapter_id)
    if existing.get("exists"):
        snapshot_version(
            work_dir, chapter_id, text=existing["text"],
            meta=existing.get("meta") or {}, reason=f"恢复 {version_id} 前备份",
        )
    saved = save_draft(
        work_dir, chapter_id, text=text,
        meta={
            **(version.get("meta") or {}),
            "restored_from_version": version_id,
            "restored_at": _now_iso(),
            "chars": count_chars(text),
        },
    )
    return {"saved": True, "version_id": version_id, "chars": count_chars(text), **saved}
