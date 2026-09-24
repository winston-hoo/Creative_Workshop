"""作品录入：编码统一、章节切分、清洗、完整性核验、清单生成。

全部纯脚本，不调模型。这是唯一一个完全独立于外部服务的模块。

切分的核心不是「看到第X章就切」，而是三步：
  ① 候选识别 —— 找出所有命中模板的行，此时不切
  ② 序列校验 —— 章号必须单调递增、步长符合预期
  ③ 断点修复 —— 缺口区间用宽松模板重扫

序列校验是关键：没有它，你无法区分「第 6 章标题格式写错了」和「这本书本来只有 5 章」。

三条清洗铁律：
  · 只删非作者内容，正文一个字不动
  · 每次删除都写日志
  · 清洗前后都保留
另有两处刻意不做：**不删标题行**（下游要用）、**不做标点规范化**（标点是风格指纹）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

INGEST_VERSION = "1.0.0"
PATTERN_SET = "cn-standard-v1"

# ── 章节标题模板 ──────────────────────────────────────────
# 按优先级依次尝试。命中即记录，但**不立即切分**。

_CN_NUM = "零〇一二三四五六七八九十百千两壹贰叁肆伍陆柒捌玖拾佰仟0-9"

STRICT_CHAPTER_PATTERNS: list[re.Pattern[str]] = [
    # 0  第X章 / 第X回 / 第X节 〔主模板〕
    re.compile(rf"^\s*第\s*([{_CN_NUM}]{{1,10}})\s*[章回节][\s　:：·]*(.{{0,40}}?)\s*$"),
    # 1  Chapter N
    re.compile(r"^\s*Chapter\s+(\d{1,5})[\s:：]*(.{0,40}?)\s*$", re.IGNORECASE),
    # 2  【第X章】/【X】/（N）—— 必须带章回节标记，或括号内就是一个纯数字
    re.compile(
        rf"^\s*[【\[（(]\s*(?:第\s*)?([{_CN_NUM}]{{1,10}})\s*[章回节]?\s*[】\]）)]\s*$"
    ),
]

# 宽松模板只在**缺口区间**使用。只在缺口里用，是为了压低误报。
LOOSE_CHAPTER_PATTERNS: list[re.Pattern[str]] = [
    # 容错：标题里混进了错字或乱码（如「第缌妣380章」「第424钌章」），
    # 允许「第」与数字之间、数字与「章」之间出现少量其他字符。
    # 真实作品里这种输入法/OCR 错字很常见，而这类章节因为标题不匹配而整体丢失。
    # 因为只在缺口里跑、且结果必须落在缺口章号上，误报风险很低。
    re.compile(rf"^\s*第[^\d]{{0,4}}([0-9]{{1,5}})[^\d]{{0,4}}章[\s　:：·]*(.{{0,40}}?)\s*$"),
    # 章X 标题（省略了「第」）
    re.compile(rf"^\s*章\s*([{_CN_NUM}]{{1,10}})[\s　:：·]*(.{{0,40}}?)\s*$"),
    # X、标题 / X.标题 （数字在前，正文里很常见，只在缺口里用）
    re.compile(rf"^\s*([{_CN_NUM}]{{1,10}})\s*[、.．·]\s*(.{{0,40}}?)\s*$"),
    # 第X 后直接跟标题，缺「章」字
    re.compile(rf"^\s*第\s*([{_CN_NUM}]{{1,10}})\s*[\s　:：·]+(.{{0,40}}?)\s*$"),
    # 宽松括号：任何短括号行（正文里极易误报，所以坚决不放严格模板）
    re.compile(r"^\s*[【\[（(]\s*(.{1,30}?)\s*[】\]）)]\s*$"),
]

VOLUME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"^\s*第\s*([{_CN_NUM}]{{1,10}})\s*[卷部篇][\s　:：·]*(.{{0,40}}?)\s*$"),
    re.compile(rf"^\s*[卷部]\s*([{_CN_NUM}]{{1,3}})[\s　:：·]*(.{{0,40}}?)\s*$"),
]

# 标题行里出现这些字符，多半是正文里的引用或对话，不是标题
_QUOTE_CHARS = "「」『』“”\"'‘’"
_TITLE_MAX_LEN = 44

# 目录页判定：连续多少行才算一段目录
_TOC_MIN_RUN = 5

# ── 误切检测 ──────────────────────────────────────────────
#
# 真实案例：一本讲篮球赛的小说里，「第二节很快结束。」被主模板的
# 「第X节」规则命中，正文中间被切出一个章节，标题是「很快结束。」。
# 这类误切不会让章数看起来异常，门禁也拦不住（重建校验照样通过），
# 但章节边界全错了，后面一切以章为单位的分析都建在错误基础上。
#
# 判据不能用「正文太短」——短章作品会被误伤（实测合成测试作品
# 有 37 字的章，是合法的）。改用两个更精确的信号：
#
#   ① **标题形状**：以句号/顿号/逗号等开头或结尾的，几乎一定是正文句子
#      （注意不能算感叹号与问号——「化龙！」这类标题是合法的）
#   ② **正文几乎为空**：只剩标题行本身
_TITLE_SENTENCE_ENDS = "。，、；：…—"
_TITLE_SENTENCE_STARTS = "。，、；："
_NEAR_EMPTY_CHARS = 30

# 盗版站广告与引流的判定。只认铁证，宁可漏报也不误删正文。
_AD_LINE_RE = re.compile(
    r"(https?://)"
    r"|(www\.[\w\-]+\.[a-z]{2,})"
    r"|每日更新|更新合集|搜书神器|txt下载|全集下载|电子书下载"
    r"|(加|进|书友|备用|读者)?(qq|QQ|微信|vx|VX)群"
    r"|(群|号)[:：]?\d{6,}",
    re.IGNORECASE,
)

# 作者自己的场外话。属于作品，只报告不删。
_AUTHOR_NOTE_RE = re.compile(
    r"求月票|求推荐票|求收藏|求订阅|求打赏|作者的话|上架感言|本章说|新书期|首订"
)


# ── 编码 ──────────────────────────────────────────────────


def detect_encoding(data: bytes) -> tuple[str, str]:
    """探测编码并解码。返回 (编码名, 文本)。

    不依赖第三方库：先试 UTF-8 严格模式，失败再试 GB18030，
    用「解码成功 + 中文字符占比」两个指标挑结果。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", data.decode("utf-8-sig")

    candidates = ["utf-8", "gb18030", "big5"]
    best: tuple[str, str, float] | None = None
    for encoding in candidates:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if not text:
            continue
        score = _cjk_ratio(text)
        if best is None or score > best[2]:
            best = (encoding, text, score)
    if best is not None:
        return best[0], best[1]

    # 全失败则宽容解码，并把替换字符的情况暴露出来
    return "utf-8-replace", data.decode("utf-8", errors="replace")


def _cjk_ratio(text: str) -> float:
    sample = text[:20000]
    if not sample:
        return 0.0
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(sample)


def normalize_text(text: str) -> str:
    """只统一换行，不碰任何标点。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\ufeff", "")


# ── 中文数字 ──────────────────────────────────────────────

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8,
    "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}


def cn_to_int(text: str) -> int | None:
    """把中文数字转成整数。第一百零八 → 108，第二十三 → 23，十五 → 15。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None

    total = 0
    section = 0
    digit = 0
    saw_any = False

    for ch in text:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
            saw_any = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if digit == 0:
                # 「十五」这类省略了前导一的写法
                digit = 1
            section += digit * unit
            digit = 0
            saw_any = True
        else:
            return None

    if not saw_any:
        return None
    return total + section + digit


# ── 候选识别 ──────────────────────────────────────────────


# 编号重启判定：章号回落到不大于 RESTART_MAX_NO，且满足任一条件
#   ① 标题行或紧邻的上一非空行里出现分段标记（番外 / 第二部 / 楔子 …）
#   ② 前一个章号已经超过 RESTART_MIN_PREV
# 真实作品里「番外篇」常常重新从第一章开始编号，这不是重号。
RESTART_MAX_NO = 5
RESTART_MIN_PREV = 20
_SECTION_MARKER_RE = re.compile(r"(番外|外传|前传|序章|楔子|尾声|第二部|第三部|第四部)")
_SECTION_MARKER_MAX_LEN = 40

# 缺章报告的阈值：小于等于它才算「缺章」，超过就只是编号跳变
GAP_MAX = 50

_TITLE_HINT_RE = re.compile(
    r"(第\s*[0-9零〇一二三四五六七八九十百千两]{1,10}\s*[章回节])"
    r"|番外|序章|楔子|尾声|外传|前传|章$"
)


@dataclass
class Candidate:
    line_no: int
    offset: int
    chapter_no: int | None
    title: str
    raw_line: str
    pattern_index: int
    loose: bool = False
    section: int = 1
    occurrence: int = 1
    unnumbered_index: int = 0
    """原文里这一章没有编号（如「番外XXX」）时的段内序号。

    这类章节**不占用章号**。初版让它们顺延上一个章号，结果会与后面真正的
    第 N 章撞号、凭空造出重号，还会掩盖真实的缺口。
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "chapter_no": self.chapter_no,
            "section": self.section,
            "occurrence": self.occurrence,
            "unnumbered_index": self.unnumbered_index,
            "title": self.title,
            "raw_line": self.raw_line,
            "loose": self.loose,
        }


@dataclass
class Volume:
    vol_no: int
    title: str
    offset: int


@dataclass
class Anomaly:
    kind: str
    detail: str
    line_no: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "line_no": self.line_no}


def _looks_like_body_reference(raw_line: str) -> bool:
    """标题行里带引号，多半是正文引用或对话，排除掉。"""
    return any(ch in raw_line for ch in _QUOTE_CHARS)


def find_candidates(
    text: str,
    patterns: Iterable[re.Pattern[str]] = STRICT_CHAPTER_PATTERNS,
    *,
    loose: bool = False,
    start_offset: int = 0,
    end_offset: int | None = None,
    extra_patterns: list[str] | None = None,
) -> list[Candidate]:
    """扫描文本，返回候选标题行。**此时不做任何切分。**

    extra_patterns 是这本书特有的标题写法（来自 work.yaml），
    编译失败会被显式报告而不是静默忽略——规则写错必须看得见。
    """
    compiled: list[re.Pattern[str]] = list(patterns)
    for raw in extra_patterns or []:
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            raise ValueError(f"work.yaml 里的章节模板无法编译：{raw!r} —— {exc}") from exc

    limit = len(text) if end_offset is None else end_offset
    out: list[Candidate] = []
    offset = 0

    for line_no, line in enumerate(text.split("\n"), start=1):
        line_start = offset
        offset += len(line) + 1  # +1 为换行符
        if line_start < start_offset or line_start >= limit:
            continue
        stripped = line.strip()
        if not stripped or len(stripped) > _TITLE_MAX_LEN:
            continue
        if _looks_like_body_reference(stripped):
            continue

        for idx, pattern in enumerate(compiled):
            match = pattern.match(stripped)
            if not match:
                continue
            groups = match.groups()
            if len(groups) >= 2:
                chapter_no = cn_to_int(groups[0])
                title = (groups[1] or "").strip()
            else:
                # 单捕获组的模板（如编号无关的「番外XXX」）：编号按顺序补
                chapter_no = None
                title = (groups[0] or "").strip()
            out.append(
                Candidate(
                    line_no=line_no,
                    offset=line_start,
                    chapter_no=chapter_no,
                    title=title,
                    raw_line=line,
                    pattern_index=idx,
                    loose=loose,
                )
            )
            break
    return out


def _detect_gaps(candidates: list[Candidate]) -> list[Anomaly]:
    """在**分段内**检测章号缺口。跨分段不算缺口（番外篇本来就重新编号）。

    小缺口（≤ GAP_MAX）算「缺章」，需要人核对；
    大跳变只当编号跳变记录下来——真实作品里大跳变通常是标题错字或分部编号，
    报成「缺 431 章」只会淹没真正需要处理的告警。
    """
    out: list[Anomaly] = []
    by_section: dict[int, list[int]] = {}
    for cand in candidates:
        if cand.chapter_no is not None:
            by_section.setdefault(cand.section, []).append(cand.chapter_no)

    for section, numbers in by_section.items():
        uniq = sorted(set(numbers))
        for prev, curr in zip(uniq, uniq[1:]):
            delta = curr - prev
            if delta <= 1:
                continue
            if delta <= GAP_MAX:
                out.append(
                    Anomaly(
                        kind="missing_chapter",
                        detail=(
                            f"第 {section} 段内第{prev}章与第{curr}章之间缺 {delta - 1} 章"
                            "（宽松模板也没找到，请人工核对该区段）"
                        ),
                    )
                )
            else:
                out.append(
                    Anomaly(
                        kind="numbering_jump",
                        detail=(
                            f"第 {section} 段内章号从 {prev} 跳到 {curr}（跨 {delta}）不再当作缺章。"
                            "常见原因是标题写错数字或分部重新编号，建议核对这一处"
                        ),
                    )
                )
    return out


def find_volumes(text: str) -> list[Volume]:
    out: list[Volume] = []
    offset = 0
    for line in text.split("\n"):
        line_start = offset
        offset += len(line) + 1
        stripped = line.strip()
        if not stripped or len(stripped) > _TITLE_MAX_LEN:
            continue
        for pattern in VOLUME_PATTERNS:
            match = pattern.match(stripped)
            if match:
                vol_no = cn_to_int(match.group(1)) or 0
                out.append(Volume(vol_no=vol_no, title=(match.group(2) or "").strip(), offset=line_start))
                break
    return out


# ── 序列校验与断点修复 ────────────────────────────────────


def _section_marker(cand: Candidate, text: str | None) -> str | None:
    """判断这个候选是不是新分段的开头。

    先看标题行自己（「第一章 红旗蛮眼熟(番外)」这种），
    再看紧邻的上一非空行（「未曾设想的道路(番外篇)」这种）。

    用语义标记而不是纯章号阈值，是因为阈值要靠章号积累，一本短书或早期就重启的书
    会判不出来；而标记行是作者自己写的，最贴近本意。
    """
    if _SECTION_MARKER_RE.search(cand.raw_line):
        return cand.raw_line.strip()
    if text is None:
        return None
    prev_lines = [line.strip() for line in text[: cand.offset].split("\n") if line.strip()]
    if prev_lines:
        last = prev_lines[-1]
        if len(last) <= _SECTION_MARKER_MAX_LEN and _SECTION_MARKER_RE.search(last):
            return last
    return None


def validate_sequence(
    candidates: list[Candidate], text: str | None = None
) -> tuple[list[Candidate], list[Anomaly]]:
    """给候选分配分段与出现序号，并记录异常。

    ⚠️ 这个函数**绝不丢弃任何候选**。

    初版的做法是「重号就丢掉第二个」，在真实作品上直接导致静默丢内容：这本书里
      · 行 15633「第376章」与行 16110「第376章 中原乱战三」是**两章同号、内容不同**
      · 行 19020「第一章 红旗蛮眼熟(番外)」是**番外篇的编号重启**
    两种情况都会被当成重号丢掉，而丢掉的那一章的正文会被并进上一章——
    看起来只是少了一章，实际上是内容被悄悄拼接了。这类失效最难发现，必须从设计上排除。
    """
    anomalies: list[Anomaly] = []
    out: list[Candidate] = []
    last_no = 0
    section = 1
    counts: dict[int, int] = {}
    unnumbered_counter = 0

    for cand in candidates:
        no = cand.chapter_no

        # 原文里没有编号的章节（如「番外XXX」）：不占用章号，只给段内序号。
        # 这样既不会与真正的第 N 章撞号，也不会把真实的缺口填掉。
        if no is None:
            unnumbered_counter += 1
            cand.unnumbered_index = unnumbered_counter
            cand.section = section
            cand.occurrence = 1
            out.append(cand)
            continue

        # 编号重启：判定为新分段（番外篇、第二部之类），而不是重号
        if no <= RESTART_MAX_NO and (
            _section_marker(cand, text) is not None or last_no > RESTART_MIN_PREV
        ):
            marker = _section_marker(cand, text)
            reason = f"依据标记「{marker}」" if marker else f"依据章号从 {last_no} 回落"
            anomalies.append(
                Anomaly(
                    kind="section_restart",
                    detail=(
                        f"行 {cand.line_no} 章号回到 {no}，{reason}，"
                        f"判定为新分段，已开启第 {section + 1} 段"
                    ),
                    line_no=cand.line_no,
                )
            )
            section += 1
            counts.clear()
            last_no = 0
            unnumbered_counter = 0

        counts[no] = counts.get(no, 0) + 1
        cand.section = section
        cand.occurrence = counts[no]

        if counts[no] > 1:
            anomalies.append(
                Anomaly(
                    kind="duplicate_chapter",
                    detail=(
                        f"第{no}章在同一分段内第 {counts[no]} 次出现（行 {cand.line_no}），"
                        "已保留并加字母后缀，请人工确认是重复内容还是编号冲突"
                    ),
                    line_no=cand.line_no,
                )
            )
        if no < last_no:
            anomalies.append(
                Anomaly(
                    kind="out_of_order",
                    detail=(
                        f"行 {cand.line_no} 章号 {no} 小于前一个 {last_no}，"
                        "可能是编号错误，已保留原文"
                    ),
                    line_no=cand.line_no,
                )
            )

        # 用「前一个章号」而不是「历史最大值」。
        # 初版用最大值，结果一个错字（第9229章实为第922章）会把此后每一章
        # 都判成乱序，凭空造出 150 条告警。改用一个错字只影响它自己也只影响一次。
        last_no = no
        out.append(cand)

    return out, anomalies


def find_unmatched_title_like(
    text: str,
    matched_offsets: set[int],
    *,
    max_len: int = 30,
) -> list[dict[str, Any]]:
    """找出「看起来像章节标题、但没有任何模板命中」的行。

    这是格式变体的探针。真实作品里番外章节往往换个写法（如「番外进击的东北空军上」），
    严格模板全部落空，于是这些内容会被悄悄并进上一章。只看章节总数发现不了，
    必须主动把这些行捞出来给人看。
    """
    out: list[dict[str, Any]] = []
    offset = 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        line_start = offset
        offset += len(line) + 1
        if line_start in matched_offsets:
            continue
        stripped = line.strip()
        if not stripped or len(stripped) > max_len:
            continue
        if any(ch in stripped for ch in _QUOTE_CHARS):
            continue
        if not _TITLE_HINT_RE.search(stripped):
            continue
        # 以句末标点收尾的多半是正文句子，不是标题
        if stripped[-1] in "。！？，、；：":
            continue
        out.append({"line_no": line_no, "text": stripped})
    return out


def repair_gaps(
    text: str, candidates: list[Candidate]
) -> tuple[list[Candidate], list[Anomaly]]:
    """在缺口区间用宽松模板重扫。

    只在缺口里用宽松模板，是为了压低误报——全文用宽松模板会把正文里的
    「三、他抬起头」这类句子也当成标题。
    """
    if not candidates:
        return candidates, []

    repairs: list[Anomaly] = []
    added: list[Candidate] = []

    ordered = sorted(candidates, key=lambda c: c.offset)
    for prev, curr in zip(ordered, ordered[1:]):
        prev_no = prev.chapter_no or 0
        curr_no = curr.chapter_no or 0
        if curr_no - prev_no <= 1:
            continue

        found = find_candidates(
            text,
            LOOSE_CHAPTER_PATTERNS,
            loose=True,
            start_offset=prev.offset + len(prev.raw_line),
            end_offset=curr.offset,
        )
        wanted = set(range(prev_no + 1, curr_no))
        for cand in found:
            if cand.chapter_no in wanted:
                if prev_no < (cand.chapter_no or 0) < curr_no:
                    added.append(cand)
                    repairs.append(
                        Anomaly(
                            kind="repaired_chapter",
                            detail=(
                                f"用宽松模板找回第{cand.chapter_no}章（原题「{cand.raw_line.strip()}」）"
                            ),
                            line_no=cand.line_no,
                        )
                    )

    merged = sorted(candidates + added, key=lambda c: c.offset)
    # 去重（同一行被两种模板命中）
    deduped: list[Candidate] = []
    seen_offsets: set[int] = set()
    for cand in merged:
        if cand.offset in seen_offsets:
            continue
        seen_offsets.add(cand.offset)
        deduped.append(cand)
    return deduped, repairs


# ── 目录页排除 ────────────────────────────────────────────


def detect_toc(text: str, candidates: list[Candidate]) -> list[Anomaly]:
    """兼容旧调用的薄封装。真正的实现是 split_toc_and_chapters。"""
    _, _, anomalies = split_toc_and_chapters(text, candidates)
    return anomalies


def split_toc_and_chapters(
    text: str, candidates: list[Candidate]
) -> tuple[list[Candidate], list[Candidate], list[Anomaly]]:
    """把文首的目录页块与真实章节分开。返回 (章节候选, 目录候选, 异常)。

    目录页的判定：**连续标题行之间只允许有空行**。

    实测修正：初版允许中间有少量非空文本（阈值 30 字），结果卷标题「卷一 关山」
    只有 4 个字，没超过阈值，把真实的第一章也吞进了目录块——第一章整章丢失。
    目录页的定义本来就该是「标题行之间除了空行什么都没有」，
    只要出现一行别的文字（卷标题、正文），这段连续就结束了。
    """
    if len(candidates) < _TOC_MIN_RUN:
        return candidates, [], []

    ordered = sorted(candidates, key=lambda c: c.offset)
    toc_end_index = 0
    for i in range(1, len(ordered)):
        gap = text[ordered[i - 1].offset + len(ordered[i - 1].raw_line) : ordered[i].offset]
        if any(line.strip() for line in gap.split("\n")):
            break
        toc_end_index = i

    run_len = toc_end_index + 1
    first = ordered[0]
    if run_len < _TOC_MIN_RUN or first.offset > len(text) * 0.05:
        return candidates, [], []

    toc = ordered[:run_len]
    chapters = ordered[run_len:]
    anomaly = Anomaly(
        kind="toc_block",
        detail=(
            f"识别到目录页：行 {first.line_no} 起连续 {run_len} 个标题，"
            "中间除了空行没有其他文字，已从章节序列排除"
        ),
        line_no=first.line_no,
    )
    return chapters, toc, [anomaly]


# ── 清洗 ──────────────────────────────────────────────────


@dataclass
class CleaningLog:
    entries: list[dict[str, Any]] = field(default_factory=list)
    context: str = ""

    def add(self, rule: str, line_no: int, removed: str, reason: str) -> None:
        self.entries.append(
            {
                "chapter": self.context,
                "rule": rule,
                "line_no": line_no,
                "removed": removed,
                "reason": reason,
            }
        )

    @property
    def removed_chars(self) -> int:
        return sum(len(e["removed"]) for e in self.entries)


def clean_text(
    text: str,
    log: CleaningLog,
    *,
    chapter_id: str = "",
    repeated_lines: set[str] | None = None,
    ad_lines: set[str] | None = None,
) -> str:
    """清洗。只删非作者内容；标题行保留；标点不动。

    删除优先级：广告引流 > 重复行 > 空白规整。
    广告放在最前面判，避免它们先被当成「重复行」记成别的规则名。
    """
    log.context = chapter_id
    normalized = normalize_text(text)
    lines = normalized.split("\n")
    kept: list[str] = []

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if ad_lines and stripped and stripped in ad_lines:
            log.add("ad_line", line_no, line, "盗版站广告或引流文本，非作者内容")
            continue
        if repeated_lines and stripped and stripped in repeated_lines:
            log.add("repeated_line", line_no, line, "重复出现的页眉页脚类文本")
            continue
        stripped_right = line.rstrip(" \t\u3000")
        if stripped_right != line:
            log.add("trailing_whitespace", line_no, line[len(stripped_right) :], "行尾空白")
        kept.append(stripped_right)

    # 归并 3 行以上的连续空行
    merged: list[str] = []
    blank_run = 0
    for line in kept:
        if line.strip():
            blank_run = 0
            merged.append(line)
            continue
        blank_run += 1
        if blank_run <= 2:
            merged.append(line)
        else:
            log.add("collapsed_blank", 0, "\n", "连续空行归并为 2 行")

    out = "\n".join(merged)
    out = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", out)
    return out


def find_ad_lines(text: str) -> dict[str, int]:
    """找出**盗版站广告与引流行**。这类行不是作者内容，混在正文里会污染字数与标注。

    只认两类铁证：URL，以及「每日更新 / 搜书神器 / 加群 / txt下载」这类引流话术。

    ⚠️ 刻意**不**匹配「求月票 / 求推荐票 / 作者的话」——那是作者自己写的，
    属于作品的一部分，不能当广告删掉。它们由 find_author_notes 单独报告。
    """
    counts: dict[str, int] = {}
    for line in normalize_text(text).split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        if _AD_LINE_RE.search(stripped):
            counts[stripped] = counts.get(stripped, 0) + 1
    return counts


def find_author_notes(text: str) -> list[dict[str, Any]]:
    """找出作者自己的场外话。只报告，不删——它们是作品的一部分。"""
    out: list[dict[str, Any]] = []
    for line_no, line in enumerate(normalize_text(text).split("\n"), start=1):
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            continue
        if _AUTHOR_NOTE_RE.search(stripped):
            out.append({"line_no": line_no, "text": stripped})
    return out


def find_repeated_lines(text: str, *, min_count: int = 5, max_len: int = 40) -> dict[str, int]:
    """找出反复出现的短行，多半是页眉页脚或水印。

    只报告不自动删除——误删正文的代价远大于留一行页眉。
    真正删除需要显式打开开关。
    """
    counts: dict[str, int] = {}
    for line in normalize_text(text).split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > max_len:
            continue
        counts[stripped] = counts.get(stripped, 0) + 1
    return {k: v for k, v in counts.items() if v >= min_count}


# ── 录入主流程 ────────────────────────────────────────────


@dataclass
class Chapter:
    chapter_id: str
    vol_no: int
    chapter_no: int | None
    title: str
    raw_text: str
    cleaned_text: str
    offset: int
    line_no: int
    status: str = "ok"
    unnumbered_index: int = 0

    @property
    def raw_chars(self) -> int:
        return count_chars(self.raw_text)

    @property
    def cleaned_chars(self) -> int:
        return count_chars(self.cleaned_text)


def count_chars(text: str) -> int:
    """非空白字符数。中文场景下这是最贴近「字数」的口径。"""
    return sum(1 for ch in text if not ch.isspace())


def summarize_patterns(candidates: list[Candidate]) -> list[dict[str, Any]]:
    """按模板统计命中分布。

    这是给人看的第一道防线。真实作品上第一次跑就证明了它的价值：
    主模板命中 1056 次，另两个模板各命中 17 和 20 次——而它们命中的
    全是正文里的「7.92毫米毛瑟步枪弹」「1.双方基于平等互利原则」和作者注。
    只看章节总数是发现不了这种事的，分布一眼就看出来了。
    """
    by_index: dict[int, list[Candidate]] = {}
    for cand in candidates:
        by_index.setdefault(cand.pattern_index, []).append(cand)

    total = len(candidates)
    rows: list[dict[str, Any]] = []
    for index, items in sorted(by_index.items(), key=lambda kv: -len(kv[1])):
        pattern = STRICT_CHAPTER_PATTERNS[index] if index < len(STRICT_CHAPTER_PATTERNS) else None
        share = len(items) / total if total else 0.0
        rows.append(
            {
                "pattern_index": index,
                "pattern": pattern.pattern if pattern else "",
                "count": len(items),
                "share": round(share, 4),
                # 命中占比过低基本可以断定是误报
                "suspected_noise": total > 50 and share < 0.05,
                "samples": [c.raw_line.strip()[:44] for c in items[:3]],
            }
        )
    return rows


def ingest(
    source_path: str | Path,
    *,
    work_name: str = "",
    strip_repeated: bool = False,
    strip_ads: bool = False,
    extra_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """读文件 → 切分 → 清洗 → 核验 → 返回完整录入结果（不落盘）。

    extra_patterns 来自 work.yaml 的 ingest.chapter_patterns，用于处理这本书特有的
    标题写法（例如番外章节写成「番外XXX」而不是「第N章」）。
    strip_ads / strip_repeated 默认都关闭：先报告，确认了再删。
    """
    path = Path(source_path)
    data = path.read_bytes()
    encoding, text = detect_encoding(data)
    source_text = normalize_text(text)
    source_sha = hashlib.sha256(data).hexdigest()

    candidates = find_candidates(source_text, extra_patterns=extra_patterns)
    chapters_cand, toc_cand, toc_anomalies = split_toc_and_chapters(source_text, candidates)

    # 先修复缺口，再分配分段与序号。
    # 顺序不能反：修复要用到模式里解析出的章号来判断缺口。
    repaired, repair_anomalies = repair_gaps(source_text, chapters_cand)
    assigned, seq_anomalies = validate_sequence(repaired, source_text)
    gap_anomalies = _detect_gaps(assigned)

    # 格式变体探针：像章节标题但没被任何模板命中的行
    matched_offsets = {c.offset for c in assigned} | {c.offset for c in toc_cand}
    unmatched = find_unmatched_title_like(source_text, matched_offsets)

    volumes = find_volumes(source_text)
    # 重复短行**总是**统计并报告；只有显式打开开关才会真的删除。
    # 误删正文的代价远大于留一行页眉，所以默认只报告。
    repeated = find_repeated_lines(source_text)
    suspected_ads = find_ad_lines(source_text)
    author_notes = find_author_notes(source_text)

    order = sorted(assigned, key=lambda c: c.offset)
    pre_text = source_text[: order[0].offset] if order else source_text

    chapters: list[Chapter] = []
    cleaning_log = CleaningLog()
    for idx, cand in enumerate(order):
        end = order[idx + 1].offset if idx + 1 < len(order) else len(source_text)
        raw = source_text[cand.offset:end]
        # vol_no 用分段号（section）：一本连续编号的书只有 1 段，
        # 出现番外篇这类编号重启时自动变成第 2 段，不会与正文撞号。
        vol_no = cand.section
        chapter_no = cand.chapter_no
        # 同号多次出现时加字母后缀，保证文件唯一且**一条都不丢**
        suffix = "" if cand.occurrence == 1 else chr(ord("a") + cand.occurrence - 1)
        if chapter_no is None:
            # 原文没给编号：用 x 段位，与编号章节分开，读者一眼能看出这是无编号章节
            chapter_id = f"v{vol_no:03d}-x{cand.unnumbered_index:04d}"
        else:
            chapter_id = f"v{vol_no:03d}-c{chapter_no:04d}{suffix}"
        cleaned = clean_text(
            raw,
            cleaning_log,
            chapter_id=chapter_id,
            repeated_lines=set(repeated) if strip_repeated else None,
            ad_lines=set(suspected_ads) if strip_ads else None,
        )
        chapters.append(
            Chapter(
                chapter_id=chapter_id,
                vol_no=vol_no,
                chapter_no=chapter_no,
                title=cand.title,
                raw_text=raw,
                cleaned_text=cleaned,
                offset=cand.offset,
                line_no=cand.line_no,
                status="repaired" if cand.loose else "ok",
                unnumbered_index=cand.unnumbered_index,
            )
        )

    short_anomalies: list[Anomaly] = []
    for ch in chapters:
        title = ch.title or ""
        body_chars = count_chars(ch.cleaned_text)

        if title and (
            title[0] in _TITLE_SENTENCE_STARTS or title[-1] in _TITLE_SENTENCE_ENDS
        ):
            short_anomalies.append(
                Anomaly(
                    kind="suspicious_title_shape",
                    detail=(
                        f"{ch.chapter_id} 的标题「{title}」以标点开头或结尾，"
                        "看起来是正文里的句子被当成了标题（例如把「第二节」误判成章节标记）。"
                        "请核对该处上下文"
                    ),
                    line_no=ch.line_no,
                )
            )
        elif body_chars < _NEAR_EMPTY_CHARS:
            short_anomalies.append(
                Anomaly(
                    kind="near_empty_chapter",
                    detail=(
                        f"{ch.chapter_id}「{title}」正文只有 {body_chars} 字，"
                        "几乎只剩标题行本身，疑似误切或原文本身残缺。请核对该处上下文"
                    ),
                    line_no=ch.line_no,
                )
            )

    # 卷汇总。vol_no 即分段号：连续编号的书只有 1 段，编号重启会开出新段。
    vol_groups: dict[int, list[Chapter]] = {}
    for ch in chapters:
        vol_groups.setdefault(ch.vol_no, []).append(ch)
    volumes_out = [
        {
            "vol_no": vol_no,
            "title": next((v.title for v in volumes if v.vol_no == vol_no), ""),
            "start_chapter": min(
                (c.chapter_no for c in items if c.chapter_no is not None), default=None
            ),
            "end_chapter": max(
                (c.chapter_no for c in items if c.chapter_no is not None), default=None
            ),
            "chapter_count": len(items),
            "unnumbered_count": sum(1 for c in items if c.chapter_no is None),
        }
        for vol_no, items in sorted(vol_groups.items())
    ]

    anomalies = (
        toc_anomalies + repair_anomalies + seq_anomalies + gap_anomalies + short_anomalies
    )
    integrity = _check_integrity(source_text, order, chapters, pre_text)
    byte_offsets = _byte_offsets(source_text, [ch.offset for ch in chapters])

    manifest: dict[str, Any] = {
        "work": work_name or path.stem,
        "ingest_version": INGEST_VERSION,
        "pattern_set": PATTERN_SET,
        "source": {
            "file": path.name,
            "bytes": len(data),
            "sha256": source_sha,
            "detected_encoding": encoding,
            "normalized_encoding": "utf-8",
            "total_chars_raw": count_chars(source_text),
            "line_count": source_text.count("\n") + 1,
        },
        "volumes": volumes_out,
        "chapters": [
            {
                "id": ch.chapter_id,
                "vol_no": ch.vol_no,
                "chapter_no": ch.chapter_no,
                "title": ch.title,
                "line_no": ch.line_no,
                "byte_offset_start": byte_offset,
                "char_count": ch.cleaned_chars,
                "char_count_raw": ch.raw_chars,
                "content_sha256": hashlib.sha256(ch.cleaned_text.encode("utf-8")).hexdigest(),
                "status": ch.status,
            }
            for ch, byte_offset in zip(chapters, byte_offsets)
        ],
        "unmatched_title_like": unmatched,
        "declared_volumes": [
            {"vol_no": v.vol_no, "title": v.title, "line_no": source_text[: v.offset].count("\n") + 1}
            for v in volumes
        ],
        "anomalies": [a.to_dict() for a in anomalies],
        "pattern_stats": summarize_patterns(candidates),
        "toc_excluded": [c.to_dict() for c in toc_cand],
        "repeated_lines_reported": repeated,
        "suspected_ads": suspected_ads,
        "author_notes": author_notes,
        "cleaning": {
            "removed_chars": cleaning_log.removed_chars,
            "entry_count": len(cleaning_log.entries),
            "pre_chapter_chars": count_chars(pre_text),
        },
        "integrity": integrity,
        "_chapters": chapters,
        "_pre_text": pre_text,
        "_cleaning_log": cleaning_log.entries,
    }
    return manifest


def _vol_of(volumes: list[Volume], offset: int) -> int:
    vol_no = 1
    for vol in sorted(volumes, key=lambda v: v.offset):
        if vol.offset <= offset:
            vol_no = vol.vol_no or vol_no
        else:
            break
    return vol_no


def _byte_offsets(text: str, char_offsets: list[int]) -> list[int]:
    """把字符偏移批量换算成字节偏移。

    逐章做 `text[:offset].encode()` 是 O(n²)：一本三百万字的书会卡到不能看。
    这里只遍历一遍，累计推进。

    字符偏移与字节偏移都留着是有意的——字符偏移用于内部定位，
    字节偏移是给外部工具（编辑器、grep、二进制查看）做回溯锚点用的。
    """
    out: list[int] = []
    prev_char = 0
    prev_bytes = 0
    for offset in char_offsets:
        if offset > prev_char:
            prev_bytes += len(text[prev_char:offset].encode("utf-8"))
            prev_char = offset
        out.append(prev_bytes)
    return out


def _check_integrity(
    source_text: str,
    order: list[Candidate],
    chapters: list[Chapter],
    pre_text: str,
) -> dict[str, Any]:
    """完整性核验。

    核心是**重建校验**：把所有章节的原始切片与章前区段按原顺序拼回去，
    必须与原文件逐字一致。这比对比字数强得多——它连切片偏移算错都能抓出来。
    """
    rebuilt_parts = [pre_text] + [ch.raw_text for ch in chapters]
    rebuilt = "".join(rebuilt_parts)
    reconstruction_ok = rebuilt == source_text

    total = count_chars(source_text)
    chapters_chars = sum(ch.raw_chars for ch in chapters)
    pre_chars = count_chars(pre_text)

    # 逐章校验：记录的原始字数必须等于切片实际字数
    slice_errors = [
        ch.chapter_id
        for ch in chapters
        if ch.raw_chars != count_chars(source_text[ch.offset : ch.offset + len(ch.raw_text)])
    ]

    delta = total - (chapters_chars + pre_chars)
    delta_ratio = abs(delta) / total if total else 0.0

    return {
        "reconstruction_ok": reconstruction_ok,
        "rebuilt_chars": count_chars(rebuilt),
        "source_chars": total,
        "sum_chapter_chars_raw": chapters_chars,
        "pre_chapter_chars": pre_chars,
        "delta": delta,
        "delta_ratio": round(delta_ratio, 6),
        "slice_errors": slice_errors,
        "passed": reconstruction_ok and not slice_errors and delta_ratio <= 0.005,
    }


# ── 落盘 ──────────────────────────────────────────────────


def save_ingest(result: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    """落盘：章节文件（清洗前后各一份）+ 清单 + 清洗日志 + 复核队列。

    重跑时**先清走陈旧文件**。改了切分规则之后重跑，上一轮的产物可能不再属于任何章节
    （实测过一次：番外从「并入上一章」改成「独立章节」后，留下一个悬空的重号文件，
    目录 1066 个文件而清单只有 1065 章，两边对不上）。
    陈旧文件移入 `_review/stale/` 而不是删除——目录和清单不一致这件事本身要留痕。
    """
    out = Path(out_dir)
    chapters_dir = out / "chapters"
    raw_dir = out / "raw-chapters"
    review_dir = out / "_review"
    stale_dir = review_dir / "stale"
    for d in (chapters_dir, raw_dir, review_dir):
        d.mkdir(parents=True, exist_ok=True)

    chapters: list[Chapter] = result.get("_chapters") or []
    expected = {f"{ch.chapter_id}.md" for ch in chapters}

    stale_moved: list[str] = []
    for directory in (chapters_dir, raw_dir):
        for existing in sorted(directory.glob("*.md")):
            if existing.name in expected:
                continue
            stale_dir.mkdir(parents=True, exist_ok=True)
            target = stale_dir / f"{directory.name}__{existing.name}"
            existing.replace(target)
            stale_moved.append(str(target.relative_to(out)))

    paths: dict[str, Path] = {"chapters_dir": chapters_dir, "raw_dir": raw_dir}

    for ch in chapters:
        front = (
            "---\n"
            f"id: {ch.chapter_id}\n"
            f"vol_no: {ch.vol_no}\n"
            f"chapter_no: {ch.chapter_no if ch.chapter_no is not None else 'null'}\n"
            f'title: "{ch.title}"\n'
            f"char_count: {ch.cleaned_chars}\n"
            f"unnumbered: {'true' if ch.chapter_no is None else 'false'}\n"
            "---\n\n"
        )
        (chapters_dir / f"{ch.chapter_id}.md").write_text(
            front + ch.cleaned_text.strip() + "\n", encoding="utf-8"
        )
        (raw_dir / f"{ch.chapter_id}.md").write_text(ch.raw_text, encoding="utf-8")

    result["stale_files_moved"] = stale_moved

    manifest = {k: v for k, v in result.items() if not k.startswith("_")}
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths["manifest"] = manifest_path

    log_path = out / "cleaning-log.jsonl"
    with log_path.open("w", encoding="utf-8") as fh:
        for entry in result.get("_cleaning_log") or []:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    paths["cleaning_log"] = log_path

    review_items = [
        a for a in manifest.get("anomalies", []) if a.get("kind") != "repaired_chapter"
    ]
    if review_items:
        review_path = review_dir / "anomalies.json"
        review_path.write_text(
            json.dumps(review_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        paths["review"] = review_path

    if stale_moved:
        paths["stale_dir"] = stale_dir

    return paths


def format_preview(result: dict[str, Any], *, limit: int = 20) -> str:
    """渲染切分预览。导入流程里的第 2 步必须让人看到这个再确认。"""
    src = result.get("source") or {}
    integrity = result.get("integrity") or {}
    chapters: list[Chapter] = result.get("_chapters") or []

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"切分预览  {src.get('file')}")
    lines.append("=" * 60)
    lines.append(f"编码          {src.get('detected_encoding')} → {src.get('normalized_encoding')}")
    lines.append(f"总字数        {src.get('total_chars_raw')}（非空白字符）")
    lines.append(f"总行数        {src.get('line_count')}")
    lines.append(f"识别章节      {len(chapters)} 章 / {len(result.get('volumes') or [])} 段")
    lines.append("")

    unmatched = result.get("unmatched_title_like") or []
    if unmatched:
        lines.append(f"── 格式变体探针：{len(unmatched)} 行像章节标题但未命中任何模板 ──")
        lines.append("   （这些内容会被并进上一章。若确实是章节，请把写法加进 work.yaml）")
        for item in unmatched[:10]:
            lines.append(f"   · 行 {item['line_no']}: 「{item['text'][:40]}」")
        if len(unmatched) > 10:
            lines.append(f"   … 其余 {len(unmatched) - 10} 行略")
        lines.append("")

    lines.append("── 章节样例 ────────────────────────────────")
    for ch in chapters[:limit]:
        flags = ""
        if ch.status == "repaired":
            flags += " 〔修复〕"
        if ch.chapter_no is None:
            flags += " 〔原文无编号〕"
            label = ch.title or "(无标题)"
        else:
            label = f"第{ch.chapter_no}章 {ch.title}".strip()
        lines.append(
            f"  {ch.chapter_id}  {label}  {ch.raw_chars} 字  行 {ch.line_no}{flags}"
        )
    if len(chapters) > limit:
        lines.append(f"  … 其余 {len(chapters) - limit} 章略")
    lines.append("")

    stats = result.get("pattern_stats") or []
    if stats:
        lines.append("── 模板命中分布（重点看占比过低的）────────────")
        for row in stats:
            mark = "  ← 疑似误报" if row.get("suspected_noise") else ""
            lines.append(
                f"  模板{row['pattern_index']}  {row['count']} 次"
                f"（占比 {row['share'] * 100:.1f}%）{mark}"
            )
            lines.append(f"      {row['pattern'][:56]}")
            for sample in row.get("samples") or []:
                lines.append(f"      · {sample}")
        lines.append("")

    anomalies = result.get("anomalies") or []
    lines.append(f"── 异常 {len(anomalies)} 项 ──────────────────────────")
    for a in anomalies[:15]:
        line_info = f"（行 {a['line_no']}）" if a.get("line_no") else ""
        lines.append(f"  · [{a['kind']}]{line_info} {a['detail']}")
    if len(anomalies) > 15:
        lines.append(f"  … 其余 {len(anomalies) - 15} 项略")
    if not anomalies:
        lines.append("  （无）")
    lines.append("")

    repeated = result.get("repeated_lines_reported") or {}
    if repeated:
        lines.append(f"── 疑似页眉页脚 {len(repeated)} 条（仅报告，未删除）────")
        for text, count in list(repeated.items())[:8]:
            lines.append(f"  · 「{text[:30]}」出现 {count} 次")
        lines.append("")

    ads = result.get("suspected_ads") or {}
    if ads:
        lines.append(f"── 疑似盗版站广告 {len(ads)} 条（仅报告，未删除）────")
        lines.append("   （加 --strip-ads 才会真的删掉。它们混在正文里会污染字数与标注）")
        for text, count in list(ads.items())[:10]:
            suffix = f" ×{count}" if count > 1 else ""
            lines.append(f"  · 「{text[:46]}」{suffix}")
        lines.append("")

    notes = result.get("author_notes") or []
    if notes:
        lines.append(f"── 作者场外话 {len(notes)} 条（属于作品，不删）────")
        for item in notes[:5]:
            lines.append(f"  · 行 {item['line_no']}: 「{item['text'][:40]}」")
        lines.append("")

    cleaning = result.get("cleaning") or {}
    lines.append("── 清洗 ────────────────────────────────────")
    lines.append(f"清洗条目      {cleaning.get('entry_count')} 条")
    lines.append(f"删除字符      {cleaning.get('removed_chars')}")
    lines.append(f"章前区段      {cleaning.get('pre_chapter_chars')} 字")
    lines.append("")

    lines.append("── 完整性核验 ──────────────────────────────")
    lines.append(
        f"重建校验      {'通过' if integrity.get('reconstruction_ok') else '未通过'}"
        f"（{integrity.get('rebuilt_chars')} vs {integrity.get('source_chars')}）"
    )
    lines.append(
        f"字数对账      Σ章节 {integrity.get('sum_chapter_chars_raw')}"
        f" + 章前 {integrity.get('pre_chapter_chars')} = "
        f"{integrity.get('source_chars')}  偏差 {integrity.get('delta')}"
    )
    lines.append(f"切片校验      {'通过' if not integrity.get('slice_errors') else '有误'}")
    lines.append("")
    lines.append(f"门禁          {'通过' if integrity.get('passed') else '未通过，不应进入标注阶段'}")

    return "\n".join(lines)
