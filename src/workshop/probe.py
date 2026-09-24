"""探测与基准测试。

六项探测：
  P1 端点与鉴权 / P2 基础对话 / P3 结构化输出 / P4 usage 字段 / P5 token 换算系数
  P6 并发上限明确不探（要试到限流才有成本与风控风险，且配额按账号分级，实测值会失效）

在此之上产出性能基线，用于三件事：选型对比、退化监测、参数推导。

测试句是固定内置的合成文本，逐字不变并记录哈希——探测不发送任何作品原文。
"""

from __future__ import annotations

import hashlib
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import ErrorCollector, ErrorKind, ProbeError
from .llm import (
    ApiError,
    ChatResult,
    OpenAICompatProvider,
    StreamResult,
    Usage,
    build_thinking_extra,
    extract_json_object,
)

# ── 固定测试配方 ──────────────────────────────────────────────
# 改动这里等于改动测试口径，历史报告将不可直接比较（报告里记录 prompt_hash）。

PROBE_RECIPE_VERSION = "probe-recipe-v2"

PROBE_SYSTEM_PROMPT = "你是一个配置探测助手。只输出被要求的格式，不要输出任何额外说明。"

JSON_PROBE_INSTRUCTION = '请只输出下面这个 JSON，不要任何其他文字：{"ok": true}'

# ── 用于 P5 换算系数的合成文本 ────────────────────────────────
#
# v1 用的是千字文片段反复重复，测出来 DeepSeek 是 0.968 tokens/字。
# 拿真实作品一跑才发现：真实散文只有 0.675，两者差 1.43 倍，
# 按 0.968 估成本会整体高估约 40%。
#
# 原因是那段文本太"整齐"——同一批汉字循环，标点、换行、缩进、
# 数字与拉丁字符一概没有，而真实网文正文里这些占比不小，
# 换行与缩进尤其明显（源文本每段还带 4 个空格）。
#
# v2 改成按**真实中文散文的字符构成**拼：汉字为主，混入标点、
# 换行、缩进与少量数字，段落长短交错。仍然是确定性的合成文本，
# 不含任何作品原文。
CJK_POOL = (
    "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳"
    "云腾致雨露结为霜金生丽水玉出昆冈剑号巨阙珠称夜光果珍李柰菜重芥姜"
)
PROSE_MARKS = "，。，。、；：！？…—（）「」“”"
PROSE_FILLERS = ("的时候", "已经", "没有", "一个", "什么", "自己", "这样", "然后", "就是", "还是")

DEFAULT_TOKENS_PER_CJK_CHAR = 0.7  # 仅作兜底粗估，实测值会覆盖它

# 单章语义标注的典型输出长度。
# 注意：这是**探测阶段的假设**，只用来把实测速度折算成单章耗时。
# 2026-09-23 用 20 章真实样本实测，每章实际只输出约 263 token，
# 所以由它折算出的 est_single_chapter_sec 会明显偏高——成本与耗时的
# 估算请优先用 providers.yaml 里的 measured_batch。
TYPICAL_CHAPTER_OUTPUT_TOKENS = 500

_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


@dataclass
class ProbeOptions:
    text_chars: int = 1000
    samples: int = 3
    streaming: bool = True
    max_tokens: int = 8
    timeout_sec: float = 60.0
    chapters: int | None = None
    concurrency: int | None = None
    triggered_by: str = "manual_test_connection"
    # 思考模式：disabled | enabled | auto
    #
    # 默认关闭。DeepSeek 等模型默认开启思考模式且 effort 为 high，对探测这种
    # 短输出任务是纯浪费：思维链会占满 max_tokens 导致正文为空，温度参数也会失效。
    # 探测的目标是量性能，不是让模型想得深，所以关掉才测得到真实值。
    thinking: str = "disabled"
    reasoning_effort: str | None = None


def make_cjk_text(n: int) -> str:
    """生成确定性的、**像真实中文散文**的测试文本。不用随机，保证跨次可比。

    拼法：从句池里取若干字组成长短不一的句子，句末加标点，段间换行 +
    4 空格缩进，偶尔插入数字与拉丁字母。目的是让字符构成贴近真实网文正文，
    这样两点法量出来的换算系数才能用于成本估算。
    """
    if n <= 0:
        return ""

    parts: list[str] = []
    size = 0
    sentence = 0
    while size < n:
        # 句子长度在 8-32 字之间循环变化，模拟长短句交错
        length = 8 + (sentence * 7) % 24
        filler = PROSE_FILLERS[sentence % len(PROSE_FILLERS)]
        chunk = CJK_POOL * 2
        start = (sentence * 13) % (len(chunk) - length)
        text = chunk[start : start + length]

        if sentence % 5 == 3:
            text = filler + text
        if sentence % 7 == 5:
            text = f"{text}{sentence % 90 + 10}年"
        if sentence % 11 == 8:
            text = text + "abc"

        text += PROSE_MARKS[sentence % len(PROSE_MARKS)]
        # 每句都另起一段：真实网文几乎一段一句，且每段带 4 空格缩进。
        # 实测真实正文的空白占比约 19%，靠这一段 "./n/n    " 才能对上——
        # 空白也是要计入 token 的，漏掉它就等于低估输入。
        text += "\n\n    "
        parts.append(text)
        size += len(text)
        sentence += 1

    return "".join(parts)[:n]


def recipe_hash(text_chars: int, max_tokens: int, streaming: bool) -> str:
    """测试配方指纹。hash 不同则历史报告不可直接对比。"""
    payload = "|".join(
        [
            PROBE_RECIPE_VERSION,
            PROBE_SYSTEM_PROMPT,
            JSON_PROBE_INSTRUCTION,
            CJK_POOL,
            str(text_chars),
            str(max_tokens),
            str(streaming),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def determine_time_bucket(pricing: dict[str, Any], now_utc: datetime | None = None) -> str | None:
    """按服务商的峰谷规则判定当前是高峰还是空闲。

    Windows 上 zoneinfo 依赖 tzdata 包，所以这里直接用 UTC+8 折算北京时间，不引入时区库。
    记录时段很重要：峰谷不只影响价格，高峰时段的负载也可能不同，不记录会让跨次对比得出错误结论。
    """
    tod = (pricing or {}).get("time_of_day") or {}
    if not tod.get("enabled"):
        return None

    now_utc = now_utc or datetime.now(timezone.utc)
    bjt = now_utc + timedelta(hours=8)

    peak_days = {str(d).upper() for d in (tod.get("peak_days_bjt") or list(_WEEKDAY_CODES[:5]))}
    if _WEEKDAY_CODES[bjt.weekday()] not in peak_days:
        return "offpeak"

    current_minute = bjt.hour * 60 + bjt.minute
    for window in tod.get("peak_hours_bjt") or []:
        try:
            start_s, end_s = str(window).split("-")
            sh, sm = (int(x) for x in start_s.split(":"))
            eh, em = (int(x) for x in end_s.split(":"))
        except ValueError:
            continue
        if sh * 60 + sm <= current_minute < eh * 60 + em:
            return "peak"
    return "offpeak"


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.median(clean)


def _stats(values: list[float | None]) -> dict[str, float] | None:
    clean: list[float] = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return {
        "median": round(statistics.median(clean), 2),
        "min": round(min(clean), 2),
        "max": round(max(clean), 2),
    }


def run_probe(
    *,
    client: OpenAICompatProvider,
    provider_id: str,
    provider_name: str,
    model_id: str,
    opts: ProbeOptions,
    pricing: dict[str, Any] | None = None,
    base_url: str = "",
    protocol: str = "openai_compatible",
    anthropic_base_url: str | None = None,
) -> dict[str, Any]:
    """跑完整探测，返回可直接落盘的报告字典。"""

    started = datetime.now(timezone.utc)
    errors = ErrorCollector()
    prompt_digest = recipe_hash(opts.text_chars, opts.max_tokens, opts.streaming)
    extra_body = build_thinking_extra(opts.thinking, opts.reasoning_effort)

    report: dict[str, Any] = {
        "provider": provider_id,
        "provider_name": provider_name,
        "model": model_id,
        "triggered_by": opts.triggered_by,
        "checked_at": started.astimezone().isoformat(timespec="seconds"),
        "time_bucket": determine_time_bucket(pricing or {}, started),
        "recipe_version": PROBE_RECIPE_VERSION,
        "config_snapshot": {
            "base_url": base_url,
            "base_url_anthropic": anthropic_base_url,
            "protocol": protocol,
            "prompt_hash": prompt_digest,
            "max_tokens": opts.max_tokens,
            "streaming": opts.streaming,
            "text_chars": opts.text_chars,
            "samples": opts.samples,
            "thinking": opts.thinking,
            "reasoning_effort": opts.reasoning_effort,
            "extra_body": extra_body,
        },
        "results": {
            "reachable": False,
            "models_discovered": [],
            "structured_output": None,
            "usage_in_response": None,
            "tokens_per_cjk_char": None,
            "tokens_per_cjk_char_estimated": True,
        },
        "benchmark": {},
        "derived": {},
        "usage_total": {},
        "errors": [],
    }
    results = report["results"]

    # ── P1 连通性与模型列表（零成本，一次抓住端点错与鉴权错） ──
    rtt_samples: list[float] = []
    for _ in range(max(1, opts.samples)):
        try:
            models, rtt_ms = client.list_models()
        except ApiError as exc:
            errors.add("P1", exc.kind, exc.status, exc.safe_body(client.secrets))
            report["errors"] = errors.to_list()
            return _finalize(report, errors, opts)
        rtt_samples.append(rtt_ms)
        results["models_discovered"] = models
        results["reachable"] = True

    if not results["models_discovered"]:
        errors.add("P1", ErrorKind.RESPONSE_UNPARSABLE, detail="模型列表为空")

    if model_id not in results["models_discovered"] and results["models_discovered"]:
        errors.add(
            "P1",
            ErrorKind.MODEL_NOT_FOUND,
            detail=f"配置里的模型 {model_id} 不在接口返回的列表中",
        )

    # ── P2 基础对话 + 基准测 ──
    messages = [
        {"role": "system", "content": PROBE_SYSTEM_PROMPT},
        {"role": "user", "content": make_cjk_text(opts.text_chars)},
    ]
    ttft_samples: list[float] = []
    first_content_samples: list[float] = []
    total_samples: list[float] = []
    speed_samples: list[float] = []
    output_token_samples: list[int] = []
    ok_count = 0
    usage_seen: Usage | None = None
    thinking_used = False

    for _ in range(max(1, opts.samples)):
        try:
            if opts.streaming:
                stream: StreamResult = client.chat_stream(
                    model_id, messages, max_tokens=opts.max_tokens, extra=extra_body
                )
                if stream.ttft_ms is not None:
                    ttft_samples.append(stream.ttft_ms)
                if stream.first_content_ms is not None:
                    first_content_samples.append(stream.first_content_ms)
                total_samples.append(stream.total_ms)
                if stream.thinking_used:
                    thinking_used = True
                if stream.usage and stream.usage.present:
                    usage_seen = stream.usage

                # 速度按「正文 token 数 ÷ 正文窗口」算。
                # 用 completion_tokens 与全窗口会把思维链和思考时间一起算进去，得出偏低的假速度。
                content_tokens = stream.content_tokens
                window_start = stream.first_content_ms or stream.ttft_ms or 0.0
                gen_ms = stream.total_ms - window_start
                if content_tokens and gen_ms > 1:
                    speed_samples.append(content_tokens / (gen_ms / 1000.0))
                output_token_samples.append(stream.output_tokens)
            else:
                res: ChatResult = client.chat(
                    model_id, messages, max_tokens=opts.max_tokens, extra=extra_body
                )
                total_samples.append(res.total_ms)
                if res.thinking_used:
                    thinking_used = True
                if res.usage and res.usage.present:
                    usage_seen = res.usage
                    output_token_samples.append(res.usage.completion_tokens)
            ok_count += 1
        except ApiError as exc:
            errors.add("P2", exc.kind, exc.status, exc.safe_body(client.secrets))

    if ok_count == 0:
        report["errors"] = errors.to_list()
        return _finalize(report, errors, opts)

    # ── P3 结构化输出（失败不中止，标记后走降级路径） ──
    try:
        p3: ChatResult = client.chat(
            model_id,
            [
                {"role": "system", "content": PROBE_SYSTEM_PROMPT},
                {"role": "user", "content": JSON_PROBE_INSTRUCTION},
            ],
            max_tokens=64,
            response_format={"type": "json_object"},
            extra=extra_body,
        )
        if p3.thinking_used:
            thinking_used = True
        parsed = extract_json_object(p3.text)
        if parsed is not None and "ok" in parsed:
            results["structured_output"] = True
        elif not p3.text.strip() and p3.thinking_used:
            # 关键区分：这不是「不支持结构化输出」，而是输出预算被思维链吃掉了。
            # 两种情况处理方式完全不同，混为一谈会误导后续决策，所以单独分类、单独提示。
            results["structured_output"] = None
            errors.add(
                "P3",
                ErrorKind.OUTPUT_BUDGET_CONSUMED_BY_REASONING,
                detail=(
                    f"正文为空，思维链长度 {len(p3.reasoning_text)} 字。"
                    "关闭思考模式后重测即可判定是否真的支持结构化输出。"
                ),
            )
        else:
            results["structured_output"] = False
            errors.add(
                "P3",
                ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED,
                detail=f"返回内容无法解析为预期 JSON：{(p3.text[:120] or '(空)')}",
            )
        if p3.usage and p3.usage.present:
            usage_seen = usage_seen or p3.usage
    except ApiError as exc:
        if exc.kind in (ErrorKind.BAD_REQUEST, ErrorKind.SERVER_ERROR):
            results["structured_output"] = False
            errors.add(
                "P3",
                ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED,
                exc.status,
                exc.safe_body(client.secrets),
            )
        else:
            errors.add("P3", exc.kind, exc.status, exc.safe_body(client.secrets))

    # ── P4 usage 字段（复用前面响应，不额外发请求） ──
    results["usage_in_response"] = bool(usage_seen and usage_seen.present)
    if not results["usage_in_response"]:
        errors.add(
            "P4",
            ErrorKind.USAGE_MISSING,
            detail="响应中未发现可用量字段，费用只能估算",
        )
    if usage_seen:
        report["usage_total"] = usage_seen.to_dict()

    # ── P5 token 换算系数（两点法，差分消掉固定提示词开销） ──
    char_lo = max(50, opts.text_chars // 2)
    char_hi = max(char_lo + 1, opts.text_chars)
    p5_usage = 0
    try:
        r_lo: ChatResult = client.chat(
            model_id,
            [
                {"role": "system", "content": PROBE_SYSTEM_PROMPT},
                {"role": "user", "content": make_cjk_text(char_lo)},
            ],
            max_tokens=1,
            extra=extra_body,
        )
        r_hi: ChatResult = client.chat(
            model_id,
            [
                {"role": "system", "content": PROBE_SYSTEM_PROMPT},
                {"role": "user", "content": make_cjk_text(char_hi)},
            ],
            max_tokens=1,
            extra=extra_body,
        )
        usage_lo, usage_hi = r_lo.usage, r_hi.usage
        p5_usage = (usage_lo.total_tokens if usage_lo else 0) + (
            usage_hi.total_tokens if usage_hi else 0
        )
        if usage_lo and usage_hi:
            delta_tokens = usage_hi.prompt_tokens - usage_lo.prompt_tokens
            delta_chars = char_hi - char_lo
            if delta_tokens > 0 and delta_chars > 0:
                coefficient = delta_tokens / delta_chars
                # 中文每字 token 数合理区间大致在 0.3 到 3 之间，超出视为异常
                if 0.3 <= coefficient <= 3.0:
                    results["tokens_per_cjk_char"] = round(coefficient, 4)
                    results["tokens_per_cjk_char_estimated"] = False
                else:
                    errors.add(
                        "P5",
                        ErrorKind.RESPONSE_UNPARSABLE,
                        detail=f"换算系数超出合理区间：{coefficient:.3f}",
                    )
    except ApiError as exc:
        errors.add("P5", exc.kind, exc.status, exc.safe_body(client.secrets))

    if results["tokens_per_cjk_char"] is None:
        results["tokens_per_cjk_char"] = DEFAULT_TOKENS_PER_CJK_CHAR

    if p5_usage:
        report["usage_total"]["p5_total_tokens"] = p5_usage

    # ── 基准测汇总 ──
    report["benchmark"] = {
        "samples": max(1, opts.samples),
        "success": f"{ok_count}/{max(1, opts.samples)}",
        "rtt_ms": _stats(rtt_samples),
        "ttft_ms": _stats(ttft_samples),
        "first_content_ms": _stats(first_content_samples),
        "total_latency_ms": _stats(total_samples),
        "output_tokens": int(_median([float(t) for t in output_token_samples]) or 0),
        "output_tokens_per_sec": _stats(speed_samples),
        "thinking_used": thinking_used,
    }

    # ── 推导值：把实测翻译成可直接用的配置值 ──
    median_total = _median(total_samples) or 0.0
    median_ttft = _median(ttft_samples) or 0.0
    # 单章耗时以「正文开始出现」为起点更贴近真实；思考模式下 first_content 会远晚于 ttft
    median_first_content = _median(first_content_samples) or median_ttft
    median_speed = _median(speed_samples)

    suggested_timeout = max(10, int(math.ceil(median_total * 3 / 1000)))
    single_chapter_sec: float | None = None
    if median_speed and median_speed > 0:
        single_chapter_sec = round(
            median_first_content / 1000.0 + TYPICAL_CHAPTER_OUTPUT_TOKENS / median_speed, 1
        )
    elif median_total:
        single_chapter_sec = round(median_total / 1000.0, 1)

    derived: dict[str, Any] = {
        "suggested_timeout_sec": suggested_timeout,
        "est_single_chapter_sec": single_chapter_sec,
        "est_full_run_hours": None,
        "note": None,
    }
    if single_chapter_sec and opts.chapters and opts.concurrency:
        hours = single_chapter_sec * opts.chapters / opts.concurrency / 3600
        derived["est_full_run_hours"] = round(hours, 2)

    if thinking_used:
        derived["note"] = (
            "本次探测在思考模式下进行，延迟、速度与单章耗时都包含思维链开销，"
            "不代表关闭思考后的性能。请关闭思考模式后重测再作容量规划。"
        )

    report["derived"] = derived

    report["errors"] = errors.to_list()
    return _finalize(report, errors, opts)


def _finalize(report: dict[str, Any], errors: ErrorCollector, opts: ProbeOptions) -> dict[str, Any]:
    """补上报告 id 与判定结论。"""
    checked = report.get("checked_at") or datetime.now().isoformat(timespec="seconds")
    stamp = checked.replace("-", "").replace(":", "").replace("+", "").split(".")[0]
    report["report_id"] = f"{report['provider']}-{report['model']}-{stamp}"
    report["report_id"] = report["report_id"].replace(" ", "_")

    blocking = errors.blocking_items
    report["verdict"] = {
        "enabled_ok": report["results"]["reachable"] and not any(
            e.step == "P2" and e.blocking for e in errors.items
        ),
        "blocking_error_count": len(blocking),
        "non_blocking_error_count": len(errors.items) - len(blocking),
    }
    return report


def format_report_text(report: dict[str, Any]) -> str:
    """把报告渲染成人可读文本，供命令行直接查看。"""
    r = report.get("results", {})
    b = report.get("benchmark", {})
    d = report.get("derived", {})
    v = report.get("verdict", {})

    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(f"探测报告  {report.get('report_id', '')}")
    lines.append("=" * 62)
    lines.append(f"服务商       {report.get('provider_name')} ({report.get('provider')})")
    lines.append(f"模型         {report.get('model')}")
    lines.append(f"探测时间     {report.get('checked_at')}")
    bucket = report.get("time_bucket")
    bucket_text = {"peak": "高峰时段", "offpeak": "空闲时段"}.get(bucket or "", "不适用")
    lines.append(f"计费时段     {bucket_text}")

    snapshot = report.get("config_snapshot") or {}
    thinking = snapshot.get("thinking")
    thinking_text = {"disabled": "关闭", "enabled": "开启", "auto": "不指定"}.get(
        thinking or "", "-"
    )
    if b.get("thinking_used"):
        thinking_text += "（实际检测到思维链）"
    lines.append(f"思考模式     {thinking_text}")
    lines.append(f"测试配方     {report.get('recipe_version')} / hash {snapshot.get('prompt_hash')}")

    lines.append("")
    lines.append("── 六项探测 ──────────────────────────────────")
    lines.append(f"P1 端点与鉴权    {'可达' if r.get('reachable') else '不可达'}")
    lines.append(f"   模型列表       {len(r.get('models_discovered') or [])} 个")
    for mid in (r.get("models_discovered") or [])[:12]:
        lines.append(f"                 · {mid}")
    p3 = r.get("structured_output")
    lines.append(
        f"P3 结构化输出    {'支持' if p3 else '不支持（将走降级路径）' if p3 is False else '未测到'}"
    )
    p4 = r.get("usage_in_response")
    lines.append(
        f"P4 usage 字段    {'返回' if p4 else '不返回（费用只能估算）' if p4 is False else '未测到'}"
    )
    tpc = r.get("tokens_per_cjk_char")
    flag = "粗估" if r.get("tokens_per_cjk_char_estimated") else "实测"
    lines.append(f"P5 token 系数    {tpc} tokens/字（{flag}）")
    lines.append("P6 并发上限      不探测，按控制台配额手填")

    lines.append("")
    lines.append("── 基准测 ────────────────────────────────────")
    lines.append(f"采样次数       {b.get('samples')}  成功 {b.get('success')}")
    lines.append(f"端点往返       {_fmt_stat(b.get('rtt_ms'), 'ms')}")
    lines.append(f"首字延迟 TTFT  {_fmt_stat(b.get('ttft_ms'), 'ms')}")
    lines.append(f"正文首字       {_fmt_stat(b.get('first_content_ms'), 'ms')}")
    lines.append(f"总延迟         {_fmt_stat(b.get('total_latency_ms'), 'ms')}")
    lines.append(f"输出速度       {_fmt_stat(b.get('output_tokens_per_sec'), ' tokens/s')}（按正文 token 计）")

    lines.append("")
    lines.append("── 推导值 ────────────────────────────────────")
    lines.append(f"建议超时       {d.get('suggested_timeout_sec')} 秒（中位总延迟 × 3）")
    lines.append(f"单章耗时       约 {d.get('est_single_chapter_sec')} 秒")
    if d.get("est_full_run_hours") is not None:
        lines.append(f"全量耗时       约 {d.get('est_full_run_hours')} 小时")
    if d.get("note"):
        lines.append("")
        lines.append(f"⚠️  {d['note']}")

    lines.append("")
    lines.append("── 结论 ──────────────────────────────────────")
    lines.append(f"可否启用       {'是' if v.get('enabled_ok') else '否'}")
    lines.append(
        f"阻断性错误 {v.get('blocking_error_count', 0)} 个 / "
        f"非阻断 {v.get('non_blocking_error_count', 0)} 个"
    )

    errs = report.get("errors") or []
    if errs:
        lines.append("")
        lines.append("── 错误明细 ──────────────────────────────────")
        for e in errs:
            if not isinstance(e, dict):
                continue
            tag = "阻断" if e.get("blocking") else "提示"
            status = f" HTTP {e['http_status']}" if e.get("http_status") else ""
            # 归档的报告可能被手工编辑过，或是更早版本写的，渲染时按缺失字段容错
            lines.append(f"[{tag}] {e.get('step', '?')} {e.get('kind', 'unknown')}{status}")
            if e.get("hint"):
                lines.append(f"       {e['hint']}")
            if e.get("detail"):
                lines.append(f"       细节：{str(e['detail'])[:200]}")

    return "\n".join(lines)


def _fmt_stat(stat: dict[str, float] | None, unit: str) -> str:
    if not stat:
        return "未测到"
    return f"{stat['median']}{unit}（{stat['min']} ~ {stat['max']}）"
