"""脚本轨字段：不调模型就能算出来的部分。

这一层的存在意义有两层：
  1. 它是免费的。省钱、确定、可重复，不受模型波动影响。
  2. 它挡住了本该由正则完成的活儿。破折号检测用模型是纯浪费。

所以标注结果 = 脚本轨字段 + 模型轨字段，前者本地算，后者才发请求。
"""

from __future__ import annotations

import re
from typing import Any

from .primitives import Primitives, WorkConfig, normalize_text

# 常见的中文引号对。只统计成对出现的内容，落单的引号不计。
_QUOTE_PATTERNS = [
    re.compile(r"「([^」]*)」"),
    re.compile(r"『([^』]*)』"),
    re.compile(r"“([^”]*)”"),
    re.compile(r"『([^』]*)』"),
    re.compile(r"\"([^\"]{2,})\""),
]

_SENTENCE_SPLIT = re.compile(r"[。！？…]+")


def compute_script_fields(
    text: str,
    *,
    vol_no: int,
    chapter_no: int,
    work: WorkConfig,
    primitives: Primitives,
) -> tuple[dict[str, Any], list[str]]:
    """计算全部脚本轨字段。返回 (字段字典, 问题清单)。

    这里的问题清单不是错误，而是「需要人看一眼」的提示，例如风格违规。
    """
    text = normalize_text(text)
    issues: list[str] = []

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    char_count = _count_chars(text)
    para_count = len(paragraphs)
    avg_para_chars = round(char_count / para_count, 1) if para_count else 0.0
    dialogue_ratio = round(_dialogue_ratio(text, char_count), 4)

    style_violations: list[dict[str, Any]] = []
    perspective_detected = ""

    # 人称检测**不需要**作品先声明约定——判出「第一人称还是第三人称」是纯统计；
    # 只有「偏离约定」才需要一个约定作基准。
    # 所以作品没配 perspective 规则时也要照常检测，用一条空约定跑一遍：
    # 检测结果照样给，违规判定自然为空（没有基准就无从判断偏离）。
    # 不这么做的话，没配规则的作品会得到空的 perspective_detected，
    # 界面上看起来像「没数据」，其实只是「没配规则」。
    perspective_check = next(
        (c for c in work.style_checks if str(c.get("type")) == "perspective"), None
    )
    declared_perspective = perspective_check is not None
    detected, perspective_violations = _check_perspective(
        text, paragraphs, perspective_check or {}
    )
    perspective_detected = detected
    if declared_perspective:
        rule_id = str(perspective_check.get("id") or "")
        expected = str(perspective_check.get("expected") or "")
        if expected and detected and detected != expected:
            issues.append(f"{rule_id} 人称检测为「{detected}」，与作品约定「{expected}」不一致")
        if perspective_violations:
            style_violations.append(
                {
                    "rule_id": rule_id,
                    "desc": str(perspective_check.get("desc") or ""),
                    "type": "perspective",
                    "count": len(perspective_violations),
                    "samples": perspective_violations[:5],
                }
            )

    for check in work.style_checks:
        check_type = str(check.get("type") or "")
        rule_id = str(check.get("id") or "")
        desc = str(check.get("desc") or "")

        if check_type == "perspective":
            continue  # 已在上面单独处理

        if check_type == "forbidden_chars":
            chars = [str(c) for c in (check.get("chars") or [])]
            hits = _find_chars(text, chars)
            if hits:
                style_violations.append(
                    {
                        "rule_id": rule_id,
                        "desc": desc,
                        "type": "forbidden_chars",
                        "count": len(hits),
                        "samples": hits[:5],
                    }
                )

        elif check_type == "forbidden_pattern":
            pattern = str(check.get("pattern") or "")
            hits, pattern_error = _find_pattern(text, pattern)
            if pattern_error:
                # 规则本身写错了必须报出来。静默返回空结果等于假装「没有问题」，
                # 这正是我们反复强调要避免的失效模式。
                issues.append(f"{rule_id} 规则配置有误：{pattern_error}")
            if hits:
                style_violations.append(
                    {
                        "rule_id": rule_id,
                        "desc": desc,
                        "type": "forbidden_pattern",
                        "count": len(hits),
                        "samples": hits[:5],
                    }
                )

    if style_violations:
        total = sum(int(v.get("count") or 0) for v in style_violations)
        issues.append(f"检测到 {len(style_violations)} 类风格违规，共 {total} 处")

    fields: dict[str, Any] = {
        "vol_no": vol_no,
        "chapter_no": chapter_no,
        "char_count": char_count,
        "para_count": para_count,
        "avg_para_chars": avg_para_chars,
        "dialogue_ratio": dialogue_ratio,
        "perspective_detected": perspective_detected,
        "style_violations": style_violations,
    }

    known = {f.key for f in primitives.script_fields}
    missing = known - set(fields)
    if missing:
        issues.append(f"有脚本轨原语未实现：{'、'.join(sorted(missing))}")

    return fields, issues


# ── 内部实现 ──────────────────────────────────────────────


def _count_chars(text: str) -> int:
    """非空白字符数。中文场景下这是最贴近「字数」的口径。"""
    return sum(1 for ch in text if not ch.isspace())


def _dialogue_ratio(text: str, char_count: int) -> float:
    if not char_count:
        return 0.0
    dialogue_chars = 0
    for pattern in _QUOTE_PATTERNS:
        for match in pattern.finditer(text):
            dialogue_chars += len(match.group(1))
    return min(1.0, dialogue_chars / char_count)


def _strip_dialogue(text: str) -> str:
    """去掉引号内的对话，只留叙述部分。

    实测修正：某本第三人称小说的第一章被判成「第一人称」，因为整章大量对话里
    人物都在说「我」。对话里的第一人称代词与叙述人称无关——谁说话都会说「我」。
    不剥掉它们，对话越多的章节就越容易被误判。
    """
    out = text
    for pattern in _QUOTE_PATTERNS:
        out = pattern.sub("", out)
    return out


def _check_perspective(
    text: str, paragraphs: list[str], check: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """检测人称，并找出偏离段落。

    两条规则，都是实测逼出来的：

    ① 按**段落多数投票**判定，不用全文累计计数。
       初版用全文计数时，一段第三人称段落（他出现 4 次）就压过了全篇的第一人称叙述，
       把第一人称的作品判成了第三人称。

    ② **只统计叙述部分，剥掉对话**。
       初版把对话里的人物自称也算进去，结果对话密集的第三人称章节被判成第一人称。

    这是启发式判断，用途是**指出可疑位置供人复核**，所以阈值刻意保守，宁可漏报不误报。
    """
    first_markers = [str(m) for m in (check.get("first_person_markers") or ["我"])]
    third_markers = [str(m) for m in (check.get("third_person_markers") or ["他", "她"])]
    expected = str(check.get("expected") or "")

    first_votes = 0
    third_votes = 0
    violations: list[dict[str, Any]] = []
    offset = 0

    for para in paragraphs:
        idx = text.find(para, offset)
        offset = idx + len(para) if idx >= 0 else offset

        narration = _strip_dialogue(para)
        para_first = sum(narration.count(m) for m in first_markers)
        para_third = sum(narration.count(m) for m in third_markers)

        # 投票：只要一段的叙述部分由某一方主导就投一票。
        # 这里**不能**复用违规判定的阈值——投票要的是覆盖面，
        # 违规要的是「强到值得人看一眼」，两者标准不同。
        if para_first == 0 and para_third == 0:
            continue
        if para_first > 0 and para_first >= para_third:
            first_votes += 1
        elif para_third > 0 and para_first == 0:
            third_votes += 1

        # 违规：方向必须**相对于作品约定**来看，而不是固定方向。
        # 初版固定标记「第三人称占主导」的段落，结果在第三人称小说里
        # 每一个叙述段落都符合，等于全在报假警。
        if expected == "第一人称" and para_first == 0 and para_third >= 3:
            violations.append(
                {
                    "offset": idx if idx >= 0 else None,
                    "kind": "narrative_switched_to_third",
                    "third_person_count": para_third,
                    "excerpt": para[:24],
                }
            )
        elif expected == "第三人称" and para_third == 0 and para_first >= 3:
            violations.append(
                {
                    "offset": idx if idx >= 0 else None,
                    "kind": "narrative_switched_to_first",
                    "first_person_count": para_first,
                    "excerpt": para[:24],
                }
            )

    if first_votes == 0 and third_votes == 0:
        detected = "无法判定"
    elif first_votes >= third_votes:
        detected = "第一人称"
    else:
        detected = "第三人称"

    return detected, violations


def _find_chars(text: str, chars: list[str]) -> list[dict[str, Any]]:
    """查找禁用字符，并**去重重叠命中**。

    实测修正：字符表里同时有「——」和「—」时，一个「——」会被报成 3 处
    （长的 1 次 + 短的 2 次）。数量虚高会直接误导风格违规清单的严重程度判断，
    所以按长度从长到短匹配，已被覆盖的位置不再重复统计。
    """
    targets = sorted({c for c in chars if c}, key=len, reverse=True)
    covered: list[tuple[int, int]] = []
    hits: list[dict[str, Any]] = []

    for target in targets:
        start = 0
        while True:
            pos = text.find(target, start)
            if pos < 0:
                break
            end = pos + len(target)
            overlaps = any(pos < c_end and end > c_start for c_start, c_end in covered)
            if not overlaps:
                hits.append(
                    {
                        "offset": pos,
                        "matched": target,
                        "excerpt": text[max(0, pos - 10) : end + 10],
                    }
                )
                covered.append((pos, end))
            start = end

    hits.sort(key=lambda h: h["offset"])
    return hits


def _find_pattern(text: str, pattern: str) -> tuple[list[dict[str, Any]], str | None]:
    """返回 (命中列表, 规则错误)。规则错误必须上抛，不能静默当成「没有违规」。"""
    if not pattern:
        return [], "规则未配置 pattern"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return [], f"正则无法编译：{exc}"
    hits: list[dict[str, Any]] = []
    for match in compiled.finditer(text):
        hits.append({"offset": match.start(), "matched": match.group(0)[:40]})
    return hits, None


def sentence_lengths(text: str) -> list[int]:
    """句长列表。用于后续的句式节奏分析，本身进不了单章标注，只作辅助。"""
    normalized = normalize_text(text)
    return [len(s.strip()) for s in _SENTENCE_SPLIT.split(normalized) if s.strip()]
