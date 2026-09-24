"""结构原语：加载、构造提示词片段、校验标注结果。

三条纪律：
  1. 原语全局唯一，所有作品共用同一套字段，否则跨作品聚合会失败
  2. 提示词片段必须**逐字确定**——它是 prompt 缓存的固定前缀，改一个字缓存就失效
  3. 校验失败**绝不静默填默认值**，缺失就是缺失，显式记录

第 2 条尤其重要：这个片段由数据文件派生，顺序与措辞都固定，
所以同一份 primitives.yaml 每次生成的字节完全一致。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PRIMITIVES = "primitives.yaml"
DEFAULT_WORK = "work.yaml"


@dataclass
class FieldSpec:
    id: str
    key: str
    label: str
    source: str
    type: str
    enum: str | None = None
    values: list[Any] = field(default_factory=list)
    hint: str = ""
    item_keys: list[str] = field(default_factory=list)
    item_enums: dict[str, str] = field(default_factory=dict)
    item_constraints: dict[str, str] = field(default_factory=dict)
    max_len: int = 0

    @property
    def is_model(self) -> bool:
        return self.source == "model"


class Primitives:
    """原语定义。加载后即只读使用。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        self.version = str(data.get("version") or "0")
        self.frozen = bool(data.get("frozen"))
        self.enums: dict[str, list[Any]] = {
            str(k): list(v or []) for k, v in (data.get("enums") or {}).items()
        }
        self.fields: list[FieldSpec] = []
        for item in data.get("fields") or []:
            enum_ref = item.get("enum")
            self.fields.append(
                FieldSpec(
                    id=str(item.get("id")),
                    key=str(item.get("key")),
                    label=str(item.get("label")),
                    source=str(item.get("source")),
                    type=str(item.get("type")),
                    enum=str(enum_ref) if enum_ref else None,
                    values=list(self.enums.get(str(enum_ref), [])) if enum_ref else [],
                    hint=str(item.get("hint") or ""),
                    item_keys=list(item.get("item_keys") or []),
                    item_enums={str(k): str(v) for k, v in (item.get("item_enums") or {}).items()},
                    item_constraints={
                        str(k): str(v) for k, v in (item.get("item_constraints") or {}).items()
                    },
                    max_len=int(item.get("max_len") or 0),
                )
            )
        self.meta_fields: list[dict[str, Any]] = list(data.get("meta_fields") or [])
        self.review_policy: dict[str, str] = {
            str(k): str(v) for k, v in (data.get("review_policy") or {}).items()
        }

    # ── 查询 ────────────────────────────────────────────

    @property
    def model_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.is_model]

    @property
    def script_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if not f.is_model]

    def by_key(self, key: str) -> FieldSpec | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def hash(self) -> str:
        """原语指纹。写进标注产物的来源信息里，避免「不知道这份标是用哪版原语做的」。"""
        payload = yaml.safe_dump(self.raw, allow_unicode=True, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # ── 提示词片段（固定前缀的一部分） ──────────────────

    def build_model_schema_block(self) -> str:
        """生成给模型的字段说明与 JSON 骨架。逐字确定，用作缓存前缀。"""
        lines: list[str] = []
        lines.append("请对本章输出以下字段，严格按 JSON 返回，不要任何前后说明：")
        lines.append("{")

        for f in self.model_fields:
            if f.type == "enum":
                allowed = "|".join(str(v) for v in f.values)
                lines.append(f'  "{f.key}": "{allowed}",   // {f.label}')
            elif f.type == "rating":
                lines.append(f'  "{f.key}": 1-5,   // {f.label}。{f.hint or ""}'.rstrip())
            elif f.type == "int":
                lines.append(f'  "{f.key}": int,   // {f.label}。{f.hint or ""}'.rstrip())
            elif f.type == "str":
                limit = f"（不超过 {f.max_len} 字）" if f.max_len else ""
                lines.append(
                    f'  "{f.key}": "",   // {f.label}。{f.hint or ""}{limit}'.rstrip()
                )
            elif f.type == "list_of_objects":
                item_parts = []
                for key in f.item_keys:
                    enum_ref = f.item_enums.get(key)
                    if enum_ref:
                        item_parts.append(f'"{key}": "{"|".join(str(v) for v in self.enums.get(enum_ref, []))}"')
                    elif key == "锚点":
                        item_parts.append('"锚点": "段落位置简述"')
                    else:
                        item_parts.append(f'"{key}": ""')
                hint = f"。{f.hint}" if f.hint else ""
                lines.append(f'  "{f.key}": [{{{", ".join(item_parts)}}}],   // {f.label}{hint}')
            else:
                lines.append(f'  "{f.key}": [],   // {f.label}')

        for m in self.meta_fields:
            if m.get("type") == "enum":
                lines.append(f'  "{m.get("key")}": "{"|".join(str(v) for v in m.get("values") or [])}",   // {m.get("label")}')
            else:
                lines.append(f'  "{m.get("key")}": [],   // {m.get("label")}')

        lines.append("}")

        # 枚举取值集中列一遍，减少模型猜错格式的概率
        lines.append("")
        lines.append("枚举取值说明：")
        for f in self.model_fields:
            if f.type == "enum":
                lines.append(f"- {f.label}（{f.key}）：{'、'.join(str(v) for v in f.values)}")
            elif f.type == "list_of_objects":
                for key, enum_ref in f.item_enums.items():
                    values = self.enums.get(enum_ref, [])
                    lines.append(f"- {f.label}的「{key}」：{'、'.join(str(v) for v in values)}")

        return "\n".join(lines)

    # ── 校验 ────────────────────────────────────────────

    def validate(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """校验模型返回。返回 (通过校验的字段, 问题清单)。

        缺失或非法的字段**不填默认值**，值保持 None 并记入问题清单。
        静默填默认值是最危险的做法——它会让数据集里混进大量假的空值，而且永远不知道。
        """
        if not isinstance(payload, dict):
            return {}, ["模型返回的不是 JSON 对象"]

        cleaned: dict[str, Any] = {}
        issues: list[str] = []

        for f in self.model_fields:
            if f.key not in payload:
                issues.append(f"缺少字段 {f.key}（{f.label}）")
                cleaned[f.key] = None
                continue

            value = payload[f.key]
            ok, normalized, reason = self._validate_one(f, value)
            if ok:
                cleaned[f.key] = normalized
            else:
                cleaned[f.key] = None
            # ok=True 也可能带 reason（数组里只丢掉坏的那几项，好的留下）。
            # 这类「部分成功」必须记下来，不能静默。
            if reason:
                issues.append(f"字段 {f.key}（{f.label}）：{reason}")

        for m in self.meta_fields:
            key = str(m.get("key"))
            value = payload.get(key)
            if m.get("type") == "enum":
                allowed = [str(v) for v in (m.get("values") or [])]
                cleaned[key] = value if str(value) in allowed else None
                if cleaned[key] is None and value is not None:
                    issues.append(f"字段 {key} 取值不在允许范围：{value}")
            else:
                cleaned[key] = value if isinstance(value, list) else []

        return cleaned, issues

    def _validate_one(self, f: FieldSpec, value: Any) -> tuple[bool, Any, str]:
        if value is None:
            return True, None, ""

        if f.type == "enum":
            text = str(value)
            if text in [str(v) for v in f.values]:
                return True, text, ""
            return False, None, f"应为 {'|'.join(str(v) for v in f.values)}，实际 {text!r}"

        if f.type == "rating":
            try:
                num = int(value)
            except (TypeError, ValueError):
                return False, None, f"应为 1 到 5 的整数，实际 {value!r}"
            if not 1 <= num <= 5:
                return False, None, f"超出 1 到 5 范围：{num}"
            return True, num, ""

        if f.type == "int":
            try:
                num = int(value)
            except (TypeError, ValueError):
                return False, None, f"应为整数，实际 {value!r}"
            if num < 0:
                return False, None, f"不应为负数：{num}"
            return True, num, ""

        if f.type == "str":
            if not isinstance(value, str):
                return False, None, f"应为字符串，实际 {type(value).__name__}"
            text = value.strip()
            if not text:
                # 空串按「没填」处理，保持为 null——不然「空字符串」和「没产出」
                # 在数据里长得一样，又是一次静默失效。
                return True, None, ""
            if f.max_len and len(text) > f.max_len:
                # **不硬截断。** 实测吃了亏：48 字上限把
                # 「…却得知张老师生病异能课停开」砍成「…异能课停」，
                # 半句话直接进了大纲原料。宁可长一点，不能把句子砍断。
                #
                # 所以 max_len 只是给模型的建议长度；只有离谱地长
                # （2.5 倍以上，说明模型完全没守约束）才退到最近的句末标点收口。
                hard = max(f.max_len * 2.5, f.max_len + 40)
                if len(text) > hard:
                    cut = max(text.rfind(mark, 0, int(hard)) for mark in "。！？…")
                    text = text[: cut + 1] if cut >= 20 else text[: int(hard)]
            return True, text, ""

        if f.type == "list_of_objects":
            if not isinstance(value, list):
                return False, None, "应为数组"
            items: list[dict[str, Any]] = []
            dropped: list[str] = []
            for idx, raw_item in enumerate(value):
                if not isinstance(raw_item, dict):
                    dropped.append(f"第 {idx + 1} 项不是对象")
                    continue
                item: dict[str, Any] = {}
                bad_reason = ""
                for key in f.item_keys:
                    item[key] = raw_item.get(key)
                    enum_ref = f.item_enums.get(key)
                    if enum_ref and item[key] is not None:
                        allowed = [str(v) for v in self.enums.get(enum_ref, [])]
                        if str(item[key]) not in allowed:
                            bad_reason = f"第 {idx + 1} 项的「{key}」取值非法：{item[key]!r}"
                            break
                    if key == "强度" and item[key] is not None:
                        try:
                            strength = int(item[key])
                        except (TypeError, ValueError):
                            bad_reason = f"第 {idx + 1} 项的强度不是整数"
                            break
                        if not 1 <= strength <= 5:
                            bad_reason = f"第 {idx + 1} 项的强度超出 1 到 5"
                            break
                        item[key] = strength
                    if key == "描述" and isinstance(item[key], str) and len(item[key]) > 20:
                        item[key] = item[key][:20]
                if bad_reason:
                    # 只丢坏的那一项，不牵连整个数组。
                    # 初版是一个坏条目就让整列作废——对伏笔来说等于丢掉整章的线索。
                    dropped.append(bad_reason)
                    continue
                items.append(item)
            reason = "；".join(dropped) if dropped else ""
            return True, items, reason

        return True, value, ""


def load_primitives(path: str | Path = DEFAULT_PRIMITIVES) -> Primitives:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到原语定义：{p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"原语定义格式异常：{p}")
    return Primitives(data)


# ── 作品配置 ──────────────────────────────────────────────


@dataclass
class WorkConfig:
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.raw.get("work") or "")

    @property
    def protagonist(self) -> str:
        return str(self.raw.get("protagonist") or "")

    @property
    def core_motive(self) -> str:
        return str(self.raw.get("core_motive") or "")

    @property
    def style_checks(self) -> list[dict[str, Any]]:
        return list(self.raw.get("style_checks") or [])

    def motive_line(self) -> str:
        if self.protagonist and self.core_motive:
            return f"{self.protagonist} · {self.core_motive}"
        return self.protagonist or "（未配置）"

    def hash(self) -> str:
        payload = yaml.safe_dump(self.raw, allow_unicode=True, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_work(path: str | Path = DEFAULT_WORK) -> WorkConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到作品配置：{p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"作品配置格式异常：{p}")
    return WorkConfig(data)


_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """把换行统一，便于按段落切分。不改动任何标点。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")
