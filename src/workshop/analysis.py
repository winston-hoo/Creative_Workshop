"""结构体检报告：把散在文件里的标注数据聚成一份能看出问题的报告。

## 一条硬要求：零标注也要能出报告

如果报告必须等全量标注跑完才有，那么它的价值就全压在「愿不愿意先花一次钱」上。
这与本工坊的「能力可退化」原则冲突。

所以报告分成两类小节：

| 类型 | 数据来源 | 没有标注时 |
|---|---|---|
| 脚本轨小节（长度曲线、风格违规、分卷统计） | manifest + 章节文件，**零模型成本** | 照常出 |
| 模型轨小节（情绪、冲突、动机、钩子、爽点、伏笔） | 已落盘的标注 | 显式写「无数据」 |

**「无数据」要明写，不能用空数组假装什么都正常。** 空曲线和「没跑过」在界面上
必须一眼分得开，否则就是又一次静默失效。

## 关于 1-5 量表字段

实测同一章跑三次，冲突等级给过 3、3、4——极差 1。
所以报告里的量表字段一律**看趋势、看分卷均值**，不拿单章的精确值下结论。
这一点在每节的小字里都写明了。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .annotate import load_annotation
from .batch import load_chapter_tasks
from .ledger import Ledger

REPORT_SCHEMA_VERSION = "healthcheck-v1"

# 连续多少章量表值不变，才算「节奏走平」的信号。
# 量表实测有 ±1 噪声，2 章的波动可能只是噪声，3 章以上才值得看。
FLAT_RUN_MIN = 3

# 章字数异常判据。用中位数而不是均值——网文里存在大量超短章与超长章，
# 均值会被它们拉偏，中位数更能代表「常态章长」。
SHORT_CHAPTER_RATIO = 0.4
LONG_CHAPTER_RATIO = 2.5
# 极短章单独判：它不是「写得短」，而更像切分残留或空白章，性质不同，
# 混进「相对过短」里会淹没真正该看的信号。
ABSOLUTE_MIN_CHARS = 200


@dataclass
class Report:
    work: str
    generated_at: str
    coverage: dict[str, Any]
    sections: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "work": self.work,
            "generated_at": self.generated_at,
            "coverage": self.coverage,
            "sections": self.sections,
        }


# ── 采集 ──────────────────────────────────────────────────


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def collect_annotations(annotations_dir: Path, chapter_ids: list[str]) -> dict[str, dict[str, Any]]:
    """按章节 id 读标注。读不了的跳过并记录，不静默当「没有」。"""
    out: dict[str, dict[str, Any]] = {}
    for cid in chapter_ids:
        record = load_annotation(Path(annotations_dir) / f"{cid}.json")
        if record:
            out[cid] = record
    return out


def build_report(
    *,
    work_name: str,
    ingest_dir: Path,
    annotations_dir: Path,
    ledger_path: Path | None = None,
    stale_threshold: int = 30,
) -> Report:
    tasks = load_chapter_tasks(ingest_dir)
    by_id = {t.chapter_id: t for t in tasks}
    order = [t.chapter_id for t in tasks]

    annotations = collect_annotations(annotations_dir, order)
    annotated_ids = [cid for cid in order if cid in annotations]

    coverage = {
        "chapters": len(tasks),
        "annotated": len(annotated_ids),
        "ratio": round(len(annotated_ids) / len(tasks), 3) if tasks else 0.0,
        "script_track_available": True,
        "model_track_available": bool(annotated_ids),
        "note": (
            ""
            if annotated_ids
            else "还没有任何标注。以下只有脚本轨小节有数据，它们不需要模型，随时可看。"
        ),
    }

    sections: dict[str, Any] = {}
    sections["length"] = _length_section(tasks)
    sections["volumes"] = _volume_section(tasks, annotations)
    sections["style_violations"] = _style_section(annotations, by_id)
    sections["emotion_conflict"] = _series_section(
        tasks,
        annotations,
        keys=[("emotion", "情绪值"), ("conflict", "冲突等级")],
        title="情绪值与冲突等级",
    )
    sections["motive"] = _motive_section(tasks, annotations)
    # 断点判定需要一个「当前进度」。章号在分段重启时会回落，
    # 所以取全书见过的最大章号作为进度基准（近似，但比不传强——
    # 不传的话 stale_items 恒为空，疑似断点永远是 0，那是个静默的假绿灯）。
    chapter_nos = [t.chapter_no for t in tasks if t.chapter_no is not None]
    current_chapter_no = max(chapter_nos) if chapter_nos else None
    sections["hook_types"] = _enum_section(annotations, key="hook_type", title="钩子类型分布")
    sections["payoffs"] = _payoff_section(annotations)
    sections["foreshadow"] = _foreshadow_section(
        ledger_path, annotations, stale_threshold, current_chapter_no=current_chapter_no
    )
    sections["review"] = _review_section(annotations)
    sections["consistency"] = _consistency_section(tasks, annotations)

    return Report(
        work=work_name,
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        coverage=coverage,
        sections=sections,
    )


# ── 各小节 ────────────────────────────────────────────────


def _length_section(tasks: list) -> dict[str, Any]:
    counts = [t.char_count for t in tasks if t.char_count]
    points = [
        {
            "chapter_id": t.chapter_id,
            "chapter_no": t.chapter_no,
            "vol_no": t.vol_no,
            "title": t.title,
            "chars": t.char_count,
        }
        for t in tasks
    ]
    if not counts:
        return {"available": False, "reason": "没有章节数据", "points": []}

    median = statistics.median(counts)
    short = [p for p in points if p["chars"] < median * SHORT_CHAPTER_RATIO]
    long = [p for p in points if p["chars"] > median * LONG_CHAPTER_RATIO]
    tiny = [p for p in points if p["chars"] < ABSOLUTE_MIN_CHARS]
    return {
        "available": True,
        "avg": round(statistics.mean(counts)),
        "median": int(median),
        "min": min(counts),
        "max": max(counts),
        "stdev": round(statistics.pstdev(counts), 1),
        "points": points,
        "outliers": {
            "short": short[:40],
            "long": long[:40],
            "tiny": tiny[:40],
            "short_total": len(short),
            "long_total": len(long),
            "tiny_total": len(tiny),
            "criteria": f"过短 < 中位数×{SHORT_CHAPTER_RATIO}；过长 > 中位数×{LONG_CHAPTER_RATIO}；"
            f"极短 < {ABSOLUTE_MIN_CHARS} 字（更像切分残留，单独列）。"
            "用中位数而非均值，避免被极端章拉偏。",
        },
    }


def _volume_section(tasks: list, annotations: dict[str, dict]) -> dict[str, Any]:
    buckets: dict[int, list[dict[str, Any]]] = {}
    for task in tasks:
        record = annotations.get(task.chapter_id)
        buckets.setdefault(task.vol_no, []).append(
            {
                "chars": task.char_count,
                "emotion": _safe_int((record or {}).get("fields", {}).get("emotion")),
                "conflict": _safe_int((record or {}).get("fields", {}).get("conflict")),
                "motive": _safe_int((record or {}).get("fields", {}).get("motive_strength")),
            }
        )
    out = []
    for vol_no in sorted(buckets):
        rows = buckets[vol_no]
        chars = [r["chars"] for r in rows if r["chars"]]
        emotions = [r["emotion"] for r in rows if r["emotion"] is not None]
        conflicts = [r["conflict"] for r in rows if r["conflict"] is not None]
        motives = [r["motive"] for r in rows if r["motive"] is not None]
        out.append(
            {
                "vol_no": vol_no,
                "chapters": len(rows),
                "chars_avg": round(statistics.mean(chars)) if chars else None,
                "emotion_avg": round(statistics.mean(emotions), 2) if emotions else None,
                "conflict_avg": round(statistics.mean(conflicts), 2) if conflicts else None,
                "motive_avg": round(statistics.mean(motives), 2) if motives else None,
                "annotated": sum(1 for r in rows if r["emotion"] is not None),
            }
        )
    return {
        "available": bool(out),
        "items": out,
        "note": "分卷均值比单章值可靠：1-5 量表字段实测存在 ±1 噪声，看趋势可以，单章精确值不可尽信。",
    }


def _style_section(annotations: dict[str, dict], by_id: dict) -> dict[str, Any]:
    """风格违规。这一节完全来自脚本轨，不需要模型，永远可用。"""
    by_rule: dict[str, dict[str, Any]] = {}
    per_chapter: list[dict[str, Any]] = []
    for cid, record in annotations.items():
        violations = (record.get("fields") or {}).get("style_violations") or []
        if not violations:
            continue
        total = 0
        for item in violations:
            if not isinstance(item, dict):
                continue
            rule = str(item.get("rule_id") or "UNKNOWN")
            count = int(item.get("count") or 0)
            total += count
            entry = by_rule.setdefault(
                rule, {"rule_id": rule, "desc": item.get("desc") or "", "count": 0, "chapters": 0}
            )
            entry["count"] += count
            entry["chapters"] += 1
        task = by_id.get(cid)
        per_chapter.append(
            {
                "chapter_id": cid,
                "chapter_no": (record.get("chapter_no") if task is None else task.chapter_no),
                "title": (task.title if task else record.get("title") or ""),
                "count": total,
                "rules": [str(v.get("rule_id")) for v in violations if isinstance(v, dict)],
            }
        )
    per_chapter.sort(key=lambda x: -x["count"])
    return {
        "available": bool(annotations),
        "by_rule": sorted(by_rule.values(), key=lambda x: -x["count"]),
        "top_chapters": per_chapter[:30],
        "chapters_with_violations": len(per_chapter),
        "note": (
            "违规由正则检出，零模型成本。判定方向相对于作品约定人称——"
            "第三人称小说里找的是漂移到第一人称的段落，方向反了会满篇假警。"
            if annotations
            else "还没有标注，风格违规检测需要跑脚本轨（可用 --script-only，不花钱）。"
        ),
    }


def _series_section(tasks: list, annotations: dict, *, keys: list[tuple[str, str]], title: str) -> dict[str, Any]:
    series = {key: [] for key, _label in keys}
    labels = {key: label for key, label in keys}
    for task in tasks:
        record = annotations.get(task.chapter_id)
        fields = (record or {}).get("fields") or {}
        for key, _label in keys:
            series[key].append(
                {
                    "chapter_id": task.chapter_id,
                    "chapter_no": task.chapter_no,
                    "vol_no": task.vol_no,
                    "title": task.title,
                    "value": _safe_int(fields.get(key)),
                }
            )
    available = any(any(p["value"] is not None for p in series[key]) for key, _ in keys)

    # 数字本身不构成结论——连续 N 章量表值不变才是「该去看一眼」的信号。
    # 网文里情绪连着几章走平，多半意味着没有新事件进来。
    # 量表有 ±1 噪声，所以阈值取 3 章以上，2 章的波动不算。
    flat: list[dict[str, Any]] = []
    for key, _label in keys:
        points = series[key]
        run: list[dict[str, Any]] = []
        for point in points:
            if point["value"] is None:
                if len(run) >= FLAT_RUN_MIN:
                    flat.append({"field": labels[key], "chapters": run})
                run = []
                continue
            if run and run[-1]["value"] != point["value"]:
                if len(run) >= FLAT_RUN_MIN:
                    flat.append({"field": labels[key], "chapters": run})
                run = []
            run.append(point)
        if len(run) >= FLAT_RUN_MIN:
            flat.append({"field": labels[key], "chapters": run})

    return {
        "available": available,
        "title": title,
        "labels": labels,
        "series": series,
        "flat_runs": flat[:40],
        "flat_runs_total": len(flat),
        "flat_run_min": FLAT_RUN_MIN,
        "reason": "" if available else "还没有标注数据",
    }


def _motive_section(tasks: list, annotations: dict) -> dict[str, Any]:
    points = []
    weak: list[dict[str, Any]] = []
    for task in tasks:
        record = annotations.get(task.chapter_id)
        value = _safe_int(((record or {}).get("fields") or {}).get("motive_strength"))
        if value is None:
            continue
        row = {
            "chapter_id": task.chapter_id,
            "chapter_no": task.chapter_no,
            "vol_no": task.vol_no,
            "title": task.title,
            "value": value,
        }
        points.append(row)
        if value <= 2:
            weak.append(row)
    values = [p["value"] for p in points]
    return {
        "available": bool(points),
        "avg": round(statistics.mean(values), 2) if values else None,
        "points": points,
        "weak_chapters": weak[:40],
        "weak_total": len(weak),
        "reason": "" if points else "还没有标注数据，无法统计动机线",
        "note": (
            "≤2 的章节列在弱章里。注意：core_motive 为空时这一列度量对象未定义，"
            "模型会自己猜一个动机打分，此时数值不可信。"
        ),
    }


def _enum_section(annotations: dict, *, key: str, title: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in annotations.values():
        value = ((record.get("fields") or {}).get(key))
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return {
        "available": bool(counts),
        "title": title,
        "counts": counts,
        "total": sum(counts.values()),
        "reason": "" if counts else f"还没有标注数据，无法统计{title}",
    }


def _payoff_section(annotations: dict) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    strengths: list[int] = []
    total = 0
    per_chapter: list[dict[str, Any]] = []
    for cid, record in annotations.items():
        payoffs = ((record.get("fields") or {}).get("payoffs")) or []
        if not isinstance(payoffs, list):
            continue
        total += len(payoffs)
        for item in payoffs:
            if not isinstance(item, dict):
                continue
            ptype = str(item.get("类型") or "未标")
            by_type[ptype] = by_type.get(ptype, 0) + 1
            strength = _safe_int(item.get("强度"))
            if strength is not None:
                strengths.append(strength)
        if payoffs:
            per_chapter.append(
                {
                    "chapter_id": cid,
                    "chapter_no": record.get("chapter_no"),
                    "title": record.get("title") or "",
                    "count": len(payoffs),
                }
            )
    per_chapter.sort(key=lambda x: -x["count"])
    return {
        "available": total > 0,
        "total": total,
        "by_type": by_type,
        "strength_avg": round(statistics.mean(strengths), 2) if strengths else None,
        "top_chapters": per_chapter[:20],
        "reason": "" if total else "还没有标注数据",
    }


def _foreshadow_section(
    ledger_path: Path | None,
    annotations: dict,
    stale_threshold: int,
    *,
    current_chapter_no: int | None = None,
) -> dict[str, Any]:
    # 台账不存在时，退回到标注文件里散落的伏笔动作，而不是直接说「没有伏笔」。
    # 两者含义完全不同：前者是没跑过台账，后者是这本书真的没埋伏笔。
    if ledger_path is None or not Path(ledger_path).exists():
        scattered = 0
        for record in annotations.values():
            items = ((record.get("fields") or {}).get("foreshadows")) or []
            scattered += len(items) if isinstance(items, list) else 0
        return {
            "available": False,
            "reason": "还没有生成伏笔台账" + (f"（标注里有 {scattered} 条伏笔动作尚未归集）" if scattered else ""),
        }

    ledger = Ledger.load(ledger_path)
    stale = ledger.stale_items(current_chapter_no=current_chapter_no, threshold=stale_threshold)
    open_items = ledger.open_items
    return {
        "available": True,
        "total": len(ledger.items),
        "open": len(open_items),
        "recovered": len(ledger.items) - len(open_items),
        "stale": len(stale),
        "stale_threshold": stale_threshold,
        "items": [
            {
                "id": item.id,
                "desc": item.desc,
                "status": item.status,
                "planted_chapter_id": item.planted_chapter_id,
                "planted_chapter_no": item.planted_chapter_no,
                "last_touched_chapter_no": item.last_touched_chapter_no,
                "events": len(item.events),
                "is_stale": item in stale,
            }
            for item in ledger.items
        ],
        "note": f"连续 {stale_threshold} 章没有推进的未回收伏笔记为疑似断点。这是整份报告里最该先看的一节。",
    }


# 脚本轨只分「第一人称 / 第三人称」，模型轨还细分限知与全知。
# 这两列本来就不该逐字相等，所以要先归一化再比，否则全是假告警。
def _perspective_family(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if "第一" in text or "我" in text:
        return "first"
    if "第三" in text:
        return "third"
    return "unknown"


def _consistency_section(tasks: list, annotations: dict) -> dict[str, Any]:
    """自洽性检查：同一份数据里有没有互相矛盾的东西。

    两个都来自真实踩坑：
      · 脚本轨与模型轨的人称不一致——脚本轨是启发式的，遇到自由间接引语
        （第三人称叙述 + 大段「我」的内心独白）容易判反，这时模型轨更可信，
        但**没人告诉用户去看**。不一致就该列出来让人核对。
      · 同一部作品里混用了不同的提示词前缀——加字段、改约束都会让
        prefix_hash 变。前后两批数据的字段口径不同，混在一起算趋势会得出
        错误结论，而这一点从单个标注文件里完全看不出来。
    """
    mismatched: list[dict[str, Any]] = []
    prefix_counts: dict[str, int] = {}

    for task in tasks:
        record = annotations.get(task.chapter_id)
        if not record:
            continue
        fields = record.get("fields") or {}
        prov = record.get("provenance") or {}

        ph = str(prov.get("prefix_hash") or "")
        if ph:
            prefix_counts[ph] = prefix_counts.get(ph, 0) + 1

        script_side = _perspective_family(fields.get("perspective_detected"))
        model_side = _perspective_family(fields.get("perspective"))
        if script_side and model_side and script_side != model_side:
            mismatched.append(
                {
                    "chapter_id": task.chapter_id,
                    "chapter_no": task.chapter_no,
                    "title": task.title,
                    "script": fields.get("perspective_detected"),
                    "model": fields.get("perspective"),
                }
            )

    return {
        "available": bool(annotations),
        "perspective_mismatch": mismatched[:60],
        "perspective_mismatch_total": len(mismatched),
        "prefix_hashes": [
            {"prefix_hash": k, "chapters": v}
            for k, v in sorted(prefix_counts.items(), key=lambda kv: -kv[1])
        ],
        "note": (
            "人称不一致时以模型轨为准：脚本轨是启发式判断，遇到自由间接引语"
            "（第三人称叙述 + 大量「我」的内心独白）容易判反，它只用来提示「这里值得看一眼」。"
            "若出现多个前缀指纹，说明这些标注用的不是同一套提示词（改过字段或约束），"
            "字段口径不同、不能直接放在一起比趋势。"
        ),
    }


def _review_section(annotations: dict) -> dict[str, Any]:
    by_action: dict[str, int] = {}
    needs: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    for cid, record in annotations.items():
        action = str(record.get("review_action") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
        if record.get("status") == "needs_review":
            needs.append(
                {
                    "chapter_id": cid,
                    "chapter_no": record.get("chapter_no"),
                    "title": record.get("title") or "",
                    "review_action": action,
                    "issues": list(record.get("issues") or [])[:3],
                }
            )
        for item in record.get("validation_issues") or []:
            issue_counts[str(item)[:60]] = issue_counts.get(str(item)[:60], 0) + 1
    needs.sort(key=lambda x: (x["chapter_no"] if isinstance(x["chapter_no"], int) else 0))
    return {
        "available": bool(annotations),
        "by_action": by_action,
        "needs_review": needs[:50],
        "needs_review_total": len(needs),
        "validation_issues": sorted(
            ({"text": k, "count": v} for k, v in issue_counts.items()),
            key=lambda x: -x["count"],
        )[:20],
        "note": "只强制复核低置信度章节。自评置信度由模型给出，用于分级，不作最终结论。",
    }


# ── 落盘与渲染 ────────────────────────────────────────────


def save_report(report: Report, out_dir: Path, *, stamp: str | None = None) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%dT%H%M%S")
    json_path = out / f"healthcheck-{stamp}.json"
    md_path = out / f"healthcheck-{stamp}.md"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    latest_json = out / "latest.json"
    latest_md = out / "latest.md"
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"json": json_path, "markdown": md_path, "latest_json": latest_json, "latest_md": latest_md}


def render_markdown(report: Report) -> str:
    cov = report.coverage
    s = report.sections
    lines: list[str] = []
    lines.append(f"# 结构体检报告 · {report.work}")
    lines.append("")
    lines.append(f"生成时间　{report.generated_at}")
    lines.append(
        f"标注覆盖　{cov['annotated']}/{cov['chapters']} 章（{cov['ratio'] * 100:.1f}%）"
    )
    if cov["note"]:
        lines.append("")
        lines.append(f"> {cov['note']}")
    lines.append("")

    # ① 章节长度
    length = s.get("length") or {}
    lines.append("## ① 章节长度")
    if not length.get("available"):
        lines.append("无数据。")
    else:
        lines.append(
            f"平均 {length['avg']} 字　中位数 {length['median']} 字　"
            f"最短 {length['min']}　最长 {length['max']}　标准差 {length['stdev']}"
        )
        lines.append("")
        lines.append(
            f"异常章：过短 {length['outliers']['short_total']} 章、"
            f"过长 {length['outliers']['long_total']} 章、"
            f"极短 {length['outliers']['tiny_total']} 章"
        )
        lines.append("")
        lines.append(f"判据：{length['outliers']['criteria']}")
        if length["outliers"]["tiny"]:
            lines.append("")
            lines.append("极短章（前 10，优先怀疑切分残留）：")
            lines.append("| 章节 | 标题 | 字数 |")
            lines.append("|---|---|---|")
            for row in length["outliers"]["tiny"][:10]:
                lines.append(f"| 第{row['chapter_no']}章 | {row['title']} | {row['chars']} |")
        if length["outliers"]["short"]:
            lines.append("")
            lines.append("过短章节（前 10）：")
            lines.append("| 章节 | 标题 | 字数 |")
            lines.append("|---|---|---|")
            for row in length["outliers"]["short"][:10]:
                lines.append(f"| 第{row['chapter_no']}章 | {row['title']} | {row['chars']} |")
        if length["outliers"]["long"]:
            lines.append("")
            lines.append("过长章节（前 10）：")
            lines.append("| 章节 | 标题 | 字数 |")
            lines.append("|---|---|---|")
            for row in length["outliers"]["long"][:10]:
                lines.append(f"| 第{row['chapter_no']}章 | {row['title']} | {row['chars']} |")
    lines.append("")

    # ② 情绪与冲突
    ec = s.get("emotion_conflict") or {}
    lines.append("## ② 情绪与冲突")
    if not ec.get("available"):
        lines.append(ec.get("reason") or "无数据。")
    else:
        flat = ec.get("flat_runs") or []
        lines.append(f"节奏走平的区段（连续 {ec['flat_run_min']} 章以上量表值不变）：{ec['flat_runs_total']} 处")
        lines.append("")
        if flat:
            lines.append("| 字段 | 章节 | 值 |")
            lines.append("|---|---|---|")
            for run in flat[:20]:
                nos = "、".join(str(c["chapter_no"]) for c in run["chapters"])
                lines.append(f"| {run['field']} | 第{nos}章 | {run['chapters'][0]['value']} |")
            lines.append("")
            lines.append("> 连着几章没变化，多半意味着没有新事件进来。1-5 量表有 ±1 噪声，2 章的波动不算。")
        else:
            lines.append("没有连续走平的区段。")
    lines.append("")

    # ③ 分卷
    volumes = s.get("volumes") or {}
    lines.append("## ③ 分卷概览")
    if volumes.get("items"):
        lines.append("| 卷 | 章数 | 已标注 | 平均字数 | 情绪均值 | 冲突均值 | 动机均值 |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in volumes["items"]:
            lines.append(
                f"| 第{row['vol_no']}卷 | {row['chapters']} | {row['annotated']} | "
                f"{row['chars_avg'] or '—'} | {row['emotion_avg'] or '—'} | "
                f"{row['conflict_avg'] or '—'} | {row['motive_avg'] or '—'} |"
            )
        lines.append("")
        lines.append(f"> {volumes['note']}")
    else:
        lines.append("无数据。")
    lines.append("")

    # ③ 伏笔
    fs = s.get("foreshadow") or {}
    lines.append("## ④ 伏笔追踪")
    if not fs.get("available"):
        lines.append(fs.get("reason") or "无数据。")
    else:
        lines.append(
            f"共 {fs['total']} 条　未回收 {fs['open']}　已回收 {fs['recovered']}　"
            f"疑似断点 {fs['stale']}"
        )
        lines.append("")
        lines.append(f"> {fs['note']}")
        stale_rows = [i for i in fs["items"] if i["is_stale"]]
        if stale_rows:
            lines.append("")
            lines.append("疑似断点：")
            lines.append("| 编号 | 描述 | 埋设章 | 最后推进章 |")
            lines.append("|---|---|---|---|")
            for row in stale_rows[:20]:
                lines.append(
                    f"| {row['id']} | {row['desc']} | 第{row['planted_chapter_no']}章 | "
                    f"第{row['last_touched_chapter_no']}章 |"
                )
        open_rows = [i for i in fs["items"] if i["status"] == "open" and not i["is_stale"]]
        if open_rows:
            lines.append("")
            lines.append(f"未回收（非断点）共 {len(open_rows)} 条，列前 20：")
            lines.append("| 编号 | 描述 | 埋设章 | 事件数 |")
            lines.append("|---|---|---|---|")
            for row in open_rows[:20]:
                lines.append(
                    f"| {row['id']} | {row['desc']} | 第{row['planted_chapter_no']}章 | {row['events']} |"
                )
    lines.append("")

    # ④ 风格违规
    style = s.get("style_violations") or {}
    lines.append("## ⑤ 风格规范违规")
    if not style.get("available"):
        lines.append(style.get("note") or "无数据。")
    else:
        lines.append(f"涉及 {style['chapters_with_violations']} 章　规则命中：")
        lines.append("")
        lines.append("| 规则 | 说明 | 处数 | 涉及章数 |")
        lines.append("|---|---|---|---|")
        for row in style["by_rule"]:
            lines.append(f"| {row['rule_id']} | {row['desc']} | {row['count']} | {row['chapters']} |")
        if style["top_chapters"]:
            lines.append("")
            lines.append("违规最多的章节（前 10）：")
            lines.append("| 章节 | 标题 | 处数 | 规则 |")
            lines.append("|---|---|---|---|")
            for row in style["top_chapters"][:10]:
                lines.append(
                    f"| 第{row['chapter_no']}章 | {row['title']} | {row['count']} | "
                    f"{'、'.join(row['rules'])} |"
                )
        lines.append("")
        lines.append(f"> {style['note']}")
    lines.append("")

    # ⑤ 钩子与爽点
    hooks = s.get("hook_types") or {}
    payoffs = s.get("payoffs") or {}
    lines.append("## ⑥ 钩子与爽点")
    if hooks.get("available"):
        lines.append(f"钩子类型分布（共 {hooks['total']} 章）：")
        lines.append("")
        lines.append("| 类型 | 章数 | 占比 |")
        lines.append("|---|---|---|")
        for key, value in sorted(hooks["counts"].items(), key=lambda kv: -kv[1]):
            ratio = value / hooks["total"] * 100 if hooks["total"] else 0
            lines.append(f"| {key} | {value} | {ratio:.1f}% |")
    else:
        lines.append(hooks.get("reason") or "无数据。")
    lines.append("")
    if payoffs.get("available"):
        lines.append(
            f"爽点共 {payoffs['total']} 处　平均强度 {payoffs['strength_avg']}　类型分布："
        )
        lines.append("")
        lines.append("| 类型 | 处数 |")
        lines.append("|---|---|")
        for key, value in sorted(payoffs["by_type"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {key} | {value} |")
    else:
        lines.append(payoffs.get("reason") or "无数据。")
    lines.append("")

    # ⑥ 动机线
    motive = s.get("motive") or {}
    lines.append("## ⑦ 主角动机线")
    if not motive.get("available"):
        lines.append("无数据。")
    else:
        lines.append(f"均值 {motive['avg']}　弱章（≤2）共 {motive['weak_total']} 章")
        lines.append("")
        lines.append(f"> {motive['note']}")
        if motive["weak_chapters"]:
            lines.append("")
            lines.append("弱章（前 15）：")
            lines.append("| 章节 | 标题 | 强度 |")
            lines.append("|---|---|---|")
            for row in motive["weak_chapters"][:15]:
                lines.append(f"| 第{row['chapter_no']}章 | {row['title']} | {row['value']} |")
    lines.append("")

    # ⑦ 自洽性
    cons = s.get("consistency") or {}
    lines.append("## ⑧ 自洽性检查")
    if not cons.get("available"):
        lines.append("无数据。")
    else:
        hashes = cons.get("prefix_hashes") or []
        if len(hashes) > 1:
            lines.append(
                f"⚠️ **检测到 {len(hashes)} 个不同的提示词前缀**，"
                "说明这些标注不是同一套口径产出的，跨批次的趋势对比不可靠："
            )
            lines.append("")
            lines.append("| 前缀指纹 | 章数 |")
            lines.append("|---|---|")
            for row in hashes:
                lines.append(f"| `{row['prefix_hash']}` | {row['chapters']} |")
        else:
            lines.append("提示词前缀一致（所有标注口径相同）。")
        lines.append("")
        total = cons.get("perspective_mismatch_total") or 0
        lines.append(f"脚本轨与模型轨人称不一致：{total} 章")
        if total:
            lines.append("")
            lines.append("| 章节 | 标题 | 脚本轨 | 模型轨 |")
            lines.append("|---|---|---|---|")
            for row in cons["perspective_mismatch"][:15]:
                lines.append(
                    f"| 第{row['chapter_no']}章 | {row['title']} | "
                    f"{row['script']} | {row['model']} |"
                )
        lines.append("")
        lines.append(f"> {cons['note']}")
    lines.append("")

    # ⑧ 复核队列
    review = s.get("review") or {}
    lines.append("## ⑨ 待复核")
    if not review.get("available"):
        lines.append("无数据。")
    else:
        action_text = "　".join(f"{k} {v}" for k, v in review["by_action"].items())
        lines.append(f"复核分级：{action_text}")
        lines.append("")
        lines.append(f"需人工复核共 {review['needs_review_total']} 章")
        if review["validation_issues"]:
            lines.append("")
            lines.append("校验问题（按出现次数）：")
            lines.append("| 问题 | 次数 |")
            lines.append("|---|---|")
            for row in review["validation_issues"][:10]:
                lines.append(f"| {row['text']} | {row['count']} |")
        lines.append("")
        lines.append(f"> {review['note']}")
    lines.append("")
    return "\n".join(lines)
