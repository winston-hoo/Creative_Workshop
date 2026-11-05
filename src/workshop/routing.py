"""阶段模型路由（E2）：每个阶段用哪个模型、为什么、现在能不能用。

## 为什么不直接复用 `chapter_annotation` 绑定

一条全局绑定等于「细纲、正文、审稿、抽取都用同一个模型」。这四个阶段的
要求完全不同：细纲要结构判断，正文要长输出，审稿要稳定 JSON，抽取要便宜。
绑在一起的结果通常是：给正文配了最强的模型，抽取也跟着烧钱；
或者为了省钱全用便宜模型，审稿的 JSON 天天解析失败。

## 回退链

1. `task_bindings.stages.<stage>`（设置页显式配置）
2. `task_bindings.chapter_annotation`（默认绑定，向后兼容老配置）
3. 都没有 → 抛错，而不是随便挑一个服务商。「缺省不落到未选 provider」。

## 为什么返回 `risk`

「为什么这么配」必须能回答，否则设置页只是四个下拉框。这里按模型声明的
能力给风险提示：审稿模型没声明结构化输出、正文模型输出上限过小、
上下文装不下缩略后的上下文预算——都直接标出来，而不是等跑挂了才发现。
"""

from __future__ import annotations

from typing import Any

STAGE_IDS = ("plan", "draft", "review", "extract")

STAGE_META: dict[str, dict[str, Any]] = {
    "plan": {
        "label": "写前细纲",
        "recommendation": "拆节拍靠结构判断：上下文 ≥32k、输出 ≥4k；能开推理更好。",
    },
    "draft": {
        "label": "正文生成",
        "recommendation": "长输出为主：输出上限 ≥8k；单价对总成本影响最大。",
    },
    "review": {
        "label": "正文审稿",
        "recommendation": "必须稳定输出 JSON：优先支持结构化输出的模型，上下文 ≥64k。",
    },
    "extract": {
        "label": "要点/摘要抽取",
        "recommendation": "短输出、调用频繁：便宜且快的模型即可，结构化输出能省重试。",
    },
}

SOURCE_LABELS = {
    "request": "本次请求指定",
    "stage": "阶段自定义",
    "default": "默认任务绑定",
    "unset": "未配置",
}


def stage_binding(cfg, stage: str) -> dict[str, Any]:
    if stage not in STAGE_META:
        raise ValueError(f"未知阶段「{stage}」，可选：{', '.join(STAGE_IDS)}")
    stages = ((cfg.task_bindings or {}).get("stages") or {})
    row = stages.get(stage)
    return row if isinstance(row, dict) else {}


def default_binding(cfg) -> dict[str, Any]:
    row = (cfg.task_bindings or {}).get("chapter_annotation") or {}
    return row if isinstance(row, dict) else {}


def model_of(provider, model_id: str) -> dict[str, Any]:
    for item in provider.models or []:
        if str(item.get("id")) == str(model_id):
            return item
    return {}


def resolve(
    cfg,
    stage: str,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """这一阶段实际用谁。返回 provider 对象、模型名和来源说明。

    显式请求 > 阶段配置 > 默认绑定 > 报错。不猜。
    """
    from workshop.batch import pick_model_id
    from workshop.config import resolve_default_model

    if stage not in STAGE_META:
        raise ValueError(f"未知阶段「{stage}」，可选：{', '.join(STAGE_IDS)}")

    if requested_provider:
        provider_id = requested_provider
        source = "request"
        provider = cfg.get_provider(provider_id)
        model_id = requested_model or (pick_model_id(cfg, provider, None) if provider else "")
    else:
        bound = stage_binding(cfg, stage)
        if bound.get("provider"):
            provider_id, source = str(bound["provider"]), "stage"
            if requested_model:
                model_id = requested_model
            elif bound.get("model"):
                model_id = str(bound["model"])
            else:
                model_id = resolve_default_model(cfg, _need(cfg, provider_id, stage))
        else:
            default = default_binding(cfg)
            if not default.get("provider"):
                raise ValueError(
                    f"「{STAGE_META[stage]['label']}」还没有可用模型：设置页给它配一个，"
                    "或至少配一条默认任务绑定（chapter_annotation）。"
                )
            provider_id, source = str(default["provider"]), "default"
            model_id = requested_model or str(default.get("model") or "")

    provider = cfg.get_provider(str(provider_id))
    if provider is None:
        raise ValueError(f"「{STAGE_META[stage]['label']}」绑定的服务商 {provider_id} 在配置里不存在")
    if not provider.enabled:
        raise ValueError(
            f"「{STAGE_META[stage]['label']}」绑定的服务商「{provider.name}」已停用，"
            "请先启用或换一个阶段配置。"
        )
    if not model_id:
        model_id = pick_model_id(cfg, provider, None)
    if provider.models and model_id not in provider.model_ids:
        raise ValueError(
            f"「{STAGE_META[stage]['label']}」要用的模型「{model_id}」不在服务商"
            f"「{provider.name}」的模型清单里；可用：{', '.join(provider.model_ids) or '（空）'}"
        )
    return {
        "stage": stage,
        "provider": provider,
        "provider_id": provider.id,
        "model": model_id,
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
    }


def _need(cfg, provider_id: str, stage: str):
    provider = cfg.get_provider(str(provider_id))
    if provider is None:
        raise ValueError(f"「{STAGE_META[stage]['label']}」绑定的服务商 {provider_id} 在配置里不存在")
    return provider


def risks(provider, model_id: str, stage: str) -> list[str]:
    """按模型声明的能力给这一阶段的风险提示。没有声明的不一定不可用，但要说清。"""
    info = model_of(provider, model_id)
    caps = (info.get("capabilities") or {}) if isinstance(info.get("capabilities"), dict) else {}
    out: list[str] = []
    if stage == "review" and not caps.get("structured_output") and not caps.get("json_mode"):
        out.append("该模型未声明结构化输出，审稿 JSON 可能解析失败、需要重试。")
    if stage == "draft":
        max_out = int(info.get("max_output") or 0)
        if max_out and max_out < 8000:
            out.append(f"输出上限 {max_out} tokens，写长章可能被截断。")
    if stage == "plan":
        ctx = int(info.get("max_context") or 0)
        if ctx and ctx < 32000:
            out.append(f"上下文 {ctx} tokens，放不下完整的细纲输入。")
    if stage == "extract":
        ctx = int(info.get("max_context") or 0)
        if ctx and ctx < 16000:
            out.append(f"上下文 {ctx} tokens，长章节抽取会截断。")
    return out


def matrix(cfg, *, key_configured=None) -> list[dict[str, Any]]:
    """设置页的主表：每个阶段的生效模型、来源、可用性、能力与风险。"""
    rows: list[dict[str, Any]] = []
    for stage in STAGE_IDS:
        meta = STAGE_META[stage]
        row: dict[str, Any] = {
            "stage": stage,
            "label": meta["label"],
            "recommendation": meta["recommendation"],
            "provider": None,
            "provider_name": None,
            "model": None,
            "model_alias": None,
            "source": "unset",
            "source_label": SOURCE_LABELS["unset"],
            "available": False,
            "unavailable_reason": "",
            "capabilities": {},
            "max_context": None,
            "max_output": None,
            "pricing": {},
            "risks": [],
            "configured": bool(stage_binding(cfg, stage).get("provider")),
        }
        try:
            effective = resolve(cfg, stage)
        except ValueError as exc:
            row["unavailable_reason"] = str(exc)
            rows.append(row)
            continue
        provider = effective["provider"]
        model_id = effective["model"]
        info = model_of(provider, model_id)
        has_key = True if key_configured is None else bool(key_configured(provider.api_key_ref))
        available = bool(provider.enabled and has_key)
        pricing = info.get("pricing") or {}
        row.update(
            {
                "provider": provider.id,
                "provider_name": provider.name,
                "model": model_id,
                "model_alias": info.get("alias") or model_id,
                "source": effective["source"],
                "source_label": effective["source_label"],
                "available": available,
                "unavailable_reason": "" if available else (
                    "服务商已停用" if not provider.enabled else "该服务商还没有配置密钥"
                ),
                "capabilities": info.get("capabilities") or {},
                "max_context": info.get("max_context"),
                "max_output": info.get("max_output"),
                "pricing": {
                    "input_peak": pricing.get("input_per_mtok_peak"),
                    "output_peak": pricing.get("output_per_mtok_peak"),
                },
                "risks": risks(provider, model_id, stage),
            }
        )
        rows.append(row)
    return rows


def validate(cfg, stage: str, provider_id: str, model_id: str) -> None:
    """保存前的严格校验。错误消息要能直接显示给人看，不写"invalid input"。"""
    if stage not in STAGE_META:
        raise ValueError(f"未知阶段「{stage}」，可选：{', '.join(STAGE_IDS)}")
    provider = cfg.get_provider(str(provider_id or ""))
    if provider is None:
        raise ValueError(f"服务商 {provider_id or '（空）'} 不存在。先添加服务商，再绑阶段。")
    if not model_id:
        raise ValueError("模型不能为空。")
    if provider.models and model_id not in provider.model_ids:
        raise ValueError(
            f"服务商「{provider.name}」下没有模型「{model_id}」。可用："
            f"{', '.join(provider.model_ids)}"
        )
