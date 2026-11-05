"""创作工坊的全文检索层。

## 为什么用 SQLite FTS5

Python 标准库自带 `sqlite3`，本机 3.12 的 SQLite 已编译 FTS5。它把倒排、
相关性排序（`bm25`）、事务和并发读都做完了，而且索引是**单文件**，
删掉就能重建。自己写倒排要重新处理删除、排名与持久化，收益为零。

索引是派生数据，不是真源：`<数据根>/.index/search.db`，不进版本管理。

## 中文怎么分

FTS5 的 `unicode61` 会把一整段连续汉字当成一个词，中文因此基本搜不到。
这里在**写索引前**把汉字串切成「单字 + 相邻二元组」：

    暗礁碎屑 → 暗 礁 碎 屑 | 暗礁 礁碎 碎屑

查询也按同样规则切：多字词变成若干二元组（隐式 AND），单字走单字令牌。
不引入分词词典，也就不需要第三方依赖；代价是极短词召回偏大，靠 bm25 排序。

NFKC 统一全角/半角并小写化；**不做繁简转换**（那需要 OpenCC 一类依赖）。

## 增量

每个源文件在 `files` 表里记 `(mtime_ns, size, sha)`。每次检索前扫一遍文件：
指纹没变就只做 stat；变了才算 sha，sha 变了才重新抽取该文件并把旧文档删掉。
文件消失就删掉它的文档。解析失败只记在 `files.error`，不阻断检索。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

INDEX_DIR_NAME = ".index"
INDEX_DB_NAME = "search.db"
INDEX_SCHEMA_VERSION = "search-v2"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CONTEXT_CHARS = 30
MAX_HIT_CHARS = 20000

SOURCE_LABELS: dict[str, str] = {
    "chapter": "正文",
    "setting": "设定",
    "volume": "卷表",
    "brief": "章指令",
    "foreshadow": "伏笔",
    "notes": "要点",
    "review": "审稿",
}
SOURCE_TYPES = tuple(SOURCE_LABELS)

# 汉字：基本区、扩展 A、兼容区。日文假名也当汉字串处理，作者偶尔会用。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")
_CJK_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+|[a-z0-9]+"
)
_CHAPTER_RE = re.compile(r"^v(\d{3})-([cx])(\d{4})([a-z]?)$")
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")

_LOCK = threading.RLock()
_LAST_SYNC: dict[str, dict[str, Any]] = {}


# ── 路径与建库 ──────────────────────────────────────────────


def index_db_path(root: str | Path) -> Path:
    return Path(root) / INDEX_DIR_NAME / INDEX_DB_NAME


def _connect(root: str | Path) -> sqlite3.Connection:
    path = index_db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS docs_meta (
      doc_id TEXT PRIMARY KEY,
      fts_rowid INTEGER NOT NULL UNIQUE,
      work TEXT NOT NULL,
      source_type TEXT NOT NULL,
      ref TEXT NOT NULL DEFAULT '',
      title TEXT NOT NULL DEFAULT '',
      title_norm TEXT NOT NULL DEFAULT '',
      file TEXT NOT NULL DEFAULT '',
      anchor INTEGER NOT NULL DEFAULT 0,
      locator TEXT NOT NULL DEFAULT '{}',
      raw TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
      body,
      title,
      tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
      work TEXT NOT NULL,
      path TEXT NOT NULL,
      mtime_ns INTEGER NOT NULL,
      size INTEGER NOT NULL,
      sha TEXT NOT NULL DEFAULT '',
      doc_count INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',
      indexed_at TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (work, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_docs_meta_work_type ON docs_meta(work, source_type)",
    "CREATE INDEX IF NOT EXISTS idx_docs_meta_file ON docs_meta(work, file)",
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表；schema 版本变了就整库重建（索引没有迁移的必要）。"""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    version = None
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        version = row["value"] if row else None
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_fts'"
    ).fetchone()
    if version == INDEX_SCHEMA_VERSION and has_fts:
        return

    for table in ("docs_fts", "docs_meta", "files", "meta"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for sql in _SCHEMA_SQL:
        conn.execute(sql)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema', ?)", (INDEX_SCHEMA_VERSION,)
    )
    conn.commit()


# ── 文本归一与中文切分 ──────────────────────────────────────


def _fold(text: str) -> str:
    """NFKC（全角→半角）+ 小写。繁简不转换。"""
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def tokenize(text: str) -> str:
    """写索引用的词串：汉字出单字 + 相邻二元组，其余按字母数字切。"""
    folded = _fold(text)
    out: list[str] = []
    run: list[str] = []
    ascii_buf: list[str] = []

    def flush_cjk() -> None:
        if not run:
            return
        out.extend(run)
        out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run.clear()

    def flush_ascii() -> None:
        if ascii_buf:
            out.append("".join(ascii_buf))
            ascii_buf.clear()

    for ch in folded:
        if _is_cjk(ch):
            flush_ascii()
            run.append(ch)
        else:
            flush_cjk()
            if ch.isascii() and ch.isalnum():
                ascii_buf.append(ch)
            else:
                flush_ascii()
    flush_cjk()
    flush_ascii()
    return " ".join(out)


def query_tokens(text: str) -> list[str]:
    """查询用的词：多字汉字串切成相邻二元组，单字保留单字。"""
    folded = _fold(text)
    out: list[str] = []
    run: list[str] = []
    ascii_buf: list[str] = []

    def flush_cjk() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run.clear()

    def flush_ascii() -> None:
        if ascii_buf:
            out.append("".join(ascii_buf))
            ascii_buf.clear()

    for ch in folded:
        if _is_cjk(ch):
            flush_ascii()
            run.append(ch)
        else:
            flush_cjk()
            if ch.isascii() and ch.isalnum():
                ascii_buf.append(ch)
            else:
                flush_ascii()
    flush_cjk()
    flush_ascii()
    return out


def display_terms(text: str) -> list[str]:
    """片段高亮用：保留完整的中文词与英文词，优先长词。"""
    folded = _fold(text)
    terms = _CJK_RUN_RE.findall(folded)
    return sorted(set(terms), key=lambda x: (-len(x), x))


def _match_expression(tokens: Iterable[str]) -> str:
    # 每个词都加引号，避免用户输入里的 FTS5 语法符号被当成操作符。
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens if t)


# ── 抽取：文件 → 文档 ──────────────────────────────────────


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "；".join(part for part in (_flatten(v) for v in value) if part)
    if isinstance(value, dict):
        return "；".join(
            f"{k}：{_flatten(v)}" for k, v in value.items() if _flatten(v)
        )
    return str(value).strip()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(_read_text(path)) or {}
    return data if isinstance(data, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(_read_text(path))
    return data if isinstance(data, dict) else {}


def _doc(
    *,
    doc_id: str,
    source_type: str,
    ref: str,
    title: str,
    body: str,
    anchor: int = 0,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "source_type": source_type,
        "ref": ref,
        "title": title,
        "body": body,
        "anchor": int(anchor or 0),
        "locator": locator or {},
    }


def _setting_items(raw: Any) -> list[dict[str, Any]]:
    """把 section 统一成「一条一个 dict」，字符串条目也当一条。"""
    items: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        items.append(raw)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                items.append(item)
            elif isinstance(item, str) and item.strip():
                items.append({"name": item.strip()})
    elif isinstance(raw, str) and raw.strip():
        items.append({"name": raw.strip()})
    return items


def _setting_doc(
    work: str,
    docs: list[dict[str, Any]],
    *,
    section: str,
    index: int,
    label: str,
    name: str,
    body: str,
) -> None:
    if not body.strip():
        return
    docs.append(
        _doc(
            doc_id=f"{work}::setting::{section}:{index}",
            source_type="setting",
            ref=f"{label}·{name}",
            title=f"设定 · {label} · {name}",
            body=body,
            locator={
                "kind": "setting",
                "section": section,
                "index": index,
                "name": name,
            },
        )
    )


def _extract_setting(work: str, path: Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    docs: list[dict[str, Any]] = []

    overview = "；".join(
        part
        for part in (
            _flatten(data.get("genres") or data.get("genre")),
            _flatten(data.get("logline")),
            _flatten(data.get("core_motive")),
            _flatten(data.get("style")),
        )
        if part
    )
    if overview:
        _setting_doc(
            work,
            docs,
            section="overview",
            index=0,
            label="总览",
            name="总览",
            body=overview,
        )

    # world / power 这类 section 是分组结构：里面还有 rules/tiers 列表，
    # 列表里每一条要单独成一条文档，否则「一条命中」会埋在一大段拼接文本里。
    world = data.get("world") if isinstance(data.get("world"), dict) else {}
    for key, label in (("era", "时代"), ("places", "地点"), ("rules", "世界规则")):
        for i, item in enumerate(_setting_items(world.get(key))):
            content = "；".join(
                p for p in (_flatten(item.get("name")), _flatten(item.get("desc")),
                            _flatten(item.get("note"))) if p
            )
            has_detail = any(item.get(k) for k in ("desc", "note"))
            if key == "rules" and not has_detail:
                # 规则列表里常直接写一整句话：标题用「第N条」，正文才是规则本身，
                # 否则标题会命中正文里的任何人名，把真正的人物条目挤下去。
                name = f"第{i + 1}条"
                body = content or name
            else:
                name = str(item.get("name") or "").strip() or label
                body = content or name
            _setting_doc(
                work, docs,
                section=f"world:{key}", index=i, label=f"世界·{label}", name=name,
                body=body,
            )

    power = data.get("power") if isinstance(data.get("power"), dict) else {}
    system = _flatten(power.get("system"))
    if system:
        _setting_doc(
            work, docs, section="power:system", index=0,
            label="力量体系", name="体系", body=system,
        )
    for i, item in enumerate(_setting_items(power.get("tiers"))):
        content = "；".join(
            p for p in (_flatten(item.get("name")), _flatten(item.get("desc")),
                        _flatten(item.get("note"))) if p
        )
        has_detail = any(item.get(k) for k in ("desc", "note"))
        if not has_detail:
            full = str(item.get("name") or "").strip()
            name = full.split("：")[0].split(":")[0][:20] or f"第{i + 1}境"
            body = full or name
        else:
            name = str(item.get("name") or "").strip() or f"第{i + 1}境"
            body = content or name
        _setting_doc(
            work, docs, section="power:tiers", index=i,
            label="境界", name=name, body=body,
        )

    sections = (
        ("factions", "势力", ("name", "stance", "leader")),
        ("characters", "人物", ("name", "role", "identity", "personality",
                                "motive", "arc", "faction", "relations", "abilities")),
        ("terms", "术语", ("name", "desc", "definition", "note")),
        ("themes", "主题", ("name", "desc", "note")),
        ("abilities", "能力", ("name", "effect", "tier", "holder")),
    )
    for section, label, keys in sections:
        for i, item in enumerate(_setting_items(data.get(section))):
            name = str(item.get("name") or "").strip() or f"第{i + 1}条"
            # 名字本身也放进正文，命中设定条目时片段才有内容可展示。
            parts = [name]
            for key in keys:
                if key == "name":
                    continue
                text = _flatten(item.get(key))
                if text:
                    parts.append(text)
            _setting_doc(
                work, docs, section=section, index=i, label=label, name=name,
                body="；".join(parts),
            )

    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    style_body = "；".join(
        p for p in (
            _flatten(style.get("perspective")),
            _flatten(style.get("tone")),
            _flatten(style.get("taboo")),
        ) if p
    )
    if style_body:
        _setting_doc(
            work, docs, section="style", index=0, label="风格", name="风格",
            body=style_body,
        )
    return docs


_BRIEF_KEYS = (
    "one_line",
    "core_plot",
    "timeline",
    "narrative",
    "settings",
    "carry_over",
    "must_not",
    "prev_tail_chars",
    "future_blueprint_count",
)


def _extract_brief(work: str, path: Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    chapter_id = str(data.get("chapter_id") or path.stem).strip()
    chapter_no = data.get("chapter_no")
    title = str(data.get("title") or "").strip()
    parts = [title] if title else []
    for key in _BRIEF_KEYS:
        if key == "prev_tail_chars" or key == "future_blueprint_count":
            continue
        text = _flatten(data.get(key))
        if text:
            parts.append(text)
    body = "；".join(parts)
    docs = []
    if body:
        docs.append(
            _doc(
                doc_id=f"{work}::brief::{chapter_id}",
                source_type="brief",
                ref=chapter_id,
                title=f"章指令 · 第{chapter_no or '·'}章 {title}".strip(),
                body=body,
                locator={
                    "kind": "brief",
                    "chapter_id": chapter_id,
                    "chapter_no": chapter_no,
                },
            )
        )
    for i, item in enumerate(data.get("foreshadow") or []):
        if not isinstance(item, dict):
            continue
        desc = str(item.get("desc") or "").strip()
        if not desc:
            continue
        fid = str(item.get("id") or f"{chapter_id}-f{i + 1}").strip()
        action = str(item.get("action") or "").strip()
        docs.append(
            _doc(
                doc_id=f"{work}::foreshadow::{chapter_id}:{fid}",
                source_type="foreshadow",
                ref=fid,
                title=f"伏笔 · {fid}" + (f" · {action}" if action else ""),
                body=desc,
                locator={
                    "kind": "foreshadow",
                    "chapter_id": chapter_id,
                    "foreshadow_id": fid,
                    "action": action,
                },
            )
        )
    return docs


def _extract_volume(work: str, path: Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    vol = data.get("vol") if isinstance(data.get("vol"), dict) else data
    vol_no = int(vol.get("vol") or 1)
    docs: list[dict[str, Any]] = []
    head_parts = [
        _flatten(vol.get("title")),
        _flatten(vol.get("core_conflict")),
        _flatten(vol.get("goal")),
        _flatten(vol.get("closing")),
        _flatten(vol.get("era")),
    ]
    head_body = "；".join(p for p in head_parts if p)
    if head_body:
        docs.append(
            _doc(
                doc_id=f"{work}::volume::{vol_no}:head",
                source_type="volume",
                ref=f"第{vol_no}卷",
                title=f"卷表 · 第{vol_no}卷 {_flatten(vol.get('title'))}".strip(),
                body=head_body,
                locator={"kind": "volume", "vol": vol_no, "chapter_no": None},
            )
        )
    for entry in vol.get("chapters") or []:
        if not isinstance(entry, dict):
            continue
        chapter_no = entry.get("chapter_no")
        title = str(entry.get("title") or "").strip()
        body = "；".join(
            p
            for p in (
                title,
                _flatten(entry.get("gist")),
                _flatten(entry.get("foreshadow")),
                _flatten(entry.get("characters")),
            )
            if p
        )
        if not body:
            continue
        docs.append(
            _doc(
                doc_id=f"{work}::volume::{vol_no}:{chapter_no}",
                source_type="volume",
                ref=f"第{vol_no}卷·第{chapter_no}章",
                title=f"卷表 · 第{chapter_no}章 {title}".strip(),
                body=body,
                locator={
                    "kind": "volume",
                    "vol": vol_no,
                    "chapter_no": chapter_no,
                },
            )
        )
    return docs


def _extract_chapter(work: str, path: Path) -> list[dict[str, Any]]:
    text = _read_text(path)
    if not text.strip():
        return []
    chapter_id = path.stem
    match = _CHAPTER_RE.match(chapter_id)
    if match:
        vol_no = int(match.group(1))
        chapter_no = int(match.group(3))
        label = f"第{chapter_no}章"
        title = f"正文 · 第{vol_no}卷 {label}"
    else:
        title = f"正文 · {chapter_id}"
    return [
        _doc(
            doc_id=f"{work}::chapter::{chapter_id}",
            source_type="chapter",
            ref=chapter_id,
            title=title,
            body=text,
            locator={"kind": "chapter", "chapter_id": chapter_id},
        )
    ]


def _extract_notes(work: str, path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    chapter_id = str(data.get("chapter_id") or path.name.split(".notes.json")[0])
    docs = []
    for note in data.get("notes") or []:
        if not isinstance(note, dict):
            continue
        # 「已确认」按代码里的定义：accepted / edited，且没有被取代。
        if str(note.get("status")) not in ("accepted", "edited"):
            continue
        if note.get("superseded_by"):
            continue
        text = str(note.get("text") or "").strip()
        evidence = str(note.get("evidence") or "").strip()
        body = "；".join(p for p in (text, evidence) if p)
        if not body:
            continue
        note_id = str(note.get("id") or "")
        ref = str(note.get("ref") or "").strip()
        docs.append(
            _doc(
                doc_id=f"{work}::notes::{chapter_id}:{note_id}",
                source_type="notes",
                ref=f"{chapter_id}·{ref}" if ref else chapter_id,
                title=f"要点 · 第{note.get('chapter_no') or '·'}章 · {ref or note_id}",
                body=body,
                locator={
                    "kind": "notes",
                    "chapter_id": chapter_id,
                    "note_id": note_id,
                    "chapter_no": note.get("chapter_no"),
                    "note_ref": ref,
                },
            )
        )
    return docs


def _extract_review(work: str, path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    chapter_id = str(data.get("chapter_id") or path.name.split(".review.json")[0])
    docs = []
    for review in data.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        review_id = str(review.get("review_id") or "")
        for item in review.get("items") or []:
            if not isinstance(item, dict):
                continue
            evidence = str(item.get("evidence") or "").strip()
            problem = str(item.get("problem") or "").strip()
            suggestion = str(item.get("suggestion") or "").strip()
            body = "；".join(p for p in (evidence, problem, suggestion) if p)
            if not body:
                continue
            finding_id = str(item.get("id") or "")
            kind = str(item.get("type") or "")
            severity = str(item.get("severity") or "")
            docs.append(
                _doc(
                    doc_id=f"{work}::review::{chapter_id}:{review_id}:{finding_id}",
                    source_type="review",
                    ref=f"{chapter_id}·{finding_id}",
                    title=f"审稿 · 第{item.get('paragraph', '·')}段 · {kind} · {severity}".strip(),
                    body=body,
                    anchor=int(item.get("start") or 0),
                    locator={
                        "kind": "review",
                        "chapter_id": chapter_id,
                        "review_id": review_id,
                        "finding_id": finding_id,
                        "paragraph": item.get("paragraph"),
                        "start": item.get("start"),
                        "end": item.get("end"),
                        "type": kind,
                        "severity": severity,
                        "status": item.get("status"),
                    },
                )
            )
    return docs


_EXTRACTORS = {
    "setting": _extract_setting,
    "brief": _extract_brief,
    "volume": _extract_volume,
    "chapter": _extract_chapter,
    "notes": _extract_notes,
    "review": _extract_review,
}


# ── 扫描文件 ────────────────────────────────────────────────


def _work_dirs(root: Path) -> list[Path]:
    workspaces = root / "workspaces"
    if not workspaces.exists():
        return []
    out = []
    for entry in sorted(workspaces.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        has_setting = (entry / "60-setting" / "setting.yaml").exists()
        has_draft = (entry / "90-draft").exists()
        if has_setting or has_draft:
            out.append(entry)
    return out


def _discover(work_dir: Path) -> dict[str, str]:
    """返回 {相对路径: 抽取器名}。"""
    found: dict[str, str] = {}

    def add(path: Path, kind: str) -> None:
        found[path.relative_to(work_dir).as_posix()] = kind

    setting = work_dir / "60-setting" / "setting.yaml"
    if setting.exists():
        add(setting, "setting")
    for path in sorted((work_dir / "70-volume").glob("*.yaml")) if (
        work_dir / "70-volume"
    ).exists() else []:
        add(path, "volume")
    for path in sorted((work_dir / "80-brief").glob("*.yaml")) if (
        work_dir / "80-brief"
    ).exists() else []:
        add(path, "brief")
    draft = work_dir / "90-draft"
    if draft.exists():
        for path in sorted(draft.glob("*.md")):
            if _CHAPTER_RE.match(path.stem):
                add(path, "chapter")
        for path in sorted(draft.glob("*.notes.json")):
            add(path, "notes")
        for path in sorted(draft.glob("*.review.json")):
            add(path, "review")
    return found


# ── 写入与删除 ──────────────────────────────────────────────


def _delete_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    row = conn.execute(
        "SELECT fts_rowid FROM docs_meta WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row["fts_rowid"],))
        conn.execute("DELETE FROM docs_meta WHERE doc_id = ?", (doc_id,))


def _delete_file_docs(conn: sqlite3.Connection, work: str, file: str) -> None:
    rows = conn.execute(
        "SELECT doc_id, fts_rowid FROM docs_meta WHERE work = ? AND file = ?",
        (work, file),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row["fts_rowid"],))
    conn.execute("DELETE FROM docs_meta WHERE work = ? AND file = ?", (work, file))


def _insert_docs(
    conn: sqlite3.Connection, work: str, file: str, docs: list[dict[str, Any]]
) -> int:
    count = 0
    for item in docs:
        body = str(item.get("body") or "")
        if not body.strip():
            continue
        doc_id = str(item["doc_id"])
        _delete_doc(conn, doc_id)  # 同一 doc_id 换了文件时先让位
        rowid = conn.execute(
            "SELECT COALESCE(MAX(fts_rowid), 0) + 1 FROM docs_meta"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO docs_meta(
                 doc_id, fts_rowid, work, source_type, ref, title,
                 title_norm, file, anchor, locator, raw
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc_id,
                rowid,
                work,
                str(item["source_type"]),
                str(item.get("ref") or ""),
                str(item.get("title") or ""),
                _fold(f"{item.get('title') or ''} {item.get('ref') or ''}"),
                file,
                int(item.get("anchor") or 0),
                json.dumps(item.get("locator") or {}, ensure_ascii=False),
                body,
            ),
        )
        # ref 也进标题索引：章节 id、伏笔编号这类「定位词」本身往往不写在正文里。
        conn.execute(
            "INSERT INTO docs_fts(rowid, body, title) VALUES (?, ?, ?)",
            (
                rowid,
                tokenize(body),
                tokenize(f"{item.get('title') or ''} {item.get('ref') or ''}"),
            ),
        )
        count += 1
    return count


def _stats(conn: sqlite3.Connection) -> dict[str, Any]:
    by_type = {
        row["source_type"]: int(row["n"])
        for row in conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM docs_meta GROUP BY source_type"
        )
    }
    return {
        "docs": int(conn.execute("SELECT COUNT(*) AS n FROM docs_meta").fetchone()["n"]),
        "works": int(
            conn.execute("SELECT COUNT(DISTINCT work) AS n FROM docs_meta").fetchone()["n"]
        ),
        "by_type": by_type,
    }


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sync(root: str | Path, *, force: bool = False) -> dict[str, Any]:
    """把磁盘上的改动同步进索引。返回本次同步的统计。"""
    root = Path(root)
    started = time.perf_counter()
    with _LOCK:
        conn = _connect(root)
        try:
            _ensure_schema(conn)
            candidates: dict[tuple[str, str], tuple[Path, Path, str]] = {}
            for work_dir in _work_dirs(root):
                for rel, kind in _discover(work_dir).items():
                    candidates[(work_dir.name, rel)] = (work_dir, work_dir / rel, kind)

            existing = {
                (row["work"], row["path"]): dict(row)
                for row in conn.execute("SELECT * FROM files")
            }
            changed = 0
            docs_written = 0
            errors: list[str] = []
            for (work, rel), (work_dir, path, kind) in candidates.items():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                row = existing.get((work, rel))
                unchanged = (
                    row is not None
                    and row["mtime_ns"] == stat.st_mtime_ns
                    and row["size"] == stat.st_size
                )
                if unchanged and not force:
                    continue
                try:
                    text = _read_text(path)
                except OSError as exc:
                    errors.append(f"{work}/{rel}: {exc}")
                    continue
                sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if row is not None and row["sha"] == sha and not force:
                    conn.execute(
                        "UPDATE files SET mtime_ns = ?, size = ? WHERE work = ? AND path = ?",
                        (stat.st_mtime_ns, stat.st_size, work, rel),
                    )
                    continue

                _delete_file_docs(conn, work, rel)
                error = ""
                try:
                    docs = _EXTRACTORS[kind](work, path)
                except Exception as exc:  # 单个坏文件不能拖垮整次检索
                    docs = []
                    error = f"{type(exc).__name__}: {exc}"[:200]
                    errors.append(f"{work}/{rel}: {error}")
                written = _insert_docs(conn, work, rel, docs)
                docs_written += written
                conn.execute(
                    """INSERT INTO files(
                         work, path, mtime_ns, size, sha, doc_count, error, indexed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(work, path) DO UPDATE SET
                         mtime_ns = excluded.mtime_ns,
                         size = excluded.size,
                         sha = excluded.sha,
                         doc_count = excluded.doc_count,
                         error = excluded.error,
                         indexed_at = excluded.indexed_at""",
                    (
                        work,
                        rel,
                        stat.st_mtime_ns,
                        stat.st_size,
                        sha,
                        written,
                        error,
                        _now(),
                    ),
                )
                changed += 1

            removed = 0
            for (work, rel), row in existing.items():
                if (work, rel) in candidates:
                    continue
                _delete_file_docs(conn, work, rel)
                conn.execute(
                    "DELETE FROM files WHERE work = ? AND path = ?", (work, rel)
                )
                removed += 1

            conn.commit()
            stats = _stats(conn)
            files = int(conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"])
        finally:
            conn.close()

    elapsed = time.perf_counter() - started
    result = {
        **stats,
        "files": files,
        "changed_files": changed,
        "removed_files": removed,
        "written_docs": docs_written,
        "errors": errors[:20],
        "elapsed_ms": round(elapsed * 1000, 2),
        "at": _now(),
        "ready": True,
    }
    _LAST_SYNC[str(root)] = result
    return result


# ── 状态、查询、片段 ────────────────────────────────────────


def status(root: str | Path, *, sync_first: bool = False) -> dict[str, Any]:
    """索引状态。默认只读，不建库也不扫描。"""
    root = Path(root)
    if sync_first:
        result = sync(root)
        return {
            **result,
            "ready": True,
            "db_path": str(index_db_path(root)),
        }
    path = index_db_path(root)
    if not path.exists():
        return {
            "ready": False,
            "docs": 0,
            "works": 0,
            "by_type": {},
            "files": 0,
            "errors": [],
            "elapsed_ms": 0.0,
            "at": "",
            "db_path": str(path),
        }
    with _LOCK:
        try:
            conn = _connect(root)
        except sqlite3.Error as exc:
            return {
                "ready": False,
                "docs": 0,
                "works": 0,
                "by_type": {},
                "files": 0,
                "errors": [str(exc)],
                "elapsed_ms": 0.0,
                "at": "",
                "db_path": str(path),
            }
        try:
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            version = (
                conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if has_meta
                else None
            )
            ready = bool(version) and version["value"] == INDEX_SCHEMA_VERSION
            if not ready:
                return {
                    "ready": False,
                    "docs": 0,
                    "works": 0,
                    "by_type": {},
                    "files": 0,
                    "errors": [],
                    "elapsed_ms": 0.0,
                    "at": "",
                    "db_path": str(path),
                }
            stats = _stats(conn)
            files = int(
                conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
            )
            errors = [
                f"{row['work']}/{row['path']}: {row['error']}"
                for row in conn.execute(
                    "SELECT work, path, error FROM files WHERE error != '' LIMIT 20"
                )
            ]
            last = _LAST_SYNC.get(str(root)) or {}
            return {
                **stats,
                "files": files,
                "errors": errors,
                "elapsed_ms": last.get("elapsed_ms", 0.0),
                "at": last.get("at", ""),
                "ready": True,
                "db_path": str(path),
            }
        finally:
            conn.close()


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """逐字 NFKC+小写，同时保留「归一后的第 i 个字来自原文第几个字」。"""
    out: list[str] = []
    mapping: list[int] = []
    for index, ch in enumerate(text):
        folded = unicodedata.normalize("NFKC", ch).lower()
        for c in folded:
            out.append(c)
            mapping.append(index)
    return "".join(out), mapping


def _find_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    folded, mapping = _fold_with_map(text)
    if not folded or not mapping:
        return []
    spans: list[tuple[int, int]] = []
    seen: set[str] = set()
    for term in terms:
        folded_term = unicodedata.normalize("NFKC", term).lower()
        if not folded_term or folded_term in seen:
            continue
        seen.add(folded_term)
        start = 0
        while True:
            pos = folded.find(folded_term, start)
            if pos < 0:
                break
            first = mapping[pos]
            last = mapping[min(pos + len(folded_term) - 1, len(mapping) - 1)]
            spans.append((first, last + 1))
            start = pos + max(1, len(folded_term))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clean_piece(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _snippet(raw: str, terms: list[str]) -> dict[str, Any]:
    spans = _find_spans(raw, terms)
    if not spans:
        # 只命中标题/编号时正文里找不到词，仍给一段开头，避免结果看起来是空的。
        head = _clean_piece(raw[:100])
        return {
            "before": "",
            "match": "",
            "after": head,
            "prefix": False,
            "suffix": len(raw) > 100,
            "start": 0,
            "end": 0,
            "length": 0,
        }
    start, end = spans[0]
    # 命中太长时只展示前 120 字，免得一条结果占满屏幕。
    shown_end = min(end, start + 120)
    left = max(0, start - CONTEXT_CHARS)
    right = min(len(raw), shown_end + CONTEXT_CHARS)
    return {
        "before": _clean_piece(raw[left:start]),
        "match": raw[start:shown_end],
        "after": _clean_piece(raw[shown_end:right]),
        "prefix": left > 0,
        "suffix": right < len(raw),
        "start": start,
        "end": shown_end,
        "length": shown_end - start,
    }


def _paragraph_index(raw: str, offset: int) -> int:
    index = 0
    for match in _PARAGRAPH_BREAK_RE.finditer(raw):
        if match.start() >= offset:
            break
        index += 1
    return index


def _parse_types(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for part in str(raw).split(","):
        name = part.strip()
        if name in SOURCE_TYPES and name not in out:
            out.append(name)
    return out


def query(
    root: str | Path,
    text: str,
    *,
    source_types: str | list[str] | None = None,
    work: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """检索。每次先做一轮增量同步，所以改完文件不用重启服务。"""
    root = Path(root)
    started = time.perf_counter()
    text = str(text or "").strip()
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    types = _parse_types(",".join(source_types)) if isinstance(source_types, list) else _parse_types(
        source_types or ""
    )
    work = str(work or "").strip()

    index_info = sync(root)
    tokens = query_tokens(text)
    terms = display_terms(text)
    result: dict[str, Any] = {
        "query": text,
        "total": 0,
        "limit": limit,
        "offset": offset,
        "items": [],
        "index": {
            "docs": index_info.get("docs", 0),
            "works": index_info.get("works", 0),
            "by_type": index_info.get("by_type", {}),
            "ready": True,
            "sync_ms": index_info.get("elapsed_ms", 0.0),
        },
        "took_ms": 0.0,
    }
    if not text or not tokens:
        result["took_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    where = ["docs_fts MATCH ?"]
    params: list[Any] = [_match_expression(tokens)]
    if work:
        where.append("m.work = ?")
        params.append(work)
    if types:
        where.append(f"m.source_type IN ({','.join('?' for _ in types)})")
        params.extend(types)
    where_sql = " AND ".join(where)

    with _LOCK:
        conn = _connect(root)
        try:
            total = int(
                conn.execute(
                    f"""SELECT COUNT(*) AS n
                        FROM docs_fts JOIN docs_meta m ON m.fts_rowid = docs_fts.rowid
                        WHERE {where_sql}""",
                    params,
                ).fetchone()["n"]
            )
            rows = conn.execute(
                f"""SELECT m.doc_id, m.work, m.source_type, m.ref, m.title,
                           m.anchor, m.locator, m.raw,
                           bm25(docs_fts, 4.0, 1.0) AS rank
                    FROM docs_fts JOIN docs_meta m ON m.fts_rowid = docs_fts.rowid
                    WHERE {where_sql}
                    ORDER BY (instr(m.title_norm, ?) > 0) DESC, rank, m.work, m.doc_id
                    LIMIT ? OFFSET ?""",
                [*params, _fold(text), limit, offset],
            ).fetchall()
        finally:
            conn.close()

    items = []
    for row in rows:
        raw = str(row["raw"] or "")
        snippet = _snippet(raw, terms or tokens)
        try:
            locator = json.loads(row["locator"] or "{}")
        except ValueError:
            locator = {}
        items.append(
            {
                "doc_id": row["doc_id"],
                "work": row["work"],
                "source_type": row["source_type"],
                "source_label": SOURCE_LABELS.get(row["source_type"], row["source_type"]),
                "ref": row["ref"],
                "title": row["title"],
                "anchor": row["anchor"],
                "locator": locator,
                "snippet": snippet,
                "offset": snippet["start"] if snippet else 0,
                "length": snippet["length"] if snippet else 0,
                "paragraph": _paragraph_index(raw, snippet["start"]) if snippet else 0,
                "score": round(-float(row["rank"]), 8),
            }
        )
    result["total"] = total
    result["items"] = items
    result["took_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def load_doc(root: str | Path, doc_id: str) -> dict[str, Any] | None:
    """命中页读取：单条文档的原始文本与定位信息。"""
    root = Path(root)
    path = index_db_path(root)
    if not path.exists():
        return None
    with _LOCK:
        conn = _connect(root)
        try:
            row = conn.execute(
                """SELECT doc_id, work, source_type, ref, title, anchor,
                          locator, raw
                   FROM docs_meta WHERE doc_id = ?""",
                (str(doc_id),),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        locator = json.loads(row["locator"] or "{}")
    except ValueError:
        locator = {}
    return {
        "doc_id": row["doc_id"],
        "work": row["work"],
        "source_type": row["source_type"],
        "source_label": SOURCE_LABELS.get(row["source_type"], row["source_type"]),
        "ref": row["ref"],
        "title": row["title"],
        "anchor": row["anchor"],
        "locator": locator,
        "raw": row["raw"],
    }
