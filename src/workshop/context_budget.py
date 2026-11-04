"""上下文预算账本：必需材料优先，可选材料按比例裁，cut 了哪块要说出来。

## 为什么要有这一层

写正文时真正喂给模型的不是「一篇文章」，是七八块来源不同的材料：
全书锚点、前情、本段任务、角色动态、伏笔、钩子、未来章蓝图、上一章末段。
原来这些块各自有上限，但没有**总量**——每块都在自己的小预算里合规，
合起来却可能把上下文撑爆；而超限时 `render_recap` 直接 break，作者
看不到「少喂了哪一段」，只看到模型写歪。

这里把「谁先进、谁后进、谁被裁、为什么被裁」变成一份可序列化的报告。
报告是 A4 的 manifest 与 U5 的可视化共用的同一份数据，不另算一遍。

约定：
  · 必需块（required）永远完整注入；装不下就抛 `ContextBudgetError`，
    由接口层回 422，**不裁剪、不静默**。
  · 可选块（optional）在剩余预算里按原始长度比例分配；分到太少（低于
    `MIN_OPTIONAL_KEEP`）就整块不注入，并在报告里写明原因。
  · 截断只发生在行/句边界，且截断后的文本以一行说明结尾，模型自己也能看到。

许可说明：结构参考了 GPL-3.0 项目的「必需优先 + omission receipt」设计，
代码与文案为本项目独立实现，没有复制对方源码。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CONTEXT_BUDGET_SCHEMA = "context-budget-v1"
CONTEXT_MANIFEST_SCHEMA = "context-manifest-v1"

# 一次写正文，「写前必读」+ 未来章蓝图的总预算。按字符算，中文约 1.4–1.5 字/token。
# 8000 字 ≈ 5–6k token，和对方 8000 字符的材料预算同一量级。
DEFAULT_BUDGET_CHARS = 8000

# 可选块按比例分到不足这么多字，就没有可读性了，整块不注入。
MIN_OPTIONAL_KEEP = 120

TRIM_MARKER = "\n…（此块超出预算，后面没有注入）"


class ContextBudgetError(ValueError):
    """必需材料已超出总预算——这是配置问题，不能靠裁剪蒙混过去。"""

    def __init__(
        self,
        *,
        budget_chars: int,
        required_chars: int,
        overhead_chars: int,
        excess_chars: int,
        required_labels: list[str],
    ) -> None:
        self.budget_chars = budget_chars
        self.required_chars = required_chars
        self.overhead_chars = overhead_chars
        self.excess_chars = excess_chars
        self.required_labels = required_labels
        labels = "、".join(required_labels) or "（没有必需块）"
        message = (
            f"上下文超出预算：必需材料 {required_chars} 字 + 分隔/尾部说明 "
            f"{overhead_chars} 字 > 总预算 {budget_chars} 字，超出 {excess_chars} 字。"
            f"需要完整注入的块：{labels}。"
            "先把这些材料精简（尤其是设定集/本段任务），或调高本次预算；"
            "不能靠裁掉必需材料继续写。"
        )
        super().__init__(message)


@dataclass
class ContextBlock:
    """一块候选材料。`text` 为空表示这来源当前没内容，也要进报告。"""

    id: str
    label: str
    kind: str  # required | optional
    text: str
    source: str = ""
    empty_note: str = "这一段目前没有内容"

    @property
    def chars(self) -> int:
        return len(self.text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _trim_to(text: str, limit: int) -> str:
    """在行/句边界截断，并附一行说明。返回长度一定 ≤ limit。"""
    if len(text) <= limit:
        return text
    marker = TRIM_MARKER
    body_limit = max(0, limit - len(marker))
    head = text[:body_limit]
    cut = head.rfind("\n")
    if cut < body_limit * 0.5:
        cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"), head.rfind("；"))
    if cut < body_limit * 0.5:
        cut = body_limit
    kept = text[:cut].rstrip()
    if not kept:
        return ""
    return kept + marker


def _allocate(blocks: list[ContextBlock], available: int) -> dict[str, int]:
    """可选块按原始长度比例分配剩余预算。返回 id → 允许字符数。"""
    sizes = {b.id: len(b.text) for b in blocks}
    total = sum(sizes.values())
    if total <= available:
        return dict(sizes)
    allocations: dict[str, int] = {}
    active = [b.id for b in blocks if sizes[b.id] > 0]
    pool = available
    # 水填式分配：先满足能整块放下的，剩下的按比例分给还放不下的。
    # guard 只是防浮点边界导致死循环，正常两三轮就结束。
    for _ in range(8):
        if not active or pool <= 0:
            break
        active_total = sum(sizes[i] for i in active)
        if active_total <= pool:
            for i in active:
                allocations[i] = sizes[i]
            pool = 0
            break
        saturated = [i for i in active if int(pool * sizes[i] / active_total) >= sizes[i]]
        if saturated:
            for i in saturated:
                allocations[i] = sizes[i]
                pool -= sizes[i]
                active.remove(i)
            continue
        for i in active:
            allocations[i] = max(0, int(pool * sizes[i] / active_total))
        break
    return allocations


def build_context(
    blocks: Iterable[ContextBlock],
    *,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    footer: str = "",
) -> tuple[str, dict[str, Any]]:
    """把候选块装进预算。返回 (实际注入文本, 可序列化报告)。

    报告里的 `used_chars` 是最终文本的真实长度；
    `block_chars + separator_chars + footer_chars == used_chars`
    （`exact` 字段就是这条恒等式的自检结果）。
    """
    budget = max(1, int(budget_chars or DEFAULT_BUDGET_CHARS))
    all_blocks = list(blocks)
    present = [b for b in all_blocks if b.text and b.text.strip()]
    required = [b for b in present if b.kind == "required"]
    optional = [b for b in present if b.kind != "required"]

    required_chars = sum(len(b.text) for b in required)
    # 分隔符与尾部说明也要算进预算，否则「块不超」但整段超。
    separator_chars = 2 * max(0, len(present) - 1)
    footer_chars = len(footer) if present else 0
    overhead = separator_chars + footer_chars
    if required_chars + overhead > budget:
        raise ContextBudgetError(
            budget_chars=budget,
            required_chars=required_chars,
            overhead_chars=overhead,
            excess_chars=required_chars + overhead - budget,
            required_labels=[f"{b.label} {len(b.text)} 字" for b in required],
        )

    available = budget - required_chars - overhead
    allocations = _allocate(optional, available)

    final_texts: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for block in all_blocks:
        text = block.text
        original = len(text)
        status = "included"
        reason_code = "required" if block.kind == "required" else "optional_fit"
        reason = "必需材料，完整注入" if block.kind == "required" else "可选材料，预算内完整注入"
        if not text or not text.strip():
            status = "omitted"
            reason_code = "empty_source"
            reason = block.empty_note
            text = ""
        elif block.kind == "required":
            text = block.text
        else:
            allowed = allocations.get(block.id, 0)
            if allowed >= original:
                text = block.text
            elif allowed >= MIN_OPTIONAL_KEEP:
                text = _trim_to(block.text, allowed)
                status = "trimmed"
                reason_code = "budget_trim"
                reason = (
                    f"可选材料按长度比例分到 {allowed} 字，原文 {original} 字，"
                    f"少注入 {original - len(text)} 字"
                )
            else:
                text = ""
                status = "omitted"
                reason_code = "budget_share_too_small"
                reason = (
                    f"按长度比例只分到 {allowed} 字，低于最小可读阈值 "
                    f"{MIN_OPTIONAL_KEEP} 字，整块不注入（原文 {original} 字）"
                )
        if text:
            final_texts[block.id] = text
        rows.append(
            {
                "id": block.id,
                "label": block.label,
                "kind": block.kind,
                "source": block.source,
                "chars": len(text),
                "original_chars": original,
                "status": status,
                "reason_code": reason_code,
                "reason": reason,
                "sha256": _sha256(block.text),
            }
        )

    ordered_texts = [final_texts[b.id] for b in all_blocks if b.id in final_texts]
    assembled = "\n\n".join(ordered_texts)
    if assembled and footer:
        assembled += footer
    block_chars = sum(len(t) for t in ordered_texts)
    accounted = block_chars + (2 * max(0, len(ordered_texts) - 1)) + (len(footer) if assembled else 0)
    report = {
        "schema_version": CONTEXT_BUDGET_SCHEMA,
        "budget_chars": budget,
        "used_chars": len(assembled),
        "block_chars": block_chars,
        "separator_chars": 2 * max(0, len(ordered_texts) - 1),
        "footer_chars": len(footer) if assembled else 0,
        "accounted_chars": accounted,
        "exact": accounted == len(assembled),
        "remaining_chars": max(0, budget - len(assembled)),
        "required_chars": required_chars,
        "optional_chars": sum(
            len(final_texts[b.id]) for b in optional if b.id in final_texts
        ),
        "text_sha256": _sha256(assembled),
        "blocks": rows,
    }
    return assembled, report


def manifest_path(work_dir: str | Path, chapter_id: str) -> Path:
    return Path(work_dir) / "90-draft" / f"{chapter_id}.context.json"


def save_manifest(
    work_dir: str | Path,
    chapter_id: str,
    report: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    """写 `<章节>.context.json`。生成前就写（失败也有据可查），生成后覆盖成终态。"""
    path = manifest_path(work_dir, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CONTEXT_MANIFEST_SCHEMA,
        "chapter_id": chapter_id,
        "saved_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "report": report,
    }
    payload.update(extra or {})
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_manifest(work_dir: str | Path, chapter_id: str) -> dict[str, Any] | None:
    path = manifest_path(work_dir, chapter_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
