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
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STALE_THRESHOLD = 30
LEDGER_SCHEMA_VERSION = "foreshadow-ledger-v1"

ACTION_PLANT = "埋设"
ACTION_ADVANCE = "推进"
ACTION_RECOVER = "回收"

# 重跑同一章时，「这次埋的」和「上次埋的」描述未必一字不差（模型措辞会漂）。
# 相似度达到这个阈值就认定是同一条伏笔，复用旧编号；低于它才发新号。
# 定 0.6：实测同一条伏笔换个说法，序列相似度普遍在 0.75 以上
# （如「主角身世有隐情」与「主角身世藏着隐情」是 0.8）；
# 而真正不同的两条伏笔通常落在 0.4 以下。
PLANT_MATCH_THRESHOLD = 0.6

# 给模型的未回收清单里，每 CONTEXT_STALE_SHARE 条留 1 条给「最久没动过」的伏笔。
# 清单必须两段取样：只给最近的，早埋的线永远等不到「回收」；只给最早的（原实现），
# 新埋的线模型根本看不见 —— 见 context_for_prompt 的说明。
CONTEXT_STALE_SHARE = 4

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _normalize_desc(text: str) -> str:
    """去掉空白与标点，只留字本身——比较的是「说了什么」不是「怎么断句」。"""
    return _PUNCT_RE.sub("", str(text or ""))


def _similarity(left: str, right: str) -> float:
    """两段描述的相似度，0~1。

    用 difflib 的序列比而不是字二元组重合度：后者对「中间插了几个字」很敏感，
    「身世有隐情」和「身世藏着隐情」只差两个字，二元组重合度却掉到 0.44，
    会误判成两条伏笔。序列比看得是**最长公共子序列**，插字掉分很少。
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    matcher = SequenceMatcher(None, left, right)
    # 先用便宜的字符多重集比做一次快速否决，省掉大部分完整比对。
    if matcher.quick_ratio() < PLANT_MATCH_THRESHOLD:
        return matcher.quick_ratio()
    return matcher.ratio()


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

    def __init__(
        self,
        work: str = "",
        next_seq: int = 1,
        items: list[ForeshadowItem] | None = None,
        last_chapter_no: int | None = None,
    ):
        self.work = work
        self.next_seq = next_seq
        self.items: list[ForeshadowItem] = items or []
        # 跑到过的最大的章号。断点判定需要一个「现在到哪了」的基准，
        # 没有它 stale_items 只能返回空——那会变成一个静默的假绿灯。
        self.last_chapter_no = last_chapter_no

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
            last_chapter_no=data.get("last_chapter_no"),
        )

    def save(self, path: str | Path, *, stale_threshold: int = DEFAULT_STALE_THRESHOLD) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 基准必须传进去。不传的话 stale_items 恒为空、疑似断点永远报 0，
        # 而体检报告自己算出的却是几百条——同一个量在两处给出相反的答案。
        stale = self.stale_items(current_chapter_no=self.last_chapter_no, threshold=stale_threshold)
        payload = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "work": self.work,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "next_seq": self.next_seq,
            "last_chapter_no": self.last_chapter_no,
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

        取样是「最近动过的」+「最久没动的」两段，**不能按创建顺序截前 limit 条**：
        未回收条数是几百上千，截前面那一小撮等于让模型从此看不见新埋的线，
        它只能把每条线索都当新伏笔重新登记。实测一本 545 章的书跑下来
        797 条里 776 条只被埋设过一次，而进过清单的只有前 21 条——
        多事件条目的下标恰好是 0..20，一个不多一个不少。
        """
        open_items = self.open_items
        if len(open_items) <= limit:
            picked = list(open_items)
        else:
            # ponytail: 只按「动过的时间」粗排，不做语义相关性检索——
            # 那要先有向量索引；等实测还出现明显的漏回收再上。
            stale_slots = max(1, limit // CONTEXT_STALE_SHARE)
            by_age = sorted(open_items, key=lambda i: (i.last_touched_chapter_no or 0, i.id))
            recent = by_age[::-1][: limit - stale_slots]
            seen = {i.id for i in recent}
            stale = [i for i in by_age if i.id not in seen][:stale_slots]
            picked = recent + stale

        out: list[dict[str, Any]] = []
        for item in picked:
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

    def revert_chapter(self, chapter_id: str) -> int:
        """抹掉某一章在台账里留下的全部痕迹，返回清掉的 event 条数。

        重跑一章之前**必须**先调它。不然同一章的动作会被追加一遍：
        「推进」变成两条 events，上次埋的伏笔被当成新伏笔再发一个编号。
        实测 20 章重跑就让台账从 794 条涨到 825 条——照这个比例全量重跑
        一本 545 章的书，台账会直接翻倍，未回收数与疑似断点统计随之失真。

        条目本身**不删**：只清这一章产生的 events。条目留着才能在下一次
        「埋设」时按描述匹配回来、复用原编号，也符合这里一贯的取舍——
        宁可多留一条，也不丢信息。
        """
        cleared = 0
        for item in self.items:
            before = len(item.events)
            if before == 0:
                continue
            item.events = [e for e in item.events if e.chapter_id != chapter_id]
            if len(item.events) == before:
                continue
            cleared += before - len(item.events)

            # 这一章动过它，派生的两个字段要跟着回退，不能留在旧值上。
            touched = [e.chapter_no for e in item.events if e.chapter_no is not None]
            item.last_touched_chapter_no = max(touched) if touched else item.planted_chapter_no
            # 回收动作若正是这一章做的，撤掉后状态要退回未回收。
            if not any(e.action == ACTION_RECOVER for e in item.events):
                item.status = "open"
        return cleared

    def _plant(
        self,
        chapter_id: str,
        chapter_no: int | None,
        desc: str,
        *,
        claimed: set[str] | None = None,
    ) -> ForeshadowItem:
        """登记一条「埋设」。这一章以前埋过同样的就复用旧编号，否则发新号。

        复用是重跑幂等的关键：不复用，每重跑一章就多出一批 F-0xx，
        台账里同一个伏笔会有好几个编号。
        """
        existing = self._match_planted(chapter_id, desc, claimed=claimed)
        if existing is not None:
            # 描述以这次的为准（措辞可能更准确），编号与埋设章保持不变。
            existing.desc = desc
            existing.planted_chapter_no = chapter_no
            existing.last_touched_chapter_no = chapter_no
            existing.events.append(ForeshadowEvent(chapter_id, chapter_no, ACTION_PLANT, desc))
            return existing

        fid = self.allocate_id()
        item = ForeshadowItem(
            id=fid,
            desc=desc,
            planted_chapter_id=chapter_id,
            planted_chapter_no=chapter_no,
            last_touched_chapter_no=chapter_no,
        )
        item.events.append(ForeshadowEvent(chapter_id, chapter_no, ACTION_PLANT, desc))
        self.items.append(item)
        return item

    def _match_planted(
        self,
        chapter_id: str,
        desc: str,
        *,
        claimed: set[str] | None = None,
        threshold: float = PLANT_MATCH_THRESHOLD,
    ) -> ForeshadowItem | None:
        """在这一章自己埋过的条目里，找描述最像的那条，供重跑复用编号。

        只在「同一章埋的」里找：不同章埋了相似的伏笔是常有的事，
        跨章匹配会把两条独立的线并成一条。
        """
        target = _normalize_desc(desc)
        if not target:
            return None
        best: ForeshadowItem | None = None
        best_score = 0.0
        for item in self.items:
            if item.planted_chapter_id != chapter_id:
                continue
            if claimed and item.id in claimed:
                continue  # 这一轮已经被别的伏笔认领走了
            if any(e.chapter_id == chapter_id and e.action == ACTION_PLANT for e in item.events):
                continue  # 这一章已经埋过它了，别再埋一次
            score = _similarity(target, _normalize_desc(item.desc))
            if score > best_score:
                best, best_score = item, score
        return best if best_score >= threshold else None

    def apply(
        self,
        *,
        chapter_id: str,
        chapter_no: int | None,
        foreshadows: list[dict[str, Any]] | None,
        replace_chapter: bool = True,
    ) -> ApplyReport:
        """把某一章的伏笔动作并入台账，并把分配好的编号写回原条目。

        写回很重要：标注文件里必须留下**台账分配的规范编号**，
        否则标注文件自身不自洽，之后按编号回溯就对不上。

        `replace_chapter` 默认为 True：**先撤销这一章的旧痕迹再写入**，
        所以同一章跑几次结果都一样（幂等）。关掉它就退回「追加」语义——
        只在明确知道这一章从未跑过时才该关。
        """
        report = ApplyReport()
        if chapter_no is not None:
            # 取最大值而不是直接赋值：重跑旧章不该让进度基准倒退。
            self.last_chapter_no = max(self.last_chapter_no or 0, chapter_no)
        if replace_chapter:
            self.revert_chapter(chapter_id)
        claimed: set[str] = set()
        for entry in foreshadows or []:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("动作") or "").strip()
            desc = str(entry.get("描述") or "").strip()
            ref = str(entry.get("编号") or "").strip()

            if action == ACTION_PLANT:
                item = self._plant(chapter_id, chapter_no, desc, claimed=claimed)
                claimed.add(item.id)
                entry["编号"] = item.id
                report.added.append(item.id)
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
                    item = self._plant(chapter_id, chapter_no, desc, claimed=claimed)
                    claimed.add(item.id)
                    entry["动作"] = ACTION_PLANT
                    entry["编号"] = item.id
                    report.added.append(item.id)
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

        if replace_chapter:
            self._prune_unconfirmed(chapter_id)

        return report

    def _prune_unconfirmed(self, chapter_id: str) -> int:
        """删掉「由这一章埋下、但这一轮没再确认」的条目，返回删掉的数量。

        撤销之后仍一个事件都不剩，说明这一章刚才埋过它、这一轮却没有再埋——
        即这一轮不认为它是伏笔。留着就会变成孤儿：每重跑一轮攒一批，
        台账只增不减。删掉才能让重跑真正收敛。

        只删**这一章埋的**、且**一个事件都没有**的条目：
        被别的章推进或回收过的条目一定带着那些事件，不会被误删。
        """
        kept: list[ForeshadowItem] = []
        dropped = 0
        for item in self.items:
            if item.planted_chapter_id == chapter_id and not item.events:
                dropped += 1
                continue
            kept.append(item)
        self.items = kept
        return dropped

    # ── 视图 ────────────────────────────────────────────

    def summary(self) -> str:
        stale = self.stale_items(current_chapter_no=self.last_chapter_no)
        lines = [
            f"伏笔台账  {self.work or '(未命名)'}",
            f"  总数 {len(self.items)}   未回收 {len(self.open_items)}   "
            f"已回收 {len(self.items) - len(self.open_items)}   疑似断点 {len(stale)}",
        ]
        return "\n".join(lines)
