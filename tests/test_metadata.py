"""派生元数据库（workshop.metadata）的单元测试。

不碰真实作品：每个用例在 tmp_path 里造一部最小作品。
重点覆盖：删库重建一致、重复同步幂等、手改文件被纠正、坏库降级、
多作品隔离、源文件零改动、版本升级前备份。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from workshop import metadata  # noqa: E402

WORK = "测试书"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, ensure_ascii=False))


def _make_work(
    root: Path,
    name: str = WORK,
    *,
    status: str = "final",
    chars_text: str = "第一段正文。\n\n第二段正文。\n\n第三段正文。",
) -> Path:
    ws = root / "workspaces" / name
    draft = ws / "90-draft"
    cid = "v001-c0001"
    _write(
        ws / "60-setting" / "setting.yaml",
        yaml.safe_dump(
            {
                "schema_version": "setting-v1",
                "work": name,
                "characters": [{"name": "主角", "role": "主角"}],
                "factions": [],
                "terms": [],
            },
            allow_unicode=True,
        ),
    )
    _write(
        ws / "70-volume" / "volume-001.yaml",
        yaml.safe_dump(
            {
                "schema_version": "volume-v1",
                "work": name,
                "vol": {
                    "vol": 1,
                    "title": "第一卷",
                    "start_chapter": 1,
                    "end_chapter": 1,
                    "chapters": [{"chapter_no": 1, "title": "第一章"}],
                },
            },
            allow_unicode=True,
        ),
    )
    _write(draft / f"{cid}.md", chars_text)
    _write_json(
        draft / f"{cid}.status.json",
        {
            "schema_version": "draft-status-v1",
            "chapter_id": cid,
            "status": status,
            "updated_at": "2026-01-01T00:00:00+08:00",
            "history": [],
        },
    )
    _write_json(
        draft / f"{cid}.notes.json",
        {
            "schema_version": "chapter-notes-v1",
            "chapter_id": cid,
            "notes": [
                {"id": "n1", "text": "要点一", "status": "pending"},
                {"id": "n2", "text": "要点二", "status": "accepted"},
                {"id": "n3", "text": "要点三", "status": "pending"},
            ],
        },
    )
    _write_json(
        draft / f"{cid}.review.json",
        {
            "schema_version": "draft-review-v1",
            "chapter_id": cid,
            "reviews": [
                {
                    "review_id": "r1",
                    "created_at": "2026-01-01T00:00:00+08:00",
                    "model": "m",
                    "items": [
                        {"id": "i1", "status": "open"},
                        {"id": "i2", "status": "adopted"},
                    ],
                    "invalid": [],
                    "attempt_count": 1,
                },
                {
                    "review_id": "r2",
                    "created_at": "2026-01-02T00:00:00+08:00",
                    "model": "m",
                    "items": [
                        {"id": "i3", "status": "open"},
                        {"id": "i4", "status": "rejected"},
                        {"id": "i5", "status": "open"},
                    ],
                    "invalid": [{"reason": "x"}],
                    "attempt_count": 2,
                },
            ],
        },
    )
    vdir = draft / ".versions" / cid
    for vid, at in (
        ("20260101T000000-aaaa", "2026-01-01T00:00:00+08:00"),
        ("20260102T000000-bbbb", "2026-01-02T00:00:00+08:00"),
    ):
        _write_json(
            vdir / f"{vid}.json",
            {
                "schema_version": "draft-version-v1",
                "version_id": vid,
                "chapter_id": cid,
                "created_at": at,
                "chars": 120,
                "reason": "覆盖前自动备份",
            },
        )
        _write(vdir / f"{vid}.md", "旧正文")
    cdir = draft / ".candidate" / cid
    _write_json(
        cdir / "cand1.json",
        {
            "schema_version": "draft-candidate-v1",
            "candidate_id": "cand1",
            "chapter_id": cid,
            "created_at": "2026-01-03T00:00:00+08:00",
            "chars": 130,
            "reason": "模型改写",
        },
    )
    _write_json(
        draft / ".tasks" / "task-abc.json",
        {
            "schema_version": "generation-task-v1",
            "task_id": "task-abc",
            "work": name,
            "chapter_id": cid,
            "kind": "draft",
            "status": "done",
            "stage": "完成",
            "created_at": "2026-01-01T00:30:00+08:00",
            "updated_at": "2026-01-01T01:00:00+08:00",
            "finished_at": "2026-01-01T01:00:00+08:00",
            "chars": 120,
            "elapsed_ms": 1234,
        },
    )
    return ws


def _dump(root: Path) -> dict[str, list[dict]]:
    """导出所有派生行；剔除纯时钟字段，剩下的应当可重建且逐字一致。"""
    conn = sqlite3.connect(str(metadata.meta_db_path(root)))
    conn.row_factory = sqlite3.Row
    try:
        out: dict[str, list[dict]] = {}
        chapters = conn.execute(
            "SELECT * FROM chapters ORDER BY work, chapter_id"
        ).fetchall()
        out["chapters"] = [
            {k: r[k] for k in r.keys() if k != "updated_at"} for r in chapters
        ]
        out["review_runs"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM review_runs ORDER BY work, chapter_id, review_id"
            ).fetchall()
        ]
        out["artifacts"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM artifacts ORDER BY work, kind, chapter_id, item_id"
            ).fetchall()
        ]
        out["tasks"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM tasks ORDER BY work, task_id"
            ).fetchall()
        ]
        out["files"] = [
            {k: r[k] for k in r.keys() if k != "extracted_at"}
            for r in conn.execute(
                "SELECT * FROM files ORDER BY work, path"
            ).fetchall()
        ]
        return out
    finally:
        conn.close()


def _sha_of_sources(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".index" in path.parts:
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def work_root(tmp_path: Path) -> Path:
    _make_work(tmp_path)
    return tmp_path


def test_rebuild_is_identical(work_root: Path) -> None:
    first = metadata.sync(work_root, force=True)
    assert first["ok"] is True
    before = _dump(work_root)

    result = metadata.rebuild(work_root)
    assert result["ok"] is True
    assert result["changed"] > 0
    after = _dump(work_root)
    assert after == before


def test_sync_is_idempotent(work_root: Path) -> None:
    metadata.sync(work_root, force=True)
    before = _dump(work_root)
    second = metadata.sync(work_root)
    assert second["ok"] is True
    assert second["changed"] == 0
    assert _dump(work_root) == before
    third = metadata.sync(work_root, force=True)
    assert third["changed"] > 0
    assert _dump(work_root) == before


def test_extracted_values_match_live_files(work_root: Path) -> None:
    metadata.sync(work_root, force=True)
    state = metadata.chapter_states(work_root, WORK)["v001-c0001"]
    assert state["draft_exists"] == 1
    assert state["status"] == "final"
    assert state["chars"] == len("第一段正文。第二段正文。第三段正文。")
    assert state["paragraphs"] == 3
    assert state["notes_total"] == 3
    assert state["notes_pending"] == 2
    assert state["review_total"] == 5
    assert state["review_open"] == 3
    assert state["reviews_count"] == 2
    assert state["version_count"] == 2
    assert state["current_version_id"] == "20260102T000000-bbbb"
    assert state["candidate_count"] == 1

    runs = metadata.review_counters(work_root, WORK, "v001-c0001")
    assert [r["review_id"] for r in runs] == ["r2", "r1"]
    assert runs[0]["items"] == 3 and runs[0]["open"] == 2 and runs[0]["invalid"] == 1

    tasks = metadata.task_history(work_root, WORK, chapter_id="v001-c0001")
    assert [t["task_id"] for t in tasks] == ["task-abc"]
    assert tasks[0]["elapsed_ms"] == 1234


def test_manual_status_edit_is_corrected(work_root: Path) -> None:
    metadata.sync(work_root, force=True)
    assert metadata.chapter_states(work_root, WORK)["v001-c0001"]["status"] == "final"

    status_path = work_root / "workspaces" / WORK / "90-draft" / "v001-c0001.status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["status"] = "draft"
    _write_json(status_path, payload)

    sync = metadata.sync(work_root)
    assert sync["ok"] is True and sync["changed"] == 1
    assert metadata.chapter_states(work_root, WORK)["v001-c0001"]["status"] == "draft"


def test_git_rollback_changes_are_detected(work_root: Path) -> None:
    """内容改回旧版本（即使 mtime 被改到过去）也要按 sha 认出来。"""
    metadata.sync(work_root, force=True)
    status_path = work_root / "workspaces" / WORK / "90-draft" / "v001-c0001.status.json"
    original = status_path.read_text(encoding="utf-8")
    payload = json.loads(original)
    payload["status"] = "reviewed"
    _write_json(status_path, payload)
    metadata.sync(work_root)
    assert metadata.chapter_states(work_root, WORK)["v001-c0001"]["status"] == "reviewed"

    _write(status_path, original)
    metadata.sync(work_root)
    assert metadata.chapter_states(work_root, WORK)["v001-c0001"]["status"] == "final"


def test_deleted_files_clear_contributions(work_root: Path) -> None:
    metadata.sync(work_root, force=True)
    notes = work_root / "workspaces" / WORK / "90-draft" / "v001-c0001.notes.json"
    notes.unlink()
    sync = metadata.sync(work_root)
    assert sync["removed"] == 1
    state = metadata.chapter_states(work_root, WORK)["v001-c0001"]
    assert state["notes_total"] == 0 and state["notes_pending"] == 0

    version = (
        work_root / "workspaces" / WORK / "90-draft" / ".versions"
        / "v001-c0001" / "20260102T000000-bbbb.json"
    )
    version.unlink()
    metadata.sync(work_root)
    state = metadata.chapter_states(work_root, WORK)["v001-c0001"]
    assert state["version_count"] == 1
    assert state["current_version_id"] == "20260101T000000-aaaa"


def test_multiple_works_are_isolated(tmp_path: Path) -> None:
    _make_work(tmp_path, "甲书", status="final")
    _make_work(tmp_path, "乙书", status="draft")
    assert metadata.sync(tmp_path, force=True)["ok"] is True
    assert metadata.chapter_states(tmp_path, "甲书")["v001-c0001"]["status"] == "final"
    assert metadata.chapter_states(tmp_path, "乙书")["v001-c0001"]["status"] == "draft"
    only_a = metadata.sync(tmp_path, work="甲书")
    assert only_a["works"] == ["甲书"]


def test_corrupt_db_degrades_to_files(work_root: Path) -> None:
    metadata.sync(work_root, force=True)
    db = metadata.meta_db_path(work_root)
    db.write_bytes(b"this is not a sqlite database")
    assert metadata.chapter_states(work_root, WORK) is None
    sync = metadata.sync(work_root)
    assert sync["ok"] is False and sync["error"]
    assert metadata.status(work_root)["available"] is False

    # 页面路径必须继续能跑：退回现读文件。
    from server import services

    paths = services.Paths(work_root)
    overview = services.creation_overview(paths, WORK)
    chapter = next(c for c in overview["chapters"] if c["chapter_id"] == "v001-c0001")
    assert chapter["draft_status"] == "final"
    assert chapter["pending_notes"] == 2
    assert chapter["open_reviews"] == 3

    # 删掉坏库后自动恢复。
    db.unlink()
    assert metadata.sync(work_root)["ok"] is True
    assert metadata.status(work_root)["available"] is True


def test_schema_change_backs_up_before_rebuild(work_root: Path) -> None:
    metadata.sync(work_root, force=True)
    conn = sqlite3.connect(str(metadata.meta_db_path(work_root)))
    conn.execute("UPDATE meta SET value='meta-v0' WHERE key='schema'")
    conn.commit()
    conn.close()

    result = metadata.sync(work_root, force=True)
    assert result["ok"] is True
    backup = metadata.meta_db_path(work_root).with_name("meta.db.bak-meta-v0")
    assert backup.exists() and backup.stat().st_size > 0
    assert metadata.status(work_root)["schema"] == metadata.META_SCHEMA_VERSION
    assert metadata.chapter_states(work_root, WORK)["v001-c0001"]["status"] == "final"


def test_source_files_are_never_modified(work_root: Path) -> None:
    before = _sha_of_sources(work_root)
    metadata.sync(work_root, force=True)
    metadata.sync(work_root, force=True)
    metadata.rebuild(work_root)
    assert _sha_of_sources(work_root) == before


def test_creation_overview_matches_live_values(work_root: Path) -> None:
    from server import services

    paths = services.Paths(work_root)
    metadata.sync(work_root, force=True)
    overview = services.creation_overview(paths, WORK)
    chapter = next(c for c in overview["chapters"] if c["chapter_id"] == "v001-c0001")
    assert chapter["has_draft"] is True
    assert chapter["draft_status"] == "final"
    assert chapter["pending_notes"] == 2
    assert chapter["open_reviews"] == 3


def test_api_endpoints(work_root: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from server import main as server_main
    from server import services

    monkeypatch.setattr(server_main, "PATHS", services.Paths(work_root))
    client = TestClient(server_main.app)

    status = client.get("/api/meta/status").json()
    assert status["available"] is True
    assert status["schema"] == metadata.META_SCHEMA_VERSION

    synced = client.post("/api/meta/sync", params={"force": True}).json()
    assert synced["ok"] is True

    chapters = client.get(f"/api/works/{WORK}/meta/chapters").json()
    assert chapters["available"] is True
    assert chapters["items"][0]["current_version_id"] == "20260102T000000-bbbb"

    tasks = client.get(f"/api/works/{WORK}/meta/tasks").json()
    assert tasks["available"] is True and len(tasks["items"]) == 1

    reviews = client.get(f"/api/works/{WORK}/meta/reviews/v001-c0001").json()
    assert reviews["available"] is True and len(reviews["items"]) == 2

