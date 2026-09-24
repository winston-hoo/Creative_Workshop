"""批量标注调度。

单章链路跑通之后，批量不是重写，而是加四件事：
筛选待跑章节、估算成本、控并发、能续跑。

## 一个绕不开的矛盾：并发与台账的顺序依赖

伏笔台账和「上一章摘要」都要求**第 i-1 章的结果在第 i 章发起调用之前就已经确定**：

- 章 i 的提示词里带着未回收伏笔清单，它必须包含章 i-1 刚埋下的编号，
  否则章 i 只能把同一个伏笔再登记一次；
- 章 i 的上一章摘要来自章 i-1 的标注结果。

所以这两件事天然要求串行。硬上并发的后果是**台账上下文滞后 N-1 章**，
表现为「相邻章的伏笔推进/回收被漏掉」——这是一类安静的质量退化：
一切看起来都在正常跑，但伏笔链断了。

处理方式不是偷偷并发，而是把选择权显式交出去：

| 并发 | 表现 |
|---|---|
| **1（默认，启用台账时）** | 台账与上一章摘要严格有序，结果可复现 |
| >1 | 快 N 倍，但台账上下文滞后，重复登记风险上升 → 必须显式 `--concurrency` 并接受警告 |

## 三条纪律

1. **失败不落盘**。失败章写进运行记录，重跑时自然会被重试；
   若把失败记录也落盘，幂等判定会误以为「已有标注」而永久跳过它——
   那正是本项目反复栽跟头的静默失效。
2. **超预算中止**，不静默继续。估算只是估算，真正拦闸的是实测 usage 累计。
3. **成本必须能展示计算过程**，没有单价就明说算不出，绝不给假数字。
"""

from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .annotate import (
    AnnotateOptions,
    AnnotateResult,
    annotate_chapter,
    is_up_to_date,
    load_annotation,
    save_annotation,
    strip_front_matter,
    text_sha256,
)
from .config import ProviderConfig, WorkshopConfig, resolve_default_model
from .errors import hint_for, is_fatal
from .ledger import Ledger
from .llm import OpenAICompatProvider
from .primitives import Primitives, WorkConfig
from .prompts import build_stable_prefix
from .secrets import redact

BJT = timezone(timedelta(hours=8))
STATE_VERSION = "batch-state-v1"

# 估算里无法从实测得出的那些开销，只能作为**声明过的假设**参与计算。
# 它们不精确，但比「假装没有」诚实：每一项都写进 breakdown 让人核对。
ASSUMED_PREV_SUMMARY_TOKENS = 200
ASSUMED_LEDGER_CTX_TOKENS = 150
# 单章输出 token 的默认假设。原来是 500（拍脑袋），2026-09-23 用 20 章真实样本
# 实测下来只有 263，于是估算整体高了约三分之一。现在按实测取整到 300；
# 若配置里有 measured_batch.avg_output_tokens，以那个为准。
DEFAULT_EST_OUTPUT_TOKENS = 300
FALLBACK_TOKENS_PER_CJK_CHAR = 1.0

_WEEKDAY_CODE = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


# ── 数据形状 ──────────────────────────────────────────────


@dataclass
class ChapterTask:
    index: int
    chapter_id: str
    vol_no: int
    chapter_no: int | None
    title: str
    path: Path
    char_count: int
    # 送进提示词的正文长度（**含空白与缩进**）。
    # manifest 里的 char_count 是非空白字符数，而真正发出去的是带缩进的原文，
    # 两者差约 24%——估算输入 token 必须用这个，不然会系统性偏低。
    # 0 表示还没读过文件（例如只出计划时按 1.24 倍折算）。
    prompt_chars: int = 0

    @property
    def body_chars_for_prompt(self) -> int:
        if self.prompt_chars:
            return self.prompt_chars
        return int(self.char_count * 1.24)


@dataclass
class Estimate:
    """成本与耗时预估。**每一项都要能追到来源。**"""

    chapters: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tokens_per_cjk_char: float
    coefficient_basis: str  # measured | provider_measured | fallback
    prefix_tokens: int
    chapter_tokens: int
    overhead_tokens: int
    est_seconds: float | None
    price_bucket: str | None  # peak | offpeak | None（无定价）
    cost_cny: float | None
    price_note: str
    breakdown: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapters": self.chapters,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_cjk_char": self.tokens_per_cjk_char,
            "coefficient_basis": self.coefficient_basis,
            "prefix_tokens": self.prefix_tokens,
            "chapter_tokens": self.chapter_tokens,
            "overhead_tokens": self.overhead_tokens,
            "est_seconds": self.est_seconds,
            "price_bucket": self.price_bucket,
            "cost_cny": self.cost_cny,
            "price_note": self.price_note,
            "breakdown": self.breakdown,
        }


@dataclass
class BudgetGate:
    mode: str = "token"  # token | cost
    token_limit: int | None = None
    cost_limit_cny: float | None = None
    on_exceed: str = "abort"


@dataclass
class BatchPlan:
    work: str
    provider_id: str
    model_id: str
    total_chapters: int
    pending: list[ChapterTask]
    skipped: int
    estimate: Estimate
    budget: BudgetGate
    over_budget: bool
    concurrency: int
    warnings: list[str] = field(default_factory=list)
    ledger_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "work": self.work,
            "provider": self.provider_id,
            "model": self.model_id,
            "total_chapters": self.total_chapters,
            "pending": len(self.pending),
            "skipped": self.skipped,
            "concurrency": self.concurrency,
            "ledger_enabled": self.ledger_enabled,
            "estimate": self.estimate.to_dict(),
            "budget": {
                "mode": self.budget.mode,
                "token_limit": self.budget.token_limit,
                "cost_limit_cny": self.budget.cost_limit_cny,
                "on_exceed": self.budget.on_exceed,
            },
            "over_budget": self.over_budget,
            "warnings": self.warnings,
        }


@dataclass
class BatchState:
    """一次批量任务的运行态。落盘成状态文件，供断点续跑与界面轮询。"""

    run_id: str
    work: str
    status: str = "running"  # running | done | aborted | failed
    started_at: str = ""
    finished_at: str = ""
    total: int = 0
    committed: int = 0
    ok: int = 0
    needs_review: int = 0
    failed: int = 0
    skipped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_cny: float | None = None
    concurrency: int = 1
    current_chapter: str = ""
    recent: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    ledger_gaps: list[str] = field(default_factory=list)
    message: str = ""
    limit_note: str = ""
    # 按错误类别汇总。界面上的「运行记录」直接读它——
    # 只报「失败 20」而说不出为什么，等于让人猜。
    error_kinds: dict[str, int] = field(default_factory=dict)
    aborted_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        elapsed = None
        if self.started_at:
            try:
                start = datetime.fromisoformat(self.started_at)
                end = datetime.fromisoformat(self.finished_at) if self.finished_at else datetime.now().astimezone()
                elapsed = round((end - start).total_seconds(), 1)
            except ValueError:
                elapsed = None
        done = self.committed
        eta_sec = None
        if elapsed and done > 0 and self.status == "running":
            eta_sec = round((self.total - done) * (elapsed / done), 1)
        return {
            "schema_version": STATE_VERSION,
            "run_id": self.run_id,
            "work": self.work,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": elapsed,
            "eta_sec": eta_sec,
            "total": self.total,
            "committed": done,
            "ok": self.ok,
            "needs_review": self.needs_review,
            "failed": self.failed,
            "skipped": self.skipped,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_cny": self.cost_cny,
            "concurrency": self.concurrency,
            "current_chapter": self.current_chapter,
            "recent": self.recent,
            "failures": self.failures,
            "ledger_gaps": self.ledger_gaps,
            "message": self.message,
            "limit_note": self.limit_note,
            "error_kinds": self.error_kinds,
            "aborted_reason": self.aborted_reason,
            "error_hints": [
                {"kind": kind, "hint": hint_for(kind), "count": count, "fatal": is_fatal(kind)}
                for kind, count in sorted(self.error_kinds.items(), key=lambda kv: -kv[1])
            ],
        }


# ── 章节筛选 ──────────────────────────────────────────────


def load_chapter_tasks(ingest_dir: Path) -> list[ChapterTask]:
    """从 manifest 还原待处理章节的顺序与路径。

    顺序用 manifest 的顺序，不用文件名字典序——虽然命名规则保证了两者一致，
    但真相源是 manifest。
    """
    manifest_path = Path(ingest_dir) / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return []

    chapters_dir = Path(ingest_dir) / "chapters"
    tasks: list[ChapterTask] = []
    for index, item in enumerate(manifest.get("chapters") or []):
        chapter_id = str(item.get("id") or "")
        if not chapter_id:
            continue
        tasks.append(
            ChapterTask(
                index=index,
                chapter_id=chapter_id,
                vol_no=int(item.get("vol_no") or 1),
                chapter_no=item.get("chapter_no"),
                title=str(item.get("title") or ""),
                path=chapters_dir / f"{chapter_id}.md",
                char_count=int(item.get("char_count") or 0),
            )
        )
    return tasks


def _needs_rerun(existing: dict[str, Any] | None) -> bool:
    """已有标注是否该重跑。

    幂等判定不能只看哈希。上一次跑失败的章如果也被落盘，
    哈希是对得上的，于是会被永久跳过——失败就此静默固化。
    """
    if not existing:
        return True
    if existing.get("errors"):
        return True
    prov = existing.get("provenance") or {}
    filled = prov.get("model_fields_filled")
    total = prov.get("model_fields_total")
    if isinstance(filled, int) and isinstance(total, int) and total > 0 and filled == 0:
        return True
    return False


def split_pending(
    tasks: list[ChapterTask], annotations_dir: Path
) -> tuple[list[ChapterTask], list[ChapterTask]]:
    """把章节分成「要跑的」和「已经跑过不用再跑的」。"""
    pending: list[ChapterTask] = []
    done: list[ChapterTask] = []
    for task in tasks:
        if not task.path.exists():
            # 章节文件缺失是数据问题，不能静默跳过
            pending.append(task)
            continue
        try:
            raw = task.path.read_text(encoding="utf-8")
        except OSError:
            pending.append(task)
            continue
        _meta, body = strip_front_matter(raw)
        # 顺手记下真正的提示词正文长度。文件本来就读了，不多花成本，
        # 而这一步能让输入 token 估算从「偏低 24%」变成接近实测。
        task.prompt_chars = len(body)
        target = Path(annotations_dir) / f"{task.chapter_id}.json"
        if is_up_to_date(target, text_sha256(body)) and not _needs_rerun(load_annotation(target)):
            done.append(task)
        else:
            pending.append(task)
    return pending, done


# ── 估算 ──────────────────────────────────────────────────


def resolve_token_coefficient(
    model_cfg: dict[str, Any], provider: ProviderConfig
) -> tuple[float, str]:
    """中文 token 换算系数。只能用实测值，没有就明说是兜底值。

    0.7 这类通行粗估在这里不能用：实测 DeepSeek 是 0.968，
    差 38%，成本与预算会整体算错。
    """
    measured = (model_cfg.get("measured") or {}).get("tokens_per_cjk_char")
    if measured:
        return float(measured), "measured"
    probe = (provider.raw.get("probe_results") or {}).get("tokens_per_cjk_char")
    if probe:
        return float(probe), "provider_measured"
    return FALLBACK_TOKENS_PER_CJK_CHAR, "fallback"


def price_bucket(provider: ProviderConfig, now: datetime | None = None) -> str | None:
    """当前处于高峰还是空闲。峰谷不只影响价格，也影响该不该现在跑。"""
    pricing = provider.pricing
    tod = pricing.get("time_of_day") or {}
    if not tod.get("enabled"):
        return None
    moment = now or datetime.now(BJT)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BJT)
    moment = moment.astimezone(BJT)

    peak_days = [str(d).upper() for d in (tod.get("peak_days_bjt") or [])]
    if peak_days and _WEEKDAY_CODE[moment.weekday()] not in peak_days:
        return "offpeak"

    minutes = moment.hour * 60 + moment.minute
    for span in tod.get("peak_hours_bjt") or []:
        try:
            start_text, end_text = str(span).split("-")
            sh, sm = (int(x) for x in start_text.split(":"))
            eh, em = (int(x) for x in end_text.split(":"))
        except (ValueError, AttributeError):
            continue
        if sh * 60 + sm <= minutes < eh * 60 + em:
            return "peak"
    return "offpeak"


def _pick_price(model_cfg: dict[str, Any], bucket: str | None, side: str) -> float | None:
    """side: input | output。取不到就返回 None——没有单价就是没有，不猜。"""
    pricing = model_cfg.get("pricing") or {}
    if bucket:
        value = pricing.get(f"{side}_per_mtok_{bucket}")
        if value is not None:
            return float(value)
    for key in (f"{side}_per_mtok", f"{side}_per_mtok_offpeak", f"{side}_per_mtok_peak"):
        value = pricing.get(key)
        if value is not None:
            return float(value)
    return None


def estimate_run(
    *,
    provider: ProviderConfig,
    model_cfg: dict[str, Any],
    tasks: list[ChapterTask],
    prefix_chars: int,
    tokens_per_cjk_char: float,
    coefficient_basis: str,
    est_output_tokens: int | None = None,
    with_prev_summary: bool = True,
    with_ledger: bool = True,
    now: datetime | None = None,
) -> Estimate:
    """估算一次批量标注的 token 与费用。

    展示计算过程是硬性要求：章数、系数、单价逐项列出，
    让人能自己复核，而不是给一个孤零零的数字。
    """
    batch_measured = model_cfg.get("measured_batch") or {}

    # 输出 token：批量实测值 > 调用方指定 > 默认假设
    output_from_measurement = False
    if est_output_tokens is None:
        if batch_measured.get("avg_output_tokens"):
            est_output_tokens = int(batch_measured["avg_output_tokens"])
            output_from_measurement = True
        else:
            est_output_tokens = DEFAULT_EST_OUTPUT_TOKENS

    coeff = tokens_per_cjk_char
    body_chars = sum(t.body_chars_for_prompt for t in tasks)
    per_chapter_overhead = (ASSUMED_PREV_SUMMARY_TOKENS if with_prev_summary else 0) + (
        ASSUMED_LEDGER_CTX_TOKENS if with_ledger else 0
    )
    overhead_tokens = per_chapter_overhead * len(tasks)

    # 输入侧有两条路，优先走**批量实测拟合**：
    #
    #   A. 有 measured_batch 时：prompt = 固定部分 + 正文长度 × 实测斜率。
    #      固定部分已经把「每章小开销」吸收进去了，所以不再叠加 overhead。
    #   B. 没有实测时：老办法，前缀与正文都按探测系数折算。
    #      探测系数来自合成文本，比真实散文高约 1.4 倍，所以这条是保守估计。
    fixed_tokens = batch_measured.get("prompt_fixed_tokens")
    slope = batch_measured.get("prompt_tokens_per_char")
    input_from_measurement = bool(fixed_tokens and slope)
    if input_from_measurement:
        input_tokens = int(len(tasks) * float(fixed_tokens) + body_chars * float(slope))
        prefix_tokens = int(fixed_tokens)
        chapter_tokens = int(body_chars * float(slope))
    else:
        prefix_tokens = int(math.ceil(prefix_chars * coeff))
        chapter_tokens = int(math.ceil(body_chars * coeff))
        input_tokens = (prefix_tokens + per_chapter_overhead) * len(tasks) + chapter_tokens

    output_tokens = est_output_tokens * len(tasks)
    total_tokens = input_tokens + output_tokens

    bucket = price_bucket(provider, now)
    in_price = _pick_price(model_cfg, bucket, "input")
    out_price = _pick_price(model_cfg, bucket, "output")

    cost: float | None = None
    note: str
    if in_price is None or out_price is None:
        note = "未填写单价，费用无法计算。系统只负责 token，价格永远由你填写。"
    else:
        cost = round(input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price, 4)
        bucket_text = {"peak": "高峰", "offpeak": "空闲"}.get(bucket or "", "不分时段")
        note = f"按{bucket_text}单价估算：输入 {in_price} 元/百万、输出 {out_price} 元/百万。"

    # 耗时：优先用批量实测的单章延迟，其次才是探测折算值。
    # 探测的 est_single_chapter_sec 是按假设的输出长度折算的，实测偏差能有 3 倍。
    probe_measured = model_cfg.get("measured") or {}
    est_seconds: float | None = None
    seconds_source = ""
    if batch_measured.get("avg_latency_ms"):
        est_seconds = round(float(batch_measured["avg_latency_ms"]) / 1000 * len(tasks), 1)
        seconds_source = "批量实测"
    elif probe_measured.get("est_single_chapter_sec"):
        est_seconds = round(float(probe_measured["est_single_chapter_sec"]) * len(tasks), 1)
        seconds_source = "探测折算（按假设的输出长度）"

    return Estimate(
        chapters=len(tasks),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        tokens_per_cjk_char=coeff,
        coefficient_basis=coefficient_basis,
        prefix_tokens=prefix_tokens,
        chapter_tokens=chapter_tokens,
        overhead_tokens=overhead_tokens,
        est_seconds=est_seconds,
        price_bucket=bucket,
        cost_cny=cost,
        price_note=note,
        breakdown={
            "章数": len(tasks),
            "每章固定部分 token": prefix_tokens
            + (0 if input_from_measurement else per_chapter_overhead),
            "每章正文 token": round(chapter_tokens / len(tasks)) if tasks else 0,
            "正文长度口径": "含空白与缩进的原始字符数"
            + ("（读文件实测）" if tasks and tasks[0].prompt_chars else "（按 1.24 倍折算）"),
            "附加开销（上一章摘要 + 伏笔清单）": 0
            if input_from_measurement
            else per_chapter_overhead,
            "输入侧依据": "批量实测拟合" if input_from_measurement else "探测系数折算（偏保守）",
            "中文换算系数": f"{coeff} tokens/字（{coefficient_basis}）"
            + ("　注：该系数由合成文本测得，对真实散文偏高" if not input_from_measurement else ""),
            "输出 token 假设": f"{est_output_tokens}/章"
            + ("（来自批量实测）" if output_from_measurement else "（经验假设，未实测）"),
            "单章耗时": f"{round(est_seconds / len(tasks), 2)} 秒（{seconds_source}）"
            if (est_seconds and tasks)
            else None,
            "输入单价": in_price,
            "输出单价": out_price,
            "计费时段": bucket or "服务商未启用峰谷",
        },
    )


def build_budget(cfg: WorkshopConfig) -> BudgetGate:
    settings = cfg.settings
    budget = settings.get("budget") or {}
    return BudgetGate(
        mode=str(settings.get("budget_gate_mode") or "token"),
        token_limit=budget.get("per_task_token_limit"),
        cost_limit_cny=budget.get("per_task_cost_limit"),
        on_exceed=str(budget.get("on_exceed") or "abort"),
    )


# ── 计划 ──────────────────────────────────────────────────


def build_plan(
    *,
    cfg: WorkshopConfig,
    provider: ProviderConfig,
    model_id: str,
    primitives: Primitives,
    work: WorkConfig,
    tasks: list[ChapterTask],
    annotations_dir: Path,
    concurrency: int = 1,
    ledger_enabled: bool = True,
    limit: int | None = None,
    only_ids: list[str] | None = None,
    force: bool = False,
    # 默认 None：让 estimate_run 去挑「批量实测值 > 默认假设」。
    # 这里若写成具体数字，会把实测值挡在外面——踩过这个坑。
    est_output_tokens: int | None = None,
) -> BatchPlan:
    """算出「这次要跑什么、大概花多少」。不做任何模型调用。

    `only_ids` —— 只跑勾选出来的这几章；`force` —— 连已标注的也重跑。
    force 的用处很实在：改了提示词或作品配置之后，旧标注的字段口径不同，
    混在一起比趋势会得出错误结论，所以得能选「重跑」。
    """
    pending, done = split_pending(tasks, annotations_dir)
    if only_ids:
        wanted = set(only_ids)
        candidate = [t for t in tasks if t.chapter_id in wanted]
        if force:
            pending = candidate
        else:
            done_ids = {t.chapter_id for t in done}
            pending = [t for t in candidate if t.chapter_id not in done_ids]
        pending_ids = {t.chapter_id for t in pending}
        done = [t for t in tasks if t.chapter_id not in pending_ids]
    elif force:
        pending = list(tasks)
        done = []

    if limit:
        pending = pending[:limit]

    warnings: list[str] = []
    if not work.core_motive:
        warnings.append(
            "作品配置里 core_motive 为空。P21「主角核心动机呈现强度」的度量对象未定义，"
            "模型会自己猜一个动机来打分，该列数据不可信。"
        )

    model_cfg = provider.model(model_id) or {}
    coeff, basis = resolve_token_coefficient(model_cfg, provider)
    if basis == "fallback":
        warnings.append(
            f"该模型没有实测的中文 token 换算系数，暂按 {FALLBACK_TOKENS_PER_CJK_CHAR} tokens/字 估算。"
            "实测值比通行粗估（0.7）高约 38%，建议先跑一次探测校准。"
        )

    prefix = build_stable_prefix(primitives, work)
    estimate = estimate_run(
        provider=provider,
        model_cfg=model_cfg,
        tasks=pending,
        prefix_chars=len(prefix),
        tokens_per_cjk_char=coeff,
        coefficient_basis=basis,
        est_output_tokens=est_output_tokens,
        with_ledger=ledger_enabled,
    )

    budget = build_budget(cfg)
    over = False
    if budget.mode == "token" and budget.token_limit and estimate.total_tokens > budget.token_limit:
        over = True
    if budget.mode == "cost" and budget.cost_limit_cny and (estimate.cost_cny or 0) > budget.cost_limit_cny:
        over = True

    if concurrency > 1 and ledger_enabled:
        warnings.append(
            f"并发 {concurrency} 会让台账上下文滞后最多 {concurrency - 1} 章："
            "章 i 调用时看不到章 i-1 刚埋下的伏笔，可能把同一个伏笔重复登记。"
            "要严格有序请用并发 1。"
        )

    return BatchPlan(
        work=work.name,
        provider_id=provider.id,
        model_id=model_id,
        total_chapters=len(tasks),
        pending=pending,
        skipped=len(done),
        estimate=estimate,
        budget=budget,
        over_budget=over,
        concurrency=max(1, concurrency),
        warnings=warnings,
        ledger_enabled=ledger_enabled,
    )


# ── 执行 ──────────────────────────────────────────────────


class BatchRunner:
    """批量执行器。

    并发窗口 + 按序提交：调用可以乱序完成，但**落盘与台账必须按章序**。
    这样即使开了并发，编号分配顺序也始终是章序，结果可复现。
    """

    def __init__(
        self,
        *,
        plan: BatchPlan,
        client: OpenAICompatProvider,
        primitives: Primitives,
        work: WorkConfig,
        opts: AnnotateOptions,
        annotations_dir: Path,
        ledger: Ledger | None = None,
        ledger_path: Path | None = None,
        state_path: Path | None = None,
        run_id: str = "",
        secrets: list[str] | None = None,
        pricing: dict[str, Any] | None = None,
        price_bucket_now: str | None = None,
        on_progress: Callable[[BatchState], None] | None = None,
        recent_size: int = 12,
    ) -> None:
        self.plan = plan
        self.client = client
        self.primitives = primitives
        self.work = work
        self.opts = opts
        self.annotations_dir = Path(annotations_dir)
        self.ledger = ledger
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.state_path = Path(state_path) if state_path else None
        self.secrets = list(secrets or [])
        self.pricing = dict(pricing or {})
        self.price_bucket_now = price_bucket_now
        self.on_progress = on_progress
        self.recent_size = recent_size
        self.state = BatchState(
            run_id=run_id or datetime.now().strftime("%Y%m%dT%H%M%S"),
            work=plan.work,
            total=len(plan.pending),
            skipped=plan.skipped,
            concurrency=plan.concurrency,
        )
        self._lock = threading.Lock()
        self._stop = False

    # ── 单章 ──────────────────────────────────────────────

    def _read_body(self, task: ChapterTask) -> tuple[dict[str, Any], str]:
        raw = task.path.read_text(encoding="utf-8")
        return strip_front_matter(raw)

    def _prev_summary(self, prev_task: ChapterTask | None) -> dict[str, Any] | None:
        """上一章摘要。取不到就是取不到——没有就传 None，不编一个。"""
        if prev_task is None:
            return None
        record = load_annotation(self.annotations_dir / f"{prev_task.chapter_id}.json")
        if not record:
            return None
        return record.get("fields") or None

    def _call_one(self, task: ChapterTask, prev_task: ChapterTask | None) -> AnnotateResult:
        _meta, body = self._read_body(task)
        # 台账上下文在**提交时刻**由主线程拍下快照。
        # 这样并发时也不会读到正在被改写的台账状态。
        ctx = None
        if self.ledger is not None:
            ctx = self.ledger.context_for_prompt(task.chapter_no)
        return annotate_chapter(
            client=self.client,
            primitives=self.primitives,
            work=self.work,
            chapter_id=task.chapter_id,
            chapter_no=task.chapter_no if task.chapter_no is not None else task.index + 1,
            vol_no=task.vol_no,
            title=task.title,
            text=body,
            provider_id=self.plan.provider_id,
            model_id=self.plan.model_id,
            opts=self.opts,
            source_file=str(task.path),
            prev_summary=self._prev_summary(prev_task),
            ledger=None,  # 台账在提交阶段按序处理，避免并发下状态错乱
            open_foreshadows=ctx,
            apply_ledger=False,
        )

    # ── 提交 ──────────────────────────────────────────────

    def _commit(self, task: ChapterTask, result: AnnotateResult) -> None:
        record = result.record
        usage = (record.get("provenance") or {}).get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)

        failed = bool(record.get("errors"))
        if failed:
            # 失败不落盘。落盘会让幂等判定误以为「已有标注」而永久跳过它。
            self.state.failed += 1
            for item in record.get("errors") or []:
                kind = str(item.get("kind") or "unknown")
                self.state.error_kinds[kind] = self.state.error_kinds.get(kind, 0) + 1
            self.state.failures.append(
                {
                    "chapter_id": task.chapter_id,
                    "chapter_no": task.chapter_no,
                    "title": task.title,
                    "errors": record.get("errors"),
                }
            )
            if self.ledger is not None:
                gap = f"{task.chapter_id} 标注失败，未并入伏笔台账（后续章节看不到本章的伏笔动作）"
                self.state.ledger_gaps.append(gap)
        else:
            if self.ledger is not None:
                from .annotate import apply_ledger_to_record

                apply_ledger_to_record(
                    record, self.ledger, chapter_id=task.chapter_id, chapter_no=task.chapter_no
                )
                record["provenance"]["ledger_enabled"] = True
            save_annotation(
                record,
                self.annotations_dir,
                secrets=self.secrets,
                archive_dir=self.annotations_dir / "_archive",
            )
            # 台账在标注落盘之后才存。顺序反了会留下「编号已分配但标注不存在」的幽灵条目。
            if self.ledger is not None and self.ledger_path is not None:
                self.ledger.save(self.ledger_path)
            if record.get("status") == "ok":
                self.state.ok += 1
            else:
                self.state.needs_review += 1

        self.state.committed += 1
        self.state.input_tokens += in_tok
        self.state.output_tokens += out_tok
        self.state.total_tokens += in_tok + out_tok
        self.state.recent.insert(
            0,
            {
                "chapter_id": task.chapter_id,
                "chapter_no": task.chapter_no,
                "title": task.title,
                "status": "failed" if failed else record.get("status"),
                "attempts": result.attempts,
                "latency_ms": (record.get("provenance") or {}).get("latency_ms"),
                "tokens": in_tok + out_tok,
            },
        )
        self.state.recent = self.state.recent[: self.recent_size]

    def _fatal_error(self) -> str:
        """出现了「重试也没用」的错误就返回它的说明，否则空串。"""
        for kind, count in self.state.error_kinds.items():
            if is_fatal(kind):
                return f"{kind}（{count} 次）— {hint_for(kind)}"
        return ""

    def _over_budget(self) -> bool:
        gate = self.plan.budget
        if gate.mode == "token" and gate.token_limit:
            return self.state.total_tokens > int(gate.token_limit)
        if gate.mode == "cost" and gate.cost_limit_cny and self.state.cost_cny:
            return self.state.cost_cny > float(gate.cost_limit_cny)
        return False

    def _snapshot(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2)
            self.state_path.write_text(redact(text, self.secrets), encoding="utf-8")
        if self.on_progress:
            self.on_progress(self.state)

    # ── 主循环 ────────────────────────────────────────────

    def run(self) -> BatchState:
        pending = self.plan.pending
        self.state.started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        if self.plan.estimate.cost_cny is not None:
            self.state.cost_cny = 0.0
        self._snapshot()

        if not pending:
            self.state.status = "done"
            self.state.message = "没有需要标注的章节（全部已有标注且原文未变）"
            self.state.finished_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            self._snapshot()
            return self.state

        window = max(1, self.plan.concurrency)
        futures: dict[int, Future] = {}
        submitted = 0
        commit = 0

        try:
            with ThreadPoolExecutor(max_workers=window) as pool:
                while commit < len(pending):
                    if self._stop:
                        break
                    # 填满窗口。窗口上界是 commit + window，
                    # 保证「已完成但还没轮到提交」的结果不会无限堆积。
                    while submitted < len(pending) and submitted < commit + window:
                        task = pending[submitted]
                        prev_task = pending[submitted - 1] if submitted > 0 else None
                        futures[submitted] = pool.submit(self._call_one, task, prev_task)
                        submitted += 1

                    task = pending[commit]
                    self.state.current_chapter = task.chapter_id
                    future = futures.pop(commit)
                    result = future.result()
                    self._commit(task, result)
                    commit += 1

                    if self.plan.estimate.cost_cny is not None:
                        self.state.cost_cny = self._accrued_cost()
                    self._snapshot()

                    fatal = self._fatal_error()
                    if fatal:
                        # 撞上配置类错误（密钥无效、端点写错、模型名错）就立刻停。
                        # 这类错误重试再多次也不会好——继续跑等于用同一个错误
                        # 把后面几百章挨个刷一遍，既浪费时间又把真正的原因埋进日志里。
                        self.state.status = "aborted"
                        self.state.aborted_reason = fatal
                        self.state.message = (
                            f"遇到无法通过重试解决的问题，已停止：{fatal}。"
                            f"（已完成 {self.state.committed}/{self.state.total} 章，修好后重跑会接着跑）"
                        )
                        break

                    if self._over_budget():
                        self.state.status = "aborted"
                        self.state.aborted_reason = "budget"
                        self.state.message = (
                            f"已达预算上限，任务中止（{self.plan.budget.mode} 模式）。"
                            f"已完成 {self.state.committed}/{self.state.total} 章，"
                            "再跑一次会接着跑剩下的。"
                        )
                        break
        except Exception as exc:  # noqa: BLE001
            self.state.status = "failed"
            self.state.message = f"批量任务异常中断：{type(exc).__name__}: {exc}"
            self._snapshot()
            return self.state

        if self.state.status == "running":
            self.state.status = "done"
            self.state.message = (
                f"完成 {self.state.committed}/{self.state.total} 章 · "
                f"正常 {self.state.ok} · 待复核 {self.state.needs_review} · 失败 {self.state.failed}"
            )
        if self._stop:
            self.state.status = "aborted"
            self.state.message = self.state.message or "已中止"
        self.state.current_chapter = ""
        self.state.finished_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self._snapshot()
        return self.state

    def stop(self) -> None:
        self._stop = True

    def _accrued_cost(self) -> float:
        """按实测 token 与配置单价折算已发生费用。没单价就返回 0 并由调用方不展示。"""
        model_like = {"pricing": self.pricing}
        in_price = _pick_price(model_like, self.price_bucket_now, "input")
        out_price = _pick_price(model_like, self.price_bucket_now, "output")
        if in_price is None or out_price is None:
            return 0.0
        return round(
            self.state.input_tokens / 1_000_000 * in_price
            + self.state.output_tokens / 1_000_000 * out_price,
            4,
        )


def format_plan(plan: BatchPlan) -> str:
    """把计划打印成能逐项核对的样子。成本必须能追到每一项。"""
    est = plan.estimate
    lines: list[str] = []
    lines.append(f"标注计划  {plan.work}   {plan.provider_id} / {plan.model_id}")
    lines.append("=" * 62)
    lines.append(f"章节        共 {plan.total_chapters} 章   待跑 {len(plan.pending)}   跳过（已有标注）{plan.skipped}")
    lines.append(f"并发        {plan.concurrency}    伏笔台账 {'启用' if plan.ledger_enabled else '关闭'}")
    lines.append("")
    lines.append("── 估算 ────────────────────────────────────────────")
    lines.append(f"中文换算系数  {est.tokens_per_cjk_char} tokens/字（{est.coefficient_basis}）")
    lines.append(f"输入 token    {est.input_tokens:,}   输出 token {est.output_tokens:,}   合计 {est.total_tokens:,}")
    for key, value in est.breakdown.items():
        lines.append(f"  · {key}: {value}")
    if est.cost_cny is None:
        lines.append(f"费用         {est.price_note}")
    else:
        lines.append(f"费用         ≈ ¥{est.cost_cny}（估算）")
        lines.append(f"             {est.price_note}")
    if est.est_seconds:
        minutes = est.est_seconds / 60
        lines.append(f"耗时         ≈ {minutes:.1f} 分钟（按实测单章耗时折算，并发 {plan.concurrency} 未计入）")
    lines.append("")

    gate = plan.budget
    if gate.mode == "token" and gate.token_limit:
        lines.append(f"预算闸门     token 模式，本次上限 {int(gate.token_limit):,} tokens，超出即中止")
        if plan.over_budget:
            lines.append(
                f"⚠️  估算 {est.total_tokens:,} 已超上限，直接跑会被闸门拦下。"
                "请调高上限，或分批跑（用 --limit）。"
            )
    elif gate.mode == "cost":
        lines.append(f"预算闸门     金额模式，本次上限 ¥{gate.cost_limit_cny}")
    else:
        lines.append("预算闸门     未设置上限")

    if plan.warnings:
        lines.append("")
        lines.append("── 提醒 ────────────────────────────────────────────")
        for item in plan.warnings:
            lines.append(f"  · {item}")
    return "\n".join(lines)


def format_state(state: BatchState) -> str:
    data = state.to_dict()
    lines = [
        f"运行状态  {state.work}   {state.status}",
        f"进度      {state.committed}/{state.total}"
        + (f"    已用 {data['elapsed_sec']} 秒" if data.get("elapsed_sec") else "")
        + (f"    预计还需 {data['eta_sec']} 秒" if data.get("eta_sec") else ""),
        f"结果      正常 {state.ok}    待复核 {state.needs_review}    失败 {state.failed}",
        f"实测 token 输入 {state.input_tokens:,}  输出 {state.output_tokens:,}  合计 {state.total_tokens:,}",
    ]
    if state.cost_cny is not None:
        lines.append(f"费用      ≈ ¥{state.cost_cny}")
    if state.recent:
        lines.append("")
        lines.append("最近完成：")
        for item in state.recent[:8]:
            lines.append(
                f"  · {item['chapter_id']}  第{item['chapter_no']}章  {item['status']}"
                f"  {item['attempts']} 次  {item['latency_ms']} ms"
            )
    if state.failures:
        lines.append("")
        lines.append(f"失败 {len(state.failures)} 章（未落盘，重跑会重试）：")
        for item in state.failures[:10]:
            lines.append(f"  · {item['chapter_id']}  第{item['chapter_no']}章 {item['title']}")
    if state.ledger_gaps:
        lines.append("")
        lines.append(f"台账缺口 {len(state.ledger_gaps)} 处")
    if state.message:
        lines.append("")
        lines.append(state.message)
    return "\n".join(lines)


def resolve_annotation_options(cfg: WorkshopConfig, opts: AnnotateOptions) -> AnnotateOptions:
    """任务绑定里配好的参数优先于命令行默认值。"""
    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    return AnnotateOptions(
        temperature=float(binding.get("temperature") or opts.temperature),
        thinking=str(binding.get("thinking") or opts.thinking),
        max_tokens=opts.max_tokens,
        max_attempts=int(binding.get("retry") or 0) + 1 if binding.get("retry") else opts.max_attempts,
        timeout_sec=float(binding.get("timeout_sec") or opts.timeout_sec),
    )


def resolve_concurrency(cfg: WorkshopConfig, requested: int | None) -> int:
    if requested:
        return max(1, requested)
    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    # 默认 1 不是保守，是正确性要求：台账与上一章摘要都依赖上一章的结果。
    # 想快就显式指定并发，并接受上面那条警告。
    return 1


def pick_model_id(cfg: WorkshopConfig, provider: ProviderConfig, requested: str | None) -> str:
    if requested:
        return requested
    binding = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    bound = binding.get("model")
    if bound and (not provider.models or bound in provider.model_ids):
        return str(bound)
    return resolve_default_model(cfg, provider)


def unused_sleep() -> None:
    """占位：保留给将来的限速节奏控制。"""
    time.sleep(0)
