"""E1/E2：提示词三级覆盖 + 阶段模型路由。

这两件事的共同点是「配置写在文件里，但生效的必须和界面看到的一致」：
提示词被项目级覆盖后，渲染要真的读那一份；阶段绑了模型后，解析要真的用它。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from server import services  # noqa: E402
from workshop import prompt_registry, routing  # noqa: E402
from workshop.config import load_config  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    cfg = yaml.safe_load((ROOT / "providers.yaml").read_text(encoding="utf-8"))
    cfg["providers"][0]["enabled"] = True      # 测试要验证「可用」，先启用
    (root / "providers.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (root / "config").mkdir()
    monkeypatch.setenv("WORKSHOP_ROOT", str(root))
    return root


# ── E1 提示词 ──────────────────────────────────────────────


def test_prompt_levels_and_variables(sandbox):
    work = sandbox / "workspaces" / "某作品"
    work.mkdir(parents=True)

    builtin = prompt_registry.resolve("draft.system", work_dir=work)
    assert builtin["source"] == "builtin"
    assert len(builtin["text"]) > 100

    (sandbox / "prompts").mkdir()
    (sandbox / "prompts" / "draft.system.md").write_text("全局覆盖的正文提示。", encoding="utf-8")
    glob = prompt_registry.resolve("draft.system", work_dir=work)
    assert glob["source"] == "global"
    assert glob["text"] == "全局覆盖的正文提示。"

    (work / "prompts").mkdir()
    (work / "prompts" / "draft.system.md").write_text("这一部专用的正文提示。", encoding="utf-8")
    proj = prompt_registry.resolve("draft.system", work_dir=work)
    assert proj["source"] == "project"
    assert proj["text"] == "这一部专用的正文提示。"

    # 缺变量必须报错并点名，不能把 {{var}} 交给模型
    with pytest.raises(prompt_registry.PromptVariableError) as exc:
        prompt_registry.render("beats.system")
    assert "beats_min" in str(exc.value) and "beats_max" in str(exc.value)

    rendered = prompt_registry.render("review.system", variables={"min_evidence": 12})
    assert "{{" not in rendered["text"] and "12" in rendered["text"]

    rows = prompt_registry.catalog(work_dir=work)
    assert {row["id"] for row in rows} == set(prompt_registry.SPECS)
    draft = next(row for row in rows if row["id"] == "draft.system")
    assert draft["source"] == "project" and draft["overridden"] is True


# ── E2 阶段路由 ────────────────────────────────────────────


def test_stage_resolution_order(sandbox):
    cfg = load_config(sandbox / "providers.yaml")
    default = routing.resolve(cfg, "draft")
    assert default["source"] == "default"
    assert default["provider_id"] == "deepseek"
    assert default["model"] == "deepseek-flash"

    # 显式请求 > 阶段配置 > 默认
    requested = routing.resolve(cfg, "draft", requested_provider="deepseek",
                                requested_model="deepseek-flash")
    assert requested["source"] == "request"

    matrix = routing.matrix(cfg, key_configured=lambda ref: True)
    assert [row["stage"] for row in matrix] == list(routing.STAGE_IDS)
    assert all(row["available"] for row in matrix)
    assert all(row["recommendation"] for row in matrix)


def test_save_and_clear_stage_binding(sandbox):
    paths = services.Paths(root=sandbox)
    result = services.save_stage_binding(
        paths, stage="extract", provider="deepseek", model="deepseek-flash"
    )
    assert result["ok"] and result["effective"]["source"] == "stage"
    assert result["effective"]["model"] == "deepseek-flash"

    cfg = load_config(sandbox / "providers.yaml")
    assert cfg.task_bindings["stages"]["extract"]["provider"] == "deepseek"

    cleared = services.save_stage_binding(paths, stage="extract", clear=True)
    assert cleared["effective"]["source"] == "default"
    cfg2 = load_config(sandbox / "providers.yaml")
    assert "extract" not in ((cfg2.task_bindings or {}).get("stages") or {})


def test_stage_validation_errors_are_readable(sandbox):
    paths = services.Paths(root=sandbox)
    with pytest.raises(ValueError) as exc:
        services.save_stage_binding(
            paths, stage="draft", provider="deepseek", model="no-such-model"
        )
    message = str(exc.value)
    assert "no-such-model" in message and "可用" in message

    with pytest.raises(ValueError) as exc2:
        services.save_stage_binding(paths, stage="nope", provider="deepseek", model="deepseek-flash")
    assert "未知阶段" in str(exc2.value)


def test_settings_overview_exposes_matrix_and_prompts(sandbox):
    overview = services.settings_overview(services.Paths(root=sandbox))
    assert len(overview["stages"]) == 4
    assert {"stage", "source", "available", "risks", "recommendation"} <= set(overview["stages"][0])
    assert {row["id"] for row in overview["prompts"]} == set(prompt_registry.SPECS)
    assert overview["binding"]["provider"] == "deepseek"
