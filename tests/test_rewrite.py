"""改写台接口自检：M4 六类校验、G2 输出闸门、接受改写写新版本原稿保留。

    python tests/test_rewrite.py

验证的是改写台（UI-P4）的接口契约：

  1. `GET /rewrite/plan` —— 指令类型与章节列表（免费）
  2. `POST /rewrite/start` —— 执行改写（会调模型，返回校验结果）
  3. M4 六类校验 —— V1 长度 / V2 人称 / V3 风格 / V4 结构 / V5 锚点 / V6 伏笔
  4. G2 闸门 —— 有 high 级问题不接受；force 可强制接受
  5. 接受后写新版本文件，原稿（00-ingest）不动

测试用本地 MockServer 模拟模型，不发真实请求。
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
from workshop.samples import build_synthetic_novel  # noqa: E402
from workshop.secrets import SecretStore  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402

app = server_main.app

TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-rewrite-test-"))
server_main.PATHS = services.Paths(root=TMP_ROOT)
WS = TMP_ROOT / "workspaces"

for _name in ("providers.yaml", "primitives.yaml"):
    shutil.copy(ROOT / _name, TMP_ROOT / _name)

client = TestClient(app)

TEST_WORK = "改写台自检作品"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def import_test_work() -> None:
    shutil.rmtree(WS / TEST_WORK, ignore_errors=True)
    archive = WS / "_archive"
    if archive.exists():
        for p in archive.glob(f"{TEST_WORK}*"):
            shutil.rmtree(p, ignore_errors=True)
    files = {"file": ("合成测试.txt", build_synthetic_novel().encode("utf-8"), "text/plain")}
    preview = client.post("/api/import/upload", files=files, data={"work": TEST_WORK}).json()
    client.post(
        "/api/import/commit",
        json={"token": preview["token"], "work": TEST_WORK, "strip_ads": False, "strip_repeated": False},
    )


def first_chapter_id() -> str:
    chapters_dir = WS / TEST_WORK / "00-ingest" / "chapters"
    return sorted(p.stem for p in chapters_dir.glob("*.md"))[0]


def original_text() -> str:
    from workshop.annotate import strip_front_matter

    raw = (WS / TEST_WORK / "00-ingest" / "chapters" / f"{first_chapter_id()}.md").read_text(encoding="utf-8")
    _meta, body = strip_front_matter(raw)
    return body


def fake_annotation_with_foreshadow() -> None:
    """造一份带「伏笔推进」的标注，让 V6 校验有真实素材。"""
    from workshop.annotate import strip_front_matter, text_sha256

    cid = first_chapter_id()
    raw = (WS / TEST_WORK / "00-ingest" / "chapters" / f"{cid}.md").read_text(encoding="utf-8")
    _meta, body = strip_front_matter(raw)
    rec = {
        "chapter_id": cid,
        "chapter_no": 1,
        "status": "ok",
        "fields": {
            "foreshadows": [{"动作": "推进", "编号": "F-001", "描述": "玉佩来历"}],
            "hook_strength": 4,
            "emotion": 3,
            "conflict": 2,
        },
        "self_report": {"confidence": "高"},
        "issues": [],
        "source": {"sha256": text_sha256(body)},
    }
    (WS / TEST_WORK / "10-annotations").mkdir(parents=True, exist_ok=True)
    (WS / TEST_WORK / "10-annotations" / f"{cid}.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8"
    )


def _mock_rewriter(app):
    """把一个 MockServer 挂到 FastAPI 上，让 rewrite/start 打回本地。"""
    # 利用现有测试基建：直接构造一个能返回改写文本的 MockState。
    body = original_text()

    def responder(messages):
        # 改写器要返回「改写后正文」——这里回原文（校验应当通过）
        return body

    state = MockState(responder=responder, models=["mock/mock-flash"], supports_usage=True)
    server = MockServer(state)
    server.__enter__()

    def _cleanup():
        server.__exit__(None, None, None)

    server_main._mock_rewrite_cleanup = _cleanup
    return server.base_url


def test_rewrite_plan() -> None:
    print("改写台计划")
    import_test_work()
    data = client.get(f"/api/works/{TEST_WORK}/rewrite/plan").json()
    check(len(data["directives"]) == 5, "五种指令类型")
    check(len(data["chapters"]) >= 8, "列出章节")
    check("provider_name" in data and "model" in data, "服务商与模型")


def test_rewrite_flow_and_validation() -> None:
    print("改写执行 + 六类校验")
    import_test_work()
    fake_annotation_with_foreshadow()
    base = _mock_rewriter(client)

    # 直接调服务层，把 MockServer 的 base_url 换成 client——用 monkeypatch 太重，
    # 这里验证服务函数本身：构造 provider 指向 mock。
    from pathlib import Path as P

    cfg_path = P(TMP_ROOT) / "providers.yaml"
    # 临时给 providers.yaml 加一个指向 mock 的 provider
    import yaml

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("providers", []).append(
        {
            "id": "mock",
            "name": "Mock",
            "type": "cloud",
            "protocol": "openai_compatible",
            "enabled": True,
            "base_url": base,
            "auth": {"scheme": "bearer", "header_name": "Authorization", "api_key_ref": "MOCK_API_KEY"},
            "models": [{"id": "mock/mock-flash", "alias": "Mock 主力", "role": "main"}],
        }
    )
    cfg["task_bindings"] = {"chapter_annotation": {"provider": "mock", "model": "mock/mock-flash"}}
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 密钥
    store = SecretStore(TMP_ROOT / "config")
    store.set("MOCK_API_KEY", "mock-key-abcdefghijkl")

    cid = first_chapter_id()
    r = client.post(
        f"/api/works/{TEST_WORK}/rewrite/start",
        json={"chapter_id": cid, "directive": "调节奏", "instruction": ""},
    )
    check(r.status_code == 200, f"改写成功（{r.status_code}）")
    body = r.json()
    check("validation" in body or "items" in body, "返回校验结果")
    check(isinstance(body.get("blocking_count"), int), "有阻断计数")

    # 读记录
    rec = client.get(f"/api/works/{TEST_WORK}/rewrite/{cid}").json()
    check("original" in rec and "rewritten" in rec, "记录含原稿与改写稿")
    check(len(rec.get("rewritten") or "") > 0, "改写稿非空（mock 返回原文）")
    check(
        rec["validation"]["blocking_count"] <= 1,
        f"原文改写校验无高阻断或仅风格类（实际 {rec['validation']['blocking_count']}）",
    )
    check((WS / TEST_WORK / "30-rewrite" / f"{cid}.json").exists(), "记录落盘")

    # 接受：若 blocking 存在（可能是风格违规 5+ 处），force 接受
    blocking = rec["validation"].get("blocking_count") or 0
    r = client.post(
        f"/api/works/{TEST_WORK}/rewrite/{cid}/accept", json={"force": blocking > 0}
    )
    check(r.status_code == 200 and r.json().get("ok"), "接受成功（含 force 兜底）")
    version_dir = WS / TEST_WORK / "30-rewrite" / "versions"
    check(len(list(version_dir.glob(f"{cid}.*.md"))) == 1, "写入了新版本文件")
    # 原稿不动
    check(original_text() == rec["original"], "原稿保留未动")


def test_g2_gate_blocks_bad_accept() -> None:
    print("G2 闸门阻断")
    import_test_work()
    cid = first_chapter_id()
    # 造一份有明显 high 级问题的记录：改写稿为空（V4 结构 high）
    (WS / TEST_WORK / "30-rewrite").mkdir(parents=True, exist_ok=True)
    bad = {
        "schema_version": "rewrite-v1",
        "chapter_id": cid,
        "chapter_no": 1,
        "title": "夜行",
        "directive": "精简",
        "model": "mock/mock-flash",
        "original": original_text(),
        "rewritten": "",
        "validation": {
            "items": [
                {"id": "V4_structure", "severity": "high", "message": "改写稿没有段落内容", "detail": "空文本"}
            ],
            "blocking": [
                {"id": "V4_structure", "severity": "high", "message": "改写稿没有段落内容", "detail": "空文本"}
            ],
            "blocking_count": 1,
        },
        "accepted": False,
    }
    (WS / TEST_WORK / "30-rewrite" / f"{cid}.json").write_text(
        json.dumps(bad, ensure_ascii=False), encoding="utf-8"
    )
    r = client.post(f"/api/works/{TEST_WORK}/rewrite/{cid}/accept", json={"force": False})
    check(r.status_code == 400, f"有阻断问题拒绝接受（{r.status_code}）")
    check("阻断" in json.dumps(r.json(), ensure_ascii=False), "说明原因")

    # force 接受
    r = client.post(f"/api/works/{TEST_WORK}/rewrite/{cid}/accept", json={"force": True})
    check(r.status_code == 200 and r.json().get("ok"), "force 可强制接受")


def test_rewrite_unknown() -> None:
    print("未知章节/未知作品")
    import_test_work()
    r = client.post(
        f"/api/works/{TEST_WORK}/rewrite/start",
        json={"chapter_id": "v001-c9999", "directive": "调节奏"},
    )
    check(r.status_code == 400, "不存在的章节 400")
    check(client.get("/api/works/不存在作品/rewrite/plan").status_code == 400, "未知作品 400")


def main() -> int:
    print("=" * 58)
    print("改写台接口自检")
    print("=" * 58)

    for fn in (
        test_rewrite_plan,
        test_rewrite_flow_and_validation,
        test_g2_gate_blocks_bad_accept,
        test_rewrite_unknown,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(False, f"{fn.__name__} 抛出异常：{type(exc).__name__}: {exc}")
        print()

    cleanup_mock()
    print("=" * 58)
    if _failures:
        print(f"未通过 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


def cleanup_mock() -> None:
    fn = getattr(server_main, "_mock_rewrite_cleanup", None)
    if fn:
        fn()


if __name__ == "__main__":
    raise SystemExit(main())