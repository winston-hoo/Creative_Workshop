"""全文检索的单元测试。

不碰真实作品：每个用例在 tmp_path 里造一部最小作品，直接测 workshop.search。
重点覆盖中文二元切分、来源过滤、增量（改/删）、片段与段落定位、排序。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from workshop import search  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def work_root(tmp_path: Path) -> Path:
    """一部最小作品：设定 + 卷表 + 章指令 + 正文 + 要点 + 审稿。"""
    ws = tmp_path / "workspaces" / "测试书"
    _write(
        ws / "60-setting" / "setting.yaml",
        """
work: 测试书
genre: 都市
logline: 一个挑夫替死人接压送终。
characters:
- name: 苏挽月
  role: 师尊
  personality: 外冷内热
  motive: 找出殡门白账的源头
factions:
- name: 殡门
  stance: 控制暗礁与活药
world:
  rules:
  - 暗礁只能由活人背出
terms:
- name: 接压送终
  desc: 替死者背走最后一程的行话
""",
    )
    _write(
        ws / "70-volume" / "volume-001.yaml",
        """
vol:
  vol: 1
  title: 第一桩活药
  core_conflict: 殡门要收回活药
  chapters:
  - chapter_no: 1
    title: 暗礁碎屑
    gist: 苏挽月从挑夫背上取出一枚暗礁碎屑
""",
    )
    _write(
        ws / "80-brief" / "v001-c0001.yaml",
        """
chapter_id: v001-c0001
vol: 1
chapter_no: 1
title: 暗礁碎屑
one_line: 苏挽月取出一枚暗礁碎屑
core_plot: 挑夫背着暗礁进城，苏挽月拦下他并取走碎屑。
foreshadow:
- id: v001-c0001-f01
  action: 埋设
  desc: 碎屑里封着一缕死人的记忆。
""",
    )
    _write(
        ws / "90-draft" / "v001-c0001.md",
        "第一段：挑夫把麻绳勒在肩上，黢黑的脖子像一截老树皮。\n\n"
        "苏挽月把暗礁碎屑从挑夫背上取出来，活药只剩半罐。\n",
    )
    _write_json(
        ws / "90-draft" / "v001-c0001.notes.json",
        {
            "chapter_id": "v001-c0001",
            "notes": [
                {
                    "id": "n1",
                    "ref": "灰烬",
                    "text": "她袖口的灰烬没有被交代",
                    "status": "accepted",
                    "chapter_no": 1,
                },
                {
                    "id": "n2",
                    "ref": "钟声",
                    "text": "待确认要点：夜里的钟声",
                    "status": "pending",
                    "chapter_no": 1,
                },
                {
                    "id": "n3",
                    "ref": "麻绳",
                    "text": "旧结论：麻绳是法器",
                    "status": "accepted",
                    "superseded_by": "n4",
                    "chapter_no": 1,
                },
            ],
        },
    )
    _write_json(
        ws / "90-draft" / "v001-c0001.review.json",
        {
            "chapter_id": "v001-c0001",
            "reviews": [
                {
                    "review_id": "r1",
                    "items": [
                        {
                            "id": "r1-1",
                            "type": "thesis",
                            "severity": "major",
                            "paragraph": 1,
                            "start": 30,
                            "end": 46,
                            "evidence": "活药只剩半罐",
                            "problem": "主角处境没有量化",
                            "suggestion": "这一段是万能悬念，应该给一个台阶",
                        }
                    ],
                }
            ],
        },
    )
    return tmp_path


def test_chinese_bigram_single_char_and_ranking(work_root: Path) -> None:
    result = search.query(work_root, "苏挽月")
    assert result["total"] >= 2
    types = {item["source_type"] for item in result["items"]}
    assert "chapter" in types and "setting" in types
    # 标题权重高于正文：设定条目里的名字应排在正文之前。
    assert result["items"][0]["source_type"] == "setting"

    # 四字中文词切成三个二元组，仍要命中同一章。
    long_word = search.query(work_root, "暗礁碎屑")
    assert any(item["source_type"] == "chapter" for item in long_word["items"])

    # 单字查询走单字令牌，不能因为只建二元组就完全搜不到。
    single = search.query(work_root, "黢")
    assert single["total"] >= 1


def test_source_filter_and_setting_only_term(work_root: Path) -> None:
    only_setting = search.query(work_root, "接压送终")
    assert only_setting["total"] >= 1
    assert all(item["source_type"] == "setting" for item in only_setting["items"])

    filtered = search.query(work_root, "接压送终", source_types="review,notes")
    assert filtered["total"] == 0


def test_english_number_and_foreshadow_id(work_root: Path) -> None:
    result = search.query(work_root, "v001-c0001-f01")
    assert result["total"] == 1
    assert result["items"][0]["source_type"] == "foreshadow"
    assert result["items"][0]["locator"]["foreshadow_id"] == "v001-c0001-f01"

    # 全角字符经 NFKC 归一后也要能找到章节编号。
    fullwidth = search.query(work_root, "Ｖ００１－Ｃ０００１")
    assert fullwidth["total"] >= 1
    assert any(item["locator"].get("kind") == "chapter" for item in fullwidth["items"])


def test_notes_only_confirmed(work_root: Path) -> None:
    accepted = search.query(work_root, "袖口的灰烬")
    assert accepted["total"] == 1
    assert accepted["items"][0]["source_type"] == "notes"

    # pending 与 superseded 的条目不应进索引。
    assert search.query(work_root, "夜里的钟声")["total"] == 0
    assert search.query(work_root, "麻绳是法器")["total"] == 0


def test_review_text_is_searchable(work_root: Path) -> None:
    result = search.query(work_root, "万能悬念")
    assert result["total"] == 1
    assert result["items"][0]["source_type"] == "review"
    assert "台阶" in result["items"][0]["snippet"]["match"] + result["items"][0]["snippet"]["after"]


def test_snippet_offset_and_paragraph(work_root: Path) -> None:
    result = search.query(work_root, "活药")
    item = next(x for x in result["items"] if x["source_type"] == "chapter")
    assert item["snippet"]["match"] == "活药"
    assert item["offset"] > 0
    assert item["paragraph"] == 1
    assert item["length"] == 2
    assert item["locator"]["chapter_id"] == "v001-c0001"
    # 命中页按 doc_id 取回同一条文档。
    doc = search.load_doc(work_root, item["doc_id"])
    assert doc is not None and doc["raw"][item["offset"]: item["offset"] + 2] == "活药"


def test_incremental_modify_and_delete(work_root: Path) -> None:
    chapter = work_root / "workspaces" / "测试书" / "90-draft" / "v001-c0001.md"
    assert search.query(work_root, "玄铁钉")["total"] == 0

    chapter.write_text(
        "苏挽月又找到了一枚玄铁钉。\n\n她把暗礁碎屑收进袖子里。\n",
        encoding="utf-8",
    )
    os.utime(chapter, ns=(time.time_ns(), time.time_ns()))
    updated = search.query(work_root, "玄铁钉")
    assert updated["total"] == 1
    assert updated["items"][0]["source_type"] == "chapter"
    # 旧内容被替换，不应残留。
    assert search.query(work_root, "黢黑")["total"] == 0

    chapter.unlink()
    assert search.query(work_root, "玄铁钉")["total"] == 0
    assert search.load_doc(work_root, updated["items"][0]["doc_id"]) is None


def test_empty_index_and_limit_clamp(tmp_path: Path) -> None:
    assert search.status(tmp_path)["ready"] is False
    result = search.query(tmp_path, "任何人")
    assert result["total"] == 0
    assert result["index"]["ready"] is True

    # limit 超过上限时按 200 截断；offset 为负时归零。
    clamped = search.query(tmp_path, "任何人", limit=9999, offset=-5)
    assert clamped["limit"] == search.MAX_LIMIT
    assert clamped["offset"] == 0


def test_work_filter(work_root: Path, tmp_path: Path) -> None:
    result = search.query(work_root, "暗礁", work="测试书")
    assert result["total"] >= 1
    assert all(item["work"] == "测试书" for item in result["items"])

    missing = search.query(work_root, "暗礁", work="不存在的书")
    assert missing["total"] == 0
