"""探测报告的落盘、索引、对比与告警。

报告放全局 config/probe-reports/，因为服务商配置是全局的，不随作品走。
保留历史版本不覆盖——没有历史就没有退化监测。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .probe import format_report_text
from .secrets import redact

logger = logging.getLogger(__name__)

INDEX_NAME = "index.json"


# ── 落盘 ─────────────────────────────────────────────────────


def save_report(
    report: dict[str, Any],
    reports_dir: Path | str,
    *,
    secrets: list[str] | None = None,
    write_markdown: bool = True,
) -> dict[str, Path]:
    """保存报告。返回 {'yaml': ..., 'markdown': ...} 路径。

    落盘前对**序列化后的文本**统一做一次脱敏。这是纵深防御：
    上游错误体里可能夹带密钥，而错误体是要写进报告的。
    只在写入端做一次过滤，比在每个赋值点都记得过滤可靠得多。
    """
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = str(report.get("report_id") or f"probe-{datetime.now():%Y%m%dT%H%M%S}")
    yaml_path = out_dir / f"{stem}.yaml"
    yaml_text = yaml.safe_dump(report, allow_unicode=True, sort_keys=False)
    yaml_path.write_text(redact(yaml_text, secrets), encoding="utf-8")

    paths = {"yaml": yaml_path}

    if write_markdown:
        md_path = out_dir / f"{stem}.md"
        md_text = format_report_text(report) + "\n"
        md_path.write_text(redact(md_text, secrets), encoding="utf-8")
        paths["markdown"] = md_path

    update_index(out_dir, report, yaml_path)
    return paths


def update_index(reports_dir: Path | str, report: dict[str, Any], yaml_path: Path) -> None:
    """维护索引。索引只存摘要，完整数据在各自的报告文件里。"""
    out_dir = Path(reports_dir)
    index_path = out_dir / INDEX_NAME
    entries = load_index(out_dir)

    entry = {
        "report_id": report.get("report_id"),
        "file": yaml_path.name,
        "provider": report.get("provider"),
        "model": report.get("model"),
        "checked_at": report.get("checked_at"),
        "time_bucket": report.get("time_bucket"),
        "prompt_hash": (report.get("config_snapshot") or {}).get("prompt_hash"),
        "reachable": (report.get("results") or {}).get("reachable"),
        "structured_output": (report.get("results") or {}).get("structured_output"),
        "usage_in_response": (report.get("results") or {}).get("usage_in_response"),
        "tokens_per_cjk_char": (report.get("results") or {}).get("tokens_per_cjk_char"),
        "ttft_ms_median": ((report.get("benchmark") or {}).get("ttft_ms") or {}).get("median"),
        "total_latency_ms_median": (
            (report.get("benchmark") or {}).get("total_latency_ms") or {}
        ).get("median"),
        "suggested_timeout_sec": (report.get("derived") or {}).get("suggested_timeout_sec"),
        "enabled_ok": (report.get("verdict") or {}).get("enabled_ok"),
    }

    entries = [e for e in entries if e.get("report_id") != entry["report_id"]]
    entries.append(entry)
    entries.sort(key=lambda e: str(e.get("checked_at") or ""))

    index_path.write_text(
        json.dumps({"reports": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_index(reports_dir: Path | str) -> list[dict[str, Any]]:
    index_path = Path(reports_dir) / INDEX_NAME
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("索引文件损坏，按空处理：%s", index_path)
        return []
    reports = data.get("reports") if isinstance(data, dict) else None
    return list(reports) if isinstance(reports, list) else []


def load_report(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


# ── 对比与告警 ───────────────────────────────────────────────


def find_previous(
    reports_dir: Path | str,
    provider: str,
    model: str,
    before_report_id: str | None = None,
) -> dict[str, Any] | None:
    """找同服务商同模型的上一次报告。不限制 prompt_hash，由调用方判定可比性。"""
    out_dir = Path(reports_dir)
    candidates = [
        e
        for e in load_index(out_dir)
        if e.get("provider") == provider and e.get("model") == model
    ]
    candidates.sort(key=lambda e: str(e.get("checked_at") or ""))
    if before_report_id:
        candidates = [e for e in candidates if e.get("report_id") != before_report_id]
    if not candidates:
        return None
    return load_report(out_dir / str(candidates[-1]["file"]))


# 参与对比的基准指标：(显示名, 取值路径, 单位, 最小绝对变化)
#
# 为什么要「最小绝对变化」：只设百分比阈值会让极小绝对值产生噪音告警。
# 实测中本机环回的端点往返从 0.91ms 涨到 2.47ms 就触发了 +171% 的告警，
# 而 1.5ms 的差异毫无意义。所以同时要求绝对变化达到有意义的量级。
_COMPARABLE_METRICS: list[tuple[str, tuple[str, ...], str, float]] = [
    ("首字延迟", ("benchmark", "ttft_ms", "median"), "ms", 50.0),
    ("总延迟", ("benchmark", "total_latency_ms", "median"), "ms", 50.0),
    ("输出速度", ("benchmark", "output_tokens_per_sec", "median"), " tokens/s", 10.0),
    ("端点往返", ("benchmark", "rtt_ms", "median"), "ms", 50.0),
]


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


# 会影响性能口径的配置项：(快照键, 显示名)
#
# 这些项一变，两次探测的性能差异就主要来自配置而不是服务本身。
# 不识别它们，就会把「关掉思考模式带来的 7 倍提速」误报成「服务商负载变化」。
_CONFIG_KEYS: list[tuple[str, str]] = [
    ("thinking", "思考模式"),
    ("reasoning_effort", "思考强度"),
    ("streaming", "流式"),
    ("max_tokens", "最大输出"),
    ("text_chars", "测试文本长度"),
]


def _config_diffs(previous: dict, current: dict) -> list[str]:
    prev_snap = previous.get("config_snapshot") or {}
    curr_snap = current.get("config_snapshot") or {}
    diffs: list[str] = []
    for key, label in _CONFIG_KEYS:
        old, new = prev_snap.get(key), curr_snap.get(key)
        if old != new:
            diffs.append(f"{label} {old} → {new}")
    return diffs


def compare_reports(
    previous: dict[str, Any],
    current: dict[str, Any],
    threshold_pct: float = 50.0,
) -> list[dict[str, Any]]:
    """对比两份报告，返回变化说明列表。

    可比性校验优先：测试配方 hash 不同则拒绝直接对比。
    其次识别配置差异：配置变过时，性能差异应归因于配置而不是服务。
    """
    prev_hash = (previous.get("config_snapshot") or {}).get("prompt_hash")
    curr_hash = (current.get("config_snapshot") or {}).get("prompt_hash")
    if prev_hash != curr_hash:
        return [
            {
                "level": "incomparable",
                "title": "测试配方已变更，不可直接对比",
                "detail": (
                    f"上次 hash {prev_hash}，本次 hash {curr_hash}。"
                    "测试句或采样参数改动过，历史数据与本次不同口径，强行对比会得出错误结论。"
                ),
            }
        ]

    notes: list[dict[str, Any]] = []
    config_diffs = _config_diffs(previous, current)

    if config_diffs:
        notes.append(
            {
                "level": "info",
                "title": "两次探测的配置不同，性能差异应先归因于配置变更",
                "detail": (
                    "差异项：" + "、".join(config_diffs) + "。"
                    "配置变过时，延迟与速度的变化是预期结果，不应解读为服务退化；"
                    "要判断服务本身是否变化，请用相同配置再测一次。"
                ),
            }
        )

    prev_bucket = previous.get("time_bucket")
    curr_bucket = current.get("time_bucket")
    if prev_bucket != curr_bucket and prev_bucket and curr_bucket:
        label = {"peak": "高峰", "offpeak": "空闲"}
        notes.append(
            {
                "level": "info",
                "title": "两次探测的计费时段不同",
                "detail": (
                    f"上次为{label.get(prev_bucket, prev_bucket)}时段，"
                    f"本次为{label.get(curr_bucket, curr_bucket)}时段。"
                    "时段不同时延时有差异属正常，对比结论需保留这一前提。"
                ),
            }
        )

    for label, path, unit, min_delta in _COMPARABLE_METRICS:
        old = _dig(previous, path)
        new = _dig(current, path)
        if old in (None, 0) or new is None:
            continue
        delta = float(new) - float(old)
        if abs(delta) < min_delta:
            continue
        change_pct = delta / float(old) * 100.0
        if abs(change_pct) < threshold_pct:
            continue
        direction = "升高" if change_pct > 0 else "降低"
        notes.append(
            {
                "level": "alert" if change_pct > 0 else "good",
                "metric": label,
                "previous": old,
                "current": new,
                "change_pct": round(change_pct, 1),
                "title": f"{label}从 {old}{unit} {direction}到 {new}{unit}（{change_pct:+.0f}%）",
                "detail": (
                    "本次配置有变更，该差异大概率由配置导致（见上方说明）。"
                    if config_diffs
                    else _suggest(change_pct)
                ),
            }
        )

    return notes


def _suggest(change_pct: float) -> str:
    if change_pct > 0:
        return (
            "可能原因：服务商负载变化、网络波动、或该模型规格调整。"
            "建议重测一次确认；若持续偏高，考虑调整 timeout_sec 或换模型。"
        )
    return "延迟明显改善。若已稳定，可考虑适当提高并发数。"


def format_comparison(notes: list[dict[str, Any]]) -> str:
    if not notes:
        return "与上一次相比无明显变化（未超过告警阈值）。"
    labels = {
        "alert": "注意",
        "good": "改善",
        "info": "说明",
        "incomparable": "不可比",
    }
    lines: list[str] = []
    for note in notes:
        prefix = labels.get(str(note.get("level")), "信息")
        lines.append(f"[{prefix}] {note.get('title')}")
        if note.get("detail"):
            lines.append(f"        {note['detail']}")
    return "\n".join(lines)
