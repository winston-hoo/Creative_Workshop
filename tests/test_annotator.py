"""标注台接口自检：字段定义、章节列表、单章明细、保存人工修正。

    python tests/test_annotator.py

验证的是标注台 UI-P3 的接口契约：

  1. `GET /annotator/fields` —— 右栏字段面板的字段定义（模型轨）
  2. `GET /annotator/chapters` —— 左栏章节列表（带状态色标）
  3. `GET /annotator/chapters/{id}` —— 中栏正文 + 当前标注字段
  4. `PUT /annotator/chapters/{id}` —— 保存人工修正

保存修正的纪律是本文件重点：
  · 只改已有字段，不新增
  · 人改过字段 → status 自动转 needs_review
  · 显式确认 ok → 状态正常
  · 未标注章节不允许保存（标注台是修正，不是 create）
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

TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-annotator-test-"))
server_main.PATHS = services.Paths(root=TMP_ROOT)
WS = TMP_ROOT / "workspaces"

for _name in ("providers.yaml", "primitives.yaml"):
    shutil.copy(ROOT / _name, TMP_ROOT / _name)

client = TestClient(app)

TEST_WORK = "标注台自检作品"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def import_test_work() -> dict:
    shutil.rmtree(WS / TEST_WORK, ignore_errors=True)
    archive = WS / "_archive"
    if archive.exists():
        for p in archive.glob(f"{TEST_WORK}*"):
            shutil.rmtree(p, ignore_errors=True)
    files = {"file": ("合成测试.txt", build_synthetic_novel().encode("utf-8"), "text/plain")}
    preview = client.post("/api/import/upload", files=files, data={"work": TEST_WORK}).json()
    return client.post(
        "/api/import/commit",
        json={"token": preview["token"], "work": TEST_WORK, "strip_ads": False, "strip_repeated": False},
    ).json()


def fake_annotation(chapter_id: str, *, status: str = "ok", confidence: str = "高") -> None:
    """造一条含模型轨字段的标注。"""
    from workshop.annotate import strip_front_matter, text_sha256

    work_dir = WS / TEST_WORK
    raw = (work_dir / "00-ingest" / "chapters" / f"{chapter_id}.md").read_text(encoding="utf-8")
    _meta, body = strip_front_matter(raw)
    record = {
        "schema_version": "annotation-v1",
        "chapter_id": chapter_id,
        "chapter_no": 1,
        "title": "夜行",
        "status": status,
        "fields": {
            "perspective": "第三限知",
            "hook_strength": 4,
            "emotion": 3,
            "conflict": 2,
            "mainline_progress": 3,
            "chapter_summary": "一句话梗概",
            "payoffs": [{"锚点": "重要段落", "类型": "反转", "强度": 4}],
        },
        "self_report": {"confidence": confidence, "uncertain_fields": []},
        "issues": [],
        "provenance": {"model_fields_filled": 8, "model_fields_total": 8},
        "source": {"sha256": text_sha256(body)},
    }
    target = work_dir / "10-annotations" / f"{chapter_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def test_fields_endpoint() -> None:
    print("字段定义")
    import_test_work()
    data = client.get(f"/api/works/{TEST_WORK}/annotator/fields").json()
    check("model_fields" in data and len(data["model_fields"]) > 0, "返回了模型轨字段")
    keys = [f["key"] for f in data["model_fields"]]
    check("hook_strength" in keys and "emotion" in keys, "包含评分字段")
    check("payoffs" in keys and "foreshadows" in keys, "包含对象列表字段")
    check("field_order" in data and len(data["field_order"]) > 0, "有字段顺序")


def test_chapters_list() -> None:
    print("章节列表")
    fake_annotation("v001-c0001", status="ok")
    fake_annotation("v001-c0002", status="needs_review")
    data = client.get(f"/api/works/{TEST_WORK}/annotator/chapters").json()
    check(data["total"] == 8, f"全部章节读出来（{data['total']}）")
    by_id = {c["id"]: c for c in data["items"]}
    check(by_id["v001-c0001"]["status"] == "ok", "正常章状态 ok")
    check(by_id["v001-c0002"]["status"] == "needs_review", "待复核章状态正确")
    check(by_id["v001-c0003"]["status"] is None, "未标注章状态为空")


def test_chapter_detail() -> None:
    print("单章明细")
    fake_annotation("v001-c0001")
    data = client.get(f"/api/works/{TEST_WORK}/annotator/chapters/v001-c0001").json()
    check("text" in data and len(data["text"]) > 50, "有正文")
    check(data["fields"]["hook_strength"] == 4, "字段带出来")
    check(data["self_report"]["confidence"] == "高", "自评带出来")
    check(data["prev"] is None and data["next"] is not None, "前后章导航")
    r = client.get(f"/api/works/{TEST_WORK}/annotator/chapters/v001-c9999")
    check(r.status_code == 404, "不存在的章 404")


def test_save_review() -> None:
    print("保存人工修正")
    fake_annotation("v001-c0001", status="ok")
    fake_annotation("v001-c0002", status="ok")

    # ① 改字段 → 自动转待复核
    r = client.put(
        f"/api/works/{TEST_WORK}/annotator/chapters/v001-c0001",
        json={"fields": {"hook_strength": 2}, "status": None},
    )
    check(r.status_code == 200 and r.json()["status"] == "needs_review", "改字段后自动转待复核")
    saved = json.loads((WS / TEST_WORK / "10-annotations" / "v001-c0001.json").read_text(encoding="utf-8"))
    check(saved["fields"]["hook_strength"] == 2, "修正值落盘")

    # ② 显式确认 ok
    r = client.put(
        f"/api/works/{TEST_WORK}/annotator/chapters/v001-c0001",
        json={"fields": None, "status": "ok"},
    )
    check(r.status_code == 200 and r.json()["status"] == "ok", "显式确认后状态为正常")

    # ③ 传了未定义字段 → 被忽略，不新增
    r = client.put(
        f"/api/works/{TEST_WORK}/annotator/chapters/v001-c0001",
        json={"fields": {"不存在的字段": 1}, "status": None},
    )
    saved = json.loads((WS / TEST_WORK / "10-annotations" / "v001-c0001.json").read_text(encoding="utf-8"))
    check("不存在的字段" not in saved["fields"], "未定义字段不落盘")

    # ④ 未标注章节不允许保存
    r = client.put(
        f"/api/works/{TEST_WORK}/annotator/chapters/v001-c0005",
        json={"fields": {"hook_strength": 3}, "status": None},
    )
    check(r.status_code == 400, "未标注章节拒绝保存")

    # ⑤ 浅层 merge：未改的字段保留
    r = client.put(
        f"/api/works/{TEST_WORK}/annotator/chapters/v001-c0002",
        json={"fields": {"conflict": 5}, "status": None},
    )
    saved = json.loads((WS / TEST_WORK / "10-annotations" / "v001-c0002.json").read_text(encoding="utf-8"))
    check(
        saved["fields"]["hook_strength"] == 4 and saved["fields"]["conflict"] == 5,
        "只改提交的字段，其他保留",
    )


def test_unknown_work() -> None:
    print("未知作品")
    check(
        client.get(f"/api/works/不存在作品/annotator/chapters").status_code == 400,
        "未知作品给 400 而不是 500",
    )


def main() -> int:
    print("=" * 58)
    print("标注台接口自检")
    print("=" * 58)

    for fn in (
        test_fields_endpoint,
        test_chapters_list,
        test_chapter_detail,
        test_save_review,
        test_unknown_work,
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