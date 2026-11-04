"""生成可靠性自检（P0 批次 2：B1/B2/B3/A2/E3/E6）。

跑：python tests/test_generation_reliability.py

这里不测「模型写得好不好」——那是作者的工作。这里只测机械契约：
  · 截断续写真的触发、有轮数上限、拼接处没重复；
  · 细纲能落盘、能复用、指令一改就作废；
  · 失败/中断只留候选，不覆盖正式稿；候选能转正；
  · 取消信号能穿透到调用层；重启后任务不会再显示「还在跑」；
  · 每类错误都有 code + 可读文案 + 建议动作。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from server import tasks  # noqa: E402
from server.services import Paths  # noqa: E402
from workshop import beats, draft_store  # noqa: E402
from workshop.draft import (  # noqa: E402
    MAX_CONTINUE_ROUNDS,
    generate_draft,
    load_draft,
    save_draft,
)
from workshop.errors import ErrorKind, GenerationCancelled, describe_error  # noqa: E402
from workshop.llm import StreamResult, Usage  # noqa: E402
from workshop.text_overlap import dedupe_continuation  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


class StubClient:
    """按顺序回放预设的流式结果。每次调用都记下来，供断言轮次。"""

    secrets = ["sk-test"]

    def __init__(self, results: list[StreamResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def chat_stream(self, model, messages, max_tokens=8, temperature=0.0, extra=None,
                    cancel_event=None):
        self.calls.append({
            "model": model, "messages": messages,
            "max_tokens": max_tokens, "cancel_event": cancel_event,
        })
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("已取消")
        if not self.results:
            raise AssertionError("StubClient 的结果用完了——续写轮数超出预期")
        return self.results.pop(0)


def _usage(p: int, c: int) -> Usage:
    return Usage(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


def test_dedupe() -> None:
    print("\n[1] n-gram 去重")
    prev = "……他推开门。屋里没有灯。"
    cont = "屋里没有灯。他摸到桌边，点燃火折子，看见墙角蹲着一个人。"
    cleaned, stats = dedupe_continuation(prev, cont)
    check(not cleaned.startswith("屋里没有灯"), "续写开头复读上一轮尾巴被裁掉")
    check(cleaned.startswith("他摸到桌边"), "裁掉重叠后从新内容开始")
    check(stats["overlap_removed"] >= 6, "去重统计记录裁掉的字数")

    unrelated = "与此同时，院外传来急促的敲门声。"
    cleaned2, stats2 = dedupe_continuation(prev, unrelated)
    check(cleaned2 == unrelated and stats2["overlap_removed"] == 0, "不重叠就不动正文")

    para = "这是一段足够长的描写，用来验证连续重复段落会被去掉。\n\n" \
           "这是一段足够长的描写，用来验证连续重复段落会被去掉。\n\n他继续赶路。"
    cleaned3, stats3 = dedupe_continuation("", para)
    check(cleaned3.count("这是一段足够长的描写") == 1, "段内连续重复段落只留一份")
    check(stats3["paragraphs_deduped"] == 1, "段内去重计数正确")


def test_generate_continuation() -> None:
    print("\n[2] 截断自动续写 + 轮数上限 + 拼接不重复")
    first = "他推开门。屋里没有灯。"
    second = "屋里没有灯。他摸到桌边，点燃火折子，看见墙角蹲着一个人。"
    third = "墙角那个人抬起头，竟然是他自己。"
    client = StubClient([
        StreamResult(text=first, finish_reason="length", usage=_usage(100, 20), total_ms=500),
        StreamResult(text=second, finish_reason="stop", usage=_usage(150, 40), total_ms=700),
    ])
    result = generate_draft(
        client=client, model_id="m", messages=[{"role": "user", "content": "写"}],
        max_tokens=2000, max_attempts=1,
    )
    check(result.continued and result.continue_rounds == 1, "首轮截断后自动续写 1 轮")
    check(not result.truncated, "续写后正常结束，不再标记截断")
    check(result.text.count("屋里没有灯") == 1, "拼接处没有复读上一轮尾巴")
    check(result.text == first + "他摸到桌边，点燃火折子，看见墙角蹲着一个人。",
          "最终正文 = 首轮 + 去重后的续写")
    check(len(result.calls) == 2, "两次调用都进了调用账")
    check(result.calls[1]["dedupe"]["overlap_removed"] >= 6, "续写轮记录了去重字数")
    check(result.calls[0]["finish_reason"] == "length", "首轮 finish_reason 被记录")
    check(result.calls[0]["elapsed_ms"] == 500, "每轮耗时被记录")

    # 一直截断：必须有上限，且不能把没写完的半章当成功
    all_truncated = [
        StreamResult(text=f"第{i}段内容。" * 10, finish_reason="length",
                     usage=_usage(100, 20), total_ms=300)
        for i in range(MAX_CONTINUE_ROUNDS + 2)
    ]
    client2 = StubClient(all_truncated)
    limited = generate_draft(
        client=client2, model_id="m", messages=[{"role": "user", "content": "写"}],
        max_tokens=2000, max_attempts=1, max_continue_rounds=MAX_CONTINUE_ROUNDS,
    )
    check(limited.hit_continue_limit, "续写撞上轮数上限")
    check(limited.truncated and limited.error_code == ErrorKind.TRUNCATED.value,
          "上限后仍截断 → 明确标记没写完")
    check(len(limited.calls) == MAX_CONTINUE_ROUNDS + 1, "调用次数 = 首轮 + 上限轮数")
    check(len(client2.results) == 1, "撞上限后不再发起多余调用")

    # 取消要在调用层真正穿透
    import threading

    event = threading.Event()
    event.set()
    client3 = StubClient([StreamResult(text="不该被调用")])
    try:
        generate_draft(
            client=client3, model_id="m", messages=[{"role": "user", "content": "写"}],
            cancel_event=event,
        )
        check(False, "取消后不应继续调用模型")
    except GenerationCancelled as exc:
        check(not client3.calls, "取消置位时一次模型调用都没发起")
        check(isinstance(exc.partial_text, str), "取消异常带 partial_text 字段")


def test_beats() -> None:
    print("\n[3] 写前细纲：解析/落盘/复用/指令变更作废")
    raw = json.dumps({"beats": [
        {"no": 1, "title": "进门", "goal": "主角推门看见空屋", "must": ["主角", "空屋"]},
        {"no": 2, "title": "点灯", "goal": "摸到桌边点燃火折子", "must": ["火折子"]},
        {"no": 3, "title": "发现", "goal": "墙角蹲着一个人", "must": ["墙角的人"]},
        {"no": 4, "title": "对峙", "goal": "对方抬头，身份出乎意料"},
        {"no": 5, "title": "钩子", "goal": "章末留下下一章的悬念"},
    ]}, ensure_ascii=False)
    parsed = beats.parse_beats("```json\n" + raw + "\n```")
    check(len(parsed) == 5, "JSON 细纲解析出 5 个节拍")
    check(parsed[0]["must"] == ["主角", "空屋"], "节拍里的 must 字段保留")

    fallback = beats.parse_beats("1. 主角推门\n2. 屋里没人\n3. 发现墙角有人")
    check(len(fallback) == 3, "JSON 失败时按编号行兜底解析")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "作品"
        brief_a = {"chapter_id": "v01-c001", "one_line": "进门发现有人", "core_plot": ["进门", "发现"]}
        meta = beats.save_beats(work, "v01-c001", beats=parsed, brief=brief_a, model="m")
        check(Path(meta["path"]).exists(), "细纲落盘到 90-draft 下的 .beats.json")
        reused, saved = beats.load_reusable_beats(work, "v01-c001", brief_a)
        check(len(reused) == 5 and saved is not None, "指令没变时复用已落盘细纲")

        brief_b = dict(brief_a, one_line="进门发现的是另一个人")
        reused2, _ = beats.load_reusable_beats(work, "v01-c001", brief_b)
        check(not reused2, "指令改过之后旧细纲作废，不会拿旧计划写新章")

    check(beats.render_beats_for_prompt(parsed).startswith("【写前细纲】"), "细纲能渲染成正文提示词块")


def test_draft_store() -> None:
    print("\n[4] 历史版本与恢复候选")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "作品"
        save_draft(work, "v01-c001", text="第一版正文。", meta={"model": "m"})
        for i in range(2, 5):
            draft_store.snapshot_version(
                work, "v01-c001", text=load_draft(work, "v01-c001")["text"],
                reason=f"第 {i} 次重写前备份",
            )
            save_draft(work, "v01-c001", text=f"第 {i} 版正文。", meta={"model": "m"})
        versions = draft_store.list_versions(work, "v01-c001")
        check(len(versions) == 3, "连续重写后留下 v1–v3 三份历史版本")
        check(all(v["chars"] > 0 for v in versions), "每份版本都有字数")

        before = load_draft(work, "v01-c001")
        cand = draft_store.save_candidate(
            work, "v01-c001", text="断网时写出的半截正文。", reason="网络不可达",
        )
        after = load_draft(work, "v01-c001")
        check(cand is not None and cand["candidate_id"], "失败时留下候选")
        check(after["text"] == before["text"], "候选不覆盖正式稿，正文 hash 不变")
        check(len(draft_store.list_candidates(work, "v01-c001")) >= 1, "候选可列出")

        promoted = draft_store.promote_candidate(work, "v01-c001", cand["candidate_id"])
        check(load_draft(work, "v01-c001")["text"] == "断网时写出的半截正文。", "候选可一键转正")
        check(promoted["chars"] == len("断网时写出的半截正文。"), "转正返回字数")

        vid = versions[-1]["version_id"]
        restored = draft_store.restore_version(work, "v01-c001", vid)
        check(restored["version_id"] == vid, "历史版本可恢复")
        restored_text = draft_store.load_version(work, "v01-c001", vid)["text"]
        check(load_draft(work, "v01-c001")["text"] == restored_text, "恢复后正文与版本一致")


def test_errors() -> None:
    print("\n[5] 错误码 / 文案 / 建议动作")
    auth = describe_error(ErrorKind.AUTH_FAILED, "401")
    check(auth["code"] == "auth_failed" and auth["retryable"] is False,
          "401 有错误码且标记不重试")
    timeout = describe_error(ErrorKind.NETWORK_TIMEOUT)
    check(timeout["retryable"] is True and "候选" in str(timeout["action"]),
          "超时给出重试与候选提示")
    trunc = describe_error(ErrorKind.TRUNCATED)
    check(trunc["code"] == "truncated" and "max_tokens" in str(trunc["action"]),
          "截断有独立错误码与可执行建议")
    check(describe_error("cancelled")["retryable"] is False, "取消不是故障，不建议重试")


def test_tasks() -> None:
    print("\n[6] 任务注册表 / 取消 / 重启中断")
    with tempfile.TemporaryDirectory() as tmp:
        paths = Paths(root=Path(tmp))
        paths.workspaces.mkdir(parents=True, exist_ok=True)
        task = tasks.create(paths, work="作品", chapter_id="v01-c001", total_stages=4, max_rounds=3)
        check(task.status == "running", "新任务是 running")
        tasks.update(task, stage="正文生成", stage_index=2, round=1, chars=123)
        check(tasks.get(paths, task.task_id).stage == "正文生成", "进度可查且已落盘")

        cancelled = tasks.request_cancel(paths, task.task_id)
        check(cancelled["status"] == "cancelling", "取消请求进入 cancelling")
        check(task.cancel_event.is_set(), "取消置位了传给模型层的 cancel_event")

        # 模拟进程死亡后重启：磁盘上还是 running，启动扫描要把它标成 interrupted
        task2 = tasks.create(paths, work="作品", chapter_id="v01-c002", total_stages=4)
        tasks.update(task2, stage="正文生成", stage_index=2)
        count = tasks.mark_interrupted(paths)
        check(count >= 1, "启动扫描标记了未完成任务")
        check(tasks.get(paths, task2.task_id).status == "interrupted", "重启后不再显示 running")
        latest = tasks.latest(paths, "作品", "v01-c002")
        check(latest is not None and latest["status"] == "interrupted", "最近任务状态可恢复")


def main() -> int:
    test_dedupe()
    test_generate_continuation()
    test_beats()
    test_draft_store()
    test_errors()
    test_tasks()
    print("\n" + ("全部通过" if not _failures else f"{len(_failures)} 项失败"))
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
