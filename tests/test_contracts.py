"""E5 契约测试：用固定夹具钉住四类 JSON 合同。

四类合同分别是：细纲（beats）、审稿（review）、要点（notes）、上下文预算（context）。
每类都有一份合法样本和一份非法样本：

- 合法样本必须被**既有解析器**接受，并产出可用的结构；
- 非法样本必须被**同一个解析器**拒绝（返回空条目，或抛出明确的错误）。

夹具放在 `tests/contracts/*.json`，改协议时先改夹具，测试会立刻指出哪条合同漂了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from workshop.beats import parse_beats  # noqa: E402
from workshop.context_budget import ContextBlock, ContextBudgetError, build_context  # noqa: E402
from workshop.notes import normalize_notes  # noqa: E402
from workshop.review import extract_json_object, normalize_items  # noqa: E402

CONTRACTS = Path(__file__).resolve().parent / "contracts"


def load_contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def test_beats_contract() -> None:
    fixture = load_contract("beats.json")
    valid = parse_beats(json.dumps(fixture["valid"]["response"], ensure_ascii=False))
    assert len(valid) >= 5, "合法细纲应解析出 5 个以上节拍"
    assert all(row["goal"] for row in valid), "每个节拍都要有 goal"

    invalid = parse_beats(json.dumps(fixture["invalid"]["response"], ensure_ascii=False))
    assert invalid == [], "没有 goal 的节拍必须被拒（不能当成可用细纲）"


def test_review_contract() -> None:
    fixture = load_contract("review.json")
    prose = fixture["prose"]

    valid = extract_json_object(json.dumps(fixture["valid"], ensure_ascii=False))
    items, rejected = normalize_items(valid.get("items"), prose)
    assert items, "合法审稿条目应能定位到正文"
    assert rejected == [], "合法条目不应被拒"
    assert items[0]["start"] >= 0 and items[0]["suggestion"]

    invalid = extract_json_object(json.dumps(fixture["invalid"], ensure_ascii=False))
    items2, rejected2 = normalize_items(invalid.get("items"), prose)
    assert items2 == [], "正文里找不到的引文必须被拒"
    assert rejected2 and "找不到" in rejected2[0]["reason"], "拒绝时要说明原因"


def test_notes_contract() -> None:
    fixture = load_contract("notes.json")
    prose = fixture["prose"]
    refs = fixture["refs"]

    items, rejected = normalize_notes(
        fixture["valid"]["items"], prose,
        chapter_id="v001-c0001", vol=1, chapter_no=1, refs=refs,
    )
    assert len(items) == 2, "合法要点应全部入库"
    assert rejected == [], "合法要点不应被拒"
    assert {row["type"] for row in items} == {"fact", "state"}

    items2, rejected2 = normalize_notes(
        fixture["invalid"]["items"], prose,
        chapter_id="v001-c0001", vol=1, chapter_no=1, refs=refs,
    )
    assert items2 == [], "对不上 ref 的要点必须被拒"
    assert rejected2 and "对不上" in rejected2[0]["reason"]


def test_context_contract() -> None:
    fixture = load_contract("context.json")
    valid = fixture["valid"]
    text, report = build_context(
        [ContextBlock(**row) for row in valid["blocks"]],
        budget_chars=valid["budget_chars"],
    )
    assert text, "合法上下文应产出非空文本"
    assert report["exact"] is True, "字数恒等式必须成立"
    assert report["used_chars"] == report["accounted_chars"]
    anchors = [row for row in report["blocks"] if row["id"] == "anchor"]
    assert anchors and anchors[0]["status"] == "included", "必需块必须完整注入"

    invalid = fixture["invalid"]
    with pytest.raises(ContextBudgetError) as exc:
        build_context(
            [ContextBlock(**row) for row in invalid["blocks"]],
            budget_chars=invalid["budget_chars"],
        )
    assert "超出预算" in str(exc.value), "报错要说清是预算装不下必需材料"
