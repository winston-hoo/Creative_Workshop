"""知识库接口自检：K1 实体卡片 / K2 台账 / K3 指纹，构建与读取。

    python tests/test_kb.py

验证的是知识库的接口契约：

  1. `GET /api/works/{name}/kb` —— 没构建过 exists:false，构建过返回三层
  2. `POST /api/works/{name}/kb/build` —— 纯脚本构建，不调模型，随时可点
  3. K2 聚合的纪律 —— 标注里没有的维度（支线名）不编造，显式说明
  4. K3 指纹 —— 样本不足时明说无意义，不作假
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

app = server_main.app

TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-kb-test-"))
server_main.PATHS = services.Paths(root=TMP_ROOT)
WS = TMP_ROOT / "workspaces"

for _name in ("providers.yaml", "primitives.yaml"):
    shutil.copy(ROOT / _name, TMP_ROOT / _name)

client = TestClient(app)

TEST_WORK = "知识库自检作品"

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


def fake_annotations(count: int = 8) -> None:
    """给前 count 章写标注。
    故意给 subplot_count 与 time_span 让 K2 有时间线/支线数据。"""
    from workshop.annotate import strip_front_matter, text_sha256

    work_dir = WS / TEST_WORK
    (work_dir / "10-annotations").mkdir(parents=True, exist_ok=True)
    chapters_dir = work_dir / "00-ingest" / "chapters"
    files = sorted(chapters_dir.glob("*.md"))[:count]
    assert files, "章节文件不存在，测试数据异常"
    for idx, path in enumerate(files, start=1):
        cid = path.stem
        _meta, body = strip_front_matter(path.read_text(encoding="utf-8"))
        rec = {
            "chapter_id": cid,
            "chapter_no": idx,
            "status": "ok",
            "fields": {
                "hook_strength": 4,
                "emotion": 3,
                "conflict": 2,
                "info_release": 3,
                "mainline_progress": 3,
                "motive_strength": 2,
                "subplot_count": 1,
                "time_span": "即时",
                "hook_type": "悬念",
                "perspective": "第三限知",
                "chapter_summary": "本章剧情推进",
            },
            "self_report": {"confidence": "高", "uncertain_fields": []},
            "issues": [],
            "source": {"sha256": text_sha256(body)},
        }
        (work_dir / "10-annotations" / f"{cid}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8"
        )


def test_kb_not_built() -> None:
    print("未构建")
    import_test_work()
    data = client.get(f"/api/works/{TEST_WORK}/kb").json()
    check(data.get("exists") is False, "未构建时 exists:false")


def test_kb_build_and_read() -> None:
    print("构建并读取")
    fake_annotations()
    r = client.post(f"/api/works/{TEST_WORK}/kb/build")
    check(r.status_code == 200, f"构建成功（{r.status_code}）")
    body = r.json()
    check("k1" in body and "k2" in body and "k3" in body, "返回三层结构")

    # K2 台账
    check(isinstance(body["k2"]["person"]["count"], int), "人物台账有计数")
    check(isinstance(body["k2"]["timeline"]["span_counts"], dict), "时间线有跨度分布")
    check(isinstance(body["k2"]["sideplot"]["ranges"], list), "支线在场区间存在")
    check(
        body["k2"]["sideplot"]["note"] and "支线" in body["k2"]["sideplot"]["note"],
        "数据不足的维度显式说明，不编造",
    )

    # K3 指纹
    check(body["k3"]["available"] is True, "标注足够时指纹可用")
    check(len(body["k3"]["items"]) > 0, "指纹有条目")
    for it in body["k3"]["items"][:3]:
        check(it.get("sample") and it.get("confidence"), "条目带样本量与置信度")

    # 落盘文件
    for name in ("index.json", "k1-entities.json", "k2-material.json", "k3-fingerprint.json"):
        check((WS / TEST_WORK / "20-kb" / name).exists(), f"落盘 {name}")

    # 读取接口
    data = client.get(f"/api/works/{TEST_WORK}/kb").json()
    check(data.get("exists") is True, "构建后可读取")


def test_kb_insufficient_samples() -> None:
    print("样本不足时指纹不作假")
    import_test_work()
    fake_annotations(3)  # 只有 3 章，低于 5 章门槛
    r = client.post(f"/api/works/{TEST_WORK}/kb/build")
    body = r.json()
    check(body["k3"]["available"] is False, "标注不足时指纹标记为不可用")
    check("样本" in body["k3"]["note"] or "标注不足" in body["k3"]["note"], "说明原因")


def test_kb_missing_work() -> None:
    print("未知作品")
    check(
        client.post("/api/works/不存在作品/kb/build").status_code == 400,
        "未知作品给 400 而不是 500",
    )


def main() -> int:
    print("=" * 58)
    print("知识库接口自检")
    print("=" * 58)

    for fn in (
        test_kb_not_built,
        test_kb_build_and_read,
        test_kb_insufficient_samples,
        test_kb_missing_work,
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