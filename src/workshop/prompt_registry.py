"""提示词注册表：内置 / 全局 / 项目三级覆盖 + `{{var}}` 变量校验。

## 为什么要把提示词搬到文件里

改一句提示词要改 Python、跑全量测试、再回滚——实验成本高到没人愿意做实验。
放到文件后，改文本就是改文件；项目级覆盖甚至不用动代码仓库。

## 三级覆盖

查找顺序（先命中先用）：

1. **项目级**：`<作品>/prompts/<id>.md`
2. **全局**：`<数据根>/prompts/<id>.md`（源码方式是仓库根目录的 `prompts/`）
3. **内置**：`src/workshop/prompts/<id>.md`，随仓库发布

`catalog()` 会带上每一级的来源，设置页据此显示「这条现在是内置还是被你覆盖了」。

## 变量为什么要在渲染时校验

模板里的 `{{var}}` 是**必填**的：漏传时模型看到的是字面量 `{{brief}}`，
它不会报错，只会照着字面量瞎写。所以渲染时缺变量直接抛 `PromptVariableError`，
在测试里就是红，而不是在生成结果里变成一段玄学文本。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
VAR_RE = re.compile(r"{{\s*([A-Za-z0-9_]+)\s*}}")


@dataclass(frozen=True)
class PromptSpec:
    """一条提示词的元数据。`variables` 是文档化的必填项，渲染时以模板实际占位符为准。"""

    id: str
    label: str
    stage: str
    variables: tuple[str, ...]
    used_by: str


SPECS: dict[str, PromptSpec] = {
    "draft.system": PromptSpec(
        id="draft.system",
        label="正文生成 · 系统提示",
        stage="draft",
        variables=(),
        used_by="draft.build_draft_messages",
    ),
    "beats.system": PromptSpec(
        id="beats.system",
        label="写前细纲 · 系统提示",
        stage="plan",
        variables=("beats_min", "beats_max"),
        used_by="beats.build_beat_messages",
    ),
    "review.system": PromptSpec(
        id="review.system",
        label="正文审稿 · 系统提示",
        stage="review",
        variables=("min_evidence",),
        used_by="review.build_review_messages",
    ),
    "notes.extract.system": PromptSpec(
        id="notes.extract.system",
        label="要点抽取 · 系统提示",
        stage="extract",
        variables=(),
        used_by="notes.build_extract_messages",
    ),
}


class PromptVariableError(ValueError):
    """模板里有 `{{var}}` 但调用方没给。消息里点名缺了哪几个。"""

    def __init__(self, prompt_id: str, missing: list[str]) -> None:
        self.prompt_id = prompt_id
        self.missing = missing
        super().__init__(
            f"提示词「{prompt_id}」缺少必填变量：{', '.join(missing)}。"
            "模板里的 {{var}} 是必填的，漏传时模型会照着字面量写。"
        )


class UnknownPromptError(ValueError):
    """prompt id 不在 SPECS 里——多半是拼错了。"""


def _data_root() -> Path:
    return Path(os.environ.get("WORKSHOP_ROOT") or Path(__file__).resolve().parent.parent.parent)


def _candidate(prompt_id: str, work_dir: str | Path | None, root: str | Path | None) -> list[tuple[str, Path]]:
    """按优先级列出候选文件。项目级与全局级路径与内置目录重合时跳过。"""
    builtin = PROMPT_DIR / f"{prompt_id}.md"
    data_root = Path(root) if root else _data_root()
    out: list[tuple[str, Path]] = []
    if work_dir:
        project = Path(work_dir) / "prompts" / f"{prompt_id}.md"
        if project.resolve() != builtin.resolve():
            out.append(("project", project))
    global_path = data_root / "prompts" / f"{prompt_id}.md"
    if global_path.resolve() != builtin.resolve():
        out.append(("global", global_path))
    out.append(("builtin", builtin))
    return out


def resolve(
    prompt_id: str,
    *,
    work_dir: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """读到这一条提示词，并说明它来自哪一级、哪个文件。"""
    spec = SPECS.get(prompt_id)
    if spec is None:
        raise UnknownPromptError(f"未知提示词「{prompt_id}」，可选：{', '.join(sorted(SPECS))}")
    for source, path in _candidate(prompt_id, work_dir, root):
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise ValueError(f"提示词文件读不了：{path}（{exc}）") from exc
            return {
                "id": prompt_id,
                "label": spec.label,
                "stage": spec.stage,
                "variables": list(spec.variables),
                "text": text,
                "source": source,
                "path": str(path),
            }
    raise FileNotFoundError(
        f"找不到提示词「{prompt_id}」的内置文件：{PROMPT_DIR / (prompt_id + '.md')}。"
        "内置文件缺失说明安装包不完整。"
    )


def render(
    prompt_id: str,
    *,
    variables: dict[str, Any] | None = None,
    work_dir: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """取模板 + 替换 `{{var}}`。缺变量直接抛错，不把字面量交给模型。"""
    resolved = resolve(prompt_id, work_dir=work_dir, root=root)
    values = variables or {}
    placeholders: list[str] = []
    for name in VAR_RE.findall(resolved["text"]):
        if name not in placeholders:
            placeholders.append(name)
    missing = [name for name in placeholders if name not in values]
    if missing:
        raise PromptVariableError(prompt_id, missing)
    text = VAR_RE.sub(lambda m: str(values[m.group(1)]), resolved["text"])
    return {**resolved, "text": text, "placeholders": placeholders}


def catalog(
    *,
    work_dir: str | Path | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """所有提示词的来源一览：哪一级生效、模板里有几个占位符、文件在哪。"""
    out: list[dict[str, Any]] = []
    for prompt_id in sorted(SPECS):
        spec = SPECS[prompt_id]
        row = resolve(prompt_id, work_dir=work_dir, root=root)
        placeholders: list[str] = []
        for name in VAR_RE.findall(row["text"]):
            if name not in placeholders:
                placeholders.append(name)
        out.append(
            {
                "id": prompt_id,
                "label": spec.label,
                "stage": spec.stage,
                "variables": [x for x in spec.variables],
                "placeholders": placeholders,
                "source": row["source"],
                "path": row["path"],
                "overridden": row["source"] != "builtin",
                "used_by": spec.used_by,
            }
        )
    return out
