"""正文审稿闭环自检（P0 批次 3：D1 / D2 / D3 / D5 / A3）。

跑：python tests/test_review_cycle.py

这里查的四件事，都是「界面看起来正常但其实是假的」高危区：
  1. 意见的 evidence 能不能**真的**在正文里唯一对上；
  2. 采纳是不是只动了勾中的那几段（其余字节必须一致）；
  3. 拒绝、采纳、重复审稿有没有留下可查的记录；
  4. 状态闸门是不是真的会拒绝（未审不能定稿、定稿不能悄悄重写）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from workshop import draft_store, refine, review  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


PROSE = (
    "他站在山门前，手心全是汗。\n\n"
    "山风卷着碎石，砸在铁栏上叮当作响。\n\n"
    "他站在原地，迟迟没有迈步。\n\n"
    "远处传来钟声，一共九下。\n\n"
    "他站在原地，迟迟没有迈步，直到钟声散尽。\n"
)


def _sha(text: str) -> str:
    return review.sha256(text)


def _review_entry(
    prose: str, items: list[dict], rid: str = "20261104T900000-aaaaaa",
    created_at: str = "2026-11-04T09:00:00+08:00",
) -> dict:
    return {
        "review_id": rid,
        "created_at": created_at,
        "model": "test-model",
        "provider": "test",
        "prose_sha": _sha(prose),
        "chars": len(prose),
        "summary": "测试审稿",
        "items": items,
        "invalid": [],
        "attempts": [],
        "attempt_count": 1,
        "usage": {},
    }


def _item(evidence: str, suggestion: str, prose: str, *, status: str = "open") -> dict:
    spot = review.locate_evidence(prose, evidence)
    return {
        "id": review._finding_id(evidence, suggestion),
        "index": 0,
        "type": "language",
        "severity": "major",
        "evidence": evidence,
        "start": spot["start"],
        "end": spot["end"],
        "paragraph": spot["paragraph"],
        "problem": "测试问题",
        "suggestion": suggestion,
        "locatable": True,
        "status": status,
        "repeat_of": "",
        "status_at": "",
        "reject_reason": "",
    }


# ── D1：定位与校验 ──────────────────────────────────────────


def test_locate_unique_and_reject_ambiguous() -> None:
    print("D1 · 引文定位：唯一才对，重复必须拒绝")
    spot = review.locate_evidence(PROSE, "山风卷着碎石，砸在铁栏上叮当作响。")
    check(spot is not None, "唯一引文能定位")
    check(PROSE[spot["start"]:spot["end"]] == "山风卷着碎石，砸在铁栏上叮当作响。",
          "偏移与原文逐字一致")
    check(spot["paragraph"] == 2, f"段号正确（实际 {spot['paragraph']}）")

    dup = review.locate_evidence(PROSE, "他站在原地，迟迟没有迈步")
    check(dup is None, "出现两次的引文默认拒绝定位")

    hinted = review.locate_evidence(PROSE, "他站在原地，迟迟没有迈步", paragraph_hint=3)
    check(hinted is not None and hinted["paragraph"] == 3, "带段号时可以钉到第 3 段")
    check(hinted is not None and PROSE[hinted["start"]:hinted["end"]] == "他站在原地，迟迟没有迈步",
          "钉住的仍是逐字原文")

    missing = review.locate_evidence(PROSE, "他从怀里掏出一枚青铜令牌")
    check(missing is None, "正文里没有的引文定位失败")
    short = review.locate_evidence(PROSE, "钟声")
    check(short is None, "过短引文拒绝定位")


def test_normalize_items_reports_invalid() -> None:
    print("D1 · 解析：好的收下，坏的写清原因，不静默丢")
    raw = [
        {"type": "language", "severity": "minor",
         "evidence": "山风卷着碎石，砸在铁栏上叮当作响。",
         "problem": "声音堆得太满", "suggestion": "山风贴着石阶掠过，铁栏轻轻颤了一下。"},
        {"type": "logic", "severity": "blocker",
         "evidence": "他从怀里掏出一枚青铜令牌", "problem": "凭空出现", "suggestion": "他摸了摸空空的腰间。"},
        {"type": "pacing", "evidence": "钟声", "problem": "太短", "suggestion": "远处的钟声敲了九下。"},
        {"type": "language", "evidence": "他站在原地，迟迟没有迈步", "problem": "重复",
         "suggestion": "他没有动。"},
        {"type": "other", "evidence": "远处传来钟声，一共九下。", "problem": "漏了建议"},
    ]
    items, invalid = review.normalize_items(raw, PROSE)
    check(len(items) == 1, f"只有 1 条可采纳（实际 {len(items)}）")
    check(items[0]["severity"] == "minor" and items[0]["locatable"] is True,
          "可采纳条目带严重度与定位")
    check(len(invalid) == 4, f"另外 4 条都进了 invalid（实际 {len(invalid)}）")
    reasons = " / ".join(str(x.get("reason")) for x in invalid)
    check("找不到" in reasons, "不存在的引文写明「找不到」")
    check("太短" in reasons, "过短引文写明「太短」")
    check("出现 2 次" in reasons, "重复引文写明出现次数")
    check("缺少改写建议" in reasons, "缺建议的条目也有原因")


def test_parse_and_retry_contract() -> None:
    print("D1 · JSON 解析：能剥围栏，坏 JSON 抛可读错误")
    data = review.extract_json_object('```json\n{"summary":"ok","items":[]}\n```')
    check(data["summary"] == "ok", "带围栏的 JSON 能剥出来")
    data = review.extract_json_object('说明一下：{"summary":"x","items":[]} 以上')
    check(data["summary"] == "x", "前后有废话也能抠出对象")
    try:
        review.extract_json_object("这不是 JSON")
    except review.ReviewPayloadError as exc:
        check(exc.code == "invalid_json", "坏 JSON 的错误码是 invalid_json")
        check("不是合法 JSON" in str(exc), "错误文案可读")
    else:
        check(False, "坏 JSON 没有抛错")


# ── D2/D3：采纳只动选中项 ───────────────────────────────────


def test_apply_only_selected_changes() -> None:
    print("D2 · 采纳：只替换勾中的，其余字节不变")
    a = _item("山风卷着碎石，砸在铁栏上叮当作响。",
              "山风贴着石阶掠过，铁栏轻轻颤了一下。", PROSE)
    b = _item("远处传来钟声，一共九下。", "远处传来钟声，九下，一下不多。", PROSE)
    c = _item("他站在原地，迟迟没有迈步。", "他站住，没有迈出去。", PROSE)
    result = refine.apply_findings(PROSE, [a, b], {})
    check(result["regions"] == 2, f"实际改写 2 处（实际 {result['regions']}）")
    check(result["text"].count("山风贴着石阶掠过，铁栏轻轻颤了一下。") == 1, "第一条建议进了正文")
    check("远处传来钟声，九下，一下不多。" in result["text"], "第二条建议进了正文")
    check("他站在原地，迟迟没有迈步。" in result["text"], "没勾中的第三条原样保留")
    check(refine.only_selected_changed(PROSE, result["text"], result["applied"]),
          "选中范围之外逐字未变")
    check(result["changed_chars"] == len("山风贴着石阶掠过，铁栏轻轻颤了一下。")
          - len(a["evidence"]) + len("远处传来钟声，九下，一下不多。") - len(b["evidence"]),
          "改写字数账算对")


def test_apply_author_override_and_stale_evidence() -> None:
    print("D3 · 作者改写建议 / 原文已变的条目要跳过")
    a = _item("远处传来钟声，一共九下。", "远处传来钟声，九下。", PROSE)
    custom = refine.apply_findings(PROSE, [a], {a["id"]: "钟声九下，散在夜色里。"})
    check("钟声九下，散在夜色里。" in custom["text"], "作者改过的文本优先生效")

    moved = PROSE.replace("远处传来钟声，一共九下。", "远处传来钟声，只响了三下。")
    stale = refine.apply_findings(moved, [a], {})
    check(stale["ok"] is False and stale["text"] == moved, "原文变了就不写，正文保持原样")
    check("找不到这句原文" in str(stale["skipped"][0]["reason"]), "跳过原因写清楚")


def test_apply_via_services_snapshots_and_persists() -> None:
    print("D2/A2 · 服务层采纳：写前快照、正文真变、状态入记录")
    from server import services

    with tempfile.TemporaryDirectory() as tmp:
        paths = services.Paths(Path(tmp))
        wd = paths.work_dir("关山灯")
        (wd / "90-draft").mkdir(parents=True)
        (wd / "90-draft" / "v001-c0001.md").write_text(PROSE, encoding="utf-8")
        (wd / "90-draft" / "v001-c0001.json").write_text(
            json.dumps({"schema_version": "draft-v1", "chapter_id": "v001-c0001"}),
            encoding="utf-8",
        )
        a = _item("山风卷着碎石，砸在铁栏上叮当作响。",
                  "山风贴着石阶掠过，铁栏轻轻颤了一下。", PROSE)
        b = _item("远处传来钟声，一共九下。", "远处传来钟声，九下，一下不多。", PROSE)
        c = _item("他站在原地，迟迟没有迈步。", "他站住，没有迈出去。", PROSE)
        entry = _review_entry(PROSE, [a, b, c])
        review.append_review(wd, "v001-c0001", entry)
        draft_store.set_status(wd, "v001-c0001", "reviewed", reason="先审", by="test")

        out = services.apply_review_findings(
            paths, "关山灯", "v001-c0001", entry["review_id"], [a["id"], b["id"]],
        )
        new_text = (wd / "90-draft" / "v001-c0001.md").read_text(encoding="utf-8")
        check(out["ok"] is True and out["regions"] == 2, "接口回报改写了 2 处")
        check(new_text != PROSE, "磁盘上的正文真的变了")
        check("他站在原地，迟迟没有迈步。" in new_text, "没勾中的那条没被动")
        check(out["version_id"] != "", "写回前生成了版本快照")
        versions = draft_store.list_versions(wd, "v001-c0001")
        check(len(versions) == 1, f"版本目录里有 1 份（实际 {len(versions)}）")
        check(draft_store.load_version(wd, "v001-c0001", out["version_id"])["text"] == PROSE,
              "快照里是改写前的原文")
        check(out["status"] == "draft", "改写后状态退回草稿（需复审）")

        payload = review.load_reviews(wd, "v001-c0001")
        stored = review.find_review(payload, entry["review_id"])
        statuses = {it["id"]: it["status"] for it in stored["items"]}
        check(statuses[a["id"]] == "adopted" and statuses[b["id"]] == "adopted",
              "采纳记录写进了审稿文件")
        check(statuses[c["id"]] == "open", "没勾中的仍是 open")
        check(stored["items"][0]["applied_version_id"] == out["version_id"],
              "采纳记录指向具体版本")

        manifest = json.loads(
            (wd / "90-draft" / "v001-c0001.context.json").read_text(encoding="utf-8")
        )
        check(manifest.get("refine", {}).get("regions") == 2, "manifest 里留下改写摘要")


# ── 拒绝 / 重复 / 历史 ─────────────────────────────────────


def test_reject_persists_and_blocks_adoption() -> None:
    print("U4/A3 · 拒绝：落盘、留原因，不能再被采纳")
    from server import services

    with tempfile.TemporaryDirectory() as tmp:
        paths = services.Paths(Path(tmp))
        wd = paths.work_dir("关山灯")
        (wd / "90-draft").mkdir(parents=True)
        (wd / "90-draft" / "v001-c0001.md").write_text(PROSE, encoding="utf-8")
        a = _item("远处传来钟声，一共九下。", "远处传来钟声，九下。", PROSE)
        entry = _review_entry(PROSE, [a])
        review.append_review(wd, "v001-c0001", entry)

        out = services.reject_review_findings(
            paths, "关山灯", "v001-c0001", entry["review_id"], [a["id"]], "不想动这句",
        )
        check(len(out["rejected"]) == 1, "拒绝成功")
        stored = review.find_review(
            review.load_reviews(wd, "v001-c0001"), entry["review_id"])
        check(stored["items"][0]["status"] == "rejected", "拒绝状态已落盘")
        check(stored["items"][0]["reject_reason"] == "不想动这句", "拒绝原因已落盘")
        try:
            services.apply_review_findings(
                paths, "关山灯", "v001-c0001", entry["review_id"], [a["id"]],
            )
        except ValueError as exc:
            check("不能采纳" in str(exc), "被拒绝的条目不能再采纳")
        else:
            check(False, "被拒绝的条目居然还能采纳")


def test_duplicate_finding_after_adoption() -> None:
    print("A3 · 已采纳的意见再被审出来时标 duplicate，不进可采纳区")
    a = _item("远处传来钟声，一共九下。", "远处传来钟声，九下。", PROSE, status="adopted")
    raw = [
        {"type": "pacing", "evidence": "远处传来钟声，一共九下。",
         "problem": "钟声太满", "suggestion": "远处传来钟声，九下。"},
    ]
    items, invalid = review.normalize_items(raw, PROSE, existing_items=[a])
    check(len(items) == 1, "条目仍保留（能看见「已处理过」）")
    check(items[0]["status"] == "duplicate", "状态是 duplicate")
    check(items[0]["repeat_of"] == "adopted", "记录了它对应之前的采纳")
    public = review.public_items(_review_entry(PROSE, items), _sha(PROSE))
    check(public[0]["adoptable"] is False, "duplicate 不可采纳")


def test_history_and_stale() -> None:
    print("A3 · 历史可对比；正文改过后旧意见标 stale")
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "90-draft").mkdir()
        a = _item("远处传来钟声，一共九下。", "远处传来钟声，九下。", PROSE)
        first = _review_entry(PROSE, [a], rid="r1", created_at="2026-11-04T09:00:00+08:00")
        review.append_review(wd, "v001-c0001", first)
        second = _review_entry(PROSE, [], rid="r2", created_at="2026-11-04T10:00:00+08:00")
        review.append_review(wd, "v001-c0001", second)
        payload = review.load_reviews(wd, "v001-c0001")
        check(len(payload["reviews"]) == 2, "两次审稿都保留，不覆盖")
        rows = review.summarize(payload, _sha(PROSE))
        check(rows[0]["review_id"] == "r2", "历史按时间倒序，最新在前")
        check(rows[1]["counts"]["open"] == 1, "旧审稿的 open 数还在")

        changed = PROSE + "\n\n他又补了一句。\n"
        rows = review.summarize(payload, _sha(changed))
        check(rows[1]["stale"] is True, "正文改过的旧审稿标记 stale")
        check(rows[1]["counts"]["stale"] == 1, "挂着的条目计入 stale")
        public = review.public_items(first, _sha(changed))
        check(public[0]["status"] == "stale" and public[0]["adoptable"] is False,
              "stale 条目不可采纳")


# ── D5：状态机 ──────────────────────────────────────────────


def test_status_machine() -> None:
    print("D5 · 稿子状态机：未审不能定稿，定稿不能悄悄重写")
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "90-draft").mkdir()
        (wd / "90-draft" / "v001-c0001.md").write_text(PROSE, encoding="utf-8")
        check(draft_store.get_status(wd, "v001-c0001")["status"] == "draft",
              "没有状态文件时默认 draft")
        try:
            draft_store.set_status(wd, "v001-c0001", "final")
        except ValueError as exc:
            check("还没审过" in str(exc), "draft 直接跳到 final 被拒绝")
        else:
            check(False, "draft 居然能直接定稿")

        state = draft_store.set_status(wd, "v001-c0001", "reviewed", reason="审完")
        check(state["status"] == "reviewed", "draft → reviewed 允许")
        state = draft_store.set_status(wd, "v001-c0001", "final", reason="定稿")
        check(state["status"] == "final", "reviewed → final 允许")
        check(len(state["history"]) == 2, "两次迁移都有历史")
        check(state["history"][-1]["from"] == "reviewed", "历史记下从哪来")
        try:
            draft_store.set_status(wd, "v001-c0001", "reviewed")
        except ValueError as exc:
            check("已定稿" in str(exc), "final 状态下非法跳转有明确原因")
        else:
            check(False, "final 的非法跳转没报错")

        state = draft_store.set_status(wd, "v001-c0001", "draft", reason="重新打开")
        check(state["status"] == "draft", "final → draft 可以显式 reopen")

        from server import services

        paths = services.Paths(Path(tmp))
        work = paths.work_dir("关山灯")
        (work / "90-draft").mkdir(parents=True)
        (work / "90-draft" / "v001-c0001.md").write_text(PROSE, encoding="utf-8")
        try:
            services.set_draft_status(paths, "关山灯", "v001-c0001", "final")
        except ValueError as exc:
            check("还没审过" in str(exc), "服务层同样拦住未审定稿")
        else:
            check(False, "服务层居然允许未审定稿")


# ── 账本：审稿调用不能冲掉生成账 ────────────────────────────


def test_manifest_keeps_generation_calls() -> None:
    print("硬约束 · 审稿调用追加进 manifest，生成账一行不少")
    from server import services
    from workshop.context_budget import load_manifest, save_manifest

    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "90-draft").mkdir()
        report = {"schema_version": "context-budget-v1", "blocks": [], "used_chars": 100}
        save_manifest(wd, "v001-c0001", report, extra={
            "phase": "generated",
            "calls": [
                {"stage": "beats", "round": 0, "chars": 900, "finish_reason": "stop"},
                {"stage": "draft", "round": 1, "chars": 3200, "finish_reason": "length"},
            ],
        })
        services._append_manifest_call(
            wd, "v001-c0001",
            {"stage": "review", "round": 0, "chars": 3200, "finish_reason": "stop"},
            review_meta={"review_id": "r1", "items": 4},
        )
        manifest = load_manifest(wd, "v001-c0001")
        check(len(manifest["calls"]) == 3, "原来 2 条 + 审稿 1 条 = 3 条")
        check(manifest["calls"][0]["stage"] == "beats", "细纲那一行还在")
        check(manifest["calls"][1]["finish_reason"] == "length", "续写那一行还在")
        check(manifest["calls"][2]["stage"] == "review", "审稿那一行在最后")
        check(manifest.get("phase") == "generated", "生成阶段的 phase 没被覆盖")
        check(manifest.get("review", {}).get("items") == 4, "审稿摘要也写进去了")
        check(manifest.get("report") == report, "批次 1 的预算报告没被冲掉")


def main() -> int:
    tests = [
        test_locate_unique_and_reject_ambiguous,
        test_normalize_items_reports_invalid,
        test_parse_and_retry_contract,
        test_apply_only_selected_changes,
        test_apply_author_override_and_stale_evidence,
        test_apply_via_services_snapshots_and_persists,
        test_reject_persists_and_blocks_adoption,
        test_duplicate_finding_after_adoption,
        test_history_and_stale,
        test_status_machine,
        test_manifest_keeps_generation_calls,
    ]
    for fn in tests:
        fn()
        print()
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
