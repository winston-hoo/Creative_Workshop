"""参照作品的档位自检。

钉死一个真实发生过的失效：设定集助手的三档（不注入 / 只注入结构指纹 / 注入全部素材）
原先共用「知识库 K1 非空」一个判据，于是**只想借节奏**的作者也被挡在门外，
而放开它要跑实体统计——读全本调模型，是这条链上最贵的一步，
对结构指纹却一点贡献都没有（结构指纹来自标注，不是实体统计）。

判据必须跟着档位走：
  · k3   只需要 K3 结构指纹 → 标注满 5 章
  · full 才需要 K1 实体卡片 → 跑过实体统计
以及「挡人的时候要说清缺的是哪一步」，否则作者会去点「构建知识库」，
点完仍然不可用，因为缺的根本不是那一下。

**自足**：合成章节 + 合成标注，不碰 `workspaces/`，不联网，不调模型。
    python tests/test_refs_levels.py
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

from server.services import Paths, _setting_refs_block, assist_refs  # noqa: E402
from workshop.ingest import ingest, save_ingest  # noqa: E402
from workshop.kb import build_kb  # noqa: E402
from workshop.samples import build_synthetic_novel  # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-refs-test-"))
_failures: list[str] = []

# 标注字段给全：K3 指纹是从这些量表聚合出来的，少一个就少一条指纹。
FIELDS = {
    "vol_no": 1,
    "chapter_summary": "一句话剧情梗概",
    "hook_strength": 3,
    "hook_type": "悬念",
    "emotion": 3,
    "conflict": 3,
    "info_release": 3,
    "mainline_progress": 3,
    "motive_strength": 3,
    "time_span": "即时",
    "subplot_count": 1,
    "perspective": "第三限知",
}


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def cleanup() -> None:
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


def make_work(name: str, *, annotated: int, build: bool = True) -> Path:
    """造一本已入库作品：合成正文 + 前 N 章标注，可选地构建知识库。

    一直不写 `50-entities`：这正是「跑了标注、没跑实体统计」那类作品的形态，
    也是这次要钉的场景。
    """
    source = TMP_ROOT / f"{name}.txt"
    source.write_text(build_synthetic_novel(), encoding="utf-8")
    work_dir = TMP_ROOT / "workspaces" / name
    save_ingest(ingest(source, work_name=name), work_dir / "00-ingest")

    manifest = json.loads(
        (work_dir / "00-ingest" / "manifest.json").read_text(encoding="utf-8-sig")
    )
    (work_dir / "10-annotations").mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(manifest.get("chapters") or [], start=1):
        if index > annotated:
            break
        chapter_id = str(item["id"])
        record = {
            "schema_version": "annotation-v1",
            "chapter_id": chapter_id,
            "vol_no": int(item.get("vol_no") or 1),
            "chapter_no": item.get("chapter_no"),
            "title": str(item.get("title") or ""),
            "status": "ok",
            "fields": dict(FIELDS),
            "provenance": {"reviewed_by": "test"},
        }
        (work_dir / "10-annotations" / f"{chapter_id}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )

    if build:
        build_kb(
            work_name=name,
            ingest_dir=work_dir / "00-ingest",
            annotations_dir=work_dir / "10-annotations",
            entities_path=work_dir / "50-entities" / "latest.json",
            out_dir=work_dir / "20-kb",
        )
    return work_dir


def main() -> int:
    paths = Paths(root=TMP_ROOT)

    print("只标了 5 章、没跑实体统计（k3 档必须可选）")
    make_work("只标5章", annotated=5)
    row = next((r for r in assist_refs(paths) if r["name"] == "只标5章"), {})
    check(row.get("k3_ready") is True, "结构指纹就绪 → k3 档可选")
    check(row.get("k1_ready") is False, "K1 未就绪照样如实报（没有假阳性）")
    check("实体统计" in (row.get("missing") or ""),
          f"缺因说到点子上（{row.get('missing')}）")

    block, missing = _setting_refs_block(paths, ["只标5章"], "k3")
    check(bool(block.strip()), f"k3 档真的拼出了素材（{len(block)} 字）")
    check(missing == [], "k3 档不再被「K1 空」拦下")

    block_full, missing_full = _setting_refs_block(paths, ["只标5章"], "full")
    check(not block_full.strip(), "full 档确实给不出人物素材（K1 空）")
    check(any("实体统计" in item for item in missing_full), "full 档点名缺实体统计")

    print()
    print("还没构建过知识库（两档都不可选，且要说清该做什么）")
    make_work("没建库", annotated=0, build=False)
    row = next((r for r in assist_refs(paths) if r["name"] == "没建库"), {})
    check(row.get("k3_ready") is False and row.get("k1_ready") is False, "两档都未就绪")
    check("还没构建过知识库" in (row.get("missing") or ""),
          f"缺因指名构建动作（{row.get('missing')}）")

    cleanup()
    print()
    if _failures:
        print(f"失败 {len(_failures)} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
