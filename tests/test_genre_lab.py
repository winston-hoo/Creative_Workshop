"""题材库（L2/M6）、对比分析（M8）、融合器（M5）接口自检。

    python tests/test_genre_lab.py

验证：

  1. 作品登记题材（genres/_index.yaml）
  2. M6 聚合：同题材 <3 部作品 → 「样本不足」不产规则；≥3 部 → 产出规则
  3. M8 对比：同题材 / 跨题材 / 偏离度三份报告
  4. M5 融合：用自有素材生成骨架，元素可追溯到来源作品
  5. 全部纯脚本，不调模型

为了测 M6/M8，需要「多部作品有 L3 指纹」。测试直接手工构造
workspaces/{作品}/20-kb/k3-fingerprint.json 与 k1-entities.json，
不跑知识库（那样太慢且依赖标注）。
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

app = server_main.app

TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-genrelab-test-"))
server_main.PATHS = services.Paths(root=TMP_ROOT)
WS = TMP_ROOT / "workspaces"

for _name in ("providers.yaml", "primitives.yaml"):
    shutil.copy(ROOT / _name, TMP_ROOT / _name)

client = TestClient(app)

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def make_work(work: str, genre: str, *, hook: float, emotion: float, conflict: float,
              characters: list[dict] | None = None) -> None:
    """构造一个带 L3 指纹与 K1 实体的工作区。"""
    dir_ = WS / work / "20-kb"
    dir_.mkdir(parents=True, exist_ok=True)
    fp = {
        "available": True,
        "note": "",
        "items": [
            {"metric": "hook_strength", "rule": "均值", "statistic": {"mean": hook}, "sample": 10, "confidence": "高"},
            {"metric": "emotion", "rule": "均值", "statistic": {"mean": emotion}, "sample": 10, "confidence": "高"},
            {"metric": "conflict", "rule": "均值", "statistic": {"mean": conflict}, "sample": 10, "confidence": "高"},
            {"metric": "hook_type", "rule": "分布", "statistic": {"dominant": "悬念", "distribution": {"悬念": 8}}, "sample": 10, "confidence": "高"},
        ],
    }
    (dir_ / "k3-fingerprint.json").write_text(json.dumps(fp, ensure_ascii=False), encoding="utf-8")
    k1 = {
        "available": True,
        "counts": {"characters": 2, "locations": 1, "factions": 1, "abilities": 1},
        "characters": characters or [
            {"name": f"{work}主角", "role": "主角", "identity": "主角身份"},
            {"name": f"{work}反派", "role": "反派", "identity": "反派身份"},
        ],
        "locations": [{"name": f"{work}城", "note": "主城"}],
        "factions": [{"name": f"{work}门", "stance": "守序"}],
        "abilities": [{"name": f"{work}术", "effect": "特殊能力"}],
        "relations": [],
    }
    (dir_ / "k1-entities.json").write_text(json.dumps(k1, ensure_ascii=False), encoding="utf-8")

    # 登记题材
    client.put("/api/genres/assign", json={"work": work, "genre": genre})


def test_assign_genre() -> None:
    print("作品登记题材")
    make_work("作品A", "玄幻", hook=3.8, emotion=3.5, conflict=3.0)
    data = client.get("/api/genres").json()
    check(data["mapping"].get("作品A") == "玄幻", "映射里有登记")
    check(any(g["genre"] == "玄幻" for g in data["genres"]), "题材列表里有玄幻")
    # 移除
    client.put("/api/genres/assign", json={"work": "作品A", "genre": ""})
    data = client.get("/api/genres").json()
    check("作品A" not in data["mapping"], "空串移除登记")


def test_aggregate_insufficient() -> None:
    print("样本不足不产规则")
    make_work("作品A", "悬疑", hook=3.8, emotion=3.5, conflict=3.0)
    r = client.post("/api/genres/悬疑/aggregate")
    check(r.status_code == 200, "可聚合")
    body = r.json()
    check(len(body["rules"]) == 0, "作品数 <3 不产出规则")
    check("样本不足" in body["note"], "没有静默成功，而是显式说明")


def test_aggregate_sufficient() -> None:
    print("样本足够产出规则")
    for i, hook in enumerate((3.8, 4.0, 4.2, 3.9)):
        make_work(f"玄{i+1}", "玄幻", hook=hook, emotion=3.5, conflict=3.0)
    r = client.post("/api/genres/玄幻/aggregate")
    body = r.json()
    check(len(body["rules"]) >= 1, "作品数 ≥3 产出规则")
    hook_rule = next((x for x in body["rules"] if x["id"] == "L2-玄幻-hook_strength"), None)
    check(hook_rule is not None, "有钩子强度聚合规则")
    if hook_rule:
        check("均值" in hook_rule["rule"] or "3." in hook_rule["rule"], "规则带均值")
        check(isinstance(hook_rule["hit_rate"], (int, float)), "有命中率")
    check(body["works"] == ["玄1", "玄2", "玄3", "玄4"], "聚合的作品列表正确")
    check((TMP_ROOT / "genres" / "玄幻" / "rules.json").exists(), "规则落盘")


def test_compare_reports() -> None:
    print("M8 对比分析")
    make_work("玄1", "玄幻", hook=3.8, emotion=3.5, conflict=2.0)
    make_work("玄2", "玄幻", hook=4.0, emotion=3.5, conflict=3.0)
    make_work("玄3", "玄幻", hook=4.2, emotion=4.0, conflict=3.0)
    make_work("都1", "都市", hook=3.0, emotion=4.5, conflict=2.0)
    make_work("都2", "都市", hook=2.8, emotion=4.2, conflict=2.5)
    make_work("都3", "都市", hook=3.2, emotion=4.0, conflict=2.0)

    client.post("/api/genres/玄幻/aggregate")
    client.post("/api/genres/都市/aggregate")

    r = client.post("/api/compare/build", json={"work": "玄1", "genre": "玄幻"})
    check(r.status_code == 200, f"对比构建成功（{r.status_code}）")
    body = r.json()

    check(body["same_genre"].get("genre") == "玄幻", "同题材报告题材正确")
    check(len(body["same_genre"]["rows"]) >= 3, f"同题材列出多部作品（{len(body['same_genre']['rows'])}）")
    check(body["same_genre"]["rows"][0]["means"].get("hook_strength") is not None, "同题材带各指标均值")

    cg = body["cross_genre"]["comparisons"]
    check(len(cg) >= 1, "跨题材有对比项")
    check(any(c["metric"] == "hook_strength" for c in cg), "钩子强度在跨题材对比里")
    # 玄 vs 都 的情绪值跨度应当较大（3.5 vs 4.2 左右）
    emo = next((c for c in cg if c["metric"] == "emotion"), None)
    if emo:
        check(len(emo["per_genre"]) >= 2, "情绪值跨题材分布有 2 个题材")

    dv = body["deviation"]
    check(dv["work"] == "玄1", "偏离度基准作品正确")
    check(all(isinstance(d["delta"], float) or isinstance(d["delta"], int) for d in dv["deviations"]), "偏离度带数值")

    check((TMP_ROOT / "compare" / "latest.json").exists(), "对比报告落盘")


def test_fusion() -> None:
    print("M5 融合器")
    make_work("玄1", "玄幻", hook=3.8, emotion=3.5, conflict=3.0,
              characters=[{"name": "玄1主角", "role": "主角", "identity": "剑客"},
                          {"name": "玄1对手", "role": "反派", "identity": "魔王"}])
    make_work("都1", "都市", hook=3.0, emotion=4.0, conflict=2.0,
              characters=[{"name": "都1主角", "role": "主角", "identity": "记者"},
                          {"name": "都1拍档", "role": "配角", "identity": "侦探"}])

    r = client.post("/api/fusion/generate", json={"source_works": ["玄1", "都1"], "seed": 1})
    check(r.status_code == 200, f"融合成功（{r.status_code}）")
    body = r.json()
    check(set(body["sources"]) == {"玄1", "都1"}, "素材来源列出两部作品")
    check(len(body["cast"]) >= 2, "骨架有人物")
    for c in body["cast"]:
        check(bool(c.get("source")), f"人物 {c['name']} 带来源（可追溯）")
    check(len(body["world"]) >= 3, "世界元素有地点/势力/能力")
    for w in body["world"]:
        check(bool(w.get("source")), f"世界元素 {w['name']} 带来源")
    check(len(body["beats"]) >= 10, "有三幕节拍")
    check((TMP_ROOT / "workspaces" / "_fusion" / "latest.json").exists(), "骨架落盘")

    # 无素材时给明确错误
    r = client.post("/api/fusion/generate", json={"source_works": ["不存在作品"]})
    check(r.status_code == 400, "无素材时拒绝并说明")


def main() -> int:
    print("=" * 58)
    print("题材库 · 对比 · 融合 接口自检")
    print("=" * 58)

    for fn in (
        test_assign_genre,
        test_aggregate_insufficient,
        test_aggregate_sufficient,
        test_compare_reports,
        test_fusion,
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