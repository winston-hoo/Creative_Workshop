"""定稿后处理编排（P0 批次 4：B5）。

定稿不是「改一个状态字段」就完事。定稿意味着这一章从此是后续章节的事实来源，
所以要做四件事：

    1. snapshot  存一份定稿版本（可回滚，已有则跳过）
    2. notes     抽章节要点（省钱：同一份正文只抽一次）
    3. summary   重建这一章的摘要快照（纯脚本）
    4. state     把已确认角色状态聚合成派生索引（纯脚本）

## 幂等与重试

每一步都记在 `<章节>.finalize.json` 里，带输入指纹（正文 sha）。重复调用时：

· 正文没变、这一步已完成 → `skipped`，不重复写、不重复花钱；
· 某一步失败 → 其它步照常走，整体记 `partial`，作者可只重试失败的那一步；
· `steps=["notes"]` + `force=True` → 只重跑这一步（例如换了提示词或模型）。

## 不碰作者真源

这里所有产物都是派生层（快照 / 要点提案 / 摘要 / 状态索引），
**没有一个字节写进 `60-setting/setting.yaml`**。要点还要作者在 U8 里逐条确认，
确认后才进下一章上下文。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

FINALIZE_SCHEMA = "chapter-finalize-v1"
STEPS = ("snapshot", "notes", "summary", "state")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def finalize_path(work_dir: str | Path, chapter_id: str) -> Path:
    return Path(work_dir) / "90-draft" / f"{chapter_id}.finalize.json"


def load_state(work_dir: str | Path, chapter_id: str) -> dict[str, Any]:
    path = finalize_path(work_dir, chapter_id)
    empty = {
        "schema_version": FINALIZE_SCHEMA, "chapter_id": chapter_id,
        "status": "idle", "prose_sha": "", "updated_at": "",
        "steps": {step: {"status": "pending"} for step in STEPS},
        "history": [],
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("steps", {})
    for step in STEPS:
        data["steps"].setdefault(step, {"status": "pending"})
    data.setdefault("history", [])
    return data


def save_state(work_dir: str | Path, chapter_id: str, payload: dict[str, Any]) -> Path:
    path = finalize_path(work_dir, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "schema_version": FINALIZE_SCHEMA,
               "chapter_id": chapter_id, "updated_at": now_iso()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# 每一步的调用签名：返回 {ok, skipped?, detail...}。注入它，测试就不用真调模型。
StepFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _chapter_meta(work_dir: Path, chapter_id: str) -> tuple[int, int]:
    from .chapter_brief import parse_chapter_id

    parsed = parse_chapter_id(chapter_id)
    if not parsed:
        return 1, 0
    vol, cno = parsed
    return int(vol), int(cno)


def _snapshot_step(ctx: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
    from . import draft_store

    work_dir, chapter_id = ctx["work_dir"], ctx["chapter_id"]
    text = ctx["prose"]
    versions = draft_store.list_versions(work_dir, chapter_id)
    latest = versions[-1] if versions else None
    if latest:
        loaded = draft_store.load_version(work_dir, chapter_id, str(latest.get("version_id")))
        if loaded and str(loaded.get("text") or "") == text:
            return {"ok": True, "skipped": True, "reason": "最新版本就是当前定稿",
                    "version_id": latest.get("version_id")}
    version = draft_store.snapshot_version(
        work_dir, chapter_id, text=text, meta=ctx.get("meta") or {},
        reason="定稿时自动备份",
    )
    return {"ok": True, "skipped": False, "version_id": (version or {}).get("version_id", "")}


def _notes_step(ctx: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
    from . import notes as notes_mod

    work_dir, chapter_id = ctx["work_dir"], ctx["chapter_id"]
    prose = ctx["prose"]
    payload = notes_mod.load_notes(work_dir, chapter_id)
    same_source = str((payload.get("source") or {}).get("prose_sha") or "") == sha256(prose)
    if payload.get("notes") and same_source and not ctx.get("force_component"):
        return {"ok": True, "skipped": True, "count": len(payload.get("notes") or []),
                "reason": "同一份正文已经抽过要点"}
    extractor = ctx.get("extractor")
    if extractor is None:
        return {"ok": False, "skipped": False,
                "error": "没有可用的抽取器（未配置模型或调用被跳过）"}
    result = extractor()
    count = len(result.get("items") or [])
    return {
        "ok": True, "skipped": False, "count": count,
        "invalid": len(result.get("invalid") or []),
        "call": result.get("call") or {},
        "attempts": result.get("attempts") or [],
    }


def _summary_step(ctx: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
    from . import summary as summary_mod

    work_dir, chapter_id = ctx["work_dir"], ctx["chapter_id"]
    vol, cno = _chapter_meta(work_dir, chapter_id)
    snap = summary_mod.build_snapshot(work_dir, chapter_id, vol=vol, chapter_no=cno)
    return {"ok": True, "skipped": False, "source_sha": snap["source_sha"],
            "note_count": snap["note_count"], "available": snap["available"]}


def _state_step(ctx: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
    from . import notes as notes_mod

    rows = notes_mod.rebuild_character_state(ctx["work_dir"])
    return {"ok": True, "skipped": False,
            "characters": len(rows.get("characters") or [])}


STEP_FUNCS: dict[str, StepFn] = {
    "snapshot": _snapshot_step,
    "notes": _notes_step,
    "summary": _summary_step,
    "state": _state_step,
}


def run(
    work_dir: str | Path,
    chapter_id: str,
    *,
    text: str,
    meta: dict[str, Any] | None = None,
    steps: list[str] | None = None,
    force: bool = False,
    extractor: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """跑定稿后处理。返回完整状态（界面直接画）。

    `steps` 只跑指定步骤；`force=True` 忽略「已完成」的短路。
    """
    work_dir = Path(work_dir)
    state = load_state(work_dir, chapter_id)
    prose_sha = sha256(text)
    same_prose = str(state.get("prose_sha") or "") == prose_sha
    wanted = [s for s in (steps or STEPS) if s in STEPS]
    if not wanted:
        raise ValueError("steps 只能是 " + " / ".join(STEPS))
    if not same_prose:
        # 正文换过了：旧步骤结论全部作废，重跑（但保留历史）。
        state["steps"] = {step: {"status": "pending"} for step in STEPS}
        state["history"].append({
            "at": now_iso(), "action": "reset",
            "reason": f"正文已变（sha {str(state.get('prose_sha') or '')[:12]} → {prose_sha[:12]}）",
        })
    state["prose_sha"] = prose_sha
    state["status"] = "running"
    save_state(work_dir, chapter_id, state)

    ctx = {
        "work_dir": work_dir, "chapter_id": chapter_id, "prose": text,
        "meta": meta or {}, "extractor": extractor,
    }
    calls: list[dict[str, Any]] = []
    for step in wanted:
        prev = state["steps"].get(step) or {}
        if same_prose and not force and str(prev.get("status")) == "done":
            state["steps"][step] = {**prev, "status": "done", "skipped": True,
                                    "checked_at": now_iso()}
            state["history"].append({"at": now_iso(), "action": "skip", "step": step,
                                     "reason": "这一步已经在同一份正文上完成过"})
            continue
        try:
            ctx["force_component"] = bool(force)
            result = STEP_FUNCS[step](ctx, state)
        except Exception as exc:  # noqa: BLE001 — 单步失败不能拖垮其它步
            state["steps"][step] = {
                "status": "failed", "at": now_iso(), "error": str(exc),
            }
            state["history"].append({"at": now_iso(), "action": "failed", "step": step,
                                     "error": str(exc)})
            continue
        status = "done" if result.get("ok") else "failed"
        state["steps"][step] = {"status": status, "at": now_iso(), **result}
        state["history"].append({
            "at": now_iso(), "action": status, "step": step,
            "skipped": bool(result.get("skipped")), "error": result.get("error") or "",
        })
        if result.get("call"):
            calls.append(result["call"])

    failed = [s for s in wanted if state["steps"][s].get("status") == "failed"]
    state["status"] = "partial" if failed else "done"
    state["calls"] = calls
    state["finished_at"] = now_iso()
    state["history"].append({"at": now_iso(), "action": state["status"],
                             "steps": wanted, "failed": failed})
    save_state(work_dir, chapter_id, state)
    return state
