"""设置页新功能自检：自定义服务商 CRUD、默认模型绑定、限速配置。

    python tests/test_provider_admin.py

覆盖三块新增能力：

  1. 设置页可以新增/编辑/删除自定义服务商（中转站等 OpenAI 兼容端点），
     配置落盘在 custom-providers.yaml，不改动带注释的 providers.yaml
  2. 可以设置「默认模型」——没单独指定模型的任务都回退到它
  3. 每分钟限速配置可读写，并被客户端限速器真实执行

与 providers.yaml 的关系是本文件最该守住的边界：
**程序只写 custom-providers.yaml，绝不重写 providers.yaml**。
否则手写注释会被 yaml.dump 冲掉，这是不可逆的。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from server import main as server_main  # noqa: E402
from server import services  # noqa: E402
from workshop.ratelimit import SlidingWindowRateLimiter  # noqa: E402

app = server_main.app

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def fresh_root() -> Path:
    """干净的临时工作台根目录。"""
    root = Path(tempfile.mkdtemp(prefix="workshop-provider-admin-"))
    shutil.copy(ROOT / "providers.yaml", root / "providers.yaml")
    shutil.copy(ROOT / "primitives.yaml", root / "primitives.yaml")
    (root / "config").mkdir(parents=True, exist_ok=True)
    return root


def client_for(root: Path) -> TestClient:
    server_main.PATHS = services.Paths(root=root)
    return TestClient(app)


# ── 服务商 CRUD ──────────────────────────────────────────


def test_create_provider() -> None:
    print("新增自定义服务商")
    root = fresh_root()
    client = client_for(root)
    r = client.post(
        "/api/settings/providers",
        json={
            "name": "我的中转",
            "base_url": "https://relay.example.com/v1",
            "models": ["DeepSeek 中转 | deepseek/deepseek-flash", "Claude 中转 | anthropic/claude-3"],
            "api_key": "sk-test-0123456789abcdef",
            "requests_per_minute": 60,
        },
    )
    check(r.status_code == 200, f"创建成功（{r.status_code}）")
    body = r.json()
    check(body.get("ok") is True, "返回 ok")
    check(body.get("provider"), "返回了服务商 id")

    # 配置落在 custom-providers.yaml，而不是 providers.yaml
    custom = root / "custom-providers.yaml"
    check(custom.exists(), "生成了 custom-providers.yaml")
    check(
        "我的中转" in custom.read_text(encoding="utf-8"),
        "custom 文件里有服务商",
    )
    base_text = (root / "providers.yaml").read_text(encoding="utf-8")
    check("我的中转" not in base_text, "providers.yaml 未被程序改写（注释安全）")

    # 概览里能看到，且带 custom / rate_limit 标记
    ov = client.get("/api/settings").json()
    me = [p for p in ov["providers"] if p["id"] == body["provider"]][0]
    check(me["custom"] is True, "标记为设置页管理")
    check(me["rate_limit"]["requests_per_minute"] == 60, "限速配置回读正确")
    ids = [m["id"] for m in me["models"]]
    check("deepseek/deepseek-flash" in ids, "模型列表里有中转站模型名（带斜杠）")

    # 密钥不随配置回显
    dump = json.dumps(ov, ensure_ascii=False)
    check("sk-test-0123456789abcdef" not in dump, "接口不回显明文密钥")


def test_update_and_delete_provider() -> None:
    print("编辑与删除自定义服务商")
    root = fresh_root()
    client = client_for(root)
    pid = client.post(
        "/api/settings/providers",
        json={"name": "服务商A", "base_url": "https://a.example.com/v1", "models": ["m1"]},
    ).json()["provider"]

    r = client.put(
        f"/api/settings/providers/{pid}",
        json={
            "name": "服务商A改名",
            "base_url": "https://b.example.com/v1",
            "models": ["m2"],
            "requests_per_minute": 10,
        },
    )
    check(r.status_code == 200, "编辑成功")
    ov = client.get("/api/settings").json()
    me = [p for p in ov["providers"] if p["id"] == pid][0]
    check(me["name"] == "服务商A改名", "名字已更新")
    check(me["base_url"] == "https://b.example.com/v1", "base_url 已更新")
    check([m["id"] for m in me["models"]] == ["m2"], "模型列表已更新")
    check(me["rate_limit"]["requests_per_minute"] == 10, "限速已更新")

    # 未绑定时可以直接删除
    r = client.delete(f"/api/settings/providers/{pid}")
    check(r.status_code == 200 and r.json().get("ok"), "删除成功")
    ids = [p["id"] for p in client.get("/api/settings").json()["providers"]]
    check(pid not in ids, "概览里已没有它")
    check((root / "custom-providers.yaml").exists(), "custom 文件保留")


def test_delete_bound_provider_blocked() -> None:
    print("被任务绑定引用的服务商拒绝删除")
    root = fresh_root()
    client = client_for(root)
    pid = client.post(
        "/api/settings/providers",
        json={"name": "绑定服务商", "base_url": "https://c.example.com/v1", "models": ["m1"]},
    ).json()["provider"]
    client.put("/api/settings/binding", json={"provider": pid, "model": "m1"})
    r = client.delete(f"/api/settings/providers/{pid}")
    check(r.status_code == 400, f"拒绝删除（{r.status_code}）")
    check("任务绑定" in json.dumps(r.json(), ensure_ascii=False), "说明原因与任务名")


def test_invalid_provider_rejected() -> None:
    print("非法入参拒绝")
    root = fresh_root()
    client = client_for(root)
    r = client.post(
        "/api/settings/providers",
        json={"name": "没地址", "base_url": "not-a-url", "models": ["m1"]},
    )
    check(r.status_code == 400, f"非法 URL 拒绝（{r.status_code}）")
    r = client.post(
        "/api/settings/providers",
        json={"name": "", "base_url": "https://x.example.com", "models": ["m1"]},
    )
    check(r.status_code == 400, "空名称拒绝")
    r = client.post(
        "/api/settings/providers",
        json={"name": "没模型", "base_url": "https://x.example.com", "models": []},
    )
    check(r.status_code == 400, "空模型列表拒绝")


# ── 默认模型绑定 ──────────────────────────────────────────


def test_default_binding() -> None:
    print("设置默认模型")
    root = fresh_root()
    client = client_for(root)
    pid = client.post(
        "/api/settings/providers",
        json={"name": "默认服务商", "base_url": "https://d.example.com/v1", "models": ["main-model", "backup"]},
    ).json()["provider"]

    r = client.put("/api/settings/binding", json={"provider": pid, "model": "main-model"})
    check(r.status_code == 200 and r.json().get("ok"), "保存成功")
    ov = client.get("/api/settings").json()
    check(
        ov["binding"] == {"provider": pid, "model": "main-model"},
        "概览回读绑定正确",
    )
    # 不存在的模型名被拒绝
    r = client.put("/api/settings/binding", json={"provider": pid, "model": "no-such-model"})
    check(r.status_code == 400, "不存在的模型名被拒绝")
    # 不存在的服务商被拒绝
    r = client.put("/api/settings/binding", json={"provider": "nope", "model": "x"})
    check(r.status_code == 400, "不存在的服务商被拒绝")
    # 换回内置 deepseek 也可以
    r = client.put("/api/settings/binding", json={"provider": "deepseek", "model": "deepseek-flash"})
    check(r.status_code == 200, "绑定切回内置服务商成功")


# ── 限速 ──────────────────────────────────────────────────


def test_rate_limiter_blocks_over_limit() -> None:
    print("限速器真实阻挡超窗请求")
    rl = SlidingWindowRateLimiter(requests_per_minute=2, max_wait_sec=1.0)
    rl.acquire()
    rl.acquire()
    raised = False
    try:
        rl.acquire()
    except Exception as exc:  # noqa: BLE001
        raised = True
        text = str(exc)
        check("每分钟请求上限 2 次" in text, "错误说明了是限速")
        check("降低并发" in text, "错误给了处置建议")
    check(raised, "超窗的第三次被拒绝（不在窗口内排队干等）")


def test_rate_limit_round_trip() -> None:
    print("限速配置读写回环")
    root = fresh_root()
    client = client_for(root)
    pid = client.post(
        "/api/settings/providers",
        json={
            "name": "限速服务商",
            "base_url": "https://e.example.com/v1",
            "models": ["m1"],
            "requests_per_minute": 30,
            "tokens_per_minute": 200000,
        },
    ).json()["provider"]
    me = [p for p in client.get("/api/settings").json()["providers"] if p["id"] == pid][0]
    check(me["rate_limit"]["requests_per_minute"] == 30, "RPM 回读正确")
    check(me["rate_limit"]["tokens_per_minute"] == 200000, "TPM 回读正确")


def main() -> int:
    print("=" * 58)
    print("设置页新功能自检（服务商 CRUD / 默认模型 / 限速）")
    print("=" * 58)

    for fn in (
        test_create_provider,
        test_update_and_delete_provider,
        test_delete_bound_provider_blocked,
        test_invalid_provider_rejected,
        test_default_binding,
        test_rate_limiter_blocks_over_limit,
        test_rate_limit_round_trip,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(False, f"{fn.__name__} 抛出异常：{type(exc).__name__}: {exc}")
        print()

    print("=" * 58)
    if _failures:
        print(f"未通过 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())