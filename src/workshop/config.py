"""读取 providers.yaml。

设计原则：文件系统是唯一真相源。配置就是文件，脚本与界面读同一份，
不存在「数据库里一份、文件里一份」的不一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "providers.yaml"
CUSTOM_CONFIG_NAME = "custom-providers.yaml"  # 设置页维护的服务商，加载时合并进 providers


@dataclass
class ProviderConfig:
    """单个服务商配置。raw 保留原始字典，便于读写未建模的字段。"""

    id: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.raw.get("name") or self.id

    @property
    def type(self) -> str:
        return self.raw.get("type") or "cloud"

    @property
    def protocol(self) -> str:
        return self.raw.get("protocol") or "openai_compatible"

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled"))

    @property
    def base_url(self) -> str:
        return str(self.raw.get("base_url") or "")

    @property
    def base_url_anthropic(self) -> str | None:
        value = self.raw.get("base_url_anthropic")
        return str(value) if value else None

    @property
    def api_key_ref(self) -> str | None:
        auth = self.raw.get("auth") or {}
        ref = auth.get("api_key_ref")
        return str(ref) if ref else None

    @property
    def auth_scheme(self) -> str:
        auth = self.raw.get("auth") or {}
        return str(auth.get("scheme") or "bearer").lower()

    @property
    def models(self) -> list[dict[str, Any]]:
        return list(self.raw.get("models") or [])

    @property
    def pricing(self) -> dict[str, Any]:
        return dict(self.raw.get("pricing") or {})

    @property
    def manual(self) -> dict[str, Any]:
        return dict(self.raw.get("manual") or {})

    @property
    def rate_limit(self) -> dict[str, Any]:
        """限速配置。兼容两种写法：

            rate_limit:                       # 顶层字段（设置页新增服务商用这个）
              requests_per_minute: 60
              tokens_per_minute: 200000
            manual:
              rate_limit:                     # 跟随模板里的 manual 段
                requests_per_minute: 60
        """
        block = self.raw.get("rate_limit")
        if not isinstance(block, dict) and isinstance(self.raw.get("manual"), dict):
            block = self.raw["manual"].get("rate_limit")
        return dict(block) if isinstance(block, dict) else {}

    @property
    def max_concurrency(self) -> int | None:
        """配置里手填的并发上限，未填时由调度器自己决定。"""
        value = self.manual.get("max_concurrency")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    def model(self, model_id: str) -> dict[str, Any] | None:
        for item in self.models:
            if item.get("id") == model_id:
                return item
        return None

    def model_by_role(self, role: str) -> dict[str, Any] | None:
        for item in self.models:
            if item.get("role") == role:
                return item
        return None

    @property
    def model_ids(self) -> list[str]:
        return [str(m.get("id")) for m in self.models if m.get("id")]


@dataclass
class WorkshopConfig:
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self.raw.get("settings") or {})

    @property
    def providers(self) -> list[ProviderConfig]:
        out: list[ProviderConfig] = []
        for item in self.raw.get("providers") or []:
            pid = str(item.get("id") or "").strip()
            if pid:
                out.append(ProviderConfig(id=pid, raw=item))
        return out

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        for item in self.providers:
            if item.id == provider_id:
                return item
        return None

    # ── 探测相关设置 ──────────────────────────────────────────

    @property
    def probe_settings(self) -> dict[str, Any]:
        return dict(self.settings.get("probe") or {})

    @property
    def probe_thinking(self) -> str:
        """探测时的思考模式默认值。默认关闭，理由见 probe.py 的 ProbeOptions。"""
        return str(self.probe_settings.get("thinking") or "disabled")

    @property
    def report_settings(self) -> dict[str, Any]:
        return dict(self.settings.get("probe_reports") or {})

    @property
    def reports_dir(self) -> Path:
        rel = self.report_settings.get("path") or "config/probe-reports/"
        return (self.path.parent / str(rel)).resolve()

    @property
    def archive_enabled(self) -> bool:
        return bool(self.report_settings.get("archive", True))

    @property
    def alert_threshold_pct(self) -> float:
        return float(self.report_settings.get("alert_on_change_pct") or 50)

    @property
    def task_bindings(self) -> dict[str, Any]:
        return dict(self.raw.get("task_bindings") or {})


def load_config(path: str | Path = DEFAULT_CONFIG_NAME) -> WorkshopConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到配置文件：{p}\n请确认路径，或从 providers.template.yaml 复制一份。"
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式异常，顶层应为映射：{p}")
    data = _merge_custom_config(p.parent, data)
    return WorkshopConfig(path=p.resolve(), raw=data)


def custom_config_path(config_path: str | Path) -> Path:
    """设置页维护的服务商文件路径。与 providers.yaml 同目录。"""
    return Path(config_path).resolve().parent / CUSTOM_CONFIG_NAME


def _merge_custom_config(config_dir: Path, base: dict[str, Any]) -> dict[str, Any]:
    """把 custom-providers.yaml 合并进主配置。

    设置页新增/编辑的服务商写在单独的文件里，因为 providers.yaml 里
    有大量手写注释，程序全量重写会把这些注释冲掉。合并规则：

      · providers：同 id 覆盖，新 id 追加
      · task_bindings：同任务覆盖，新任务追加

    没有 custom 文件就等于没加过任何服务商，主配置原样返回。
    """
    custom_path = Path(config_dir) / CUSTOM_CONFIG_NAME
    if not custom_path.exists():
        return base
    try:
        custom = yaml.safe_load(custom_path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError):
        # 文件坏了不回滚主配置——宁可让设置页下次保存时重写。
        return base
    if not isinstance(custom, dict):
        return base

    merged = dict(base)

    # 服务商：同 id 覆盖
    by_id: dict[str, dict[str, Any]] = {}
    for item in merged.get("providers") or []:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = dict(item)
    for item in custom.get("providers") or []:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = dict(item)
    merged["providers"] = list(by_id.values())

    # 任务绑定：同任务覆盖
    bindings: dict[str, Any] = dict(merged.get("task_bindings") or {})
    for key, value in (custom.get("task_bindings") or {}).items():
        bindings[key] = value
    merged["task_bindings"] = bindings

    return merged


def save_custom_config(config_path: str | Path, custom: dict[str, Any]) -> Path:
    """把设置页编辑过的内容写回 custom-providers.yaml。

    先写临时文件再原子替换——中途断电/报错都不会留下半个文件。
    """
    path = custom_config_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        k: custom.get(k)
        for k in ("providers", "task_bindings")
        if custom.get(k) not in (None, [], {})
    }
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def resolve_default_model(cfg: WorkshopConfig, provider: ProviderConfig) -> str:
    """没显式指定模型时的默认选择：probe_model > main 角色 > 第一个模型。"""
    probe_model = cfg.probe_settings.get("probe_model")
    if probe_model and (not provider.models or probe_model in provider.model_ids):
        return str(probe_model)
    role_main = provider.model_by_role("main")
    if role_main and role_main.get("id"):
        return str(role_main["id"])
    if provider.model_ids:
        return provider.model_ids[0]
    raise ValueError(f"服务商 {provider.id} 没有配置任何模型，无法探测")
