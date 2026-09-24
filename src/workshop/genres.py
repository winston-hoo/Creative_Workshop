"""L2 题材规则库（M6 题材聚合器）。

设计出处：`小说创作工坊-通用设计方案.md` §3（三层规则库）/ §7（M6）。

聚合规则（v2 设计明文）：
  · 该题材下作品数 < 3   → 不产出题材规则，标记「样本不足」
  · 3 到 9 部           → 产出规则，置信度最高到「中」
  · >= 10 部            → 置信度可到「高」
  · 某条规则跨作品命中率方差过大 → 拆分为子规则，或降级回作品级（L3）

数据源：每部作品 20-kb/k3-fingerprint.json（kb.py 产出的 L3 条目）。
聚合的对象是「同一指标在多部作品里的取值」，不是重新调模型。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GENRE_SCHEMA_VERSION = "genre-rules-v1"
INDEX_NAME = "_index.yaml"

# 置信度按作品数分档（设计 §7 M6）
_LOW_SAMPLE = 3
_MID_SAMPLE = 10
_TOP_SAMPLE = 15


@dataclass
class GenreRules:
    genre: str
    generated_at: str
    works: list[str]
    source: str = "workspaces/…/20-kb/k3-fingerprint.json"
    rules: list[dict[str, Any]] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GENRE_SCHEMA_VERSION,
            "genre": self.genre,
            "generated_at": self.generated_at,
            "works": self.works,
            "source": self.source,
            "rules": self.rules,
            "baseline": self.baseline,
            "note": self.note,
        }


def index_path(genres_root: Path) -> Path:
    return Path(genres_root) / INDEX_NAME


def load_index(genres_root: Path) -> dict[str, str]:
    """作品 → 题材 映射。没有文件就空。"""
    import yaml

    path = index_path(genres_root)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def save_index(genres_root: Path, mapping: dict[str, str]) -> Path:
    import yaml

    path = index_path(genres_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(mapping, allow_unicode=True, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def set_genre(genres_root: Path, work: str, genre: str) -> dict[str, str]:
    """登记/修改一部作品的题材。"""
    genre = (genre or "").strip()
    mapping = load_index(genres_root)
    if genre:
        mapping[work] = genre
    else:
        mapping.pop(work, None)
    save_index(genres_root, mapping)
    return {"work": work, "genre": genre or None, "mapping": mapping}


def _metric_key(metric: str) -> tuple[str, str] | None:
    """把 L3 条目的 metric 转成可跨作品对比的 (类型, 指标名)。"""
    if metric in ("hook_strength", "emotion", "conflict", "info_release", "mainline_progress", "motive_strength"):
        return ("rating", metric)
    if metric in ("hook_type", "perspective"):
        return ("dominant", metric)
    return None


def aggregate_genre(
    genres_root: Path,
    workspaces_root: Path,
    genre: str,
) -> GenreRules:
    """聚合某题材下全部作品的 L3 条目为 L2 规则。纯脚本，不调模型。"""
    mapping = load_index(genres_root)
    works = sorted(w for w, g in mapping.items() if g == genre)
    fingerprints: list[tuple[str, dict[str, Any]]] = []
    for work in works:
        path = Path(workspaces_root) / work / "20-kb" / "k3-fingerprint.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue
        if data.get("available"):
            fingerprints.append((work, data))

    rules: list[dict[str, Any]] = []
    baseline: dict[str, Any] = {}
    note = ""

    n = len(fingerprints)
    if n == 0:
        return GenreRules(
            genre=genre,
            generated_at=_now(),
            works=works,
            note="题材下没有作品产出了 L3 指纹。先给作品构建知识库（K3 指纹）再聚合。",
        )

    if n < _LOW_SAMPLE:
        note = (
            f"该题材只有 {n} 部作品。方案要求 ≥{_LOW_SAMPLE} 部才产出题材规则，"
            "现在标记「样本不足」——单部作品的特征不能当题材规律。"
        )

    # 按指标聚合：同指标在多部作品里的值
    ratings_agg: dict[str, list[float]] = {}
    dominants_agg: dict[str, dict[str, int]] = {}
    for work, fp in fingerprints:
        for item in fp.get("items") or []:
            pair = _metric_key(str(item.get("metric") or ""))
            if not pair:
                continue
            kind, name = pair
            if kind == "rating":
                mean = (item.get("statistic") or {}).get("mean")
                if isinstance(mean, (int, float)):
                    ratings_agg.setdefault(name, []).append(float(mean))
            else:
                dom = (item.get("statistic") or {}).get("dominant")
                if dom:
                    bucket = dominants_agg.setdefault(name, {})
                    bucket[str(dom)] = bucket.get(str(dom), 0) + 1

    def _rating_rule(name: str, title: str, values: list[float]) -> dict[str, Any] | None:
        values = sorted(values)
        mean = sum(values) / len(values)
        # 命中：作品值与均值差不超过 0.8（1-5 量表有一格以上差距才算偏离）
        hits = sum(1 for v in values if abs(v - mean) <= 0.8)
        hit_rate = round(hits / len(values), 2)
        spread = round(max(values) - min(values), 2)
        return {
            "id": f"L2-{genre}-{name}",
            "layer": "题材",
            "genre": genre,
            "rule": f"该题材作品 {title} 均值集中在 {mean:.1f}",
            "statistic": {"mean": round(mean, 2), "spread": spread, "per_work": values},
            "sample_works": len(values),
            "hit_rate": hit_rate,
            "confidence": _confidence_for(n, hit_rate, spread),
        }

    def _dominant_rule(name: str, title: str, counts: dict[str, int]) -> dict[str, Any] | None:
        total = sum(counts.values())
        dom, dom_count = max(counts.items(), key=lambda kv: kv[1])
        hit = round(dom_count / total, 2)
        return {
            "id": f"L2-{genre}-{name}",
            "layer": "题材",
            "genre": genre,
            "rule": f"该题材作品以「{dom}」为主",
            "statistic": {"dominant": dom, "dom_count": dom_count, "distribution": counts},
            "sample_works": total,
            "hit_rate": hit,
            "confidence": _confidence_for(n, hit, 0),
        }

    titles = {
        "hook_strength": "章末钩子强度", "emotion": "情绪值", "conflict": "冲突等级",
        "info_release": "信息释放量", "mainline_progress": "主线推进度", "motive_strength": "动机呈现强度",
        "hook_type": "钩子类型", "perspective": "视角人称",
    }

    if n >= _LOW_SAMPLE:
        for name, values in ratings_agg.items():
            rule = _rating_rule(name, titles.get(name, name), values)
            if rule:
                rules.append(rule)
        for name, counts in dominants_agg.items():
            rule = _dominant_rule(name, titles.get(name, name), counts)
            if rule:
                rules.append(rule)

    # 基线：跨该题材所有作品求均值（供 M8 偏离度用）
    if ratings_agg:
        baseline = {
            "means": {k: round(sum(v) / len(v), 2) for k, v in ratings_agg.items()},
            "n_works": n,
        }

    rules.sort(key=lambda r: (-r.get("hit_rate", 0), r.get("id", "")))
    return GenreRules(
        genre=genre,
        generated_at=_now(),
        works=works,
        rules=rules,
        baseline=baseline,
        note=note or "聚合完成。规则命中率与置信度按设计口径计算：命中 = 作品值与题材均值差 ≤0.8。",
    )


def _confidence_for(n_works: int, hit_rate: float, spread: float) -> str:
    """置信度：作品数 <3 不给；3-9 最高「中」；>=10 且命中率高可到「高」。"""
    if n_works < _LOW_SAMPLE:
        return "低（样本不足）"
    base = "中"
    if n_works >= _MID_SAMPLE and hit_rate >= 0.8 and spread <= 1.5:
        base = "高"
    if n_works >= _TOP_SAMPLE and hit_rate >= 0.9:
        base = "高"
    return base


def save_genre_rules(genres_root: Path, result: GenreRules) -> Path:
    out = Path(genres_root) / result.genre
    out.mkdir(parents=True, exist_ok=True)
    path = out / "rules.json"
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_genres(genres_root: Path) -> list[dict[str, Any]]:
    mapping = load_index(genres_root)
    counts: dict[str, int] = {}
    for g in mapping.values():
        counts[g] = counts.get(g, 0) + 1
    items = []
    root = Path(genres_root)
    for genre in sorted(counts):
        rules_path = root / genre / "rules.json"
        rules_ready = rules_path.exists()
        items.append(
            {
                "genre": genre,
                "work_count": counts[genre],
                "rules_ready": rules_ready,
                # 设计 §7 M6：<3 部标记样本不足
                "sample_ok": counts[genre] >= _LOW_SAMPLE,
            }
        )
    return items


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")