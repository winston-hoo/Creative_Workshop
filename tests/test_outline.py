"""大纲链路自检。

    python tests/test_outline.py

跑在本地模拟服务上：不花钱、不需要密钥。

验证的是这条链路的四件事：

  1. 原料从哪来：逐章梗概挂在标注上（P22），没跑标注就明确说缺原料，
     而不是凭空生成一份像模像样的大纲
  2. 分层归约：分块 → 块梗概 → 全书大纲，块与块之间独立（可断点续跑）
  3. 失败不伪装：归约失败的块在产物里是显式的 error，不是空字符串
  4. 分块标签在章号回落时不能写成「第 151-3 章」
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import AnnotateOptions, save_annotation  # noqa: E402
from workshop.batch import load_chapter_tasks  # noqa: E402
from workshop.ingest import ingest, save_ingest  # noqa: E402
from workshop.llm import ChatResult, OpenAICompatProvider  # noqa: E402
from workshop.outline import (  # noqa: E402
    MISSING_FAILED,
    MISSING_NO_ANNOTATION,
    MISSING_SUMMARY_LOST,
    OutlineOptions,
    OutlineCall,
    _BOOK_PROMPT,
    _block_label,
    _call,
    build_outline_plan,
    collect_summaries,
    generate_outline,
    missing_detail,
    render_markdown,
    save_outline,
    summarize_missing,
)
from workshop.primitives import load_primitives  # noqa: E402
from workshop.samples import build_synthetic_novel  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402

WORK = "大纲自检"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="workshop-outline-test-"))
WORK_DIR = TMP / "workspaces" / WORK
INGEST = WORK_DIR / "00-ingest"
ANNOTATIONS = WORK_DIR / "10-annotations"
OUT_DIR = WORK_DIR / "40-outline"


def prepare(with_summaries: int = 8) -> None:
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    source = TMP / "source.txt"
    source.write_text(build_synthetic_novel(), encoding="utf-8")
    save_ingest(ingest(source, work_name=WORK), INGEST)
    (WORK_DIR / "work.yaml").write_text('work: 大纲自检\ncore_motive: "活下去"\n', encoding="utf-8")

    tasks = load_chapter_tasks(INGEST)
    for index, task in enumerate(tasks[:with_summaries], start=1):
        save_annotation(
            {
                "chapter_id": task.chapter_id,
                "chapter_no": task.chapter_no,
                "title": task.title,
                "status": "ok",
                "fields": {
                    "char_count": task.char_count,
                    "chapter_summary": f"第 {index} 章：主角做了一件事，处境有变化。",
                },
                "source": {"sha256": f"fake-{task.chapter_id}"},
            },
            ANNOTATIONS,
        )


def plan_for(block_size: int = 3, tasks=None):
    return build_outline_plan(
        work_name=WORK,
        provider_id="mock",
        model_id="mock-flash",
        tasks=tasks if tasks is not None else load_chapter_tasks(INGEST),
        annotations_dir=ANNOTATIONS,
        block_size=block_size,
        price_input_per_mtok=1.0,
        price_output_per_mtok=4.0,
        bucket="offpeak",
    )


def block_responder(messages: list):
    system = str(messages[0].get("content") or "") if messages else ""
    user = str(messages[1].get("content") or "") if len(messages) > 1 else ""
    if "structure" in system:
        if "失败" in user:
            return "这不是 JSON"
        return {
            "logline": "一句话大纲",
            "premise": "开局设定",
            "structure": [{"part": "第一段", "gist": "这一段的事", "turn": "一次转折"}],
            "main_threads": [{"thread": "主线", "gist": "走向"}],
            "key_turns": ["转折一"],
            "ending": "结局",
            "confidence": "中",
            "uncertain_fields": [],
        }
    if "失败" in user:
        return "这不是 JSON"
    return {"summary": f"（模拟）{user.splitlines()[0][:20]} 的段落梗概。"}


def run(plan, *, responder=block_responder, max_attempts=1):
    with MockServer(MockState(responder=responder, per_token_ms=0)) as server:
        client = OpenAICompatProvider(base_url=server.base_url, api_key="k", timeout_sec=15)
        return generate_outline(
            plan=plan,
            client=client,
            annotations_dir=ANNOTATIONS,
            opts=OutlineOptions(block_size=plan.block_size, max_attempts=max_attempts),
        )


# ── 输出被截断 ──────────────────────────────────────────────
#
# 一部数百章的连载，真实运行时：11 块段梗概全部成功，最后一步「全书归约」
# 却报「返回内容不是 JSON」。真实原因是输出预算写死 1200，大纲正文还没写完
# 就被砍断——截断的 JSON 必然不是合法 JSON，于是报错文案把病根盖住了。

# 真实截断长得就是这样：写到 structure 的第二条，值还没写完就没了。
# 用的样例是 samples/original-demo 那套合成设定（数值之眼 / 青云宗），
# 与任何真实作品无关。
BOOK_TRUNCATED = (
    "{\n"
    '  "logline": "一个只能看、不能改的数值之眼，让零层废柴在修真界先活下来。",\n'
    '  "premise": "外门柴房杂役李默，靠一双看得见修为数值的眼睛活了下来。",\n'
    '  "structure": [\n'
    '    {"part": "第一段 第1-163章", "gist": "外门柴房的挣扎与第一次看穿。", "turn": "觉醒数值之眼"},\n'
    '    {"part": "第二段 第164-326章", "gist": "走出宗门之后的对抗'
)

BOOK_COMPLETE = json.dumps(
    {
        "logline": "一个只能看、不能改的数值之眼，让零层废柴在修真界先活下来。",
        "premise": "外门杂役李默凭数值之眼看穿修真界的规矩。",
        "structure": [
            {"part": "第一段", "gist": "外门挣扎。", "turn": "觉醒数值之眼"},
            {"part": "第二段", "gist": "走出宗门。", "turn": "公开这双眼"},
        ],
        "main_threads": [{"thread": "主线", "gist": "从柴房走到宗主面前"}],
        "key_turns": ["觉醒", "公开"],
        "ending": "在青云宗大殿上摊牌。",
        "confidence": "中",
        "uncertain_fields": [],
    },
    ensure_ascii=False,
)


class StubClient:
    """假客户端：按提示词分派应答，并记下每次请求的 max_tokens。

    finish_reason 由应答方显式给出：「模型把话说完了但没给 JSON」和
    「被 max_tokens 砍断」是两回事，靠文本猜会把截断判断的逻辑测歪。
    """

    def __init__(self, responder) -> None:
        self.responder = responder
        self.seen_max_tokens: list[int] = []
        self.secrets: list[str] = []

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 8,
        temperature: float = 0.0,
        response_format: dict | None = None,
        extra: dict | None = None,
    ) -> ChatResult:
        self.seen_max_tokens.append(max_tokens)
        text, finish = self.responder(messages, max_tokens)
        return ChatResult(text=text, finish_reason=finish)


# ── 用例 ──────────────────────────────────────────────────


def test_no_summaries_is_explicit() -> None:
    """没有梗概就要说不出来，不能生成一份凭空的大纲。"""
    print("没有逐章梗概时明确报缺原料")
    prepare(with_summaries=0)
    plan = plan_for()
    check(plan.summarized == 0, "有梗概 0 章")
    check(len(plan.blocks) == 0, "切不出任何块")
    check(plan.to_dict()["ready"] is False, "标记为未就绪")
    check(bool(plan.warnings), "给出了原因")
    check("先跑标注" in plan.warnings[0], f"原因说清了要跑标注：{plan.warnings[0][:40]}")

    result = run(plan)
    check(result.outline is None, "不会产出大纲")
    check(bool(result.errors), "明确报了错")


def test_missing_partial_is_reported() -> None:
    print("只跑了一部分标注时说清缺多少")
    prepare(with_summaries=3)
    plan = plan_for()
    check(plan.summarized == 3, "有梗概 3 章")
    check(plan.missing == 5, f"缺梗概 5 章（实际 {plan.missing}）")
    check(any("缺梗概" in w for w in plan.warnings), "警告里点出了缺口")


def _write_annotation(cid: str, *, summary=None, errors=None, rich: bool = True) -> None:
    """手造一份标注文件。rich=True 表示模型轨字段都填上了（这一章跑成功了）。"""
    fields: dict = {"char_count": 100, "chapter_summary": summary}
    if rich:
        fields.update(
            {
                "perspective": "第三限知",
                "scene_switches": 1,
                "time_span": "即时",
                "hook_strength": 3,
                "hook_type": "期待",
                "emotion": 3,
            }
        )
    record = {
        "chapter_id": cid,
        "chapter_no": 1,
        "status": "needs_review",
        "fields": fields,
        "source": {"sha256": f"fake-{cid}"},
    }
    if errors:
        record["errors"] = errors
    ANNOTATIONS.mkdir(parents=True, exist_ok=True)
    (ANNOTATIONS / f"{cid}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def test_missing_reasons_are_distinguished() -> None:
    """回归：三种缺梗概曾混成一句「没跑过标注或标注失败」。

    一部 545 章的连载真实踩到：500 章的梗概是被演示模式误覆盖清空的，
    既不是没跑标注、也不是标注失败，按那句提示根本不知道该怎么办。
    """
    print("缺梗概要按原因分开：未跑标注 / 标注失败 / 梗概缺失")

    prepare(with_summaries=0)
    tasks = load_chapter_tasks(INGEST)
    c = [t.chapter_id for t in tasks]

    _write_annotation(c[0], summary="正常梗概")                                   # 有梗概
    _write_annotation(c[1])                                                        # 梗概缺失
    _write_annotation(c[2], errors=["chapter_summary 被演示模式误覆盖，已置空"])   # 梗概缺失
    _write_annotation(c[3], rich=False, errors=["模型返回不是 JSON"])              # 标注失败

    _have, missing = collect_summaries(tasks, ANNOTATIONS)
    reasons = summarize_missing(missing)

    check(reasons.get(MISSING_SUMMARY_LOST) == 2, f"梗概缺失 2 章（实际 {reasons.get(MISSING_SUMMARY_LOST)}）")
    check(reasons.get(MISSING_FAILED) == 1, f"标注失败 1 章（实际 {reasons.get(MISSING_FAILED)}）")
    check(
        reasons.get(MISSING_NO_ANNOTATION) == len(tasks) - 4,
        f"未跑标注 {len(tasks) - 4} 章（实际 {reasons.get(MISSING_NO_ANNOTATION)}）",
    )

    # 关键：带 errors 不等于标注失败——别的模型字段都填上了就是梗概被清空
    by_reason: dict = {}
    for task, reason in missing:
        by_reason.setdefault(reason, []).append(task.chapter_id)
    check(
        by_reason.get(MISSING_SUMMARY_LOST) == [c[1], c[2]],
        f"被误覆盖的那章归到「梗概缺失」而不是「标注失败」（实际 {by_reason.get(MISSING_SUMMARY_LOST)}）",
    )
    check(by_reason.get(MISSING_FAILED) == [c[3]], "模型字段全空的才判为标注失败")

    plan = plan_for(tasks=tasks)
    check(plan.missing_reasons == reasons, "计划里带上了原因分布")
    warn = " ".join(plan.warnings)
    for reason in (MISSING_NO_ANNOTATION, MISSING_FAILED, MISSING_SUMMARY_LOST):
        check(reason in warn, f"警告里点出了「{reason}」")

    detail = missing_detail(reasons)
    check(len(detail) == 3, f"按原因摊成 3 行（实际 {len(detail)}）")
    check(all(d.get("hint") for d in detail), "每一行都给了「该怎么办」")
    check(detail[0]["count"] >= detail[-1]["count"], "按章数从多到少排")


def test_blocking_and_estimate() -> None:
    print("分块与估算")
    prepare()
    plan = plan_for(block_size=3)
    check(len(plan.blocks) == 3, f"8 章按 3 章一块切成 3 块（实际 {len(plan.blocks)}）")
    check(plan.blocks[0][0].chapter_id == "v001-c0001", "第一块从第一章起")
    check(plan.blocks[-1][-1].chapter_id == "v002-c0001", "最后一块到最后一章")
    check(plan.est_input_tokens > 0 and plan.est_output_tokens > 0, "给出了 token 估算")
    check(plan.est_cost_cny is not None, "有单价时算得出费用")

    no_price = build_outline_plan(
        work_name=WORK,
        provider_id="mock",
        model_id="mock-flash",
        tasks=load_chapter_tasks(INGEST),
        annotations_dir=ANNOTATIONS,
        block_size=3,
    )
    check(no_price.est_cost_cny is None, "没单价就不给费用数字")


def test_generate_outline() -> None:
    print("分块归约 + 全书归约")
    prepare()
    plan = plan_for(block_size=3)
    result = run(plan)

    check(len(result.blocks) == 3, "产出 3 块")
    check(all(b.get("summary") for b in result.blocks), "每块都有梗概")
    check(result.outline is not None, "产出了全书大纲")
    check(result.outline.get("logline") == "一句话大纲", "大纲字段解析正确")
    meta = result.outline.get("_meta") or {}
    check(meta.get("blocks_used") == 3, "元信息记录了用了几块")
    check(meta.get("missing") == 0, "元信息记录了缺口")
    check(result.usage.get("total_tokens", 0) > 0, "累计了实测 usage")


def test_failed_block_is_not_faked() -> None:
    print("归约失败的块不伪装成空梗概")
    prepare()
    tasks = load_chapter_tasks(INGEST)
    plan = plan_for(block_size=3)

    def responder(messages: list):
        user = str(messages[1].get("content") or "") if len(messages) > 1 else ""
        # 让包含第 4 章的那一块失败
        if "第4章" in user:
            return "这不是 JSON"
        return block_responder(messages)

    result = run(plan, responder=responder)
    failed = [b for b in result.blocks if not b.get("summary")]
    check(len(failed) == 1, f"恰有 1 块失败（实际 {len(failed)}）")
    check(bool(failed and failed[0].get("error")), "失败的块带 error 说明，不是空串")
    check(bool(result.errors), "错误进了 errors 列表")
    check(result.outline is not None, "其余块够用，仍能出全书大纲")
    meta = result.outline.get("_meta") or {}
    check(meta.get("blocks_failed") == 1, "元信息里记了失败块数")


def test_block_label_with_restart() -> None:
    """章号在分段重启时会回落，标签不能写成「第 151-3 章」。"""
    print("章号回落时的分块标签")
    prepare()
    tasks = load_chapter_tasks(INGEST)
    cross = [t for t in tasks if t.chapter_id in ("v001-c0006b", "v002-c0001")]
    check(len(cross) == 2, "取到跨界的两章")
    label = _block_label(cross)
    check("→" in label, f"回落时退回用章节 id 标识（{label}）")

    normal = tasks[:3]
    check("第 1-3 章" == _block_label(normal), f"单调时用章号范围（{_block_label(normal)}）")
    check("第 1 章" == _block_label(tasks[:1]), "单章就写一章")


def test_resume_from_state() -> None:
    print("断点续跑：已算好的块不重算")
    prepare()
    plan = plan_for(block_size=3)
    state = OUT_DIR / "_state.json"

    with MockServer(MockState(responder=block_responder, per_token_ms=0)) as server:
        client = OpenAICompatProvider(base_url=server.base_url, api_key="k", timeout_sec=15)
        first = generate_outline(
            plan=plan,
            client=client,
            annotations_dir=ANNOTATIONS,
            opts=OutlineOptions(block_size=3, max_attempts=1),
            state_path=state,
        )
    calls_after_first = len(first.blocks)
    check(calls_after_first == 3, "第一次算出 3 块")

    # 把中间那块从状态里删掉，模拟「跑到一半中断」
    cached = json.loads(state.read_text(encoding="utf-8"))
    cached["blocks"] = cached["blocks"][:2]
    state.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

    seen: list[str] = []

    def counting(messages: list):
        user = str(messages[1].get("content") or "") if len(messages) > 1 else ""
        seen.append(user.splitlines()[0][:24])
        return block_responder(messages)

    with MockServer(MockState(responder=counting, per_token_ms=0)) as server:
        client = OpenAICompatProvider(base_url=server.base_url, api_key="k", timeout_sec=15)
        second = generate_outline(
            plan=plan,
            client=client,
            annotations_dir=ANNOTATIONS,
            opts=OutlineOptions(block_size=3, max_attempts=1),
            state_path=state,
        )
    check(len(second.blocks) == 3, "第二次仍然得到 3 块")
    check(len(seen) == 2, f"只重算了缺的 1 块 + 1 次全书归约（实际发了 {len(seen)} 次）")


def test_markdown_and_save() -> None:
    print("Markdown 渲染与落盘")
    prepare()
    plan = plan_for(block_size=3)
    result = run(plan)
    md = render_markdown(WORK, result, plan)
    for title in ("《大纲自检》大纲", "一句话", "分段结构", "关键转折", "逐段梗概"):
        check(title in md, f"包含「{title}」")

    paths = save_outline(WORK_DIR, result, plan)
    check(paths["json"].exists(), "JSON 落盘")
    check(paths["markdown"].exists(), "Markdown 落盘")
    check(paths["latest_md"].exists(), "latest.md 存在（界面读它）")
    saved = json.loads(paths["latest_json"].read_text(encoding="utf-8"))
    check(saved["outline"]["logline"] == "一句话大纲", "落盘内容正确")


def test_primitive_p22() -> None:
    print("P22 本章剧情梗概")
    primitives = load_primitives(ROOT / "primitives.yaml")
    spec = primitives.by_key("chapter_summary")
    check(spec is not None, "原语里有 chapter_summary")
    check(spec is not None and spec.source == "model", "它属于模型轨")
    check(spec is not None and spec.max_len == 60, "带建议长度")

    block = primitives.build_model_schema_block()
    check('"chapter_summary"' in block, "schema 骨架里有它")
    check('"chapter_summary": []' not in block, "字符串字段不能渲染成数组骨架（那会让模型填错类型）")

    value, _ = primitives.validate({"chapter_summary": "  主角活了下来。  "})
    check(value["chapter_summary"] == "主角活了下来。", "正常值被去掉首尾空白")

    # 超长**不截断**。实测吃过亏：48 字上限把
    # 「…却得知张老师生病异能课停开」砍成「…异能课停」，半句话进了大纲原料。
    # max_len 是给模型的建议，只有离谱地长才退到句末收口。
    long_text = "这是一句完整的剧情梗概，描述了主角在生日当天发生的事。" * 2
    value, _ = primitives.validate({"chapter_summary": long_text})
    check(value["chapter_summary"] == long_text, f"略超建议长度时原样保留（{len(long_text)} 字）")
    check(value["chapter_summary"].endswith("。"), "句子没有被砍断")

    huge = "字" * 400
    value, _ = primitives.validate({"chapter_summary": huge})
    check(len(value["chapter_summary"]) < len(huge), "离谱地长才截断")
    check(len(value["chapter_summary"]) <= 160, f"且截到硬上限内（{len(value['chapter_summary'])}）")

    value, _ = primitives.validate({"chapter_summary": "   "})
    check(value["chapter_summary"] is None, "纯空白按没填处理（保持 null，不产生假内容）")

    _value, issues = primitives.validate({"chapter_summary": 123})
    check(
        any("chapter_summary" in i and "字符串" in i for i in issues),
        "类型不对要显式报错",
    )


def test_book_reduction_escalates_budget() -> None:
    """回归：书级归约被截断却报「返回内容不是 JSON」。

    现在撞上截断就加大输出预算重试，不再原地重试三次后报一个与病根无关的错。
    """
    print("全书归约被截断时加大输出预算重试")

    opts = OutlineOptions(max_tokens=1200, max_tokens_cap=32000, max_attempts=3)
    client = StubClient(
        lambda messages, budget: (BOOK_TRUNCATED, "length") if budget <= 1200 else (BOOK_COMPLETE, "stop")
    )
    call = _call(client, "mock-flash", _BOOK_PROMPT, "各段梗概如下", opts)

    check(isinstance(call, OutlineCall), "返回结构化的结果，而不是裸三元组")
    check(client.seen_max_tokens == [1200, 2400], f"第二次把预算翻倍（实际 {client.seen_max_tokens}）")
    check(call.payload is not None and call.payload.get("ending") == "在青云宗大殿上摊牌。", "最终拿到完整大纲")
    check(not call.salvaged, "拿到完整结果就不标记为「只抢救出部分」")
    check(call.error == "", "没有错误，更不会报成「返回内容不是 JSON」")


def test_salvage_when_budget_exhausted() -> None:
    """预算加到顶还是截断时，保住截断前的字段，而不是整份大纲作废。"""
    print("预算用尽时从截断处抢救")

    opts = OutlineOptions(max_tokens=1200, max_tokens_cap=4800, max_attempts=5)
    client = StubClient(lambda messages, budget: (BOOK_TRUNCATED, "length"))
    call = _call(client, "mock-flash", _BOOK_PROMPT, "各段梗概如下", opts)

    check(client.seen_max_tokens == [1200, 2400, 4800], f"预算递增到上限（实际 {client.seen_max_tokens}）")
    check(call.salvaged, "标记为「只抢救出部分」，不假装完整")
    check((call.payload or {}).get("logline"), "截断前的 logline 保住了")
    check("截断" in call.notice, "说明里讲清了是截断，而不是含糊的「不是 JSON」")


def test_non_json_is_not_called_truncation() -> None:
    """反向的误判也要防：普通文字不能被打上「截断」的标签，预算也不能无谓翻倍。"""
    print("不是 JSON 就不说是截断")

    opts = OutlineOptions(max_tokens=8000, max_tokens_cap=32000, max_attempts=2)
    client = StubClient(lambda messages, budget: ("这是一段普通文字，没有 JSON，也没被截断。", "stop"))
    call = _call(client, "mock-flash", _BOOK_PROMPT, "各段梗概如下", opts)

    check(call.payload is None, "解析不出大纲")
    check(not call.truncated and not call.salvaged, "没有被误判成截断")
    check("不是 JSON" in call.error, f"原因说的是「不是 JSON」（实际 {call.error[:30]}）")
    check(set(client.seen_max_tokens) == {8000}, f"预算没有无谓地翻倍（实际 {client.seen_max_tokens}）")


def test_salvaged_outline_is_marked() -> None:
    """抢救出来的大纲必须显式标注可能缺字段，不能冒充完整产物。"""
    print("抢救出来的大纲在产物和渲染里都标出来")
    prepare()
    plan = plan_for(block_size=3)

    def responder(messages, budget):
        system = str(messages[0].get("content") or "")
        if "structure" in system:  # 书级归约的提示词里有 structure
            return BOOK_TRUNCATED, "length"
        return json.dumps({"summary": "（模拟）段落梗概"}, ensure_ascii=False), "stop"

    client = StubClient(responder)
    result = generate_outline(
        plan=plan,
        client=client,
        annotations_dir=ANNOTATIONS,
        opts=OutlineOptions(block_size=3, max_tokens=8000, max_attempts=1),
    )

    check(result.outline is not None, "仍然产出了大纲（有总比没有强）")
    meta = (result.outline or {}).get("_meta") or {}
    check(meta.get("salvaged") is True, "元信息里记了「是抢救出来的」")
    check(any("截断" in e for e in result.errors), "错误列表里说清了真实病根")
    md = render_markdown(WORK, result, plan)
    check("抢救" in md, "渲染出的 Markdown 里也标了")


def main() -> int:
    print("=" * 58)
    print("大纲链路自检")
    print("=" * 58)

    for fn in (
        test_primitive_p22,
        test_no_summaries_is_explicit,
        test_missing_partial_is_reported,
        test_missing_reasons_are_distinguished,
        test_blocking_and_estimate,
        test_generate_outline,
        test_failed_block_is_not_faked,
        test_block_label_with_restart,
        test_resume_from_state,
        test_book_reduction_escalates_budget,
        test_salvage_when_budget_exhausted,
        test_non_json_is_not_called_truncation,
        test_salvaged_outline_is_marked,
        test_markdown_and_save,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(False, f"{fn.__name__} 抛出异常：{type(exc).__name__}: {exc}")
        print()

    shutil.rmtree(TMP, ignore_errors=True)
    print("=" * 58)
    if _failures:
        print(f"未通过 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
