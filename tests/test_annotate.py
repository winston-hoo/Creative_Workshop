"""单章标注链路的自检。

不需要密钥、不需要作品原文、不产生任何真实调用。
直接跑：python tests/test_annotate.py

它验证的是链路的**判定逻辑**，不只是「能不能跑通」：
  · 脚本轨字段算得对不对
  · 风格违规检测有没有漏
  · 模型轨字段有没有正确合并与校验
  · 校验失败时**会不会静默填默认值**（这是最关键的一条）
  · 固定前缀是否逐字稳定（缓存的前提）
  · 幂等是否成立
  · 落盘前是否脱敏
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import (  # noqa: E402
    AnnotateOptions,
    annotate_chapter,
    is_up_to_date,
    load_annotation,
    save_annotation,
    text_sha256,
)
from workshop.llm import OpenAICompatProvider  # noqa: E402
from workshop.primitives import WorkConfig, load_primitives, load_work  # noqa: E402
from workshop.prompts import build_messages, build_stable_prefix, build_variable_tail  # noqa: E402
from workshop.selftest import MockServer, MockState  # noqa: E402

from workshop.samples import (  # noqa: E402
    BAD_MODEL_OUTPUT,
    INVALID_ENUM_ANNOTATION,
    SAMPLE_CHAPTER,
    VALID_ANNOTATION,
)
from workshop.script_fields import compute_script_fields  # noqa: E402

FAKE_KEY = "sk-abcdefghijklmnop1234567890"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def run_annotate(state: MockState, text: str = SAMPLE_CHAPTER, **overrides):
    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(ROOT / "work.yaml")
    opts = AnnotateOptions(max_attempts=overrides.pop("max_attempts", 3), timeout_sec=15)
    with MockServer(state) as server:
        client = OpenAICompatProvider(
            base_url=server.base_url, api_key="mock-key", timeout_sec=15
        )
        return annotate_chapter(
            client=client,
            primitives=primitives,
            work=work,
            chapter_id="v001-c0001",
            chapter_no=1,
            vol_no=1,
            title="夜行",
            text=text,
            provider_id="mock",
            model_id="mock-flash",
            opts=opts,
            source_file="chapters/v001-c0001.md",
        )


# ── 脚本轨 ──────────────────────────────────────────────────


def test_script_fields() -> None:
    print("脚本轨字段（不调模型）")

    result = run_annotate(MockState(annotation_payload=VALID_ANNOTATION, per_token_ms=0))
    fields = result.record["fields"]

    check(fields["char_count"] > 150, f"字数统计合理（{fields['char_count']}）")
    check(fields["para_count"] >= 7, f"段落数合理（{fields['para_count']}）")
    check(fields["avg_para_chars"] > 0, "平均段长算出")
    check(fields["dialogue_ratio"] > 0, f"对话占比大于 0（{fields['dialogue_ratio']}）")
    check(
        0 < fields["dialogue_ratio"] < 0.5,
        "对话占比在合理区间（未把全文误判为对话）",
    )
    check(fields["perspective_detected"] == "第一人称", "人称检测识别出第一人称")

    violations = fields["style_violations"]
    rule_ids = {v.get("rule_id") for v in violations}
    check("STYLE-002" in rule_ids, "检测到破折号违规")
    check("STYLE-003" in rule_ids, "检测到「不是X，是Y」句式违规")
    check("STYLE-001" in rule_ids, "检测到人称偏离段落")

    check(
        any("风格违规" in i for i in result.record["issues"]),
        "风格违规写进了 issues",
    )

    # 重叠字符表会导致同一个破折号被重复计数（「——」同时命中「——」和「—」）
    dash_rule = next((v for v in violations if v.get("rule_id") == "STYLE-002"), None)
    check(dash_rule is not None, "找到破折号规则条目")
    if dash_rule:
        check(
            dash_rule["count"] == 1,
            f"一个「——」只计 1 处，不重复计数（实际 {dash_rule['count']}）",
        )


def test_dialogue_does_not_flip_perspective() -> None:
    """回归：对话里的「我」曾被算进叙述人称，把第三人称章节判成第一人称。

    真实案例：某本第三人称小说第一章被判成「第一人称」，因为整章大量对话里
    人物都在说「我」。谁说话都会说「我」，这与叙述人称无关。
    """
    print("对话不参与人称判定（回归）")

    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(ROOT / "work.yaml")

    # 第三人称叙述 + 大量第一人称对话
    third_person_with_dialogue = """他推开门，屋里没有人。

「我不管！」她大声说，「我就是要走！」

他皱了皱眉，没有说话。

「你到底听没听见我说话？」她转过身来盯着他，「我问你，我怎么办？」

他站在原地，看着窗外。
"""
    fields, _ = compute_script_fields(
        third_person_with_dialogue, vol_no=1, chapter_no=1, work=work, primitives=primitives
    )
    check(
        fields["perspective_detected"] == "第三人称",
        f"对话密集的第三人称章节仍判为第三人称（实际 {fields['perspective_detected']}）",
    )
    check(
        not fields["style_violations"],
        "不再因为对话里的「我」误报人称违规",
    )

    # 反向：第一人称叙述仍应判为第一人称
    first_person = """我按住腰间的断刀，朝铁门关看了一眼。

「你还是要走？」她问。

我没有说话，只是握紧了刀柄。
"""
    fields2, _ = compute_script_fields(
        first_person, vol_no=1, chapter_no=1, work=work, primitives=primitives
    )
    check(
        fields2["perspective_detected"] == "第一人称",
        f"第一人称叙述仍判为第一人称（实际 {fields2['perspective_detected']}）",
    )


def test_bad_rule_reported_not_swallowed() -> None:
    """规则写错必须报出来。

    初版 _find_pattern 遇到正则编译失败会静默返回空列表，等于假装「没有违规」。
    这正是我们反复强调要避免的失效模式：错误被伪装成「一切正常」。
    """
    print("规则配置错误不被吞掉")

    from workshop.script_fields import _find_pattern

    hits, error = _find_pattern("任意文本", "不是[未闭合")
    check(hits == [], "无法编译时不返回命中")
    check(error is not None and "正则" in str(error), "正则错误被显式返回")
    check(
        _find_pattern("任意文本", "")[1] is not None,
        "未配置 pattern 也被视为规则问题",
    )

    work = load_work(ROOT / "work.yaml")
    broken = dict(work.raw)
    broken["style_checks"] = [
        {"id": "X-001", "desc": "坏正则", "type": "forbidden_pattern", "pattern": "不是[未闭合"}
    ]

    fields, issues = compute_script_fields(
        "随便一段文本。",
        vol_no=1,
        chapter_no=1,
        work=WorkConfig(broken),
        primitives=load_primitives(ROOT / "primitives.yaml"),
    )
    check(any("规则配置有误" in i for i in issues), "坏规则出现在 issues 里而不是被吞掉")
    check(
        not any(v.get("rule_id") == "X-001" for v in fields["style_violations"]),
        "坏规则不会被当成「有违规」上报",
    )


# ── 模型轨 ──────────────────────────────────────────────────


def test_model_fields_merged() -> None:
    print("模型轨字段合并与校验")

    result = run_annotate(MockState(annotation_payload=VALID_ANNOTATION, per_token_ms=0))
    fields = result.record["fields"]

    check(fields["emotion"] == 4, "情绪值正确合并")
    check(fields["hook_type"] == "情感", "钩子类型正确合并")
    check(isinstance(fields["payoffs"], list) and fields["payoffs"], "爽点数组保留")
    check(
        fields["payoffs"][0].get("类型") == "情感",
        "爽点内层字段保留",
    )
    check(
        isinstance(fields["foreshadows"], list) and fields["foreshadows"][0].get("编号") == "F-017",
        "伏笔编号保留",
    )

    prov = result.record["provenance"]
    check(prov["model_fields_filled"] == prov["model_fields_total"], "模型轨字段全部填充")
    check(
        result.record["self_report"].get("confidence") == "高",
        "自评置信度单独存放，不混进结构原语字段",
    )
    check(
        "confidence" not in result.record["fields"],
        "fields 里只有结构原语字段",
    )
    check(result.record["status"] == "ok", "状态为 ok")
    check(result.record["review_action"] == "auto_accept", "高置信度走自动通过")
    check(prov["thinking"] == "disabled", "思考模式记录为关闭")
    check(
        prov["prefix_hash"] and prov["primitives_hash"] and prov["work_hash"],
        "来源信息含前缀与原语指纹",
    )


def test_invalid_enum_rejected() -> None:
    print("非法取值被拒绝")

    result = run_annotate(MockState(annotation_payload=INVALID_ENUM_ANNOTATION, per_token_ms=0))

    check(result.record["status"] == "needs_review", "非法取值导致转人工")
    check(
        bool(result.record.get("validation_issues")),
        "校验问题被记录",
    )
    check(
        result.record["fields"].get("emotion") is None,
        "非法字段保持 None，没有被替换成默认值",
    )


def test_no_silent_defaults_on_unparsable() -> None:
    print("解析失败时不填默认值（最关键的一条）")

    state = MockState(annotation_raw_text="这不是 JSON，只是一段普通文字。", per_token_ms=0)
    result = run_annotate(state)

    check(result.attempts == 3, f"三次尝试后放弃（实际 {result.attempts} 次）")
    check(result.record["status"] == "needs_review", "状态转为 needs_review")

    model_keys = [
        "perspective",
        "hook_strength",
        "emotion",
        "motive_strength",
    ]
    for key in model_keys:
        check(result.record["fields"].get(key) is None, f"{key} 保持 None，未被填默认值")
    check(
        result.record["self_report"].get("confidence") is None,
        "自评置信度为 None，未被填默认值",
    )

    check(bool(result.record.get("errors")), "错误被显式记录")
    check(
        bool((result.record.get("debug") or {}).get("raw_response")),
        "保留了原始返回用于排查",
    )


def test_missing_fields_reported() -> None:
    print("缺字段被显式记录")

    partial = {k: v for k, v in VALID_ANNOTATION.items() if k not in ("emotion", "hook_strength")}
    result = run_annotate(MockState(annotation_payload=partial, per_token_ms=0))

    issues = result.record.get("validation_issues") or []
    check(any("emotion" in i for i in issues), "缺失字段 emotion 被点名")
    check(any("hook_strength" in i for i in issues), "缺失字段 hook_strength 被点名")
    check(result.record["status"] == "needs_review", "缺字段导致转人工")


# ── 前缀稳定性（缓存前提） ──────────────────────────────────


def test_prefix_stability() -> None:
    print("固定前缀逐字稳定（prompt 缓存的前提）")

    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(ROOT / "work.yaml")

    prefix_a = build_stable_prefix(primitives, work)
    prefix_b = build_stable_prefix(primitives, work)
    check(prefix_a == prefix_b, "两次构造的固定前缀完全一致")

    tail = build_variable_tail(
        chapter_label="第1章 夜行", chapter_text=SAMPLE_CHAPTER, prev_summary=None
    )
    messages = build_messages(stable_prefix=prefix_a, variable_tail=tail)
    check(len(messages) == 2, "消息为两条")
    check(messages[0]["role"] == "system", "固定前缀在 system 消息里")
    check(messages[0]["content"] == prefix_a, "system 消息内容即固定前缀")
    check("夜风从关外" in messages[1]["content"], "正文在变量尾巴里")
    check("夜风从关外" not in messages[0]["content"], "正文没有混进固定前缀")

    # 不同章节的固定前缀必须一致，否则缓存每次都失效
    tail2 = build_variable_tail(
        chapter_label="第2章 断刀", chapter_text="另一章的正文。", prev_summary=None
    )
    check(
        build_messages(stable_prefix=build_stable_prefix(primitives, work), variable_tail=tail2)[0][
            "content"
        ]
        == prefix_a,
        "换章节不改变固定前缀",
    )

    check("上一章" not in prefix_a, "固定前缀里没有上一章摘要（它属于变量部分）")


def test_empty_core_motive_is_explicit() -> None:
    """core_motive 留空时，P21 的度量对象不存在。

    不加说明的话模型会自己编一个动机打分（实测给 3 和 4），
    「猜出来的分数」和「诚实空值」在数据集里无法区分，前者更糟。
    所以固定前缀必须显式指示填 null——并且这个指示要能压住
    「本章没有出现的内容，强度类字段填 1」的通用约束，否则两者打架。
    """
    print("core_motive 为空时前缀必须显式指示 null")

    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(ROOT / "work.yaml")  # 模板里 core_motive 就是空的
    check(not work.core_motive, "模板配置的 core_motive 为空（测试前提）")

    prefix = build_stable_prefix(primitives, work)
    check("motive_strength" in prefix, "前缀点名了 motive_strength 字段")
    check("一律填 null" in prefix, "前缀显式指示填 null")
    check("优先级高于" in prefix, "与「强度类填 1」的通用约束的冲突被显式裁决")

    # 有 core_motive 时走另一条路，且不出现 null 指示
    filled = WorkConfig(
        {"work": "测试作品", "protagonist": "测试主角", "core_motive": "守护关城"}
    )
    prefix2 = build_stable_prefix(primitives, filled)
    check("主角核心动机：守护关城" in prefix2, "有动机时写入前缀")
    check("一律填 null" not in prefix2, "有动机时不出现 null 指示")


# ── 落盘与幂等 ──────────────────────────────────────────────


def test_save_and_idempotency() -> None:
    print("落盘与幂等")

    result = run_annotate(MockState(annotation_payload=VALID_ANNOTATION, per_token_ms=0))
    record = result.record

    with tempfile.TemporaryDirectory() as tmp:
        annotations_dir = Path(tmp) / "annotations"
        archive_dir = Path(tmp) / "archive"

        saved = save_annotation(record, annotations_dir, secrets=[FAKE_KEY], archive_dir=archive_dir)
        check(saved.exists(), "标注文件已落盘")
        check(saved.name == "v001-c0001.json", "文件按章节 id 命名")

        check(is_up_to_date(saved, text_sha256(SAMPLE_CHAPTER)), "原文一致时判定为最新")
        check(
            not is_up_to_date(saved, text_sha256(SAMPLE_CHAPTER + "改了")),
            "原文变化时判定为需重跑",
        )

        reloaded = load_annotation(saved)
        check(reloaded is not None, "文件可读回")
        check(reloaded["fields"]["emotion"] == 4, "读回后字段完整")
        check(reloaded["source"]["sha256"] == text_sha256(SAMPLE_CHAPTER), "来源哈希正确")

        # 重跑：旧产物应进归档而不是被静默覆盖
        save_annotation(record, annotations_dir, secrets=[FAKE_KEY], archive_dir=archive_dir)
        archived = list(archive_dir.glob("*.json"))
        check(len(archived) == 1, "重跑时旧产物进归档")


def test_annotation_file_redacted() -> None:
    print("标注文件落盘前脱敏")

    result = run_annotate(MockState(annotation_raw_text="解析失败", per_token_ms=0))
    record = result.record
    # 模拟上游错误体夹带密钥
    record["errors"] = [
        {
            "attempt": 1,
            "kind": "auth",
            "detail": f"upstream echoed header Authorization: Bearer {FAKE_KEY}",
        }
    ]

    with tempfile.TemporaryDirectory() as tmp:
        saved = save_annotation(record, tmp, secrets=[FAKE_KEY])
        content = saved.read_text(encoding="utf-8")

    check(FAKE_KEY not in content, "标注文件不含密钥明文")
    check("sk-****" in content, "密钥被替换为掩码")


def test_prev_summary_in_variable_tail() -> None:
    print("上一章摘要属于变量部分")

    primitives = load_primitives(ROOT / "primitives.yaml")
    work = load_work(ROOT / "work.yaml")
    prefix = build_stable_prefix(primitives, work)

    summary = {"hook_strength": 4, "hook_type": "情感", "emotion": 3, "foreshadows": [
        {"编号": "F-017", "动作": "埋设"}
    ]}
    tail = build_variable_tail(
        chapter_label="第2章", chapter_text="正文", prev_summary=summary
    )
    check("上一章" in tail, "摘要出现在变量尾巴里")
    check("F-017" in tail, "伏笔编号带进摘要")
    check("上一章" not in prefix, "摘要没有污染固定前缀")

    result = run_annotate(
        MockState(annotation_payload=VALID_ANNOTATION, per_token_ms=0), text="短正文"
    )
    check("prev_summary" not in json.dumps(result.record, ensure_ascii=False), "摘要不进产物")


def main() -> int:
    print("=" * 58)
    print("单章标注链路自检")
    print("=" * 58)

    for fn in (
        test_script_fields,
        test_dialogue_does_not_flip_perspective,
        test_bad_rule_reported_not_swallowed,
        test_model_fields_merged,
        test_invalid_enum_rejected,
        test_no_silent_defaults_on_unparsable,
        test_missing_fields_reported,
        test_prefix_stability,
        test_empty_core_motive_is_explicit,
        test_save_and_idempotency,
        test_annotation_file_redacted,
        test_prev_summary_in_variable_tail,
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
