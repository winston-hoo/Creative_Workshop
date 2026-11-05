"""作品元数据的 SQLite 派生层。

## 死线：真源永远是文件

`60-setting/`、`70-volume/`、`80-brief/`、`90-draft/` 下的 md / yaml / json
是**唯一真源**：可读、可手改、可 git diff。本模块只存**能从这里完整重建**的
派生元数据——章节状态、字数、段落数、审稿/要点计数、版本与候选的元数据、
任务记录。正文、指令、审稿意见原文一律不进库。

所以这里没有「迁移真源」这一步：删掉 `meta.db`，下一轮 `sync()` 会从文件
重新算出完全一样的内容。任何需要正文的接口仍然读文件。

## 为什么单独一个库，不塞进 search.db

两个库都在 `<数据根>/.index/` 下，都是可删缓存，但生命周期不同：
检索库会因为分词器/表结构变化整库重建，而元数据库要走「版本号 + 备份 + 重建」。
塞在一起，重建检索索引会顺手把元数据也删掉，而且坏一个影响另一个。
分开存，故障域互不牵连；两库都 gitignore。多作品隔离靠每张表的 `work` 列。

## 一致性：以文件指纹为准

`files` 表记录每个源文件的 `(mtime_ns, size, sha)`。每次 `sync()` 扫一遍
关注的文件：stat 没变就跳过；变了就算 sha，sha 变了才重新抽取。因此
手改 JSON、git 回滚、外部编辑器保存都会被下一轮同步发现并纠正。
`sync()` 在读取概览之前执行，所以不存在「库说已定稿、文件其实没定稿」。

## 降级

所有对外函数捕获 `sqlite3.Error` / `OSError`：库损坏、磁盘只读、权限不足时
返回 `None` / `ok=False`，调用方退回原来的「现读文件」路径。库只是加速层，
不是可用性的前置条件。

## 结构升级

`meta(key='schema')` 记版本号。版本不匹配时：先把旧库复制成
`meta.db.bak-<旧版本>`（备份失败就放弃重建、继续用旧库），再删表重建。
重复执行 `sync()` 是幂等的：没变的文件一个都不重新解析。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

META_DIR_NAME = ".index"
META_DB_NAME = "meta.db"
META_SCHEMA_VERSION = "meta-v1"

DRAFT_DIR = "90-draft"
VERSIONS_DIR = ".versions"
CANDIDATES_DIR = ".candidate"
TASKS_DIR = ".tasks"

# 概览只需要「每章当前状态」这一组文件；版本/候选/任务不进这条路径。
# 分开扫描是为了让一次页面加载只付出它真正需要的 stat 成本。
STATE_KINDS = ("draft", "status", "notes", "review")
ALL_KINDS = STATE_KINDS + ("version", "candidate", "task")

STATUS_SUFFIX = ".status.json"
NOTES_SUFFIX = ".notes.json"
REVIEW_SUFFIX = ".review.json"

_LOCKS: dict[str, threading.RLock] = {}
_LAST_SYNC: dict[str, dict[str, Any]] = {}


def meta_db_path(root: str | Path) -> Path:
    return Path(root) / META_DIR_NAME / META_DB_NAME


def _lock(root: str | Path) -> threading.RLock:
    key = str(Path(root).resolve())
    return _LOCKS.setdefault(key, threading.RLock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_from_mtime(ns: int) -> str:
    try:
        return (
            datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
      work TEXT NOT NULL,
      path TEXT NOT NULL,
      kind TEXT NOT NULL,
      chapter_id TEXT NOT NULL DEFAULT '',
      mtime_ns INTEGER NOT NULL,
      size INTEGER NOT NULL,
      sha TEXT NOT NULL DEFAULT '',
      extracted_at TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (work, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chapters (
      work TEXT NOT NULL,
      chapter_id TEXT NOT NULL,
      draft_exists INTEGER NOT NULL DEFAULT 0,
      chars INTEGER NOT NULL DEFAULT 0,
      paragraphs INTEGER NOT NULL DEFAULT 0,
      draft_mtime TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'draft',
      status_updated_at TEXT NOT NULL DEFAULT '',
      notes_total INTEGER NOT NULL DEFAULT 0,
      notes_pending INTEGER NOT NULL DEFAULT 0,
      review_total INTEGER NOT NULL DEFAULT 0,
      review_open INTEGER NOT NULL DEFAULT 0,
      reviews_count INTEGER NOT NULL DEFAULT 0,
      last_review_at TEXT NOT NULL DEFAULT '',
      version_count INTEGER NOT NULL DEFAULT 0,
      current_version_id TEXT NOT NULL DEFAULT '',
      candidate_count INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (work, chapter_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_runs (
      work TEXT NOT NULL,
      chapter_id TEXT NOT NULL,
      review_id TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT '',
      model TEXT NOT NULL DEFAULT '',
      items INTEGER NOT NULL DEFAULT 0,
      open INTEGER NOT NULL DEFAULT 0,
      adopted INTEGER NOT NULL DEFAULT 0,
      rejected INTEGER NOT NULL DEFAULT 0,
      stale INTEGER NOT NULL DEFAULT 0,
      duplicate INTEGER NOT NULL DEFAULT 0,
      unlocatable INTEGER NOT NULL DEFAULT 0,
      invalid INTEGER NOT NULL DEFAULT 0,
      attempts INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (work, chapter_id, review_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
      work TEXT NOT NULL,
      chapter_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      item_id TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT '',
      chars INTEGER NOT NULL DEFAULT 0,
      reason TEXT NOT NULL DEFAULT '',
      file_exists INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (work, kind, chapter_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
      work TEXT NOT NULL,
      task_id TEXT NOT NULL,
      chapter_id TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT '',
      stage TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL DEFAULT '',
      finished_at TEXT NOT NULL DEFAULT '',
      chars INTEGER NOT NULL DEFAULT 0,
      elapsed_ms REAL NOT NULL DEFAULT 0,
      error_code TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (work, task_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chapters_work ON chapters(work)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_work_chapter ON tasks(work, chapter_id)",
)


def _connect(root: str | Path) -> sqlite3.Connection:
    path = meta_db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        # 坏库在这里就会报错。不关掉句柄的话，Windows 上随后「删库重建」
        # 会因文件被占用而删不掉，重建静默失败。
        conn.close()
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection, root: str | Path) -> None:
    """建表；版本变了先备份旧库，再整库重建。

    备份失败就**不重建**：宁可继续用旧结构跑（外层会退回现读文件），
    也不能在没有备份的情况下把一份可能是唯一副本的派生库删掉。
    """
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    old = None
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        old = row["value"] if row else None
    has_files = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
    ).fetchone()
    if old == META_SCHEMA_VERSION and has_files:
        return

    if old and old != META_SCHEMA_VERSION:
        db_path = meta_db_path(root)
        backup_path = db_path.with_name(f"{db_path.name}.bak-{old}")
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            # 用 sqlite 的备份 API 而不是直接复制文件：WAL 里可能还有没落盘
            # 的最新页，直接 copy 会得到一份缺数据的「备份」。
            target = sqlite3.connect(str(backup_path))
            try:
                conn.backup(target)
            finally:
                target.close()
        except (sqlite3.Error, OSError):
            # 备份失败就不重建：宁可继续用旧结构，也不能在没有备份的情况下
            # 把一份可能是唯一副本的派生库删掉。
            raise

    for table in (
        "chapters", "review_runs", "artifacts", "tasks", "files", "meta",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for sql in _SCHEMA_SQL:
        conn.execute(sql)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema', ?)", (META_SCHEMA_VERSION,))
    conn.commit()


# ── 文件发现与分类 ──────────────────────────────────────────


def _works(root: str | Path) -> list[Path]:
    base = Path(root) / "workspaces"
    if not base.exists():
        return []
    return [
        entry for entry in sorted(base.iterdir())
        if entry.is_dir() and not entry.name.startswith(("_", "."))
    ]


def _chapter_id_of(name: str, suffix: str) -> str:
    return name[: -len(suffix)] if name.endswith(suffix) else name


def _iter_work_files(work_dir: Path, kinds: tuple[str, ...] = ALL_KINDS) -> Iterable[tuple[Path, str, str, str]]:
    """产出 (绝对路径, 相对 work_dir 的 POSIX 路径, kind, chapter_id)。"""
    draft = work_dir / DRAFT_DIR
    if not draft.exists():
        return
    if any(k in kinds for k in ("draft", "status", "notes", "review")):
        try:
            entries = list(os.scandir(draft))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name
            path = Path(entry.path)
            if "draft" in kinds and name.endswith(".md"):
                yield path, f"{DRAFT_DIR}/{name}", "draft", Path(name).stem
            elif "status" in kinds and name.endswith(STATUS_SUFFIX):
                cid = _chapter_id_of(name, STATUS_SUFFIX)
                yield path, f"{DRAFT_DIR}/{name}", "status", cid
            elif "notes" in kinds and name.endswith(NOTES_SUFFIX):
                cid = _chapter_id_of(name, NOTES_SUFFIX)
                yield path, f"{DRAFT_DIR}/{name}", "notes", cid
            elif "review" in kinds and name.endswith(REVIEW_SUFFIX):
                cid = _chapter_id_of(name, REVIEW_SUFFIX)
                yield path, f"{DRAFT_DIR}/{name}", "review", cid
    for kind, folder in (("version", VERSIONS_DIR), ("candidate", CANDIDATES_DIR)):
        if kind not in kinds:
            continue
        base = draft / folder
        if not base.exists():
            continue
        try:
            chapter_dirs = [e for e in os.scandir(base) if e.is_dir()]
        except OSError:
            chapter_dirs = []
        for chapter_dir in chapter_dirs:
            try:
                entries = list(os.scandir(chapter_dir.path))
            except OSError:
                entries = []
            for entry in entries:
                if not entry.is_file() or not entry.name.endswith(".json"):
                    continue
                yield (
                    Path(entry.path),
                    f"{DRAFT_DIR}/{folder}/{chapter_dir.name}/{entry.name}",
                    kind,
                    chapter_dir.name,
                )
    if "task" in kinds:
        tasks = draft / TASKS_DIR
        if tasks.exists():
            try:
                entries = list(os.scandir(tasks))
            except OSError:
                entries = []
            for entry in entries:
                if not entry.is_file() or not entry.name.endswith(".json"):
                    continue
                # 任务文件自带 chapter_id；扫描时先留空，抽取时再填
                yield Path(entry.path), f"{DRAFT_DIR}/{TASKS_DIR}/{entry.name}", "task", ""


def _classify(rel: str) -> tuple[str, str]:
    """给磁盘上已删除的文件重新判定 kind / chapter_id（按路径约定）。"""
    parts = rel.split("/")
    name = parts[-1]
    if len(parts) >= 4 and parts[1] in (VERSIONS_DIR, CANDIDATES_DIR):
        kind = "version" if parts[1] == VERSIONS_DIR else "candidate"
        return kind, parts[2]
    if rel.startswith(f"{DRAFT_DIR}/"):
        if name.endswith(STATUS_SUFFIX):
            return "status", _chapter_id_of(name, STATUS_SUFFIX)
        if name.endswith(NOTES_SUFFIX):
            return "notes", _chapter_id_of(name, NOTES_SUFFIX)
        if name.endswith(REVIEW_SUFFIX):
            return "review", _chapter_id_of(name, REVIEW_SUFFIX)
        if name.endswith(".json") and len(parts) == 3 and parts[1] == TASKS_DIR:
            return "task", ""
        if name.endswith(".md"):
            return "draft", Path(name).stem
    return "unknown", ""


# ── 抽取 ────────────────────────────────────────────────────


def _ensure_chapter(conn: sqlite3.Connection, work: str, cid: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO chapters(work, chapter_id) VALUES (?, ?)", (work, cid)
    )


def _reset(conn: sqlite3.Connection, work: str, kind: str, cid: str, item_id: str = "") -> None:
    if not cid:
        return
    _ensure_chapter(conn, work, cid)
    if kind == "draft":
        conn.execute(
            "UPDATE chapters SET draft_exists=0, chars=0, paragraphs=0, draft_mtime='', updated_at=? "
            "WHERE work=? AND chapter_id=?",
            (_now_iso(), work, cid),
        )
    elif kind == "status":
        conn.execute(
            "UPDATE chapters SET status='draft', status_updated_at='', updated_at=? "
            "WHERE work=? AND chapter_id=?",
            (_now_iso(), work, cid),
        )
    elif kind == "notes":
        conn.execute(
            "UPDATE chapters SET notes_total=0, notes_pending=0, updated_at=? "
            "WHERE work=? AND chapter_id=?",
            (_now_iso(), work, cid),
        )
    elif kind == "review":
        conn.execute("DELETE FROM review_runs WHERE work=? AND chapter_id=?", (work, cid))
        conn.execute(
            "UPDATE chapters SET review_total=0, review_open=0, reviews_count=0, "
            "last_review_at='', updated_at=? WHERE work=? AND chapter_id=?",
            (_now_iso(), work, cid),
        )
    elif kind in ("version", "candidate"):
        if item_id:
            conn.execute(
                "DELETE FROM artifacts WHERE work=? AND kind=? AND chapter_id=? AND item_id=?",
                (work, kind, cid, item_id),
            )
        else:
            conn.execute(
                "DELETE FROM artifacts WHERE work=? AND kind=? AND chapter_id=?",
                (work, kind, cid),
            )
        _refresh_artifacts(conn, work, cid)
    elif kind == "task" and item_id:
        conn.execute("DELETE FROM tasks WHERE work=? AND task_id=?", (work, item_id))


def _refresh_artifacts(conn: sqlite3.Connection, work: str, cid: str) -> None:
    versions = conn.execute(
        "SELECT item_id, created_at FROM artifacts WHERE work=? AND kind='version' AND chapter_id=? "
        "ORDER BY created_at DESC, item_id DESC",
        (work, cid),
    ).fetchall()
    candidates = conn.execute(
        "SELECT COUNT(*) AS n FROM artifacts WHERE work=? AND kind='candidate' AND chapter_id=?",
        (work, cid),
    ).fetchone()
    current = versions[0]["item_id"] if versions else ""
    conn.execute(
        "UPDATE chapters SET version_count=?, current_version_id=?, candidate_count=?, updated_at=? "
        "WHERE work=? AND chapter_id=?",
        (len(versions), current, int(candidates["n"] if candidates else 0), _now_iso(), work, cid),
    )


def _paragraphs(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", text or "") if p.strip()])


def _apply(
    conn: sqlite3.Connection,
    work: str,
    kind: str,
    cid: str,
    rel: str,
    path: Path,
) -> None:
    """把一个文件的内容折算成派生字段。正文只取字数/段落，不落文本。"""
    if kind == "draft":
        from .draft import count_chars

        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = int(path.stat().st_mtime_ns)
        _ensure_chapter(conn, work, cid)
        conn.execute(
            "UPDATE chapters SET draft_exists=1, chars=?, paragraphs=?, draft_mtime=?, updated_at=? "
            "WHERE work=? AND chapter_id=?",
            (
                count_chars(text),
                _paragraphs(text),
                _iso_from_mtime(mtime),
                _now_iso(),
                work,
                cid,
            ),
        )
    elif kind == "status":
        data = _read_json(path) or {}
        status = str(data.get("status") or "draft")
        if status not in ("draft", "reviewed", "final"):
            status = "draft"
        _ensure_chapter(conn, work, cid)
        conn.execute(
            "UPDATE chapters SET status=?, status_updated_at=?, updated_at=? "
            "WHERE work=? AND chapter_id=?",
            (status, str(data.get("updated_at") or ""), _now_iso(), work, cid),
        )
    elif kind == "notes":
        data = _read_json(path) or {}
        notes = [n for n in (data.get("notes") or []) if isinstance(n, dict)]
        pending = sum(1 for n in notes if str(n.get("status") or "pending") == "pending")
        _ensure_chapter(conn, work, cid)
        conn.execute(
            "UPDATE chapters SET notes_total=?, notes_pending=?, updated_at=? "
            "WHERE work=? AND chapter_id=?",
            (len(notes), pending, _now_iso(), work, cid),
        )
    elif kind == "review":
        data = _read_json(path) or {}
        reviews = [r for r in (data.get("reviews") or []) if isinstance(r, dict)]
        _ensure_chapter(conn, work, cid)
        conn.execute("DELETE FROM review_runs WHERE work=? AND chapter_id=?", (work, cid))
        total = 0
        open_count = 0
        last_at = ""
        for review in reviews:
            items = [i for i in (review.get("items") or []) if isinstance(i, dict)]
            counts = {
                "open": 0, "adopted": 0, "rejected": 0, "stale": 0,
                "duplicate": 0, "unlocatable": 0,
            }
            for item in items:
                status = str(item.get("status") or "open")
                counts[status] = counts.get(status, 0) + 1
            created = str(review.get("created_at") or "")
            conn.execute(
                "INSERT OR REPLACE INTO review_runs("
                "work, chapter_id, review_id, created_at, model, items, open, adopted, "
                "rejected, stale, duplicate, unlocatable, invalid, attempts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    work, cid, str(review.get("review_id") or ""), created,
                    str(review.get("model") or ""), len(items), counts["open"],
                    counts["adopted"], counts["rejected"], counts["stale"],
                    counts["duplicate"], counts["unlocatable"],
                    len(review.get("invalid") or []), int(review.get("attempt_count") or 0),
                ),
            )
            total += len(items)
            open_count += counts["open"]
            last_at = max(last_at, created)
        conn.execute(
            "UPDATE chapters SET review_total=?, review_open=?, reviews_count=?, "
            "last_review_at=?, updated_at=? WHERE work=? AND chapter_id=?",
            (total, open_count, len(reviews), last_at, _now_iso(), work, cid),
        )
    elif kind in ("version", "candidate"):
        data = _read_json(path) or {}
        item_id = str(
            data.get("version_id") if kind == "version" else data.get("candidate_id")
        ) or path.stem
        exists = (
            (path.parent / f"{item_id}.md").exists() if kind == "version" else True
        )
        _ensure_chapter(conn, work, cid)
        conn.execute(
            "INSERT OR REPLACE INTO artifacts("
            "work, chapter_id, kind, item_id, created_at, chars, reason, file_exists) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                work, cid, kind, item_id, str(data.get("created_at") or ""),
                int(data.get("chars") or 0), str(data.get("reason") or ""),
                1 if exists else 0,
            ),
        )
        _refresh_artifacts(conn, work, cid)
    elif kind == "task":
        data = _read_json(path) or {}
        task_id = str(data.get("task_id") or path.stem)
        conn.execute(
            "INSERT OR REPLACE INTO tasks("
            "work, task_id, chapter_id, kind, status, stage, created_at, updated_at, "
            "finished_at, chars, elapsed_ms, error_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                work, task_id, str(data.get("chapter_id") or ""),
                str(data.get("kind") or ""), str(data.get("status") or ""),
                str(data.get("stage") or ""), str(data.get("created_at") or ""),
                str(data.get("updated_at") or ""), str(data.get("finished_at") or ""),
                int(data.get("chars") or 0), float(data.get("elapsed_ms") or 0),
                str(data.get("error_code") or ""),
            ),
        )


# ── 同步 ────────────────────────────────────────────────────


def sync(
    root: str | Path,
    *,
    work: str | None = None,
    force: bool = False,
    scope: str = "all",
) -> dict[str, Any]:
    """扫文件、按指纹增量更新派生库。返回统计；失败时 `ok=False` 并带原因。

    `scope="states"` 只同步章节状态那四类文件（页面加载用），版本/候选/任务
    保留上一轮全量同步的结果。
    """
    started = time.perf_counter()
    root_path = Path(root)
    kinds = STATE_KINDS if scope == "states" else ALL_KINDS
    result: dict[str, Any] = {
        "ok": False, "error": "", "works": [], "scanned": 0, "changed": 0,
        "removed": 0, "chapters": 0, "duration_ms": 0.0,
    }
    try:
        with _lock(root_path):
            conn = _connect(root_path)
            try:
                _ensure_schema(conn, root_path)
                targets = [w for w in _works(root_path) if not work or w.name == work]
                result["works"] = [w.name for w in targets]
                for work_dir in targets:
                    stats = _sync_work(conn, work_dir, force=force, kinds=kinds)
                    for key in ("scanned", "changed", "removed", "chapters"):
                        result[key] += stats[key]
                conn.commit()
                result["chapters"] = int(
                    conn.execute("SELECT COUNT(*) AS n FROM chapters").fetchone()["n"]
                )
                result["ok"] = True
            finally:
                conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    _LAST_SYNC[str(root_path)] = {**result, "at": _now_iso()}
    return result


def _sync_work(
    conn: sqlite3.Connection, work_dir: Path, *, force: bool, kinds: tuple[str, ...] = ALL_KINDS
) -> dict[str, int]:
    work = work_dir.name
    seen: set[str] = set()
    stats = {"scanned": 0, "changed": 0, "removed": 0, "chapters": 0}

    stored = {
        row["path"]: row
        for row in conn.execute(
            "SELECT path, kind, chapter_id, mtime_ns, size, sha FROM files "
            "WHERE work=? AND kind IN (" + ",".join("?" * len(kinds)) + ")",
            (work, *kinds),
        )
    }

    for path, rel, kind, cid in _iter_work_files(work_dir, kinds):
        seen.add(rel)
        stats["scanned"] += 1
        try:
            stat = path.stat()
        except OSError:
            continue
        prev = stored.get(rel)
        if (
            not force
            and prev
            and int(prev["mtime_ns"]) == stat.st_mtime_ns
            and int(prev["size"]) == stat.st_size
        ):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        sha = _sha_bytes(data)
        if prev and prev["sha"] and prev["sha"] == sha and not force:
            conn.execute(
                "UPDATE files SET mtime_ns=?, size=? WHERE work=? AND path=?",
                (stat.st_mtime_ns, stat.st_size, work, rel),
            )
            continue

        try:
            _apply(conn, work, kind, cid, rel, path)
            conn.execute(
                "INSERT OR REPLACE INTO files("
                "work, path, kind, chapter_id, mtime_ns, size, sha, extracted_at, error) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (work, rel, kind, cid, stat.st_mtime_ns, stat.st_size, sha, _now_iso(), ""),
            )
            stats["changed"] += 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # 单个文件解析失败不影响别的章节；把它的贡献清空并记下原因。
            try:
                item_id = (
                    path.name[: -len(".json")]
                    if kind in ("version", "candidate", "task") and path.name.endswith(".json")
                    else ""
                )
                _reset(conn, work, kind, cid, item_id)
            except sqlite3.Error:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO files("
                "work, path, kind, chapter_id, mtime_ns, size, sha, extracted_at, error) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    work, rel, kind, cid, stat.st_mtime_ns, stat.st_size, sha,
                    _now_iso(), f"{type(exc).__name__}: {exc}",
                ),
            )

    for rel, row in stored.items():
        if rel in seen:
            continue
        kind, cid = _classify(rel)
        if kind not in kinds:
            continue
        item_id = Path(rel).name[: -len(".json")] if rel.endswith(".json") else ""
        if kind != "unknown" and cid:
            _reset(conn, work, kind, cid, item_id)
        conn.execute("DELETE FROM files WHERE work=? AND path=?", (work, rel))
        stats["removed"] += 1

    stats["chapters"] = int(
        conn.execute("SELECT COUNT(*) AS n FROM chapters WHERE work=?", (work,)).fetchone()["n"]
    )
    return stats


# ── 查询 ────────────────────────────────────────────────────


def chapter_states(root: str | Path, work: str) -> dict[str, dict[str, Any]] | None:
    """读取某部作品的逐章派生状态。库不可用时返回 None（调用方退回现读文件）。"""
    try:
        conn = _connect(root)
        try:
            rows = conn.execute(
                "SELECT * FROM chapters WHERE work=?", (work,)
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    return {row["chapter_id"]: dict(row) for row in rows}


def task_history(
    root: str | Path,
    work: str,
    *,
    chapter_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]] | None:
    """从派生库查任务记录（按创建时间倒序）。库不可用时返回 None。"""
    try:
        conn = _connect(root)
        try:
            if chapter_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE work=? AND chapter_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (work, chapter_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE work=? ORDER BY created_at DESC LIMIT ?",
                    (work, limit),
                ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    return [dict(row) for row in rows]


def review_counters(root: str | Path, work: str, chapter_id: str) -> list[dict[str, Any]] | None:
    """每次审稿的条数与各状态计数。库不可用时返回 None。"""
    try:
        conn = _connect(root)
        try:
            rows = conn.execute(
                "SELECT * FROM review_runs WHERE work=? AND chapter_id=? "
                "ORDER BY created_at DESC",
                (work, chapter_id),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    return [dict(row) for row in rows]


def status(root: str | Path, *, sync_first: bool = False) -> dict[str, Any]:
    """库状态：路径、版本、各作品计数、最近一次同步。损坏时 `available=False`。"""
    root_path = Path(root)
    out: dict[str, Any] = {
        "path": str(meta_db_path(root_path)),
        "schema": META_SCHEMA_VERSION,
        "available": False,
        "works": [],
        "counts": {},
        "last_sync": _LAST_SYNC.get(str(root_path)),
        "error": "",
    }
    if sync_first:
        out["last_sync"] = sync(root_path)
    try:
        conn = _connect(root_path)
        try:
            _ensure_schema(conn, root_path)
            row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            out["schema"] = row["value"] if row else ""
            works = [
                dict(r) for r in conn.execute(
                    "SELECT work, COUNT(*) AS chapters FROM chapters GROUP BY work ORDER BY work"
                ).fetchall()
            ]
            out["works"] = works
            for table in ("files", "chapters", "review_runs", "artifacts", "tasks"):
                n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                out["counts"][table] = int(n)
            out["available"] = True
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def rebuild(root: str | Path) -> dict[str, Any]:
    """删除派生库后全量重建。只动 `.index/meta.db*`，不碰任何真源。"""
    path = meta_db_path(root)
    removed = []
    failed = []
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if target.exists():
            try:
                target.unlink()
                removed.append(target.name)
            except OSError as exc:
                failed.append(f"{target.name}: {exc}")
    result = sync(root, force=True)
    result["removed_files"] = removed
    if failed:
        result["ok"] = False
        result["error"] = "无法删除旧库：" + "; ".join(failed)
    return result
