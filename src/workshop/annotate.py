"""逐章标注：单章往返链路。

这一版只做**单章**，不做批量调度。理由：链路先跑通，批量只是加并发。
链路里包含：脚本轨字段 → 固定前缀 + 变量尾巴 → 模型调用 → JSON 校验 →
重试 → 落盘 → 幂等。这七步任何一步不对，批量跑起来只会放大错误。

三条纪律：
  1. 校验失败绝不静默填默认值，缺失就是缺失
  2. 每章一个独立文件，幂等键是原文内容哈希
  3. 落盘前统一脱敏（上游错误体可能夹带密钥）
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ErrorKind, hint_for
from .llm import (
    ApiError,
    ChatResult,
    OpenAICompatProvider,
    build_thinking_extra,
    extract_json_object,
)
from .primitives import Primitives, WorkConfig, normalize_text
from .ledger import Ledger
from .prompts import (
    PROMPT_VERSION,
    build_messages,
    build_stable_prefix,
    build_variable_tail,
    prefix_hash,
)
from .script_fields import compute_script_fields
from .secrets import redact

logger = logging.getLogger(__name__)

ANNOTATION_SCHEMA_VERSION = "annotation-v1"

_CONFIDENCE_ACTION = {"高": "auto_accept", "中": "spot_check", "低": "must_review"}


@dataclass
class AnnotateOptions:
    temperature: float = 0.3
    thinking: str = "disabled"
    reasoning_effort: str | None = None
    max_tokens: int = 2000
    max_attempts: int = 3
    timeout_sec: float = 30.0


@dataclass
class AnnotateResult:
    record: dict[str, Any]
    attempts: int = 0
    skipped: bool = False
    saved_path: Path | None = None

    @property
    def status(self) -> str:
        return str(self.record.get("status") or "")


# ── 工具 ──────────────────────────────────────────────────


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def strip_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """剥掉 YAML front-matter，返回 (元数据, 正文)。

    录入环节产出的章节文件带 front-matter，标注只关心正文与其中的章序信息。
    """
    normalized = normalize_text(text)
    if not normalized.startswith("---"):
        return {}, normalized
    parts = normalized.split("---", 2)
    if len(parts) < 3:
        return {}, normalized
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, normalized
    return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")


# ── 主流程 ────────────────────────────────────────────────


def annotate_chapter(
    *,
    client: OpenAICompatProvider,
    primitives: Primitives,
    work: WorkConfig,
    chapter_id: str,
    chapter_no: int,
    vol_no: int,
    title: str,
    text: str,
    provider_id: str,
    model_id: str,
    opts: AnnotateOptions,
    source_file: str = "",
    prev_summary: dict[str, Any] | None = None,
    ledger: Ledger | None = None,
    open_foreshadows: list[dict[str, Any]] | None = None,
    apply_ledger: bool = True,
) -> AnnotateResult:
    """标注单章，返回记录（不落盘，落盘由调用方决定）。

    传入 ledger 时会启用伏笔台账：未回收清单进变量尾巴，模型只判断动作与描述，
    编号由台账分配并写回标注结果。

    批量调度下新增两个参数，是为了把「读台账」和「改台账」拆开：

      · `open_foreshadows`  —— 由调用方在**提交时刻**拍下的快照。
        并发时若让工作线程自己去读台账，读到的是正在被改写的状态。
      · `apply_ledger=False` —— 台账由调用方**按章序**统一并入
        （见 `apply_ledger_to_record`）。编号分配顺序必须是章序，否则结果不可复现。
    """
    body = normalize_text(text)
    source_digest = text_sha256(body)

    script_fields, script_issues = compute_script_fields(
        body, vol_no=vol_no, chapter_no=chapter_no, work=work, primitives=primitives
    )

    if open_foreshadows is None and ledger is not None:
        open_foreshadows = ledger.context_for_prompt(chapter_no)
    stable_prefix = build_stable_prefix(primitives, work)
    variable_tail = build_variable_tail(
        chapter_label=f"第{chapter_no}章 {title}".strip(),
        chapter_text=body,
        prev_summary=prev_summary,
        open_foreshadows=open_foreshadows,
    )
    messages = build_messages(stable_prefix=stable_prefix, variable_tail=variable_tail)

    extra_body = build_thinking_extra(opts.thinking, opts.reasoning_effort)

    model_fields: dict[str, Any] = {}
    validation_issues: list[str] = []
    attempts = 0
    usage_total: dict[str, Any] = {}
    latency_ms = 0.0
    raw_response = ""
    last_error: dict[str, Any] | None = None

    for attempt in range(1, max(1, opts.max_attempts) + 1):
        attempts = attempt
        try:
            result: ChatResult = client.chat(
                model_id,
                messages,
                max_tokens=opts.max_tokens,
                temperature=opts.temperature,
                response_format={"type": "json_object"},
                extra=extra_body,
            )
        except ApiError as exc:
            last_error = {
                "attempt": attempt,
                "kind": exc.kind.value,
                "http_status": exc.status,
                # 带上下一步该怎么办。只给一句 401 的原文，用户只能猜。
                "hint": hint_for(exc.kind),
                "detail": exc.safe_body(client.secrets),
            }
            continue

        latency_ms = result.total_ms
        raw_response = result.text
        if result.usage:
            usage_total = result.usage.to_dict()

        payload = extract_json_object(result.text)
        if payload is None:
            last_error = {
                "attempt": attempt,
                "kind": ErrorKind.RESPONSE_UNPARSABLE.value,
                "detail": f"无法从返回内容中解析出 JSON：{result.text[:160]}",
            }
            continue

        cleaned, issues = primitives.validate(payload)
        if issues:
            validation_issues = issues
            last_error = {
                "attempt": attempt,
                "kind": "schema_violation",
                "detail": "；".join(issues[:6]),
            }
            continue

        model_fields = cleaned
        validation_issues = []
        last_error = None
        break

    filled = sum(
        1
        for f in primitives.model_fields
        if model_fields.get(f.key) is not None
    )
    if not model_fields:
        # 三次都没成功：不填默认值，模型轨字段保持全 None
        model_fields = {f.key: None for f in primitives.model_fields}
        for key in (m.get("key") for m in primitives.meta_fields):
            if key:
                model_fields[str(key)] = None

    # 语义字段与自评字段分开存：
    #   fields       只放结构原语（脚本轨 + 模型轨）
    #   self_report  放模型的自评（置信度、存疑字段），用于分级复核
    semantic_fields = {f.key: model_fields.get(f.key) for f in primitives.model_fields}
    self_report = {
        str(m.get("key")): model_fields.get(str(m.get("key")))
        for m in primitives.meta_fields
        if m.get("key")
    }

    confidence = self_report.get("confidence")
    uncertain = self_report.get("uncertain_fields") or []
    issues: list[str] = list(script_issues)

    # 伏笔并入台账。**编号在这里被真正分配并写回 semantic_fields**，
    # 所以落盘的标注文件里带的是台账的规范编号，而不是模型随口编的字符串。
    ledger_report: dict[str, Any] | None = None
    if ledger is not None and apply_ledger:
        report = ledger.apply(
            chapter_id=chapter_id,
            chapter_no=chapter_no,
            foreshadows=semantic_fields.get("foreshadows") or [],
        )
        ledger_report = report.to_dict()
        issues.extend(report.anomalies)

    needs_review = (
        bool(validation_issues)
        or bool(last_error)
        or confidence == "低"
        or bool(uncertain)
        or bool(ledger_report and ledger_report.get("anomalies"))
    )

    if not confidence and not validation_issues:
        issues.append("模型未返回自评置信度")

    record: dict[str, Any] = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "vol_no": vol_no,
        "chapter_no": chapter_no,
        "title": title,
        "status": "needs_review" if needs_review else "ok",
        "review_action": _CONFIDENCE_ACTION.get(str(confidence), "spot_check"),
        "source": {
            "file": source_file,
            "sha256": source_digest,
            "char_count": script_fields.get("char_count"),
        },
        "fields": {**script_fields, **semantic_fields},
        "self_report": self_report,
        "foreshadow_ledger": ledger_report,
        "issues": issues,
        "provenance": {
            "annotated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "provider": provider_id,
            "model": model_id,
            "temperature": opts.temperature,
            "thinking": opts.thinking,
            "prompt_version": PROMPT_VERSION,
            "primitives_version": primitives.version,
            "primitives_hash": primitives.hash(),
            "work_hash": work.hash(),
            "prefix_hash": prefix_hash(stable_prefix),
            "attempts": attempts,
            "model_fields_filled": filled,
            "model_fields_total": len(primitives.model_fields),
            "latency_ms": round(latency_ms, 1),
            "usage": usage_total,
            "ledger_enabled": ledger is not None,
            # 台账由调用方按序并入时，这里为 False，提交后会被改回 True。
            "ledger_applied": bool(ledger is not None and apply_ledger),
            "open_foreshadows_before": len(open_foreshadows) if open_foreshadows is not None else None,
        },
    }

    if last_error:
        record["errors"] = [last_error]
    if validation_issues:
        record["validation_issues"] = validation_issues
    if raw_response and (needs_review or validation_issues):
        # 只在结果可疑时保留原始返回，用于排查提示词问题；正常结果不留，避免文件膨胀
        record["debug"] = {"raw_response": raw_response[:2000]}

    return AnnotateResult(record=record, attempts=attempts)


def apply_ledger_to_record(
    record: dict[str, Any],
    ledger: Ledger,
    *,
    chapter_id: str,
    chapter_no: int | None,
) -> dict[str, Any]:
    """把某一章的伏笔动作并入台账，并写回记录。

    批量调度专用：调用发生在**按章序提交**的时刻，而不是模型返回的时刻。
    编号分配顺序因此始终等于章序，重跑能得出同一套编号。
    """
    fields = record.get("fields") or {}
    report = ledger.apply(
        chapter_id=chapter_id,
        chapter_no=chapter_no,
        foreshadows=fields.get("foreshadows") or [],
    )
    record["foreshadow_ledger"] = report.to_dict()
    if report.anomalies:
        issues = record.get("issues")
        if not isinstance(issues, list):
            issues = []
        issues.extend(report.anomalies)
        record["issues"] = issues
        record["status"] = "needs_review"
    prov = record.get("provenance")
    if isinstance(prov, dict):
        prov["ledger_applied"] = True
    return report.to_dict()


# ── 落盘与幂等 ────────────────────────────────────────────


def annotation_path(annotations_dir: Path | str, chapter_id: str) -> Path:
    return Path(annotations_dir) / f"{chapter_id}.json"


def load_annotation(path: Path | str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def is_up_to_date(path: Path | str, source_sha256: str) -> bool:
    """幂等判定：已有标注且原文哈希一致就跳过。

    改一个字才重跑一章，而不是重跑整本。
    """
    existing = load_annotation(path)
    if not existing:
        return False
    return (existing.get("source") or {}).get("sha256") == source_sha256


def archive_existing(path: Path | str, archive_dir: Path | str) -> Path | None:
    p = Path(path)
    if not p.exists():
        return None
    archive = Path(archive_dir)
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    target = archive / f"{p.stem}.{stamp}{p.suffix}"
    p.replace(target)
    return target


def save_annotation(
    record: dict[str, Any],
    annotations_dir: Path | str,
    *,
    secrets: list[str] | None = None,
    archive_dir: Path | str | None = None,
) -> Path:
    """落盘。序列化后统一脱敏——上游错误体可能夹带密钥，而错误体是要写进记录的。"""
    chapter_id = str(record.get("chapter_id") or "unknown")
    target = annotation_path(annotations_dir, chapter_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and archive_dir:
        archive_existing(target, archive_dir)

    text = json.dumps(record, ensure_ascii=False, indent=2)
    target.write_text(redact(text, secrets), encoding="utf-8")
    return target
