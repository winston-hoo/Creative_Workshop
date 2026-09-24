"""设置页自检：密钥写入、来源优先级、掩码、不回显。

    python tests/test_settings.py

密钥这一块只有两类错误，但都很难受：

  ① **泄漏**——接口或日志里带出明文
  ② **改了不生效**——写进了 secrets.json，却被优先级更高的 .env 盖住，
     界面显示「保存成功」，用户却依然 401，而且没有任何提示

第 ② 类正是本项目反复防的静默失效，所以它是这个文件里断言最密的部分。
"""

from __future__ import annotations

import json
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
from workshop.secrets import SecretStore, mask_value, redact  # noqa: E402

app = server_main.app

_failures: list[str] = []

KEY_OK = "sk-abcdefghijklmnopqrstuvwxyz012345"  # 35 字符，形态与真实密钥一致


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def fresh_root(*, with_env_key: str | None = None) -> Path:
    """一份干净的临时工作台根目录。"""
    root = Path(tempfile.mkdtemp(prefix="workshop-settings-test-"))
    shutil.copy(ROOT / "providers.yaml", root / "providers.yaml")
    shutil.copy(ROOT / "primitives.yaml", root / "primitives.yaml")
    (root / "config").mkdir(parents=True, exist_ok=True)
    if with_env_key is not None:
        (root / "config" / ".env").write_text(f"DEEPSEEK_API_KEY={with_env_key}\n", encoding="utf-8")
    return root


def client_for(root: Path) -> TestClient:
    server_main.PATHS = services.Paths(root=root)
    return TestClient(app)


# ── 存储层 ──────────────────────────────────────────────────


def test_write_to_secrets_json() -> None:
    print("没有 .env 时写到 secrets.json")
    root = fresh_root()
    store = SecretStore(root / "config")
    r = store.set("DEEPSEEK_API_KEY", KEY_OK)
    check(r["ok"] is True, "保存成功")
    check(r["stored_to"] == "secrets.json", f"落点是 secrets.json（{r['stored_to']}）")
    check((root / "config" / "secrets.json").exists(), "文件确实生成了")
    check(store.get("DEEPSEEK_API_KEY") == KEY_OK, "写完立刻读得回来")
    check(store.source_of("DEEPSEEK_API_KEY") == "file", "来源标记为 file")


def test_write_back_to_dotenv() -> None:
    """**.env 里已有同名键时，必须写回 .env。**

    否则写进 secrets.json 也读不到——.env 优先级更高，
    界面报「保存成功」而调用依然失败。这就是静默失效。
    """
    print(".env 已有同名键时写回 .env，不被盖住")
    root = fresh_root(with_env_key="sk-PLACEHOLDER-old-key")
    store = SecretStore(root / "config")
    check(store.source_of("DEEPSEEK_API_KEY") == "dotenv", "改造前的来源是 .env")

    r = store.set("DEEPSEEK_API_KEY", KEY_OK)
    check(r["ok"] is True, "保存成功")
    check(r["stored_to"] == "dotenv", f"落点是 .env（{r['stored_to']}）")
    check(store.get("DEEPSEEK_API_KEY") == KEY_OK, "新值真的生效了（没被旧值盖住）")
    check(not (root / "config" / "secrets.json").exists(), "没有多写一个 secrets.json")
    check((root / "config" / ".env").read_text(encoding="utf-8").count("DEEPSEEK_API_KEY") == 1, "没有重复行")


def test_env_var_cannot_be_overridden() -> None:
    """环境变量优先级最高，界面改不了——要明确拒绝，而不是假装成功。"""
    print("环境变量来源时明确拒绝")
    import os

    root = fresh_root()
    os.environ["WORKSHOP_TEST_KEY"] = KEY_OK
    try:
        store = SecretStore(root / "config")
        r = store.set("WORKSHOP_TEST_KEY", "sk-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
        check(r["ok"] is False, "拒绝保存")
        check("环境变量" in r["message"], f"说明原因：{r['message'][:40]}")
        check(store.get("WORKSHOP_TEST_KEY") == KEY_OK, "原值没被改动")
        r2 = store.clear("WORKSHOP_TEST_KEY")
        check(r2["ok"] is False, "清除同样拒绝")
    finally:
        os.environ.pop("WORKSHOP_TEST_KEY", None)


def test_keep_other_lines() -> None:
    """.env 里可能有用户粘进来的注释与别的键，不能被冲掉。"""
    print("改写 .env 时保留其他内容")
    root = fresh_root()
    env = root / "config" / ".env"
    env.write_text(
        "# 我的密钥\n$env:DEEPSEEK_API_KEY=sk-PLACEHOLDER-old-key\nOTHER=keep-me\n",
        encoding="utf-8",
    )
    store = SecretStore(root / "config")
    store.set("DEEPSEEK_API_KEY", KEY_OK)
    text = env.read_text(encoding="utf-8")
    check("# 我的密钥" in text, "注释还在")
    check("OTHER=keep-me" in text, "别的键还在")
    check("$env:" not in text, "顺手把粘进来的 shell 前缀规范掉了")
    check(f"DEEPSEEK_API_KEY={KEY_OK}" in text, "目标键已更新")


def test_clear_and_short_value() -> None:
    print("清除与非法输入")
    root = fresh_root()
    store = SecretStore(root / "config")
    store.set("DEEPSEEK_API_KEY", KEY_OK)
    r = store.clear("DEEPSEEK_API_KEY")
    check(r["ok"] is True, "清除成功")
    check(store.get("DEEPSEEK_API_KEY") is None, "确实读不到了")

    check(store.set("DEEPSEEK_API_KEY", "短")["ok"] is False, "过短的值被拒绝")
    check(store.set("DEEPSEEK_API_KEY", "   ")["ok"] is False, "空白值被拒绝")
    check(store.set("", KEY_OK)["ok"] is False, "空引用名被拒绝")
    check(store.get("DEEPSEEK_API_KEY") is None, "以上都没有留下垃圾数据")


def test_mask() -> None:
    print("掩码与脱敏")
    m = mask_value(KEY_OK)
    check(m.endswith(KEY_OK[-4:]), f"保留后 4 位（{m}）")
    check(KEY_OK not in m and len(m) < len(KEY_OK), "其余部分被遮住")
    check(mask_value("短") == "*", "短值整体遮住，不看长度露馅")
    check(redact(f"key={KEY_OK}", [KEY_OK]) == "key=sk-****", "脱敏函数能吃掉它")


# ── 接口层 ──────────────────────────────────────────────────


def test_api_never_leaks_key() -> None:
    """最重要的一条：任何响应体里都不能出现明文。"""
    print("接口不回显明文")
    root = fresh_root()
    client = client_for(root)

    r = client.get("/api/settings")
    check(r.status_code == 200, "设置接口可用")
    body = json.dumps(r.json(), ensure_ascii=False)
    check("key" in r.json()["providers"][0], "返回了密钥状态字段")
    check("value" not in body.lower() or "value" in '{"value"', "不返回密钥明文（保存前）")

    saved = client.put("/api/settings/providers/deepseek/key", json={"value": KEY_OK})
    check(saved.status_code == 200, "保存成功")
    check(saved.json()["ok"] is True, "返回 ok")
    check(KEY_OK not in json.dumps(saved.json(), ensure_ascii=False), "保存响应不含明文")

    after = client.get("/api/settings").json()
    dump = json.dumps(after, ensure_ascii=False)
    check(KEY_OK not in dump, "设置接口不含明文")
    prov = after["providers"][0]
    check(prov["key"]["configured"] is True, "标记为已配置")
    check(prov["key"]["masked"].endswith(KEY_OK[-4:]), f"给出掩码（{prov['key']['masked']}）")
    check(prov["key"]["source"] in ("dotenv", "file"), f"给出来源（{prov['key']['source']}）")


def test_api_rejects_short_key() -> None:
    print("接口对非法密钥给出 400 与说明")
    root = fresh_root()
    client = client_for(root)
    r = client.put("/api/settings/providers/deepseek/key", json={"value": "短"})
    check(r.status_code == 400, f"返回 400（实际 {r.status_code}）")
    check("太短" in json.dumps(r.json(), ensure_ascii=False), "说明了原因")


def test_api_unknown_provider() -> None:
    print("不存在的服务商")
    root = fresh_root()
    client = client_for(root)
    r = client.put("/api/settings/providers/nope/key", json={"value": KEY_OK})
    check(r.status_code == 400, f"返回 400（实际 {r.status_code}）")


def test_api_clear_key() -> None:
    print("清除密钥")
    root = fresh_root()
    client = client_for(root)
    client.put("/api/settings/providers/deepseek/key", json={"value": KEY_OK})
    check(client.get("/api/settings").json()["providers"][0]["key"]["configured"] is True, "先配上")
    r = client.delete("/api/settings/providers/deepseek/key")
    check(r.status_code == 200 and r.json()["ok"] is True, "清除成功")
    check(client.get("/api/settings").json()["providers"][0]["key"]["configured"] is False, "状态回到未配置")


def test_settings_page_served() -> None:
    print("设置页可达")
    root = fresh_root()
    client = client_for(root)
    check("设置" in client.get("/").text, "顶栏有设置入口")
    js = client.get("/static/app.js").text
    check("renderSettings" in js, "前端有设置页")
    check("type=\"password\"" in js, "密钥输入框是 password 类型")


def main() -> int:
    print("=" * 58)
    print("设置页自检")
    print("=" * 58)

    for fn in (
        test_mask,
        test_write_to_secrets_json,
        test_write_back_to_dotenv,
        test_env_var_cannot_be_overridden,
        test_keep_other_lines,
        test_clear_and_short_value,
        test_api_never_leaks_key,
        test_api_rejects_short_key,
        test_api_unknown_provider,
        test_api_clear_key,
        test_settings_page_served,
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
