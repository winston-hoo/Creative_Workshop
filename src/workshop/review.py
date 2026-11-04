"""正文审稿：结构化 finding + 精确定位 + 逐条处置（P0 批次 3：D1 / D2 / A3）。

## 为什么不能只给一段散文点评

「这段感觉不对」作者没法执行：不知道指哪一句、改了算不算好、改完会不会动到别处。
所以这里把意见做成**合同**：每条必须有

  · `evidence`：正文里**逐字**出现过的一个片段（不是概括，不是转述）；
  · `start / end`：它在正文里的精确偏移，我们自己算，不信模型给的；
  · `problem`：问题是什么；`suggestion`：直接替换 `evidence` 的改写文本；
  · `severity / type`：严重度与维度，用来排序和筛。

定位不到的条目不进「可采纳」区——它只会是噪音，还会把真问题淹掉。

## 落盘与版本

`90-draft/<章节>.review.json` 保存**历次审稿**（新的追加，不覆盖旧的），
每条 finding 自带 `status`（open/adopted/rejected/stale/duplicate）。
这样关掉页面再回来，意见还在；上一版采纳了什么、拒绝了什么，也都查得到。
正文一旦改过，旧审稿里还挂着的条目按 `stale` 处理——**不复用旧偏移**，
因为那已经是另一份正文了。

## 与「审指令」的区别

`assist task=review` 那套产出的是**对本章指令的修正**（proposal 表格，改了要重写整章）。
这里的产出是**对正文的局部改写**（逐条替换，未选中的字节一个都不动）。
两者共用技法库，但提示词与产物完全不同，所以分开实现。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REVIEW_SCHEMA = "draft-review-v1"
REVIEWS_KEEP = 20
DEFAULT_REVIEW_MAX_TOKENS = 4000
MAX_ITEMS = 12

SEVERITIES = ("blocker", "major", "minor")
TYPE_LABELS = {
    "opening": "开头抓力",
    "function": "本章功能",
    "hook": "章末钩子",
    "character": "人物",
    "dialogue": "对话",
    "continuity": "连贯穿帮",
    "pacing": "节奏与信息",
    "language": "语言与 AI 痕迹",
    "logic": "逻辑硬伤",
    "other": "其他",
}

# 短于这个长度的引文不给定位：它必然在正文里出现很多次，
# 就算第一次碰巧对上了，改的地方也多半不是作者以为的那句。
MIN_EVIDENCE_CHARS = 6


class ReviewPayloadError(ValueError):
    """审稿输出不可用（不是 JSON、或连续两轮没有一条能定位）。

    `code` 给接口层与界面用；`detail` 是给人看的原因。
    """

    def __init__(self, message: str, *, code: str = "invalid_review", detail: str = "") -> None:
        self.code = code
        self.detail = detail or message
        super().__init__(message)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _now_iso() -> str:  # 兼容模块内旧名
    return now_iso()


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def review_path(work_dir: str | Path, chapter_id: str) -> Path:
    return Path(work_dir) / "90-draft" / f"{chapter_id}.review.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_reviews(work_dir: str | Path, chapter_id: str) -> dict[str, Any]:
    path = review_path(work_dir, chapter_id)
    empty = {"schema_version": REVIEW_SCHEMA, "chapter_id": chapter_id, "reviews": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("reviews", [])
    return data


def save_reviews(work_dir: str | Path, chapter_id: str, payload: dict[str, Any]) -> Path:
    path = review_path(work_dir, chapter_id)
    payload = {**payload, "schema_version": REVIEW_SCHEMA, "chapter_id": chapter_id,
               "updated_at": _now_iso()}
    reviews = payload.get("reviews") or []
    if len(reviews) > REVIEWS_KEEP:
        payload["reviews"] = reviews[-REVIEWS_KEEP:]
    _write_json(path, payload)
    return path


# ── 定位 ────────────────────────────────────────────────────


def paragraphs(prose: str) -> list[dict[str, Any]]:
    """按空行切段，同时保留每段在全文里的偏移。

    真实正文就是空行分段（`90-draft/*.md` 是作者能直接打开改的纯文本）。
    这里不重排、不规范化，偏移必须和磁盘上的字节一一对应。
    """
    out: list[dict[str, Any]] = []
    cursor = 0
    for index, match in enumerate(re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", prose or ""), 1):
        block = match.group(0)
        out.append({"index": index, "start": match.start(), "end": match.end(),
                    "chars": len(block), "text": block})
        cursor = match.end()
    _ = cursor
    return out


def locate_evidence(prose: str, quote: str, *, paragraph_hint: int = 0) -> dict[str, Any] | None:
    """把引文钉到正文里的唯一位置。找不到 / 不唯一都返回 None。

    模型偶尔会给一个「正文里出现两次」的短句。这种条目**宁可不收**：
    选错一次，作者改的就是另一处，而界面上看起来一切正常。
    唯一例外是它同时给了段号，且那一段里只出现一次——那就能钉死。
    """
    text = prose or ""
    quote = (quote or "").strip()
    if not quote or len(quote) < MIN_EVIDENCE_CHARS:
        return None
    hits = [m.start() for m in re.finditer(re.escape(quote), text)]
    if not hits:
        return None
    if len(hits) == 1:
        start = hits[0]
    else:
        if not paragraph_hint:
            return None
        target = next((p for p in paragraphs(text) if p["index"] == int(paragraph_hint)), None)
        if not target:
            return None
        local = [m.start() for m in re.finditer(re.escape(quote), target["text"])]
        if len(local) != 1:
            return None
        start = target["start"] + local[0]
    end = start + len(quote)
    if text[start:end] != quote:
        return None
    para = next((p["index"] for p in paragraphs(text)
                 if p["start"] <= start < p["end"]), 0)
    return {"start": start, "end": end, "paragraph": para}


# ── 解析模型输出 ────────────────────────────────────────────


def extract_json_object(text: str) -> dict[str, Any]:
    """从可能带围栏/前后废话的输出里抠出 JSON 对象。"""
    raw = (text or "").strip()
    if not raw:
        raise ReviewPayloadError("审稿模型返回了空内容", code="empty_response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        first, last = raw.find("{"), raw.rfind("}")
        if first >= 0 and last > first:
            raw = raw[first:last + 1]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ReviewPayloadError(
            f"审稿输出不是合法 JSON：{exc}", code="invalid_json", detail=str(exc),
        ) from exc
    if not isinstance(data, dict):
        raise ReviewPayloadError("审稿输出顶层不是对象", code="invalid_json")
    return data


def _finding_id(evidence: str, suggestion: str) -> str:
    digest = hashlib.sha1(f"{evidence}||{suggestion}".encode("utf-8")).hexdigest()
    return f"f-{digest[:10]}"


def _clean_severity(value: Any) -> str:
    sev = str(value or "").strip().lower()
    return sev if sev in SEVERITIES else "major"


def _clean_type(value: Any) -> str:
    kind = str(value or "").strip().lower()
    for alias, key in (
        ("开头", "opening"), ("功能", "function"), ("钩子", "hook"), ("人物", "character"),
        ("对话", "dialogue"), ("连贯", "continuity"), ("穿帮", "continuity"),
        ("节奏", "pacing"), ("信息", "pacing"), ("语言", "language"), ("逻辑", "logic"),
    ):
        if alias in kind:
            return key
    return kind if kind in TYPE_LABELS else "other"


_QUOTE_PUNCT = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "、": ",", "“": '"', "”": '"', "‘": "'", "’": "'",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "—": "-", "－": "-", "…": ".",
})


def normalize_quote(text: str) -> str:
    """比对「同一句」用的归一化：去空白、统一全半角标点、去掉首尾标点。

    只用于**判断重复**，不用于定位——定位必须用原文，否则偏移会错。
    """
    raw = re.sub(r"\s+", "", str(text or ""))
    raw = raw.translate(_QUOTE_PUNCT).lower()
    return raw.strip(",.!?;:\"'()[]<>-·")


def _suppressors(existing_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """上一轮「已采纳」和「已拒绝」都进抑制集。

    已拒绝尤其重要：作者明确说「保留原句」之后，下一次审稿再把同一句提出来，
    等于没听见作者的裁决。但抑制不是永久封死——作者在界面上点「重新考虑」后
    （条目带 `reopened_at`），它就不再抑制。
    """
    out: list[dict[str, Any]] = []
    for it in existing_items:
        status = str(it.get("status") or "")
        if status not in ("adopted", "rejected"):
            continue
        if it.get("reopened_at"):
            continue
        quote = str(it.get("evidence") or "").strip()
        if not quote:
            continue
        out.append({
            "quote": quote,
            "normalized": normalize_quote(quote),
            "status": status,
            "id": str(it.get("id") or ""),
            "review_id": str(it.get("review_id") or ""),
            "created_at": str(it.get("review_created_at") or it.get("status_at") or ""),
            "reject_reason": str(it.get("reject_reason") or ""),
        })
    return out


def _match_suppressor(
    evidence: str, suppressors: list[dict[str, Any]]
) -> tuple[dict[str, Any], str] | None:
    """先按原文逐字比对，再按归一化比对。返回 (命中条目, 比对方式)。"""
    norm = normalize_quote(evidence)
    for row in suppressors:
        if row["quote"] == evidence:
            return row, "原文逐字一致"
    for row in suppressors:
        if norm and row["normalized"] == norm:
            return row, "归一化后一致（仅标点/空白不同）"
    return None


def normalize_items(
    raw_items: Any, prose: str, *, existing_items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把模型给的条目收成可执行 finding；收不了的写进 invalid（带原因，不静默丢）。

    `existing_items` 是历史审稿条目：引用与建议已采纳或已拒绝过的，新一次审稿再审出
    同一条，标成 duplicate —— 否则作者会看到「刚改完的」或「刚拒绝的」那句又被提一次。
    抑制只在**引文仍能在当前正文里定位**时成立：正文改过、这句话已经不在了，
    就属于新情况，不抑制。
    """
    existing_items = existing_items or []
    suppressors = _suppressors(existing_items)
    items: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items if isinstance(raw_items, list) else []):
        row = raw if isinstance(raw, dict) else {}
        evidence = str(row.get("evidence") or "").strip()
        suggestion = str(row.get("suggestion") or "").strip()
        problem = str(row.get("problem") or row.get("comment") or "").strip()
        hint = 0
        try:
            hint = int(row.get("paragraph") or 0)
        except (TypeError, ValueError):
            hint = 0
        if not evidence:
            invalid.append({"index": index, "reason": "缺少 evidence（正文原句）"})
            continue
        if not suggestion:
            invalid.append({"index": index, "reason": f"缺少改写建议：{evidence[:24]}", "evidence": evidence})
            continue
        if len(evidence) < MIN_EVIDENCE_CHARS:
            invalid.append({"index": index, "reason": "引文太短，无法唯一定位", "evidence": evidence})
            continue
        spot = locate_evidence(prose, evidence, paragraph_hint=hint)
        if not spot:
            occurrences = (prose or "").count(evidence)
            reason = ("引文在正文里找不到（模型可能改过字）" if occurrences == 0
                      else f"引文在正文里出现 {occurrences} 次，无法唯一定位")
            invalid.append({"index": index, "reason": reason, "evidence": evidence})
            continue
        fid = _finding_id(evidence, suggestion)
        if fid in seen:
            invalid.append({"index": index, "reason": "与本次另一条完全重复", "evidence": evidence})
            continue
        seen.add(fid)
        status = "open"
        repeat_of = ""
        repeat_reason = ""
        matched = _match_suppressor(evidence, suppressors)
        if matched:
            row_hit, how = matched
            status = "duplicate"
            repeat_of = row_hit["status"]
            repeat_reason = (
                f"与审稿 {row_hit['review_id'] or '（旧稿）'} 中"
                f"{'已采纳' if row_hit['status'] == 'adopted' else '已拒绝'}"
                f"的条目 {row_hit['id'] or ''} 相同（{how}）；"
                "正文改过且这句仍在，按作者上一次裁决不再重复提出"
            )
            if row_hit["status"] == "rejected" and row_hit["reject_reason"]:
                repeat_reason += f"。上次拒绝理由：{row_hit['reject_reason']}"
        items.append({
            "id": fid,
            "index": len(items),
            "type": _clean_type(row.get("type")),
            "severity": _clean_severity(row.get("severity")),
            "evidence": evidence,
            "start": spot["start"],
            "end": spot["end"],
            "paragraph": spot["paragraph"],
            "problem": problem or TYPE_LABELS.get(_clean_type(row.get("type")), "问题"),
            "suggestion": suggestion,
            "locatable": True,
            "status": status,
            "repeat_of": repeat_of,
            "repeat_reason": repeat_reason,
            "status_at": "",
            "reject_reason": "",
        })
        if len(items) >= MAX_ITEMS:
            break
    return items, invalid


# ── 提示词 ──────────────────────────────────────────────────


def build_review_messages(
    *, prose: str, brief: dict[str, Any], setting: dict[str, Any],
    craft_block: str = "", word_range: list[int] | None = None,
) -> list[dict[str, str]]:
    """审正文。规则写死在这里：**引文必须逐字**，建议必须能直接替换。"""
    target = "、".join(str(x) for x in (word_range or [])) or "未给"
    setting_brief = json.dumps(
        {
            "genre": (setting or {}).get("genre") or "",
            "logline": (setting or {}).get("logline") or "",
            "style": (setting or {}).get("style") or {},
        },
        ensure_ascii=False,
    )
    command = json.dumps(
        {
            "one_line": (brief or {}).get("one_line") or "",
            "core_plot": (brief or {}).get("core_plot") or [],
            "must_not": (brief or {}).get("must_not") or [],
            "word_target": (brief or {}).get("word_target") or [],
        },
        ensure_ascii=False,
    )
    system = (
        "你是网文主编，只做一件事：给这一章正文挑出**能立刻动手改**的问题。"
        "你的输出要被人逐条勾选后直接写回正文，所以每一条都必须能落到具体句子。\n"
        "铁律：\n"
        "1. evidence 必须是从下方正文里**逐字复制**的连续片段，"
        f"长度 {MIN_EVIDENCE_CHARS}–60 字，且在整章里只出现一次；不要概括、不要加省略号、不要改标点。\n"
        "2. suggestion 是**用来直接替换 evidence 的改写文本**，语义要贴合上下文，"
        "不要写「建议增加细节」这种没法执行的指令。\n"
        "3. 宁可只提 3 条真问题，不要凑 10 条套话；每条都要指出位置。\n"
        "4. 只输出 JSON，不要 markdown 围栏，不要解释。\n"
        '格式：{"summary":"一句话总评","items":[{"type":"opening|function|hook|character|'
        'dialogue|continuity|pacing|language|logic|other","severity":"blocker|major|minor",'
        '"evidence":"正文原句","problem":"问题是什么","suggestion":"替换这句的改写"}]}'
    )
    user = (
        (f"【设定摘要】{setting_brief}\n\n" if setting_brief else "")
        + (f"【本章任务】{command}（目标字数 {target}）\n\n" if command else "")
        + (f"{craft_block}\n\n" if craft_block else "")
        + "【正文】\n" + (prose or "")
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ── 调用与落盘 ──────────────────────────────────────────────


def _call_usage(result: Any) -> dict[str, int]:
    usage = getattr(result, "usage", None)
    return usage.to_dict() if usage is not None and getattr(usage, "present", False) else {}


def run_review(
    *,
    client: Any,
    model_id: str,
    prose: str,
    brief: dict[str, Any],
    setting: dict[str, Any],
    craft_block: str = "",
    max_tokens: int = DEFAULT_REVIEW_MAX_TOKENS,
    existing_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """调一次审稿。JSON 解析失败**重试一次**（D1）；两次都不行就明确报错。"""
    import time

    from .errors import ErrorKind
    from .llm import build_thinking_extra

    messages = build_review_messages(
        prose=prose, brief=brief, setting=setting, craft_block=craft_block,
        word_range=(brief or {}).get("word_target"),
    )
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for attempt in range(2):
        start = time.perf_counter()
        try:
            result = client.chat(
                model=model_id, messages=messages,
                max_tokens=max_tokens, temperature=0.2,
                # 审稿要的是一份短 JSON。默认思考模式下，思维链会先吃满
                # max_tokens，正文一个字都吐不出来（实测 4000/8000 全被 reasoning 吃光）。
                # 所以这一类结构化任务必须显式关掉思考。
                extra=build_thinking_extra("disabled"),
            )
        except Exception as exc:  # noqa: BLE001 — 上游错误统一归类
            code = str(getattr(getattr(exc, "kind", None), "value", "") or
                       ErrorKind.UNKNOWN.value)
            attempts.append({"attempt": attempt + 1, "ok": False, "error_code": code,
                             "error": str(exc)})
            raise ReviewPayloadError(
                f"审稿调用失败（{code}）：{exc}", code=code,
            ) from exc
        elapsed_ms = (time.perf_counter() - start) * 1000
        usage = _call_usage(result)
        finish_reason = str(getattr(result, "finish_reason", "") or "")
        row = {
            "attempt": attempt + 1,
            "elapsed_ms": round(elapsed_ms, 1),
            "usage": usage,
            "finish_reason": finish_reason,
            "chars": len(getattr(result, "text", "") or ""),
        }
        attempts.append(row)
        try:
            data = extract_json_object(getattr(result, "text", "") or "")
            items, invalid = normalize_items(
                data.get("items"), prose, existing_items=existing_items,
            )
        except ReviewPayloadError as exc:
            last_error = exc
            # 截断导致的非法 JSON 不是「模型不听话」，是输出预算不够。
            # 这时重试同一份提示词只会再截一次，必须点明病根。
            if finish_reason == "length":
                raise ReviewPayloadError(
                    "审稿输出被 max_tokens 截断，JSON 不完整。"
                    "把审稿的 max_tokens 调大再试一次。",
                    code="truncated", detail=str(exc),
                ) from exc
            row["error"] = str(exc)
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": (getattr(result, "text", "") or "")[:400]},
                    {"role": "user", "content":
                        "上一条输出不是合法 JSON。只输出一个 JSON 对象，"
                        "不要围栏、不要解释；evidence 必须逐字复制正文原句。"},
                ]
                continue
            raise exc
        if not items and invalid:
            # 全都没法定位：不要给作者一张「0 条可采纳」的空表还以为审得很干净。
            if attempt == 0:
                row["error"] = "没有任何一条能定位到正文"
                messages = messages + [
                    {"role": "assistant", "content": (getattr(result, "text", "") or "")[:400]},
                    {"role": "user", "content":
                        "你给的所有 evidence 都没能在正文里逐字找到。"
                        "重新审：每条 evidence 必须从【正文】里原样复制一段连续文字。"},
                ]
                continue
            raise ReviewPayloadError(
                f"连续两次审稿都没有一条能定位到正文（{len(invalid)} 条被拒："
                f"{invalid[0].get('reason') or '引文对不上'}）",
                code="no_locatable_findings",
            )
        return {
            "ok": True,
            "summary": str(data.get("summary") or "").strip(),
            "items": items,
            "invalid": invalid,
            "attempts": attempts,
            "usage": usage,
            "finish_reason": finish_reason,
            "call": {
                "stage": "review",
                "round": 0,
                "chars": len(prose or ""),
                "new_chars": 0,
                "total_chars": len(prose or ""),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "finish_reason": finish_reason,
                "elapsed_ms": round(sum(float(a.get("elapsed_ms") or 0) for a in attempts), 1),
                "max_tokens": max_tokens,
                "attempt_count": len(attempts),
                "error_code": "",
                "ok": True,
            },
        }
    if last_error:
        raise last_error
    raise ReviewPayloadError("审稿没有产出结果", code="empty_response")


def append_review(
    work_dir: str | Path, chapter_id: str, entry: dict[str, Any]
) -> dict[str, Any]:
    """把一次审稿追加进历史。旧的审稿**不覆盖**（A3 要能对比前后两次）。"""
    payload = load_reviews(work_dir, chapter_id)
    payload["reviews"].append(entry)
    save_reviews(work_dir, chapter_id, payload)
    return entry


def new_review_id() -> str:
    return f"{_stamp()}-{uuid.uuid4().hex[:6]}"


def find_review(payload: dict[str, Any], review_id: str) -> dict[str, Any] | None:
    return next((r for r in payload.get("reviews") or []
                 if str(r.get("review_id") or "") == str(review_id)), None)


def all_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把多次审稿的条目摊平，并附上来源审稿信息。

    附 `review_id` 是为了让「这条为什么不再提」在界面上能说清出处，
    而不是只给一个 `duplicate` 状态。
    """
    out: list[dict[str, Any]] = []
    for review in payload.get("reviews") or []:
        for item in review.get("items") or []:
            out.append({
                **item,
                "review_id": review.get("review_id") or "",
                "review_created_at": review.get("created_at") or "",
            })
    return out


def effective_status(item: dict[str, Any], review: dict[str, Any], prose_sha: str) -> str:
    """正文改过之后，旧审稿里还挂着的条目按 stale 算——偏移已经不是那份正文的了。"""
    status = str(item.get("status") or "open")
    if status == "open" and str(review.get("prose_sha") or "") != (prose_sha or ""):
        return "stale"
    return status


def summarize(payload: dict[str, Any], prose_sha: str = "") -> list[dict[str, Any]]:
    """给界面的历史列表：每次审稿的条数、各状态计数、以及模型/时间。"""
    rows: list[dict[str, Any]] = []
    for review in payload.get("reviews") or []:
        counts = {"open": 0, "adopted": 0, "rejected": 0, "stale": 0,
                  "duplicate": 0, "unlocatable": 0}
        for item in review.get("items") or []:
            status = effective_status(item, review, prose_sha) if prose_sha else str(
                item.get("status") or "open")
            counts[status] = counts.get(status, 0) + 1
        rows.append({
            "review_id": review.get("review_id") or "",
            "created_at": review.get("created_at") or "",
            "model": review.get("model") or "",
            "summary": review.get("summary") or "",
            "prose_sha": review.get("prose_sha") or "",
            "chars": review.get("chars") or 0,
            "count": len(review.get("items") or []),
            "counts": counts,
            "invalid_count": len(review.get("invalid") or []),
            "attempts": review.get("attempt_count") or 0,
            "stale": bool(prose_sha) and str(review.get("prose_sha") or "") != prose_sha,
        })
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows


def public_items(review: dict[str, Any], prose_sha: str = "") -> list[dict[str, Any]]:
    """界面用的条目：带 label、按严重度排序，并把 stale 算进去。"""
    sev_rank = {"blocker": 0, "major": 1, "minor": 2}
    rows: list[dict[str, Any]] = []
    for item in review.get("items") or []:
        row = dict(item)
        row["status"] = effective_status(item, review, prose_sha) if prose_sha else (
            item.get("status") or "open")
        row["type_label"] = TYPE_LABELS.get(str(item.get("type") or ""), "其他")
        row["adoptable"] = row["status"] == "open" and bool(item.get("locatable"))
        rows.append(row)
    rows.sort(key=lambda r: (sev_rank.get(str(r.get("severity") or ""), 9), r.get("index") or 0))
    return rows
