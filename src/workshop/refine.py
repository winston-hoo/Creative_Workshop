"""按审稿意见局部改写正文（P0 批次 3：D2 / D3）。

## 为什么是「替换」而不是「让模型重写全章」

重写全章有两个问题：作者没法验收（改了什么要逐句找），也没法保留判断权
（不想听的建议会跟着一起进正文）。这里做的是**按偏移替换**：

  · 只动被勾选的条目对应的那几段文字；
  · 替换文本默认是审稿给的 `suggestion`，作者也可以在界面上改；
  · 未勾选的区域字节不变——这不是「尽量」，是构造上就不会变。

偏移来自 `review.normalize_items`，也就是**在落盘那一刻对过原文**的位置。
写入前再核一次原文：正文在这期间被人编辑过、这一句已经不是原来那句，
就跳过这一条并给出原因，绝不硬套。
"""

from __future__ import annotations

from typing import Any

from .review import locate_evidence


def apply_findings(
    prose: str,
    items: list[dict[str, Any]],
    replacements: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把选中的 finding 逐条替换进正文。

    `replacements` 是作者在界面上改过的改写文本；没给的条目用模型建议。
    返回的新文本之外，还带回每条的实际偏移与改动，供界面显示与审计。
    """
    original = prose or ""
    replacements = replacements or {}
    skipped: list[dict[str, Any]] = []
    picks: list[dict[str, Any]] = []

    for item in items:
        fid = str(item.get("id") or "")
        evidence = str(item.get("evidence") or "")
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, int) or not isinstance(end, int) \
                or original[start:end] != evidence:
            spot = locate_evidence(original, evidence)
            if not spot:
                skipped.append({
                    "id": fid, "reason": "正文里已经找不到这句原文（可能被改过或已被采纳）",
                    "evidence": evidence[:40],
                })
                continue
            start, end = spot["start"], spot["end"]
        after = replacements[fid] if fid in replacements else str(item.get("suggestion") or "")
        if after == evidence:
            skipped.append({"id": fid, "reason": "改写文本与原句相同，无需改动"})
            continue
        picks.append({
            "id": fid, "start": int(start), "end": int(end),
            "before": evidence, "after": after,
            "type": item.get("type") or "", "severity": item.get("severity") or "",
        })

    # 从后往前替换，前面的偏移就不会被后面的改动推走。
    picks.sort(key=lambda p: p["start"], reverse=True)
    applied: list[dict[str, Any]] = []
    kept_spans: list[tuple[int, int]] = []
    for pick in picks:
        overlapped = next(
            (span for span in kept_spans
             if not (pick["end"] <= span[0] or pick["start"] >= span[1])),
            None,
        )
        if overlapped:
            skipped.append({
                "id": pick["id"],
                "reason": f"与另一条改写的范围重叠（{overlapped[0]}–{overlapped[1]}），跳过",
            })
            continue
        kept_spans.append((pick["start"], pick["end"]))
        applied.append(pick)

    text = original
    for pick in applied:  # 已按 start 降序
        text = text[:pick["start"]] + pick["after"] + text[pick["end"]:]

    applied.sort(key=lambda p: p["start"])
    changed_chars = sum(len(p["after"]) - (p["end"] - p["start"]) for p in applied)
    return {
        "ok": bool(applied),
        "text": text,
        "original_chars": len(original),
        "chars": len(text),
        "changed_chars": changed_chars,
        "applied": applied,
        "skipped": skipped,
        "regions": len(applied),
    }


def only_selected_changed(
    original: str, updated: str, applied: list[dict[str, Any]]
) -> bool:
    """核验：除了 `applied` 里那几段，其余字节完全一致。

    测试与接口回执都用它。改写是确定性替换，理论上必然成立；
    真要哪天替换逻辑写歪了，这里会立刻红，而不是等作者发现别的段落被动了。

    注意替换前后长度会变，所以要一边走原文游标、一边走新文本游标，
    不能拿同一组偏移去比对两份文本。
    """
    cursor_old = 0
    cursor_new = 0
    for pick in sorted(applied, key=lambda p: p["start"]):
        start, end = int(pick["start"]), int(pick["end"])
        after = str(pick["after"])
        span = start - cursor_old
        if original[cursor_old:start] != updated[cursor_new:cursor_new + span]:
            return False
        cursor_new += span
        if updated[cursor_new:cursor_new + len(after)] != after:
            return False
        cursor_new += len(after)
        cursor_old = end
    return original[cursor_old:] == updated[cursor_new:]
