"""M9 创作台 · 第二层：分卷目录。

设定集管「不变的」，分卷目录管「怎么排」：
    卷 → 部分 → 章（章号 / 标题 / 内容概要）

一层一个文件（`70-volume/volume-001.yaml`），对标预案里的「第N卷_详细目录」。
**这一版是人手填的**，不接模型——先把数据形状和校验定下来，确认形状对了再接生成。

校验分两档，跟项目其他地方一致：
  阻断 —— 缺了它下游就没法用（章号断档、卷与卷之间对不上、指向不存在的人物）
  提示 —— 能往下走，但多半是漏了或者前后漂了，值得看一眼

跨卷的序列校验是这一层的重点：**章号连续性是最强的信号**，跟录入切分那边同一个道理。
「第 3 卷少了一章」和「第 3 卷本来就只有 99 章」不靠连续性根本分不出来。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .primitives import yaml_scalar

VOLUME_SCHEMA_VERSION = "volume-v1"
VOLUME_FILE_RE = re.compile(r"^volume-(\d{1,4})\.ya?ml$")


# ── 骨架 ────────────────────────────────────────────────────


def empty_volume(work: str, vol_no: int = 1, start: int = 1, end: int = 0) -> dict[str, Any]:
    return {
        "schema_version": VOLUME_SCHEMA_VERSION,
        "work": work,
        "vol": {
            "vol": vol_no,
            "title": "",
            "start_chapter": start,
            "end_chapter": end,
            "era": "",
            "core_conflict": "",
            "goal": "",
            "closing": "",
            "parts": [],
            "chapters": [],
        },
    }


def volume_template_text(work: str, vol_no: int, start: int, end: int) -> str:
    """给人手填的带注释骨架。跟设定集一样：打开就能填，不用先去读代码。"""
    return f"""# 分卷目录 · 第 {vol_no} 卷
#
# 一层一个文件。卷号写进文件名（volume-{vol_no:03d}.yaml），改卷号别忘了改文件名。
# 章号连续性是最强的信号：断档、重叠、跟上一卷接不上，都会被 --check-volume 抓到。
# 校验：python create_cli.py --check-volume {work} --vol {vol_no}

schema_version: {VOLUME_SCHEMA_VERSION}
work: {yaml_scalar(work)}

vol:
  vol: {vol_no}
  title: ""                  # 卷名
  start_chapter: {start}
  end_chapter: {end}
  era: ""                    # 时间跨度，例：木叶45-46年（约一年半）
  core_conflict: ""          # 这一卷的核心冲突，一句话
  goal: ""                   # 这一卷结束时，主角或世界要变成什么样
  closing: ""                # 卷末总结

  # 部分划分。区间必须在卷内、首尾相接、合起来正好铺满整卷。
  parts: []
  #  - title: 觉醒
  #    start_chapter: {start}
  #    end_chapter: {start + 9}
  #    gist: 这一部分要完成什么

  # 逐章表。章号必须恰好铺满本卷区间，一章不多一章不少。
  chapters: []
  #  - chapter_no: {start}
  #    title: 血红刻印
  #    gist: 谁做了什么，结果如何。只写剧情，不评价。
  #    characters: []        # 出场人物，名字要能在设定集里找到
  #    foreshadow: 埋设      # 埋设 | 推进 | 回收 | 留空
"""


# ── 读写 ────────────────────────────────────────────────────


def load_volume(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"分卷目录格式异常（顶层不是映射）：{p}")
    return data


def save_volume(path: str | Path, data: dict[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )
    return p


def volume_path(volume_dir: str | Path, vol_no: int) -> Path:
    return Path(volume_dir) / f"volume-{vol_no:03d}.yaml"


def list_volume_files(volume_dir: str | Path) -> list[tuple[int, Path]]:
    """列出卷文件，按文件名里的卷号排序。没有卷号的杂项文件直接跳过。"""
    d = Path(volume_dir)
    if not d.exists():
        return []
    out: list[tuple[int, Path]] = []
    for path in sorted(d.iterdir()):
        match = VOLUME_FILE_RE.match(path.name)
        if match:
            out.append((int(match.group(1)), path))
    return sorted(out)


def load_all_volumes(volume_dir: str | Path) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """读全部卷。返回 (卷号 → 卷数据, 读不出来的文件说明)。

    读不出来的**不会被静默跳过**：那一卷当成不存在，会让「覆盖到第几章」这种
    结论悄悄变错，而人还以为只是少了一卷。
    """
    volumes: dict[int, dict[str, Any]] = {}
    broken: list[str] = []
    for vol_no, path in list_volume_files(volume_dir):
        try:
            data = load_volume(path)
        except (ValueError, OSError) as exc:
            broken.append(f"{path.name}：{exc}")
            continue
        data["_file"] = path.name
        vol = data.get("vol") or {}
        declared = vol.get("vol")
        if isinstance(declared, int) and declared != vol_no:
            broken.append(
                f"{path.name}：文件名说第 {vol_no} 卷，里面的 vol.vol 写的是 {declared}——"
                "两处不一致，改哪一处都得同时改另一处"
            )
            continue
        volumes[vol_no] = data
    return volumes, broken


# ── 校验 ────────────────────────────────────────────────────


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chapter_nos(vol: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for ch in vol.get("chapters") or []:
        if isinstance(ch, dict):
            no = _int_or_none(ch.get("chapter_no"))
            if no is not None:
                out.append(no)
    return sorted(out)


def validate_volume(
    data: dict[str, Any],
    *,
    setting: dict[str, Any] | None = None,
    file_vol_no: int | None = None,
) -> tuple[list[str], list[str]]:
    """单卷校验。跨卷的衔接由 validate_chain 管。"""
    errors: list[str] = []
    warnings: list[str] = []
    vol = data.get("vol") or {}
    if not isinstance(vol, dict):
        return ["vol 段不存在或不是映射"], []

    vol_no = _int_or_none(vol.get("vol"))
    if vol_no is None:
        errors.append("vol.vol 没写或不是整数")
    elif file_vol_no is not None and vol_no != file_vol_no:
        errors.append(f"文件名说第 {file_vol_no} 卷，vol.vol 写的是 {vol_no}")

    start = _int_or_none(vol.get("start_chapter"))
    end = _int_or_none(vol.get("end_chapter"))
    if start is None or end is None:
        errors.append("缺 start_chapter / end_chapter")
    elif end < start:
        errors.append(f"卷区间反了：{start}-{end}")

    if not str(vol.get("title") or "").strip():
        warnings.append("卷名没写")
    if not str(vol.get("core_conflict") or "").strip():
        warnings.append("core_conflict（核心冲突）没写——分卷目录里最该写清的一项")
    if not str(vol.get("goal") or "").strip():
        warnings.append("goal（卷目标）没写")

    chapters = [c for c in (vol.get("chapters") or []) if isinstance(c, dict)]
    nos = _chapter_nos(vol)

    if start is not None and end is not None and end >= start:
        want = set(range(start, end + 1))
        got = set(nos)
        missing = sorted(want - got)
        extra = sorted(got - want)
        if missing:
            errors.append(
                f"本卷缺 {len(missing)} 章：{missing[:8]}{'…' if len(missing) > 8 else ''}"
                "——章号断档通常意味着有内容被悄悄漏掉"
            )
        if extra:
            errors.append(f"本卷多出区间外的章：{extra[:8]}")

    dup = sorted({n for n in nos if nos.count(n) > 1})
    if dup:
        errors.append(f"章号重复：{dup[:8]}")

    for ch in chapters:
        no = _int_or_none(ch.get("chapter_no"))
        label = f"第{no}章" if no is not None else "（没写章号的那条）"
        if not str(ch.get("title") or "").strip():
            errors.append(f"{label} 缺 title")
        if not str(ch.get("gist") or "").strip():
            errors.append(f"{label} 缺 gist（内容概要）——没有它就写不出创作任务指令")

    # 出场人物要能在设定集里找到。找不到多半是名字漂了，而名字漂了是最难回头改的。
    if setting is not None:
        known = {str(c.get("name") or "").strip()
                 for c in (setting.get("characters") or []) if isinstance(c, dict)}
        known.discard("")
        for ch in chapters:
            no = _int_or_none(ch.get("chapter_no"))
            for name in ch.get("characters") or []:
                name = str(name).strip()
                if name and known and name not in known:
                    warnings.append(f"第{no}章的出场人物「{name}」不在设定集里")

    _validate_parts(vol, start, end, errors, warnings)
    _validate_foreshadow_actions(vol, errors)
    # 每一段都要有「必须完成的功能」，否则写到那一段没人知道任务达成了没有
    from .chapter_brief import part_task_problems

    warnings = warnings + part_task_problems(data)
    return errors, warnings


def _validate_parts(
    vol: dict[str, Any], start: int | None, end: int | None,
    errors: list[str], warnings: list[str],
) -> None:
    parts = [p for p in (vol.get("parts") or []) if isinstance(p, dict)]
    if not parts:
        warnings.append("没划部分（parts）——长卷不划部分会让逐章表一路平铺，看不出节奏")
        return

    spans: list[tuple[int, int, str]] = []
    for part in parts:
        p_start = _int_or_none(part.get("start_chapter"))
        p_end = _int_or_none(part.get("end_chapter"))
        title = str(part.get("title") or "").strip() or "（没写名）"
        if p_start is None or p_end is None:
            errors.append(f"部分「{title}」缺 start_chapter / end_chapter")
            continue
        if p_end < p_start:
            errors.append(f"部分「{title}」区间反了：{p_start}-{p_end}")
            continue
        if start is not None and end is not None:
            if p_start < start or p_end > end:
                errors.append(f"部分「{title}」的区间 {p_start}-{p_end} 超出本卷 {start}-{end}")
        if not str(part.get("gist") or "").strip():
            warnings.append(f"部分「{title}」没写 gist")
        spans.append((p_start, p_end, title))

    if not spans:
        return
    spans.sort()
    for (a_start, a_end, a_title), (b_start, _b_end, b_title) in zip(spans, spans[1:]):
        if b_start != a_end + 1:
            errors.append(
                f"部分之间不接续：「{a_title}」到 {a_end}，「{b_title}」从 {b_start} 开始"
                + ("（重叠）" if b_start <= a_end else "（中间断档）")
            )
    if start is not None and spans[0][0] != start:
        errors.append(f"第一部分从 {spans[0][0]} 开始，但本卷从 {start} 开始")
    if end is not None and spans[-1][1] != end:
        errors.append(f"最后一部分到 {spans[-1][1]} 结束，但本卷到 {end} 结束")


_FORESHADOW_ACTIONS = ("埋设", "推进", "回收")


def _validate_foreshadow_actions(vol: dict[str, Any], errors: list[str]) -> None:
    for ch in vol.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        action = str(ch.get("foreshadow") or "").strip()
        if action and action not in _FORESHADOW_ACTIONS:
            errors.append(
                f"第{ch.get('chapter_no')}章的 foreshadow「{action}」不合法："
                f"{' | '.join(_FORESHADOW_ACTIONS)} | 留空"
            )


def validate_chain(
    volumes: dict[int, dict[str, Any]],
    *,
    setting: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """跨卷校验：卷号连续、章区间首尾相接、全书覆盖到目标章数。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not volumes:
        return ["一卷都还没填"], []

    nos = sorted(volumes)
    expected = list(range(1, nos[-1] + 1))
    if nos != expected:
        errors.append(f"卷号不连续：有 {nos}，缺 {sorted(set(expected) - set(nos))}")

    spans: list[tuple[int, int, int]] = []
    for no in nos:
        vol = volumes[no].get("vol") or {}
        start = _int_or_none(vol.get("start_chapter"))
        end = _int_or_none(vol.get("end_chapter"))
        if start is None or end is None:
            continue  # 单卷校验已经报过了
        spans.append((start, end, no))

    spans.sort()
    for (a_start, a_end, a_no), (b_start, _b_end, b_no) in zip(spans, spans[1:]):
        if b_start != a_end + 1:
            errors.append(
                f"第 {a_no} 卷到 {a_end} 结束，第 {b_no} 卷从 {b_start} 开始"
                + ("（区间重叠）" if b_start <= a_end else "（中间断档）")
            )

    if setting is not None and spans:
        target = (setting.get("target") or {}).get("chapters")
        if isinstance(target, int) and target > 0:
            covered = spans[-1][1] - spans[0][0] + 1
            if covered < target:
                warnings.append(f"目前铺到第 {spans[-1][1]} 章，设定集的目标是 {target} 章，还差 {target - covered} 章")
            elif covered > target:
                warnings.append(f"目前铺到第 {spans[-1][1]} 章，比设定集的目标 {target} 章多 {covered - target} 章")
    return errors, warnings


# ── 渲染 ────────────────────────────────────────────────────


def render_volume_markdown(data: dict[str, Any]) -> str:
    vol = data.get("vol") or {}
    title = vol.get("title") or "（未命名）"
    lines = [
        f"# 第 {vol.get('vol')} 卷：{title}",
        "",
        f"**卷范围**：第{vol.get('start_chapter')}-{vol.get('end_chapter')}章"
        + (f" | **时间跨度**：{vol['era']}" if vol.get("era") else "")
        + (f" | **核心冲突**：{vol['core_conflict']}" if vol.get("core_conflict") else ""),
        "",
    ]
    if vol.get("goal"):
        lines += [f"**卷目标**：{vol['goal']}", ""]

    by_no = {}
    for ch in vol.get("chapters") or []:
        if isinstance(ch, dict) and isinstance(ch.get("chapter_no"), int):
            by_no[ch["chapter_no"]] = ch

    parts = [p for p in (vol.get("parts") or []) if isinstance(p, dict)]
    if not parts:
        lines += ["## 章节", "", "| 章节 | 标题 | 内容概要 |", "|---|---|---|"]
        for no in sorted(by_no):
            ch = by_no[no]
            lines.append(f"| 第{no}章 | **{ch.get('title') or '—'}** | {ch.get('gist') or '—'} |")
        lines.append("")
    else:
        for part in parts:
            span = f"第{part.get('start_chapter')}-{part.get('end_chapter')}章"
            lines += [f"## {part.get('title') or '（未命名）'}（{span}）", ""]
            if part.get("gist"):
                lines += [part["gist"], ""]
            lines += ["| 章节 | 标题 | 内容概要 |", "|---|---|---|"]
            for no in range(_int_or_none(part.get("start_chapter")) or 0,
                            (_int_or_none(part.get("end_chapter")) or -1) + 1):
                ch = by_no.get(no)
                if ch is None:
                    continue
                lines.append(f"| 第{no}章 | **{ch.get('title') or '—'}** | {ch.get('gist') or '—'} |")
            lines.append("")

    if vol.get("closing"):
        lines += ["## 卷末总结", "", vol["closing"], ""]
    return "\n".join(lines).rstrip() + "\n"


def render_volumes_markdown(volumes: dict[int, dict[str, Any]]) -> str:
    return "\n\n".join(render_volume_markdown(volumes[no]) for no in sorted(volumes))
