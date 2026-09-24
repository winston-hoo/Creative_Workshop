"""不变式检查：验证两条硬约束真的成立。

不需要测试框架，直接跑：
    python tests/test_invariants.py

检查的是最要命的两件事——密钥不泄漏、错误分类正确。
这两条一旦破了，后果不是「功能不对」而是「安全事故」。
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.errors import (  # noqa: E402
    NON_BLOCKING,
    ErrorKind,
    ProbeError,
    classify_http_status,
)
from workshop.report import save_report  # noqa: E402
from workshop.secrets import RedactingFormatter, SecretStore, redact  # noqa: E402

FAKE_KEY = "sk-abcdefghijklmnop1234567890"
MASK = "sk-****"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


# ── 密钥脱敏 ────────────────────────────────────────────────


def test_redaction() -> None:
    print("密钥脱敏")

    check(FAKE_KEY not in redact(f"Authorization: Bearer {FAKE_KEY}"), "已知密钥被替换")
    check(FAKE_KEY not in redact(f"key={FAKE_KEY}", [FAKE_KEY]), "按已知值替换")
    check(MASK in redact(f"Authorization: Bearer {FAKE_KEY}"), "替换为掩码")

    # 没见过但形似密钥的串也要拦住
    unknown = "sk-zzzzzzzzzzzzzzzz9999"
    check(unknown not in redact(f"header sk-{unknown[3:]}"), "形似密钥的未知串被拦截")

    check(redact("这段文字里没有密钥") == "这段文字里没有密钥", "无密钥文本保持不变")


def test_logging_formatter() -> None:
    print("日志出口脱敏")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s", [FAKE_KEY]))
    logger = logging.getLogger("invariant-test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)

    logger.error("request failed, Authorization: Bearer %s", FAKE_KEY)
    output = stream.getvalue()

    check(FAKE_KEY not in output, "日志里不出现密钥明文")
    check(MASK in output, "日志里出现掩码")


def test_report_never_contains_key() -> None:
    print("报告落盘前脱敏（纵深防御）")

    report = {
        "report_id": "invariant-check",
        "provider": "mock",
        "model": "mock",
        "checked_at": "2026-09-22T00:00:00+08:00",
        "results": {"reachable": True},
        "errors": [
            {
                "step": "P2",
                # 模拟上游错误体里夹带了密钥——这是实践中最常漏的一处泄漏点
                "detail": f"upstream echoed header Authorization: Bearer {FAKE_KEY}",
                "hint": "鉴权失败，检查密钥",
            }
        ],
    }

    with tempfile.TemporaryDirectory() as tmp:
        paths = save_report(report, tmp, secrets=[FAKE_KEY])
        yaml_text = paths["yaml"].read_text(encoding="utf-8")
        md_text = paths["markdown"].read_text(encoding="utf-8")
        # 索引也要检查，它也承载了报告字段
        index_text = (Path(tmp) / "index.json").read_text(encoding="utf-8")

    check(FAKE_KEY not in yaml_text, "YAML 报告不含密钥")
    check(FAKE_KEY not in md_text, "Markdown 报告不含密钥")
    check(MASK in yaml_text, "YAML 里出现掩码（说明替换确实发生）")
    check(FAKE_KEY not in index_text, "索引不含密钥")


def test_secret_store_prefers_env() -> None:
    print("密钥来源优先级")

    import os

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "secrets.json").write_text(
            '{"TEST_KEY_REF": "from-file-value-1234"}', encoding="utf-8"
        )
        store = SecretStore(tmp)

        check(store.get("TEST_KEY_REF") == "from-file-value-1234", "可从文件读取")
        check(store.source_of("TEST_KEY_REF") == "file", "来源标记为 file")

        os.environ["TEST_KEY_REF"] = "from-env-value-5678"
        try:
            store2 = SecretStore(tmp)
            check(store2.get("TEST_KEY_REF") == "from-env-value-5678", "环境变量优先于文件")
            check(store2.source_of("TEST_KEY_REF") == "env", "来源标记为 env")
        finally:
            os.environ.pop("TEST_KEY_REF", None)

        check(SecretStore(tmp).get("NOT_EXIST_REF") is None, "取不到时返回 None 而不是报错")


# ── 错误分类 ────────────────────────────────────────────────


def test_error_classification() -> None:
    print("错误分类")

    cases = [
        (401, ErrorKind.AUTH_FAILED),
        (403, ErrorKind.AUTH_FAILED),
        (404, ErrorKind.ENDPOINT_NOT_FOUND),
        (429, ErrorKind.RATE_LIMITED),
        (500, ErrorKind.SERVER_ERROR),
        (503, ErrorKind.SERVER_ERROR),
        (418, ErrorKind.BAD_REQUEST),
    ]
    for status, expect in cases:
        actual = classify_http_status(status)
        check(actual is expect, f"HTTP {status} → {expect.value}（实际 {actual.value}）")

    kind = classify_http_status(400, '{"error":{"message":"model foo not found"}}')
    check(kind is ErrorKind.MODEL_NOT_FOUND, "400 且报文提到 model → 模型名错")

    kind = classify_http_status(404, '{"error":{"message":"model bar not found"}}')
    check(kind is ErrorKind.MODEL_NOT_FOUND, "404 且报文提到 model → 模型名错")


def test_non_blocking_semantics() -> None:
    print("非阻断错误的语义")

    check(
        ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED in NON_BLOCKING,
        "不支持结构化输出属非阻断（走降级即可）",
    )
    check(ErrorKind.USAGE_MISSING in NON_BLOCKING, "用量缺失属非阻断（费用改估算）")
    check(ErrorKind.AUTH_FAILED not in NON_BLOCKING, "鉴权失败属阻断")

    err = ProbeError(step="P3", kind=ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED)
    check(not err.blocking, "非阻断错误 blocking 为 False")
    check(bool(err.hint), "错误自带面向用户的提示文案")

    d = err.to_dict()
    check(d["kind"] == "structured_output_unsupported", "序列化 kind 为字符串")
    check(d["blocking"] is False, "序列化 blocking 正确")


def test_comparison_attribution() -> None:
    """对比归因。真实事故：关掉思考模式带来 7 倍提速，却被归因成「服务商负载变化」。"""
    print("对比归因")

    from workshop.report import compare_reports

    def make(thinking: str, speed: float, prompt_hash: str = "same") -> dict:
        return {
            "config_snapshot": {
                "prompt_hash": prompt_hash,
                "thinking": thinking,
                "streaming": True,
                "max_tokens": 8,
                "text_chars": 1000,
            },
            "time_bucket": "peak",
            "benchmark": {"output_tokens_per_sec": {"median": speed}},
        }

    # 配置变过：应提示先归因于配置，而不是服务
    notes = compare_reports(make("enabled", 12.5), make("disabled", 95.7))
    titles = " ".join(n.get("title", "") for n in notes)
    details = " ".join(n.get("detail", "") for n in notes)
    check(
        any(n.get("level") == "info" and "配置" in str(n.get("title")) for n in notes),
        "配置不同时给出配置变更说明",
    )
    check("思考模式" in details, "点明具体是哪一项配置变了")
    check("enabled → disabled" in details, "说明文案里带上具体的变化值")
    speed_note = next((n for n in notes if n.get("metric") == "输出速度"), None)
    check(speed_note is not None, "速度变化仍会被报出来")
    if speed_note:
        check("配置" in str(speed_note.get("detail")), "速度变化的归因指向配置而非服务")

    # 配置相同：应给出服务层面的建议
    notes_same = compare_reports(make("disabled", 12.5), make("disabled", 95.7))
    check(
        not any("配置" in str(n.get("title")) for n in notes_same),
        "配置相同时不添加配置变更说明",
    )
    same_note = next((n for n in notes_same if n.get("metric") == "输出速度"), None)
    if same_note:
        check("配置" not in str(same_note.get("detail")), "配置相同时归因回到服务层面")

    # 配方不同：直接拒绝对比
    notes_bad = compare_reports(
        make("disabled", 95.7, "hash-a"), make("disabled", 95.7, "hash-b")
    )
    check(
        len(notes_bad) == 1 and notes_bad[0].get("level") == "incomparable",
        "配方不同时只返回不可比说明",
    )

    # 噪音抑制：变化幅度不够大时不出告警
    notes_noise = compare_reports(make("disabled", 95.0), make("disabled", 96.0))
    check(
        not any(n.get("metric") for n in notes_noise),
        "微小变化不产生指标告警（绝对变化未达阈值）",
    )


def main() -> int:
    print("=" * 58)
    print("不变式检查")
    print("=" * 58)

    for fn in (
        test_redaction,
        test_logging_formatter,
        test_report_never_contains_key,
        test_secret_store_prefers_env,
        test_error_classification,
        test_non_blocking_semantics,
        test_comparison_attribution,
    ):
        fn()
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
