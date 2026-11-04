"""截断续写的去重：模型接着写时，最容易先把上一轮的尾巴再复读一遍。

## 为什么单独一层

`finish_reason=length` 后把已写内容作为 assistant 消息回喂，模型常见两种毛病：
  1. 从上一句的**前一句**重新开头，于是正文里出现一段重复；
  2. 干脆把最后一段整段复读，再往下写。

只靠提示词「不要重复」不可靠——实测它会重复，而且重复得很自然，
作者读起来像是"有意反复"的修辞，不翻到拼接处根本看不出来。
所以这里做机械判定：**后缀-前缀最长重叠** + 段内连续重复段落。

判定用字符 n-gram，不做分词。中文没有空格，按词切反而会把专名切碎。
重叠不足 `MIN_OVERLAP` 就不动它——少删一点比误删正文安全。
"""

from __future__ import annotations

import re
from typing import Any

# 至少连续这么多字与上一轮尾巴相同，才判定为复读。
# 6 个字在中文里已足够构成一句可辨认的重复，又不会把「他说」这种常见开头误杀。
MIN_OVERLAP = 6
DEFAULT_NGRAM = 6

# 只回看上一轮末尾这么多字。整章回看会把「他」「的」这类高频片段误判成重叠。
MAX_TAIL_SCAN = 1200


def _norm(text: str) -> str:
    """去空白后再比较，避免模型在拼接处多一个换行就判不出来。"""
    return re.sub(r"\s+", "", text or "")


def longest_suffix_prefix_overlap(
    previous: str,
    continuation: str,
    *,
    ngram: int = DEFAULT_NGRAM,
    max_scan: int = MAX_TAIL_SCAN,
) -> int:
    """返回 `previous` 的后缀与 `continuation` 的前缀最长重合字数（已归一化）。

    返回的是**归一化后的长度**。调用方拿它做判断即可；真正裁剪时用
    `dedupe_continuation`，那里会把原始空白一起处理。
    """
    prev = _norm(previous)[-max_scan:]
    cont = _norm(continuation)
    if not prev or not cont:
        return 0
    limit = min(len(prev), len(cont), max_scan)
    for size in range(limit, 0, -1):
        if prev[-size:] == cont[:size]:
            return size
    return 0


def dedupe_continuation(
    previous_text: str,
    continuation: str,
    *,
    ngram: int = DEFAULT_NGRAM,
) -> tuple[str, dict[str, Any]]:
    """裁掉续写开头复读上一轮的部分，并去掉段内连续重复段落。

    返回 (清理后的续写, 统计)。统计会写进调用账本，便于作者看到
    「这一轮去掉了多少字」，而不是悄悄少了一段。
    """
    cont = (continuation or "").strip()
    stats: dict[str, Any] = {
        "overlap_removed": 0,
        "paragraphs_deduped": 0,
        "original_chars": len(cont),
        "kept_chars": len(cont),
    }
    if not cont:
        return "", stats

    # 先按原始文本找重叠。归一化长度和原始长度在有换行/空格时不等，
    # 所以用归一化长度定位后，从原始串开头逐字推进到对应位置。
    overlap_norm = longest_suffix_prefix_overlap(previous_text, cont, ngram=ngram)
    if overlap_norm >= ngram:
        consumed = 0
        norm_len = 0
        while consumed < len(cont) and norm_len < overlap_norm:
            if not cont[consumed].isspace():
                norm_len += 1
            consumed += 1
        cont = cont[consumed:].lstrip(" \t\r\n")
        stats["overlap_removed"] = overlap_norm

    # 段内连续重复：模型有时会把同一段连写两遍。
    paragraphs = re.split(r"(\n\s*\n)", cont)
    cleaned: list[str] = []
    prev_para_norm = ""
    for piece in paragraphs:
        if not piece.strip():
            cleaned.append(piece)
            continue
        norm = _norm(piece)
        if norm and norm == prev_para_norm and len(norm) >= 18:
            stats["paragraphs_deduped"] += 1
            prev_para_norm = ""
            continue
        cleaned.append(piece)
        prev_para_norm = norm
    cont = "".join(cleaned).strip()
    stats["kept_chars"] = len(cont)
    return cont, stats


def stitch(previous: str, continuation: str) -> str:
    """拼接两轮正文。中文直接接上，不额外插空格。"""
    return (previous or "") + (continuation or "")
