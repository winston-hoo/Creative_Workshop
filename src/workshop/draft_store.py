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
STATUS_SCHEMA = "draft-status-v1"

# D5：稿子状态机。**未审不能定稿**——`draft → final` 不在这个集合里。
# final 之后要重写，必须先显式 reopen 回 draft，不能悄悄覆盖一个「已定稿」。
DRAFT_STATUSES = ("draft", "reviewed", "final")
_ALLOWED_TRANSITIONS = {
    ("draft", "reviewed"),      # 审稿通过
    ("reviewed", "draft"),      # 按意见改写后需要复审
    ("reviewed", "final"),      # 定稿
    ("final", "draft"),         # 显式重新打开
}


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
    ensure_editable(work_dir, chapter_id)
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


def status_path(work_dir: str | Path, chapter_id: str) -> Path:
    return draft_dir(Path(work_dir)) / f"{chapter_id}.status.json"


def get_status(work_dir: str | Path, chapter_id: str) -> dict[str, Any]:
    """当前稿子状态。没有状态文件就当 `draft`——旧稿不用迁移也能继续用。"""
    path = status_path(work_dir, chapter_id)
    data = _read_json(path)
    if not data:
        return {"schema_version": STATUS_SCHEMA, "chapter_id": chapter_id,
                "status": "draft", "updated_at": "", "history": []}
    status = str(data.get("status") or "draft")
    if status not in DRAFT_STATUSES:
        status = "draft"
    data["status"] = status
    data.setdefault("history", [])
    return data


def ensure_editable(work_dir: str | Path, chapter_id: str) -> None:
    """正文写入前的闸门：已定稿的章不许悄悄改。

    `set_status` 只拦状态跳转，拦不住「直接把正文覆盖掉」——保存、候选转正、
    恢复版本、采纳意见这四条路都是写文件的。所以闸门必须放在写之前，
    并且报错要说清下一步该点哪里（「重新打开」）。
    """
    if get_status(work_dir, chapter_id).get("status") == "final":
        raise ValueError(
            "这一章已定稿。要改写正文（保存/候选转正/恢复版本/采纳意见），先点「重新打开」。"
        )


def set_status(
    work_dir: str | Path,
    chapter_id: str,
    to: str,
    *,
    reason: str = "",
    by: str = "ui",
) -> dict[str, Any]:
    """迁移状态。**非法跳转直接报错**，不悄悄改——闸门值钱的地方就在会拒绝。

    每次迁移写进 history（时间、从哪到哪、为什么、谁干的），
    否则「这章什么时候定的稿」只能靠猜。
    """
    if to not in DRAFT_STATUSES:
        raise ValueError(f"未知的稿子状态「{to}」，只能是 {'/'.join(DRAFT_STATUSES)}")
    data = get_status(work_dir, chapter_id)
    current = str(data.get("status") or "draft")
    if current == to:
        return data
    if (current, to) not in _ALLOWED_TRANSITIONS:
        if current == "draft" and to == "final":
            raise ValueError("这一章还没审过，不能定稿。先跑一次正文审稿。")
        if current == "final":
            raise ValueError("这一章已定稿。要重写或再审，先点「重新打开」。")
        raise ValueError(f"状态不能从「{current}」直接变成「{to}」")
    entry = {
        "at": _now_iso(),
        "from": current,
        "to": to,
        "reason": reason,
        "by": by,
    }
    data["status"] = to
    data["updated_at"] = entry["at"]
    data["history"] = list(data.get("history") or []) + [entry]
    _write_json(status_path(work_dir, chapter_id), data)
    return data


def restore_version(
    work_dir: str | Path,
    chapter_id: str,
    version_id: str,
) -> dict[str, Any]:
    """恢复到某个历史版本。恢复前先把**当前**正文存一份，避免这次恢复又变成不可逆。"""
    ensure_editable(work_dir, chapter_id)
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
