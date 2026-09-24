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
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.outline import (  # noqa: E402
    OutlineOptions,
    _block_label,
    build_outline_plan,
    collect_summaries,
    generate_outline,
    render_markdown,
    save_outline,
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


def main() -> int:
    print("=" * 58)
    print("大纲链路自检")
    print("=" * 58)

    for fn in (
        test_primitive_p22,
        test_no_summaries_is_explicit,
        test_missing_partial_is_reported,
        test_blocking_and_estimate,
        test_generate_outline,
        test_failed_block_is_not_faked,
        test_block_label_with_restart,
        test_resume_from_state,
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
