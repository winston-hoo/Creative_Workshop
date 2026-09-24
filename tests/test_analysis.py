"""结构体检报告自检。

    python tests/test_analysis.py

本地聚合，不调模型。跑在合成作品 + 本地模拟标注上。

核心验证的是一条硬要求：**零标注也要能出报告**，而且「没有数据」必须明写，
不能用空数组假装一切正常——那正是本项目反复栽跟头的静默失效。
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import AnnotateOptions  # noqa: E402
from workshop.analysis import build_report, render_markdown, save_report  # noqa: E402
from workshop.batch import BatchRunner, build_plan, load_chapter_tasks  # noqa: E402
from workshop.config import ProviderConfig, WorkshopConfig  # noqa: E402
from workshop.ingest import ingest, save_ingest  # noqa: E402
from workshop.ledger import Ledger  # noqa: E402
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.primitives import load_primitives, load_work  # noqa: E402
from workshop.samples import VALID_ANNOTATION, build_synthetic_novel  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402

WORK_NAME = "报告自检"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="workshop-analysis-test-"))
WORK_DIR = TMP / "workspaces" / WORK_NAME
INGEST = WORK_DIR / "00-ingest"
ANNOTATIONS = WORK_DIR / "10-annotations"
LEDGER = WORK_DIR / "20-kb" / "k2-material" / "foreshadow-ledger.json"
REPORTS = WORK_DIR / "30-reports"


def prepare() -> None:
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    source = TMP / "source.txt"
    source.write_text(build_synthetic_novel(), encoding="utf-8")
    save_ingest(ingest(source, work_name=WORK_NAME), INGEST)
    (WORK_DIR / "work.yaml").write_text(
        'work: 报告自检\nprotagonist: "测试主角"\ncore_motive: "活下去"\nstyle_checks: []\n',
        encoding="utf-8",
    )


def annotate_all() -> None:
    """跑一遍批量标注，产出报告要用的数据。"""
    provider = ProviderConfig(
        id="mock",
        raw={
            "id": "mock",
            "models": [{"id": "mock-flash", "role": "main", "pricing": {}, "measured": {}}],
            "auth": {"scheme": "bearer"},
        },
    )
    cfg = WorkshopConfig(
        path=ROOT / "providers.yaml",
        raw={"settings": {"budget": {"per_task_token_limit": 10_000_000}}, "task_bindings": {}},
    )
    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(WORK_DIR / "work.yaml")
    plan = build_plan(
        cfg=cfg,
        provider=provider,
        model_id="mock-flash",
        primitives=primitives,
        work=work,
        tasks=load_chapter_tasks(INGEST),
        annotations_dir=ANNOTATIONS,
        concurrency=1,
    )

    def responder(messages: list) -> dict:
        tail = " ".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
        payload = copy.deepcopy(VALID_ANNOTATION)
        payload["foreshadows"] = (
            [{"动作": "推进", "编号": "F-001", "描述": "推进一次"}]
            if "F-001" in tail
            else [{"动作": "埋设", "编号": "", "描述": "一道旧刀痕"}]
        )
        return payload

    with MockServer(MockState(responder=responder, per_token_ms=0)) as server:
        client = OpenAICompatProvider(base_url=server.base_url, api_key="mock-key", timeout_sec=15)
        ledger = Ledger.load(LEDGER)
        ledger.work = work.name
        BatchRunner(
            plan=plan,
            client=client,
            primitives=primitives,
            work=work,
            opts=AnnotateOptions(max_attempts=1, timeout_sec=15),
            annotations_dir=ANNOTATIONS,
            ledger=ledger,
            ledger_path=LEDGER,
        ).run()


def report(*, stale_threshold: int = 30):
    return build_report(
        work_name=WORK_NAME,
        ingest_dir=INGEST,
        annotations_dir=ANNOTATIONS,
        ledger_path=LEDGER,
        stale_threshold=stale_threshold,
    )


# ── 用例 ──────────────────────────────────────────────────


def test_report_without_annotations() -> None:
    print("零标注也能出报告")
    prepare()
    r = report()
    s = r.sections

    check(r.coverage["annotated"] == 0, "标注数为 0")
    check(bool(r.coverage["note"]), "明确写了「还没有标注」")
    check(r.coverage["script_track_available"] is True, "脚本轨可用")
    check(r.coverage["model_track_available"] is False, "模型轨标记为不可用")

    check(s["length"]["available"] is True, "章节长度有数据")
    check(len(s["length"]["points"]) == 8, "长度曲线 8 个点")
    check(s["volumes"]["available"] is True, "分卷统计有数据")

    for key in ("emotion_conflict", "motive", "hook_types", "payoffs"):
        check(s[key]["available"] is False, f"{key} 标记为无数据")
        check(bool(s[key].get("reason")), f"{key} 写了无数据的原因，而不是给空数组")

    check(s["foreshadow"]["available"] is False, "伏笔小节标记为无数据")
    check("台账" in s["foreshadow"].get("reason", ""), "说明是还没生成台账，而不是「没有伏笔」")


def test_report_with_annotations() -> None:
    print("有标注时报告完整")
    prepare()
    annotate_all()
    r = report()
    s = r.sections

    check(r.coverage["annotated"] == 8, f"覆盖 8 章（实际 {r.coverage['annotated']}）")
    check(r.coverage["ratio"] == 1.0, "覆盖率 100%")
    check(not r.coverage["note"], "有标注时不再显示提示")

    check(s["emotion_conflict"]["available"] is True, "情绪/冲突有数据")
    check(
        len(s["emotion_conflict"]["series"]["emotion"]) == 8,
        "情绪序列 8 个点",
    )
    check(s["hook_types"]["available"] is True, "钩子类型有数据")
    check(sum(s["hook_types"]["counts"].values()) == 8, "钩子分布合计 8 章")
    check(s["payoffs"]["available"] is True, "爽点有数据")
    check(s["motive"]["available"] is True, "动机线有数据")
    check(s["review"]["available"] is True, "复核小节有数据")


def test_foreshadow_section() -> None:
    print("伏笔台账与疑似断点")
    prepare()
    annotate_all()

    r = report(stale_threshold=30)
    fs = r.sections["foreshadow"]
    check(fs["available"] is True, "台账可用")
    check(fs["total"] == 1, f"1 条伏笔（实际 {fs['total']}）")
    check(fs["open"] == 1, "未回收 1 条")
    check(fs["stale"] == 0, "阈值 30 章时不是断点")
    check(fs["items"][0]["events"] == 8, "事件链 8 条")

    # 阈值降到 2 章，同一份数据就该被判成疑似断点
    r2 = report(stale_threshold=2)
    check(r2.sections["foreshadow"]["stale"] >= 1, "阈值 2 章时识别为疑似断点")
    check(
        any(i["is_stale"] for i in r2.sections["foreshadow"]["items"]),
        "条目上带了断点标记",
    )


def test_style_violations_aggregation() -> None:
    print("风格违规聚合")
    prepare()
    annotate_all()
    # 手工塞一条违规，验证按规则聚合与按章排序
    record = json.loads((ANNOTATIONS / "v001-c0001.json").read_text(encoding="utf-8"))
    record["fields"]["style_violations"] = [
        {"rule_id": "STYLE-001", "desc": "人称漂移", "count": 2},
        {"rule_id": "STYLE-002", "desc": "禁用字符", "count": 5},
    ]
    (ANNOTATIONS / "v001-c0001.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    style = report().sections["style_violations"]
    check(style["available"] is True, "有数据")
    check(style["chapters_with_violations"] == 1, "涉及 1 章")
    rules = {x["rule_id"]: x["count"] for x in style["by_rule"]}
    check(rules.get("STYLE-002") == 5, f"按规则聚合正确（{rules}）")
    check(style["top_chapters"][0]["count"] == 7, "该章合计 7 处")


def test_markdown_and_persistence() -> None:
    print("Markdown 渲染与落盘")
    prepare()
    annotate_all()
    r = report()
    md = render_markdown(r)

    for title in ("① 章节长度", "② 情绪与冲突", "④ 伏笔追踪", "⑧ 自洽性检查", "⑨ 待复核"):
        check(title in md, f"包含小节「{title}」")

    paths = save_report(r, REPORTS)
    check(paths["json"].exists(), "JSON 落盘")
    check(paths["markdown"].exists(), "Markdown 落盘")
    check(paths["latest_json"].exists(), "latest.json 存在（界面读它）")
    saved = json.loads(paths["latest_json"].read_text(encoding="utf-8"))
    check(saved["work"] == WORK_NAME, "落盘内容正确")


def main() -> int:
    print("=" * 58)
    print("结构体检报告自检")
    print("=" * 58)

    for fn in (
        test_report_without_annotations,
        test_report_with_annotations,
        test_foreshadow_section,
        test_style_violations_aggregation,
        test_markdown_and_persistence,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(False, f"{fn.__name__} 抛出异常：{type(exc).__name__}: {exc}")
        print()

    shutil.rmtree(TMP, ignore_errors=True)
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
