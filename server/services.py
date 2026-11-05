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

import yaml

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

    @property
    def index_dir(self) -> Path:
        """全文索引目录。派生数据，不进版本管理，删掉即可重建。"""
        return self.root / ".index"

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
            # 没有录入清单有两种：正在导入的，和「原创」工作区（从零写，不走录入）。
            # 后者要有自己的入口，否则它只会显示成一部空作品，点进去还 404。
            from workshop.creation import SETTING_BASENAME, SETTING_DIR

            is_original = (entry / SETTING_DIR / SETTING_BASENAME).exists()
            counts: dict[str, Any] = {"volumes": 0, "chapters": 0, "briefs": 0}
            if is_original:
                try:
                    from workshop import chapter_brief as _cb
                    from workshop import volume as _vo

                    vols, _b = _vo.load_all_volumes(entry / "70-volume")
                    briefs, _bb = _cb.load_all_briefs(entry / "80-brief")
                    counts = {
                        "volumes": len(vols),
                        "chapters": sum(
                            len((v.get("vol") or {}).get("chapters") or []) for v in vols.values()
                        ),
                        "briefs": len(briefs),
                    }
                except (ValueError, OSError):
                    pass
            out.append(
                {
                    "name": entry.name,
                    "dir_name": entry.name,
                    "kind": "original" if is_original else "ingesting",
                    "imported": False,
                    "chapters": counts["chapters"],
                    "chars": 0,
                    "segments": counts["volumes"],
                    "anomalies": 0,
                    "annotated": 0,
                    "creation": counts,
                    "updated_at": _mtime(entry / "60-setting" / "setting.yaml")
                    if is_original
                    else _mtime(entry),
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
        # 原创作品没有录入清单——它的概览页就是创作台。返回一个最小可用的详情，
        # 免得「点进作品就 404」，而 404 页面又不会告诉你为什么。
        from workshop.creation import SETTING_BASENAME, SETTING_DIR

        if (paths.work_dir(name) / SETTING_DIR / SETTING_BASENAME).exists():
            return {
                "name": name,
                "dir_name": name,
                "kind": "original",
                "imported": False,
                "statistics": {},
                "source": {},
                "integrity": {},
                "volumes": [],
            }
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


def _resolve_stage(cfg, stage: str, requested_provider: str | None = None,
                   requested_model: str | None = None):
    """按阶段解析模型（E2）。返回 (provider, model, 解析说明)。

    说明里带 `source`/`source_label`，界面据此解释「这个模型是阶段配的，还是沿用默认绑定」。
    """
    from workshop.routing import resolve

    effective = resolve(
        cfg, stage, requested_provider=requested_provider, requested_model=requested_model
    )
    return effective["provider"], effective["model"], {
        "stage": effective["stage"],
        "source": effective["source"],
        "source_label": effective["source_label"],
    }


def _stage_summary(meta: dict[str, Any], provider, model: str) -> dict[str, Any]:
    """给计划/任务用的阶段说明：前端要把「本阶段实际模型」显示出来。"""
    return {
        "stage": meta.get("stage"),
        "source": meta.get("source"),
        "source_label": meta.get("source_label"),
        "provider": provider.id,
        "provider_name": provider.name,
        "model": model,
    }


def _build_client(cfg, provider, store, *, timeout_sec: float = 300):
    """按服务商建 OpenAI 兼容客户端。正文与细纲可能用不同服务商，所以要能分别建。"""
    from workshop.llm import OpenAICompatProvider

    return OpenAICompatProvider(
        base_url=provider.base_url, api_key=_read_api_key(cfg, provider),
        timeout_sec=timeout_sec, auth_scheme=provider.auth_scheme,
        secrets=store.known_values, rate_limit=provider.rate_limit,
    )


def _require_key(cfg, provider, stage_label: str) -> None:
    if provider.api_key_ref and not _read_api_key(cfg, provider):
        raise ValueError(f"「{stage_label}」用的服务商「{provider.name}」还没有配置密钥。")


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
        raise ValueError(_no_key_msg(provider))

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
    from workshop import routing

    # U6：每个阶段实际用哪个模型、来源、能不能用、有什么风险。
    # 密钥状态从同一个 SecretStore 判断，界面看到的和调用时读到的是同一份。
    stage_rows = routing.matrix(
        cfg,
        key_configured=lambda ref: True if not ref else bool(store.get(ref)),
    )
    from workshop.prompt_registry import catalog as prompt_catalog

    return {
        "providers": items,
        "binding": {"provider": binding.get("provider"), "model": binding.get("model")},
        "stages": stage_rows,
        "prompts": prompt_catalog(),
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


def prompts_catalog(paths: Paths, *, work: str | None = None) -> dict[str, Any]:
    """E1：提示词目录。带来源（内置/全局/项目）与占位符，设置页据此显示是否被覆盖。"""
    from workshop.prompt_registry import catalog

    work_dir = paths.work_dir(work) if work else None
    rows = catalog(work_dir=work_dir)
    return {
        "work": work or None,
        "prompts": rows,
        "levels": ["项目（作品/prompts）", "全局（数据根/prompts）", "内置（随程序发布）"],
    }


def save_stage_binding(
    paths: Paths, *, stage: str, provider: str = "", model: str = "", clear: bool = False,
) -> dict[str, Any]:
    """E2/U6：给某一阶段绑定模型。保存前严格校验，保存后重新解析一遍作为「生效确认」。

    清空（clear=True）不是删除整个 stages 段，而是移除这一阶段的覆盖，
    让它回落到默认任务绑定——界面上的「恢复默认」走这条。
    """
    from workshop import routing
    from workshop.config import load_config, save_custom_config

    cfg = load_config(paths.root / "providers.yaml")
    if stage not in routing.STAGE_IDS:
        raise ValueError(f"未知阶段「{stage}」，可选：{', '.join(routing.STAGE_IDS)}")

    custom = _load_custom(paths)
    bindings = dict(custom.get("task_bindings") or {})
    stages = dict(bindings.get("stages") or {})

    if clear:
        stages.pop(stage, None)
    else:
        routing.validate(cfg, stage, provider, model)
        stages[stage] = {"provider": provider, "model": model}

    if stages:
        bindings["stages"] = stages
    else:
        bindings.pop("stages", None)
    if bindings:
        custom["task_bindings"] = bindings
    else:
        custom.pop("task_bindings", None)
    save_custom_config(paths.root / "providers.yaml", custom)

    # 生效确认：从磁盘重新加载，重新解析这一阶段。写进去了但解析不到，是静默失效。
    fresh = load_config(paths.root / "providers.yaml")
    store = _secret_store(paths)
    rows = routing.matrix(fresh, key_configured=lambda ref: True if not ref else bool(store.get(ref)))
    row = next((r for r in rows if r["stage"] == stage), None)
    if row is None:
        raise ValueError(f"保存后读不到阶段「{stage}」，配置可能被写坏了。")
    if not clear and not row.get("provider"):
        raise ValueError(
            f"保存后这一阶段仍然没有可用服务商：{row.get('unavailable_reason') or '未知原因'}"
        )
    return {
        "ok": True,
        "stage": stage,
        "cleared": clear,
        "effective": row,
        "stages": rows,
        "binding": {
            "provider": (fresh.task_bindings or {}).get("chapter_annotation", {}).get("provider"),
            "model": (fresh.task_bindings or {}).get("chapter_annotation", {}).get("model"),
        },
        "hint": (
            "已恢复默认任务绑定，这一阶段不再单独指定模型。"
            if clear
            else f"已生效：{row.get('provider_name')} / {row.get('model')}"
            + ("" if row.get("available") else f"（当前不可用：{row.get('unavailable_reason')}）")
        ),
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
        raise ValueError(_no_key_msg(provider))

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
    from workshop.entities import (
        ALIASES_BASENAME,
        EntitiesOptions,
        build_entities_plan,
        generate_entities,
        load_aliases,
        save_entities,
    )
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
        raise ValueError(_no_key_msg(provider))
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
                aliases=load_aliases(paths.work_dir(name) / ALIASES_BASENAME),
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
        raise ValueError(_no_key_msg(provider))

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


# ── M9 创作台 ────────────────────────────────────────────────
#
# 三层都是人写的真源（设定集 / 分卷目录 / 逐章创作任务指令），界面只做两件事：
# 把原文给他改，把校验结果回给他。**没有任何"一键全生成"**——创作要一步步调，
# 一次吐一大堆只会在返工时全变成无用功。


CREATION_KINDS = ("setting", "volume", "brief")


def _creation_modules():
    from workshop import chapter_brief as cb
    from workshop import volume as vo
    from workshop.creation import BRIEF_DIR, SETTING_DIR, VOLUME_DIR

    return cb, vo, SETTING_DIR, VOLUME_DIR, BRIEF_DIR


def _creation_work_dir(paths: Paths, name: str) -> Path:
    wd = paths.work_dir(name)
    if not wd.exists():
        raise FileNotFoundError(f"作品目录不存在：{wd}")
    from workshop.creation import SETTING_DIR

    if not (wd / SETTING_DIR).exists():
        raise FileNotFoundError(
            f"「{name}」不是原创工作区（没有 {SETTING_DIR}/）。创作台只处理原创作品。"
        )
    return wd


def creation_overview(paths: Paths, name: str) -> dict[str, Any]:
    """创作台首页：三层各自的完成度 + 逐卷逐章清单。"""
    cb, vo, SETTING_DIR, VOLUME_DIR, BRIEF_DIR = _creation_modules()
    from workshop.creation import load_work_setting, setting_path_of, validate_setting

    wd = _creation_work_dir(paths, name)
    setting_path = setting_path_of(wd)
    setting = None
    setting_view: dict[str, Any] = {"exists": setting_path.exists(), "path": str(setting_path)}
    if setting_view["exists"]:
        try:
            setting = load_work_setting(wd)
            s_errors, s_warnings = validate_setting(setting)
        except (ValueError, OSError) as exc:
            s_errors, s_warnings = [f"设定集读不了：{exc}"], []
        setting_view.update(
            {
                "characters": len(setting.get("characters") or []),
                "errors": s_errors,
                "warnings": s_warnings,
                "done": not s_errors,
            }
        )

    volumes, broken_volumes = vo.load_all_volumes(wd / VOLUME_DIR)
    volume_rows: list[dict[str, Any]] = []
    errors: list[str] = [f"分卷目录：{b}" for b in broken_volumes]
    warnings: list[str] = [f"设定集：{w}" for w in setting_view.get("warnings") or []]
    errors += [f"设定集：{e}" for e in setting_view.get("errors") or []]

    for no in sorted(volumes):
        vol = volumes[no].get("vol") or {}
        v_errors, v_warnings = vo.validate_volume(volumes[no], setting=setting, file_vol_no=no)
        errors += [f"第{no}卷：{e}" for e in v_errors]
        warnings += [f"第{no}卷：{w}" for w in v_warnings]
        volume_rows.append(
            {
                "vol": no,
                "file": volumes[no].get("_file"),
                "title": vol.get("title") or "",
                "start_chapter": vol.get("start_chapter"),
                "end_chapter": vol.get("end_chapter"),
                "chapters": len(vol.get("chapters") or []),
                "errors": len(v_errors),
                "warnings": len(v_warnings),
            }
        )
    chain_errors, chain_warnings = (
        vo.validate_chain(volumes, setting=setting) if volumes else ([], [])
    )
    errors += chain_errors
    warnings += chain_warnings

    briefs, broken_briefs = cb.load_all_briefs(wd / BRIEF_DIR)
    errors += [f"创作任务指令：{b}" for b in broken_briefs]
    brief_errors: dict[str, list[str]] = {}
    brief_warnings: dict[str, list[str]] = {}
    for cid in sorted(briefs):
        parsed = cb.parse_chapter_id(cid)
        e, w = cb.validate_brief(
            briefs[cid], setting=setting,
            volume=volumes.get(parsed[0]) if parsed else None,
        )
        brief_errors[cid] = e
        brief_warnings[cid] = w
        errors += [f"{cid}：{x}" for x in e]
        warnings += [f"{cid}：{x}" for x in w]
    for item in cb.validate_brief_chain(briefs)[0] if briefs else []:
        errors.append(f"创作任务指令：{item}")

    # 左侧章节导航要显示每章状态。逐个另发三个请求会让首屏多几十次往返，
    # 这里顺手把状态文件读出来一起返回——都是几十字节的小 JSON。
    #
    # P2 起改为先查派生元数据库（.index/meta.db）。它只存由文件重建出来的
    # 状态/计数，读之前先按 mtime+size+sha 增量同步，所以手改文件也会立刻纠正。
    # 库损坏或不存在时 `states is None`，整体退回下面的现读文件分支。
    from workshop import metadata as _metadata

    states: dict[str, dict[str, Any]] | None = None
    try:
        if _metadata.sync(paths.root, work=name, scope="states").get("ok"):
            states = _metadata.chapter_states(paths.root, name)
    except (OSError, ValueError):
        states = None

    from workshop import draft_store as _draft_store_mod
    from workshop import notes as _notes_mod
    from workshop import review as _review_mod

    chapters: list[dict[str, Any]] = []
    for no in sorted(volumes):
        vol = volumes[no].get("vol") or {}
        for row in vol.get("chapters") or []:
            if not isinstance(row, dict) or not isinstance(row.get("chapter_no"), int):
                continue
            cid = cb.chapter_id_of(no, row["chapter_no"])
            if states is not None:
                state = states.get(cid) or {}
                has_draft = bool(state.get("draft_exists"))
                draft_status = str(state.get("status") or "draft")
                pending_notes = int(state.get("notes_pending") or 0)
                open_reviews = int(state.get("review_open") or 0)
            else:
                has_draft = (wd / "90-draft" / f"{cid}.md").exists()
                try:
                    draft_status = _draft_store_mod.get_status(wd, cid).get("status") or "draft"
                except (OSError, ValueError):
                    draft_status = "draft"
                try:
                    notes_payload = _notes_mod.load_notes(wd, cid)
                    pending_notes = len(
                        [x for x in (notes_payload.get("notes") or [])
                         if x.get("status") == "pending"]
                    )
                except (OSError, ValueError):
                    pending_notes = 0
                try:
                    review_payload = _review_mod.load_reviews(wd, cid)
                    open_reviews = len(
                        [x for x in _review_mod.all_items(review_payload)
                         if x.get("status") == "open"]
                    )
                except (OSError, ValueError):
                    open_reviews = 0
            chapters.append(
                {
                    "chapter_id": cid,
                    "vol": no,
                    "chapter_no": row["chapter_no"],
                    "title": row.get("title") or "",
                    "has_brief": cid in briefs,
                    "has_draft": has_draft,
                    "draft_status": draft_status,
                    "pending_notes": pending_notes,
                    "open_reviews": open_reviews,
                    "errors": len(brief_errors.get(cid, [])),
                    "warnings": len(brief_warnings.get(cid, [])),
                }
            )
    missing = [c for c in chapters if not c["has_brief"]]
    if missing:
        errors.append(
            f"逐章指令只填了 {len(chapters) - len(missing)}/{len(chapters)} 章，"
            f"还缺 {len(missing)} 章"
        )

    # 全书级硬闸：主角缺席、卷间接不上。闸要并进 errors 才算真的拦住。
    #
    # 设定集/卷表/逐章指令在上面的流程里已经全部解析过一遍；这里把解析结果
    # 传下去，避免同一批 YAML 在梗概、人物动向、一致性、硬闸里各解析一次
    # （200 章时这是最大的一笔纯浪费）。
    from workshop.creation import render_synopsis, story_gates

    _gate_errors, _gate_warnings = story_gates(
        wd, setting=setting, volumes=volumes, briefs=briefs
    )
    errors = list(errors) + list(_gate_errors)
    warnings = list(warnings) + list(_gate_warnings)
    return {
        "foreshadows": foreshadow_ledger(paths, name, briefs=briefs),
        "synopsis": render_synopsis(wd, setting=setting, volumes=volumes),
        "cast": creation_character_presence(paths, name, briefs=briefs, volumes=volumes),
        "consistency": creation_consistency(
            paths, name, setting=setting, volumes=volumes, briefs=briefs
        ),
        "work": name,
        "ok": not errors,
        "setting": setting_view,
        "volumes": volume_rows,
        "chapters": chapters,
        "counts": {
            "volumes": len(volume_rows),
            "chapters": len(chapters),
            "briefs": len(briefs),
            "missing_briefs": len(missing),
        },
        "errors": errors,
        "warnings": warnings,
    }


def _seed_brief(wd: Path, name: str, chapter_id: str) -> dict[str, Any]:
    """给还没建过的这一章拼一份骨架。

    ⚠️ 这件事**本来就该发生**：`chapter_brief.empty_brief()` 的参数表（title / gist /
    perspective）几乎和卷表那条章节记录一一对应，它就是为这件事写的——但全仓库
    零个调用点，是死代码。结果第三步「新建一章」是一张白纸：作者得凭记忆把前两层
    写过的东西重抄一遍，抄错了自己都不知道（作者 2026-09-29 报的）。
    """
    cb, vo, _setting_dir, VOLUME_DIR, BRIEF_DIR = _creation_modules()
    from workshop.creation import load_work_setting

    parsed = cb.parse_chapter_id(chapter_id)
    if not parsed:
        return {}
    vol_no, chapter_no = parsed
    # 卷表：这一章的章名、梗概、出场人物、伏笔都在这儿
    try:
        doc = vo.load_volume(vo.volume_path(wd / VOLUME_DIR, vol_no))
    except (OSError, ValueError):
        doc = {}
    chapters = (doc.get("vol") or {}).get("chapters") or []
    chapter = next((c for c in chapters if str(c.get("chapter_no")) == str(chapter_no)), {})
    # 设定集：视角是全书的，卷表里没有
    try:
        setting = load_work_setting(wd)
    except (OSError, ValueError):
        setting = {}
    data = cb.empty_brief(
        name, vol_no, chapter_no,
        title=str(chapter.get("title") or ""),
        gist=str(chapter.get("gist") or ""),
        perspective=str((setting.get("style") or {}).get("perspective") or ""),
    )
    data["settings"]["characters"] = _names(chapter.get("characters"))
    # ⚠️ core_plot 是**阻断项**（「core_plot 是空的——没有核心情节，写正文的人只能自己编」），
    # 而卷表那一格 gist 本来就是「这一章发生什么」——同一份东西的不同粒度。
    # 不给它，作者打开这页先吃一条红色阻断，还得把卷表里的情节重抄一遍（作者 2026-09-29 报的）。
    beats = _beats(chapter.get("gist"))
    data["core_plot"] = beats
    # 一句话概要就取首句。原来整段 gist 塞进去，一格「一句话概要」里躺着三段话，
    # 而下面 core_plot 是空的——正好反了。
    if beats:
        data["one_line"] = beats[0]
    # ⚠️ 卷表那条 `foreshadow` 是**自由文本**，分不出哪条是埋设、哪条是回收，一律当「埋设」。
    # 猜错会污染伏笔台账（ledger 读的就是这个字段），所以这一格作者必须自己过一眼。
    data["foreshadow"] = [
        {"action": "埋设", "id": f"{chapter_id}-f{i:02d}", "desc": text}
        for i, text in enumerate(_split_threads(chapter.get("foreshadow")), start=1)
    ]
    # 需承接：**上一章指令**里埋设过的伏笔。首章没有上一章，留空就对了——
    # 别拿卷表上一章的梗概去凑：那是那一章自己的料，不是这一章要接的东西。
    data["carry_over"] = (
        _open_threads_of(wd / BRIEF_DIR, cb, cb.chapter_id_of(vol_no, chapter_no - 1))
        if chapter_no > 1 else []
    )
    return data


def _beats(text: Any) -> list[str]:
    """把卷表的 gist 拆成一条一个情节节拍。

    句末标点（。！？；和换行）切刀，标点留着——读起来还是一句完整的话。
    拆不出多句时（整段就一句），原样一条返回。
    """
    raw = str(text or "").replace("\r", " ")
    for sep in ("\n", "；", ";", "！", "？", "!", "?"):
        raw = raw.replace(sep, "。")
    return [part.strip() + "。" for part in raw.split("。") if part.strip()]


def _names(value: Any) -> list[str]:
    """卷表里的人物可能是一张列表，也可能是「主角、挑夫」这样一条字符串——
    白名单把 characters 标成 list_fields，但 YAML 里手写和模型给的都是字符串。

    ⚠️ 字符串必须**按顿号拆**，不能直接遍历：遍历会得到「主」「角」「、」「挑」「夫」，
    而它看起来还有模有样，不核对根本发现不了（实测 2026-09-29）。
    """
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    raw = str(value or "")
    for sep in ("，", ",", "；", ";", "/"):
        raw = raw.replace(sep, "、")
    return [part.strip() for part in raw.split("、") if part.strip()]


def _split_threads(text: Any) -> list[str]:
    """把卷表里那一条自由文本拆成几条。中文分号、英文分号、换行都算分隔。"""
    raw = str(text or "").replace("\n", "；").replace(";", "；")
    return [part.strip() for part in raw.split("；") if part.strip()]


def _open_threads_of(brief_dir: Path, cb: Any, chapter_id: str) -> list[str]:
    """上一章指令里「埋设」过的伏笔。上一章还没建指令就返回空——不是错误。"""
    if not chapter_id:
        return []
    path = cb.brief_path(brief_dir, chapter_id)
    if not path.exists():
        return []
    try:
        prev = cb.load_brief(path)
    except (OSError, ValueError):
        return []
    return [
        str(f.get("desc"))
        for f in (prev.get("foreshadow") or [])
        if str(f.get("action") or "") == "埋设" and str(f.get("desc") or "").strip()
    ]


def _no_key_msg(provider: Any) -> str:
    """密钥读不到时该说什么。

    ⚠️ 原来只说「请设置环境变量 DEEPSEEK_API_KEY」——**对打包版的用户是死路**：
    双击 exe 的人不会去设环境变量，他需要知道的是「往哪个文件里填」。
    打包版数据根里连 config/secrets.json 都不存在，所以他照着那句提示做，
    在文件系统里找不到任何对应的东西（作者 2026-09-29 卡在这儿）。
    """
    ref = getattr(provider, "api_key_ref", "DEEPSEEK_API_KEY")
    return (
        f"读不到密钥 {ref}。在数据目录下的 config/secrets.json 里填 "
        f'{{"{ref}": "你的密钥"}}，或者设置同名环境变量 {ref}。'
    )


def creation_read(paths: Paths, name: str, kind: str, key: str = "") -> dict[str, Any]:
    """读一份原文 + 它的校验结果。"""
    wd = _creation_work_dir(paths, name)
    path = _creation_path(wd, kind, key)
    if not path.exists():
        out: dict[str, Any] = {"exists": False, "path": str(path), "text": "",
                               "errors": [], "warnings": []}
        # 指令层没建过就**不是空壳**：拿卷表和设定集把骨架拼出来。前端照常渲染成表单，
        # 作者在别人写好的基础上改，而不是从零抄一遍。exists 仍然是 false——
        # 「磁盘上还没有这份文件」这个事实不能改，保存路径还要靠它。
        if kind == "brief" and key:
            out["data"] = _seed_brief(wd, name, key)
            # 明确告诉前端「这份是我拼的、没落过盘」。前端拿它去标记「有未保存的改动」——
            # 靠 !exists && data 非空去猜也行，但那是猜；多一个字段省掉一次误判。
            out["seeded"] = bool(out["data"])
        else:
            out["data"] = {}
        return out
    text = path.read_text(encoding="utf-8")
    errors, warnings = _creation_validate_text(wd, kind, key, text)
    out = {"exists": True, "path": str(path), "text": text,
           "errors": errors, "warnings": warnings}
    # 表单要的是结构化的那份。三层都要给——只给 setting 那一层的话，卷层和指令层
    # 的表单会拿到空对象，然后「保存」把磁盘上好好的文件清成 {}。
    # 这个 bug 实测发生过一次，是数据丢失级别的，别再只给一层。
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed = None
    out["data"] = parsed if isinstance(parsed, dict) else {}
    # 文件已经存在、但 core_plot 是空的：这一章多半是在「预填」上线**之前**就建出来的，
    # 于是永远停在一条阻断上——播种只在文件不存在时跑，够不着它（作者 2026-09-29 报的）。
    # 把「照卷表补出来该是什么样」一起发回去，页面给一个按钮，**作者点了才动他的文件**。
    # 不在这儿自动合并：那是他的文件，我们只负责把选项摆出来。
    if (kind == "brief" and key and not (out["data"].get("core_plot") or [])):
        seed = _seed_brief(wd, name, key)
        if seed.get("core_plot"):
            out["suggest"] = seed
    return out

def creation_write(paths: Paths, name: str, kind: str, key: str, text: str) -> dict[str, Any]:
    """保存一份原文。

    先解析再落盘：解析不了就**不写**，把错误原样回给用户。存进去一份读不回来的文件，
    比拒收它坏得多——那份文件会一直躺在那里，直到生成阶段才发现。
    落盘写的是**用户自己那段文本**，不是重新 dump 的 YAML，注释和排版都留着。
    """
    wd = _creation_work_dir(paths, name)
    path = _creation_path(wd, kind, key)
    errors, warnings = _creation_validate_text(wd, kind, key, text, allow_unparsable=False)
    fatal = [e for e in errors if e.startswith("解析不了")]
    if fatal:
        return {"saved": False, "path": str(path), "errors": errors, "warnings": warnings}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"saved": True, "path": str(path), "errors": errors, "warnings": warnings}


def creation_write_setting_data(
    paths: Paths, name: str, data: dict[str, Any], *, kind: str = "setting", key: str = ""
) -> dict[str, Any]:
    """表单保存：把结构化文档写成 YAML。三层共用。

    ⚠️ 写出来的是机器生成的 YAML，**手写注释会没有**。所以文件头会挂一句说明，
    并告诉作者想保留注释就切回「原文编辑」。不写这句的话，作者只会发现注释莫名消失了。
    """
    wd = _creation_work_dir(paths, name)
    path = _creation_path(wd, kind, key)

    # ⚠️ 数据保护：拿到的草稿是空的，而磁盘上这份文件有内容 → 拒绝写入。
    # 空草稿从来不是作者的本意（表单里删光所有字段 ≠ 要一个空文件），
    # 它几乎总是读取环节出了问题。上一版没有这道闸，卷层和指令层被清成过 {}。
    if path.exists():
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            existing = None
        if isinstance(existing, dict):
            real_keys = [k for k in existing if k not in ("schema_version", "work")]
            incoming_keys = [k for k in (data or {}) if k not in ("schema_version", "work")]
            if real_keys and not incoming_keys:
                raise ValueError(
                    f"表单拿到的是空内容，但磁盘上这份 {kind} 文件是有东西的。"
                    "这多半是读取出了问题，已拒绝写入，免得把内容清空。"
                    "要真的清空，请切到「原文编辑」自己删。"
                )

    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
    header = (
        "# 这份文件由创作台「表单」保存生成；手写注释会被表单保存覆盖。\n"
        "# 想保留自己写的注释，就切到「原文编辑」改。\n"
    )
    text = header + body
    errors, warnings = _creation_validate_text(wd, kind, key, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"saved": True, "path": str(path), "errors": errors, "warnings": warnings, "text": text}


def _creation_path(wd: Path, kind: str, key: str) -> Path:
    """把 (kind, key) 映射成文件路径。

    这里是一道信任边界：key 直接来自 URL，**必须**先过白名单再拼路径，
    否则 `../` 能写到工作区外面去。
    """
    cb, vo, SETTING_DIR, VOLUME_DIR, BRIEF_DIR = _creation_modules()
    if kind not in CREATION_KINDS:
        raise ValueError(f"不认识的类型：{kind}")
    if kind == "setting":
        from workshop.creation import SETTING_BASENAME

        if key:
            # 设定集只有一份，没有 key 的概念。传了多半是客户端搞错了，直接说出来，
            # 别默默忽略——被忽略的参数是 bug 的藏身处。
            raise ValueError("setting 只有一份，不接受 key")
        return wd / SETTING_DIR / SETTING_BASENAME
    if kind == "volume":
        if not key.isdigit() or not (1 <= int(key) <= 9999):
            raise ValueError(f"卷号必须是 1-9999 的数字：{key!r}")
        return vo.volume_path(wd / VOLUME_DIR, int(key))
    if cb.parse_chapter_id(key) is None:
        raise ValueError(f"章节 id 不合约定（应为 v001-c0001）：{key!r}")
    return cb.brief_path(wd / BRIEF_DIR, key)


def _creation_validate_text(
    wd: Path, kind: str, key: str, text: str, *, allow_unparsable: bool = True
) -> tuple[list[str], list[str]]:
    cb, vo, _s, VOLUME_DIR, _b = _creation_modules()
    from workshop.creation import load_work_setting, setting_path_of, validate_setting

    try:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("顶层不是映射（要是一组 key: value）")
    except (yaml.YAMLError, ValueError) as exc:
        return [f"解析不了：{exc}"], []

    setting = None
    if setting_path_of(wd).exists():
        try:
            setting = load_work_setting(wd)
        except (ValueError, OSError):
            setting = None

    if kind == "setting":
        return validate_setting(data)
    if kind == "volume":
        volumes, _broken = vo.load_all_volumes(wd / VOLUME_DIR)
        file_vol_no = int(key) if key.isdigit() else None
        errors, warnings = vo.validate_volume(data, setting=setting, file_vol_no=file_vol_no)
        # 把「只有跨卷才看得出来」的问题也一并回给这一份，编辑完就知道有没有把邻卷碰坏
        merged = dict(volumes)
        if file_vol_no is not None:
            merged[file_vol_no] = data
        chain_errors, chain_warnings = vo.validate_chain(merged, setting=setting) if merged else ([], [])
        return errors + chain_errors, warnings + chain_warnings

    volumes, _broken = vo.load_all_volumes(wd / VOLUME_DIR)
    parsed = cb.parse_chapter_id(key)
    errors, warnings = cb.validate_brief(
        data, setting=setting, volume=volumes.get(parsed[0]) if parsed else None
    )
    return errors, warnings


def creation_new(paths: Paths, name: str, **kwargs: Any) -> dict[str, Any]:
    from workshop.creation import create_original_work

    info = create_original_work(paths.workspaces, name, **kwargs)
    return {
        "work": name,
        "work_dir": str(info["work_dir"]),
        "setting": str(info["setting"]),
        "first_volume": str(info["first_volume"]),
    }


def creation_seed_briefs(paths: Paths, name: str, vol: int | None = None) -> dict[str, Any]:
    from workshop.creation import seed_briefs

    wd = _creation_work_dir(paths, name)
    return seed_briefs(wd, vol_no=vol)


# ── M9 创作台 · 设定集助手 ──────────────────────────────────
#
# 形态是**提案制**：模型只提「建议新增/修改哪几条」，作者逐条勾选，
# 勾中的进界面草稿，作者再点保存才落盘。这里不替作者做决定，也不整份重写。


# 每个档位真正吃的是哪份数据。以前三档共用一个「K1 非空」的判据，
# 于是只想借节奏（k3 只用结构指纹）的作品也被卡住，而放开它要跑实体统计——
# 那是这条链上最贵的一步（读全本调模型），却对结构指纹一点贡献都没有。
REFS_LEVEL_NEEDS: dict[str, str] = {"none": "", "k3": "k3", "full": "k1"}

# 「不可用」要说清缺的是哪一步。只说「没知识库」的话，
# 作者会去点「构建知识库」，而点完仍然不可用——缺的根本不是那一下。
REFS_MISSING_HINTS: dict[str, str] = {
    "k1": "没跑实体统计，给不出人物/势力/能力",
    "k3": "标注不足 5 章，给不出结构指纹",
}


def _setting_refs_block(
    paths: Paths, refs: list[str], level: str = "k3"
) -> tuple[str, list[str]]:
    """把作者选定的已入库作品拼成参照素材。

    `level` 决定给多少：none 什么都不给 / k3 只给结构指纹 / full 再加人物势力能力等。
    就绪判据**跟着档位走**（见 REFS_LEVEL_NEEDS）：k3 只要指纹，full 才要 K1。
    读不到或本档给不出东西的会被跳过并说明——列出来点了却没反应的选项比没有更烦人。
    """
    from workshop.setting_assist import build_refs_block

    need = REFS_LEVEL_NEEDS.get(level, "k1")
    picked: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in refs[:6]:
        kb_dir = paths.work_dir(name) / "20-kb"
        k1_path = kb_dir / "k1-entities.json"
        if not k1_path.exists():
            missing.append(f"{name}（还没构建过知识库）")
            continue
        try:
            k1 = json.loads(k1_path.read_text(encoding="utf-8-sig"))
            k3_raw = json.loads((kb_dir / "k3-fingerprint.json").read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            missing.append(f"{name}（知识库读不了）")
            continue
        if need == "k3" and not k3_raw.get("available"):
            missing.append(f"{name}（{REFS_MISSING_HINTS['k3']}）")
            continue
        if need == "k1" and not k1.get("available"):
            missing.append(f"{name}（{REFS_MISSING_HINTS['k1']}）")
            continue
        k3_lines = [
            f"{it.get('rule')}：{(it.get('statistic') or {}).get('mean') or (it.get('statistic') or {}).get('dominant') or ''}"
            f"（{it.get('sample')} 章）"
            for it in (k3_raw.get("items") or [])
            if isinstance(it, dict)
        ]
        picked.append({"work": name, "k1": k1, "k3": k3_lines})
    if level == "k3":
        # 中间档不需要 K1：连读都不读，免得把几百条人物装进内存又丢掉
        picked = [{"work": p["work"], "k3": p["k3"], "k1": {}} for p in picked]
    block = build_refs_block(picked, level=level)
    if picked and not block.strip():
        # 挑中的作品一条素材都拼不出来（指纹空 / 条目全被白名单丢掉）。
        # 这种时候前缀里少了这几千字，模型会自己补，而作者以为它是照着知识库写的。
        missing.append("选中的作品没拼出任何素材，这次等于没注入参照")
    return block, missing


def _kb_flag(path: Path) -> bool:
    """读一份知识库切片，问它「本档可用吗」。读不到就是不可用。"""
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8-sig")).get("available"))
    except (ValueError, OSError):
        return False


def assist_refs(paths: Paths) -> list[dict[str, Any]]:
    """可选的参照作品。只列真的能用的——列出来点了报错的选项比没有更烦人。

    就绪状态**按档位分开报**，不合成一个总开关：k3 只需要结构指纹（标注满 5 章），
    full 才需要 K1（跑过实体统计）。合成一个「有没有知识库」的话，
    只想借节奏的作者会被推去跑最贵的实体统计，而跑完指纹仍然是空的。
    """
    out: list[dict[str, Any]] = []
    for work in list_works(paths):
        if not work.get("imported"):
            continue
        kb_dir = paths.work_dir(work["dir_name"]) / "20-kb"
        has_kb = (kb_dir / "k1-entities.json").exists()
        k1_ready = _kb_flag(kb_dir / "k1-entities.json")
        k3_ready = _kb_flag(kb_dir / "k3-fingerprint.json")
        if not has_kb:
            missing = "还没构建过知识库"
        elif not k1_ready and not k3_ready:
            missing = "知识库是空的：没跑实体统计，标注也不足 5 章"
        elif not k3_ready:
            missing = REFS_MISSING_HINTS["k3"]
        elif not k1_ready:
            missing = REFS_MISSING_HINTS["k1"]
        else:
            missing = ""
        out.append(
            {
                "name": work["dir_name"],
                "label": work.get("name") or work["dir_name"],
                # 兼容旧语义：kb_ready 就是「有 K1 素材」，取素材面板按它筛
                "kb_ready": k1_ready,
                "k1_ready": k1_ready,
                "k3_ready": k3_ready,
                "missing": missing,
                "chapters": work.get("chapters") or 0,
            }
        )
    return out


def setting_assist_plan(
    paths: Paths, name: str, *, refs: list[str], level: str = "k3", layer: str = "setting",
    key: str = "", provider_id: str | None, model_id: str | None
) -> dict[str, Any]:
    """免费计划：这次对话大概多少 token、多少钱。**不发起调用。**"""
    from workshop.batch import _pick_price, price_bucket
    from workshop.config import load_config
    from workshop.setting_assist import build_assist_plan

    document = creation_document(paths, name, layer, key)
    cfg = load_config(paths.root / "providers.yaml")
    provider, model = _resolve_provider_and_model(cfg, provider_id, model_id)
    block, missing = _setting_refs_block(paths, refs, level)
    model_cfg = provider.model(model) or {}
    bucket = price_bucket(provider)
    plan = build_assist_plan(
        work=name, setting=document, refs_block=block, refs=refs, level=level, layer=layer,
        context_chars=len(_assist_context(paths, name, layer, key)),
        provider_id=provider.id, model_id=model,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket,
    ).to_dict()
    plan["refs_available"] = assist_refs(paths)
    plan["refs_missing"] = missing
    plan["model_id"] = model
    return plan


def creation_document(paths: Paths, name: str, layer: str, key: str = "") -> dict[str, Any]:
    """读一层的**结构化**文档，给提案引擎用。

    这里刻意不走各层自己的 load_* 函数：那些会补空默认值（vol=0、chapters=[]），
    而给模型看的「现在已经有什么」必须是**文件里真实存在的东西**——
    补一堆空壳进去，模型会以为作者已经定了章数或卷号。
    """
    wd = _creation_work_dir(paths, name)
    path = _creation_path(wd, layer, key)
    if not path.exists():
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def apply_setting_proposals(
    *, document: dict[str, Any], proposals: list[dict[str, Any]],
    accepted: list[int], layer: str = "setting"
) -> dict[str, Any]:
    """把勾中的提案并进**草稿**。不落盘——落盘是作者点保存之后的事。"""
    from workshop.setting_assist import apply_proposals

    new_document, results = apply_proposals(document, proposals, accepted=accepted, layer=layer)
    return {"document": new_document, "results": results}


def _assist_context(paths: Paths, name: str, layer: str, key: str = "") -> str:
    """「问助手」这次要喂进去的上下文：写前必读（锚点/前情/角色动态…）+ **完整设定卡**。

    设定卡（世界观硬规则、时代、地点、术语、禁忌、能力、主题）原来在卷表与逐章指令
    这两层**根本不在提示词里**：那时只给「写前必读」，而它只抄了力量体系、势力、文风三行。
    作者把设定写在世界观与术语表里，起草卷表时模型一个字都看不到，于是编出另一本书
    （2026-09-30 实测：科幻设定 + 题材「科幻未来」，排出来的卷表是怪谈那一套（老井、夜谈），
    出场人物一栏整排写着「主角」「同伴」——设定集里的名字一次都没用上）。

    顺序与写正文那条路一致：锚点压在最前面，设定卡跟在后面。
    """
    from workshop.creation import load_work_setting, render_injection_block, render_write_brief

    wd = _creation_work_dir(paths, name)
    chapter_no = 0
    brief_doc: dict[str, Any] | None = None
    if layer == "brief" and key:
        brief_doc = creation_document(paths, name, "brief", key) or {}
        chapter_no = int(brief_doc.get("chapter_no") or 0)

    parts: list[str] = []
    # 同一份章指令配置（C3 末段字数 / C4 未来蓝图数）在助手路径也要生效，
    # 否则作者在指令里选了「只看末段 500 字」，问助手时却还是 900 字。
    brief_block = render_write_brief(
        wd, chapter_no=chapter_no, prev_chapter_no=max(0, chapter_no - 1),
        brief=brief_doc,
    )
    if brief_block:
        parts.append(brief_block)
    # 设定集那一层不用再拼：它的「当前文档」本身就是这张卡，拼进去等于喂两遍。
    if layer != "setting":
        try:
            card = render_injection_block(load_work_setting(wd))
        except (OSError, ValueError):
            card = ""
        if card:
            parts.append("【设定集：这是唯一真源，人物与世界观都按它来】\n" + card)
    return "\n\n".join(parts)


def _split_section_pointer(layer: str, section: str) -> tuple[str, str]:
    """拆「层:小节」：**当前层认识这个小节，就按当前层解**；不认识才认前缀。

    回填写回三层时，小节名本来就带层前缀（`setting:abilities` / `volume:chapters` /
    `brief:foreshadow`），而这三个小节**都只存在于它自己那一层**，所以下面的顺序
    对它们毫无影响——前缀照样认。

    要治的是另一种：起草时模型也会自己加前缀，而且层名加错。实测（2026-09-30）
    在卷表层问「配齐章表」，模型连着提出 `brief:title` / `brief:era` / `brief:core_conflict`。
    旧逻辑只认「head 是不是层名」，于是整批按指令层去解析：提案表显示「未知小节「era」」，
    作者勾完一条也进不去——白问一次、白花一次钱，还以为是软件坏了。

    两层都有这个小节时（title、characters 这种重名），按当前层解。
    取的是「可预期」：写错层最多落错一格，作者在提案表上一眼能看出来；
    跟着模型乱写的前缀走，则是整批显示成未知小节，反倒看不出来。
    """
    from workshop.setting_assist import LAYER_SPECS

    head, sep, tail = section.partition(":")
    if not sep or not tail:
        return layer, section
    if tail in (LAYER_SPECS.get(layer) or {}):
        return layer, tail
    if head in LAYER_SPECS and tail in (LAYER_SPECS[head] or {}):
        return head, tail
    return layer, tail


def run_setting_assist(
    paths: Paths,
    name: str,
    *,
    message: str,
    history: list[dict[str, str]] | None = None,
    refs: list[str] | None = None,
    level: str = "k3",
    layer: str = "setting",
    key: str = "",
    provider_id: str | None = None,
    model_id: str | None = None,
    max_tokens: int = 2000,
    task: str = "",
) -> dict[str, Any]:
    """问一次助手。**只返回提案，不写任何文件。**

    `max_tokens` 要能调大：「配齐」一次要提几十条势力/能力，2000 输出 token 只够十来条，
    模型会在半路被截断，而作者看到的是一份「提了一半」的清单，还以为提完了。
    """
    from workshop.config import load_config
    from workshop.llm import OpenAICompatProvider
    from workshop.secrets import SecretStore, setup_logging
    from workshop.setting_assist import (  # noqa: PLC0415
        LAYER_SPECS,
        describe_proposal,
        resolve_section,
        run_assist,
    )

    if not str(message or "").strip() and task not in ("writeback", "review"):
        raise ValueError("要说点什么才能问")

    document = creation_document(paths, name, layer, key)
    # 回填：把这一章的正文当事实来源。回填写回的是**设定集**（一份文档），
    # 所以 layer 定死 setting——卷表和逐章指令要改，作者自己在对应那一层改。
    task_prose = ""
    volume_doc: dict[str, Any] = {}
    brief_doc: dict[str, Any] = {}
    if task in ("writeback", "review"):
        from workshop.chapter_brief import parse_chapter_id
        from workshop.draft import load_draft

        task_prose = load_draft(_creation_work_dir(paths, name), key).get("text") or ""
        if not task_prose.strip():
            raise ValueError(f"{key or '这一章'} 还没有正文，没什么可回填的。先写这一章。")
        parsed = parse_chapter_id(key)
        if parsed:
            volume_doc = creation_document(paths, name, "volume", f"{int(parsed[0]):03d}")
        brief_doc = creation_document(paths, name, "brief", key)
        layer = "setting"
        document = creation_document(paths, name, "setting", "")
        if task == "review":
            # 审稿的修正落在**本章指令**上：正文改不了（改了等于重写，作者还得逐句对比），
            # 指令能改——改完指令重写这一章，作者既看得见改了什么，也能否决。
            layer = "brief"
            document = brief_doc
    cfg = load_config(paths.root / "providers.yaml")
    # 写前必读 + 完整设定卡：锚点、前情、角色动态、待回收伏笔、上一章末段，以及整张设定集。
    # 起草卷表/指令时，模型原先只看得到那一层自己的文档——看不到设定集，也看不到前面各卷。
    _ctx = _assist_context(paths, name, layer, key)
    provider, model = _resolve_provider_and_model(cfg, provider_id, model_id)
    if provider.api_key_ref and not _read_api_key(cfg, provider):
        raise ValueError(_no_key_msg(provider))

    store = SecretStore(paths.root / "config")
    setup_logging(store.known_values)
    client = OpenAICompatProvider(
        base_url=provider.base_url,
        api_key=_read_api_key(cfg, provider),
        timeout_sec=90,
        auth_scheme=provider.auth_scheme,
        secrets=store.known_values,
        rate_limit=provider.rate_limit,
    )
    block, missing = _setting_refs_block(paths, refs or [], level)
    result = run_assist(
        client=client, model_id=model, setting=document,
        message=message, history=history or [], refs_block=block, layer=layer,
        max_tokens=max_tokens, task=task, prose=task_prose,
        context_block=_ctx,
        volume=volume_doc, brief=brief_doc, title=(brief_doc or {}).get("title") or "",
    )
    proposals = []
    # 上限 80：配齐一次可能提 20 势力 + 20 能力 + 20 规则，40 条会把后面的直接丢掉
    for index, raw in enumerate(result.proposals[:80]):
        row = raw if isinstance(raw, dict) else {}
        # 回填会提「volume:chapters」这种带层前缀的小节名。拆开：层进 layer、裸名进 section，
        # 这样每一层往下走的还是同一个 apply_proposals（白名单、主键去重、逐条原因都一样）。
        # 前缀是必须的：title 在卷层和指令层都有、characters 在两层形状完全不同，裸名分不出。
        section = str(row.get("section") or "")
        target_layer, section = _split_section_pointer(layer, section)
        # 模型有时给中文标签而不是键名（见 resolve_section）。这里就归一，
        # 否则提案表那一列会显示「未知小节「世界硬规则」」，而它其实是能认的。
        section = resolve_section(target_layer, section)
        spec = (LAYER_SPECS.get(target_layer) or {}).get(section) or {}
        entry = row.get("entry") if isinstance(row.get("entry"), dict) else None
        if entry is None:
            # 模型有时把字段平铺在顶层（格式示例里原先没有 update 的样子可抄，它就自己猜——
            # 实测三条 update/add 都栽在这上面）。平铺的收进来当 entry，
            # **闸没有放松**：_clean_entry 的白名单会把不该有的字段全丢掉，
            # 缺主键、重名、缺必填照样拒。
            flat = {k: v for k, v in row.items()
                    if k not in ("section", "op", "value", "entry")}
            entry = flat or None
        # 表格里那一个「内容」输入框绑到哪儿：条目表默认绑主键，其余绑 value。
        # 改完的值会覆盖模型给的原值——批量生成最容易出问题的就是名字。
        # 但主键不一定好认：章节的主键是章节号，那一列整排「1、2、3」，
        # 作者看不出哪一章是哪一章。白名单里可以用 display 指定一个给人看的字段
        # （章节用 title），主键就挪到「类别」列，谁也不丢。
        if spec.get("key"):
            primary_field = str(spec.get("display") or spec["key"])
            primary = str((entry or {}).get(primary_field) or "")
            keyed = ("" if primary_field == spec["key"]
                     else str((entry or {}).get(spec["key"]) or ""))
        else:
            primary_field, primary, keyed = "value", str(row.get("value") or ""), ""
        proposals.append(
            {
                "index": index,
                "section": section,
                "layer": target_layer,
                "section_label": (spec.get("label") or f"未知小节「{section}」")
                                 + (f" {keyed}" if keyed else ""),
                "op": str(row.get("op") or ""),
                "label": describe_proposal(row, target_layer),
                # 发**解析后**的 entry（含平铺兜底那份）。发 row 里的原始值的话，
                # 模型平铺时客户端拿到 null，apply 照样拒——修正就白做了。
                "entry": entry,
                "value": row.get("value"),
                "primary_field": primary_field,
                "text": primary,
                "keep": True,
                "full": json.dumps(
                    {"entry": entry, "value": row.get("value")}, ensure_ascii=False
                ),
            }
        )
    return {
        "reply": result.reply,
        "proposals": proposals,
        "error": result.error,
        "usage": result.usage,
        "attempts": result.attempts,
        "refs_missing": missing,
        "layer": layer,
    }


# ── M9 创作台 · 写正文 ──────────────────────────────────────
#
# 三层真源拼成一个调用，**一次只出一章**。跑完就停，等作者读完再决定下一章。
# 不做批量：一次吐十章，第一章不满意就九章全废。


def _draft_inputs(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    """攒齐写这一章需要的东西：设定集、本卷、本章指令，各自的校验状态。"""
    from workshop.chapter_brief import parse_chapter_id

    wd = _creation_work_dir(paths, name)
    parsed = parse_chapter_id(chapter_id)
    if not parsed:
        raise ValueError(f"章节编号「{chapter_id}」不合约定，应当是 v001-c0001 这种")

    setting_read = creation_read(paths, name, "setting", "")
    brief_read = creation_read(paths, name, "brief", chapter_id)

    vol_no = parsed[0]   # parse_chapter_id 返回 (卷号, 章号)
    volume_key = f"{int(vol_no):03d}"
    volume_read = creation_read(paths, name, "volume", volume_key)

    return {
        "work_dir": wd,
        "chapter_id": chapter_id,
        "vol_no": int(vol_no),
        "volume_key": volume_key,
        "setting": setting_read.get("data") or {},
        "setting_errors": setting_read.get("errors") or [],
        "volume": volume_read.get("data") or {},
        "brief": brief_read.get("data") or {},
        "brief_errors": brief_read.get("errors") or [],
        "brief_exists": bool(brief_read.get("exists")),
        "title": (brief_read.get("data") or {}).get("title") or "",
    }


def _draft_context_bundle(
    ctx: dict[str, Any], *, budget_chars: int | None = None
) -> tuple[str, dict[str, Any]]:
    """计划与生成共用同一次装配：文本和预算报告必须来自同一份，不能各算一遍。"""
    from workshop.creation import render_write_brief_report

    cno = int((ctx["brief"] or {}).get("chapter_no") or 0)
    return render_write_brief_report(
        ctx["work_dir"],
        chapter_no=cno,
        prev_chapter_no=max(0, cno - 1),
        brief=ctx["brief"],
        budget_chars=budget_chars,
    )


def draft_plan(
    paths: Paths, name: str, chapter_id: str, *,
    refs: list[str] | None = None, level: str = "k3",
    provider_id: str | None = None, model_id: str | None = None,
    budget_chars: int | None = None,
) -> dict[str, Any]:
    """写这一章的免费计划：多少 token、多少钱、会喂进去什么。**不调模型。**"""
    from workshop.batch import _pick_price, price_bucket
    from workshop.config import load_config
    from workshop.draft import (
        MAX_CONTINUE_ROUNDS, build_draft_plan, derive_max_tokens, load_draft,
    )

    ctx = _draft_inputs(paths, name, chapter_id)
    cfg = load_config(paths.root / "providers.yaml")
    provider, model, stage_meta = _resolve_stage(cfg, "draft", provider_id, model_id)
    block, missing = _setting_refs_block(paths, refs or [], level)
    model_cfg = provider.model(model) or {}
    bucket = price_bucket(provider)

    # 设定集有阻断就不该开写：人物对不上、主角不唯一，写出来必然歪
    errors = list(ctx["brief_errors"])
    if ctx["setting_errors"]:
        errors = [f"设定集有 {len(ctx['setting_errors'])} 项阻断：{ctx['setting_errors'][0]}"] + errors

    from workshop.context_budget import load_manifest, manifest_path

    _ctx, _report = _draft_context_bundle(ctx, budget_chars=budget_chars)
    plan = build_draft_plan(
        chapter_id=chapter_id, title=ctx["title"], setting=ctx["setting"],
        volume=ctx["volume"], brief=ctx["brief"], refs_block=block,
        provider_id=provider.id, model_id=model,
        price_input_per_mtok=_pick_price(model_cfg, bucket, "input"),
        price_output_per_mtok=_pick_price(model_cfg, bucket, "output"),
        bucket=bucket, brief_errors=errors, brief_exists=ctx["brief_exists"],
        context_block=_ctx, context_report=_report,
    ).to_dict()
    # 输出上限按目标字数反推，计划里直接可见（只读，不调模型）。
    plan["max_tokens"] = derive_max_tokens(
        (ctx["brief"] or {}).get("word_target"), model_cfg
    )
    context_manifest = manifest_path(ctx["work_dir"], chapter_id)
    plan["context_manifest_path"] = str(context_manifest)
    plan["context_manifest_exists"] = load_manifest(ctx["work_dir"], chapter_id) is not None

    existing = load_draft(ctx["work_dir"], chapter_id)
    plan["refs_missing"] = missing
    plan["has_draft"] = bool(existing.get("exists"))
    plan["existing_chars"] = len(existing.get("text") or "")
    plan["refs_available"] = assist_refs(paths)

    # B1：细纲是落盘的，计划接口直接读磁盘，不重新生成。
    # 指令改过（brief_sha 对不上）就把旧细纲标成 stale，生成时会重新做一份。
    from workshop import beats as beats_mod

    saved_beats = beats_mod.load_beats(ctx["work_dir"], chapter_id)
    reusable, _ = beats_mod.load_reusable_beats(ctx["work_dir"], chapter_id, ctx["brief"])
    plan["beats"] = {
        "exists": bool(saved_beats),
        "path": str(beats_mod.beats_path(ctx["work_dir"], chapter_id)),
        "count": len((saved_beats or {}).get("beats") or []),
        "source": (saved_beats or {}).get("source") or "",
        "saved_at": (saved_beats or {}).get("generated_at") or "",
        "brief_sha": (saved_beats or {}).get("brief_sha") or "",
        "reusable": bool(reusable),
        "stale": bool(saved_beats) and not reusable,
        "beats": (saved_beats or {}).get("beats") or [],
    }
    plan["max_continue_rounds"] = int(MAX_CONTINUE_ROUNDS)

    # C6：技法按阶段分发，并把「给了哪几篇、占多少字」写进计划——
    # 正文的注入量已经算进估算 token（draft.build_draft_plan 里同一份 craft_block），
    # 细纲是另一次调用，单独列出字数，避免把两次调用的预算混成一个数。
    from workshop import craft as craft_mod

    cno = int((ctx["brief"] or {}).get("chapter_no") or 0)
    first_vol = bool((ctx["brief"] or {}).get("first_of_volume"))
    craft_rows = {}
    for stage in ("brief", "draft"):
        meta = craft_mod.plan_block(
            stage=stage, chapter_no=cno, is_first_of_volume=first_vol, brief=ctx["brief"]
        )
        craft_rows[stage] = {k: v for k, v in meta.items() if k != "text"}
    plan["craft"] = craft_rows
    plan["stage"] = {
        "stage": "draft",
        **stage_meta,
        "provider": provider.id,
        "provider_name": provider.name,
        "model": model,
    }
    # 细纲（节拍）是另一阶段：可能换了更便宜的模型。计划里要能提前看见，
    # 否则作者只会看到「正文用 A」，点下去实际先用 B 生成细纲。
    try:
        plan_provider, plan_model, plan_stage_meta = _resolve_stage(cfg, "plan")
        plan["beats_stage"] = {
            "stage": "plan",
            **plan_stage_meta,
            "provider": plan_provider.id,
            "provider_name": plan_provider.name,
            "model": plan_model,
            "available": True,
        }
    except ValueError as exc:
        plan["beats_stage"] = {
            "stage": "plan", "available": False, "reason": str(exc),
        }
    plan["last_run"] = _last_run_summary(ctx["work_dir"], chapter_id)
    return plan


def _last_run_summary(work_dir: Path, chapter_id: str) -> dict[str, Any]:
    """上一轮生成的调用账。U5 靠它显示「细纲/每一轮续写各占多少」。"""
    from workshop.context_budget import load_manifest

    manifest = load_manifest(work_dir, chapter_id)
    if not manifest:
        return {}
    keys = (
        "phase", "saved_at", "usage", "finish_reason", "truncated", "continued",
        "continue_rounds", "max_continue_rounds", "beats", "error_code", "error",
    )
    out = {key: manifest.get(key) for key in keys if manifest.get(key) is not None}
    out["calls"] = manifest.get("calls") or []
    return out


def run_draft(
    paths: Paths, name: str, chapter_id: str, *,
    refs: list[str] | None = None, level: str = "k3",
    provider_id: str | None = None, model_id: str | None = None,
    max_tokens: int | None = None, temperature: float = 0.8,
    budget_chars: int | None = None,
    task: Any | None = None,
    resume_candidate: str = "",
    force_beats: bool = False,
) -> dict[str, Any]:
    """写这一章（**会花钱**）。跑完落盘并做机械自检。只出一章，不往下串。

    批次 2 的三件新事都在这里串起来：
      · 细纲：先花一次短调用拆节拍，失败就回退为不注入；
      · 续写：`finish_reason=length` 时按章续，最多 `MAX_CONTINUE_ROUNDS` 轮；
      · 失败关闭：失败/截断/取消**不覆盖正式稿**，只留候选恢复点。
    所有调用（含细纲）都进 manifest 的 `calls`，U5 能看到各自占了多少。
    """
    import time
    from datetime import datetime, timezone

    from server import tasks as task_registry
    from workshop import beats as beats_mod
    from workshop import draft_store
    from workshop.config import load_config
    from workshop.context_budget import save_manifest
    from workshop.creation import render_prev_tail
    from workshop.draft import (
        MAX_CONTINUE_ROUNDS, build_draft_messages, check_draft, count_chars,
        generate_draft, load_draft, save_draft,
    )
    from workshop.errors import ErrorKind, GenerationCancelled, describe_error
    from workshop.llm import OpenAICompatProvider
    from workshop.secrets import SecretStore, setup_logging

    if task is not None:
        task.started_ms = time.monotonic()
        task_registry.update(task, status="running", stage="装配材料", stage_index=0,
                             message="正在装配设定集、指令与写前必读")
    ctx = _draft_inputs(paths, name, chapter_id)
    if not ctx["brief_exists"] or not ctx["brief"]:
        raise ValueError(
            f"还没有 {chapter_id} 的创作任务指令。没有指令就写正文，"
            "等于让模型替你决定这一章要干什么。"
        )
    if ctx["brief_errors"]:
        raise ValueError(f"这一章的指令有 {len(ctx['brief_errors'])} 项阻断没解决：{ctx['brief_errors'][0]}")
    if ctx["setting_errors"]:
        raise ValueError(f"设定集有 {len(ctx['setting_errors'])} 项阻断没解决：{ctx['setting_errors'][0]}")

    cfg = load_config(paths.root / "providers.yaml")
    # E2：正文与细纲是两个阶段，可以各自配模型；它们的服务商也可能不同。
    provider, model, draft_stage = _resolve_stage(cfg, "draft", provider_id, model_id)
    _require_key(cfg, provider, "正文生成")
    # 输出预算由本章目标字数反推：一次给够，比「写一半→续写再发一遍上下文」更省。
    # 调用方显式传值（调试）时照旧尊重传进来的数。
    max_tokens_derived = max_tokens is None
    if max_tokens is None:
        from workshop.draft import derive_max_tokens

        max_tokens = derive_max_tokens(
            (ctx["brief"] or {}).get("word_target"), provider.model(model) or {}
        )
    plan_provider, plan_model, plan_stage = _resolve_stage(cfg, "plan")
    _require_key(cfg, plan_provider, "写前细纲")

    store = SecretStore(paths.root / "config")
    setup_logging(store.known_values)
    client = _build_client(cfg, provider, store)
    plan_client = client if plan_provider.id == provider.id else _build_client(
        cfg, plan_provider, store
    )
    block, _missing = _setting_refs_block(paths, refs or [], level)
    _ctx, _report = _draft_context_bundle(ctx, budget_chars=budget_chars)

    # C6：细纲只吃结构技法，正文吃行文技法；两份选择都写进 manifest，
    # 调用账里能解释「这次到底注入了哪几篇、多少字」。
    from workshop import craft as craft_mod

    cno = int((ctx["brief"] or {}).get("chapter_no") or 0)
    first_vol = bool((ctx["brief"] or {}).get("first_of_volume"))
    craft_brief = craft_mod.plan_block(
        stage="brief", chapter_no=cno, is_first_of_volume=first_vol, brief=ctx["brief"]
    )
    craft_draft = craft_mod.plan_block(
        stage="draft", chapter_no=cno, is_first_of_volume=first_vol, brief=ctx["brief"]
    )

    calls: list[dict[str, Any]] = []
    beats_meta: dict[str, Any] = {
        "source": "none", "count": 0, "path": str(beats_mod.beats_path(ctx["work_dir"], chapter_id)),
    }
    resume_meta: dict[str, Any] = {}
    warnings_store: list[str] = []

    manifest_extra = {
        "phase": "assembled",
        "work": name,
        "provider": provider.id,
        "model": model,
        "stage": _stage_summary(draft_stage, provider, model),
        "beats_stage": _stage_summary(plan_stage, plan_provider, plan_model),
        "craft": {
            "brief": {k: v for k, v in craft_brief.items() if k != "text"},
            "draft": {k: v for k, v in craft_draft.items() if k != "text"},
        },
        "budget_chars": _report.get("budget_chars"),
        "refs_level": level,
        "refs": refs or [],
        "calls": calls,
        "beats": beats_meta,
        # 输出上限与来源写进账本：事后要能回答「这一次为什么只给了这么多」。
        "max_tokens": max_tokens,
        "max_tokens_source": "word_range" if max_tokens_derived else "explicit",
        "max_continue_rounds": MAX_CONTINUE_ROUNDS,
    }

    def write_manifest(phase: str, **extra: Any) -> None:
        save_manifest(
            ctx["work_dir"], chapter_id, _report,
            extra={**manifest_extra, "phase": phase, "calls": calls,
                   "beats": beats_meta, **extra},
        )

    # A4：生成前先落一份「这次到底喂了什么」。模型失败也留着这份证据。
    write_manifest("assembled")

    def fail(
        *,
        status: str,
        code: str,
        error: str,
        text: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """失败/截断/取消的统一出口：**不覆盖正式稿**，只留候选。"""
        candidate_id = ""
        if (text or "").strip():
            cand = draft_store.save_candidate(
                ctx["work_dir"], chapter_id, text=text,
                meta={"model": model, "provider": provider.id, "beats": beats_meta,
                      "calls": calls, "candidate_from": "generation"},
                reason=error, task_id=task.task_id if task else "",
            )
            candidate_id = (cand or {}).get("candidate_id", "")
        info = describe_error(code, error)
        payload = {
            "ok": False, "status": status, "chapter_id": chapter_id,
            "cancelled": status == "cancelled",
            "error": error, "error_code": code,
            "title": info["title"], "action": info["action"],
            "retryable": info["retryable"],
            "candidate_id": candidate_id, "chars": count_chars(text),
            "calls": calls, "beats": beats_meta, **(extra or {}),
        }
        write_manifest(
            status if status in ("cancelled",) else "failed",
            error=error, error_code=code, candidate_id=candidate_id, **(extra or {}),
        )
        if task is not None:
            task_registry.finish(
                task, status=status, message=error, result=payload,
                error=error, error_code=code, candidate_id=candidate_id,
                chars=count_chars(text), calls=calls,
            )
        return payload

    def on_round(row: dict[str, Any]) -> None:
        # E2：每次调用都带上是哪个服务商、哪个模型。只写阶段名不够——
        # 一旦阶段与默认绑定不同，账本上就对不上「这行到底是谁花的钱」。
        if row.get("stage") == "beats":
            row.setdefault("provider", plan_provider.id)
            row.setdefault("model", plan_model)
        else:
            row.setdefault("provider", provider.id)
            row.setdefault("model", model)
        calls.append(row)
        if task is not None:
            label = "细纲" if row.get("stage") == "beats" else (
                "正文首轮" if row.get("round") == 0 and row.get("stage") == "draft"
                else f"续写第 {int(row.get('round') or 0)} 轮"
            )
            task_registry.update(
                task,
                stage="正文生成", stage_index=2,
                round=int(row.get("round") or 0),
                chars=int(row.get("total_chars") or 0),
                calls=list(calls),
                message=f"{label}完成：{int(row.get('new_chars') or 0)} 字，"
                        f"{row.get('finish_reason') or '未给结束原因'}",
            )

    def on_partial(text: str, row: dict[str, Any]) -> None:
        # 截断时把这一轮的半截正文留成候选。进程随后被杀、或取消，
        # 作者至少还有这一轮的字可以从断点继续。
        if row.get("finish_reason") not in ("length", "max_tokens") or not (text or "").strip():
            return
        cand = draft_store.save_candidate(
            ctx["work_dir"], chapter_id, text=text,
            meta={"model": model, "provider": provider.id, "beats": beats_meta,
                  "calls": list(calls), "candidate_from": "truncated_round"},
            reason="正文被 max_tokens 截断时的中间稿",
            task_id=task.task_id if task else "",
        )
        if task is not None and cand:
            task_registry.update(task, candidate_id=cand.get("candidate_id", ""))

    try:
        # ── 1. 装配材料（已落 manifest，这里只推进阶段） ──
        if task is not None:
            task_registry.update(task, stage="装配材料", stage_index=0,
                                 message="写前必读已装配，预算报告已落盘")

        # 恢复候选：从断点续写时，先把它当已写正文，模型只需接着往下写。
        prior_text = ""
        if resume_candidate:
            candidate = draft_store.load_candidate(ctx["work_dir"], chapter_id, resume_candidate)
            if not candidate:
                raise ValueError(f"找不到候选「{resume_candidate}」，无法从断点继续")
            prior_text = str(candidate.get("text") or "")
            resume_meta = {
                "resumed_from_candidate": resume_candidate,
                "resumed_chars": count_chars(prior_text),
            }
            manifest_extra.update(resume_meta)
            write_manifest("assembled")

        # ── 2. 写前细纲（B1） ──
        if task is not None:
            task_registry.update(task, stage="写前细纲", stage_index=1,
                                 message="正在拆这一章的 5–8 个节拍")
        reusable, saved = beats_mod.load_reusable_beats(ctx["work_dir"], chapter_id, ctx["brief"])
        if reusable and not force_beats:
            beats_list = reusable
            beats_meta = {
                "source": "reused", "count": len(reusable),
                "path": str(beats_mod.beats_path(ctx["work_dir"], chapter_id)),
                "saved_at": (saved or {}).get("generated_at") or "",
            }
        else:
            prev_tail = ""
            try:
                prev_tail = render_prev_tail(
                    ctx["work_dir"], before_chapter_no=int(ctx["brief"].get("chapter_no") or 0),
                    chars=600,
                )
            except (ValueError, OSError):
                prev_tail = ""
            beats_result = beats_mod.generate_beats(
                client=plan_client, model_id=plan_model, setting=ctx["setting"],
                volume=ctx["volume"], brief=ctx["brief"],
                word_range=None, prev_tail=prev_tail,
                craft_block=craft_brief["text"], work_dir=ctx["work_dir"],
                cancel_event=task.cancel_event if task is not None else None,
                cancel_check=(lambda: bool(task.cancel_event.is_set())) if task is not None else None,
            )
            calls.append(beats_result["call"])
            if task is not None:
                task_registry.update(task, calls=list(calls))
            if beats_result.get("ok"):
                beats_list = beats_result["beats"]
                saved_meta = beats_mod.save_beats(
                    ctx["work_dir"], chapter_id, beats=beats_list, brief=ctx["brief"],
                    model=plan_model, source="model", usage=beats_result.get("usage") or {},
                )
                beats_meta = {
                    "source": "model", "count": len(beats_list),
                    "path": saved_meta.get("path", ""),
                    "brief_sha": saved_meta.get("brief_sha", ""),
                }
            else:
                beats_list = []
                beats_meta = {
                    "source": "failed", "count": 0,
                    "path": str(beats_mod.beats_path(ctx["work_dir"], chapter_id)),
                    "error": beats_result.get("error") or "细纲生成失败",
                    "error_code": beats_result.get("error_code") or "",
                }
                warnings_store.append(
                    "细纲没生成成功，已回退为不注入细纲继续写正文"
                    f"（{beats_result.get('error_code') or 'unknown'}：{beats_result.get('error') or ''}）"
                )
            write_manifest("beats_ready")
        manifest_extra["beats"] = beats_meta

        # ── 3. 正文（B2：截断自动续写） ──
        if task is not None:
            task_registry.update(
                task, stage="正文生成", stage_index=2, round=0,
                max_rounds=MAX_CONTINUE_ROUNDS,
                message="正在写正文（首轮）",
            )
        messages = build_draft_messages(
            setting=ctx["setting"], volume=ctx["volume"], brief=ctx["brief"],
            refs_block=block, title=ctx["title"], context_block=_ctx,
            beats=beats_mod.render_beats_for_prompt(beats_list) if beats_list else "",
            craft=craft_draft["text"], work_dir=ctx["work_dir"],
        )
        result = generate_draft(
            client=client, model_id=model, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
            cancel_event=task.cancel_event if task is not None else None,
            on_round=on_round, on_partial=on_partial,
            prior_text=prior_text,
        )

        # 失败/截断/空响应：一律不落正式稿
        if result.error_code or result.truncated or not result.text.strip():
            code = result.error_code or ErrorKind.UNKNOWN.value
            error = result.error or "模型没有返回可用的正文"
            return fail(status="failed", code=code, error=error, text=result.text,
                        extra={"usage": result.usage, "finish_reason": result.finish_reason,
                               "continued": result.continued,
                               "continue_rounds": result.continue_rounds})

        # ── 4. 自检与落盘 ──
        if task is not None:
            task_registry.update(task, stage="自检与落盘", stage_index=3,
                                 message="正在做机械自检并保存正文")
        errors, warnings, stats = check_draft(
            result.text, brief=ctx["brief"], setting=ctx["setting"],
        )
        warnings = warnings_store + list(warnings)
        if beats_meta.get("source") == "failed":
            beats_meta["fallback"] = True
        if result.continued:
            warnings.append(
                f"这一章靠续写凑成：首轮被截断后自动续了 {result.continue_rounds} 轮"
                f"（上限 {MAX_CONTINUE_ROUNDS}）；拼接处已做 n-gram 去重。"
            )

        existing = load_draft(ctx["work_dir"], chapter_id)
        if existing.get("exists") and existing.get("text") != result.text:
            draft_store.snapshot_version(
                ctx["work_dir"], chapter_id, text=existing["text"],
                meta=existing.get("meta") or {}, reason="重写前自动备份",
            )

        saved = save_draft(
            ctx["work_dir"], chapter_id, text=result.text,
            meta={
                "work": name, "model": model, "provider": provider.id,
                "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "usage": result.usage, "finish_reason": result.finish_reason,
                "truncated": False, "continued": result.continued,
                "continue_rounds": result.continue_rounds,
                "max_continue_rounds": MAX_CONTINUE_ROUNDS,
                "max_tokens": max_tokens,
                "refs_level": level, "refs": refs or [],
                "beats": beats_meta, "calls": calls,
                **resume_meta,
                "stats": stats, "errors": errors, "warnings": warnings,
            },
        )
        write_manifest(
            "generated",
            usage=result.usage, finish_reason=result.finish_reason,
            truncated=False, continued=result.continued,
            continue_rounds=result.continue_rounds,
            # 字数偏离也进账本：界面之外，事后回看 manifest 同样能看到短了/长了多少。
            word_stats=stats,
        )
        # 新正文写进来，之前那次审稿的结论就作废了。状态自动退回草稿，
        # 免得界面上还挂着「已审」而正文其实是重写的另一份。
        if draft_store.get_status(ctx["work_dir"], chapter_id).get("status") != "draft":
            draft_store.set_status(
                ctx["work_dir"], chapter_id, "draft",
                reason="重新生成正文，之前的审稿结论作废", by="generation",
            )
        payload = {
            "ok": True, "chapter_id": chapter_id, "text": result.text,
            "errors": errors, "warnings": warnings, "stats": stats,
            "usage": result.usage, "truncated": False,
            "continued": result.continued, "continue_rounds": result.continue_rounds,
            "beats": beats_meta, "calls": calls, **resume_meta, **saved,
        }
        if task is not None:
            task_registry.finish(
                task, status="done",
                message=f"完成：{count_chars(result.text)} 字"
                        + (f"（续写 {result.continue_rounds} 轮）" if result.continued else ""),
                # 任务状态是轮询接口，**不能把整章正文塞进去**：
                # 每秒返回几千字既浪费又会让任务 JSON 越来越大。正文走 GET draft。
                result={k: v for k, v in payload.items() if k != "text"},
                chars=count_chars(result.text),
                calls=calls, beats_count=int(beats_meta.get("count") or 0),
                round=result.continue_rounds,
            )
        return payload

    except GenerationCancelled as exc:
        # 取消发生在一次调用中途：这一次没有正常结束帧，但**它产生了字数、花了时间**。
        # 不记的话，U5 的账本会显示 0 次调用，作者会以为取消没花钱、也没吐出半截正文。
        cancelled_stage = "beats" if (task is not None and task.stage_index == 1) else "draft"
        calls.append({
            "stage": cancelled_stage,
            "round": int(getattr(task, "round", 0) or 0) if task is not None else 0,
            "aborted": True,
            "chars": count_chars(exc.partial_text),
            "new_chars": count_chars(exc.partial_text),
            "total_chars": count_chars(exc.partial_text),
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "finish_reason": "cancelled",
            "elapsed_ms": float(getattr(task, "elapsed_ms", 0) or 0) if task is not None else 0,
            "max_tokens": max_tokens,
            "error_code": ErrorKind.CANCELLED.value,
            "ok": False,
        })
        return fail(
            status="cancelled", code=ErrorKind.CANCELLED.value,
            error="已取消：服务端已停止模型调用，未覆盖正式稿",
            text=exc.partial_text,
        )


def start_draft(
    paths: Paths, name: str, chapter_id: str, *,
    refs: list[str] | None = None, level: str = "k3",
    provider_id: str | None = None, model_id: str | None = None,
    max_tokens: int | None = None, temperature: float = 0.8,
    budget_chars: int | None = None,
    resume_candidate: str = "",
    force_beats: bool = False,
) -> dict[str, Any]:
    """异步启动一次生成，立刻返回 task。**同步校验**放在线程外，参数错不排队。"""
    from server import tasks as task_registry
    from workshop.draft import MAX_CONTINUE_ROUNDS
    from workshop.errors import ErrorKind, describe_error

    ctx = _draft_inputs(paths, name, chapter_id)
    if not ctx["brief_exists"] or not ctx["brief"]:
        raise ValueError(f"还没有 {chapter_id} 的创作任务指令，无法开始生成。")
    if ctx["brief_errors"]:
        raise ValueError(f"这一章的指令有 {len(ctx['brief_errors'])} 项阻断没解决：{ctx['brief_errors'][0]}")
    if ctx["setting_errors"]:
        raise ValueError(f"设定集有 {len(ctx['setting_errors'])} 项阻断没解决：{ctx['setting_errors'][0]}")
    if resume_candidate:
        from workshop import draft_store

        if not draft_store.load_candidate(ctx["work_dir"], chapter_id, resume_candidate):
            raise ValueError(f"找不到候选「{resume_candidate}」，无法从断点继续")
    from workshop import draft_store

    if draft_store.get_status(ctx["work_dir"], chapter_id).get("status") == "final":
        raise ValueError("这一章已定稿。要重写，先点「重新打开」把状态退回草稿。")

    task = task_registry.create(
        paths, work=name, chapter_id=chapter_id, kind="draft",
        total_stages=4, max_rounds=MAX_CONTINUE_ROUNDS,
    )

    def worker() -> None:
        try:
            run_draft(
                paths, name, chapter_id, refs=refs, level=level,
                provider_id=provider_id, model_id=model_id,
                max_tokens=max_tokens, temperature=temperature,
                budget_chars=budget_chars, task=task,
                resume_candidate=resume_candidate, force_beats=force_beats,
            )
        except Exception as exc:  # noqa: BLE001
            code = ErrorKind.BAD_REQUEST.value if isinstance(exc, ValueError) else ErrorKind.UNKNOWN.value
            info = describe_error(code, str(exc))
            task_registry.finish(
                task, status="failed", message=str(exc), error=str(exc), error_code=code,
                action=str(info["action"]), result={"ok": False, "error": str(exc), "error_code": code},
            )

    task_registry.spawn(worker, name=f"draft-{task.task_id}")
    return task.public()


def draft_task_status(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    from server import tasks as task_registry

    return task_registry.latest(paths, name, chapter_id) or {
        "status": "idle", "chapter_id": chapter_id, "cancellable": False,
    }


def cancel_task(paths: Paths, task_id: str) -> dict[str, Any]:
    from server import tasks as task_registry

    return task_registry.request_cancel(paths, task_id)


def draft_versions(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    from workshop import draft_store

    work_dir = paths.work_dir(name)
    return {
        "chapter_id": chapter_id,
        "versions": draft_store.list_versions(work_dir, chapter_id),
        "candidates": draft_store.list_candidates(work_dir, chapter_id),
    }


def draft_version(paths: Paths, name: str, chapter_id: str, version_id: str) -> dict[str, Any]:
    """读某一个历史版本的正文，供 D4 版本对比用。列表接口只给元数据。"""
    from workshop import draft_store

    data = draft_store.load_version(paths.work_dir(name), chapter_id, version_id)
    if not data:
        raise FileNotFoundError(f"找不到历史版本「{version_id}」")
    return data


def promote_candidate(paths: Paths, name: str, chapter_id: str, candidate_id: str) -> dict[str, Any]:
    from workshop import draft_store

    return draft_store.promote_candidate(paths.work_dir(name), chapter_id, candidate_id)


def restore_version(paths: Paths, name: str, chapter_id: str, version_id: str) -> dict[str, Any]:
    from workshop import draft_store

    return draft_store.restore_version(paths.work_dir(name), chapter_id, version_id)


def _append_manifest_call(
    work_dir: Path,
    chapter_id: str,
    call: dict[str, Any] | None,
    *,
    review_meta: dict[str, Any] | None = None,
    refine_meta: dict[str, Any] | None = None,
) -> None:
    """把审稿这一次调用追加进现有 manifest，**不覆盖**批次 1/2 的账。

    生成那次的 `calls`（细纲 + 每一轮续写）必须原样保留：
    U5 是按这些行算「各自占了多少」的。这里只往后面加一行 `stage=review`，
    再补上 review/refine 的摘要。
    """
    from workshop.context_budget import load_manifest, save_manifest

    manifest = load_manifest(work_dir, chapter_id) or {}
    calls = list(manifest.get("calls") or [])
    if call:
        calls.append(call)
    extra = {
        key: value for key, value in manifest.items()
        if key not in ("schema_version", "chapter_id", "saved_at", "report")
    }
    extra["calls"] = calls
    if review_meta is not None:
        extra["review"] = review_meta
    if refine_meta is not None:
        extra["refine"] = refine_meta
    save_manifest(work_dir, chapter_id, manifest.get("report") or {}, extra=extra)


def review_draft(
    paths: Paths, name: str, chapter_id: str, *,
    provider_id: str | None = None, model_id: str | None = None,
    max_tokens: int = 4000,
) -> dict[str, Any]:
    """对**正文**跑一次结构化审稿（会花钱）。意见落盘，正文不动。

    与「审指令」不同：这里审的是已经写出来的字，产出是能逐条替换回正文的 finding。
    """
    from workshop import craft, draft_store
    from workshop import review as review_mod
    from workshop.draft import count_chars, load_draft
    from workshop.llm import OpenAICompatProvider
    from workshop.config import load_config
    from workshop.secrets import SecretStore, setup_logging

    ctx = _draft_inputs(paths, name, chapter_id)
    loaded = load_draft(ctx["work_dir"], chapter_id)
    prose = str(loaded.get("text") or "")
    if not prose.strip():
        raise ValueError(f"{chapter_id} 还没有正文，没什么可审的。先写这一章。")

    state = draft_store.get_status(ctx["work_dir"], chapter_id)
    if state.get("status") == "final":
        raise ValueError("这一章已定稿。要再审，先点「重新打开」。")

    cfg = load_config(paths.root / "providers.yaml")
    # E2：审稿是独立阶段，不吃正文那次的模型；没配就落回默认。
    provider, model, review_stage = _resolve_stage(cfg, "review", provider_id, model_id)
    _require_key(cfg, provider, "正文审稿")

    store = SecretStore(paths.root / "config")
    setup_logging(store.known_values)
    client = _build_client(cfg, provider, store, timeout_sec=180)

    chapter_no = int((ctx["brief"] or {}).get("chapter_no") or 0)
    vol_start = int(((ctx["volume"] or {}).get("vol") or {}).get("start_chapter") or 0)
    block = craft.craft_block(
        chapter_no=chapter_no, is_first_of_volume=bool(vol_start and chapter_no == vol_start),
        brief=ctx["brief"], review=True,
    )

    payload = review_mod.load_reviews(ctx["work_dir"], chapter_id)
    result = review_mod.run_review(
        client=client, model_id=model, prose=prose,
        brief=ctx["brief"], setting=ctx["setting"], craft_block=block,
        max_tokens=max_tokens, work_dir=ctx["work_dir"],
        existing_items=review_mod.all_items(payload),
    )

    prose_sha = review_mod.sha256(prose)
    entry = {
        "review_id": review_mod.new_review_id(),
        "created_at": review_mod._now_iso(),
        "model": model,
        "provider": provider.id,
        "prose_sha": prose_sha,
        "chars": len(prose),
        "count_chars": count_chars(prose),
        "summary": result["summary"],
        "items": result["items"],
        "invalid": result["invalid"],
        "attempts": result["attempts"],
        "attempt_count": len(result["attempts"]),
        "usage": result["usage"],
        "call": result["call"],
        "max_tokens": max_tokens,
    }
    review_mod.append_review(ctx["work_dir"], chapter_id, entry)

    call_meta = dict(result["call"] or {})
    call_meta["provider"] = provider.id
    call_meta["model"] = model
    _append_manifest_call(
        ctx["work_dir"], chapter_id, call_meta,
        review_meta={
            "review_id": entry["review_id"],
            "items": len(entry["items"]),
            "invalid": len(entry["invalid"]),
            "prose_sha": prose_sha,
            "summary": entry["summary"],
        },
    )

    status = state.get("status") or "draft"
    if status == "draft":
        status = draft_store.set_status(
            ctx["work_dir"], chapter_id, "reviewed",
            reason=f"正文审稿完成：{len(entry['items'])} 条意见", by="review",
        )["status"]

    items = review_mod.public_items(entry, prose_sha)
    counts = {"blocker": 0, "major": 0, "minor": 0}
    for item in items:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return {
        "ok": True,
        "chapter_id": chapter_id,
        "review_id": entry["review_id"],
        "summary": entry["summary"],
        "items": items,
        "invalid": entry["invalid"],
        "counts": counts,
        "attempts": entry["attempts"],
        "usage": entry["usage"],
        "call": result["call"],
        "status": status,
        "prose_sha": prose_sha,
        "chars": entry["chars"],
        "error_code": "",
    }


def apply_review_findings(
    paths: Paths, name: str, chapter_id: str, review_id: str,
    finding_ids: list[str], replacements: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把勾选的意见逐条写回正文（D2/D3）。**写回前一定存版本快照。**"""
    from workshop import draft_store, refine as refine_mod
    from workshop import review as review_mod
    from workshop.draft import count_chars, load_draft, save_draft

    work_dir = paths.work_dir(name)
    payload = review_mod.load_reviews(work_dir, chapter_id)
    review = review_mod.find_review(payload, review_id)
    if not review:
        raise ValueError(f"找不到审稿记录「{review_id}」，可能已被清理。")

    loaded = load_draft(work_dir, chapter_id)
    prose = str(loaded.get("text") or "")
    if not prose.strip():
        raise ValueError("这一章没有正文，无法应用意见。")
    prose_sha = review_mod.sha256(prose)

    items = review_mod.public_items(review, prose_sha)
    wanted = {str(x) for x in finding_ids or []}
    if not wanted:
        raise ValueError("没有选中任何意见")
    selected = [item for item in items if str(item.get("id")) in wanted]
    missing = wanted - {str(item.get("id")) for item in selected}
    if missing:
        raise ValueError("这些意见在这次审稿里找不到：" + "、".join(sorted(missing)))
    blocked = [item for item in selected if not item.get("adoptable")]
    if blocked:
        detail = "；".join(
            f"{item.get('id')}（{item.get('status')}）" for item in blocked
        )
        raise ValueError(
            "这些意见当前不能采纳：" + detail
            + "。正文改过（stale）或引文定位不到的意见只能重新审稿后再处理。"
        )

    result = refine_mod.apply_findings(prose, selected, replacements or {})
    if not result["applied"]:
        reasons = "；".join(str(x.get("reason") or "") for x in result["skipped"]) or "没有可替换的内容"
        raise ValueError(f"没有一条能应用：{reasons}")
    if not refine_mod.only_selected_changed(prose, result["text"], result["applied"]):
        raise RuntimeError("改写校验失败：选中范围之外的文字被改动了，已拒绝写入")

    # A2/D3：先快照，再覆盖。这一步不能省——采纳是覆盖式的。
    version = draft_store.snapshot_version(
        work_dir, chapter_id, text=prose, meta=loaded.get("meta") or {},
        reason="按审稿意见改写前自动备份",
    )
    version_id = (version or {}).get("version_id", "")
    applied_ids = [str(pick["id"]) for pick in result["applied"]]
    review_apply = {
        "review_id": review_id,
        "finding_ids": applied_ids,
        "regions": result["regions"],
        "changed_chars": result["changed_chars"],
        "version_id": version_id,
        "at": review_mod._now_iso(),
    }
    save_draft(
        work_dir, chapter_id, text=result["text"],
        meta={
            **(loaded.get("meta") or {}),
            "chars": count_chars(result["text"]),
            "review_apply": review_apply,
            "last_edited_at": review_apply["at"],
        },
    )

    # 把状态写进审稿记录：采纳过什么、写进哪个版本，以后查得到。
    applied_map = {str(pick["id"]): pick for pick in result["applied"]}
    for item in review.get("items") or []:
        pick = applied_map.get(str(item.get("id")))
        if not pick:
            continue
        item["status"] = "adopted"
        item["status_at"] = review_apply["at"]
        item["applied_version_id"] = version_id
        item["applied_text"] = pick["after"]
    review["applied_at"] = review_apply["at"]
    review_mod.save_reviews(work_dir, chapter_id, payload)

    state = draft_store.get_status(work_dir, chapter_id)
    status = state.get("status") or "draft"
    if status == "reviewed":
        status = draft_store.set_status(
            work_dir, chapter_id, "draft",
            reason=f"按审稿意见改写 {result['regions']} 处，需要复审", by="refine",
        )["status"]

    _append_manifest_call(
        work_dir, chapter_id, None,
        refine_meta={**review_apply, "chars": count_chars(result["text"])},
    )
    return {
        "ok": True,
        "chapter_id": chapter_id,
        "review_id": review_id,
        "applied": result["applied"],
        "skipped": result["skipped"],
        "regions": result["regions"],
        "changed_chars": result["changed_chars"],
        "chars": count_chars(result["text"]),
        "version_id": version_id,
        "status": status,
        "prose_sha": review_mod.sha256(result["text"]),
    }


def reject_review_findings(
    paths: Paths, name: str, chapter_id: str, review_id: str,
    finding_ids: list[str], reason: str = "",
) -> dict[str, Any]:
    """拒绝也留记录：以后重新审稿时，作者能看出「这条我早就不要了」。"""
    from workshop import review as review_mod
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    payload = review_mod.load_reviews(work_dir, chapter_id)
    review = review_mod.find_review(payload, review_id)
    if not review:
        raise ValueError(f"找不到审稿记录「{review_id}」")
    wanted = {str(x) for x in finding_ids or []}
    if not wanted:
        raise ValueError("没有选中任何意见")

    rejected: list[str] = []
    skipped: list[dict[str, str]] = []
    now = review_mod._now_iso()
    for item in review.get("items") or []:
        fid = str(item.get("id"))
        if fid not in wanted:
            continue
        current = str(item.get("status") or "open")
        if current == "adopted":
            skipped.append({"id": fid, "reason": "已经采纳并写回正文，不能再拒绝"})
            continue
        item["status"] = "rejected"
        item["status_at"] = now
        item["reject_reason"] = reason or "作者拒绝"
        rejected.append(fid)
    if not rejected:
        raise ValueError("选中的意见都没法拒绝：" +
                         "；".join(f"{x['id']}（{x['reason']}）" for x in skipped))
    review_mod.save_reviews(work_dir, chapter_id, payload)

    loaded = load_draft(work_dir, chapter_id)
    prose_sha = review_mod.sha256(str(loaded.get("text") or ""))
    return {
        "ok": True, "review_id": review_id, "rejected": rejected,
        "skipped": skipped, "prose_sha": prose_sha,
    }


def reopen_review_finding(
    paths: Paths, name: str, chapter_id: str, review_id: str, finding_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """作者要求「重新考虑」某条已拒绝意见。

    抑制不是永久封死：打上 `reopened_at` 后，下一次审稿不再按旧裁决压制它。
    这里只改历史条目的标记，不动正文，也不制造新意见。
    """
    from workshop import review as review_mod

    work_dir = paths.work_dir(name)
    payload = review_mod.load_reviews(work_dir, chapter_id)
    review = review_mod.find_review(payload, review_id)
    if not review:
        raise ValueError(f"找不到审稿记录「{review_id}」")
    item = next((x for x in review.get("items") or []
                 if str(x.get("id")) == str(finding_id)), None)
    if not item:
        raise ValueError(f"这次审稿里没有条目「{finding_id}」")
    status = str(item.get("status") or "open")
    if status != "rejected":
        raise ValueError(f"只有已拒绝的意见需要重新考虑（当前状态：{status}）")
    now = review_mod._now_iso()
    item["reopened_at"] = now
    item["reopen_reason"] = reason or "作者要求重新考虑"
    item.setdefault("history", []).append({
        "at": now, "action": "reopen", "by": "作者", "reason": item["reopen_reason"],
    })
    review_mod.save_reviews(work_dir, chapter_id, payload)
    return {"ok": True, "review_id": review_id, "item": item,
            "note": "下一次审稿时这条不再被抑制；是否重新提出由审稿模型判断。"}


def notes_state(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    """U8 面板数据：本章抽取了什么、每条状态、会进下一章的有几条。"""
    from workshop import notes as notes_mod
    from workshop import summary as summary_mod
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    payload = notes_mod.load_notes(work_dir, chapter_id)
    summary = notes_mod.notes_summary(payload)
    summary["path"] = f"90-draft/{chapter_id}.notes.json"
    snap = summary_mod.load_snapshot(work_dir, chapter_id)
    return {
        "chapter_id": chapter_id,
        "exists": bool(payload.get("notes")),
        "summary": summary,
        "notes": notes_mod.public_notes(payload),
        "source": payload.get("source") or {},
        "invalid": payload.get("last_invalid") or [],
        "history": payload.get("history") or [],
        "summary_snapshot": None if not snap else {
            "available": bool(snap.get("available")), "stale": bool(snap.get("stale")),
            "source_sha": snap.get("source_sha"), "current_source_sha": snap.get("current_source_sha"),
            "note_count": snap.get("note_count"), "summary": snap.get("summary"),
            "built_at": snap.get("built_at"),
        },
        "status": _draft_state_summary(work_dir, chapter_id,
                                       str(load_draft(work_dir, chapter_id).get("text") or "")),
    }


def _note_refs(ctx: dict[str, Any]) -> list[dict[str, str]]:
    from workshop import notes as notes_mod
    from workshop.chapter_brief import foreshadow_ledger

    led = foreshadow_ledger(ctx["work_dir"] / "80-brief")
    ids = [str(r.get("id") or "") for r in led.get("open") or []]
    ids += [str(r.get("id") or "") for r in led.get("recovered") or []]
    return notes_mod.known_refs(ctx["setting"], ctx["brief"], extra_foreshadow_ids=ids)


def extract_notes(
    paths: Paths, name: str, chapter_id: str, *,
    provider_id: str | None = None, model_id: str | None = None,
    max_tokens: int = 3000,
) -> dict[str, Any]:
    """从正文抽章节要点（会花钱）。结果只是**提案**，等作者逐条确认。"""
    from workshop import notes as notes_mod
    from workshop.config import load_config
    from workshop.draft import load_draft
    from workshop.secrets import SecretStore, setup_logging

    ctx = _draft_inputs(paths, name, chapter_id)
    loaded = load_draft(ctx["work_dir"], chapter_id)
    prose = str(loaded.get("text") or "")
    if not prose.strip():
        raise ValueError(f"{chapter_id} 还没有正文，抽不出要点。先写这一章。")

    cfg = load_config(paths.root / "providers.yaml")
    # E2：要点抽取是独立阶段，通常配便宜的小模型。
    provider, model, extract_stage = _resolve_stage(cfg, "extract", provider_id, model_id)
    _require_key(cfg, provider, "章节要点抽取")

    store = SecretStore(paths.root / "config")
    setup_logging(store.known_values)
    client = _build_client(cfg, provider, store, timeout_sec=180)

    prior: list[dict[str, Any]] = []
    from pathlib import Path as _Path

    # 包含当前章节：作者可能已经确认过一条状态，再次抽取时模型给出不同说法，
    # 同样属于「与现有状态冲突」，要标出来而不是覆盖。
    for path in sorted((_Path(ctx["work_dir"]) / "90-draft").glob("*.notes.json")):
        other = path.name[: -len(".notes.json")]
        prior.extend(notes_mod.load_notes(ctx["work_dir"], other).get("notes") or [])

    chapter_no = int((ctx["brief"] or {}).get("chapter_no") or 0)
    vol_no = int(ctx["vol_no"] or 0)
    result = notes_mod.run_extract(
        client=client, model_id=model, provider_id=provider.id, prose=prose,
        brief=ctx["brief"], setting=ctx["setting"], refs=_note_refs(ctx),
        chapter_id=chapter_id, vol=vol_no, chapter_no=chapter_no,
        prior_notes=prior, max_tokens=max_tokens, work_dir=ctx["work_dir"],
    )
    payload = notes_mod.save_extraction(
        ctx["work_dir"], chapter_id, items=result["items"],
        invalid=result["invalid"], prose=prose,
        source={"provider": provider.id, "model": model, "chapter_no": chapter_no,
                "vol": vol_no, "chars": len(prose)},
        replace=True, call=result["call"],
    )
    call_meta = dict(result["call"] or {})
    call_meta["provider"] = provider.id
    call_meta["model"] = model
    _append_manifest_call(ctx["work_dir"], chapter_id, call_meta)
    state = notes_state(paths, name, chapter_id)
    return {
        "ok": True, "chapter_id": chapter_id,
        "added": len(result["items"]), "invalid": result["invalid"],
        "attempts": result["attempts"], "usage": result["usage"],
        "call": result["call"], "notes": state,
        "source": payload.get("source") or {},
    }


def update_chapter_note(
    paths: Paths, name: str, chapter_id: str, note_id: str, *,
    action: str, text: str = "", reason: str = "",
) -> dict[str, Any]:
    """U8：接受 / 编辑 / 丢弃 / 退回一条要点。

    **只有 accept / edit 的条目会进下一章上下文**；discard 的绝不。
    """
    from workshop import notes as notes_mod

    work_dir = paths.work_dir(name)
    result = notes_mod.update_note(
        work_dir, chapter_id, note_id, action=action, text=text, reason=reason,
    )
    note = result["note"]
    if str(note.get("type")) == "state":
        # 派生索引跟着变；真源（setting.yaml）一动不动。
        notes_mod.rebuild_character_state(work_dir)
    return {"ok": True, "note": note, "notes": notes_state(paths, name, chapter_id)}


def rebuild_summary_snapshot(
    paths: Paths, name: str, chapter_id: str
) -> dict[str, Any]:
    """重建某一章的摘要快照（A5）。纯脚本，零成本。"""
    from workshop import summary as summary_mod

    ctx = _draft_inputs(paths, name, chapter_id)
    chapter_no = int((ctx["brief"] or {}).get("chapter_no") or 0)
    snap = summary_mod.build_snapshot(
        ctx["work_dir"], chapter_id, vol=int(ctx["vol_no"] or 0), chapter_no=chapter_no
    )
    return {"ok": True, "snapshot": snap, "summary_status": summary_mod.status(ctx["work_dir"])}


def finalize_status(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    from workshop import finalize as finalize_mod
    from workshop import summary as summary_mod
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    state = finalize_mod.load_state(work_dir, chapter_id)
    loaded = load_draft(work_dir, chapter_id)
    state["prose_sha_now"] = finalize_mod.sha256(str(loaded.get("text") or ""))
    state["outdated"] = bool(state.get("prose_sha")) and \
        state["prose_sha"] != state["prose_sha_now"]
    state["summary_status"] = summary_mod.status(work_dir)
    return state


def run_finalize(
    paths: Paths, name: str, chapter_id: str, *,
    steps: list[str] | None = None, force: bool = False,
) -> dict[str, Any]:
    """定稿后处理（B5）：快照 → 抽要点 → 摘要快照 → 角色状态索引。

    幂等：同一份正文重复调用不会重复写、不重复花钱；
    单步失败不拖垮其它步，可以只重试失败的那一步。
    """
    from workshop import draft_store, finalize as finalize_mod, notes as notes_mod
    from workshop.config import load_config
    from workshop.draft import count_chars, load_draft
    from workshop.secrets import SecretStore, setup_logging

    ctx = _draft_inputs(paths, name, chapter_id)
    loaded = load_draft(ctx["work_dir"], chapter_id)
    prose = str(loaded.get("text") or "")
    if not prose.strip():
        raise ValueError("还没有正文，无法做定稿后处理。")
    state = draft_store.get_status(ctx["work_dir"], chapter_id)
    if str(state.get("status") or "") != "final":
        raise ValueError("还没定稿。先把状态改成「已定稿」，再做定稿后处理。")

    want = list(steps or [])
    if "notes" in (want or finalize_mod.STEPS):
        existing = notes_mod.load_notes(ctx["work_dir"], chapter_id)
        same = str((existing.get("source") or {}).get("prose_sha") or "") == \
            finalize_mod.sha256(prose)
        if not (existing.get("notes") and same and not force):
            cfg = load_config(paths.root / "providers.yaml")
            # E2：定稿后的要点抽取走 extract 阶段，与「手动抽要点」用同一套配置。
            provider, model, _extract_stage = _resolve_stage(cfg, "extract")
            _require_key(cfg, provider, "章节要点抽取")

            def _extract() -> dict[str, Any]:
                store = SecretStore(paths.root / "config")
                setup_logging(store.known_values)
                client = _build_client(cfg, provider, store, timeout_sec=180)
                prior: list[dict[str, Any]] = []
                for path in sorted((ctx["work_dir"] / "90-draft").glob("*.notes.json")):
                    other = path.name[: -len(".notes.json")]
                    prior.extend(notes_mod.load_notes(ctx["work_dir"], other).get("notes") or [])
                result = notes_mod.run_extract(
                    client=client, model_id=model, provider_id=provider.id, prose=prose,
                    brief=ctx["brief"], setting=ctx["setting"], refs=_note_refs(ctx),
                    chapter_id=chapter_id, vol=int(ctx["vol_no"] or 0),
                    chapter_no=int((ctx["brief"] or {}).get("chapter_no") or 0),
                    prior_notes=prior, max_tokens=3000, work_dir=ctx["work_dir"],
                )
                notes_mod.save_extraction(
                    ctx["work_dir"], chapter_id, items=result["items"],
                    invalid=result["invalid"], prose=prose,
                    source={"provider": provider.id, "model": model,
                            "chars": len(prose)},
                    replace=True, call=result["call"],
                )
                _append_manifest_call(ctx["work_dir"], chapter_id, result["call"])
                return result
        else:
            _extract = None  # type: ignore[assignment]
    else:
        _extract = None

    chapter_no = int((ctx["brief"] or {}).get("chapter_no") or 0)
    result = finalize_mod.run(
        ctx["work_dir"], chapter_id, text=prose,
        meta={"vol": int(ctx["vol_no"] or 0), "chapter_no": chapter_no,
              "chars": count_chars(prose), "title": ctx["title"]},
        steps=steps, force=force, extractor=_extract,
    )
    result["chapter_id"] = chapter_id
    from workshop import summary as summary_mod

    result["summary_status"] = summary_mod.status(ctx["work_dir"])
    return result


def review_history(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    """审稿历史（A3）：每次审了几条、各状态多少、正文是否已经改过。"""
    from workshop import draft_store
    from workshop import review as review_mod
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    payload = review_mod.load_reviews(work_dir, chapter_id)
    prose = str(load_draft(work_dir, chapter_id).get("text") or "")
    prose_sha = review_mod.sha256(prose)
    reviews = review_mod.summarize(payload, prose_sha)
    latest = (payload.get("reviews") or [None])[-1]
    return {
        "chapter_id": chapter_id,
        "prose_sha": prose_sha,
        "chars": len(prose),
        "status": draft_store.get_status(work_dir, chapter_id),
        "reviews": reviews,
        "latest": ({
            "review_id": latest.get("review_id"),
            "created_at": latest.get("created_at"),
            "model": latest.get("model"),
            "summary": latest.get("summary"),
            "prose_sha": latest.get("prose_sha"),
            "chars": latest.get("chars"),
            "items": review_mod.public_items(latest, prose_sha),
            "invalid": latest.get("invalid") or [],
        } if latest else None),
    }


def draft_status(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    from workshop import draft_store
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    state = draft_store.get_status(work_dir, chapter_id)
    state["has_draft"] = bool(load_draft(work_dir, chapter_id).get("exists"))
    return state


def set_draft_status(
    paths: Paths, name: str, chapter_id: str, to: str, *, reason: str = ""
) -> dict[str, Any]:
    """D5 的闸门：未审不能定稿；定稿后要重写必须先 reopen。"""
    from workshop import draft_store
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    loaded = load_draft(work_dir, chapter_id)
    if to == "final":
        if not loaded.get("exists"):
            raise ValueError("还没有正文，不能定稿。")
        if not str(loaded.get("text") or "").strip():
            raise ValueError("正文是空的，不能定稿。")
    state = draft_store.set_status(work_dir, chapter_id, to, reason=reason, by="ui")
    state["has_draft"] = bool(loaded.get("exists"))
    if to == "final":
        # 定稿后处理是显式动作（其中抽要点会花钱），这里只提示，不偷偷跑。
        from workshop import finalize as finalize_mod

        fstate = finalize_mod.load_state(work_dir, chapter_id)
        state["finalize_hint"] = {
            "needed": str(fstate.get("status") or "idle") not in ("done",),
            "steps": list(finalize_mod.STEPS),
            "status": fstate.get("status") or "idle",
        }
    return state


def _selfcheck_key(text: str, brief: dict[str, Any] | None, setting: dict[str, Any] | None) -> str:
    """自检缓存的键：**正文 + 章指令 + 设定集**三者任一变化就换一把钥匙。

    只按正文 sha 缓存是不够的：作者改了章指令（字数、must_not）或设定集，
    自检结论也必须跟着变，否则就是拿旧标准判新稿。
    """
    import hashlib

    payload = json.dumps(
        {"text": text, "brief": brief or {}, "setting": setting or {}},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selfcheck_cache_path(work_dir: Path, chapter_id: str) -> Path:
    return work_dir / "90-draft" / ".cache" / "selfcheck" / f"{chapter_id}.json"


def _selfcheck_cached(
    work_dir: Path, chapter_id: str, text: str,
    brief: dict[str, Any] | None, setting: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """读自检结果：命中缓存就直接返回，不重跑 check_draft。

    缓存不是「少算一次」这么简单：`GET /draft` 是切章时会反复走的热路径，
    正文越长重算越贵；而自检是纯函数，输入没变就没有理由再算一遍。
    """
    from workshop.draft import check_draft

    key = _selfcheck_key(text, brief, setting)
    path = _selfcheck_cache_path(work_dir, chapter_id)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if str(cached.get("key") or "") == key:
            return (
                cached.get("errors") or [], cached.get("warnings") or [],
                cached.get("stats") or {},
                {"hit": True, "checked_at": cached.get("checked_at") or "",
                 "key": key[:12]},
            )
    except (OSError, ValueError):
        pass
    errors, warnings, stats = check_draft(text, brief=brief, setting=setting)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "key": key, "text_sha": __import__("hashlib").sha256(
                text.encode("utf-8")).hexdigest(), "chars": len(text),
            "errors": errors, "warnings": warnings, "stats": stats,
            "checked_at": now,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # 缓存写不进去不该让读正文失败——这只是加速层。
        pass
    return errors, warnings, stats, {"hit": False, "checked_at": now, "key": key[:12]}


def _stat_fingerprint(paths: list[Path]) -> str:
    """按文件 mtime/大小做指纹。正文/指令/设定任一改动都会换指纹。"""
    import hashlib

    rows = []
    for p in sorted(paths):
        try:
            st = p.stat()
        except OSError:
            rows.append(f"{p.name}:missing")
        else:
            rows.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha256("|".join(rows).encode("utf-8")).hexdigest()


def _draft_input_files(work_dir: Path, chapter_id: str) -> list[Path]:
    """所有会影响「读正文」结果的文件。

    只缓存正文文件是不够的：章指令、设定集、卷表、状态、审稿记录
    任一改动，返回的 stats/自检/状态摘要都会变。
    """
    files = [
        work_dir / "90-draft" / f"{chapter_id}.md",
        work_dir / "90-draft" / f"{chapter_id}.status.json",
        work_dir / "90-draft" / f"{chapter_id}.review.json",
        work_dir / "60-setting" / "setting.yaml",
        work_dir / "80-brief" / f"{chapter_id}.yaml",
    ]
    volume_dir = work_dir / "70-volume"
    if volume_dir.exists():
        files.extend(volume_dir.glob("*.yaml"))
    return files


def _draft_read_cache_path(work_dir: Path, chapter_id: str) -> Path:
    return work_dir / "90-draft" / ".cache" / "read" / f"{chapter_id}.json"


def read_draft(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    """读已写的正文 + 自检。

    读正文是切章/保存后都会走的热路径。这里按「影响到结果的文件的
    mtime+大小」缓存整份结果；文件一改指纹就变，缓存自动失效，
    不会拿旧自检去判新正文。命中时只做几次 stat，开销从 ~30ms 降到 ~1ms。
    """
    from workshop.draft import load_draft

    work_dir = paths.work_dir(name)
    key = _stat_fingerprint(_draft_input_files(work_dir, chapter_id))
    cache_path = _draft_read_cache_path(work_dir, chapter_id)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if str(cached.get("key") or "") == key and isinstance(cached.get("payload"), dict):
            payload = dict(cached["payload"])
            payload["read_cache"] = {"hit": True, "key": key[:12]}
            return payload
    except (OSError, ValueError):
        pass

    # 只有未命中时才解析 YAML（指令/设定/卷表）——命中路径只做几次 stat。
    ctx = _draft_inputs(paths, name, chapter_id)
    loaded = load_draft(work_dir, chapter_id)
    if not loaded.get("exists"):
        return {"exists": False, "chapter_id": chapter_id, "text": "", "meta": {},
                "errors": [], "warnings": [], "stats": {}}
    errors, warnings, stats, cache_info = _selfcheck_cached(
        work_dir, chapter_id, loaded["text"], ctx["brief"], ctx["setting"],
    )
    loaded.update({"errors": errors, "warnings": warnings, "stats": stats,
                   "selfcheck_cache": cache_info})
    loaded.update(_draft_state_summary(work_dir, chapter_id, loaded["text"]))
    loaded["read_cache"] = {"hit": False, "key": key[:12]}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"key": key, "payload": loaded},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_path)
    except (OSError, TypeError):
        pass
    return loaded


def _draft_state_summary(work_dir: Path, chapter_id: str, prose: str) -> dict[str, Any]:
    """稿子状态 + 最近一次审稿的摘要，随正文一起下发。

    界面不该为了画一个「已审/已定稿」的小标签再多跑两个请求——
    这类额外请求一多，切章时就会看到状态闪一下才出来。
    """
    from workshop import draft_store
    from workshop import review as review_mod

    state = draft_store.get_status(work_dir, chapter_id)
    payload = review_mod.load_reviews(work_dir, chapter_id)
    latest = (payload.get("reviews") or [None])[-1]
    summary = None
    if latest:
        summary = {
            "review_id": latest.get("review_id"),
            "created_at": latest.get("created_at"),
            "count": len(latest.get("items") or []),
            "invalid_count": len(latest.get("invalid") or []),
            "prose_sha": latest.get("prose_sha"),
            "stale": str(latest.get("prose_sha") or "") != review_mod.sha256(prose),
        }
    return {"status": state, "review_summary": summary}


def write_draft_text(paths: Paths, name: str, chapter_id: str, text: str) -> dict[str, Any]:
    """作者手改过的正文存回去。**空文本不许覆盖有内容的文件**。

    D4「保存不覆盖」：内容真的变了，先把旧稿快照成一版再写。
    没有这一版，手改就没有回退点，而作者是拿这个当唯一后悔药的。
    """
    from workshop.draft import count_chars, load_draft, save_draft
    from workshop import draft_store

    ctx = _draft_inputs(paths, name, chapter_id)
    existing = load_draft(ctx["work_dir"], chapter_id)
    old_text = existing.get("text") or ""
    if existing.get("exists") and not text.strip() and old_text.strip():
        raise ValueError(
            "要保存的正文是空的，但磁盘上这一章已经有内容。已拒绝写入，免得把正文清空。"
        )
    version_id = ""
    if existing.get("exists") and old_text != text:
        version = draft_store.snapshot_version(
            ctx["work_dir"], chapter_id, text=old_text,
            meta=existing.get("meta") or {}, reason="手改保存前自动备份",
        )
        version_id = (version or {}).get("version_id", "")
    saved = save_draft(ctx["work_dir"], chapter_id, text=text, meta={
        **(existing.get("meta") or {}), "edited": True, "chars": count_chars(text),
    })
    return {"saved": True, "version_id": version_id, "chars": count_chars(text), **saved}


# ── M9 创作台 · 从知识库导入 ────────────────────────────────
#
# 作者说知识库「可充当素材依据、可以用来参考、自己决定」——那就给他能翻的东西，
# 每条由他点。不做「一键全收」：整批搬进自己书里，他既没看过也没法反悔。


def kb_browse(paths: Paths, ref_work: str, section: str, q: str = "") -> dict[str, Any]:
    """翻某一本已入库作品的知识库。纯读，零成本。"""
    from workshop.setting_assist import KB_SECTION_MAP, LAYER_SPECS

    if section != "relations" and section not in KB_SECTION_MAP:
        raise ValueError(f"知识库里没有「{section}」这一类（可翻：{'、'.join(KB_SECTION_MAP)}、relations）")

    kb_dir = paths.work_dir(ref_work) / "20-kb"
    k1_path = kb_dir / "k1-entities.json"
    if not k1_path.exists():
        raise ValueError(f"「{ref_work}」还没有知识库（没标注过），先跑标注")
    try:
        k1 = json.loads(k1_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"「{ref_work}」的知识库读不了：{exc}") from exc
    if not k1.get("available"):
        raise ValueError(f"「{ref_work}」的知识库 K1 是空的，需要重建")

    key_field = "name"
    if section in KB_SECTION_MAP:
        key_field = LAYER_SPECS["setting"][KB_SECTION_MAP[section]]["key"]
    elif section == "terms":
        key_field = "term"

    rows: list[dict[str, Any]] = []
    for row in k1.get(section) or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get(key_field) or row.get("name") or row.get("from") or "").strip()
        if q and q not in name and q not in json.dumps(row, ensure_ascii=False):
            continue
        rows.append({"key": name, **{k: v for k, v in row.items() if not isinstance(v, (list, dict))}})

    counts = {k: len(k1.get(k) or []) for k in ("characters", "factions", "abilities",
                                               "locations", "terms", "relations")}
    return {
        "work": ref_work, "section": section, "counts": counts,
        "total": len(k1.get(section) or []), "shown": len(rows), "rows": rows[:500],
        "truncated": len(rows) > 500,
    }


def kb_import(
    paths: Paths, name: str, ref_work: str, section: str, *,
    keys: list[str] | None = None, document: dict[str, Any],
    layer: str = "setting", subject: str = "",
) -> dict[str, Any]:
    """把作者勾中的知识库条目**转成提案**，走同一套白名单与去重，再并进草稿。

    零成本：不调模型。这条路径做的判断和模型提案路径一样严——
    只是提案不是模型生成的，是从 K1 实体表里读出来的。
    """
    from workshop.setting_assist import (
        apply_proposals, kb_proposals, kb_relations_proposals,
    )

    kb_dir = paths.work_dir(ref_work) / "20-kb"
    k1_path = kb_dir / "k1-entities.json"
    if not k1_path.exists():
        raise ValueError(f"「{ref_work}」还没有知识库")
    k1 = json.loads(k1_path.read_text(encoding="utf-8-sig"))

    if section == "relations":
        if not subject:
            raise ValueError("导入关系要指明是谁的关系（subject）")
        proposals = kb_relations_proposals(k1, subject=subject)
    else:
        proposals = kb_proposals(k1, section=section, keys=keys)
    if not proposals:
        return {"document": document, "results": [], "proposal_count": 0,
                "note": "没有可导入的条目（勾的都没匹配上，或者这个人没有关系记录）"}

    new_document, results = apply_proposals(
        document, proposals, accepted=None, layer=layer, allow_partial=True,
    )
    return {
        "document": new_document, "results": results, "proposal_count": len(proposals),
        "imported": sum(1 for r in results if r["ok"]),
        "skipped": sum(1 for r in results if not r["ok"]),
    }


def foreshadow_ledger(
    paths: Paths, name: str, *, briefs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """一本伏笔账：埋了哪些、推到哪一步、哪些该收还没收。纯脚本，零成本。"""
    from workshop.chapter_brief import foreshadow_ledger as _ledger

    wd = _creation_work_dir(paths, name)
    return _ledger(wd / "80-brief", briefs=briefs)


def creation_character_presence(
    paths: Paths,
    name: str,
    *,
    briefs: dict[str, Any] | None = None,
    volumes: dict[int, Any] | None = None,
) -> dict[str, Any]:
    """人物动向：谁在第几章出场、谁已经连着好几章没露面。纯脚本，零成本。"""
    from workshop.creation import character_presence

    return character_presence(
        _creation_work_dir(paths, name), briefs=briefs, volumes=volumes
    )


def creation_consistency(
    paths: Paths,
    name: str,
    *,
    setting: dict[str, Any] | None = None,
    volumes: dict[int, Any] | None = None,
    briefs: dict[str, Any] | None = None,
) -> list[str]:
    """卷表/指令里点过名、但设定集里没有的人与势力。纯脚本，零成本。"""
    from workshop.creation import consistency_report

    return consistency_report(
        _creation_work_dir(paths, name), setting=setting, volumes=volumes, briefs=briefs
    )


def creation_synopsis(
    paths: Paths,
    name: str,
    *,
    setting: dict[str, Any] | None = None,
    volumes: dict[int, Any] | None = None,
) -> str:
    """全书梗概：从三层真源渲染，不新增字段。纯脚本，零成本。"""
    from workshop.creation import render_synopsis

    return render_synopsis(
        _creation_work_dir(paths, name), setting=setting, volumes=volumes
    )


def creation_story_gates(
    paths: Paths,
    name: str,
    *,
    setting: dict[str, Any] | None = None,
    volumes: dict[int, Any] | None = None,
    briefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """全书级硬闸：主角缺席、卷间接不上。纯脚本，零成本。"""
    from workshop.creation import story_gates

    errors, warnings = story_gates(
        _creation_work_dir(paths, name), setting=setting, volumes=volumes, briefs=briefs
    )
    return {"errors": errors, "warnings": warnings}


# ── 全文检索 ────────────────────────────────────────────────
#
# 检索是纯本地索引，不调用模型。这里只做薄封装，业务在 workshop.search。


def search_status(paths: Paths, *, sync_first: bool = False) -> dict[str, Any]:
    from workshop import search

    return search.status(paths.root, sync_first=sync_first)


def search_query(
    paths: Paths,
    text: str,
    *,
    source_types: str = "",
    work: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    from workshop import search

    return search.query(
        paths.root,
        text,
        source_types=source_types,
        work=work,
        limit=limit,
        offset=offset,
    )


def search_load_doc(paths: Paths, doc_id: str) -> dict[str, Any] | None:
    from workshop import search

    return search.load_doc(paths.root, doc_id)


def search_sync(paths: Paths, *, force: bool = False) -> dict[str, Any]:
    from workshop import search

    return search.sync(paths.root, force=force)


# ── 派生元数据库 ────────────────────────────────────────────
#
# 真源仍是 60-setting / 70-volume / 80-brief / 90-draft 下的文件。
# 这里只暴露查询与同步；库不可用时调用方读到的 `available=False`，
# 页面本身的读取路径会自动退回现读文件。


def meta_status(paths: Paths, *, sync_first: bool = False) -> dict[str, Any]:
    from workshop import metadata

    return metadata.status(paths.root, sync_first=sync_first)


def meta_sync(
    paths: Paths, *, force: bool = False, work: str | None = None, scope: str = "all"
) -> dict[str, Any]:
    from workshop import metadata

    return metadata.sync(paths.root, work=work, force=force, scope=scope)


def meta_chapters(paths: Paths, name: str) -> dict[str, Any]:
    """逐章派生状态（字数、段落、状态、要点/审稿计数、当前版本）。"""
    from workshop import metadata

    synced = metadata.sync(paths.root, work=name)
    states = metadata.chapter_states(paths.root, name)
    if states is None:
        return {"available": False, "sync": synced, "items": []}
    items = []
    for cid in sorted(states):
        row = states[cid]
        items.append(
            {
                "chapter_id": cid,
                "draft_exists": bool(row.get("draft_exists")),
                "chars": int(row.get("chars") or 0),
                "paragraphs": int(row.get("paragraphs") or 0),
                "draft_mtime": row.get("draft_mtime") or "",
                "status": row.get("status") or "draft",
                "status_updated_at": row.get("status_updated_at") or "",
                "notes_total": int(row.get("notes_total") or 0),
                "notes_pending": int(row.get("notes_pending") or 0),
                "review_total": int(row.get("review_total") or 0),
                "review_open": int(row.get("review_open") or 0),
                "reviews_count": int(row.get("reviews_count") or 0),
                "last_review_at": row.get("last_review_at") or "",
                "version_count": int(row.get("version_count") or 0),
                "current_version_id": row.get("current_version_id") or "",
                "candidate_count": int(row.get("candidate_count") or 0),
            }
        )
    return {"available": True, "sync": synced, "items": items}


def meta_tasks(
    paths: Paths, name: str, *, chapter_id: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """任务记录查询：等价于扫 `.tasks/*.json`，但走派生库的索引。"""
    from workshop import metadata

    synced = metadata.sync(paths.root, work=name)
    rows = metadata.task_history(paths.root, name, chapter_id=chapter_id, limit=limit)
    return {"available": rows is not None, "sync": synced, "items": rows or []}


def meta_reviews(paths: Paths, name: str, chapter_id: str) -> dict[str, Any]:
    """每次审稿的条数与状态计数。"""
    from workshop import metadata

    synced = metadata.sync(paths.root, work=name)
    rows = metadata.review_counters(paths.root, name, chapter_id)
    return {"available": rows is not None, "sync": synced, "items": rows or []}
