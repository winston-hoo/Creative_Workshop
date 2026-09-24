"""大纲：把上千章的逐章梗概逐层归约成一份能读的大纲。

## 原料从哪来

每章的 `chapter_summary`（P22，一句话剧情梗概）在**标注时顺带产出**，
不额外花钱。这是刻意的：与其将来为了做大纲再单独跑一遍上千次调用，
不如在标注那一次调用里一起要。所以**大纲的前置条件是有标注**。

没有标注时本模块会明确说「缺原料」，而不是生成一份凭空捏造的大纲——
那正是这个项目一直在防的那类静默失效。

## 为什么要分层归约

933 章的梗概按 48 字算约 4.5 万字 ≈ 4.3 万 token，单次调用塞得下。
但一次性喂进去有两个问题：一是长输入里的细节会被忽略，
二是任何一处失败就得整本重跑。

所以按块归约（Map-Reduce 的第二层，设计文档里本来就有）：

    逐章梗概 ──每 50 章一块──▶ 块梗概 ──▶ 全书大纲

每块一次调用，块与块之间彼此独立，**可以断点续跑**。

## 显式原则

和标注一样：看计划免费，跑要用户主动点。成本按块数算，一眼可见。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .annotate import load_annotation
from .batch import ChapterTask, load_chapter_tasks
from .errors import ErrorKind  # noqa: F401
from .llm import (
    DEFAULT_MAX_TOKENS,
    MAX_TOKENS_CAP,
    ApiError,
    ChatResult,
    OpenAICompatProvider,
    build_thinking_extra,
    extract_json_object,
    looks_truncated,
    repair_truncated_json,
)
from .secrets import redact

OUTLINE_SCHEMA_VERSION = "outline-v1"
DEFAULT_BLOCK_SIZE = 50

_BLOCK_PROMPT = """你是长篇小说的结构分析员。下面是一批连续的章节梗概，请把它们归约成一段连贯的段落梗概。

要求：
- 按事件发生的先后顺序，讲清这一段里主线发生了什么、主角处境如何变化
- 保留关键转折、重要配角的登场与退场、以及悬念的提出与解答
- 只写这一段里真实出现的内容，不要推测、不要补写、不要评价文笔
- 输出 150 到 350 字，用一段话，不要分点、不要小标题
- 只输出 JSON：{"summary": "……"}"""

_BOOK_PROMPT = """你是长篇小说的结构分析员。下面是一部长篇小说各分段的段落梗概，请据此产出全书大纲。

要求：
- 只依据给出的分段梗概，不要推测未写明的情节，不要评价文笔
- 严格按 JSON 输出，不要任何前后说明，不要用代码围栏
- 所有描述用中文

字段：
{
  "logline": "一句话讲清这本书讲了什么（不超过 60 字）",
  "premise": "开局设定与主角处境（不超过 150 字）",
  "structure": [{"part": "部分名（如「第一段 第1-163章」）", "gist": "这一段发生了什么（不超过 150 字）", "turn": "这一段最关键的一次转折（不超过 40 字）"}],
  "main_threads": [{"thread": "主线/支线名（不超过 12 字）", "gist": "走向（不超过 60 字）"}],
  "key_turns": ["全书最重要的转折，按顺序，每条不超过 30 字"],
  "ending": "结局状态（不超过 120 字）",
  "confidence": "高|中|低",
  "uncertain_fields": ["依据不足的字段名"]
}"""


@dataclass
class OutlineOptions:
    block_size: int = DEFAULT_BLOCK_SIZE
    temperature: float = 0.3
    thinking: str = "disabled"
    max_tokens: int = DEFAULT_MAX_TOKENS
    """单次输出的 token 预算起点。

    原来写死 1200：书级归约要一口气写完 logline + premise + 全书结构与主线，
    正文还没写完就被砍，报出来却是「返回内容不是 JSON」。
    """

    max_tokens_cap: int = MAX_TOKENS_CAP
    """预算升级的上限。撞上截断时逐次翻倍，但不超过它。"""

    max_attempts: int = 3
    timeout_sec: float = 60.0


@dataclass
class OutlinePlan:
    work: str
    provider_id: str
    model_id: str
    total_chapters: int
    summarized: int
    missing: int
    blocks: list[list[ChapterTask]]
    est_input_tokens: int
    est_output_tokens: int
    est_cost_cny: float | None
    price_note: str
    block_size: int
    missing_reasons: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work": self.work,
            "provider": self.provider_id,
            "model": self.model_id,
            "total_chapters": self.total_chapters,
            "summarized": self.summarized,
            "missing": self.missing,
            "missing_reasons": self.missing_reasons,
            "blocks": len(self.blocks),
            "block_size": self.block_size,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "est_cost_cny": self.est_cost_cny,
            "price_note": self.price_note,
            "warnings": self.warnings,
            "ready": self.missing == 0 and bool(self.blocks),
        }


@dataclass
class OutlineResult:
    blocks: list[dict[str, Any]] = field(default_factory=list)
    outline: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OUTLINE_SCHEMA_VERSION,
            "blocks": self.blocks,
            "outline": self.outline,
            "errors": self.errors,
            "usage": self.usage,
        }


# ── 采集 ──────────────────────────────────────────────────


MISSING_NO_ANNOTATION = "未跑标注"
MISSING_FAILED = "标注失败"
MISSING_SUMMARY_LOST = "梗概缺失"

# 缺梗概有三种截然不同的成因，处理方式也不一样，不能混成一句「没跑过标注或标注失败」：
#   · 未跑标注 → 去跑标注
#   · 标注失败 → 先看失败原因，再决定重跑
#   · 梗概缺失 → 标注其实跑成功了，只是 chapter_summary 这个字段没了
#                （模型没写，或事后被别的工具覆盖），重跑这一章就能补回
_MISSING_HINTS: dict[str, str] = {
    MISSING_NO_ANNOTATION: "这一章还没有标注文件，跑标注就有",
    MISSING_FAILED: "这一章的标注调用没能拿到可用结果，先看批跑面板的失败原因",
    MISSING_SUMMARY_LOST: "标注跑成功了，但梗概字段是空的，重跑这一章可补回",
}

# 判断「这一章的标注到底成没成」时抽查的模型轨字段。
_MODEL_PROBE_KEYS = (
    "perspective",
    "scene_switches",
    "time_span",
    "hook_strength",
    "hook_type",
    "emotion",
    "conflict",
    "info_release",
    "mainline_progress",
    "subplot_count",
)
# 抽查字段里填了这么多个，就认为模型当时确实给出了结果。
_MODEL_PROBE_THRESHOLD = 3


def _probe_filled(fields: dict) -> int:
    return sum(1 for key in _MODEL_PROBE_KEYS if fields.get(key) not in (None, "", []))


def collect_summaries(
    tasks: list[ChapterTask], annotations_dir: Path
) -> tuple[list[tuple[ChapterTask, str]], list[tuple[ChapterTask, str]]]:
    """按章序取出逐章梗概。返回 (有梗概的, 缺梗概的)。

    缺梗概的章**不会**被静默跳过——调用方要明确报出缺了多少、
    以及这会让大纲的可信度打折扣。

    缺梗概的每一项都带**原因**：同样是缺，成因不同、补救办法也不同，
    只报一个总数等于让人猜。
    """
    have: list[tuple[ChapterTask, str]] = []
    missing: list[tuple[ChapterTask, str]] = []
    for task in tasks:
        record = load_annotation(Path(annotations_dir) / f"{task.chapter_id}.json")
        if not record:
            missing.append((task, MISSING_NO_ANNOTATION))
            continue

        fields = record.get("fields") or {}
        summary = fields.get("chapter_summary")
        errors = record.get("errors") or []
        usable = isinstance(summary, str) and bool(summary.strip())

        if errors:
            # 记了错误不等于这一章没跑成：别的模型字段都填上了，说明结果是好的，
            # 只是梗概这一个字段后来没了（实测被演示模式整批覆盖过一次）。
            # 只有模型字段也一起空着，才是真的没拿到结果。
            if usable or _probe_filled(fields) < _MODEL_PROBE_THRESHOLD:
                missing.append((task, MISSING_FAILED))
            else:
                missing.append((task, MISSING_SUMMARY_LOST))
            continue

        if usable:
            have.append((task, summary.strip()))
        else:
            missing.append((task, MISSING_SUMMARY_LOST))
    return have, missing


def summarize_missing(missing: list[tuple[ChapterTask, str]]) -> dict[str, int]:
    """把缺梗概的章按原因归类计数。"""
    out: dict[str, int] = {}
    for _task, reason in missing:
        out[reason] = out.get(reason, 0) + 1
    return out


def missing_detail(missing_reasons: dict[str, int]) -> list[dict[str, Any]]:
    """缺梗概按原因摊开，带上「该怎么办」。

    给界面用：只报「缺 500 章」，三种成因混在一起，看的人只能猜。
    """
    return [
        {"reason": reason, "count": count, "hint": _MISSING_HINTS.get(reason, "")}
        for reason, count in sorted(missing_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def build_outline_plan(
    *,
    work_name: str,
    provider_id: str,
    model_id: str,
    tasks: list[ChapterTask],
    annotations_dir: Path,
    block_size: int = DEFAULT_BLOCK_SIZE,
    price_input_per_mtok: float | None = None,
    price_output_per_mtok: float | None = None,
    bucket: str | None = None,
    max_output_tokens: int = DEFAULT_MAX_TOKENS,
) -> OutlinePlan:
    """算出这次要跑几块、大概花多少。**不发起任何调用。**

    `max_output_tokens` 只用来估费，默认值要和实际用的输出预算一致，
    不然「预估费用」会按一个早就被证明不够用的数字报给你。
    """
    have, missing = collect_summaries(tasks, annotations_dir)
    warnings: list[str] = []

    if not have:
        warnings.append(
            "还没有任何逐章梗概。大纲的原料是标注顺带产出的 chapter_summary（P22），"
            "所以要先跑标注。"
        )
    elif missing:
        reasons = summarize_missing(missing)
        detail = "、".join(
            f"{reason} {count} 章" for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])
        )
        warnings.append(
            f"有 {len(missing)} 章缺梗概（{detail}），大纲会缺这一段的内容。"
            "缺的章不会被猜补——按下面的原因分别处理后会更完整。"
        )

    size = max(1, block_size)
    blocks: list[list[ChapterTask]] = []
    for start in range(0, len(have), size):
        blocks.append([task for task, _summary in have[start : start + size]])

    # 逐项算，别拍一个总数——出问题时才知道该改哪一项。
    coeff = 0.968  # 中文 token 系数，与其他模块用同一个实测值
    summary_chars = sum(len(s) for _t, s in have)
    est_in = int(summary_chars * coeff)                       # 分块归约的输入：逐章梗概
    est_in += int(len(_BLOCK_PROMPT) * coeff * len(blocks))   # 每块的提示词
    est_in += int(350 * coeff * len(blocks))                  # 全书归约的输入：各块梗概
    est_in += int(len(_BOOK_PROMPT) * coeff)
    est_out = int(350 * coeff * len(blocks)) + int(max_output_tokens * coeff)

    cost = None
    note = "未填写单价，费用无法计算。"
    if price_input_per_mtok is not None and price_output_per_mtok is not None:
        cost = round(
            est_in / 1_000_000 * price_input_per_mtok
            + est_out / 1_000_000 * price_output_per_mtok,
            4,
        )
        bucket_text = {"peak": "高峰", "offpeak": "空闲"}.get(bucket or "", "不分时段")
        note = f"按{bucket_text}单价估算：输入 {price_input_per_mtok} 元/百万、输出 {price_output_per_mtok} 元/百万。"

    return OutlinePlan(
        work=work_name,
        provider_id=provider_id,
        model_id=model_id,
        total_chapters=len(tasks),
        summarized=len(have),
        missing=len(missing),
        missing_reasons=summarize_missing(missing),
        blocks=blocks,
        est_input_tokens=est_in,
        est_output_tokens=est_out,
        est_cost_cny=cost,
        price_note=note,
        block_size=size,
        warnings=warnings,
    )


# ── 归约 ──────────────────────────────────────────────────


def _block_text(task_range: list[ChapterTask], summaries: dict[str, str]) -> str:
    lines: list[str] = []
    for task in task_range:
        label = f"第{task.chapter_no}章" if task.chapter_no is not None else task.chapter_id
        title = f" {task.title}" if task.title else ""
        lines.append(f"{label}{title}：{summaries.get(task.chapter_id, '（缺）')}")
    return "\n".join(lines)


def _block_label(task_range: list[ChapterTask]) -> str:
    """分块的标签。

    章号在分段重启时会回落（番外/第二部重新从第 1 章编号），
    这时用「第 A-B 章」会写出「第 151-3 章」这种笑不出来也看不懂的东西，
    甚至退化成「第 1 章」——一个跨了 50 章的分块被标成单独一章。
    所以要判一下单调性，不单调就退回用章节 id 标识。
    """
    if not task_range:
        return ""
    first, last = task_range[0], task_range[-1]
    a, b = first.chapter_no, last.chapter_no
    if a is not None and b is not None and b > a:
        return f"第 {a}-{b} 章"
    if a is not None and b is not None and a == b and len(task_range) == 1:
        return f"第 {a} 章"
    return f"{first.chapter_id} → {last.chapter_id}"


@dataclass
class OutlineCall:
    """一次（可能重试多次的）调用结果。

    `salvaged` 必须单独带出来：抢救出来的大纲比什么都没有强，但它**不是完整的**，
    调用方要据此显式标注「有字段可能缺失」，不能当成一次正常成功。
    """

    payload: dict[str, Any] | None = None
    error: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    salvaged: bool = False
    truncated: bool = False
    max_tokens: int = 0

    @property
    def notice(self) -> str:
        if not self.salvaged:
            return ""
        return (
            f"输出撞上 max_tokens={self.max_tokens} 被截断，"
            "只抢救出截断前的字段，后面的内容可能缺失。"
        )


def _call(
    client: OpenAICompatProvider,
    model_id: str,
    system: str,
    user: str,
    opts: OutlineOptions,
) -> OutlineCall:
    """一次调用 + 重试。撞上截断就加大输出预算重试，实在不行从截断处抢救。

    旧版把「被截断」和「返回的不是 JSON」混成一个错误：写死 1200 token 的输出，
    被砍断后原样重试三次（每次都在同一处被砍），最后报出来的是「返回内容不是 JSON」——
    与真实病根毫无关系，还会让人以为是模型不守格式。
    """
    extra = build_thinking_extra(opts.thinking, None)
    budget = max(1, opts.max_tokens)
    cap = max(budget, opts.max_tokens_cap)
    attempts = max(1, opts.max_attempts)
    last_error = ""
    usage_total: dict[str, int] = {}
    truncated = False
    salvaged_payload: dict[str, Any] | None = None
    # 部分中转站不认 response_format=json_object，撞上 BAD_REQUEST 就降级为
    # 提示词约束 + 解析（extract_json_object 仍能抠出 JSON）。
    json_mode = True
    for attempt in range(1, attempts + 1):
        try:
            result: ChatResult = client.chat(
                model_id,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=budget,
                temperature=opts.temperature,
                response_format={"type": "json_object"} if json_mode else None,
                extra=extra,
            )
        except ApiError as exc:
            last_error = f"{exc.kind.value}：{exc.safe_body(client.secrets)}"
            if json_mode and exc.kind in (ErrorKind.BAD_REQUEST, ErrorKind.RESPONSE_UNPARSABLE):
                json_mode = False
                continue
            if exc.kind == ErrorKind.RATE_LIMITED:
                time.sleep(min(2.0 * attempt, 15.0))
                continue
            continue
        if result.usage:
            for key, value in result.usage.to_dict().items():
                if isinstance(value, int):
                    usage_total[key] = usage_total.get(key, 0) + value
        payload = extract_json_object(result.text)
        if payload is not None:
            return OutlineCall(payload=payload, usage=usage_total, max_tokens=budget)

        if result.truncated or looks_truncated(result.text):
            truncated = True
            repaired = repair_truncated_json(result.text)
            if repaired is not None:
                salvaged_payload = repaired
            if budget < cap:
                # 加大预算重试：整本书的结构本来就写不下，原样重试只会在同一处再被砍
                budget = min(cap, budget * 2)
                last_error = f"输出被 max_tokens 截断，已把上限提高到 {budget} 重试"
                continue
            # 预算已经加不动了。同一个预算再试一次只会在同一个位置再被砍一次，
            # 旧版就是这样把几次调用全烧在同一个截断点上的，到这里就该停手。
            last_error = f"输出撞上 max_tokens={budget} 被截断，且截断处没有可用的完整字段"
            break
        last_error = f"返回内容不是 JSON：{result.text[:120]}"

    if salvaged_payload is not None:
        return OutlineCall(
            payload=salvaged_payload,
            usage=usage_total,
            salvaged=True,
            truncated=True,
            max_tokens=budget,
        )
    return OutlineCall(
        error=last_error, usage=usage_total, truncated=truncated, max_tokens=budget
    )


def generate_outline(
    *,
    plan: OutlinePlan,
    client: OpenAICompatProvider,
    annotations_dir: Path,
    opts: OutlineOptions | None = None,
    secrets: list[str] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    state_path: Path | None = None,
) -> OutlineResult:
    """跑一遍大纲生成。分块归约 + 全书归约，中途落盘以便续跑。"""
    opts = opts or OutlineOptions(block_size=plan.block_size)
    have, _missing = collect_summaries(
        [t for block in plan.blocks for t in block], annotations_dir
    )
    summaries = {task.chapter_id: text for task, text in have}

    result = OutlineResult()

    # 断点续跑：已经算好的块直接用
    done_blocks: dict[str, dict[str, Any]] = {}
    if state_path and Path(state_path).exists():
        try:
            cached = json.loads(Path(state_path).read_text(encoding="utf-8-sig"))
            for item in cached.get("blocks") or []:
                if isinstance(item, dict) and item.get("range"):
                    done_blocks[str(item["range"])] = item
        except (ValueError, OSError):
            done_blocks = {}

    total = len(plan.blocks)
    for index, block in enumerate(plan.blocks, start=1):
        label = _block_label(block)
        if on_progress:
            on_progress(index, total, label)
        if label in done_blocks:
            result.blocks.append(done_blocks[label])
            continue

        call = _call(
            client,
            plan.model_id,
            _BLOCK_PROMPT,
            f"这一批是 {label}：\n\n{_block_text(block, summaries)}",
            opts,
        )
        for key, value in call.usage.items():
            result.usage[key] = result.usage.get(key, 0) + value

        payload, error = call.payload, call.error
        if payload is None or not str(payload.get("summary") or "").strip():
            # 失败不落盘，重跑时会重试；也不伪造一个空块把缺口藏起来。
            result.errors.append(f"{label}：{error or '返回里没有 summary 字段'}")
            item = {"range": label, "summary": None, "chapters": [_chapter_ref(t) for t in block],
                    "error": error or "返回里没有 summary 字段"}
        else:
            item = {
                "range": label,
                "summary": str(payload["summary"]).strip(),
                "chapters": [_chapter_ref(t) for t in block],
            }
            if call.salvaged:
                # 抢救出来的段梗概比什么都没有强，但它比别的段短一截。
                # 标出来是为了别把它当成「这一段本来就这么简略」。
                item["salvaged"] = True
                result.errors.append(f"{label}：{call.notice}")
        result.blocks.append(item)
        _save_state(state_path, result, secrets)

    usable = [b for b in result.blocks if b.get("summary")]
    if not usable:
        result.errors.append("没有一块成功，无法汇总全书大纲")
        _save_state(state_path, result, secrets)
        return result

    body = "\n\n".join(f"【{b['range']}】\n{b['summary']}" for b in usable)
    call = _call(
        client,
        plan.model_id,
        _BOOK_PROMPT,
        f"《{plan.work}》共 {plan.total_chapters} 章，分为 {len(usable)} 段。各段梗概如下：\n\n{body}",
        opts,
    )
    for key, value in call.usage.items():
        result.usage[key] = result.usage.get(key, 0) + value

    if call.payload is None:
        result.errors.append(f"全书归约失败：{call.error}")
    else:
        payload = call.payload
        payload["_meta"] = {
            "chapters": plan.total_chapters,
            "summarized": plan.summarized,
            "missing": plan.missing,
            "missing_reasons": plan.missing_reasons,
            "missing_detail": missing_detail(plan.missing_reasons),
            "blocks_used": len(usable),
            "blocks_failed": len(result.blocks) - len(usable),
            "salvaged": call.salvaged,
            "model": plan.model_id,
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        if call.salvaged:
            # 有总比没有强，但不能让这份残缺的大纲冒充完整的。
            result.errors.append(f"全书归约：{call.notice}")
        result.outline = payload

    _save_state(state_path, result, secrets)
    return result


def _chapter_ref(task: ChapterTask) -> dict[str, Any]:
    return {"id": task.chapter_id, "chapter_no": task.chapter_no, "title": task.title}


def _save_state(state_path: Path | None, result: OutlineResult, secrets: list[str] | None) -> None:
    if not state_path:
        return
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        redact(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), secrets),
        encoding="utf-8",
    )


# ── 渲染 ──────────────────────────────────────────────────


def render_markdown(work: str, result: OutlineResult, plan: OutlinePlan | None = None) -> str:
    lines: list[str] = [f"# 《{work}》大纲", ""]
    outline = result.outline

    if outline:
        meta = outline.get("_meta") or {}
        lines.append(f"按 {meta.get('blocks_used', '?')} 段梗概归约，覆盖 "
                     f"{meta.get('summarized', '?')}/{meta.get('chapters', '?')} 章"
                     f"（缺 {meta.get('missing', '?')} 章）。")
        if meta.get("blocks_failed"):
            lines.append(f"**有 {meta['blocks_failed']} 段归约失败，这些段的内容不在下面的大纲里。**")
        if meta.get("salvaged"):
            lines.append("**这份大纲是从被截断的输出里抢救出来的，越靠后的字段越可能缺失。**")
        lines.append("")

        if outline.get("logline"):
            lines.append(f"**一句话**　{outline['logline']}")
            lines.append("")
        if outline.get("premise"):
            lines.append("## 开局设定")
            lines.append(str(outline["premise"]))
            lines.append("")

        structure = outline.get("structure") or []
        if structure:
            lines.append("## 分段结构")
            for item in structure:
                if not isinstance(item, dict):
                    continue
                lines.append(f"### {item.get('part') or '（未命名）'}")
                if item.get("gist"):
                    lines.append(str(item["gist"]))
                if item.get("turn"):
                    lines.append("")
                    lines.append(f"**关键转折**　{item['turn']}")
                lines.append("")

        threads = outline.get("main_threads") or []
        if threads:
            lines.append("## 线索")
            lines.append("| 线索 | 走向 |")
            lines.append("|---|---|")
            for item in threads:
                if isinstance(item, dict):
                    lines.append(f"| {item.get('thread') or ''} | {item.get('gist') or ''} |")
            lines.append("")

        turns = outline.get("key_turns") or []
        if turns:
            lines.append("## 关键转折（按顺序）")
            for index, item in enumerate(turns, start=1):
                lines.append(f"{index}. {item}")
            lines.append("")

        if outline.get("ending"):
            lines.append("## 结局")
            lines.append(str(outline["ending"]))
            lines.append("")

        if outline.get("confidence"):
            lines.append(f"> 模型自评置信度：{outline['confidence']}")
            if outline.get("uncertain_fields"):
                lines.append(f"> 依据不足的字段：{'、'.join(str(x) for x in outline['uncertain_fields'])}")
            lines.append("")

    if result.errors:
        lines.append("## 未完成的部分")
        for item in result.errors:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("## 逐段梗概")
    for block in result.blocks:
        lines.append(f"### {block.get('range')}")
        if block.get("summary"):
            lines.append(str(block["summary"]))
            if block.get("salvaged"):
                lines.append("")
                lines.append("（这一段是被截断的输出里抢救出来的，可能不完整）")
        else:
            lines.append(f"（这一段归约失败：{block.get('error') or '未完成'}）")
        lines.append("")

    return "\n".join(lines)


def save_outline(work_dir: Path, result: OutlineResult, plan: OutlinePlan, *, stamp: str | None = None) -> dict[str, Path]:
    out_dir = Path(work_dir) / "40-outline"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%dT%H%M%S")
    json_path = out_dir / f"outline-{stamp}.json"
    md_path = out_dir / f"outline-{stamp}.md"
    payload = result.to_dict()
    payload["plan"] = plan.to_dict()
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(plan.work, result, plan), encoding="utf-8")
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"json": json_path, "markdown": md_path, "latest_json": latest_json, "latest_md": latest_md}


__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "OutlineOptions",
    "OutlinePlan",
    "OutlineResult",
    "build_outline_plan",
    "collect_summaries",
    "generate_outline",
    "render_markdown",
    "save_outline",
]
