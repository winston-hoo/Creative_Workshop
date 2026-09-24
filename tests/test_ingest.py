"""录入与切分链路的自检。

不需要任何真实作品、不调模型。直接跑：
    python tests/test_ingest.py

验证的是判定逻辑，不只是「能不能切出章节」。这里每一条断言都对应一次真实的失败模式：

  · 目录页吞掉真正的第一章
  · 重号被丢弃导致整章内容被静默拼接
  · 番外篇编号重启被误判成重号
  · 格式变体（番外XXX）完全检测不到
  · 清洗误删正文或偷偷规范化标点
  · 完整性核验形同虚设
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.annotate import strip_front_matter  # noqa: E402
from workshop.ingest import (  # noqa: E402
    cn_to_int,
    count_chars,
    detect_encoding,
    find_candidates,
    find_unmatched_title_like,
    format_preview,
    ingest,
    save_ingest,
)
from workshop.samples import build_synthetic_novel  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def with_synthetic(strip_repeated: bool = False, extra_patterns=None):
    tmp = Path(tempfile.mkdtemp(prefix="ingest-test-"))
    source = tmp / "夜行录.txt"
    source.write_text(build_synthetic_novel(), encoding="utf-8")
    result = ingest(
        source,
        work_name="演示作品",
        strip_repeated=strip_repeated,
        extra_patterns=extra_patterns,
    )
    return tmp, result


def chapters_of(result) -> list:
    return result.get("_chapters") or []


def by_id(result) -> dict:
    return {ch.chapter_id: ch for ch in chapters_of(result)}


def kinds_of(result) -> set[str]:
    return {a["kind"] for a in result["anomalies"]}


# ── 中文数字与编码 ──────────────────────────────────────────


def test_cn_numbers() -> None:
    print("中文数字转换")

    cases = {
        "一": 1, "十": 10, "十五": 15, "二十三": 23, "一百": 100,
        "一百零八": 108, "三百二十": 320, "一千零一": 1001, "7": 7, "42": 42,
    }
    for text, expected in cases.items():
        actual = cn_to_int(text)
        check(actual == expected, f"{text} → {expected}（实际 {actual}）")

    check(cn_to_int("东") is None, "非数字返回 None 而不是瞎猜")


def test_encoding_detection() -> None:
    print("编码探测")

    content = "夜风从关外卷过来，带着沙。"
    encoding, text = detect_encoding(content.encode("utf-8"))
    check(text == content, "UTF-8 正确解码")
    check(encoding == "utf-8", f"识别为 utf-8（实际 {encoding}）")

    _, text_gbk = detect_encoding(content.encode("gb18030"))
    check(text_gbk == content, "GB18030 正确解码（中文不会乱码）")

    _, text_bom = detect_encoding(b"\xef\xbb\xbf" + content.encode("utf-8"))
    check(text_bom == content, "BOM 被剥离")


# ── 切分 ────────────────────────────────────────────────────


def test_toc_does_not_swallow_first_chapter() -> None:
    """回归：目录页块曾把真正的第一章一起吞掉。

    原因是判定阈值写成了「中间非空文本不超过 30 字」，而卷标题「卷一 关山」
    只有 4 个字，没能终止目录块。现在规则改成「标题行之间只允许有空行」。
    """
    print("目录页不吞第一章（回归）")

    _, result = with_synthetic()
    chapters = chapters_of(result)

    check(len(chapters) == 8, f"切出 8 章（实际 {len(chapters)}）")
    check(chapters[0].chapter_id == "v001-c0001", "第一章存在于第 1 段")
    check(
        all(ch.chapter_no == i + 1 for i, ch in enumerate(chapters[:6])),
        "第 1 段前 6 章编号连续（1 到 6）",
    )
    check(len(result.get("toc_excluded") or []) == 5, "目录页排除 5 条")
    check("toc_block" in kinds_of(result), "记录了目录页异常")


def test_duplicate_kept_not_dropped() -> None:
    """回归：重号曾被丢弃，导致那一章正文被静默并进上一章。

    真实作品里两章同号、内容不同的情况很常见（编号冲突），丢了就是吞内容。
    现在的策略是：一条都不丢，加字母后缀，记异常让人确认。
    """
    print("重号保留 + 字母后缀（回归）")

    _, result = with_synthetic()
    mapping = by_id(result)

    check("v001-c0006" in mapping, "首个第 6 章保留")
    check("v001-c0006b" in mapping, "第二个第 6 章也保留，加了后缀 b")
    if "v001-c0006b" in mapping:
        check(
            "她问我这三天去了哪里" in mapping["v001-c0006b"].cleaned_text,
            "重号那一章的正文没有被并进上一章",
        )
        check(
            "她问我这三天去了哪里" not in mapping.get("v001-c0006", None).cleaned_text
            if mapping.get("v001-c0006")
            else False,
            "上一章的正文里不含重号章的内容",
        )

    check("duplicate_chapter" in kinds_of(result), "记录了重号异常")
    check(
        any("字母后缀" in a["detail"] for a in result["anomalies"] if a["kind"] == "duplicate_chapter"),
        "异常说明里讲清了处理方式",
    )


def test_section_restart() -> None:
    """回归：番外篇重新从第一章编号，曾被误判成重号。

    真实作品里「未曾设想的道路(番外篇)」之后又出现「第一章 红旗蛮眼熟(番外)」。
    现在按分段标记语义判定，会开启第 2 段。
    """
    print("编号重启判定为新分段（回归）")

    _, result = with_synthetic()
    mapping = by_id(result)

    check("section_restart" in kinds_of(result), "记录了分段重启异常")
    check("v002-c0001" in mapping, "番外篇第一章进入第 2 段（id 为 v002-c0001）")
    if "v002-c0001" in mapping:
        check(
            "铁锈味" in mapping["v002-c0001"].cleaned_text,
            "番外篇正文完整，没有被并进上一章",
        )
    check(len(result["volumes"]) == 2, f"清单里分为 2 段（实际 {len(result['volumes'])}）")


def test_unnumbered_chapters_do_not_steal_numbers() -> None:
    """回归：原文没编号的章节（如「番外XXX」）不能去抢一个章号。

    初版让它们顺延上一个章号，结果会与后面真正的第 N 章撞号、凭空造出重号，
    还会把真实的缺口填掉。现在它们用 `v{段}-x{序号}`，与编号章节分开。
    """
    print("无编号章节不占章号（回归）")

    _, result = with_synthetic(extra_patterns=[r"^番外\s*(.{1,30})$"])
    mapping = by_id(result)

    x_ids = [cid for cid in mapping if cid.split("-")[1].startswith("x")]
    check(len(x_ids) == 1, f"识别出 1 个无编号章节（实际 {len(x_ids)}）")
    if x_ids:
        ch = mapping[x_ids[0]]
        check(ch.chapter_no is None, "无编号章节的 chapter_no 为 None")
        check("进击的东北空军上" in ch.title, "标题正确")
        check(
            "飞机从云层里钻出来" in ch.cleaned_text,
            "正文完整，没有被并进上一章",
        )

    # 关键：无编号章节出现之后，后面的编号章节不能因此变成重号
    dup = [a for a in result["anomalies"] if a["kind"] == "duplicate_chapter"]
    check(
        all("进击的东北空军" not in a["detail"] for a in dup),
        "无编号章节没有引发假重号",
    )

    volumes = result["volumes"]
    check(
        any(v.get("unnumbered_count") for v in volumes),
        "段落统计里记录了无编号章节数",
    )


def test_gap_repaired_by_loose_patterns() -> None:
    print("缺口用宽松模板修复")

    _, result = with_synthetic()
    mapping = by_id(result)

    check("v001-c0004" in mapping, "第 4 章被找回")
    if "v001-c0004" in mapping:
        check(mapping["v001-c0004"].status == "repaired", "标记为「修复」")
        check("刀是断的" in mapping["v001-c0004"].cleaned_text, "第 4 章正文完整")

    found = kinds_of(result)
    check("repaired_chapter" in found, "记录了修复动作")
    check("missing_chapter" not in found, "修复后不应残留缺口")


def test_unmatched_title_like_probe() -> None:
    """格式变体探针：像章节标题但没命中任何模板的行必须被捞出来。

    真实作品里番外章节常写成「番外进击的东北空军上」，严格模板全部落空，
    这些内容会被悄悄并进上一章。只看章节总数发现不了。
    """
    print("格式变体探针")

    _, result = with_synthetic()
    unmatched = result.get("unmatched_title_like") or []
    texts = [item["text"] for item in unmatched]

    check(any("番外进击的东北空军上" in t for t in texts), "捞出未命中的番外标题")
    check(len(unmatched) >= 1, f"探针有输出（{len(unmatched)} 行）")

    # 带句末标点的正文句子不该被误捞
    check(
        not any(t.endswith("。") for t in texts),
        "正文句子（句号结尾）没有被误报",
    )


def test_extra_patterns_from_work_config() -> None:
    print("work.yaml 自定义模板")

    _, result = with_synthetic(extra_patterns=[r"^番外(.{1,30})$"])
    mapping = by_id(result)

    check(len(chapters_of(result)) == 9, f"加上自定义模板后应为 9 章（实际 {len(chapters_of(result))}）")
    check(
        any("进击的东北空军上" in ch.title for ch in chapters_of(result)),
        "自定义模板把番外章节切出来了",
    )
    check(
        not any("进击的东北空军上" in t["text"] for t in result.get("unmatched_title_like") or []),
        "已被模板命中的行不再出现在探针里",
    )

    # 坏正则必须报错，不能静默忽略
    try:
        with_synthetic(extra_patterns=["不是[未闭合"])
    except ValueError as exc:
        check("无法编译" in str(exc), "坏模板被显式报错")
    else:
        check(False, "坏模板应当报错")


def test_candidates_exclude_quoted_lines() -> None:
    print("候选识别排除带引号的行")

    text = "第一章 夜行\n正文。\n「第一章 夜行」\n第二章 旧约\n正文。\n"
    candidates = find_candidates(text)
    check(len(candidates) == 2, f"只识别 2 个候选（实际 {len(candidates)}）")
    check(all("「" not in c.raw_line for c in candidates), "带引号的行被排除")


def test_body_text_not_mistaken_for_chapter() -> None:
    """回归：这两类正文行曾被我当成章节。

      · 「7.92毫米毛瑟步枪弹（尖头弹）两条」  ← 小数被当成「7、」
      · 「1.双方基于平等互利原则…」            ← 列表序号被当成「1、」
      · 「（这章刚码完着急上传忘记改vip了）」  ← 作者注被当成括号标题
    """
    print("正文行不被误判为章节（回归）")

    bad_lines = [
        "7.92毫米毛瑟步枪弹（尖头弹）两条、7.7毫米子弹生产线六条。",
        "1.双方基于平等互利原则，建立以中国工业现代化为核心的合作关系。",
        "2.中国以钨、锑、锡等战略矿产偿付德国提供的工业设备。",
        "3.外务省转来的英法驻华公使对张学良施压的最新情况简报。",
        "（这章刚码完着急上传忘记改vip了，就当赠送大家了。）",
        "(1930年6月28日·洛阳)",
        "(主笔 张季鸾)",
    ]
    text = "第一章 夜行\n正文。\n" + "\n".join(bad_lines) + "\n第二章 旧约\n正文。\n"
    candidates = find_candidates(text)

    check(
        len(candidates) == 2,
        f"只识别出两个真章节（实际 {len(candidates)}：{[c.raw_line.strip()[:20] for c in candidates]}）",
    )
    check(
        all(c.raw_line.strip() in ("第一章 夜行", "第二章 旧约") for c in candidates),
        "命中的两行都是真标题",
    )


# ── 清洗 ────────────────────────────────────────────────────


def test_cleaning_is_conservative() -> None:
    print("清洗的保守性")

    _, result = with_synthetic()
    mapping = by_id(result)
    ch1 = mapping["v001-c0001"]

    check(ch1.cleaned_text.lstrip().startswith("第一章 夜行"), "清洗后标题行仍在正文开头")
    check("——" in ch1.cleaned_text, "破折号原样保留（未做标点规范化）")
    check("夜风从关外卷过来，带着沙。" in ch1.cleaned_text, "第 1 章正文逐字保留")
    check(
        "我不是怕死，我是怕她等不到。" in mapping["v001-c0004"].cleaned_text,
        "第 4 章正文逐字保留",
    )
    check(
        not any(line != line.rstrip() for line in ch1.cleaned_text.split("\n")),
        "行尾空白已清理",
    )

    log = result.get("_cleaning_log") or []
    check(len(log) > 0, f"清洗留下了日志（{len(log)} 条）")
    check(all("chapter" in entry for entry in log), "每条日志都带章节归属")
    check(
        any(entry["rule"] == "trailing_whitespace" for entry in log),
        "行尾空白的删除被记录",
    )


def test_repeated_lines_reported_not_deleted() -> None:
    print("页眉默认只报告不删除")

    _, result = with_synthetic()
    repeated = result.get("repeated_lines_reported") or {}
    check("关山灯·内部稿" in repeated, "反复出现的短行被报告")
    check(repeated.get("关山灯·内部稿") == 5, f"计数为 5（实际 {repeated.get('关山灯·内部稿')}）")

    ch1 = by_id(result)["v001-c0001"]
    check("关山灯·内部稿" in ch1.cleaned_text, "默认情况下页眉仍在正文里（未误删）")

    _, result_strip = with_synthetic(strip_repeated=True)
    ch1_strip = by_id(result_strip)["v001-c0001"]
    check("关山灯·内部稿" not in ch1_strip.cleaned_text, "显式打开开关后页眉被删除")
    check(
        any(e["rule"] == "repeated_line" for e in result_strip.get("_cleaning_log") or []),
        "删除页眉的行为被记录在日志里",
    )


# ── 完整性与落盘 ────────────────────────────────────────────


def test_integrity() -> None:
    print("完整性核验")

    _, result = with_synthetic()
    integrity = result["integrity"]

    check(integrity["reconstruction_ok"], "重建校验通过（拼回去与原文件逐字一致）")
    check(integrity["delta"] == 0, f"字数对账偏差为 0（实际 {integrity['delta']}）")
    check(not integrity["slice_errors"], "无切片错误")
    check(integrity["passed"], "门禁通过")
    check(
        integrity["rebuilt_chars"] == integrity["source_chars"],
        "重建字数等于源文件字数",
    )


def test_preview_renders() -> None:
    print("预览可渲染")

    _, result = with_synthetic()
    text = format_preview(result)
    for keyword in ("切分预览", "模板命中分布", "完整性核验", "门禁", "格式变体探针"):
        check(keyword in text, f"预览含「{keyword}」")


def test_save_and_roundtrip() -> None:
    print("落盘与回读")

    tmp, result = with_synthetic()
    paths = save_ingest(result, tmp / "00-ingest")

    check(paths["manifest"].exists(), "清单已写出")
    check(paths["cleaning_log"].exists(), "清洗日志已写出")
    check("review" in paths, "复核队列已写出（有异常）")

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    check(not any(k.startswith("_") for k in manifest), "清单里不含内部字段")
    check(len(manifest["chapters"]) == 8, "清单含 8 章")
    check(manifest["integrity"]["passed"] is True, "清单里记录了门禁结论")
    check(len(manifest["pattern_stats"]) >= 1, "清单含模板命中分布")
    check(isinstance(manifest["unmatched_title_like"], list), "清单含格式变体探针结果")

    chapter_file = paths["chapters_dir"] / "v001-c0001.md"
    meta, body = strip_front_matter(chapter_file.read_text(encoding="utf-8"))
    check(meta.get("chapter_no") == 1, "front-matter 可被标注模块读回")
    check("夜风从关外" in body, "回读后正文完整")
    check((paths["raw_dir"] / "v001-c0001.md").exists(), "清洗前原文也保留了")

    first = manifest["chapters"][0]
    check(first["char_count"] == count_chars(body), "清单字数与章节文件字数一致")
    check(first["byte_offset_start"] >= 0, "记录了字节偏移（回溯锚点）")


def test_save_reports_stale_files() -> None:
    """回归：改了切分规则重跑，上一轮的产物会悬空。

    实测过一次：番外从「并入上一章」改成「独立章节」后，留下一个不属于任何章节的
    重号文件，目录 1066 个而清单 1065 章，两边对不上。现在会移入 _review/stale/ 而不是删掉。
    """
    print("重跑时清理陈旧文件（回归）")

    tmp, result = with_synthetic()
    out = tmp / "00-ingest"
    save_ingest(result, out)

    # 手工塞一个不属于本次清单的残留文件，模拟改规则后的悬空产物
    stale = out / "chapters" / "v001-c0006c.md"
    stale.write_text("---\nid: v001-c0006c\n---\n\n残留内容\n", encoding="utf-8")
    check(stale.exists(), "残留文件已就位")

    paths = save_ingest(result, out)

    check(not stale.exists(), "陈旧文件已从 chapters/ 移走")
    check("stale_dir" in paths, "返回里给出了陈旧文件目录")
    quarantined = list((out / "_review" / "stale").glob("*v001-c0006c.md"))
    check(len(quarantined) == 1, "陈旧文件被移到 _review/stale/ 而不是删除")
    if quarantined:
        check("残留内容" in quarantined[0].read_text(encoding="utf-8"), "内容保留")

    # 目录与清单必须一致
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    ids = {c["id"] for c in manifest["chapters"]}
    files = {p.stem for p in (out / "chapters").glob("*.md")}
    check(ids == files, f"目录与清单完全一致（{len(files)} 个文件 / {len(ids)} 章）")
    check(
        manifest.get("stale_files_moved"),
        "清单里记录了清理动作",
    )


def main() -> int:
    print("=" * 58)
    print("录入与切分链路自检")
    print("=" * 58)

    for fn in (
        test_cn_numbers,
        test_encoding_detection,
        test_toc_does_not_swallow_first_chapter,
        test_duplicate_kept_not_dropped,
        test_section_restart,
        test_unnumbered_chapters_do_not_steal_numbers,
        test_gap_repaired_by_loose_patterns,
        test_unmatched_title_like_probe,
        test_extra_patterns_from_work_config,
        test_candidates_exclude_quoted_lines,
        test_body_text_not_mistaken_for_chapter,
        test_cleaning_is_conservative,
        test_repeated_lines_reported_not_deleted,
        test_integrity,
        test_preview_renders,
        test_save_and_roundtrip,
        test_save_reports_stale_files,
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
