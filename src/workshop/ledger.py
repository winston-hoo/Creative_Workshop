"""伏笔台账：跨章状态的唯一来源。

## 为什么必须有它

伏笔天然跨章，而逐章标注每次只看一章。常见错误做法是「把上下文扩到 10 章」——
那会立刻撞上 token 预算和长上下文退化（300 万字 ≈ 上百万 token）。

正确做法是把跨章状态**外化成文件**：每章只读台账、只改台账，从不把历史原文塞进上下文。
状态因此从「上下文里的信息」变成「文件里的状态」，这才能扩展到上千章。

## 为什么编号必须由台账分配

实测中让模型自己编编号，同一章跑三次分别给出 `F1`~`F4`、`F-01`~`F-04`、以及留空。
跨章必然对不上，台账就废了。

所以约定：**模型只判断动作与描述，编号由台账分配**；
只有「推进 / 回收」时才引用编号，而且必须来自台账给出的未回收清单。
引用不存在的编号会被记为异常，并按「埋设」处理——宁可多记一条，也不丢信息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STALE_THRESHOLD = 30
LEDGER_SCHEMA_VERSION = "foreshadow-ledger-v1"

ACTION_PLANT = "埋设"
ACTION_ADVANCE = "推进"
ACTION_RECOVER = "回收"


@dataclass
class ForeshadowEvent:
    chapter_id: str
    chapter_no: int | None
    action: str
    desc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "chapter_no": self.chapter_no,
            "action": self.action,
            "desc": self.desc,
        }


@dataclass
class ForeshadowItem:
    id: str
    desc: str
    planted_chapter_id: str
    planted_chapter_no: int | None
    status: str = "open"  # open | recovered
    last_touched_chapter_no: int | None = None
    events: list[ForeshadowEvent] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "desc": self.desc,
            "status": self.status,
            "planted_chapter_id": self.planted_chapter_id,
            "planted_chapter_no": self.planted_chapter_no,
            "last_touched_chapter_no": self.last_touched_chapter_no,
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ForeshadowItem":
        events = [
            ForeshadowEvent(
                chapter_id=str(e.get("chapter_id") or ""),
                chapter_no=e.get("chapter_no"),
                action=str(e.get("action") or ""),
                desc=str(e.get("desc") or ""),
            )
            for e in (raw.get("events") or [])
            if isinstance(e, dict)
        ]
        return cls(
            id=str(raw.get("id") or ""),
            desc=str(raw.get("desc") or ""),
            planted_chapter_id=str(raw.get("planted_chapter_id") or ""),
            planted_chapter_no=raw.get("planted_chapter_no"),
            status=str(raw.get("status") or "open"),
            last_touched_chapter_no=raw.get("last_touched_chapter_no"),
            events=events,
        )


@dataclass
class ApplyReport:
    """一次应用的结果。所有「没按预期发生」的情况都要在这里显式留痕。"""

    added: list[str] = field(default_factory=list)
    advanced: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    ignored: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "advanced": self.advanced,
            "recovered": self.recovered,
            "ignored": self.ignored,
            "anomalies": self.anomalies,
        }


class Ledger:
    """伏笔台账。使用方式：load → context_for_prompt → apply → save。"""

    def __init__(self, work: str = "", next_seq: int = 1, items: list[ForeshadowItem] | None = None):
        self.work = work
        self.next_seq = next_seq
        self.items: list[ForeshadowItem] = items or []

    # ── 持久化 ──────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            work=str(data.get("work") or ""),
            next_seq=int(data.get("next_seq") or 1),
            items=[ForeshadowItem.from_dict(i) for i in (data.get("items") or []) if isinstance(i, dict)],
        )

    def save(self, path: str | Path, *, stale_threshold: int = DEFAULT_STALE_THRESHOLD) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        stale = self.stale_items(threshold=stale_threshold)
        payload = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "work": self.work,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "next_seq": self.next_seq,
            "statistics": {
                "total": len(self.items),
                "open": sum(1 for i in self.items if i.is_open),
                "recovered": sum(1 for i in self.items if not i.is_open),
                "stale": len(stale),
            },
            "items": [i.to_dict() for i in self.items],
            "stale_threshold": stale_threshold,
            "stale_items": [i.id for i in stale],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    # ── 查询 ────────────────────────────────────────────

    def find(self, foreshadow_id: str) -> ForeshadowItem | None:
        for item in self.items:
            if item.id == foreshadow_id:
                return item
        return None

    @property
    def open_items(self) -> list[ForeshadowItem]:
        return [i for i in self.items if i.is_open]

    def context_for_prompt(self, chapter_no: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """给模型的未回收伏笔清单。

        这属于**变量部分**，不是固定前缀——它每章都在变，放前缀里会让缓存全失效。
        """
        out: list[dict[str, Any]] = []
        for item in self.open_items[:limit]:
            gap = None
            if chapter_no is not None and item.last_touched_chapter_no is not None:
                gap = chapter_no - item.last_touched_chapter_no
            out.append(
                {
                    "id": item.id,
                    "desc": item.desc,
                    "planted_chapter_no": item.planted_chapter_no,
                    "gap": gap,
                }
            )
        return out

    def stale_items(
        self, *, current_chapter_no: int | None = None, threshold: int = DEFAULT_STALE_THRESHOLD
    ) -> list[ForeshadowItem]:
        """长期未推进的未回收伏笔——疑似断点，是体检报告里最有价值的一节。"""
        out: list[ForeshadowItem] = []
        for item in self.open_items:
            last = item.last_touched_chapter_no
            if last is None:
                continue
            if current_chapter_no is None:
                continue
            if current_chapter_no - last >= threshold:
                out.append(item)
        return out

    # ── 应用 ────────────────────────────────────────────

    def allocate_id(self) -> str:
        fid = f"F-{self.next_seq:03d}"
        self.next_seq += 1
        return fid

    def apply(
        self,
        *,
        chapter_id: str,
        chapter_no: int | None,
        foreshadows: list[dict[str, Any]] | None,
    ) -> ApplyReport:
        """把某一章的伏笔动作并入台账，并把分配好的编号写回原条目。

        写回很重要：标注文件里必须留下**台账分配的规范编号**，
        否则标注文件自身不自洽，之后按编号回溯就对不上。
        """
        report = ApplyReport()
        for entry in foreshadows or []:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("动作") or "").strip()
            desc = str(entry.get("描述") or "").strip()
            ref = str(entry.get("编号") or "").strip()

            if action == ACTION_PLANT:
                fid = self.allocate_id()
                item = ForeshadowItem(
                    id=fid,
                    desc=desc,
                    planted_chapter_id=chapter_id,
                    planted_chapter_no=chapter_no,
                    last_touched_chapter_no=chapter_no,
                )
                item.events.append(
                    ForeshadowEvent(chapter_id, chapter_no, ACTION_PLANT, desc)
                )
                self.items.append(item)
                entry["编号"] = fid
                report.added.append(fid)
                continue

            if action in (ACTION_ADVANCE, ACTION_RECOVER):
                item = self.find(ref) if ref else None
                if item is None:
                    # 模型引用了一个不存在的编号。可能是上一轮编号变过、也可能是它自己编的。
                    # 不丢信息：记异常，并按「埋设」重新登记。
                    reason = f"引用了台账中不存在的编号「{ref or '(空)'}」" if ref else "推进/回收未填编号"
                    report.anomalies.append(
                        f"{chapter_id} 的伏笔动作「{action}」{reason}，已按「埋设」重新登记"
                    )
                    fid = self.allocate_id()
                    item = ForeshadowItem(
                        id=fid,
                        desc=desc,
                        planted_chapter_id=chapter_id,
                        planted_chapter_no=chapter_no,
                        last_touched_chapter_no=chapter_no,
                    )
                    item.events.append(
                        ForeshadowEvent(chapter_id, chapter_no, ACTION_PLANT, desc)
                    )
                    self.items.append(item)
                    entry["动作"] = ACTION_PLANT
                    entry["编号"] = fid
                    report.added.append(fid)
                    continue

                if not item.is_open:
                    report.anomalies.append(
                        f"{chapter_id} 对已回收的伏笔 {item.id} 又做了「{action}」，已忽略该动作"
                    )
                    report.ignored.append({"id": item.id, "action": action, "reason": "已回收"})
                    entry["编号"] = item.id
                    continue

                item.events.append(ForeshadowEvent(chapter_id, chapter_no, action, desc))
                item.last_touched_chapter_no = chapter_no
                if action == ACTION_RECOVER:
                    item.status = "recovered"
                    report.recovered.append(item.id)
                else:
                    report.advanced.append(item.id)
                continue

            report.anomalies.append(f"{chapter_id} 的伏笔动作「{action or '(空)'}」无法识别，已忽略")

        return report

    # ── 视图 ────────────────────────────────────────────

    def summary(self) -> str:
        stale = self.stale_items()
        lines = [
            f"伏笔台账  {self.work or '(未命名)'}",
            f"  总数 {len(self.items)}   未回收 {len(self.open_items)}   "
            f"已回收 {len(self.items) - len(self.open_items)}   疑似断点 {len(stale)}",
        ]
        return "\n".join(lines)
