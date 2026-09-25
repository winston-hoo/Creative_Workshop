"""实体统计链路的自检。

不需要真实作品、不联网。直接跑：
    python tests/test_entities.py

每条断言都对应一次真实的失败模式（一部 545 章连载的真实运行）：

  · 11 块里 9 块被 max_tokens=4000 截断，整块实体被丢掉
  · 截断后原样重试三次，每次都在同一处截断，白烧 448 万 token
  · 报出来的原因是「返回内容不是 JSON」，与真实病根无关
  · 长任务只能一口气跑完，中途停下就前功尽弃
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.batch import load_chapter_tasks  # noqa: E402
from workshop.entities import (  # noqa: E402
    CallOutcome,
    EntitiesOptions,
    _call,
    _merge,
    build_entities_plan,
    generate_entities,
    load_aliases,
)
from workshop.llm import ChatResult, repair_truncated_json  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


# 真实截断长得就是这样：JSON 写到一半，最后一条没写完。
TRUNCATED_TEXT = (
    "{\n"
    '  "characters": [\n'
    '    {"name": "王铁柱", "role": "主角", "identity": "星闪大学学生", "first_chapter": 1},\n'
    '    {"name": "林小满", "role": "配角", "identity": "同班同学", "first_chapter": 3},\n'
    '    {"name": "屈老二", "role": "配角", "identity": "异能协会"\n'
)

COMPLETE_PAYLOAD = {
    "characters": [
        {"name": "王铁柱", "role": "主角", "identity": "星闪大学学生", "first_chapter": 1},
        {"name": "林小满", "role": "配角", "identity": "同班同学", "first_chapter": 3},
        {"name": "屈老二", "role": "配角", "identity": "异能协会成员", "first_chapter": 7},
    ],
    "factions": [{"name": "异能协会", "stance": "官方机构", "first_chapter": 7}],
    "abilities": [{"name": "隔空拳", "holder": "王铁柱", "effect": "隔空发力", "first_chapter": 2}],
    "locations": [{"name": "星闪大学", "note": "学校", "first_chapter": 1}],
}
COMPLETE_TEXT = json.dumps(COMPLETE_PAYLOAD, ensure_ascii=False)


class StubClient:
    """假客户端：按顺序吐预设的 (文本, finish_reason)，并记下每次请求的 max_tokens。

    finish_reason 必须逐条显式给出：「模型把话说完了但没给 JSON」和
    「被 max_tokens 砍断」是两回事，靠文本猜会把后者的判断逻辑测歪。
    """

    def __init__(self, turns: list[tuple[str, str]]) -> None:
        self.turns = turns
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
        index = min(len(self.seen_max_tokens) - 1, len(self.turns) - 1)
        text, finish = self.turns[index]
        return ChatResult(text=text, finish_reason=finish)


def _make_tasks(tmp: Path, chapters: int, block_size: int) -> tuple[list, object]:
    """造一份最小的作品工作区：manifest + 章节文件。"""
    ingest = tmp / "00-ingest"
    (ingest / "chapters").mkdir(parents=True, exist_ok=True)
    items = []
    for no in range(1, chapters + 1):
        cid = f"v001-c{no:04d}"
        (ingest / "chapters" / f"{cid}.md").write_text(
            f"---\nid: {cid}\nchapter_no: {no}\n---\n\n第{no}章 夜行\n正文内容。\n",
            encoding="utf-8",
        )
        items.append(
            {"id": cid, "vol_no": 1, "chapter_no": no, "title": "夜行", "char_count": 12}
        )
    (ingest / "manifest.json").write_text(
        json.dumps({"chapters": items}, ensure_ascii=False), encoding="utf-8"
    )
    tasks = load_chapter_tasks(ingest)
    plan = build_entities_plan(
        work="演示作品",
        provider_id="mock",
        model_id="mock-flash",
        tasks=tasks,
        block_size=block_size,
    )
    return tasks, plan


# ── 截断抢救 ────────────────────────────────────────────────


def test_repair_truncated_json() -> None:
    """回归：截断的 JSON 曾整块丢弃，报出来的原因还是「不是 JSON」。

    截断点之前的部分本身是合法的，能救回来多少就该救回多少。
    """
    print("截断 JSON 的抢救")

    repaired = repair_truncated_json(TRUNCATED_TEXT)
    check(repaired is not None, "截断的 JSON 被救回来了")
    if repaired:
        names = [c.get("name") for c in repaired.get("characters") or []]
        check(names == ["王铁柱", "林小满"], f"只保留写完整的人物（实际 {names}）")
        check("屈老二" not in json.dumps(repaired, ensure_ascii=False), "没写完的那条不要")
        check(repaired.get("factions") is None, "本来就没写到的字段不会凭空出现")

    check(repair_truncated_json('{"characters": [{"name": "甲') is None, "一个完整元素都没有时不硬凑")
    check(repair_truncated_json(COMPLETE_TEXT) is None, "完整的 JSON 走正常解析，不需要抢救")
    check(repair_truncated_json('{"characters": [}') is None, "括号已闭合但内容非法时不当截断处理")


# ── 输出预算 ────────────────────────────────────────────────


def test_truncation_escalates_budget() -> None:
    """回归：截断后原样重试三次，同一处截断，白烧 token。

    现在撞上截断就加大输出预算重试，能拿完整结果就不要退而求其次。
    """
    print("截断时加大输出预算重试")

    opts = EntitiesOptions(max_tokens=8000, max_tokens_cap=32000, max_attempts=3)
    client = StubClient([(TRUNCATED_TEXT, "length"), (COMPLETE_TEXT, "stop")])
    outcome = _call(client, "mock-flash", "抽实体", opts)

    check(isinstance(outcome, CallOutcome), "返回结构化的结果而不是裸三元组")
    check(client.seen_max_tokens == [8000, 16000], f"第二次把预算翻倍（实际 {client.seen_max_tokens}）")
    check(outcome.payload is not None and len(outcome.payload["characters"]) == 3, "最终拿到完整结果")
    check(not outcome.salvaged, "拿到完整结果就不标记为「只抢救出部分」")
    check(outcome.error == "", "没有错误")


def test_salvage_when_budget_exhausted() -> None:
    """预算加到顶还是截断时，保住截断前的部分，而不是整块丢掉。"""
    print("预算用尽时从截断处抢救")

    opts = EntitiesOptions(max_tokens=8000, max_tokens_cap=32000, max_attempts=3)
    client = StubClient([(TRUNCATED_TEXT, "length")])
    outcome = _call(client, "mock-flash", "抽实体", opts)

    check(client.seen_max_tokens == [8000, 16000, 32000], f"预算按 2 倍递增到上限（实际 {client.seen_max_tokens}）")
    check(outcome.salvaged, "标记为「只抢救出部分」，不假装完整")
    check(
        [c.get("name") for c in (outcome.payload or {}).get("characters") or []]
        == ["王铁柱", "林小满"],
        "截断前的人物保住了",
    )
    check("截断" in outcome.notice, "说明里讲清了是截断，而不是含糊的「失败」")


def test_non_json_is_not_called_truncation() -> None:
    """反向的误判也要防：普通文字不能被打上「截断」的标签。"""
    print("不是 JSON 就不说是截断")

    opts = EntitiesOptions(max_tokens=8000, max_tokens_cap=32000, max_attempts=2)
    client = StubClient([("这是一段普通文字，没有 JSON，也没被截断。", "stop")])
    outcome = _call(client, "mock-flash", "抽实体", opts)

    check(outcome.payload is None, "解析不出实体")
    check(not outcome.truncated and not outcome.salvaged, "没有被误判成截断")
    check("不是 JSON" in outcome.error, f"原因说的是「不是 JSON」（实际 {outcome.error[:30]}）")
    check(outcome.max_tokens == 8000, "预算没有无谓地翻倍")


# ── 分次执行 ────────────────────────────────────────────────


def test_limit_marks_pending_then_resumes() -> None:
    """回归：长任务只能一口气跑完。

    现在可以只跑一部分，没轮到的块显式标成 pending（不能留个空结果
    假装「这一段没有实体」），下次接着跑，已完成的块不重复花钱。
    """
    print("分次执行：未跑的块标 pending，下次接着跑")

    with tempfile.TemporaryDirectory(prefix="entities-test-") as raw:
        tmp = Path(raw)
        tasks, plan = _make_tasks(tmp, chapters=3, block_size=1)
        state_path = tmp / "50-entities" / "_state.json"

        first_client = StubClient([(COMPLETE_TEXT, "stop")])
        first = generate_entities(
            plan=plan, tasks=tasks, client=first_client, state_path=state_path, limit=1
        )

        check(len(first["blocks"]) == 3, f"三块都出现在结果里（实际 {len(first['blocks'])}）")
        check(
            [b.get("range") for b in first["blocks"] if b.get("pending")] == ["第 2 章", "第 3 章"],
            "没跑的两块被标成 pending",
        )
        check(len(first_client.seen_max_tokens) == 1, "本次只发起了一次调用")
        check(first["merged"]["counts"]["characters"] == 3, "已跑的块照常归并")
        check("pending" in json.dumps(first["blocks"], ensure_ascii=False), "pending 标记进了结果文件")

        second_client = StubClient([(COMPLETE_TEXT, "stop")])
        second = generate_entities(
            plan=plan, tasks=tasks, client=second_client, state_path=state_path
        )
        check(len(second_client.seen_max_tokens) == 2, "第二次只跑剩下两块，已完成的块不重复花钱")
        check(second["pending"] == [], "全部跑完后没有 pending")
        check(second["merged"]["counts"]["characters"] == 3, "归并里没有重复人物")


def test_stop_midway_keeps_finished_blocks() -> None:
    """回归：「停止」曾只能整本重来。

    停下来时已完成的块要保留，剩下的标 pending，下次接着跑。
    """
    print("中途停止：已完成的块保留")

    with tempfile.TemporaryDirectory(prefix="entities-test-") as raw:
        tmp = Path(raw)
        tasks, plan = _make_tasks(tmp, chapters=3, block_size=1)
        state_path = tmp / "50-entities" / "_state.json"

        calls = {"n": 0}

        def should_stop() -> bool:
            # 第一块跑完之后喊停
            return calls["n"] >= 1

        client = StubClient([(COMPLETE_TEXT, "stop")])

        def chat(*args, **kwargs):
            calls["n"] += 1
            return StubClient.chat(client, *args, **kwargs)

        client.chat = chat  # type: ignore[method-assign]
        result = generate_entities(
            plan=plan, tasks=tasks, client=client, state_path=state_path, should_stop=should_stop
        )

        check(result["stopped"], "结果里记下了「是被停下来的」")
        check(len(client.seen_max_tokens) == 1, "停下后不再发起调用")
        done = [b for b in result["blocks"] if not b.get("error") and not b.get("pending")]
        check(len(done) == 1, f"已完成的那一块保留（实际 {len(done)}）")
        check(result["pending"] == ["第 2 章", "第 3 章"], f"其余标 pending（实际 {result['pending']}）")
        check(result["merged"]["counts"]["characters"] == 3, "已完成的块照常归并")


def test_alias_merge_reaches_every_table() -> None:
    """别名归并要落到**每一张**表，漏一处就等于没并。

    真实失败模式：一部 545 章的连载里，张老师 / 张老鳖 / 张教授 是同一个人
    （正文第 1、28、303 章都写明了），却在人物表里是三张卡，
    并且在人物关系、势力成员、能力持有者里各算一份。
    """
    print("别名归并：同一人物的多个叫法并成一张卡")

    blocks = [
        {
            "range": "第 1-50 章",
            "error": "",
            "characters": [
                {
                    "name": "雷老师",
                    "role": "重要配角",
                    "identity": "星闪大学异能系教师",
                    "first_chapter": 47,
                    "factions": ["星闪大学"],
                    "abilities": ["雷法"],
                    "relations": [{"to": "雷正刚", "type": "同一人", "note": ""}],
                }
            ],
        },
        {
            "range": "第 51-97 章",
            "error": "",
            "characters": [
                {
                    "name": "雷正刚",
                    "role": "重要配角",
                    "identity": "星闪大学异能系老师、辅导员",
                    "first_chapter": 46,
                    "factions": [],
                    "abilities": ["雷法"],
                    "relations": [],
                },
                {
                    "name": "王铁柱",
                    "role": "主角",
                    "identity": "星闪大学学生",
                    "first_chapter": 1,
                    "factions": ["星闪大学"],
                    "abilities": [],
                    "relations": [],
                },
            ],
        },
    ]

    merged = _merge(blocks, {"雷正刚": "雷老师"})
    chars = {c["name"]: c for c in merged["characters"]}
    check(set(chars) == {"雷老师", "王铁柱"}, f"两张卡并成一张（实际 {sorted(chars)}）")
    check(chars["雷老师"]["aliases"] == ["雷正刚"], "别名挂回卡片上，可核对并进了谁")
    check(chars["雷老师"]["identity"].endswith("辅导员"), "身份取更完整的那条")
    check(
        merged["factions"][0]["members"] == ["雷老师", "王铁柱"],
        f"势力成员只用正名（实际 {merged['factions'][0]['members']}）",
    )
    check(
        merged["abilities"][0]["holders"] == ["雷老师"],
        f"能力持有者只用正名、且不重复（实际 {merged['abilities'][0]['holders']}）",
    )
    check(
        not any("雷正刚" in (r["from"], r["to"]) for r in merged["relations"]),
        f"关系表里不留别名（实际 {merged['relations']}）",
    )

    plain = _merge(blocks)
    check(len(plain["characters"]) == 3, "不给别名表时行为不变（不猜、不并）")

    with tempfile.TemporaryDirectory() as tmp:
        check(load_aliases(Path(tmp) / "aliases.yaml") == {}, "没有别名表就返回空表，不是错误")
        path = Path(tmp) / "aliases.yaml"
        path.write_text(
            "aliases:\n"
            "  - canonical: 雷老师\n"
            "    also: [雷正刚, '  雷教官  ']\n"
            "    origin: corpus\n"
            "  - canonical: 空条目\n"
            "  - canonical: 自己并自己\n"
            "    also: [自己并自己]\n",
            encoding="utf-8",
        )
        table = load_aliases(path)
        check(table == {"雷正刚": "雷老师", "雷教官": "雷老师"}, f"别名表读对了（实际 {table}）")
        check(load_aliases(Path(tmp) / "坏.yaml") == {}, "文件不存在时不抛异常")


def main() -> int:
    print("=" * 58)
    print("实体统计链路自检")
    print("=" * 58)

    for fn in (
        test_repair_truncated_json,
        test_truncation_escalates_budget,
        test_salvage_when_budget_exhausted,
        test_non_json_is_not_called_truncation,
        test_limit_marks_pending_then_resumes,
        test_stop_midway_keeps_finished_blocks,
        test_alias_merge_reaches_every_table,
    ):
        fn()
        print()

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