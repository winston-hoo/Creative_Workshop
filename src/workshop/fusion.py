"""M5 融合器：用自有素材生成新故事骨架。

设计出处：`小说创作工坊-通用设计方案.md` §7（M5）与分阶段 P6（可选）。

只能用自己的素材：人物/地点/势力/能力全部来自已入库作品（K1 实体卡片），
骨架元素可追溯到来源作品——这是「融合」与「抄袭缝合」的分界线：
每条素材都带 `source`（哪部作品、哪个条目），不合成不可追溯的新设定。

## 生成什么

  · 一句话前提（logline）：从素材里选主角 + 核心冲突 + 世界背景拼出来
  · 世界元素清单：地点 / 势力 / 能力，全部带来源
  · 人物卡片：至少主角与对手，性格槽位由来源人物填充
  · 骨架章节：按三幕结构铺一个 12 章左右的节拍表（不含正文）

所有输出都是「可编辑的草稿」：素材引用是建议不是决定，
作者随时可以替换任何一条。仅用自有 / 公版 / 已授权作品。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FUSION_SCHEMA_VERSION = "fusion-skeleton-v1"

# 三幕节拍：每一拍给个动作模板，融合器用素材填槽
_BEATS = [
    ("开场", "主角进入日常，世界与处境被快速交代"),
    ("触发", "一件意外打破日常，主角做出第一次选择"),
    ("建立", "与新伙伴结盟，目标逐渐清晰"),
    ("阻碍", "第一次重大失败，敌对势力亮出底牌"),
    ("深入", "主角获得新认知或关键道具，代价开始出现"),
    ("转折", "阵营背叛或真相揭露，计划被推翻"),
    ("低谷", "主角被迫放弃旧目标，直面最初的恐惧"),
    ("觉醒", "把素材里的能力/势力真正使用起来"),
    ("对决", "与对手的最终冲突，赌上一切"),
    ("收束", "冲突解决，世界因主角的选择而改变"),
    ("回响", "回到开场场景，一切已然不同"),
]


@dataclass
class Skeleton:
    work: str
    generated_at: str
    sources: list[str]
    logline: dict[str, Any]
    world: list[dict[str, Any]]
    cast: list[dict[str, Any]]
    beats: list[dict[str, Any]]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FUSION_SCHEMA_VERSION,
            "work": self.work,
            "generated_at": self.generated_at,
            "sources": self.sources,
            "logline": self.logline,
            "world": self.world,
            "cast": self.cast,
            "beats": self.beats,
            "note": self.note,
        }


def _entities_for(workspaces_root: Path, work: str) -> dict[str, Any] | None:
    path = Path(workspaces_root) / work / "20-kb" / "k1-entities.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("available"):
            return data
    except (ValueError, OSError):
        pass
    return None


def generate_skeleton(
    workspaces_root: Path,
    source_works: list[str],
    *,
    seed: int | None = None,
) -> Skeleton:
    """从多部作品的自有素材拼一个新骨架。

    source_works 传空则扫描全部已入库作品（_ 开头除外）。
    """
    rng = random.Random(seed)

    candidates: list[str] = []
    if source_works:
        candidates = source_works
    else:
        root = Path(workspaces_root)
        if root.exists():
            candidates = sorted(
                p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
            )

    all_characters: list[dict[str, Any]] = []
    all_locations: list[dict[str, Any]] = []
    all_factions: list[dict[str, Any]] = []
    all_abilities: list[dict[str, Any]] = []
    used_works: list[str] = []

    for work in candidates:
        entities = _entities_for(workspaces_root, work)
        if not entities:
            continue
        used_works.append(work)
        for c in entities.get("characters") or []:
            entry = dict(c)
            entry["source"] = work
            all_characters.append(entry)
        for loc in entities.get("locations") or []:
            entry = dict(loc)
            entry["source"] = work
            all_locations.append(entry)
        for f in entities.get("factions") or []:
            entry = dict(f)
            entry["source"] = work
            all_factions.append(entry)
        for a in entities.get("abilities") or []:
            entry = dict(a)
            entry["source"] = work
            all_abilities.append(entry)

    if not used_works:
        raise ValueError("没有可用的自有素材（先为作品构建知识库 K1 实体卡片）")

    def pick(pool: list[dict[str, Any]], default: dict[str, Any]) -> dict[str, Any]:
        return rng.choice(pool) if pool else default

    protagonist = pick(
        [c for c in all_characters if (c.get("role") not in ("路人", "反派"))],
        {"name": "（待定主角）", "source": "(未指定)"},
    )
    antagonist = pick(
        [c for c in all_characters if c.get("role") == "反派"]
        or [c for c in all_characters if c.get("name") != protagonist.get("name")],
        {"name": "（待定对手）", "source": "(未指定)"},
    )

    world_pool = all_locations + (all_factions or [])
    world_spot = pick(all_locations, {"name": "（待定地点）", "source": "(未指定)"})
    faction_spot = pick(all_factions, {"name": "（待定势力）", "source": "(未指定)"})
    ability_spot = pick(
        [a for a in all_abilities if a.get("name")],
        {"name": "（待定能力）", "source": "(未指定)"},
    )

    logline = {
        "template": "主角「{protagonist}」身处「{world}」，当「{conflict}」发生时，他必须借助「{ability}」去改变什么。",
        "protagonist": protagonist.get("name", "（待定）"),
        "world": world_spot.get("name", "（待定）"),
        "conflict": f"{faction_spot.get('name', '未知势力')}的图谋",
        "ability": ability_spot.get("name", "（待定能力）"),
        "source_works": used_works,
    }

    world = [
        {"kind": "地点", "name": world_spot.get("name"), "note": world_spot.get("note") or "", "source": world_spot.get("source")},
        {"kind": "势力", "name": faction_spot.get("name"), "note": faction_spot.get("stance") or "", "source": faction_spot.get("source")},
        {"kind": "能力", "name": ability_spot.get("name"), "note": ability_spot.get("effect") or "", "source": ability_spot.get("source")},
    ]

    cast = [
        {
            "slot": "主角",
            "name": protagonist.get("name"),
            "identity": protagonist.get("identity") or protagonist.get("role") or "",
            "source": protagonist.get("source"),
        },
        {
            "slot": "对手",
            "name": antagonist.get("name"),
            "identity": antagonist.get("identity") or antagonist.get("role") or "",
            "source": antagonist.get("source"),
        },
    ]
    # 补充至多 2 个配角
    extras = [
        c for c in all_characters
        if c.get("name") not in (protagonist.get("name"), antagonist.get("name"))
    ]
    for slot, extra in zip(("关键配角", "盟友"), extras[:2]):
        cast.append(
            {
                "slot": slot,
                "name": extra.get("name"),
                "identity": extra.get("identity") or extra.get("role") or "",
                "source": extra.get("source"),
            }
        )

    beats = [
        {
            "no": i + 1,
            "stage": stage,
            "action": template,
            "hint": world,
            "note": "骨架草稿，正文待写；素材引用可随时替换",
        }
        for i, (stage, template) in enumerate(_BEATS)
    ]

    return Skeleton(
        work="融合骨架",
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        sources=used_works,
        logline=logline,
        world=world,
        cast=cast,
        beats=beats,
        note="素材全部来自已入库作品（K1 实体卡片），每条带来源。骨架是可编辑草稿，不是成品。",
    )


def save_skeleton(out_dir: Path, skeleton: Skeleton) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out / f"skeleton-{stamp}.json"
    path.write_text(json.dumps(skeleton.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "latest.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path