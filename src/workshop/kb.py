"""知识库构建：K1 实体卡片 / K2 四类台账 / K3 作品指纹。

设计出处：`小说创作工坊-通用设计方案.md` §6 目录结构与 §7 模块设计。

分层：
  · K1 实体卡片  —— 人物/势力/能力/地点（原料：实体统计 latest.json）
  · K2 四类台账  —— 人物 / 支线 / 时间线 / 伏笔（伏笔已有 ledger.py，这里聚合另外三类）
  · K3 作品指纹  —— L3 条目：命中率 / 置信度 / 断点章节

纪律（与全项目一致）：
  · **只聚合已有数据，不编造**。模型没有输出的维度（如支线名称）就显式标注
    「数据不足」，不硬造一个名字出来。
  · 纯脚本、零模型成本。全部用正则 / 计数 / 序列分析完成。
  · 落盘在 `20-kb/`，与伏笔台账同层。

## 数据源与局限

逐章标注里**没有人物名**（那是实体统计的活），所以在 K2 人物台账里，
「出场章」是通过把实体词表（K1 的 characters）拿去正文做包含匹配得出来的。
这是纯字符串匹配，会有「名字太短误命中」的噪声；命中数很少的条目
会标注「疑似」，不假装精确。

支线台账：标注只有 `subplot_count`（几根支线在场），没有支线的名字和编号。
所以这里只给「在场区间」与「变化点」，不编支线名。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .annotate import load_annotation, strip_front_matter
from .batch import load_chapter_tasks

KB_SCHEMA_VERSION = "kb-v1"
# 1-5 量表的走平阈值（与体检报告一致：实测有 ±1 噪声，3 章以上才值得看）
FLAT_RUN_MIN = 3
# 名字太短容易误命中正文，低于这个长度不做人物出场匹配
MIN_NAME_LEN = 2


@dataclass
class KBResult:
    work: str
    generated_at: str
    k1: dict[str, Any]
    k2: dict[str, Any]
    k3: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": KB_SCHEMA_VERSION,
            "work": self.work,
            "generated_at": self.generated_at,
            "k1": self.k1,
            "k2": self.k2,
            "k3": self.k3,
        }


# ── K1 实体卡片 ──────────────────────────────────────────


def build_k1(work_name: str, entities_latest: dict[str, Any] | None) -> dict[str, Any]:
    """K1 实体卡片：直接从实体统计合并结果复制，不重算。"""
    if not entities_latest:
        return {
            "available": False,
            "note": "还没生成过实体统计（世界观与人物）。这是知识库人物数据的来源。",
            "characters": [],
            "factions": [],
            "abilities": [],
            "locations": [],
        }
    merged = entities_latest.get("merged") or {}
    return {
        "available": True,
        "entities_generated_at": entities_latest.get("generated_at"),
        "model": entities_latest.get("model"),
        "counts": merged.get("counts") or {},
        "characters": merged.get("characters") or [],
        "factions": merged.get("factions") or [],
        "abilities": merged.get("abilities") or [],
        "locations": merged.get("locations") or [],
        "relations": merged.get("relations") or [],
    }


# ── K2 台账 ──────────────────────────────────────────────


def _match_characters_in_body(characters: list[dict[str, Any]], body: str) -> set[str]:
    """把人物名拿到正文里做包含匹配。返回命中的人名集合。"""
    names = []
    for c in characters:
        name = str(c.get("name") or "").strip()
        if len(name) >= MIN_NAME_LEN:
            names.append(name)
    if not names:
        return set()
    hits = set()
    for name in names:
        if name in body:
            hits.add(name)
    return hits


def build_k2(
    work_name: str,
    k1: dict[str, Any],
    tasks: list,
    annotations: dict[str, dict[str, Any]],
    corpus: dict[str, str],
) -> dict[str, Any]:
    """K2 素材台账：人物 / 支线 / 时间线（伏笔台账是别处已有的文件）。

    - 人物台账：出场章列表、首次/最后出场、出场章数占比（正文匹配，纯脚本）
    - 支线台账：subplot_count 序列 → 在场区间与变化点（无支线名，明确说明）
    - 时间线台账：time_span 枚举 × 章号（事件与跨度，标注返回什么就记什么）
    """
    order = [t.chapter_id for t in tasks]
    chap_no_of = {t.chapter_id: t.chapter_no for t in tasks}

    # ── 人物出场 ──
    characters = k1.get("characters") or []
    person_rows: list[dict[str, Any]] = []
    for c in characters:
        name = str(c.get("name") or "")
        # 别名也算出场。只认正名的话，「出场章数」量的是名字拼写而不是人——
        # 张老师一个人 164 章，加上张老鳖/张教授才是他真正的在场长度。
        also = [str(a) for a in (c.get("aliases") or []) if str(a).strip()]
        needles = [n for n in [name, *also] if len(n) >= MIN_NAME_LEN]
        appearance_chapters: list[int] = []
        for cid in order:
            body = corpus.get(cid, "")
            if needles and any(n in body for n in needles):
                no = chap_no_of.get(cid)
                if no is not None:
                    appearance_chapters.append(no)
        if not appearance_chapters:
            # 实体统计有名字但正文没命中：可能是名字太泛（如「老爹」）或过短
            continue
        person_rows.append(
            {
                "name": name,
                "aliases": also,
                "role": c.get("role"),
                "identity": c.get("identity"),
                "first_chapter": c.get("first_chapter"),
                "appearance_chapters": appearance_chapters,
                "appearance_count": len(appearance_chapters),
                "first_seen": appearance_chapters[0],
                "last_seen": appearance_chapters[-1],
                "factions": c.get("factions") or [],
                "abilities": c.get("abilities") or [],
            }
        )
    person_rows.sort(key=lambda r: (r["first_seen"], r["name"]))
    total_chapters = len(order)
    for row in person_rows:
        row["coverage"] = round(row["appearance_count"] / total_chapters, 3) if total_chapters else 0

    # ── 支线在场区间 ──
    sideplot_points: list[dict[str, Any]] = []
    for cid in order:
        rec = annotations.get(cid) or {}
        count = (rec.get("fields") or {}).get("subplot_count")
        sideplot_points.append(
            {
                "chapter_id": cid,
                "chapter_no": chap_no_of.get(cid),
                "subplot_count": count if isinstance(count, int) else 0,
            }
        )
    # 区段：连续 subplot_count > 0
    sideplot_ranges: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for p in sideplot_points:
        if p["subplot_count"] > 0:
            current.append(p)
        else:
            if current:
                sideplot_ranges.append(_range_of(current))
                current = []
    if current:
        sideplot_ranges.append(_range_of(current))
    # 变化点：subplot_count 从上一步变化的位置
    changes: list[dict[str, Any]] = []
    for i in range(1, len(sideplot_points)):
        a, b = sideplot_points[i - 1], sideplot_points[i]
        if a["subplot_count"] != b["subplot_count"]:
            changes.append(
                {
                    "at_chapter": b["chapter_no"],
                    "from": a["subplot_count"],
                    "to": b["subplot_count"],
                }
            )

    # ── 时间线 ──
    timeline_events: list[dict[str, Any]] = []
    span_counts: dict[str, int] = {}
    for cid in order:
        rec = annotations.get(cid) or {}
        fields = rec.get("fields") or {}
        span = fields.get("time_span")
        no = chap_no_of.get(cid)
        if span:
            span_counts[str(span)] = span_counts.get(str(span), 0) + 1
            timeline_events.append(
                {
                    "chapter_no": no,
                    "chapter_id": cid,
                    "time_span": span,
                    "summary": (fields.get("chapter_summary") or "")[:40],
                }
            )

    return {
        "person": {
            "note": "出场章来自实体词表与正文的包含匹配（纯脚本），别名一并计入。名字过短或太泛的不会列出。",
            "count": len(person_rows),
            "rows": person_rows[:400],
        },
        "sideplot": {
            "note": "标注只记录「几根支线在场」，没有支线名字。在场区间与变化点是全部可得的信息。",
            "ranges": sideplot_ranges,
            "changes": changes,
        },
        "timeline": {
            "note": "时间跨度来自标注的 time_span 字段（即时/数日/跨月）。",
            "span_counts": span_counts,
            "events": timeline_events[:600],
        },
        "foreshadow": {
            "note": "伏笔台账在 k2-material/foreshadow-ledger.json，由标注台账单独维护，这里不再复制。",
        },
    }


def _range_of(points: list[dict[str, Any]]) -> dict[str, Any]:
    nos = [p.get("chapter_no") for p in points if p.get("chapter_no") is not None]
    return {
        "from_chapter": nos[0] if nos else None,
        "to_chapter": nos[-1] if nos else None,
        "span_count": len(points),
        "max_subplot_count": max((p.get("subplot_count") or 0) for p in points),
    }


# ── K3 作品指纹 ──────────────────────────────────────────


def _rating_mean(annotations: dict[str, dict[str, Any]], key: str) -> float | None:
    values = []
    for rec in annotations.values():
        v = (rec.get("fields") or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            values.append(float(v))
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _flat_runs(annotations: dict[str, dict[str, Any]], order: list[str], key: str) -> list[dict[str, Any]]:
    """连续 ≥FLAT_RUN_MIN 章量表值不变的区段。"""
    runs: list[dict[str, Any]] = []
    current: list[int] = []
    prev_no: int | None = None
    prev_val: Any = None
    for t in order:
        no = t.chapter_no
        rec = annotations.get(t.chapter_id) or {}
        val = (rec.get("fields") or {}).get(key) if rec else None
        if val is not None and val == prev_val and prev_no is not None and no is not None:
            if current and current[-1] == prev_no - 1:
                current.append(no)
            else:
                current = [prev_no, no]
        else:
            if len(current) >= FLAT_RUN_MIN:
                runs.append({"from": current[0], "to": current[-1], "value": prev_val})
            current = []
        prev_no, prev_val = no, val
    if len(current) >= FLAT_RUN_MIN:
        runs.append({"from": current[0], "to": current[-1], "value": prev_val})
    return runs


def build_k3(
    annotations: dict[str, dict[str, Any]],
    order: list,
) -> dict[str, Any]:
    """L3 作品指纹条目：从标注统计聚合，全部可复算、可解释。
    置信度按样本量分级：≥50 章为「高」，20-49 为「中」，<20 为「低」（样本不足）。"""
    n = len(annotations)
    if n < 5:
        return {
            "available": False,
            "note": "标注不足 5 章，指纹没有统计意义。先跑一批标注再来。",
            "items": [],
        }
    confidence = "高" if n >= 50 else ("中" if n >= 20 else "低")

    def item(metric: str, rule: str, stat: Any, note: str = "") -> dict[str, Any]:
        return {
            "metric": metric,
            "rule": rule,
            "statistic": stat,
            "sample": n,
            "confidence": confidence,
            "note": note,
        }

    items: list[dict[str, Any]] = []

    for key, label in (
        ("hook_strength", "章末钩子强度"),
        ("emotion", "情绪值"),
        ("conflict", "冲突等级"),
        ("info_release", "信息释放量"),
        ("mainline_progress", "主线推进度"),
        ("motive_strength", "主角核心动机呈现强度"),
    ):
        mean = _rating_mean(annotations, key)
        if mean is None:
            continue
        items.append(
            item(
                key,
                f"全书 {label} 均值",
                {"mean": mean},
                "均值只描述整体倾向，不替代逐章阅读。",
            )
        )

    # 钩子类型 / 视角 / 时间跨度分布
    for key, label in (("hook_type", "钩子类型"), ("perspective", "视角人称")):
        dist: dict[str, int] = {}
        for rec in annotations.values():
            v = (rec.get("fields") or {}).get(key)
            if v:
                dist[str(v)] = dist.get(str(v), 0) + 1
        if dist:
            top = sorted(dist.items(), key=lambda kv: -kv[1])[0]
            items.append(
                item(
                    key,
                    f"主要{label}",
                    {"distribution": dist, "dominant": top[0], "dominant_count": top[1]},
                    "分布来自全部已标注章节。",
                )
            )

    # 节奏走平区段
    for key, label in (("emotion", "情绪值"), ("conflict", "冲突等级")):
        runs = _flat_runs(annotations, order, key)
        if runs:
            items.append(
                item(
                    f"{key}_flat",
                    f"{label}连续{FLAT_RUN_MIN}章 + 不变的区段",
                    {"runs": runs},
                    "数字不构成结论，要指出「哪里该看」——用原文验证后才有意义。",
                )
            )

    # 钩子强度弱章（≤2）：通常意味着平淡收尾
    weak_hook = [
        t.chapter_no
        for t in order
        if int((annotations.get(t.chapter_id) or {}).get("fields", {}).get("hook_strength") or 0) <= 2
    ]
    if weak_hook:
        items.append(
            item(
                "weak_hook_chapters",
                "钩子强度 ≤ 2 的章节（平淡收尾候选）",
                {"chapters": weak_hook[:100], "count": len(weak_hook)},
                "这些章的章末可能缺乏悬念，最该先看。",
            )
        )

    return {
        "available": True,
        "note": f"作品级指纹（L3）。样本 {n} 章，置信度按样本量分级为「{confidence}」。",
        "items": items,
    }


# ── 主入口 ──────────────────────────────────────────────


def build_kb(
    *,
    work_name: str,
    ingest_dir: Path,
    annotations_dir: Path,
    entities_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """聚合全部知识库数据并落盘到 out_dir。纯脚本，不调模型。"""
    tasks = load_chapter_tasks(ingest_dir)
    if not tasks:
        raise FileNotFoundError(f"作品「{work_name}」还没有章节数据")

    entities_latest = None
    if entities_path.exists():
        try:
            entities_latest = json.loads(entities_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            entities_latest = None

    k1 = build_k1(work_name, entities_latest)

    # 读全部标注 + 构造正文语料（人物匹配用）
    annotations: dict[str, dict[str, Any]] = {}
    corpus: dict[str, str] = {}
    for t in tasks:
        record = load_annotation(annotations_dir / f"{t.chapter_id}.json")
        if record:
            annotations[t.chapter_id] = record
        try:
            raw = t.path.read_text(encoding="utf-8")
            _meta, body = strip_front_matter(raw)
            corpus[t.chapter_id] = body
        except OSError:
            corpus[t.chapter_id] = ""

    k2 = build_k2(work_name, k1, tasks, annotations, corpus)
    k3 = build_k3(annotations, tasks)

    result = KBResult(
        work=work_name,
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        k1=k1,
        k2=k2,
        k3=k3,
    ).to_dict()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "k1-entities.json").write_text(json.dumps(k1, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "k2-material.json").write_text(json.dumps(k2, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "k3-fingerprint.json").write_text(json.dumps(k3, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest.md").write_text(render_markdown(result), encoding="utf-8")
    return result


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# 知识库 · {result.get('work')}",
        "",
        f"生成于 {result.get('generated_at', '')}",
        "",
    ]
    k1 = result.get("k1") or {}
    if k1.get("available"):
        counts = k1.get("counts") or {}
        lines.append(
            f"人物 {counts.get('characters', 0)}　势力 {counts.get('factions', 0)}　"
            f"能力 {counts.get('abilities', 0)}　地点 {counts.get('locations', 0)}　"
            f"关系 {counts.get('relations', 0)}"
        )
    else:
        lines.append("> 还没有实体统计，K1 实体卡片为空。")
    lines.append("")

    k2 = result.get("k2") or {}
    person = k2.get("person") or {}
    if person.get("rows"):
        lines.append("## 人物出场")
        lines.append("| 人物 | 别名 | 定位 | 出场章数 | 首次 | 最后 |")
        lines.append("|---|---|---|---|---|---|")
        for r in person["rows"][:60]:
            also = "、".join(r.get("aliases") or []) or "—"
            lines.append(
                f"| {r['name']} | {also} | {r.get('identity') or r.get('role') or '—'} | "
                f"{r['appearance_count']} | 第{r['first_seen']}章 | 第{r['last_seen']}章 |"
            )
    else:
        lines.append("## 人物出场\n无数据。")
    lines.append("")

    k3 = result.get("k3") or {}
    if k3.get("available"):
        lines.append("## 作品指纹（L3）")
        for it in k3.get("items") or []:
            stat = it.get("statistic") or {}
            detail = str(stat.get("mean") if "mean" in stat else stat.get("dominant") or "")
            if "runs" in stat:
                detail = "、".join(f"第{a.get('from')}-{a.get('to')}章" for a in stat["runs"][:5])
            lines.append(f"- {it['rule']}：{detail}（{it['sample']} 章，置信度{it['confidence']}）")
    else:
        lines.append("## 作品指纹（L3）\n" + (k3.get("note") or "无数据。"))
    return "\n".join(lines)