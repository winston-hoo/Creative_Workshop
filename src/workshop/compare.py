"""M8 对比分析器：同题材对比 / 跨题材差异 / 作品 vs 基线偏离度。

设计出处：`小说创作工坊-通用设计方案.md` §7（M8）与目录结构 compare/。

三份产出（各自独立、都可解释）：
  · same-genre   —— 同一题材下每部作品与题材基线的对照表
  · cross-genre  —— 同一指标在不同题材间的分布对比，找出差异最大的指标
  · deviation    —— 单部作品偏离题材基线的指标列表（哪里「不像这个题材」）

全部纯脚本，不调模型：原料是每部作品的 20-kb/k3-fingerprint.json
与题材库 genres/{genre}/rules.json 里的 baseline。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .genres import load_index

COMPARE_SCHEMA_VERSION = "compare-v1"

_RATING_METRICS = ("hook_strength", "emotion", "conflict", "info_release", "mainline_progress", "motive_strength")
_TITLES = {
    "hook_strength": "章末钩子强度", "emotion": "情绪值", "conflict": "冲突等级",
    "info_release": "信息释放量", "mainline_progress": "主线推进度", "motive_strength": "动机呈现强度",
    "hook_type": "钩子类型", "perspective": "视角人称",
}


@dataclass
class CompareResult:
    generated_at: str
    same_genre: dict[str, Any]
    cross_genre: dict[str, Any]
    deviation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPARE_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "same_genre": self.same_genre,
            "cross_genre": self.cross_genre,
            "deviation": self.deviation,
        }


def _work_fingerprint(workspaces_root: Path, work: str) -> dict[str, Any] | None:
    path = Path(workspaces_root) / work / "20-kb" / "k3-fingerprint.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("available"):
            return data
    except (ValueError, OSError):
        pass
    return None


def _per_work_means(fp: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in fp.get("items") or []:
        metric = str(item.get("metric") or "")
        stat = item.get("statistic") or {}
        if metric in _RATING_METRICS and isinstance(stat.get("mean"), (int, float)):
            out[metric] = float(stat["mean"])
    return out


def same_genre_report(
    workspaces_root: Path, genres_root: Path, genre: str,
) -> dict[str, Any]:
    """同题材每部作品 vs 题材基线。"""
    mapping = load_index(genres_root)
    works = sorted(w for w, g in mapping.items() if g == genre)
    rows = []
    for work in works:
        fp = _work_fingerprint(workspaces_root, work)
        if not fp:
            continue
        means = _per_work_means(fp)
        rows.append({"work": work, "means": means, "n_chapters": fp.get("items") and len(fp.get("items") or [])})

    baseline: dict[str, Any] = {}
    rules_path = Path(genres_root) / genre / "rules.json"
    if rules_path.exists():
        try:
            baseline = (json.loads(rules_path.read_text(encoding="utf-8-sig")) or {}).get("baseline", {})
        except (ValueError, OSError):
            baseline = {}

    return {
        "genre": genre,
        "works": [r["work"] for r in rows],
        "rows": rows,
        "baseline": baseline,
        "metrics": {m: _TITLES.get(m, m) for m in _RATING_METRICS if any(m in r["means"] for r in rows)},
    }


def cross_genre_report(workspaces_root: Path, genres_root: Path) -> dict[str, Any]:
    """跨题材：同一指标在不同题材间的分布与差异最大的指标。"""
    mapping = load_index(genres_root)
    genres = sorted(set(mapping.values()))
    # 每个题材聚合一部「代表作品」的均值（题材均值取自题材库 baseline，更稳）
    per_genre_means: dict[str, dict[str, float]] = {}
    for genre in genres:
        rules_path = Path(genres_root) / genre / "rules.json"
        if not rules_path.exists():
            continue
        try:
            data = json.loads(rules_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue
        base = data.get("baseline") or {}
        means = base.get("means") or {}
        if means:
            per_genre_means[genre] = {k: float(v) for k, v in means.items()}

    comparisons: list[dict[str, Any]] = []
    all_metrics = sorted({m for g in per_genre_means.values() for m in g})
    for metric in all_metrics:
        values = {g: per_genre_means[g].get(metric) for g in per_genre_means if metric in per_genre_means[g]}
        values = {g: v for g, v in values.items() if v is not None}
        if len(values) >= 2:
            spread = max(values.values()) - min(values.values())
            comparisons.append(
                {
                    "metric": metric,
                    "title": _TITLES.get(metric, metric),
                    "spread": round(spread, 2),
                    "per_genre": values,
                }
            )
    comparisons.sort(key=lambda c: -c["spread"])

    return {
        "genres": genres,
        "comparisons": comparisons,
        "note": "跨题材差异以题材库 baseline 为准（聚合后的题材均值，而不是某部作品）。",
    }


def deviation_report(
    workspaces_root: Path, genres_root: Path, work: str, genre: str,
) -> dict[str, Any]:
    """单部作品偏离题材基线的指标。偏离 > 0.8（一格量表）才列出来。"""
    fp = _work_fingerprint(workspaces_root, work)
    if not fp:
        return {"work": work, "genre": genre, "available": False, "note": "作品还没有 L3 指纹，先构建知识库。"}

    rules_path = Path(genres_root) / genre / "rules.json"
    if not rules_path.exists():
        return {"work": work, "genre": genre, "available": False,
                "note": f"题材「{genre}」还没有聚合规则，先聚合题材库。"}

    try:
        base = (json.loads(rules_path.read_text(encoding="utf-8-sig")) or {}).get("baseline", {})
    except (ValueError, OSError):
        base = {}
    baseline_means = base.get("means") or {}
    if not baseline_means:
        return {"work": work, "genre": genre, "available": False, "note": "题材基线为空。"}

    means = _per_work_means(fp)
    deviations = []
    for metric, base_val in baseline_means.items():
        if metric not in means:
            continue
        delta = means[metric] - float(base_val)
        if abs(delta) > 0.8:
            deviations.append(
                {
                    "metric": metric,
                    "title": _TITLES.get(metric, metric),
                    "work_mean": means[metric],
                    "genre_mean": round(float(base_val), 2),
                    "delta": round(delta, 2),
                    "direction": "高于题材均值" if delta > 0 else "低于题材均值",
                }
            )
    deviations.sort(key=lambda d: -abs(d["delta"]))

    return {
        "work": work,
        "genre": genre,
        "available": True,
        "deviations": deviations,
        "note": "偏离阈值 ±0.8（1-5 量表大约一格）。列出的是「哪里不像这个题材」，供你判断是风格特色还是问题。",
    }


def build_compare(workspaces_root: Path, genres_root: Path, work: str, genre: str) -> CompareResult:
    return CompareResult(
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        same_genre=same_genre_report(workspaces_root, genres_root, genre),
        cross_genre=cross_genre_report(workspaces_root, genres_root),
        deviation=deviation_report(workspaces_root, genres_root, work, genre),
    )


def save_compare(compare_root: Path, result: CompareResult) -> Path:
    out = Path(compare_root)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "latest.json"
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path