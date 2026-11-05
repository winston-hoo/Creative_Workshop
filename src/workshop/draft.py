"""M9 创作台 · 写正文：三层真源拼成一个调用，**一次只出一章**。

## 为什么一次只出一章

这是作者自己的书。一次吐十章，他改起来等于重写；而且第一章就不满意的话，
后面九章全废——那九章的钱和读的时间都是白花的。一章一停，他读完再决定下一章。

## 这里不做的事

不自动串章、不自动往下写。跑完这一章就停，等作者看完再说。
（`ponytail:` 刻意不做批量——省下来的那点点击，换来的是作者失去对每一章的否决权。
  什么时候加：作者明确说「连着写三章我看着改」，那就加，但要一章一确认。）

## 自检只做机械能查的

字数、禁令字面命中、该出场的人有没有漏。钩子够不够、情节顺序对不对、
伏笔回收得漂不漂亮——这些机器查不了，硬查只会给出假绿灯。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import (
    MAX_TOKENS_CAP, ApiError, ChatResult, OpenAICompatProvider, build_thinking_extra,
)

DRAFT_SCHEMA_VERSION = "draft-v1"

# 自检判据。太松等于没查，太紧会天天误报，作者就不看了。
SHORT_RATIO = 0.5   # 低于目标下限的一半 → 阻断（多半是没写完 / 被截断）

# 句末标点。模型被 max_tokens 截断时，末尾会是断的——
# 各家 API 对 finish_reason 的支持不一致（实测这家返回 None），
# 所以用「末尾有没有句末标点」当独立信号，不依赖供应商。
_SENTENCE_END = "。！？…—\"'”’）】》」』"


def draft_dir(work_dir: Path) -> Path:
    return work_dir / "90-draft"


def draft_text_path(work_dir: Path, chapter_id: str) -> Path:
    return draft_dir(work_dir) / f"{chapter_id}.md"


def draft_meta_path(work_dir: Path, chapter_id: str) -> Path:
    return draft_dir(work_dir) / f"{chapter_id}.json"


def count_chars(text: str) -> int:
    """字数：去掉所有空白。中英混排也按字符算，不搞分词那套。"""
    return len(re.sub(r"\s", "", text or ""))


# ── 计划（免费） ────────────────────────────────────────────


@dataclass
class DraftPlan:
    chapter_id: str
    title: str
    provider_id: str
    model_id: str
    setting_chars: int
    volume_chars: int
    brief_chars: int
    refs_chars: int
    word_range: list[int]
    est_input_tokens: int
    est_output_tokens: int
    est_cost_cny: float | None
    price_note: str
    blocker: str = ""
    warnings: list[str] = field(default_factory=list)
    preview: str = ""
    context_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "provider": self.provider_id,
            "model": self.model_id,
            "setting_chars": self.setting_chars,
            "volume_chars": self.volume_chars,
            "brief_chars": self.brief_chars,
            "refs_chars": self.refs_chars,
            "word_range": self.word_range,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "est_cost_cny": self.est_cost_cny,
            "price_note": self.price_note,
            "blocker": self.blocker,
            "warnings": self.warnings,
            "preview": self.preview,
            "context_report": self.context_report,
            "ready": bool(self.model_id) and not self.blocker,
        }


def build_draft_plan(
    *,
    chapter_id: str,
    title: str,
    setting: dict[str, Any],
    volume: dict[str, Any],
    brief: dict[str, Any],
    refs_block: str = "",
    provider_id: str,
    model_id: str,
    price_input_per_mtok: float | None = None,
    price_output_per_mtok: float | None = None,
    bucket: str | None = None,
    brief_errors: list[str] | None = None,
    brief_exists: bool = True,
    context_block: str = "",
    context_report: dict[str, Any] | None = None,
    word_range: list[int] | None = None,
) -> DraftPlan:
    """算出写这一章大概多少 token、多少钱。**不发起任何调用。**"""
    from .setting_assist import render_document

    setting_text = render_document(setting, layer="setting") if setting else ""
    volume_text = render_document(volume, layer="volume") if volume else ""
    brief_text = render_document(brief, layer="brief") if brief else ""
    rng = word_range or brief.get("word_target") or [2800, 4000]
    if not isinstance(rng, list) or len(rng) != 2:
        rng = [2800, 4000]

    # 技法也要算进输入估算。不算的话，界面上显示的 token 与费用会明显低于真实——
    # 技法一篇就几千字，低估起来是成倍的，而作者是照着这个数决定要不要点下去的。
    from .craft import craft_block

    craft_text = craft_block(
        chapter_no=int(brief.get("chapter_no") or 0),
        is_first_of_volume=int(brief.get("chapter_no") or 0) == 1 and int(brief.get("vol") or 1) > 1,
        brief=brief,
    )

    est_in = int((len(setting_text) + len(volume_text) + len(brief_text)
                  + len(context_block)
                  + len(refs_block) + len(craft_text) + 900) * 0.675)
    est_out = int((int(rng[1]) if rng[1] else 4000) * 0.9)
    cost = None
    note = "未填写单价，费用无法计算。"
    if price_input_per_mtok is not None and price_output_per_mtok is not None:
        cost = round(
            est_in / 1_000_000 * price_input_per_mtok + est_out / 1_000_000 * price_output_per_mtok, 4
        )
        bucket_text = {"peak": "高峰", "offpeak": "空闲"}.get(bucket or "", "不分时段")
        note = (f"按{bucket_text}单价估算：输入 {price_input_per_mtok} 元/百万、"
                f"输出 {price_output_per_mtok} 元/百万。输出按目标字数上限算。")

    blocker = ""
    warnings: list[str] = []
    if not brief_exists:
        blocker = f"还没有 {chapter_id} 的创作任务指令。先写指令——没有指令就写正文，等于让模型替你决定这一章要干什么。"
    elif brief_errors:
        blocker = (f"这一章的指令有 {len(brief_errors)} 项阻断没解决，先修指令：{brief_errors[0]}")
    elif not brief:
        blocker = "指令是空的。"
    if not setting:
        warnings.append("设定集是空的：模型没有任何人物与世界观可依，只会自己编一套")
    if not volume:
        warnings.append("没有本卷目录：模型不知道这一章在整卷里的位置")

    return DraftPlan(
        chapter_id=chapter_id,
        title=title,
        provider_id=provider_id,
        model_id=model_id,
        setting_chars=len(setting_text),
        volume_chars=len(volume_text),
        brief_chars=len(brief_text),
        refs_chars=len(refs_block),
        word_range=[int(rng[0]), int(rng[1])],
        est_input_tokens=est_in,
        est_output_tokens=est_out,
        est_cost_cny=cost,
        price_note=note,
        blocker=blocker,
        warnings=warnings,
        preview=brief_text,
        context_report=context_report or {},
    )


# ── 提示词 ──────────────────────────────────────────────────

def draft_system_text(work_dir: str | Path | None = None) -> str:
    """正文系统提示。文本在 `prompts/draft.system.md`，支持项目级覆盖（E1）。"""
    from .prompt_registry import render

    return render("draft.system", work_dir=work_dir)["text"]


def build_draft_messages(
    *,
    setting: dict[str, Any],
    volume: dict[str, Any],
    brief: dict[str, Any],
    refs_block: str = "",
    word_range: list[int] | None = None,
    title: str = "",
    craft: str | None = None,
    context_block: str = "",
    beats: str = "",
    work_dir: str | Path | None = None,
) -> list[dict[str, str]]:
    from .setting_assist import REFS_USAGE_HINT, render_document

    rng = word_range or [2800, 4000]
    # 技法默认自己挑（按章号与本章指令）。传 `craft=""` 可以显式关掉。
    if craft is None:
        from .craft import craft_block

        craft = craft_block(
            chapter_no=int(brief.get("chapter_no") or 0),
            is_first_of_volume=int(brief.get("chapter_no") or 0) == 1
            and int(brief.get("vol") or 1) > 1,
            brief=brief,
        )
    parts: list[str] = []

    # 写前必读放最前面：锚点要压在所有内容之上，否则模型先看完几千字设定集再看到它，
    # 那就成了"事后补充"，而不是"动笔前钉住"。
    if context_block:
        parts += [context_block, ""]
    if setting:
        parts += ["【设定集：这是唯一真源，人物与世界观都按它来】",
                  render_document(setting, layer="setting"), ""]
    if volume:
        parts += ["【本卷目录：这一章在整卷里的位置】",
                  render_document(volume, layer="volume"), ""]
    if brief:
        parts += ["【本章创作任务指令：按它写】", render_document(brief, layer="brief"), ""]
    # B1：细纲紧随指令之后。放在设定集/卷表前面会被几千字材料冲掉；
    # 放在指令后面，模型读完「这一章要干什么」，立刻看到「按这几步干」。
    if beats:
        parts += [beats, ""]
    if craft:
        parts += ["【技法参考：这些是写法，不是内容；按它组织，但不要照抄例句】", craft, ""]
    if refs_block:
        parts += ["【可借的素材参照（来自已入库作品的知识库）】", refs_block, "", REFS_USAGE_HINT, ""]
    if title:
        parts += [f"（本章标题是「{title}」，但正文里不要写标题。）"]

    parts += ["【字数】", f"落在 {rng[0]}–{rng[1]} 字之间。", "",
              "现在只输出这一章的正文。"]
    return [
        {"role": "system", "content": draft_system_text(work_dir)},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ── 生成（会花钱） ──────────────────────────────────────────


MAX_CONTINUE_ROUNDS = 3
"""截断后最多再续写几轮。刻意有上限：模型如果反复写不完整，
再多轮也只是把同一段尾巴复读得更长，钱和时间都白花。"""

CONTINUE_INSTRUCTION = (
    "上一段正文被 max_tokens 截断了。请**从断掉的地方接着写**，只输出接下来的正文：\n"
    "- 不要重写开头，不要重复已经写过的任何句子或段落；\n"
    "- 不要写「续」「（未完）」之类的标记，不要解释；\n"
    "- 保持原来的视角、语气与人物状态，直接往下叙事。"
)

# 输出预算按「本章目标字数上限」反推，而不是给一个拍脑袋的小常数。
# 实测教训：首轮只给 1200 输出 token，写到一半就被截断，续写一轮等于把整段上下文
# 再发一次（两轮 prompt token 合计 22,934，输入侧几乎翻倍），比一次给足更贵。
# 系数 0.968 来自探测报告的中文 token/字实测值；1.3 是标点与轻度扩写的余量；
# 下限 2048 兜住小目标。上限取「模型声明的 max_output」与全局 MAX_TOKENS_CAP 的较小值：
# 本机配置里声明的 384000 明显大于上游实际可用输出，直接当真没有意义。
CJK_TOKENS_PER_CHAR = 0.968
OUTPUT_TOKEN_MARGIN = 1.3
MIN_OUTPUT_TOKENS = 2048


def derive_max_tokens(
    word_range: list[int] | None = None,
    model_cfg: dict[str, Any] | None = None,
) -> int:
    """按本章目标字数算这一次的输出上限（含下限与模型上限）。"""
    rng = word_range if isinstance(word_range, (list, tuple)) and len(word_range) == 2 else [2800, 4000]
    try:
        high = int(rng[1])
    except (TypeError, ValueError):
        high = 4000
    if high <= 0:
        high = 4000
    want = int(round(high * CJK_TOKENS_PER_CHAR * OUTPUT_TOKEN_MARGIN))
    size = max(MIN_OUTPUT_TOKENS, want)
    try:
        declared = int((model_cfg or {}).get("max_output") or 0)
    except (TypeError, ValueError):
        declared = 0
    # 声明值只作为「更小的上限」来用，不把它直接当真；全局上限始终生效。
    cap = min(declared, MAX_TOKENS_CAP) if declared > 0 else MAX_TOKENS_CAP
    # 模型声明的上限必须遵守；只有它低于下限时才让步，否则请求本身会被上游拒绝。
    if cap >= MIN_OUTPUT_TOKENS:
        return int(min(size, cap))
    return int(max(1024, cap))


@dataclass
class DraftResult:
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
    error_code: str = ""
    finish_reason: str = ""
    truncated: bool = False
    continued: bool = False
    """这一章是否靠续写才写成（首轮就被截断过）。"""
    continue_rounds: int = 0
    hit_continue_limit: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    """每一次模型调用的账：轮次、字数、finish_reason、耗时、用量。写进 manifest。"""


def _merge_usage(total: dict[str, int], usage: dict[str, int] | None) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _call_row(
    *,
    stage: str,
    round_no: int,
    attempt: int,
    raw_text: str,
    new_text: str,
    total_chars: int,
    usage: dict[str, int],
    finish_reason: str,
    elapsed_ms: float,
    max_tokens: int,
    dedupe: dict[str, Any] | None = None,
    error_code: str = "",
) -> dict[str, Any]:
    return {
        "stage": stage,
        "round": round_no,
        "attempt": attempt,
        "chars": count_chars(raw_text),
        "new_chars": count_chars(new_text),
        "total_chars": total_chars,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "finish_reason": finish_reason,
        "elapsed_ms": round(float(elapsed_ms), 1),
        "max_tokens": max_tokens,
        "dedupe": dedupe or {},
        "error_code": error_code,
        "ok": not error_code,
    }


def generate_draft(
    *,
    client: OpenAICompatProvider,
    model_id: str,
    messages: list[dict[str, str]],
    max_tokens: int = 8000,
    temperature: float = 0.8,
    max_attempts: int = 2,
    thinking: str | None = "disabled",
    cancel_event: Any | None = None,
    cancel_check: Any | None = None,
    on_round: Any | None = None,
    on_partial: Any | None = None,
    prior_text: str = "",
    max_continue_rounds: int = MAX_CONTINUE_ROUNDS,
) -> DraftResult:
    """写这一章。**只返回文本，不落盘**——落盘由调用方决定。

    2026-11 起改走流式：
      · 只有流式能在读到一半时**真的停掉**（取消）；
      · `finish_reason=length` 也只有流式结束帧才稳定拿得到。

    被截断时自动按章续写，最多 `max_continue_rounds` 轮；续写开头与上一轮尾巴的
    重叠交给 `text_overlap.dedupe_continuation` 机械裁掉，不靠提示词自觉。
    """
    from .errors import ErrorKind, GenerationCancelled
    from .text_overlap import dedupe_continuation, stitch

    extra = build_thinking_extra(thinking, None)
    usage_total: dict[str, int] = {}
    calls: list[dict[str, Any]] = []
    accumulated = strip_wrapping(prior_text) if prior_text else ""
    continued = bool(prior_text)
    continue_rounds = 0
    last_finish = ""
    last_error = ""
    last_error_code = ""
    hit_limit = False
    round_no = 0

    def cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set()) or bool(
            cancel_check and cancel_check()
        )

    def emit(row: dict[str, Any]) -> None:
        calls.append(row)
        if on_round:
            on_round(row)

    while True:
        if cancelled():
            raise GenerationCancelled("已取消")
        first = not prior_text and round_no == 0
        stage = "draft" if first else "continue"
        if first:
            call_messages = messages
        else:
            call_messages = list(messages) + [
                {"role": "assistant", "content": accumulated},
                {"role": "user", "content": CONTINUE_INSTRUCTION},
            ]

        attempt = 0
        result = None
        while attempt < max(1, max_attempts):
            attempt += 1
            if cancelled():
                raise GenerationCancelled("已取消")
            try:
                result = client.chat_stream(
                    model_id, call_messages, max_tokens=max_tokens,
                    temperature=temperature, extra=extra, cancel_event=cancel_event,
                )
            except GenerationCancelled as exc:
                # 把**此前各轮**的正文一起带出去。只给当前这一轮的话，
                # 从第二轮取消时，第一轮写好的内容就丢了，作者没法从断点继续。
                raise GenerationCancelled(
                    str(exc), partial_text=(accumulated + (exc.partial_text or ""))
                ) from exc
            except ApiError as exc:
                last_error_code = exc.kind.value
                last_error = f"{exc.kind.value}：{exc.safe_body(client.secrets)}"
                emit(_call_row(
                    stage=stage, round_no=round_no, attempt=attempt, raw_text="",
                    new_text="", total_chars=count_chars(accumulated), usage={},
                    finish_reason="", elapsed_ms=0, max_tokens=max_tokens,
                    error_code=last_error_code,
                ))
                if exc.kind in (ErrorKind.AUTH_FAILED, ErrorKind.ENDPOINT_NOT_FOUND,
                                ErrorKind.MODEL_NOT_FOUND):
                    return DraftResult(
                        text=accumulated, usage=usage_total, error=last_error,
                        error_code=last_error_code, calls=calls,
                        continued=continued, continue_rounds=continue_rounds,
                    )
                continue
            usage = result.usage.to_dict() if result.usage else {
                "prompt_tokens": 0,
                "completion_tokens": result.output_tokens,
                "total_tokens": result.output_tokens,
            }
            _merge_usage(usage_total, usage)
            if not (result.text or "").strip():
                last_error_code = ErrorKind.EMPTY_RESPONSE.value
                last_error = "模型返回了空正文"
                emit(_call_row(
                    stage=stage, round_no=round_no, attempt=attempt, raw_text="",
                    new_text="", total_chars=count_chars(accumulated), usage=usage,
                    finish_reason=result.finish_reason, elapsed_ms=result.total_ms,
                    max_tokens=max_tokens, error_code=last_error_code,
                ))
                continue
            break

        if result is None or not (result.text or "").strip():
            return DraftResult(
                text=accumulated, usage=usage_total, error=last_error or "模型没有返回内容",
                error_code=last_error_code, calls=calls, continued=continued,
                continue_rounds=continue_rounds,
            )

        finish = result.finish_reason or ""
        truncated = finish in ("length", "max_tokens")
        # 有些供应商不给 finish_reason。输出刚好顶满 max_tokens 时用用量兜底——
        # 不能因为上游少一个字段，就让半章正文被当成完整章节落盘。
        if not finish and max_tokens and int(usage.get("completion_tokens") or 0) >= max_tokens:
            truncated = True
        last_finish = finish

        if first:
            cleaned = strip_wrapping(result.text)
            dedupe_stats: dict[str, Any] = {}
        else:
            cleaned_raw = strip_wrapping(result.text)
            cleaned, dedupe_stats = dedupe_continuation(accumulated, cleaned_raw)
        if not cleaned.strip() and not truncated:
            last_error_code = ErrorKind.EMPTY_RESPONSE.value
            last_error = "续写只重复了上一段，没有产生新内容"
        new_total = stitch(accumulated, cleaned) if not first else cleaned
        if stage == "continue":
            continue_rounds += 1
            continued = True
        accumulated = new_total
        row = _call_row(
            stage=stage, round_no=round_no, attempt=attempt, raw_text=result.text,
            new_text=cleaned, total_chars=count_chars(accumulated), usage=usage,
            finish_reason=finish, elapsed_ms=result.total_ms, max_tokens=max_tokens,
            dedupe=dedupe_stats,
            error_code=(ErrorKind.EMPTY_RESPONSE.value
                        if not cleaned.strip() and not truncated else ""),
        )
        emit(row)
        if on_partial:
            on_partial(accumulated, row)

        if not truncated:
            return DraftResult(
                text=accumulated, usage=usage_total, error=last_error,
                error_code=last_error_code, finish_reason=last_finish, truncated=False,
                continued=continued, continue_rounds=continue_rounds, calls=calls,
            )
        # 到了轮数上限还截断：这一章就是没写完，交给调用方按失败处理。
        if continue_rounds >= max(1, max_continue_rounds):
            hit_limit = True
            return DraftResult(
                text=accumulated, usage=usage_total,
                error=f"续写到上限（{continue_rounds} 轮）仍被 max_tokens 截断，这一章没写完",
                error_code=ErrorKind.TRUNCATED.value,
                finish_reason=last_finish, truncated=True, continued=continued,
                continue_rounds=continue_rounds, hit_continue_limit=True, calls=calls,
            )
        round_no += 1


_TITLE_PATTERNS = (
    re.compile(r"^\s*#{1,6}\s+.*\n+"),
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百零]+\s*章[^\n]*\n+"),
)


def strip_wrapping(text: str) -> str:
    """去掉模型爱加的壳：markdown 标题、章节号、前后引号、```围栏。

    它总会加。不加壳是提示词里的要求，但要求归要求，回来还是要洗一遍——
    正文文件里混进「第 1 章 数值」这行，后面所有字数统计和拼接都得跟着脏。
    """
    out = (text or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\s*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out)
    for _ in range(3):
        before = out
        for pattern in _TITLE_PATTERNS:
            out = pattern.sub("", out, count=1).strip()
        if out == before:
            break
    # 整段被引号包起来的情况
    for left, right in (("“", "”"), ("「", "」"), ('"', '"'), ("'", "'")):
        if len(out) > 2 and out.startswith(left) and out.endswith(right):
            out = out[1:-1].strip()
    return out


# ── 自检（纯脚本，零成本） ──────────────────────────────────


def check_draft(
    text: str,
    *,
    brief: dict[str, Any] | None = None,
    setting: dict[str, Any] | None = None,
    word_range: list[int] | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """机械自检。返回 (阻断, 提示, 统计)。

    **只查机器查得准的**：字数、禁令字面命中、该出场的人有没有漏。
    钩子够不够、情节顺不顺、伏笔漂不漂亮——查不了，硬查只会给假绿灯。
    """
    brief = brief or {}
    setting = setting or {}
    body = strip_wrapping(text)
    chars = count_chars(body)
    rng = word_range or brief.get("word_target") or [2800, 4000]
    if not isinstance(rng, list) or len(rng) != 2:
        rng = [2800, 4000]
    low, high = int(rng[0]), int(rng[1])

    errors: list[str] = []
    warnings: list[str] = []

    if chars == 0:
        errors.append("正文是空的")
    elif chars < low * SHORT_RATIO:
        errors.append(
            f"只有 {chars} 字，不到目标下限 {low} 的一半——多半是没写完或被截断了"
        )
    elif chars < low:
        warnings.append(f"{chars} 字，短于目标下限 {low}")
    elif chars > high:
        # 超出就报，不给宽容带。宽容带会造成「统计说不在区间内、却一条提示都没有」
        # 这种假绿灯——实测第一次真写就撞上了：4784 字对 2800–4000 的目标，静默通过。
        warnings.append(f"{chars} 字，超出目标上限 {high}")

    tail = body.rstrip()
    if tail and tail[-1] not in _SENTENCE_END:
        warnings.append(
            f"正文末尾是「…{tail[-12:]}」，不是句末标点——可能是被截断的，读的时候留意结尾"
        )

    must_not = [str(x).strip() for x in (brief.get("must_not") or []) if str(x).strip()]
    for item in must_not:
        if item and item in body:
            errors.append(f"写到了「禁止出现」里的内容：{item}")

    settings = brief.get("settings") or {}
    appeared, missing = [], []
    for name in settings.get("characters") or []:
        name = str(name).strip()
        if not name:
            continue
        (appeared if name in body else missing).append(name)
    if missing:
        warnings.append("指令里写了要出场、但正文里找不到的人物：" + "、".join(missing))

    foreshadow = brief.get("foreshadow") or []
    # 偏离量单独落一份，别只藏在 warnings 的一句话里：
    # 界面要显示「短了/长了多少」，账本也要能回看，而不是去正则里抠字符串。
    if chars < low:
        deviation, deviation_kind = chars - low, "short"
    elif chars > high:
        deviation, deviation_kind = chars - high, "long"
    else:
        deviation, deviation_kind = 0, "ok"
    base = low if deviation_kind == "short" else high
    deviation_pct = round(abs(deviation) / base * 100, 1) if base else 0.0
    stats = {
        "chars": chars,
        "word_range": [low, high],
        "in_range": deviation_kind == "ok",
        "deviation": deviation,
        "deviation_kind": deviation_kind,
        "deviation_pct": deviation_pct,
        "characters_appeared": appeared,
        "characters_missing": missing,
        "foreshadow_actions": len(foreshadow),
        "paragraphs": len([p for p in re.split(r"\n\s*\n", body) if p.strip()]),
    }
    return errors, warnings, stats


# ── 落盘 ────────────────────────────────────────────────────


def save_draft(
    work_dir: Path,
    chapter_id: str,
    *,
    text: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, str]:
    """正文写成普通 .md（作者能拿去任何地方读），元数据写 .json 侧车。"""
    directory = draft_dir(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    text_path = draft_text_path(work_dir, chapter_id)
    text_path.write_text(text, encoding="utf-8")
    payload = {"schema_version": DRAFT_SCHEMA_VERSION, "chapter_id": chapter_id}
    payload.update(meta or {})
    draft_meta_path(work_dir, chapter_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"text_path": str(text_path), "meta_path": str(draft_meta_path(work_dir, chapter_id))}


def load_draft(work_dir: Path, chapter_id: str) -> dict[str, Any]:
    text_path = draft_text_path(work_dir, chapter_id)
    if not text_path.exists():
        return {"exists": False, "chapter_id": chapter_id, "text": "", "meta": {}}
    meta: dict[str, Any] = {}
    meta_path = draft_meta_path(work_dir, chapter_id)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            meta = {}
    return {
        "exists": True,
        "chapter_id": chapter_id,
        "text": text_path.read_text(encoding="utf-8"),
        "meta": meta,
        "text_path": str(text_path),
    }


def list_drafts(work_dir: Path) -> list[dict[str, Any]]:
    directory = draft_dir(work_dir)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        chapter_id = path.stem
        loaded = load_draft(work_dir, chapter_id)
        rows.append(
            {
                "chapter_id": chapter_id,
                "chars": count_chars(loaded["text"]),
                "model": (loaded.get("meta") or {}).get("model") or "",
                "generated_at": (loaded.get("meta") or {}).get("generated_at") or "",
            }
        )
    return rows
