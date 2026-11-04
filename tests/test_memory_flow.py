"""记忆回流自检（P0 批次 4：A1/A5/B5/U8 + 两个批次 3 遗留修复）。

跑：python tests/test_memory_flow.py

这里查的是四条容易被做假的链路：

  1. 抽取出来的要点**必须逐条经作者裁决**才进下一章，丢弃的绝不进；
  2. 要点是**独立上下文块**，不是偷偷塞进前情；
  3. 新要点与旧状态冲突时**标出来**，接受后旧条目留历史，不静默覆盖；
  4. 已拒绝的审稿意见**不再重复提出**，但作者点「重新考虑」能解除抑制。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from server import services  # noqa: E402
from workshop import finalize as fin  # noqa: E402
from workshop import notes as nt  # noqa: E402
from workshop import summary as sm  # noqa: E402
from workshop.chapter_brief import chapter_id_of  # noqa: E402
from workshop.creation import render_recap, render_write_brief_report  # noqa: E402
from workshop.review import normalize_items  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def _setting() -> dict:
    return {
        "schema_version": "setting-v1", "work": "关山灯", "genre": "玄幻",
        "logline": "一个能看到数值的废柴。", "core_motive": "活着回去",
        "target": {"chapters": 12, "chars_per_chapter": [2800, 4000]},
        "style": {"perspective": "第三限知", "tone": "轻松搞笑", "taboo": []},
        "world": {"era": "天元三百年", "places": [{"name": "青云宗", "note": "第一大宗"}],
                  "rules": ["灵力用尽会昏迷"]},
        "power": {"system": "修为", "tiers": ["炼气", "筑基"]},
        "factions": [{"name": "青云宗", "stance": "表面中立", "leader": "玄阳真人"}],
        "characters": [
            {"name": "李默", "role": "主角", "identity": "穿越者", "motive": "活着回去",
             "faction": "青云宗"},
            {"name": "苏清月", "role": "重要配角", "identity": "大师姐",
             "faction": "青云宗"},
        ],
        "terms": [{"term": "数值之眼", "meaning": "能看到目标的修为数值"}],
    }


def _volume() -> dict:
    return {
        "schema_version": "volume-v1", "work": "关山灯",
        "vol": {
            "vol": 1, "title": "第一卷", "start_chapter": 1, "end_chapter": 4,
            "era": "天元三百年秋", "core_conflict": "自保 vs 扛事",
            "goal": "活下来", "closing": "卷末",
            "parts": [{"title": "觉醒", "start_chapter": 1, "end_chapter": 4,
                       "gist": "主角认清处境", "must_complete": [],
                       "ending_demand": "落在第一次选择上"}],
            "chapters": [
                {"chapter_no": n, "title": f"第{n}章标题", "gist": f"第{n}章发生了什么",
                 "characters": ["李默"]} for n in range(1, 5)
            ],
        },
    }


def _brief(chapter_no: int) -> dict:
    return {
        "schema_version": "brief-v1", "work": "关山灯",
        "chapter_id": chapter_id_of(1, chapter_no), "vol": 1, "chapter_no": chapter_no,
        "title": f"第{chapter_no}章标题", "one_line": "只干一件事",
        "core_plot": ["第一件事", "第二件事"], "timeline": "接上一章",
        "word_target": [2800, 4000],
        "narrative": {"perspective": "第三限知", "tone": "轻松搞笑", "focus": ["对话"]},
        "settings": {"characters": ["李默"], "factions": ["青云宗"], "terms": ["数值之眼"]},
        "foreshadow": [], "carry_over": [], "must_not": [],
    }


PROSE = (
    "李默睁开眼。\n\n"
    "他确认了一遍面板，数值之眼不是幻觉。\n\n"
    "他把门闩好，往后退了半步，心里记下：欠青云宗三块灵石。\n\n"
    "窗外有人踩着雪走过去，脚步很轻。\n"
)


def _write_work(root: Path) -> Path:
    wd = root / "关山灯"
    for sub in ("60-setting", "70-volume", "80-brief", "90-draft"):
        (wd / sub).mkdir(parents=True, exist_ok=True)
    (wd / "60-setting" / "setting.yaml").write_text(
        yaml.safe_dump(_setting(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    (wd / "70-volume" / "volume-001.yaml").write_text(
        yaml.safe_dump(_volume(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    for n in range(1, 5):
        (wd / "80-brief" / f"v001-c{n:04d}.yaml").write_text(
            yaml.safe_dump(_brief(n), allow_unicode=True, sort_keys=False), encoding="utf-8")
    (wd / "90-draft" / "v001-c0001.md").write_text(PROSE, encoding="utf-8")
    return wd


def _refs() -> list[dict[str, str]]:
    return nt.known_refs(_setting(), _brief(1))


# ── A1：结构化抽取 + 校验 + 冲突标记 ──────────────────────


def test_extract_validation_and_conflict() -> None:
    print("A1 · 要点抽取必须结构化、能对上号、不合格不入库")
    refs = _refs()
    raw = [
        {"type": "fact", "ref": "李默", "text": "李默确认数值之眼不是幻觉。",
         "evidence": "数值之眼不是幻觉"},
        {"type": "state", "ref": "李默", "text": "李默欠青云宗三块灵石。"},
        {"type": "foreshadow", "ref": "李默", "text": "窗外有人在踩雪。"},
        {"type": "fact", "ref": "王二", "text": "王二把门板卸走了。"},
        {"type": "fact", "ref": "李默", "text": "李默把门闩好。",
         "evidence": "把门栓好"},
    ]
    items, invalid = nt.normalize_notes(
        raw, PROSE, chapter_id="v001-c0001", vol=1, chapter_no=1, refs=refs,
        prior_notes=[], source={"prose_sha": nt.sha256(PROSE), "model": "test"},
    )
    check(len(items) == 2, "合格条目入库，不合格条目不进列表")
    check(all(x.get("chapter") == "v001-c0001" and x.get("vol") == 1 for x in items),
          "每条带章节与卷")
    check(all(x.get("ref") in ("李默", "苏清月") and x.get("type") for x in items),
          "每条带可关联的 ref 与类型")
    check(any("对不上" in x["reason"] for x in invalid if x.get("ref") == "王二"),
          "自造人名被明确拒绝（不是静默丢）")
    check(any("不是伏笔编号" in x["reason"] for x in invalid),
          "伏笔要点必须指向伏笔编号")
    check(any("找不到" in x["reason"] for x in invalid),
          "evidence 被改字导致定位不到时拒绝入库")

    old = {
        "id": "nt-old", "chapter": "v001-c0000", "chapter_no": 0, "ref": "李默",
        "type": "state", "text": "李默身上没有灵石，也没欠债。", "status": "accepted",
        "superseded_by": "", "history": [],
    }
    new_items, _ = nt.normalize_notes(
        [{"type": "state", "ref": "李默", "text": "李默欠青云宗三块灵石。"}],
        PROSE, chapter_id="v001-c0002", vol=1, chapter_no=2, refs=refs,
        prior_notes=[old], source={"prose_sha": "x"},
    )
    conflict = (new_items[0].get("conflict") or {}) if new_items else {}
    check(bool(conflict), "新状态与旧状态不同：标出冲突")
    check(conflict.get("with") == "nt-old" and conflict.get("text") == old["text"],
          "冲突里带旧条目 id 与旧说法原文，供作者裁决")
    check(new_items[0]["status"] == "pending" and old["text"] != new_items[0]["text"],
          "冲突时不自动覆盖旧状态")


# ── U8：逐条裁决决定「进不进下一章」────────────────────────


def test_note_decisions_control_context() -> None:
    print("U8 · 逐条接受/编辑/丢弃，决定哪些真的进下一章")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp))
        refs = _refs()
        items, _ = nt.normalize_notes(
            [
                {"type": "fact", "ref": "李默", "text": "李默确认数值之眼不是幻觉。"},
                {"type": "state", "ref": "李默", "text": "李默欠青云宗三块灵石。"},
                {"type": "foreshadow", "ref": "李默", "text": "窗外有人踩雪走过。"},
            ],
            PROSE, chapter_id="v001-c0001", vol=1, chapter_no=1, refs=refs,
            source={"prose_sha": nt.sha256(PROSE), "model": "test"},
        )
        # foreshadow 的 ref 不是伏笔编号，会被拒；这里手动补一个合法伏笔编号
        items = items[:2]
        items[1]["type"] = "foreshadow"
        items[1]["id"] = nt._note_id("v001-c0001", "v001-c0001-f01", items[1]["text"], "foreshadow")
        items[1]["ref"] = "v001-c0001-f01"
        items[1]["ref_kind"] = "foreshadow"
        items[1]["text"] = "窗外有人踩雪走过。"
        nt.save_extraction(
            wd, "v001-c0001", items=items, invalid=[], prose=PROSE,
            source={"prose_sha": nt.sha256(PROSE), "model": "test"},
        )
        nt.update_note(wd, "v001-c0001", items[0]["id"], action="accept")
        nt.update_note(wd, "v001-c0001", items[1]["id"], action="edit",
                       text="窗外有人踩雪走过，脚步很轻。")
        nt.update_note(wd, "v001-c0001", items[1]["id"], action="discard",
                       reason="这条先不记")

        block = nt.render_notes(wd, before_chapter_no=2)
        check(items[0]["text"] in block, "接受的事实进了下一章要点块")
        check("窗外有人踩雪走过，脚步很轻。" not in block,
              "被丢弃的条目没有进上下文")
        check("【已确认章节要点】" in block, "要点块有独立标题，不伪装成前情")

        payload = nt.load_notes(wd, "v001-c0001")
        edited = nt.find_note(payload, items[0]["id"])
        check(bool(edited.get("confirmed_by")) and bool(edited.get("confirmed_at")),
              "接受条目记录了谁在何时确认")
        hist = edited.get("history") or []
        check(any(h.get("action") == "accept" for h in hist), "裁决动作留历史")

        # 丢弃的条目可以退回，退回后仍不进上下文（只有接受/编辑才进）
        nt.update_note(wd, "v001-c0001", items[1]["id"], action="reset")
        check(nt.find_note(nt.load_notes(wd, "v001-c0001"), items[1]["id"])["status"] == "pending",
              "丢弃后可退回待确认（不永久封死）")
        check(items[1]["text"] not in nt.render_notes(wd, before_chapter_no=2),
              "退回待确认后仍不会自动进下一章")


def test_notes_independent_block_and_recap_fallback() -> None:
    print("A1/A5 · notes 是独立预算块；删掉后前情自动回退卷摘要")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp))
        items, _ = nt.normalize_notes(
            [{"type": "fact", "ref": "李默", "text": "李默确认数值之眼不是幻觉。"}],
            PROSE, chapter_id="v001-c0001", vol=1, chapter_no=1, refs=_refs(),
            source={"prose_sha": nt.sha256(PROSE)},
        )
        nt.save_extraction(wd, "v001-c0001", items=items, invalid=[], prose=PROSE,
                           source={"prose_sha": nt.sha256(PROSE)})
        nt.update_note(wd, "v001-c0001", items[0]["id"], action="accept")
        sm.build_snapshot(wd, "v001-c0001", vol=1, chapter_no=1)

        _, report = render_write_brief_report(
            wd, chapter_no=2, prev_chapter_no=1, brief=_brief(2), budget_chars=8000,
        )
        block = next(b for b in report["blocks"] if b["id"] == "notes")
        check(block["status"] == "included", "下一章装配包含独立的 notes 块")
        check(block["chars"] > 0 and "v001-c0001.notes.json" in block["source"]
              and "1 条已确认" in block["source"],
              "预算账本能看到 notes 块字数、具体来源章与条数")
        check("included" == block["status"], "notes 块按预算正常注入")
        check(items[0]["text"] in nt.render_notes(wd, before_chapter_no=2),
              "接受的要点文本真的被注入")

        recap = render_recap(wd, upto_chapter_no=1)
        check("要点：" in recap, "前情优先读已确认要点摘要")

        nt.notes_path(wd, "v001-c0001").unlink()
        _, report2 = render_write_brief_report(
            wd, chapter_no=2, prev_chapter_no=1, brief=_brief(2), budget_chars=8000,
        )
        block2 = next(b for b in report2["blocks"] if b["id"] == "notes")
        check(block2["reason_code"] == "empty_source", "删掉 notes 后该块标为空来源")
        recap2 = render_recap(wd, upto_chapter_no=1)
        check("第1章发生了什么" in recap2 and "要点：" not in recap2,
              "前情自动回退卷摘要 gist")


# ── A5：摘要快照 stale / 重建 ──────────────────────────────


def test_summary_snapshot_stale_and_rebuild() -> None:
    print("A5 · 要点一改快照即过期；重建后指纹与来源一致")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp))
        items, _ = nt.normalize_notes(
            [{"type": "state", "ref": "李默", "text": "李默欠青云宗三块灵石。"}],
            PROSE, chapter_id="v001-c0001", vol=1, chapter_no=1, refs=_refs(),
            source={"prose_sha": nt.sha256(PROSE)},
        )
        nt.save_extraction(wd, "v001-c0001", items=items, invalid=[], prose=PROSE,
                           source={"prose_sha": nt.sha256(PROSE)})
        nt.update_note(wd, "v001-c0001", items[0]["id"], action="accept")
        snap = sm.build_snapshot(wd, "v001-c0001", vol=1, chapter_no=1)
        check(sm.load_snapshot(wd, "v001-c0001")["stale"] is False,
              "刚重建的快照与来源一致")

        nt.update_note(wd, "v001-c0001", items[0]["id"], action="discard")
        loaded = sm.load_snapshot(wd, "v001-c0001")
        check(loaded["stale"] is True, "改要点后快照立刻标 stale")
        check(sm.usable_snapshot(wd, "v001-c0001") is None,
              "过期快照不会被前情拿去冒充新结论")

        snap2 = sm.build_snapshot(wd, "v001-c0001", vol=1, chapter_no=1)
        loaded2 = sm.load_snapshot(wd, "v001-c0001")
        check(loaded2["stale"] is False,
              "重建后 stale 清除")
        check(snap2["source_sha"] == sm.source_digest(wd, "v001-c0001") == loaded2["source_sha"],
              "重建后快照 hash 与当前来源完全一致")
        check(snap2["available"] is False, "已无确认要点时快照标为不可用（回退卷摘要）")


# ── B5：定稿后处理幂等 / 单步可重试 ───────────────────────


def test_finalize_idempotent_and_retryable() -> None:
    print("B5 · 定稿后处理分步编排：幂等、失败不拖垮其他步、可单步重试")
    with tempfile.TemporaryDirectory() as tmp:
        wd = _write_work(Path(tmp))
        cid = "v001-c0001"
        first = fin.run(wd, cid, text=PROSE, meta={"vol": 1, "chapter_no": 1})
        check(first["steps"]["snapshot"]["status"] == "done", "版本快照完成")
        check(first["steps"]["notes"]["status"] == "failed", "没有抽取器时 notes 步失败")
        check(first["steps"]["summary"]["status"] == "done"
              and first["steps"]["state"]["status"] == "done",
              "单步失败不拖垮摘要与角色状态")
        check(first["status"] == "partial", "整体标 partial，不假装成功")
        versions = len(__import__("workshop.draft_store", fromlist=["list_versions"])
                       .list_versions(wd, cid))
        check(versions == 1, "失败重跑没有重复写版本")

        # 补上同一份正文的要点：notes 步应跳过，不再花钱
        items, _ = nt.normalize_notes(
            [{"type": "fact", "ref": "李默", "text": "李默确认数值之眼不是幻觉。"}],
            PROSE, chapter_id=cid, vol=1, chapter_no=1, refs=_refs(),
            source={"prose_sha": fin.sha256(PROSE), "model": "test"},
        )
        nt.save_extraction(wd, cid, items=items, invalid=[], prose=PROSE,
                           source={"prose_sha": fin.sha256(PROSE), "model": "test"})
        second = fin.run(wd, cid, text=PROSE, meta={"vol": 1, "chapter_no": 1})
        check(second["steps"]["notes"].get("skipped") is True,
              "同一份正文已抽过要点：跳过，不重复花钱")
        check(second["status"] == "done", "补齐后整体 done")

        third = fin.run(wd, cid, text=PROSE, meta={"vol": 1, "chapter_no": 1})
        check(all(third["steps"][s].get("skipped") for s in fin.STEPS),
              "重复定稿每一步都跳过")
        versions2 = len(__import__("workshop.draft_store", fromlist=["list_versions"])
                        .list_versions(wd, cid))
        check(versions2 == 1, "重复定稿没有重复写版本")

        # 只重试某一步
        only = fin.run(wd, cid, text=PROSE, meta={"vol": 1, "chapter_no": 1},
                       steps=["summary"], force=True)
        check(only["steps"]["summary"]["status"] == "done", "可以只强制重跑指定步骤")
        check(only.get("calls") == [], "指定步骤不会偷偷跑别的模型调用")


# ── 批次 3 遗留：已拒绝意见不再重复提出 ────────────────────


def test_rejected_findings_not_repeated() -> None:
    print("遗留修复 · 已拒绝意见不再重复提出（可重新考虑）")
    prose = "他把门闩好，往后退了半步。\n\n只是那脚步声，我总觉得在哪儿听过。"
    evidence = "只是那脚步声，我总觉得在哪儿听过。"
    prior = [{
        "id": "f-old", "status": "rejected", "evidence": evidence,
        "suggestion": "删掉这句", "review_id": "20261104T133817-6f33f5",
        "review_created_at": "2026-11-04T13:38:17", "reject_reason": "保留原句",
    }]
    items, _ = normalize_items(
        [{"type": "hook", "evidence": evidence, "problem": "结尾钩子和人物关系不符",
          "suggestion": "换个说法"}],
        prose, existing_items=prior,
    )
    check(items[0]["status"] == "duplicate", "同一句被拒后，下次再审标 duplicate")
    check(items[0]["repeat_of"] == "rejected", "标记重复来源是「已拒绝」")
    check("已拒绝" in items[0]["repeat_reason"] and "保留原句" in items[0]["repeat_reason"],
          "给出可读判定依据（含上次拒绝理由）")
    check(items[0]["locatable"] is True, "引用仍能定位才抑制")

    # 归一化后一致（只差标点/空白）也算同一句：
    # 旧记录里存的是半角逗号，当前原文是全角——定位用原文，比对用归一化。
    prior_norm = [{**prior[0],
                   "evidence": "只是那脚步声,我总觉得在哪儿听过."}]
    items2, _ = normalize_items(
        [{"type": "hook", "evidence": evidence, "suggestion": "换个说法"}],
        prose, existing_items=prior_norm,
    )
    check(items2[0]["status"] == "duplicate", "归一化后一致也抑制")
    check("归一化" in items2[0]["repeat_reason"], "写清是归一化比对命中")

    # 正文改过、这句已经不在了：不抑制，也不假装能采纳
    items3, invalid3 = normalize_items(
        [{"type": "hook", "evidence": evidence, "suggestion": "换个说法"}],
        "他把门闩好，往后退了半步。",
        existing_items=prior,
    )
    check(not items3 and "找不到" in invalid3[0]["reason"],
          "正文改到这句不存在：不抑制，明确标定位失败")

    # 作者点「重新考虑」后不再抑制
    prior_reopened = [{**prior[0], "reopened_at": "2026-11-04T14:30:00"}]
    items4, _ = normalize_items(
        [{"type": "hook", "evidence": evidence, "suggestion": "换个说法"}],
        prose, existing_items=prior_reopened,
    )
    check(items4[0]["status"] == "open", "重新考虑后不再被抑制（不永久封死）")


# ── 批次 3 遗留：正文读取缓存 ──────────────────────────────


def test_selfcheck_cache_invalidates() -> None:
    print("遗留修复 · 正文自检缓存命中，正文/指令/设定变化后失效")
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        text = PROSE
        brief = _brief(1)
        setting = _setting()
        e1, w1, s1, c1 = services._selfcheck_cached(wd, "v001-c0001", text, brief, setting)
        check(c1["hit"] is False, "第一次没有缓存，真算")
        e2, _w2, _s2, c2 = services._selfcheck_cached(wd, "v001-c0001", text, brief, setting)
        check(c2["hit"] is True, "同一份正文第二次直接命中缓存")

        changed = text + "\n\n多写了一句。"
        _e3, _w3, _s3, c3 = services._selfcheck_cached(wd, "v001-c0001", changed, brief, setting)
        check(c3["hit"] is False, "正文一改缓存立即失效")

        brief2 = {**_brief(1), "must_not": ["不要写雪"]}
        _e4, _w4, _s4, c4 = services._selfcheck_cached(wd, "v001-c0001", text, brief2, setting)
        check(c4["hit"] is False, "章指令一改缓存也失效（不拿旧标准判新稿）")

        setting2 = {**_setting(), "style": {"taboo": ["不用破折号"]}}
        _e5, _w5, _s5, c5 = services._selfcheck_cached(wd, "v001-c0001", text, brief, setting2)
        check(c5["hit"] is False, "设定集一改缓存也失效")


def test_draft_read_cache_invalidates() -> None:
    print("遗留修复 · GET /draft 整读结果缓存：命中 0.x ms，正文改动立即失效")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_work(root / "workspaces")
        paths = services.Paths(root=root)
        first = services.read_draft(paths, "关山灯", "v001-c0001")
        check(first.get("read_cache", {}).get("hit") is False, "第一次读：未命中，真解析")
        second = services.read_draft(paths, "关山灯", "v001-c0001")
        check(second.get("read_cache", {}).get("hit") is True, "再读：命中缓存")
        check(second.get("stats", {}).get("chars") == first.get("stats", {}).get("chars"),
              "命中时返回的 stats 与真读一致")

        (root / "workspaces" / "关山灯" / "90-draft" / "v001-c0001.md").write_text(
            PROSE + "\n\n他又补了一句。", encoding="utf-8")
        third = services.read_draft(paths, "关山灯", "v001-c0001")
        check(third.get("read_cache", {}).get("hit") is False,
              "正文一改，整读缓存立即失效重算")
        check(third.get("stats", {}).get("chars") > first.get("stats", {}).get("chars"),
              "重算后的统计是新正文的（不是旧缓存）")


def main() -> int:
    test_extract_validation_and_conflict()
    test_note_decisions_control_context()
    test_notes_independent_block_and_recap_fallback()
    test_summary_snapshot_stale_and_rebuild()
    test_finalize_idempotent_and_retryable()
    test_rejected_findings_not_repeated()
    test_selfcheck_cache_invalidates()
    test_draft_read_cache_invalidates()
    print("=" * 58)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print(f"  · {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
