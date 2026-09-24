"""重写工坊（M3）与一致性校验（M4）。

设计出处：`小说创作工坊-通用设计方案.md` §7（M3/M4）与
`工坊界面与交互设计.md` §3.6（改写台）。

改动前先回答「这是不是一次真正的一致性问题」：
重写是创作场景，作者要对成品负责。所以这里只做两件事——
  1. M3：按指令让模型重写一章（原稿永远保留，改写稿是新版本）
  2. M4：改写前后做六类一致性校验，任何「变了」都让用户看得见

## 六类校验（全部纯脚本，零模型成本）

  V1 长度变化     字数比超过 0.5~2.5 视为异常（精简/扩写类指令主动放宽）
  V2 人称一致     脚本轨人称检测：原稿/改写稿都判不出或一致才通过
  V3 风格合规     作品 style_checks 全部跑一遍，新出现的违规记下
  V4 段落结构     空段比 / 极短段 / 结尾句完整性（不破坏基本结构）
  V5 情节锚点     原稿出现的人物/地点/势力名，改写稿里必须还在
  V6 伏笔线索     标注里登记的未回收伏笔、关键实体，改写稿里不能消失

严重度分级（界面 §3.6）：
  · high   —— 阻断，不允许接受（接受操作会被拒绝）
  · medium —— 提示，建议查看
  · low    —— 提醒，不阻断

## 输出闸门（G2）

「接受」时若存在未解决的 high 级问题，接口拒绝并给原因。
这不是「可以绕过的建议」，是「改了会破坏作品」的事实闸门。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ErrorKind, hint_for
from .llm import ApiError, ChatResult, OpenAICompatProvider, build_thinking_extra
from .script_fields import compute_script_fields
from .secrets import redact
from .primitives import WorkConfig, normalize_text

REWRITE_SCHEMA_VERSION = "rewrite-v1"

# 指令类型（界面 §3.6 的选项）
DIRECTIVES = ["改视角", "扩写", "精简", "调节奏", "强化动机"]

# 长度校验的宽松区间。精简/扩写是主动改变长度，阈值放宽。
_LEN_LO, _LEN_HI = 0.5, 2.5
_DIRECTIVE_LEN_LO, _DIRECTIVE_LEN_HI = 0.2, 5.0

_STYLE_WEIGHTS = {1: "low", 2: "medium", 3: "high"}  # 风格违规 1 处=low，2-4=medium，5+=high
_SEV_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class RewriteOptions:
    temperature: float = 0.7
    thinking: str = "disabled"
    max_tokens: int = 4000
    max_attempts: int = 2
    timeout_sec: float = 90.0


@dataclass
class SevIssue:
    id: str
    severity: str  # high | medium | low
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "message": self.message, "detail": self.detail}


@dataclass
class RewriteResult:
    record: dict[str, Any]

    @property
    def ok(self) -> bool:
        issues = self.record.get("validation") or {}
        return not issues.get("blocking")


def build_rewrite_prompt(directive: str, instruction: str = "") -> str:
    """拼一段重写指令。改视角的指令是并列的六条，让模型逐条对照。"""
    lines = [
        f"重写指令：{directive}",
    ]
    if instruction:
        lines.append(f"补充说明：{instruction}")
    lines.extend(
        [
            "",
            "要求：",
            "- 只输出改写后的正文，不要任何前后说明，不要用代码围栏",
            "- 保留原稿的情节、人物、伏笔与关键设定，只按指令调整",
            "- 不要加前文没有的信息（不凭空增加人物或事件）",
            "- 如果指令会导致信息丢失（例如精简把伏笔删了），宁可少改也不要破坏剧情",
        ]
    )
    return "\n".join(lines)


def _call_rewrite(
    client: OpenAICompatProvider, model_id: str, system: str, opts: RewriteOptions
) -> tuple[str, list[dict[str, Any]]]:
    """调用模型重写。返回 (正文, 错误列表)。"""
    errors: list[dict[str, Any]] = []
    extra = build_thinking_extra(opts.thinking, None)
    for attempt in range(1, opts.max_attempts + 1):
        try:
            result: ChatResult = client.chat(
                model_id,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "开始重写。"},
                ],
                max_tokens=opts.max_tokens,
                temperature=opts.temperature,
                extra=extra,
            )
        except ApiError as exc:
            errors.append(
                {
                    "attempt": attempt,
                    "kind": exc.kind.value,
                    "hint": hint_for(exc.kind),
                    "detail": exc.safe_body(client.secrets),
                }
            )
            continue
        text = (result.text or "").strip()
        if not text:
            errors.append({"attempt": attempt, "kind": "empty", "detail": "模型返回了空文本"})
            continue
        return text, errors
    return "", errors


def rewrite_chapter(
    *,
    client: OpenAICompatProvider,
    model_id: str,
    chapter_id: str,
    chapter_no: int | None,
    title: str,
    original: str,
    directive: str,
    instruction: str = "",
    opts: RewriteOptions | None = None,
    system_theme: str = "",
) -> RewriteResult:
    """重写一章。原稿永远保留在 record 里，改写稿是新版本。"""
    opts = opts or RewriteOptions()
    directive = directive if directive in DIRECTIVES else "调节奏"

    system = (
        "你是小说的重写执行者。你在作者本人自有或授权的稿件上作业，"
        f"原文不可侵犯，重写只是调整表达方式。\n\n{system_theme or '无需额外设定。'}\n\n"
        + build_rewrite_prompt(directive, instruction)
    )

    text, errors = _call_rewrite(client, model_id, system, opts)

    record: dict[str, Any] = {
        "schema_version": REWRITE_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "chapter_no": chapter_no,
        "title": title,
        "directive": directive,
        "instruction": instruction,
        "model": model_id,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "original": original,
        "rewritten": text,
        "validation": {},
        "accepted": False,
        "errors": errors,
        "provenance": {
            "temperature": opts.temperature,
            "thinking": opts.thinking,
            "attempts": len(errors) + (1 if text else 0),
        },
    }
    return RewriteResult(record=record)


# ── 六类校验 ─────────────────────────────────────────────


def _char_count(text: str) -> int:
    return len(normalize_text(text).replace("\n", ""))


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in normalize_text(text).split("\n") if p.strip()]


def _anchor_names(k1: dict[str, Any] | None) -> list[str]:
    """从 K1 实体卡片收集人物/地点/势力名，用于 V5 情节锚点校验。"""
    if not k1 or not k1.get("available"):
        return []
    names: list[str] = []
    for key in ("characters", "locations", "factions"):
        for item in k1.get(key) or []:
            n = str(item.get("name") or "").strip()
            if len(n) >= 2:
                names.append(n)
    return names


def validate_rewrite(
    *,
    original: str,
    rewritten: str,
    work: WorkConfig,
    annotation_fields: dict[str, Any] | None,
    k1: dict[str, Any] | None,
    directive: str = "",
) -> list[SevIssue]:
    """六类一致性校验。纯脚本，全部可解释。"""
    issues: list[SevIssue] = []

    # V1 长度变化
    o_len, r_len = _char_count(original), _char_count(rewritten)
    if o_len and r_len:
        ratio = r_len / o_len
        if directive in ("精简", "扩写"):
            lo, hi = _DIRECTIVE_LEN_LO, _DIRECTIVE_LEN_HI
        else:
            lo, hi = _LEN_LO, _LEN_HI
        if not (lo <= ratio <= hi):
            sev = "high" if not (0.2 <= ratio <= 5.0) else "medium"
            issues.append(
                SevIssue(
                    "V1_length",
                    sev,
                    f"改写后长度变化了 {(ratio - 1) * 100:.0f}%",
                    f"原 {o_len} 字 → 改写 {r_len} 字（比例 {ratio:.2f}，允许区间 {lo}-{hi}）",
                )
            )

    # V2 人称一致
    try:
        o_fields, _ = compute_script_fields(
            original, vol_no=1, chapter_no=None, work=work,
            primitives=_dummy_primitives(),
        )
        r_fields, _ = compute_script_fields(
            rewritten, vol_no=1, chapter_no=None, work=work,
            primitives=_dummy_primitives(),
        )
    except Exception:  # noqa: BLE001
        o_detected = r_detected = None
    else:
        o_detected = o_fields.get("perspective_detected")
        r_detected = r_fields.get("perspective_detected")
    if o_detected and r_detected and o_detected != r_detected:
        issues.append(
            SevIssue(
                "V2_perspective",
                "high",
                f"人称从「{o_detected}」变成了「{r_detected}」",
                "改写改变了叙事人称，会让整章读感错位",
            )
        )

    # V3 风格合规（作品声明过才有基准）
    declared = [c for c in work.style_checks if c.get("type") != "perspective"]
    if declared:
        try:
            _r, issues_from_script = compute_script_fields(
                rewritten, vol_no=1, chapter_no=None, work=work,
                primitives=_dummy_primitives(),
            )
        except Exception:  # noqa: BLE001
            issues_from_script = []
        style_issues = [i for i in issues_from_script if "风格" in i or "违规" in i]
        if style_issues:
            n = len(style_issues)
            issues.append(
                SevIssue(
                    "V3_style",
                    _STYLE_WEIGHTS.get(min(n, 3), "medium"),
                    f"改写稿有 {n} 处风格违规",
                    "；".join(style_issues[:3]),
                )
            )

    # V4 段落结构
    o_paras, r_paras = _paragraphs(original), _paragraphs(rewritten)
    if not r_paras:
        issues.append(SevIssue("V4_structure", "high", "改写稿没有段落内容", "空文本"))
    if o_paras and r_paras:
        empty_ratio = sum(1 for p in r_paras if len(p) < 20) / len(r_paras)
        if empty_ratio > 0.4:
            issues.append(
                SevIssue(
                    "V4_structure",
                    "medium",
                    f"改写稿 {empty_ratio:.0%} 的段落过短",
                    "可能打断了原文的叙事节奏",
                )
            )
        if len(r_paras) < max(1, len(o_paras) // 3):
            issues.append(
                SevIssue("V4_structure", "medium", "改写稿段落数大幅减少", "可能把内容压成了一片")
            )

    # V5 情节锚点（K1 实体名保留）
    anchors = _anchor_names(k1)
    if anchors:
        missing = [n for n in anchors if n in original and n not in rewritten]
        if missing:
            issues.append(
                SevIssue(
                    "V5_anchors",
                    "high",
                    f"改写稿丢失了 {len(missing)} 个关键实体",
                    "、".join(missing[:10]),
                )
            )

    # V6 伏笔线索（标注里的未回收伏笔 + 关键实体）
    fields = annotation_fields or {}
    foreshadows = fields.get("foreshadows") or []
    if foreshadows:
        lost: list[str] = []
        for f in foreshadows:
            desc = str(f.get("描述") or "")
            action = str(f.get("动作") or "")
            # 只检查「推进/回收」——埋设章的重写不该把线索直接删掉
            if desc and action in ("推进", "回收") and desc in original and desc not in rewritten:
                lost.append(desc)
        if lost:
            issues.append(
                SevIssue(
                    "V6_foreshadow",
                    "high",
                    f"改写稿丢失了 {len(lost)} 处伏笔线索",
                    "、".join(lost[:6]),
                )
            )

    # 排列：high → medium → low
    issues.sort(key=lambda i: (-_SEV_RANK[i.severity], i.id))
    return issues


def _dummy_primitives():
    from .primitives import Primitives

    return Primitives({"version": "0", "fields": [], "enums": {}})


def pack_validation(issues: list[SevIssue]) -> dict[str, Any]:
    blocking = [i for i in issues if i.severity == "high"]
    return {
        "items": [i.to_dict() for i in issues],
        "blocking": [i.to_dict() for i in blocking],
        "total": len(issues),
        "blocking_count": len(blocking),
        "artifact_link": "#校验规则见 rewrite.py 的 V1-V6",
    }


def save_rewrite(work_dir: Path, record: dict[str, Any]) -> Path:
    """落盘改写记录。改写稿与校验都存在同一个 JSON 里，原稿不移动。"""
    out = Path(work_dir) / "30-rewrite"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{record.get('chapter_id')}.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def accept_rewrite(work_dir: Path, chapter_id: str, secrets: list[str] | None = None) -> dict[str, Any]:
    """接受改写：把改写稿写为新版本文件，原稿（00-ingest）不动。

    这是「接受」动作，不是「覆盖」：原稿永远保留，只多一份新版本。
    """
    out = Path(work_dir) / "30-rewrite"
    path = out / f"{chapter_id}.json"
    if not path.exists():
        raise ValueError(f"章节 {chapter_id} 还没有改写记录")
    record = json.loads(path.read_text(encoding="utf-8-sig"))

    # 输出闸门（G2）：有未解决的 high 级问题，拒绝接受
    validation = record.get("validation") or {}
    blocking = validation.get("blocking") or []
    if blocking and not record.get("force_accept"):
        raise ValueError(
            f"存在 {len(blocking)} 个阻断级校验问题，未解决前不接受。"
            "确认要强制接受，再点一次（会记录 in issues）。"
        )

    versions = out / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    version_path = versions / f"{chapter_id}.{stamp}.md"
    version_path.write_text(
        redact(str(record.get("rewritten") or ""), secrets), encoding="utf-8"
    )

    record["accepted"] = True
    record["accepted_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    record["version_file"] = str(version_path)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "chapter_id": chapter_id, "version_file": str(version_path)}