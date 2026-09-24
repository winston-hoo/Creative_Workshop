"""工作台的业务层：把已有的 workshop 模块包成 HTTP 接口能用的形状。

三条原则：
  1. **文件系统是唯一真相源**。列表与详情都从磁盘上的 manifest.json 现读，
     不引入数据库、不做内存缓存。脚本看到的和界面看到的一定是同一份数据。
  2. **返回结构化数据，不返回渲染好的文本**。命令行那套 `format_preview` 是给人看的，
     接口要把同一份信息拆成字段，让前端自己决定怎么呈现。
  3. **不泄露正文**。预览与详情只给统计、章节标题与异常片段，绝不返回整章文本。

关于后台任务：界面上的「开始标注」是长任务，用一个进程内线程跑，
进度写进状态文件。**不用 Celery/Redis**——本地单用户场景引入它们是过度设计，
代价是「换个环境就起不来」，换来的收益一个都没有。
"""

from __future__ import annotations

import json
import shutil
import secrets as py_secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workshop.annotate import load_annotation
from workshop.ingest import Chapter, ingest, save_ingest
from workshop.outline import DEFAULT_BLOCK_SIZE

# 上传的临时文件放在这里。预览时上传、确认时才真正导入，
# 所以文件必须活过一次请求的间隔。
UPLOAD_ROOT_NAME = ".tmp/uploads"


@dataclass
class Paths:
    """工作台用到的所有目录。集中一处，避免到处拼路径。"""

    root: Path

    @property
    def workspaces(self) -> Path:
        return self.root / "workspaces"

    @property
    def uploads(self) -> Path:
        return self.root / UPLOAD_ROOT_NAME

    def work_dir(self, name: str) -> Path:
        return self.workspaces / name

    def ingest_dir(self, name: str) -> Path:
        return self.work_dir(name) / "00-ingest"

    def annotations_dir(self, name: str) -> Path:
        return self.work_dir(name) / "10-annotations"

    def ledger_path(self, name: str) -> Path:
        return self.work_dir(name) / "20-kb" / "k2-material" / "foreshadow-ledger.json"

    def work_config(self, name: str) -> Path:
        return self.work_dir(name) / "work.yaml"

    def reports_dir(self, name: str) -> Path:
        return self.work_dir(name) / "30-reports"


# ── 书架 ────────────────────────────────────────────────────


# 下划线开头的目录不算作品。约定用途：
#   _archive  从书架移出的作品
#   _试用记录  验证/试验留下的产物
# 工作台刚装好时书架必须是空的——预置数据会让人分不清「这是产品自带的」还是「我自己导入的」。
NON_WORK_PREFIXES = ("_", ".")
ARCHIVE_DIR_NAME = "_archive"


def list_works(paths: Paths) -> list[dict[str, Any]]:
    """列出所有已导入的作品。数据全部来自各自工作区里的 manifest。"""
    out: list[dict[str, Any]] = []
    if not paths.workspaces.exists():
        return out

    for entry in sorted(paths.workspaces.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(NON_WORK_PREFIXES):
            continue
        manifest_path = entry / "00-ingest" / "manifest.json"
        if not manifest_path.exists():
            out.append(
                {
                    "name": entry.name,
                    "imported": False,
                    "chapters": 0,
                    "chars": 0,
                    "segments": 0,
                    "anomalies": 0,
                    "annotated": 0,
                }
            )
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue

        annotations_dir = paths.annotations_dir(entry.name)
        annotated = (
            len([p for p in annotations_dir.glob("*.json")]) if annotations_dir.exists() else 0
        )
        ledger_path = paths.ledger_path(entry.name)
        open_foreshadows = 0
        if ledger_path.exists():
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
                open_foreshadows = int((ledger.get("statistics") or {}).get("open") or 0)
            except (ValueError, OSError):
                pass

        out.append(
            {
                "name": manifest.get("work") or entry.name,
                "dir_name": entry.name,
                "imported": True,
                "chapters": len(manifest.get("chapters") or []),
                "chars": (manifest.get("source") or {}).get("total_chars_raw") or 0,
                "segments": len(manifest.get("volumes") or []),
                "anomalies": len(manifest.get("anomalies") or []),
                "integrity_passed": bool((manifest.get("integrity") or {}).get("passed")),
                "annotated": annotated,
                "open_foreshadows": open_foreshadows,
                "updated_at": _mtime(manifest_path),
            }
        )
    return out


def work_detail(paths: Paths, name: str) -> dict[str, Any] | None:
    manifest_path = paths.ingest_dir(name) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None

    source = manifest.get("source") or {}
    chapters = manifest.get("chapters") or []
    char_counts = [c.get("char_count") or 0 for c in chapters]

    return {
        "name": manifest.get("work") or name,
        "dir_name": name,
        "source": {
            "file": source.get("file"),
            "bytes": source.get("bytes"),
            "encoding": source.get("detected_encoding"),
            "chars": source.get("total_chars_raw"),
            "lines": source.get("line_count"),
        },
        "statistics": {
            "chapters": len(chapters),
            "segments": len(manifest.get("volumes") or []),
            "unnumbered": sum(1 for c in chapters if c.get("chapter_no") is None),
            "chars_avg": round(sum(char_counts) / len(char_counts)) if char_counts else 0,
            "chars_min": min(char_counts) if char_counts else 0,
            "chars_max": max(char_counts) if char_counts else 0,
            "anomalies": len(manifest.get("anomalies") or []),
            "unmatched_title_like": len(manifest.get("unmatched_title_like") or []),
            "suspected_ads": len(manifest.get("suspected_ads") or {}),
            "author_notes": len(manifest.get("author_notes") or []),
        },
        "volumes": manifest.get("volumes") or [],
        "integrity": manifest.get("integrity") or {},
        "counts": _anomaly_counts(manifest.get("anomalies") or []),
        "annotated": len(list(paths.annotations_dir(name).glob("*.json")))
        if paths.annotations_dir(name).exists()
        else 0,
    }


def _annotation_state(record: dict[str, Any] | None) -> str:
    """这一章的标注状态。四种都要能区分——混成「已标注/未标注」两种，
    就没法看出哪些是需要重跑的。"""
    if not record:
        return "未标注"
    if record.get("errors"):
        return "失败"
    if record.get("status") == "needs_review":
        return "待复核"
    return "已标注"


def list_chapters(
    paths: Paths, name: str, *, offset: int = 0, limit: int = 50, only: str = "all"
) -> dict[str, Any]:
    manifest_path = paths.ingest_dir(name) / "manifest.json"
    if not manifest_path.exists():
        return {"total": 0, "items": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {"total": 0, "items": []}

    annotations_dir = paths.annotations_dir(name)
    annotated_ids = {p.stem for p in annotations_dir.glob("*.json")} if annotations_dir.exists() else set()

    chapters = manifest.get("chapters") or []
    if only == "unnumbered":
        chapters = [c for c in chapters if c.get("chapter_no") is None]
    elif only == "repaired":
        chapters = [c for c in chapters if c.get("status") == "repaired"]
    elif only == "annotated":
        chapters = [c for c in chapters if c.get("id") in annotated_ids]
    elif only == "unannotated":
        chapters = [c for c in chapters if c.get("id") not in annotated_ids]
    elif only == "review":
        # 待复核要读文件才能判断，只对候选集做一次过滤
        chapters = [
            c
            for c in chapters
            if c.get("id") in annotated_ids
            and (load_annotation(annotations_dir / f"{c['id']}.json") or {}).get("status")
            == "needs_review"
        ]

    total = len(chapters)
    window = chapters[offset : offset + limit]
    items = []
    for c in window:
        cid = c.get("id")
        record = load_annotation(annotations_dir / f"{cid}.json") if cid in annotated_ids else None
        items.append(
            {
                "id": cid,
                "chapter_no": c.get("chapter_no"),
                "title": c.get("title") or "",
                "chars": c.get("char_count") or 0,
                "status": c.get("status") or "ok",
                "line_no": c.get("line_no"),
                # 无编号章节要显式标出来。漏了它前端会把章号显示成空白，
                # 看起来像数据缺了，而不是「原文本来就没编号」。
                "unnumbered": c.get("chapter_no") is None,
                "annot_state": _annotation_state(record),
                "has_summary": bool((record or {}).get("fields", {}).get("chapter_summary")),
            }
        )
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "annotated_total": len(annotated_ids),
        "items": items,
    }


def chapter_detail(
    paths: Paths, name: str, chapter_id: str, *, raw: bool = False
) -> dict[str, Any] | None:
    """读一章的完整内容。

    ## 关于「不泄露正文」这条约束

    那条约束是针对**导入预览**的：文件还没确认归属时，接口只给统计与标题，
    不返回整章文本。作品一旦导入完成，它就是用户自己的本地数据，
    读正文是这台机器上最正当的用法——所以这里该给就给。

    ## 路径安全

    章节 id 会拼进路径，所以**不从入参拼**：先在 manifest 里查到这个 id，
    用清单里的记录去定位文件。查不到就返回 None，
    入参因此天然无法穿越目录，不需要额外写黑名单。
    """
    manifest_path = paths.ingest_dir(name) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None

    chapters = manifest.get("chapters") or []
    position = next(
        (i for i, c in enumerate(chapters) if str(c.get("id")) == chapter_id), None
    )
    if position is None:
        return None
    item = chapters[position]

    sub = "raw-chapters" if raw else "chapters"
    path = paths.ingest_dir(name) / sub / f"{item['id']}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    from workshop.annotate import strip_front_matter

    meta, body = strip_front_matter(text)

    annotation = None
    annotation_path = paths.annotations_dir(name) / f"{item['id']}.json"
    if annotation_path.exists():
        try:
            annotation = json.loads(annotation_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            annotation = None
    if isinstance(annotation, dict):
        # 原始返回只在排查提示词时有用，而且可能很长，不进阅读页
        annotation.pop("debug", None)

    # 没有标注时现算脚本轨字段。它是纯正则、零模型成本的，
    # 所以「看一章」不该因为没跑过模型就只剩光秃秃的正文。
    if annotation is None:
        fields, _issues = _live_script_fields(paths, name, item, body)
        fields_source = "script_live"
    else:
        fields = annotation.get("fields") or {}
        fields_source = "annotation"

    def brief(entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if not entry:
            return None
        return {
            "id": entry.get("id"),
            "chapter_no": entry.get("chapter_no"),
            "title": entry.get("title") or "",
        }

    return {
        "id": item.get("id"),
        "chapter_no": item.get("chapter_no"),
        "vol_no": item.get("vol_no"),
        "title": item.get("title") or "",
        "status": item.get("status") or "ok",
        "unnumbered": item.get("chapter_no") is None,
        "line_no": item.get("line_no"),
        "chars": item.get("char_count") or 0,
        "meta": meta,
        "text": body,
        "fields": fields,
        "fields_source": fields_source,
        "variant": "raw" if raw else "cleaned",
        "index": position,
        "total": len(chapters),
        "prev": brief(chapters[position - 1]) if position > 0 else None,
        "next": brief(chapters[position + 1]) if position + 1 < len(chapters) else None,
        "annotation": annotation,
    }


def _live_script_fields(paths: Paths, name: str, item: dict[str, Any], body: str) -> tuple[dict, list]:
    """现算脚本轨字段。纯正则，不调模型，所以随时可算。"""
    from workshop.primitives import WorkConfig, load_primitives, load_work
    from workshop.script_fields import compute_script_fields

    config_path = paths.work_config(name)
    work = load_work(config_path) if config_path.exists() else WorkConfig({})
    primitives = load_primitives(paths.root / "primitives.yaml")
    return compute_script_fields(
        body,
        vol_no=int(item.get("vol_no") or 1),
        chapter_no=item.get("chapter_no"),
        work=work,
        primitives=primitives,
    )


def list_anomalies(paths: Paths, name: str) -> list[dict[str, Any]]:
    manifest_path = paths.ingest_dir(name) / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return []
    return manifest.get("anomalies") or []


# ── 导入向导 ────────────────────────────────────────────────


def stage_upload(paths: Paths, filename: str, data: bytes) -> tuple[str, Path]:
    """把上传的文件落到临时目录，返回 token 与路径。

    预览时上传、确认时才导入，所以文件必须活过一次请求的间隔。
    """
    token = py_secrets.token_hex(8)
    folder = paths.uploads / token
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix or ".txt"
    target = folder / f"original{suffix}"
    target.write_bytes(data)
    (folder / "meta.json").write_text(
        json.dumps({"filename": filename, "bytes": len(data)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return token, target


def resolve_upload(paths: Paths, token: str) -> tuple[Path, dict[str, Any]] | None:
    """按 token 找回上传的文件。token 只允许十六进制，避免路径穿越。"""
    if not token or len(token) != 16 or not all(c in "0123456789abcdef" for c in token):
        return None
    folder = paths.uploads / token
    if not folder.is_dir():
        return None
    candidates = [p for p in folder.iterdir() if p.stem == "original"]
    if not candidates:
        return None
    meta: dict[str, Any] = {}
    meta_path = folder / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            meta = {}
    return candidates[0], meta


def preview_payload(result: dict[str, Any], *, limit: int = 30) -> dict[str, Any]:
    """把 ingest 的结果转成接口返回的结构化预览。

    刻意**不返回整章正文**——预览只需要让人确认「切得对不对」。
    """
    source = result.get("source") or {}
    chapters: list[Chapter] = result.get("_chapters") or []
    anomalies = result.get("anomalies") or []
    unmatched = result.get("unmatched_title_like") or []
    ads = result.get("suspected_ads") or {}
    notes = result.get("author_notes") or []
    repeated = result.get("repeated_lines_reported") or {}

    return {
        "source": {
            "file": source.get("file"),
            "bytes": source.get("bytes"),
            "encoding": source.get("detected_encoding"),
            "chars": source.get("total_chars_raw"),
            "lines": source.get("line_count"),
        },
        "counts": {
            "chapters": len(chapters),
            "segments": len(result.get("volumes") or []),
            "anomalies": len(anomalies),
            "unmatched_title_like": len(unmatched),
            "suspected_ads": len(ads),
            "author_notes": len(notes),
            "repeated_lines": len(repeated),
        },
        "anomaly_counts": _anomaly_counts(anomalies),
        "chapters_sample": [_chapter_brief(c) for c in chapters[:limit]],
        "chapters_truncated": max(0, len(chapters) - limit),
        "volumes": result.get("volumes") or [],
        "pattern_stats": result.get("pattern_stats") or [],
        "anomalies": anomalies[:60],
        "unmatched_title_like": unmatched[:30],
        "suspected_ads": [{"text": k, "count": v} for k, v in list(ads.items())[:30]],
        "author_notes": notes[:20],
        "repeated_lines": [{"text": k, "count": v} for k, v in list(repeated.items())[:20]],
        "integrity": result.get("integrity") or {},
        "cleaning": result.get("cleaning") or {},
    }


def do_ingest(
    paths: Paths,
    source_path: Path,
    *,
    work_name: str,
    strip_ads: bool = False,
    strip_repeated: bool = False,
    extra_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """跑一遍录入。不落盘——落盘由调用方在用户确认后触发。"""
    return ingest(
        source_path,
        work_name=work_name,
        strip_ads=strip_ads,
        strip_repeated=strip_repeated,
        extra_patterns=extra_patterns,
    )


def commit_ingest(paths: Paths, result: dict[str, Any], work_name: str) -> dict[str, Any]:
    out_dir = paths.ingest_dir(work_name)
    paths_list = save_ingest(result, out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    return {
        "ok": True,
        "work": work_name,
        "out_dir": str(out_dir),
        "files": {k: str(v) for k, v in paths_list.items()},
        "chapters": len(manifest.get("chapters") or []),
        "integrity_passed": bool((manifest.get("integrity") or {}).get("passed")),
    }


def archive_work(paths: Paths, name: str) -> dict[str, Any]:
    """把作品移出书架。

    **移动，不是删除。** 整个工作区（章节文件、清洗前副本、标注、伏笔台账）原样搬走，
    想恢复就把它搬回 workspaces/ 下一层。删除是不可逆的，而「我不想在书架上看到它」
    是个可逆的诉求，两者不该用同一个动作。
    """
    source = paths.work_dir(name)
    if not source.is_dir():
        raise FileNotFoundError(f"找不到作品「{name}」")

    archive_root = paths.workspaces / ARCHIVE_DIR_NAME
    archive_root.mkdir(parents=True, exist_ok=True)
    target = archive_root / name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        target = archive_root / f"{name}.{stamp}"

    shutil.move(str(source), str(target))
    return {"ok": True, "work": name, "moved_to": str(target)}


def list_archived(paths: Paths) -> list[dict[str, Any]]:
    """列出归档区里的作品，供恢复界面使用。

    归档目录下可能有带时间戳的同名副本（重名时 archive_work 自动加后缀），
    这里只读不改，别动用户文件。
    """
    archive_root = paths.workspaces / ARCHIVE_DIR_NAME
    if not archive_root.is_dir():
        return []
    items = []
    for entry in sorted(archive_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        # 归档的恢复要以「00-ingest/manifest.json 存在」为作品判据，
        # 和书架保持一致（下划线开头的不算作品，防止把杂物列进来）。
        if entry.name.startswith(("_", ".")):
            continue
        manifest_path = entry / "00-ingest" / "manifest.json"
        chapters = 0
        annotated = 0
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                chapters = len(manifest.get("chapters") or [])
                annotations_dir = entry / "10-annotations"
                if annotations_dir.is_dir():
                    annotated = len(list(annotations_dir.glob("*.json")))
            except (ValueError, OSError):
                pass
        items.append(
            {
                "name": entry.name,
                "path": str(entry),
                "chapters": chapters,
                "annotated": annotated,
                # 带 .时间戳 后缀的是重名副本，恢复时会还原成原名。
                "is_copy": _is_archive_timestamp_suffix(entry.name),
            }
        )
    return items


def _is_archive_timestamp_suffix(name: str) -> bool:
    """判断是否是「.20260924T151222」这种归档副本后缀。"""
    import re

    parts = name.rsplit(".", 1)
    return len(parts) == 2 and re.fullmatch(r"\d{8}T\d{6}", parts[1]) is not None


def restore_work(paths: Paths, name: str) -> dict[str, Any]:
    """把归档作品搬回书架。

    `name` 可能是 `作品名` 或 `作品名.时间戳`（重名时 archive_work 自动加的后缀）。
    恢复后一律落到**基础名**（去掉末尾的 .数字 后缀）：如果书架上已有同名，
    拒绝而非覆盖——归档是保险，不是用来覆盖现役数据的。
    """
    archive_root = paths.workspaces / ARCHIVE_DIR_NAME
    source = archive_root / name
    if not source.is_dir():
        raise FileNotFoundError(f"归档区里找不到「{name}」")

    # 去掉末尾的 .20260924T151222 这类时间戳后缀，还原成作品原名
    if _is_archive_timestamp_suffix(name):
        base = name.rsplit(".", 1)[0]
    else:
        base = name

    target = paths.work_dir(base)
    if target.exists():
        # 注意：target 可能是 archive 里同名的源之外的存在。
        # restore 的语义就是「把归档品恢复到书架」，书架已有同名 = 冲突，拒绝。
        if target.resolve() == source.resolve():
            raise ValueError(f"作品「{base}」已经在书架上")
        raise ValueError(f"书架里已有作品「{base}」，归档恢复不会覆盖它。请先移出原来的，再恢复这一份。")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return {"ok": True, "work": base, "restored_to": str(target)}


def cleanup_upload(paths: Paths, token: str) -> None:
    folder = paths.uploads / token
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def load_work_patterns(paths: Paths, work_name: str) -> list[str]:
    """读作品自己的章节模板。作品配置不存在就返回空列表（用内置模板）。"""
    path = paths.work_config(work_name)
    if not path.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except Exception:  # noqa: BLE001
        return []
    patterns = ((data.get("ingest") or {}).get("chapter_patterns")) or []
    return [str(p) for p in patterns if str(p).strip()]


# ── 模型任务：批量标注 ──────────────────────────────────
#
# 界面上这一块遵守「模型调用必须显式」：计划接口不调模型，
# 启动接口只在用户点了按钮之后才被请求，且启动前已经把范围与成本展示过了。
# 这里刻意**不提供**「一键跑完全部任务」——那会破坏显式原则。


@dataclass
class BatchJob:
    thread: threading.Thread
    runner: Any
    work: str


_JOBS: dict[str, BatchJob] = {}
_JOBS_LOCK = threading.Lock()

# 实体统计的实时状态与「停止」信号。标注那边状态挂在 runner 上，
# 实体这条路没有 runner，所以单独存一份：界面要能看到「跑到第几块」和
# 「上一次为什么结束」，否则失败了只剩一句「没有产出，检查运行记录」。
_ENTITY_LIVE: dict[str, dict[str, Any]] = {}
_ENTITY_STOPS: dict[str, threading.Event] = {}


def ensure_work_config(paths: Paths, name: str) -> Path:
    """作品配置缺失时补一份最小版本。

    不能用根目录模板顶替——那里写的是别的作品的主角与动机，套上去会让 P21
    度量错对象，而且不会报错，只会安静地产出一列错数据。
    """
    path = paths.work_config(name)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"work: {name}",
                'protagonist: ""',
                'core_motive: ""',
                "",
                "# core_motive 决定 P21「主角核心动机呈现强度」在度量什么。",
                "# 留空时模型会自己猜一个动机来打分，那一列数据不可信，请按剧情填写。",
                "style_checks: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _resolve_provider_and_model(cfg, requested_provider: str | None, requested_model: str | None):
    """按任务绑定决定用哪个服务商与模型。绑的是档位，不是模型名。"""
    from workshop.batch import pick_model_id

    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    provider_id = requested_provider or binding.get("provider")
    if not provider_id:
        raise ValueError("配置里没有 chapter_annotation 的任务绑定，先配一个服务商")
    provider = cfg.get_provider(str(provider_id))
    if provider is None:
        raise ValueError(f"配置里找不到服务商 {provider_id}")
    return provider, pick_model_id(cfg, provider, requested_model)


def annotation_plan(
    paths: Paths,
    name: str,
    *,
    limit: int | None = None,
    concurrency: int | None = None,
    ledger_enabled: bool = True,
    provider_id: str | None = None,
    model_id: str | None = None,
    only_ids: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """算出「这次要跑什么、大概花多少」。**不发起任何模型调用。**"""
    from workshop.batch import build_plan, load_chapter_tasks, resolve_concurrency
    from workshop.config import load_config
    from workshop.primitives import load_primitives, load_work

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)

    work_config = ensure_work_config(paths, name)
    primitives = load_primitives(paths.root / "primitives.yaml")
    work = load_work(work_config)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据，请先完成导入")

    plan = build_plan(
        cfg=cfg,
        provider=provider,
        model_id=model_id,
        primitives=primitives,
        work=work,
        tasks=tasks,
        annotations_dir=paths.annotations_dir(name),
        concurrency=resolve_concurrency(cfg, concurrency),
        ledger_enabled=ledger_enabled,
        limit=limit,
        only_ids=only_ids,
        force=force,
    )
    payload = plan.to_dict()
    payload["force"] = force
    payload["only_ids_count"] = len(only_ids) if only_ids else None
    payload["provider_name"] = provider.name
    payload["has_api_key"] = bool(_read_api_key(cfg, provider))
    # 可选模型必须列出来。**模型由用户显式选**——档位价差能到 4.5 倍，
    # 替用户默认一个贵 4.5 倍的，不合理。
    payload["available_models"] = [
        {"id": m.get("id"), "alias": m.get("alias") or m.get("id"), "role": m.get("role")}
        for m in provider.models
    ]
    payload["enabled"] = provider.enabled
    # 单价是从官方页面抄来的还是你核对过的，这是两回事。
    # 「有数字」不等于「数字可信」，界面上必须说清楚。
    snapshot = provider.raw.get("snapshot_ref") or {}
    payload["pricing_verified"] = bool(snapshot.get("verified_by_user"))
    payload["pricing_source"] = snapshot.get("source") or ""
    if not payload["pricing_verified"] and plan.estimate.cost_cny is not None:
        plan.warnings.append(
            f"单价来自 {payload['pricing_source'] or '配置里的快照'}，尚未经你在控制台核对"
            "（verified_by_user 为 false）。费用是估算值，以实际扣费为准。"
        )
        payload["warnings"] = plan.warnings
    return payload


def _read_api_key(cfg, provider) -> str:
    from workshop.secrets import SecretStore

    store = SecretStore(cfg.reports_dir.parent)
    return store.get(provider.api_key_ref) if provider.api_key_ref else ""


def annotation_status(paths: Paths, name: str) -> dict[str, Any]:
    """最近一次批量任务的状态。正在跑就返回实时进度。"""
    with _JOBS_LOCK:
        job = _JOBS.get(name)
    if job is not None and job.thread.is_alive():
        data = job.runner.state.to_dict()
        data["running"] = True
        return data

    latest = paths.annotations_dir(name) / "_runs" / "latest.json"
    if latest.exists():
        try:
            data = json.loads(latest.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            data = {}
        data["running"] = False
        # 把运行记录文件的位置也带上：界面上说「为什么失败」的同时，
        # 要让人能找到完整原文，而不是只能看到一个摘要。
        data["state_file"] = str(latest)
        _backfill_error_hints(data)
        return data
    return {"running": False, "status": "never", "total": 0, "committed": 0}


def _backfill_error_hints(data: dict[str, Any]) -> None:
    """给旧运行记录补上错误说明。

    加这个字段之前跑出来的记录里只有原始报错，界面上就只剩「失败 20」一句。
    与其让人重跑一遍才知道原因，不如读的时候现算——
    处置建议本来就只是一张查表，不需要重跑。
    """
    if data.get("error_hints"):
        return
    failures = data.get("failures") or []
    if not failures:
        return

    from workshop.errors import hint_for, is_fatal

    counts: dict[str, int] = {}
    for item in failures:
        for err in (item or {}).get("errors") or []:
            kind = str(err.get("kind") or "unknown")
            counts[kind] = counts.get(kind, 0) + 1
    data["error_hints"] = [
        {"kind": k, "hint": hint_for(k), "count": c, "fatal": is_fatal(k)}
        for k, c in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def start_annotation(
    paths: Paths,
    name: str,
    *,
    limit: int | None = None,
    concurrency: int | None = None,
    ledger_enabled: bool = True,
    allow_over_budget: bool = False,
    provider_id: str | None = None,
    model_id: str | None = None,
    only_ids: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """启动一次批量标注。这一步会真的花钱，所以调用方必须是用户主动点的按钮。"""
    from workshop.annotate import AnnotateOptions
    from workshop.batch import (
        BatchRunner,
        build_plan,
        load_chapter_tasks,
        resolve_annotation_options,
        resolve_concurrency,
    )
    from workshop.config import load_config
    from workshop.ledger import Ledger
    from workshop.llm import OpenAICompatProvider
    from workshop.primitives import load_primitives, load_work
    from workshop.secrets import SecretStore, setup_logging

    with _JOBS_LOCK:
        existing = _JOBS.get(name)
        if existing is not None and existing.thread.is_alive():
            raise RuntimeError(f"作品「{name}」有一个批量任务正在跑，等它结束再启动")

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)

    work_config = ensure_work_config(paths, name)
    primitives = load_primitives(paths.root / "primitives.yaml")
    work = load_work(work_config)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")

    plan = build_plan(
        cfg=cfg,
        provider=provider,
        model_id=model_id,
        primitives=primitives,
        work=work,
        tasks=tasks,
        annotations_dir=paths.annotations_dir(name),
        concurrency=resolve_concurrency(cfg, concurrency),
        ledger_enabled=ledger_enabled,
        limit=limit,
        only_ids=only_ids,
        force=force,
    )
    if plan.over_budget and not allow_over_budget:
        # 闸门在这里拦一次，运行中还会按实测 token 再拦一次。
        raise ValueError(
            f"估算 {plan.estimate.total_tokens:,} tokens 已超预算上限，"
            "请调高上限或减小范围后再跑"
        )

    secrets_store = SecretStore(cfg.reports_dir.parent)
    setup_logging(secrets_store.known_values)
    api_key = _read_api_key(cfg, provider)
    if provider.api_key_ref and not api_key:
        raise ValueError(f"读不到密钥，请设置环境变量 {provider.api_key_ref}")

    opts = resolve_annotation_options(cfg, AnnotateOptions())
    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=opts.timeout_sec,
        auth_scheme=provider.auth_scheme,
        secrets=secrets_store.known_values,
        rate_limit=provider.rate_limit,
    )

    ledger = None
    if plan.ledger_enabled:
        ledger = Ledger.load(paths.ledger_path(name))
        ledger.work = work.name

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    runs_dir = paths.annotations_dir(name) / "_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / f"{run_id}.json"
    latest_path = runs_dir / "latest.json"

    runner = BatchRunner(
        plan=plan,
        client=client,
        primitives=primitives,
        work=work,
        opts=opts,
        annotations_dir=paths.annotations_dir(name),
        ledger=ledger,
        ledger_path=paths.ledger_path(name) if ledger is not None else None,
        state_path=state_path,
        run_id=run_id,
        secrets=secrets_store.known_values,
        pricing=(provider.model(model_id) or {}).get("pricing") or {},
        price_bucket_now=plan.estimate.price_bucket,
    )

    def _worker() -> None:
        try:
            runner.run()
        except Exception as exc:  # noqa: BLE001
            # 线程里的异常不会冒到界面上，必须落进状态文件，否则用户只看到「一直转圈」
            runner.state.status = "failed"
            runner.state.message = f"批量任务异常中断：{type(exc).__name__}: {exc}"
            runner.state.finished_at = (
                datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            )
            runner._snapshot()
        try:
            latest_path.write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
        with _JOBS_LOCK:
            _JOBS.pop(name, None)

    thread = threading.Thread(target=_worker, name=f"annotate-{name}", daemon=True)
    with _JOBS_LOCK:
        _JOBS[name] = BatchJob(thread=thread, runner=runner, work=name)
    thread.start()

    return {"ok": True, "run_id": run_id, "work": name, "pending": len(plan.pending)}


def stop_annotation(name: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(name)
    if job is None:
        return {"ok": False, "message": "没有正在运行的任务"}
    job.runner.stop()
    return {"ok": True, "message": "已请求中止，当前章跑完即停"}


def providers_overview(paths: Paths) -> dict[str, Any]:
    """服务商概览。**只返回有没有密钥，绝不返回密钥本身。**"""
    from workshop.config import load_config

    cfg = load_config(paths.root / "providers.yaml")
    items = []
    for provider in cfg.providers:
        models = []
        for model in provider.models:
            pricing = model.get("pricing") or {}
            models.append(
                {
                    "id": model.get("id"),
                    "alias": model.get("alias") or model.get("id"),
                    "role": model.get("role"),
                    "has_pricing": any(
                        pricing.get(k) is not None
                        for k in (
                            "input_per_mtok",
                            "input_per_mtok_peak",
                            "input_per_mtok_offpeak",
                        )
                    ),
                    "measured_coefficient": (model.get("measured") or {}).get("tokens_per_cjk_char"),
                }
            )
        items.append(
            {
                "id": provider.id,
                "name": provider.name,
                "enabled": provider.enabled,
                "has_api_key": bool(_read_api_key(cfg, provider)),
                "api_key_env": provider.api_key_ref,
                "models": models,
            }
        )
    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    return {
        "providers": items,
        "binding": {"provider": binding.get("provider"), "model": binding.get("model")},
        "budget": cfg.settings.get("budget") or {},
        "gate_mode": cfg.settings.get("budget_gate_mode") or "token",
    }


# ── 设置：服务商与密钥 ──────────────────────────────────────
#
# 密钥的第一条纪律是**永不回显**：界面只给「有没有配」「来源是哪」「后 4 位」，
# 响应体里绝不出现明文。第二条是**不写出改了却不生效**——
# 写入策略见 SecretStore.set 的注释。


def _secret_store(paths: Paths):
    from workshop.secrets import SecretStore

    return SecretStore(paths.root / "config")


def settings_overview(paths: Paths) -> dict[str, Any]:
    from workshop.config import load_config, custom_config_path

    cfg = load_config(paths.root / "providers.yaml")
    custom_file = custom_config_path(paths.root / "providers.yaml")
    custom_ids: set[str] = set()
    if custom_file.exists():
        import yaml

        try:
            custom = yaml.safe_load(custom_file.read_text(encoding="utf-8-sig")) or {}
            custom_ids = {
                str(item.get("id"))
                for item in (custom.get("providers") or [])
                if isinstance(item, dict) and item.get("id")
            }
        except (OSError, yaml.YAMLError):
            custom_ids = set()

    store = _secret_store(paths)
    items = []
    for provider in cfg.providers:
        models = []
        for model in provider.models:
            pricing = model.get("pricing") or {}
            models.append(
                {
                    "id": model.get("id"),
                    "alias": model.get("alias") or model.get("id"),
                    "role": model.get("role"),
                    "pricing": {
                        "input_peak": pricing.get("input_per_mtok_peak"),
                        "input_offpeak": pricing.get("input_per_mtok_offpeak"),
                        "cached_input_peak": pricing.get("cached_input_per_mtok_peak"),
                        "cached_input_offpeak": pricing.get("cached_input_per_mtok_offpeak"),
                        "output_peak": pricing.get("output_per_mtok_peak"),
                        "output_offpeak": pricing.get("output_per_mtok_offpeak"),
                    },
                    "measured": model.get("measured") or {},
                }
            )
        snapshot = provider.raw.get("snapshot_ref") or {}
        ref = provider.api_key_ref
        items.append(
            {
                "id": provider.id,
                "name": provider.name,
                "enabled": provider.enabled,
                "type": provider.type,
                "base_url": provider.base_url,
                "api_key_env": ref,
                "custom": provider.id in custom_ids,  # 设置页可编辑的服务商
                "rate_limit": provider.rate_limit or None,
                "key": {
                    "configured": bool(store.get(ref)),
                    "source": store.source_of(ref),
                    "masked": store.masked(ref),
                },
                "pricing_verified": bool(snapshot.get("verified_by_user")),
                "pricing_source": snapshot.get("source") or "",
                "pricing_date": snapshot.get("date") or "",
                "models": models,
            }
        )

    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    return {
        "providers": items,
        "binding": {"provider": binding.get("provider"), "model": binding.get("model")},
        "gate_mode": cfg.settings.get("budget_gate_mode") or "token",
        "budget": cfg.settings.get("budget") or {},
        "env_file": str(store.env_file) if store.env_file else str(paths.root / "config" / ".env"),
        "secrets_file": str(store.secrets_file),
        "notes": [
            "密钥只存在本机文件里，服务也只绑 127.0.0.1，不会外传。",
            "读取优先级：环境变量 > .env > secrets.json。保存时会写回当前生效的那一个来源，避免「改了不生效」。",
            "限速按服务商配置的「每分钟请求数 / token 数」在客户端排队放行；上游仍返回 429 时会明确报错并提示等待时间。",
        ],
    }


def create_provider(paths: Paths, *, name: str, base_url: str, models: list[str],
                    api_key: str = "", requests_per_minute: int | None = None,
                    tokens_per_minute: int | None = None, enabled: bool = True) -> dict[str, Any]:
    """设置页新增服务商（本地自有/中转站等任何 OpenAI 兼容端点）。

    写进 custom-providers.yaml，密码走 SecretStore——providers.yaml 的
    手写注释不能被程序重写冲掉，所以程序负责的配置都放单独文件。

    模型列表按行解析，兼容两种写法：
      deepseek-flash                     # 只有 id
      DeepSeek 主力 | deepseek-flash     # alias | id
    """
    import re

    from workshop.config import load_config, custom_config_path, save_custom_config

    cfg = load_config(paths.root / "providers.yaml")
    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    if not name:
        raise ValueError("服务商名称不能为空")
    if not base_url:
        raise ValueError("Base URL 不能为空")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    if requests_per_minute is not None and (requests_per_minute < 0 or requests_per_minute > 10**7):
        raise ValueError("每分钟请求数要在 0 到 1000 万之间")

    parsed_models = _parse_models_text(models)
    if not parsed_models:
        raise ValueError("请至少填写一个模型（每行一个，格式：别名 | 模型id）")

    # 生成稳定的 id：小写 + 连字符。中文名转拼音会失真，拼音/英文更合适；
    # 全中文名退化为 provider-序号，避免两个中文名都叫 provider。
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    custom_existing = _load_custom(paths).get("providers") or []
    if not base:
        pid = f"provider-{len(custom_existing) + 1}"
    else:
        pid = base
    n = 2
    while cfg.get_provider(pid) is not None:
        pid = f"{base or 'provider'}-{n}"
        n += 1

    ref = f"{re.sub(r'[^A-Z0-9]+', '_', pid.upper()).strip('_')}_API_KEY"

    provider_entry: dict[str, Any] = {
        "id": pid,
        "name": name,
        "type": "cloud",
        "protocol": "openai_compatible",
        "enabled": enabled,
        "base_url": base_url,
        "auth": {
            "scheme": "bearer",
            "header_name": "Authorization",
            "api_key_ref": ref,
        },
        "rate_limit": {
            "requests_per_minute": requests_per_minute,
            "tokens_per_minute": tokens_per_minute,
        } if (requests_per_minute is not None or tokens_per_minute is not None) else {},
        "models": parsed_models,
    }

    custom = _load_custom(paths)
    custom.setdefault("providers", []).append(provider_entry)
    save_custom_config(paths.root / "providers.yaml", custom)

    if api_key:
        outcome = _secret_store(paths).set(ref, api_key)
        if not outcome.get("ok"):
            raise ValueError(outcome.get("message") or "密钥保存失败")

    return {"ok": True, "provider": pid, "name": name, "api_key_ref": ref}


def update_provider(paths: Paths, provider_id: str, *, name: str | None = None,
                    base_url: str | None = None, models: list[str] | None = None,
                    api_key: str = "", requests_per_minute: int | None = None,
                    tokens_per_minute: int | None = None,
                    enabled: bool | None = None) -> dict[str, Any]:
    """设置页更新自定义服务商。只有 custom 文件里的服务商可以改。"""
    from workshop.config import load_config, save_custom_config

    cfg = load_config(paths.root / "providers.yaml")
    provider = cfg.get_provider(provider_id)
    if provider is None:
        raise ValueError(f"配置里找不到服务商 {provider_id}")

    custom = _load_custom(paths)
    entries = [e for e in (custom.get("providers") or []) if isinstance(e, dict) and e.get("id") == provider_id]
    if not entries:
        raise ValueError(f"服务商 {provider_id} 不是设置页管理的服务商（custom-providers.yaml 里没有它）")

    entry = dict(entries[0])

    if name is not None:
        entry["name"] = (name or "").strip() or entry.get("name") or provider_id
    if base_url is not None:
        cleaned = (base_url or "").strip().rstrip("/")
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        entry["base_url"] = cleaned
    if models is not None:
        parsed = _parse_models_text(models)
        if not parsed:
            raise ValueError("模型列表为空")
        entry["models"] = parsed
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if requests_per_minute is not None or tokens_per_minute is not None:
        rate = dict(entry.get("rate_limit") or {})
        if requests_per_minute is not None:
            rate["requests_per_minute"] = int(requests_per_minute)
        if tokens_per_minute is not None:
            rate["tokens_per_minute"] = int(tokens_per_minute)
        entry["rate_limit"] = rate

    custom["providers"] = [
        entry if (isinstance(e, dict) and e.get("id") == provider_id) else e
        for e in (custom.get("providers") or [])
    ]
    save_custom_config(paths.root / "providers.yaml", custom)

    if api_key:
        ref = entry.get("auth") and entry["auth"].get("api_key_ref")
        if ref:
            outcome = _secret_store(paths).set(str(ref), api_key)
            if not outcome.get("ok"):
                raise ValueError(outcome.get("message") or "密钥保存失败")

    return {"ok": True, "provider": provider_id}


def delete_provider(paths: Paths, provider_id: str) -> dict[str, Any]:
    """从设置页删除自定义服务商。

    被任务绑定引用的服务商不能删——删了下一个任务会找不到 provider，
    那属于「静默失效」。拒绝并说明比删除更友好。
    """
    from workshop.config import load_config, save_custom_config

    cfg = load_config(paths.root / "providers.yaml")
    bindings = cfg.task_bindings or {}
    for task, binding in bindings.items():
        if isinstance(binding, dict) and str(binding.get("provider") or "") == provider_id:
            raise ValueError(
                f"任务绑定「{task}」正在使用 {provider_id}，"
                "请先在设置页把默认模型换到别的服务商再删除"
            )

    custom = _load_custom(paths)
    before = len(custom.get("providers") or [])
    custom["providers"] = [
        e for e in (custom.get("providers") or [])
        if not (isinstance(e, dict) and e.get("id") == provider_id)
    ]
    if len(custom["providers"]) == before:
        raise ValueError(f"custom-providers.yaml 里没有服务商 {provider_id}，无需删除")
    save_custom_config(paths.root / "providers.yaml", custom)
    return {"ok": True, "provider": provider_id}


def save_default_binding(paths: Paths, *, provider: str, model: str | None = None) -> dict[str, Any]:
    """设置「默认模型」：把逐章标注绑定的 provider/model 写进 custom 配置。

    界面上显示成「默认模型」——所有任务（标注/大纲/实体）没单独指定时
    都回退到它。这决定了「用户自己选择使用哪个模型」落在哪里。
    """
    from workshop.config import load_config, save_custom_config

    cfg = load_config(paths.root / "providers.yaml")
    provider_obj = cfg.get_provider(provider)
    if provider_obj is None:
        raise ValueError(f"配置里找不到服务商 {provider}")
    if model:
        if provider_obj.model(model) is None:
            available = "、".join(provider_obj.model_ids[:10]) or "（无）"
            raise ValueError(f"{provider} 下没有模型 {model}，可用：{available}")
    else:
        try:
            from workshop.config import resolve_default_model

            model = resolve_default_model(cfg, provider_obj)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    custom = _load_custom(paths)
    bindings = dict(custom.get("task_bindings") or {})
    bindings["chapter_annotation"] = {"provider": provider, "model": model}
    custom["task_bindings"] = bindings
    save_custom_config(paths.root / "providers.yaml", custom)
    return {"ok": True, "provider": provider, "model": model}


def _load_custom(paths: Paths) -> dict[str, Any]:
    """读 custom-providers.yaml 的原始内容（不会走合并逻辑）。"""
    from workshop.config import custom_config_path

    path = custom_config_path(paths.root / "providers.yaml")
    if not path.exists():
        return {}
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_models_text(lines: list[str]) -> list[dict[str, Any]]:
    """把文本行解析成 models 条目。每行：「别名 | 模型id」或只有 id。"""
    out: list[dict[str, Any]] = []
    for raw in lines or []:
        line = (raw or "").strip()
        if not line:
            continue
        if "|" in line:
            alias, _, mid = line.partition("|")
            alias, mid = alias.strip(), mid.strip()
        else:
            mid, alias = line.strip(), line.strip()
        if not mid:
            continue
        out.append({
            "id": mid,
            "alias": alias or mid,
            "role": "main" if not out else "trial",
            "capabilities": {"structured_output": True},
        })
    return out


def save_provider_key(paths: Paths, provider_id: str, value: str) -> dict[str, Any]:
    from workshop.config import load_config

    cfg = load_config(paths.root / "providers.yaml")
    provider = cfg.get_provider(provider_id)
    if provider is None:
        raise ValueError(f"配置里找不到服务商 {provider_id}")
    ref = provider.api_key_ref
    if not ref:
        raise ValueError(f"服务商 {provider_id} 没有配置 api_key_ref，无法保存密钥")
    outcome = _secret_store(paths).set(ref, value)
    outcome["provider"] = provider_id
    outcome["ref"] = ref
    return outcome


def clear_provider_key(paths: Paths, provider_id: str) -> dict[str, Any]:
    from workshop.config import load_config

    cfg = load_config(paths.root / "providers.yaml")
    provider = cfg.get_provider(provider_id)
    if provider is None:
        raise ValueError(f"配置里找不到服务商 {provider_id}")
    outcome = _secret_store(paths).clear(provider.api_key_ref or "")
    outcome["provider"] = provider_id
    return outcome


def test_provider(paths: Paths, provider_id: str, *, model_id: str | None = None) -> dict[str, Any]:
    """测试连接：端点 + 鉴权 + 模型名，三件事一次问清。

    刻意**只发合成测试句**，不碰任何作品原文；这也让它能随时点。
    """
    from workshop.errors import hint_for
    from workshop.llm import ApiError, OpenAICompatProvider
    from workshop.config import load_config
    from workshop.secrets import setup_logging

    cfg = load_config(paths.root / "providers.yaml")
    provider, resolved = _resolve_provider_and_model(cfg, provider_id, model_id)
    store = _secret_store(paths)
    setup_logging(store.known_values)

    steps: list[dict[str, Any]] = []
    api_key = store.get(provider.api_key_ref) if provider.api_key_ref else ""
    if provider.api_key_ref and not api_key:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "读取密钥",
                    "ok": False,
                    "detail": f"没有找到 {provider.api_key_ref}",
                    "hint": "在下面的输入框里填入密钥并保存",
                }
            ],
            "model": resolved,
        }

    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=30,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
        rate_limit=provider.rate_limit,
    )

    # ① 端点与鉴权
    models: list[str] = []
    try:
        models, rtt = client.list_models()
        steps.append(
            {"name": "端点与鉴权", "ok": True, "detail": f"往返 {round(rtt)} ms，发现 {len(models)} 个模型"}
        )
    except ApiError as exc:
        steps.append(
            {
                "name": "端点与鉴权",
                "ok": False,
                "detail": f"{exc.kind.value}（HTTP {exc.status}）",
                "hint": hint_for(exc.kind),
                "raw": exc.safe_body(store.known_values, limit=240),
            }
        )
        return {"ok": False, "steps": steps, "model": resolved}

    # ② 模型名与最小对话
    if models and resolved not in models:
        steps.append(
            {
                "name": "模型名",
                "ok": False,
                "detail": f"{resolved} 不在可用列表里",
                "hint": f"可用模型：{'、'.join(models[:8])}",
            }
        )
        return {"ok": False, "steps": steps, "model": resolved}

    try:
        result = client.chat(
            resolved,
            [
                {"role": "system", "content": "只回一个字。"},
                {"role": "user", "content": "回复：好"},
            ],
            max_tokens=8,
            temperature=0.0,
            extra={"thinking": {"type": "disabled"}},
        )
        steps.append(
            {
                "name": "最小对话",
                "ok": True,
                "detail": f"{round(result.total_ms)} ms，返回 {len(result.text or '')} 字"
                + (f"，用量 {result.usage.total_tokens} tokens" if result.usage else "（未返回用量）"),
            }
        )
    except ApiError as exc:
        steps.append(
            {
                "name": "最小对话",
                "ok": False,
                "detail": f"{exc.kind.value}（HTTP {exc.status}）",
                "hint": hint_for(exc.kind),
                "raw": exc.safe_body(store.known_values, limit=240),
            }
        )
        return {"ok": False, "steps": steps, "model": resolved}

    return {
        "ok": all(s["ok"] for s in steps),
        "steps": steps,
        "model": resolved,
        "models": models[:40],
    }


def probe_provider(paths: Paths, provider_id: str, *, model_id: str | None = None) -> dict[str, Any]:
    """完整探测并存档：跑 P1-P5，写一份基准测报告。"""
    from workshop.config import load_config
    from workshop.llm import OpenAICompatProvider
    from workshop.probe import ProbeOptions, format_report_text, run_probe
    from workshop.report import save_report
    from workshop.secrets import setup_logging

    cfg = load_config(paths.root / "providers.yaml")
    provider, resolved = _resolve_provider_and_model(cfg, provider_id, model_id)
    store = _secret_store(paths)
    setup_logging(store.known_values)
    api_key = store.get(provider.api_key_ref) if provider.api_key_ref else ""
    if provider.api_key_ref and not api_key:
        raise ValueError(f"读不到密钥 {provider.api_key_ref}")

    probe_cfg = cfg.probe_settings
    opts = ProbeOptions(
        text_chars=int(probe_cfg.get("probe_text_chars") or 1000),
        samples=int(probe_cfg.get("samples") or 3),
        streaming=bool(probe_cfg.get("streaming", True)),
        thinking=str(probe_cfg.get("thinking") or "disabled"),
        triggered_by="manual_test_connection(界面)",
    )
    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=opts.timeout_sec,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
        rate_limit=provider.rate_limit,
    )
    report = run_probe(
        client=client,
        provider_id=provider.id,
        provider_name=provider.name,
        model_id=resolved,
        opts=opts,
        pricing=provider.pricing,
        base_url=provider.base_url,
        protocol=provider.protocol,
        anthropic_base_url=provider.base_url_anthropic,
    )
    saved: dict[str, str] = {}
    if cfg.archive_enabled:
        paths_written = save_report(report, cfg.reports_dir, secrets=store.known_values)
        saved = {k: str(v) for k, v in paths_written.items()}
    return {
        "ok": True,
        "text": format_report_text(report),
        "report": report,
        "saved": saved,
    }


# ── 标注总览 ────────────────────────────────────────────────
#
# 体检报告给的是汇总（图表、分布、断点），单章标注给的是某一章的全部字段。
# 中间缺一层：「这本书标出来的数据长什么样」——一页表格就能扫完。
# 没有它，用户只能一页页点进章节去看，等于没有总览。


_ANNOTATION_COLUMNS = (
    "hook_strength",
    "hook_type",
    "emotion",
    "conflict",
    "info_release",
    "mainline_progress",
)


def list_annotations(
    paths: Paths,
    name: str,
    *,
    offset: int = 0,
    limit: int = 50,
    only: str = "all",
) -> dict[str, Any]:
    annotations_dir = paths.annotations_dir(name)
    manifest_path = paths.ingest_dir(name) / "manifest.json"
    titles: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            manifest = {}
        titles = {c.get("id"): c for c in manifest.get("chapters") or []}

    rows: list[dict[str, Any]] = []
    if annotations_dir.exists():
        for path in sorted(annotations_dir.glob("*.json")):
            record = load_annotation(path)
            if not record:
                continue
            fields = record.get("fields") or {}
            cid = str(record.get("chapter_id") or path.stem)
            meta = titles.get(cid) or {}
            row = {
                "chapter_id": cid,
                "chapter_no": record.get("chapter_no") or meta.get("chapter_no"),
                "title": record.get("title") or meta.get("title") or "",
                "chars": fields.get("char_count") or meta.get("char_count") or 0,
                "status": record.get("status"),
                "review_action": record.get("review_action"),
                "confidence": (record.get("self_report") or {}).get("confidence"),
                "chapter_summary": fields.get("chapter_summary"),
                "payoff_count": len(fields.get("payoffs") or []),
                "foreshadow_count": len(fields.get("foreshadows") or []),
            }
            for key in _ANNOTATION_COLUMNS:
                row[key] = fields.get(key)
            rows.append(row)

    if only == "review":
        rows = [r for r in rows if r["status"] == "needs_review"]
    elif only == "ok":
        rows = [r for r in rows if r["status"] == "ok"]

    total = len(rows)
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": rows[offset : offset + limit],
    }


# ── 标注台 ────────────────────────────────────────────────
#
# UI-P3 的核心工作界面：模型先跑一遍，人只修正低置信度章节。
# 接口只要三件东西：
#   1. 原语字段定义 —— 决定右栏「标注面板」长什么样
#   2. 章节列表 —— 左栏，带状态色标
#   3. 单章明细 + 保存修正 —— 中栏正文、右栏字段与保存

def annotator_fields(paths: Paths) -> dict[str, Any]:
    """标注台用的原语字段定义（模型轨）。脚本轨不在这里——标注台只管人看的字段。"""
    from workshop.primitives import load_primitives

    primitives = load_primitives(paths.root / "primitives.yaml")
    model_fields = []
    for f in primitives.model_fields:
        item: dict[str, Any] = {
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "hint": f.hint,
            "item_keys": f.item_keys,
            "item_enums": f.item_enums,
            "max_len": f.max_len,
        }
        if f.type == "enum":
            item["values"] = f.values
        elif f.type == "list_of_objects":
            item["item_enums"] = {
                k: primitives.enums.get(ref, []) for k, ref in f.item_enums.items()
            }
        model_fields.append(item)

    order: list[str] = []
    for f in primitives.model_fields:
        if f.type in ("rating", "enum", "int", "str"):
            order.append(f.key)
    for f in primitives.model_fields:
        if f.type == "list_of_objects":
            order.append(f.key)

    return {
        "model_fields": model_fields,
        # 字母区外层套 key 顺序，评价类放前面、对象列表放后面
        "field_order": order,
        "meta_fields": primitives.meta_fields,
        "version": primitives.version,
    }


def annotator_chapters(paths: Paths, name: str) -> dict[str, Any]:
    """标注台左栏：全部章节 + 标注状态，一次拉全（标注台是键盘高频操作，不该分页跳动）。"""
    from workshop.annotate import load_annotation

    manifest_path = paths.ingest_dir(name) / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    annotations_dir = paths.annotations_dir(name)
    chapters = manifest.get("chapters") or []
    items = []
    for c in chapters:
        cid = str(c.get("id"))
        record = load_annotation(annotations_dir / f"{cid}.json") if annotations_dir.exists() else None
        items.append(
            {
                "id": cid,
                "chapter_no": c.get("chapter_no"),
                "title": c.get("title") or "",
                "vol_no": c.get("vol_no") or 1,
                "status": (record or {}).get("status"),
                "review_action": (record or {}).get("review_action"),
                "confidence": ((record or {}).get("self_report") or {}).get("confidence"),
                "issue_count": len((record or {}).get("issues") or []),
            }
        )
    return {"total": len(items), "items": items}


def annotator_chapter_detail(paths: Paths, name: str, chapter_id: str) -> dict[str, Any] | None:
    """标注台用的单章明细：正文 + 当前标注字段。"""
    detail = chapter_detail(paths, name, chapter_id)
    if detail is None:
        return None
    record = detail.get("annotation")
    fields = detail.get("fields") or {}
    self_report = (record or {}).get("self_report") or {}
    return {
        "id": detail["id"],
        "chapter_no": detail["chapter_no"],
        "vol_no": detail["vol_no"],
        "title": detail["title"],
        "text": detail["text"],
        "status": (record or {}).get("status") or "unannotated",
        "review_action": (record or {}).get("review_action"),
        "fields": fields,
        "self_report": self_report,
        "issues": (record or {}).get("issues") or [],
        "prev": detail["prev"],
        "next": detail["next"],
        "index": detail["index"],
        "total": detail["total"],
    }


def save_annotation_review(
    paths: Paths, name: str, chapter_id: str, *, fields: dict[str, Any] | None,
    status: str | None, reason: str = "",
) -> dict[str, Any]:
    """标注台保存人工修正：更新字段与复核状态，写回标注文件。

    纪律：
      · 只接受标注文件里已有字段的修正，不新增字段（跨作品一致性靠字段集固定）
      · 改了字段就把 status 扭回 needs_review（人改过的东西在复核之前不算「正常」）
      · 未标注章节不落盘——标注台是「修正」不是「create」，没有标注就明说
    """
    from workshop.annotate import annotation_path, load_annotation, save_annotation
    from workshop.secrets import SecretStore, setup_logging

    store = SecretStore(paths.root / "config")
    setup_logging(store.known_values)

    ann_dir = paths.annotations_dir(name)
    path = annotation_path(ann_dir, chapter_id)
    record = load_annotation(path)
    if record is None:
        raise ValueError(f"章节 {chapter_id} 还没有标注记录，标注台只能修正已有标注")

    if fields is not None:
        existing = record.get("fields") or {}
        # 原语字段集 = 落盘记录里的现有字段。人工修正只在这些字段上做 merge。
        merged = dict(existing)
        changed = False
        for key, value in fields.items():
            if key in existing and existing.get(key) != value:
                merged[key] = value
                changed = True
        if changed:
            record["fields"] = merged
        # 人改过字段 → 自动转待复核（改过的东西在复核之前不算「正常」）
        if changed and status not in ("ok",):
            record["status"] = "needs_review"

    if status in ("ok", "needs_review"):
        record["status"] = status

    issues = record.get("issues")
    if not isinstance(issues, list):
        issues = []
    if reason:
        issues.append(f"人工修正：{reason}")
    record["issues"] = issues

    prov = record.get("provenance")
    if not isinstance(prov, dict):
        prov = {}
    prov["reviewed_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    prov["reviewed_by"] = "ui-annotator"
    record["provenance"] = prov

    saved = save_annotation(record, ann_dir, secrets=store.known_values)
    return {
        "ok": True,
        "chapter_id": chapter_id,
        "status": record["status"],
        "path": str(saved),
    }


# ── 知识库 ────────────────────────────────────────────────
#
# K1 实体卡片 / K2 四类台账 / K3 作品指纹。纯脚本聚合，不调模型。
# 「知识库查看」随时可看；「重新构建」只在你改过标注之后需要点。

def kb_latest(paths: Paths, name: str) -> dict[str, Any] | None:
    out_dir = paths.work_dir(name) / "20-kb"
    path = out_dir / "index.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def build_kb(paths: Paths, name: str) -> dict[str, Any]:
    from workshop.kb import build_kb as _do_build

    out_dir = paths.work_dir(name) / "20-kb"
    entities_path = paths.work_dir(name) / "50-entities" / "latest.json"
    return _do_build(
        work_name=name,
        ingest_dir=paths.ingest_dir(name),
        annotations_dir=paths.annotations_dir(name),
        entities_path=entities_path,
        out_dir=out_dir,
    )


# ── 重写工坊 ──────────────────────────────────────────────
#
# M3 重写 + M4 六类校验 + G2 输出闸门。改写是新版本，原稿永远保留。
# 计划免费；执行会调模型（花钱）；接受是写新版本文件（不覆盖原稿）。

DIRECTIVES = ["改视角", "扩写", "精简", "调节奏", "强化动机"]


def rewrite_plan(paths: Paths, name: str, provider_id: str | None = None, model_id: str | None = None) -> dict[str, Any]:
    """改写台计划：列出可选指令类型与章节（免费，不调模型）。"""
    from workshop.batch import load_chapter_tasks
    from workshop.config import load_config
    from workshop.primitives import load_work

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")

    work = load_work(paths.work_config(name)) if paths.work_config(name).exists() else None
    return {
        "directives": DIRECTIVES,
        "provider": provider.id,
        "provider_name": provider.name,
        "model": model_id,
        "model_available": [m.get("id") for m in provider.models],
        "has_api_key": bool(_read_api_key(cfg, provider)),
        "chapters": [
            {
                "id": t.chapter_id,
                "chapter_no": t.chapter_no,
                "title": t.title,
                "chars": t.char_count,
            }
            for t in tasks
        ],
        "has_rewrites": rewrite_latest(paths, name) is not None,
        "core_motive": work.core_motive if work else "",
    }


def rewrite_latest(paths: Paths, name: str) -> dict[str, Any] | None:
    """最近一次改写记录（任一章）。"""
    out = paths.work_dir(name) / "30-rewrite"
    if not out.exists():
        return None
    latest_path = out / "latest.json"
    if latest_path.exists():
        try:
            return json.loads(latest_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            pass
    return None


def rewrite_chapter_record(paths: Paths, name: str, chapter_id: str) -> dict[str, Any] | None:
    out = paths.work_dir(name) / "30-rewrite"
    path = out / f"{chapter_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def start_rewrite(
    paths: Paths,
    name: str,
    *,
    chapter_id: str,
    directive: str,
    instruction: str = "",
    provider_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """执行一章的重写（会花钱）。原稿不动，改写稿 + 校验一起落盘。"""
    from workshop.batch import load_chapter_tasks
    from workshop.config import load_config
    from workshop.kb import build_k1
    from workshop.primitives import WorkConfig, load_work
    from workshop.llm import OpenAICompatProvider
    from workshop.rewrite import (
        RewriteOptions,
        pack_validation,
        rewrite_chapter,
        save_rewrite,
        validate_rewrite,
    )
    from workshop.secrets import SecretStore, setup_logging

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)
    if provider.api_key_ref and not _read_api_key(cfg, provider):
        raise ValueError(f"读不到密钥，请设置环境变量 {provider.api_key_ref}")

    tasks = load_chapter_tasks(paths.ingest_dir(name))
    task = next((t for t in tasks if t.chapter_id == chapter_id), None)
    if task is None:
        raise ValueError(f"找不到章节 {chapter_id}")

    body = task.path.read_text(encoding="utf-8")
    from workshop.annotate import strip_front_matter

    _meta, body = strip_front_matter(body)

    work = load_work(paths.work_config(name)) if paths.work_config(name).exists() else WorkConfig({})
    store = SecretStore(paths.root / "config")
    setup_logging(store.known_values)
    api_key = _read_api_key(cfg, provider)

    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=90,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
        rate_limit=provider.rate_limit,
    )

    result = rewrite_chapter(
        client=client,
        model_id=model_id,
        chapter_id=task.chapter_id,
        chapter_no=task.chapter_no,
        title=task.title,
        original=body,
        directive=directive,
        instruction=instruction,
        opts=RewriteOptions(),
        system_theme=f"当前作品：{work.name}\n主角：{work.protagonist or '（未设置）'}\n"
        f"核心动机：{work.core_motive or '（未设置）'}",
    )

    # 六类校验
    annotation = load_annotation(paths.annotations_dir(name) / f"{chapter_id}.json") if (
        paths.annotations_dir(name) / f"{chapter_id}.json"
    ).exists() else None
    annotation_fields = (annotation or {}).get("fields") or {}
    k1 = build_k1(name, _latest_entities(paths, name))
    issues = validate_rewrite(
        original=body,
        rewritten=result.record["rewritten"],
        work=work,
        annotation_fields=annotation_fields,
        k1=k1,
        directive=directive,
    )
    result.record["validation"] = pack_validation(issues)
    result.record["k1_available"] = k1.get("available", False)

    saved = save_rewrite(paths.work_dir(name), result.record)
    # 留一份 latest 便于改写台直接读
    out = paths.work_dir(name) / "30-rewrite"
    (out / "latest.json").write_text(json.dumps(result.record, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "chapter_id": chapter_id, "path": str(saved), **result.record["validation"]}


def accept_rewrite_record(paths: Paths, name: str, chapter_id: str, *, force: bool = False) -> dict[str, Any]:
    """接受改写（G2 闸门）。force 用于明确选择「改坏也认」。"""
    from workshop.rewrite import accept_rewrite as _accept
    from workshop.secrets import SecretStore

    if force:
        out = paths.work_dir(name) / "30-rewrite"
        path = out / f"{chapter_id}.json"
        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8-sig"))
            rec["force_accept"] = True
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    store = SecretStore(paths.root / "config")
    return _accept(paths.work_dir(name), chapter_id, secrets=store.known_values)


def _latest_entities(paths: Paths, name: str) -> dict[str, Any] | None:
    path = paths.work_dir(name) / "50-entities" / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


# ── 题材库 / 对比 / 融合 ────────────────────────────────
#
# L2 题材库（M6）、M8 对比分析、M5 融合器。
# 全部纯脚本聚合，不调模型；文件系统是唯一真相源。

def genres_root(paths: Paths) -> Path:
    return paths.root / "genres"


def compare_root(paths: Paths) -> Path:
    return paths.root / "compare"


def fusion_root(paths: Paths) -> Path:
    return paths.root / "workspaces" / "_fusion"


def genre_index(paths: Paths) -> dict[str, Any]:
    from workshop.genres import list_genres, load_index

    return {
        "mapping": load_index(genres_root(paths)),
        "genres": list_genres(genres_root(paths)),
    }


def set_work_genre(paths: Paths, work: str, genre: str) -> dict[str, Any]:
    from workshop.genres import set_genre

    return set_genre(genres_root(paths), work, genre)


def genre_rules(paths: Paths, genre: str) -> dict[str, Any] | None:
    path = genres_root(paths) / genre / "rules.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def aggregate_genre_rules(paths: Paths, genre: str) -> dict[str, Any]:
    from workshop.genres import aggregate_genre, save_genre_rules

    result = aggregate_genre(genres_root(paths), paths.workspaces, genre)
    saved = save_genre_rules(genres_root(paths), result)
    payload = result.to_dict()
    payload["saved"] = str(saved)
    return payload


def build_compare_report(paths: Paths, work: str, genre: str) -> dict[str, Any]:
    from workshop.compare import build_compare, save_compare

    result = build_compare(paths.workspaces, genres_root(paths), work, genre)
    saved = save_compare(compare_root(paths), result)
    payload = result.to_dict()
    payload["saved"] = str(saved)
    return payload


def compare_latest(paths: Paths) -> dict[str, Any] | None:
    path = compare_root(paths) / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def generate_fusion(paths: Paths, source_works: list[str] | None = None, seed: int | None = None) -> dict[str, Any]:
    from workshop.fusion import generate_skeleton, save_skeleton

    skeleton = generate_skeleton(paths.workspaces, source_works or [], seed=seed)
    saved = save_skeleton(fusion_root(paths), skeleton)
    payload = skeleton.to_dict()
    payload["saved"] = str(saved)
    return payload


def fusion_latest(paths: Paths) -> dict[str, Any] | None:
    path = fusion_root(paths) / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


# ── 实体统计 ────────────────────────────────────────────────
#
# 人物 / 势力 / 能力 / 地点 / 关系。原料是正文（梗概装不下关系细节），
# 所以这一步必须真的调模型，没有「零标注也能出」的降级路径——
# 没有章节就明说跑不了。


def entities_plan(
    paths: Paths, name: str, *, block_size: int = DEFAULT_BLOCK_SIZE,
    provider_id: str | None = None, model_id: str | None = None,
) -> dict[str, Any]:
    from workshop.batch import _pick_price, load_chapter_tasks, price_bucket
    from workshop.config import load_config
    from workshop.entities import build_entities_plan

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")
    model_cfg = provider.model(model_id) or {}
    bucket = price_bucket(provider)
    plan = build_entities_plan(
        work=name,
        provider_id=provider.id,
        model_id=model_id,
        tasks=tasks,
        block_size=block_size,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
    )
    payload = plan.to_dict()
    payload["provider_name"] = provider.name
    payload["has_api_key"] = bool(_read_api_key(cfg, provider))
    # 可选模型必须列出来。**模型由用户显式选**——档位价差能到 4.5 倍，
    # 替用户默认一个贵 4.5 倍的，不合理。
    payload["available_models"] = [
        {"id": m.get("id"), "alias": m.get("alias") or m.get("id"), "role": m.get("role")}
        for m in provider.models
    ]
    payload["has_result"] = entities_latest(paths, name) is not None
    # 分次跑的进度：已完成的块不重复花钱，所以「还剩几块」直接决定下一次还要花多少
    payload["progress"] = _entities_progress(paths, name, tasks, block_size)
    # 界面刷新后要能认出「还有一个任务在跑」，否则会丢掉进度与停止按钮
    with _JOBS_LOCK:
        job = _JOBS.get(f"entities:{name}")
    payload["running"] = job is not None and job.thread.is_alive()
    return payload


def entities_latest(paths: Paths, name: str) -> dict[str, Any] | None:
    path = paths.work_dir(name) / "50-entities" / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def _entities_progress(paths: Paths, name: str, tasks: list, block_size: int) -> dict[str, Any]:
    """已跑 / 失败 / 还没跑的块数。

    实体抽取允许分次跑（停一下、下次接着跑），所以「还剩多少」必须是界面上
    看得见的东西，而不是让人自己数块。
    """
    from workshop.entities import _block_label

    size = max(1, block_size)
    labels = [_block_label(tasks[i : i + size]) for i in range(0, len(tasks), size)]
    state_path = paths.work_dir(name) / "50-entities" / "_state.json"
    known: dict[str, Any] = {}
    if state_path.exists():
        try:
            cached = json.loads(state_path.read_text(encoding="utf-8-sig"))
            known = {
                str(b.get("range")): b
                for b in (cached.get("blocks") or [])
                if isinstance(b, dict) and b.get("range")
            }
        except (ValueError, OSError):
            known = {}

    done = failed = pending = 0
    for label in labels:
        item = known.get(label)
        if item is None or item.get("pending"):
            pending += 1
        elif item.get("error"):
            failed += 1
        else:
            done += 1
    salvaged = sum(1 for item in known.values() if item.get("salvaged"))
    return {
        "total": len(labels),
        "done": done,
        "failed": failed,
        "pending": pending,
        "salvaged": salvaged,
    }


def _model_max_tokens(model_cfg: dict[str, Any]) -> int:
    """单次输出的 token 预算。策略在 llm 层（命令行入口也用它），这里只做转发。

    实体统计和大纲都从这里取——两处都栽过同一个坑：写死一个小预算，
    输出被截断，报出来却是「返回内容不是 JSON」。
    """
    from workshop.llm import default_max_tokens

    return default_max_tokens(model_cfg)


def start_entities(
    paths: Paths, name: str, *, block_size: int = DEFAULT_BLOCK_SIZE,
    provider_id: str | None = None, model_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    from workshop.batch import _pick_price, load_chapter_tasks, price_bucket
    from workshop.config import load_config
    from workshop.entities import EntitiesOptions, build_entities_plan, generate_entities, save_entities
    from workshop.llm import OpenAICompatProvider
    from workshop.secrets import SecretStore, setup_logging

    key = f"entities:{name}"
    with _JOBS_LOCK:
        existing = _JOBS.get(key)
        if existing is not None and existing.thread.is_alive():
            raise RuntimeError(f"作品「{name}」已经有一个实体统计任务在跑")

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")
    model_cfg = provider.model(model_id) or {}
    bucket = price_bucket(provider)
    plan = build_entities_plan(
        work=name,
        provider_id=provider.id,
        model_id=model_id,
        tasks=tasks,
        block_size=block_size,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
    )

    store = SecretStore(cfg.reports_dir.parent)
    setup_logging(store.known_values)
    api_key = _read_api_key(cfg, provider)
    if provider.api_key_ref and not api_key:
        raise ValueError(f"读不到密钥，请设置环境变量 {provider.api_key_ref}")
    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=180,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
        rate_limit=provider.rate_limit,
    )
    out_dir = paths.work_dir(name) / "50-entities"
    state: dict[str, Any] = {"running": True, "done": 0, "total": plan.blocks, "label": "", "error": ""}
    stop_event = threading.Event()
    opts = EntitiesOptions(block_size=block_size, max_tokens=_model_max_tokens(model_cfg))

    def _worker() -> None:
        try:
            result = generate_entities(
                plan=plan,
                tasks=tasks,
                client=client,
                opts=opts,
                secrets=store.known_values,
                on_progress=lambda i, t, l: state.update({"done": i, "total": t, "label": l}),
                state_path=out_dir / "_state.json",
                limit=limit,
                should_stop=stop_event.is_set,
            )
            save_entities(paths.work_dir(name), result)
            state["error"] = "；".join(result.get("errors") or [])[:400]
            state["pending"] = len(result.get("pending") or [])
            state["stopped"] = bool(result.get("stopped"))
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            state["running"] = False
            with _JOBS_LOCK:
                _JOBS.pop(key, None)
                _ENTITY_STOPS.pop(name, None)

    thread = threading.Thread(target=_worker, name=f"entities-{name}", daemon=True)
    with _JOBS_LOCK:
        _JOBS[key] = BatchJob(thread=thread, runner=None, work=name)
        _ENTITY_LIVE[name] = state
        _ENTITY_STOPS[name] = stop_event
    thread.start()
    return {
        "ok": True,
        "work": name,
        "blocks": plan.blocks,
        "limit": limit,
        "max_tokens": opts.max_tokens,
        "state": state,
    }


def stop_entities(name: str) -> dict[str, Any]:
    """请求停止实体抽取。当前块跑完即停，已完成的块与费用都留着，下次接着跑。"""
    with _JOBS_LOCK:
        job = _JOBS.get(f"entities:{name}")
        event = _ENTITY_STOPS.get(name)
    if job is None or event is None:
        return {"ok": False, "message": "没有正在运行的实体统计任务"}
    event.set()
    return {"ok": True, "message": "已请求停止，当前块跑完即停；已完成的块会保留"}


def entities_status(paths: Paths, name: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(f"entities:{name}")
        live = _ENTITY_LIVE.get(name)
    running = job is not None and job.thread.is_alive()
    payload: dict[str, Any] = {
        "running": running,
        "has_result": entities_latest(paths, name) is not None,
    }
    if live is not None:
        payload["progress"] = {
            "done": live.get("done", 0),
            "total": live.get("total", 0),
            "label": live.get("label", ""),
        }
        payload["error"] = live.get("error", "")
        payload["pending"] = live.get("pending", 0)
        payload["stopped"] = bool(live.get("stopped"))
    return payload


# ── 大纲 ────────────────────────────────────────────────────
#
# 和标注一样：「计划」免费，「执行」要用户点。原料是标注顺带产出的逐章梗概，
# 所以没跑过标注时这里会明说缺原料，而不是编一份大纲出来。


def outline_plan(
    paths: Paths,
    name: str,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    provider_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    from workshop.batch import _pick_price, load_chapter_tasks, price_bucket
    from workshop.config import load_config
    from workshop.outline import build_outline_plan, missing_detail

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")

    model_cfg = provider.model(model_id) or {}
    bucket = price_bucket(provider)
    plan = build_outline_plan(
        work_name=name,
        provider_id=provider.id,
        model_id=model_id,
        tasks=tasks,
        annotations_dir=paths.annotations_dir(name),
        block_size=block_size,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
        max_output_tokens=_model_max_tokens(model_cfg),
    )
    payload = plan.to_dict()
    payload["missing_detail"] = missing_detail(plan.missing_reasons)
    payload["provider_name"] = provider.name
    payload["has_api_key"] = bool(_read_api_key(cfg, provider))
    # 可选模型必须列出来。**模型由用户显式选**——档位价差能到 4.5 倍，
    # 替用户默认一个贵 4.5 倍的，不合理。
    payload["available_models"] = [
        {"id": m.get("id"), "alias": m.get("alias") or m.get("id"), "role": m.get("role")}
        for m in provider.models
    ]
    payload["latest"] = outline_latest(paths, name) is not None
    return payload


def outline_latest(paths: Paths, name: str) -> dict[str, Any] | None:
    path = paths.work_dir(name) / "40-outline" / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def start_outline(
    paths: Paths,
    name: str,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    provider_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """启动一次大纲生成。会真的产生费用。"""
    from workshop.batch import _pick_price, load_chapter_tasks, price_bucket
    from workshop.config import load_config
    from workshop.llm import OpenAICompatProvider
    from workshop.outline import (
        OutlineOptions,
        build_outline_plan,
        generate_outline,
        save_outline,
    )
    from workshop.secrets import SecretStore, setup_logging

    with _JOBS_LOCK:
        existing = _JOBS.get(f"outline:{name}")
        if existing is not None and existing.thread.is_alive():
            raise RuntimeError(f"作品「{name}」已经有一个大纲任务在跑")

    cfg = load_config(paths.root / "providers.yaml")
    provider, model_id = _resolve_provider_and_model(cfg, provider_id, model_id)
    tasks = load_chapter_tasks(paths.ingest_dir(name))
    if not tasks:
        raise FileNotFoundError(f"作品「{name}」还没有章节数据")

    model_cfg = provider.model(model_id) or {}
    bucket = price_bucket(provider)
    plan = build_outline_plan(
        work_name=name,
        provider_id=provider.id,
        model_id=model_id,
        tasks=tasks,
        annotations_dir=paths.annotations_dir(name),
        block_size=block_size,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
        max_output_tokens=_model_max_tokens(model_cfg),
    )
    if not plan.blocks:
        raise ValueError(plan.warnings[0] if plan.warnings else "没有可归约的逐章梗概，请先跑标注")

    store = SecretStore(cfg.reports_dir.parent)
    setup_logging(store.known_values)
    api_key = _read_api_key(cfg, provider)
    if provider.api_key_ref and not api_key:
        raise ValueError(f"读不到密钥，请设置环境变量 {provider.api_key_ref}")

    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=api_key,
        timeout_sec=120,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
        rate_limit=provider.rate_limit,
    )
    out_dir = paths.work_dir(name) / "40-outline"

    job_state: dict[str, Any] = {"running": True, "done": 0, "total": len(plan.blocks), "label": "", "error": ""}

    def _worker() -> None:
        try:
            result = generate_outline(
                plan=plan,
                client=client,
                annotations_dir=paths.annotations_dir(name),
                opts=OutlineOptions(block_size=block_size, max_tokens=_model_max_tokens(model_cfg)),
                secrets=store.known_values,
                on_progress=lambda i, total, label: job_state.update(
                    {"done": i, "total": total, "label": label}
                ),
                state_path=out_dir / "_state.json",
            )
            save_outline(paths.work_dir(name), result, plan)
            job_state["error"] = "；".join(result.errors[:3])
        except Exception as exc:  # noqa: BLE001
            # 线程里的异常不会冒到界面上，必须写进状态，否则用户只看到一直转圈
            job_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job_state["running"] = False
            with _JOBS_LOCK:
                _JOBS.pop(f"outline:{name}", None)

    thread = threading.Thread(target=_worker, name=f"outline-{name}", daemon=True)
    with _JOBS_LOCK:
        _JOBS[f"outline:{name}"] = BatchJob(thread=thread, runner=None, work=name)
    thread.start()
    return {"ok": True, "work": name, "blocks": len(plan.blocks), "state": job_state}


def outline_status(paths: Paths, name: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(f"outline:{name}")
    if job is not None and job.thread.is_alive():
        return {"running": True, "has_result": outline_latest(paths, name) is not None}
    return {"running": False, "has_result": outline_latest(paths, name) is not None}


# ── 结构体检报告 ────────────────────────────────────────


def health_report(paths: Paths, name: str, *, stale_threshold: int = 30) -> dict[str, Any]:
    """生成体检报告。纯本地聚合，不调模型。"""
    from workshop.analysis import build_report, save_report

    report = build_report(
        work_name=name,
        ingest_dir=paths.ingest_dir(name),
        annotations_dir=paths.annotations_dir(name),
        ledger_path=paths.ledger_path(name),
        stale_threshold=stale_threshold,
    )
    save_report(report, paths.reports_dir(name))
    return report.to_dict()


def latest_report(paths: Paths, name: str) -> dict[str, Any] | None:
    path = paths.reports_dir(name) / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


# ── 内部 ────────────────────────────────────────────────────


def _chapter_brief(ch: Chapter) -> dict[str, Any]:
    return {
        "id": ch.chapter_id,
        "chapter_no": ch.chapter_no,
        "title": ch.title,
        "chars": ch.raw_chars,
        "line_no": ch.line_no,
        "status": ch.status,
        "unnumbered": ch.chapter_no is None,
    }


def _anomaly_counts(anomalies: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in anomalies:
        kind = str(item.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _mtime(path: Path) -> str:
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except OSError:
        return ""
