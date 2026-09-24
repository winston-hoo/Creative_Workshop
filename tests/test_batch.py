"""批量标注调度自检。

    python tests/test_batch.py

全部跑在本地模拟服务上：**不花钱、不需要密钥、不需要真实作品数据**。

重点验证的是批量相对单章新增的四件事，以及一个绕不开的取舍：

  1. 幂等：跑过且原文没变的章不再跑
  2. 失败不落盘：失败的章不能被幂等判定永久跳过
  3. 断点续跑：中断后重跑只补缺的
  4. 预算闸门：按实测 token 中止，不静默继续
  5. **并发与台账的顺序依赖**：并发 1 严格有序，并发 >1 会重复登记——
     这条断言把「并发会悄悄破坏伏笔链」这件事钉死在测试里
  6. 成本估算：没有单价就不给数字，有单价就给得出计算过程
"""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import AnnotateOptions  # noqa: E402
from workshop.batch import (  # noqa: E402
    BatchRunner,
    build_plan,
    estimate_run,
    load_chapter_tasks,
    price_bucket,
    resolve_token_coefficient,
    split_pending,
)
from workshop.config import ProviderConfig, WorkshopConfig  # noqa: E402
from workshop.ingest import ingest, save_ingest  # noqa: E402
from workshop.ledger import Ledger  # noqa: E402
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.primitives import load_primitives, load_work  # noqa: E402
from workshop.samples import VALID_ANNOTATION, build_synthetic_novel  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402

WORK_NAME = "批量自检"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


# ── 场地 ──────────────────────────────────────────────────

TMP = Path(tempfile.mkdtemp(prefix="workshop-batch-test-"))
WORK_DIR = TMP / "workspaces" / WORK_NAME
INGEST = WORK_DIR / "00-ingest"
ANNOTATIONS = WORK_DIR / "10-annotations"
LEDGER = WORK_DIR / "20-kb" / "k2-material" / "foreshadow-ledger.json"
RUNS = ANNOTATIONS / "_runs"


def prepare_workspace() -> None:
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    source = TMP / "source.txt"
    source.write_text(build_synthetic_novel(), encoding="utf-8")
    save_ingest(ingest(source, work_name=WORK_NAME), INGEST)
    (WORK_DIR / "work.yaml").write_text(
        'work: 批量自检\nprotagonist: "测试主角"\ncore_motive: "活下去"\nstyle_checks: []\n',
        encoding="utf-8",
    )


PEAK_HOURS = {
    "enabled": True,
    "peak_hours_bjt": ["09:00-12:00", "14:00-18:00"],
    "peak_days_bjt": ["MO", "TU", "WE", "TH", "FR"],
    "offpeak_multiplier": 0.5,
}


def mock_provider(*, pricing: dict | None = None, measured: dict | None = None) -> ProviderConfig:
    return ProviderConfig(
        id="mock",
        raw={
            "id": "mock",
            "name": "本地模拟",
            "enabled": True,
            "base_url": "",
            "auth": {"scheme": "bearer"},
            "pricing": {"time_of_day": PEAK_HOURS},
            "models": [
                {
                    "id": "mock-flash",
                    "role": "main",
                    "pricing": pricing or {},
                    "measured": measured or {},
                }
            ],
        },
    )


def cfg_with_budget(token_limit: int | None) -> WorkshopConfig:
    return WorkshopConfig(
        path=ROOT / "providers.yaml",
        raw={
            "settings": {
                "budget_gate_mode": "token",
                "budget": {"per_task_token_limit": token_limit, "on_exceed": "abort"},
            },
            "providers": [],
            "task_bindings": {"chapter_annotation": {"provider": "mock", "model": "mock-flash"}},
        },
    )


def make_plan(*, concurrency: int = 1, limit: int | None = None, token_limit: int | None = None,
              pricing: dict | None = None, measured: dict | None = None, ledger_enabled: bool = True,
              fresh: bool = False):
    # fresh=True 会重建作品目录。默认不重建——幂等与续跑这两类用例
    # 必须在**同一份数据**上连续跑两次，重建了就测不出跳过逻辑。
    if fresh:
        prepare_workspace()
    provider = mock_provider(pricing=pricing, measured=measured)
    cfg = cfg_with_budget(token_limit)
    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(WORK_DIR / "work.yaml")
    tasks = load_chapter_tasks(INGEST)
    return build_plan(
        cfg=cfg,
        provider=provider,
        model_id="mock-flash",
        primitives=primitives,
        work=work,
        tasks=tasks,
        annotations_dir=ANNOTATIONS,
        concurrency=concurrency,
        ledger_enabled=ledger_enabled,
        limit=limit,
    ), provider, primitives, work, cfg


def run_plan(plan, provider, primitives, work, *, responder=None, opts=None) -> tuple[object, Ledger | None]:
    state = MockState(responder=responder or base_responder, per_token_ms=0)
    with MockServer(state) as server:
        client = OpenAICompatProvider(base_url=server.base_url, api_key="mock-key", timeout_sec=15)
        ledger = Ledger.load(LEDGER) if plan.ledger_enabled else None
        if ledger is not None:
            ledger.work = work.name
        runner = BatchRunner(
            plan=plan,
            client=client,
            primitives=primitives,
            work=work,
            opts=opts or AnnotateOptions(max_attempts=1, timeout_sec=15),
            annotations_dir=ANNOTATIONS,
            ledger=ledger,
            ledger_path=LEDGER if ledger is not None else None,
            state_path=RUNS / "test.json",
            pricing=(provider.model("mock-flash") or {}).get("pricing") or {},
            price_bucket_now=plan.estimate.price_bucket,
        )
        return runner.run(), ledger


def base_responder(messages: list) -> dict:
    """第 1 章埋设，之后推进 F-001。用来验证跨章顺序。"""
    tail = " ".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
    payload = copy.deepcopy(VALID_ANNOTATION)
    if "F-001" in tail:
        payload["foreshadows"] = [{"动作": "推进", "编号": "F-001", "描述": "推进一次"}]
    else:
        payload["foreshadows"] = [{"动作": "埋设", "编号": "", "描述": "一道旧刀痕"}]
    return payload


# ── 用例 ──────────────────────────────────────────────────


def test_plan_is_free() -> None:
    print("计划不含任何模型调用")
    plan, *_ = make_plan(fresh=True)
    check(len(plan.pending) == 8, f"待跑 8 章（实际 {len(plan.pending)}）")
    check(plan.skipped == 0, "首次没有可跳过的")
    check(not ANNOTATIONS.exists(), "出计划不会创建标注目录")
    check(plan.concurrency == 1, "默认并发为 1")
    check(not plan.warnings or all("并发" not in w for w in plan.warnings), "并发 1 不该给并发告警")


def test_idempotent_skip() -> None:
    print("跑过且原文没变的章不再跑")
    plan, provider, primitives, work, _cfg = make_plan(fresh=True)
    state, _ledger = run_plan(plan, provider, primitives, work)
    check(state.committed == 8, f"第一次跑完 8 章（实际 {state.committed}）")
    check(state.failed == 0, "没有失败")

    again, *_ = make_plan()
    check(again.skipped == 8, f"第二次全部跳过（实际跳过 {again.skipped}）")
    check(len(again.pending) == 0, "没有待跑章节")

    # 改一个字就应该重跑那一章
    target = INGEST / "chapters" / "v001-c0002.md"
    text = target.read_text(encoding="utf-8")
    target.write_text(text + "\n加了一句话。\n", encoding="utf-8")
    third, *_ = make_plan()
    check(third.skipped == 7, f"改了一章后只跳过 7 章（实际 {third.skipped}）")
    check(len(third.pending) == 1, "被改的那一章重新进入待跑")


def test_ledger_order_with_concurrency_one() -> None:
    print("并发 1：伏笔台账严格有序")
    plan, provider, primitives, work, _cfg = make_plan(concurrency=1, fresh=True)
    state, ledger = run_plan(plan, provider, primitives, work)
    check(state.status == "done", f"任务完成（{state.status}）")
    check(len(ledger.items) == 1, f"只有 1 条伏笔（实际 {len(ledger.items)}）")
    actions = [e.action for e in ledger.items[0].events] if ledger.items else []
    check(actions[0] == "埋设", f"第一章是埋设（实际 {actions[0]}）")
    check(
        all(a == "推进" for a in actions[1:]),
        f"其余章都是推进（实际 {set(actions[1:])}）",
    )
    check(
        all(e.chapter_id.startswith(("v001", "v002")) for e in ledger.items[0].events),
        "事件链覆盖全部章节",
    )


def test_concurrency_breaks_ledger_order() -> None:
    """把「并发会破坏伏笔链」钉成一条断言。

    这不是预期行为，是可以接受的取舍，但它必须是**可见的**：
    默认并发 1，并发 >1 时计划里要给出明确警告。
    """
    print("并发 >1：台账上下文滞后，会被显式警告")
    plan, provider, primitives, work, _cfg = make_plan(concurrency=4, fresh=True)
    warned = any("并发" in w for w in plan.warnings)
    check(warned, "计划里给出了并发警告")

    state, ledger = run_plan(plan, provider, primitives, work)
    check(state.committed == 8, "8 章都跑完了")
    check(
        len(ledger.items) > 1,
        f"并发 4 下伏笔被重复登记成 {len(ledger.items)} 条（并发 1 时是 1 条）",
    )


def test_failure_is_not_persisted() -> None:
    """失败章不能落盘。

    落盘之后幂等判定会认为「已有标注」，于是它被永久跳过——
    一次网络抖动就变成一章永久缺数据，而且没有任何提示。
    """
    print("失败的章不落盘，重跑会重试")

    def bad_responder(messages: list) -> str:
        tail = " ".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
        if "第2章" in tail:
            return "这一章我就不输出 JSON 了。"
        return base_responder(messages)

    plan, provider, primitives, work, _cfg = make_plan(fresh=True)
    state, _ledger = run_plan(plan, provider, primitives, work, responder=bad_responder)
    check(state.failed == 1, f"有 1 章失败（实际 {state.failed}）")
    check(not (ANNOTATIONS / "v001-c0002.json").exists(), "失败的章没有落盘")
    check(bool(state.failures), "失败记进了运行记录")
    check(bool(state.ledger_gaps), "台账缺口被记录下来")

    again, *_ = make_plan()
    check(again.skipped == 7, f"只跳过成功的 7 章（实际 {again.skipped}）")
    check(len(again.pending) == 1, "失败章仍在待跑列表里")


def test_resume_after_partial_run() -> None:
    print("断点续跑：先跑 3 章，再跑剩下的")
    plan, provider, primitives, work, _cfg = make_plan(limit=3, fresh=True)
    state, _ledger = run_plan(plan, provider, primitives, work)
    check(state.committed == 3, "第一次只跑 3 章")

    rest, *_ = make_plan()
    check(rest.skipped == 3, f"已跑 3 章被跳过（实际 {rest.skipped}）")
    check(len(rest.pending) == 5, f"还剩 5 章（实际 {len(rest.pending)}）")

    state2, _ledger2 = run_plan(rest, provider, primitives, work)
    check(state2.committed == 5, "第二次补完剩下的 5 章")

    final, *_ = make_plan()
    check(len(final.pending) == 0, "最终没有待跑章节")


def test_budget_gate_aborts() -> None:
    print("预算闸门按实测 token 中止")
    plan, provider, primitives, work, _cfg = make_plan(token_limit=100, fresh=True)
    check(plan.over_budget is True, "估算阶段就标记为超预算（CLI/接口会拒绝启动）")
    state, _ledger = run_plan(plan, provider, primitives, work)
    check(state.status == "aborted", f"被闸门中止（实际 {state.status}）")
    check(state.committed < 8, f"没有跑完 8 章（实际跑了 {state.committed}）")
    check("预算" in (state.message or ""), "中止原因写清楚了")

    # 中止之后剩下的还能接着跑
    rest, *_ = make_plan(token_limit=100)
    check(len(rest.pending) == 8 - state.committed, "中止后剩下的章节仍在待跑列表")


def test_fatal_error_stops_early() -> None:
    """配置类错误（密钥无效等）要立刻停，不能把后面每一章都刷一遍。

    实测：密钥失效时，批量任务拿同一个 401 把 20 章各重试 3 次、白跑 8 秒。
    照这个速度跑完 933 章要白等十几分钟，而且日志里堆满同一个错误，
    真正的原因反而被埋掉。
    """
    print("配置类错误立刻中止，不刷完剩余章节")
    plan, provider, primitives, work, _cfg = make_plan(fresh=True)

    state = MockState(force_status=401, per_token_ms=0)
    with MockServer(state) as server:
        client = OpenAICompatProvider(base_url=server.base_url, api_key="bad", timeout_sec=15)
        ledger = Ledger.load(LEDGER)
        ledger.work = work.name
        runner = BatchRunner(
            plan=plan,
            client=client,
            primitives=primitives,
            work=work,
            opts=AnnotateOptions(max_attempts=1, timeout_sec=15),
            annotations_dir=ANNOTATIONS,
            ledger=ledger,
            ledger_path=LEDGER,
        )
        result = runner.run()

    check(result.status == "aborted", f"任务被中止（实际 {result.status}）")
    check(result.committed == 1, f"只跑了 1 章就停（实际 {result.committed}）")
    check(result.failed == 1, "那一章记为失败")
    check("auth_failed" in result.error_kinds, f"错误按类别汇总（{result.error_kinds}）")
    check(bool(result.aborted_reason), "记下了中止原因")
    check("密钥" in result.aborted_reason, f"原因里带处置建议：{result.aborted_reason}")
    check(not (ANNOTATIONS / "v001-c0001.json").exists(), "失败的章没有落盘")

    payload = result.to_dict()
    hints = payload.get("error_hints") or []
    check(bool(hints) and hints[0]["kind"] == "auth_failed", "状态里带了错误说明表")
    check(hints[0]["fatal"] is True, "标记为致命错误，界面可以据此显示醒目提示")


def test_cost_estimate_without_price() -> None:
    print("没有单价就不给费用数字")
    plan, *_ = make_plan(fresh=True)
    check(plan.estimate.cost_cny is None, "费用为 None")
    check("无法计算" in plan.estimate.price_note, "说明写清了算不出")
    check(plan.estimate.total_tokens > 0, "token 仍然能估")


def test_cost_estimate_with_price() -> None:
    print("有单价时给出费用与计算过程")
    pricing = {
        "input_per_mtok_peak": 2.0,
        "input_per_mtok_offpeak": 1.0,
        "output_per_mtok_peak": 8.0,
        "output_per_mtok_offpeak": 4.0,
    }
    plan, provider, *_rest = make_plan(pricing=pricing, fresh=True)
    check(plan.estimate.cost_cny is not None, "给出了费用估算")
    check(plan.estimate.price_bucket in ("peak", "offpeak"), f"带计费时段（{plan.estimate.price_bucket}）")
    breakdown = plan.estimate.breakdown
    check(breakdown.get("输入单价") is not None, "计算过程里有输入单价")
    check(breakdown.get("中文换算系数", "").find("tokens/字") > 0, "换算系数写进了计算过程")


def test_measured_coefficient_preferred() -> None:
    print("实测换算系数优先于兜底值")
    provider = mock_provider(measured={"tokens_per_cjk_char": 0.968})
    coeff, basis = resolve_token_coefficient(provider.model("mock-flash"), provider)
    check(coeff == 0.968, f"用了实测值（{coeff}）")
    check(basis == "measured", f"来源标记为 measured（{basis}）")

    plain = mock_provider()
    coeff2, basis2 = resolve_token_coefficient(plain.model("mock-flash"), plain)
    check(basis2 == "fallback", "没有实测值时才用兜底")
    check(coeff2 == 1.0, "兜底值是 1.0，且会被显式警告")


def test_price_bucket_peak_offpeak() -> None:
    print("峰谷判定")
    provider = ProviderConfig(
        id="p",
        raw={
            "pricing": {
                "time_of_day": {
                    "enabled": True,
                    "peak_hours_bjt": ["09:00-12:00", "14:00-18:00"],
                    "peak_days_bjt": ["MO", "TU", "WE", "TH", "FR"],
                    "offpeak_multiplier": 0.5,
                }
            }
        },
    )
    # 2026-09-22 是周二 → 工作日
    check(
        price_bucket(provider, datetime(2026, 9, 22, 2, 30, tzinfo=timezone.utc)) == "peak",
        "周二 10:30 BJT（UTC 02:30）落在 09:00-12:00，判为高峰",
    )
    check(
        price_bucket(provider, datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc)) == "offpeak",
        "周二 13:00 BJT 在两个高峰段之间，判为空闲",
    )
    # 2026-09-26 是周六 → 全天空闲
    check(
        price_bucket(provider, datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)) == "offpeak",
        "周六全天空闲（这也是「全量排在周末」能省一半的依据）",
    )


def test_split_pending_missing_chapter_file() -> None:
    print("章节文件缺失不能被静默跳过")
    prepare_workspace()
    tasks = load_chapter_tasks(INGEST)
    (INGEST / "chapters" / "v001-c0003.md").unlink()
    pending, done = split_pending(tasks, ANNOTATIONS)
    ids = {t.chapter_id for t in pending}
    check("v001-c0003" in ids, "缺文件的章进入待跑（而不是被跳过）")
    check(len(done) == 0, "没有可跳过的")


def test_estimate_zero_chapters() -> None:
    print("没有待跑章节时估算为 0")
    provider = mock_provider()
    est = estimate_run(
        provider=provider,
        model_cfg=provider.model("mock-flash") or {},
        tasks=[],
        prefix_chars=1000,
        tokens_per_cjk_char=0.968,
        coefficient_basis="measured",
    )
    check(est.total_tokens == 0, "token 合计为 0")
    check(est.chapters == 0, "章数为 0")


def main() -> int:
    print("=" * 58)
    print("批量标注调度自检")
    print("=" * 58)

    for fn in (
        test_plan_is_free,
        test_idempotent_skip,
        test_ledger_order_with_concurrency_one,
        test_concurrency_breaks_ledger_order,
        test_failure_is_not_persisted,
        test_resume_after_partial_run,
        test_budget_gate_aborts,
        test_fatal_error_stops_early,
        test_cost_estimate_without_price,
        test_cost_estimate_with_price,
        test_measured_coefficient_preferred,
        test_price_bucket_peak_offpeak,
        test_split_pending_missing_chapter_file,
        test_estimate_zero_chapters,
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
