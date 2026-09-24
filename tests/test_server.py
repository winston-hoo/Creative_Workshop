"""工作台接口自检。用 FastAPI TestClient 直接打接口，不需要另起服务。

    python tests/test_server.py

**自足**：整套接口跑在一块临时目录上，不碰真实的 `workspaces/`。

这一点是被逼出来的，而且被逼了两次：
  1. 早期版本断言「书架里应该有某本具体作品」，清理掉验证数据后测试就红了；
  2. 改成「书架为空」之后，用户一导入自己的作品，测试又红了。

两次都是同一个毛病：**断言依赖了「环境里恰好有什么数据」**。

修法分两层，两层都要：
  · **让测试拥有自己的根目录**（临时目录），不要求外部环境保持干净；
  · **断言按名字定位**，不用「第几个」「总数等于几」这类位置判据。

第二层不能省：临时目录解决的是「不碰真实数据」，但位置判据本身就是脆的——
真实使用中书架会有作品，而 UI 冒烟测试曾经用泛选器把用户的书移进了归档。

验证的是接口契约本身：
  · 界面文件可达
  · 书架读的是磁盘真实状态；下划线开头的目录不算作品
  · 概览的统计与清单一致
  · 章节列表分页与筛选
  · 导入必须两步：先预览（不落盘）、再确认
  · 门禁不通过时拒绝落盘
  · 移出书架是**移动而非删除**，且可恢复
  · 路径穿越与非法输入被挡住
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

# 把工作台的根目录换成临时目录。真实 workspaces/ 里有什么作品都不影响这套测试，
# 测试也不会在用户的数据目录里留下任何东西。
TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-server-test-"))
server_main.PATHS = services.Paths(root=TMP_ROOT)
WS = TMP_ROOT / "workspaces"

# 服务商与原语配置是全局的，路径由根目录决定，所以临时根目录里也要有一份。
# 测试只读它们，不写回。
for _name in ("providers.yaml", "primitives.yaml"):
    shutil.copy(ROOT / _name, TMP_ROOT / _name)

client = TestClient(app)

TEST_WORK = "接口自检作品"
JUNK_DIR = "_不该出现在书架"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def cleanup() -> None:
    shutil.rmtree(WS / TEST_WORK, ignore_errors=True)
    shutil.rmtree(WS / JUNK_DIR, ignore_errors=True)
    archive = WS / "_archive"
    if archive.exists():
        for p in archive.glob(f"{TEST_WORK}*"):
            shutil.rmtree(p, ignore_errors=True)


def import_test_work() -> dict:
    """走完整的两步导入，返回 commit 的结果。"""
    cleanup()
    files = {"file": ("合成测试.txt", build_synthetic_novel().encode("utf-8"), "text/plain")}
    preview = client.post("/api/import/upload", files=files, data={"work": TEST_WORK}).json()
    r = client.post(
        "/api/import/commit",
        json={"token": preview["token"], "work": TEST_WORK, "strip_ads": False, "strip_repeated": False},
    )
    return r.json()


# ── 基础 ────────────────────────────────────────────────────


def test_health() -> None:
    print("健康检查")
    r = client.get("/api/health")
    check(r.status_code == 200, "返回 200")
    check(r.json().get("ok") is True, "ok 为 true")
    check(bool(r.json().get("version")), "带版本号")


def test_index_served() -> None:
    print("界面文件可达")
    r = client.get("/")
    check(r.status_code == 200, "首页返回 200")
    check("小说创作工坊" in r.text, "首页含标题")
    check("确认导入" in (client.get("/static/app.js").text), "脚本里含导入流程")
    check(client.get("/static/style.css").status_code == 200, "样式表可达")


# ── 书架 ────────────────────────────────────────────────────


def test_shelf_does_not_fabricate_works() -> None:
    """书架不凭空造数据。

    原版断言「书架必须为空」——在全新安装时成立，但真实使用中书架当然会有作品。
    这条断言自己咬过两次：清理验证数据后它红，后来用户导入了真作品它又红。

    真正要守的约定是：**列表里出现的每一部，都在磁盘上有对应的导入产物**；
    测试自己造的作品，导入前不允许出现。空书架只是全新安装时的特例。
    """
    print("书架不凭空造数据")
    cleanup()
    names = {w["name"] for w in client.get("/api/works").json()}
    check(TEST_WORK not in names, "没导入的作品不会出现在书架里")

    works = client.get("/api/works").json()
    ghost = [w for w in works if not (WS / w["dir_name"]).is_dir()]
    check(not ghost, f"列表里没有磁盘上不存在的幽灵作品（{[g['name'] for g in ghost]}）")


def test_underscore_dirs_are_not_works() -> None:
    """下划线开头的目录不算作品（归档、试验产物都靠这个约定）。"""
    print("下划线目录不算作品")

    junk = WS / JUNK_DIR
    (junk / "00-ingest").mkdir(parents=True, exist_ok=True)
    (junk / "00-ingest" / "manifest.json").write_text(
        json.dumps({"work": "不该出现", "chapters": [{"id": "x"}], "source": {}}, ensure_ascii=False),
        encoding="utf-8",
    )

    names = {w["name"] for w in client.get("/api/works").json()}
    check("不该出现" not in names, "下划线目录没有出现在书架里")
    # 400 或 404 都算对：前者是名字被校验拒了，后者是找不到。
    # 关键是**不能 200**——书架上看不到、却能直接访问，两边不一致比拒绝更糟。
    r = client.get(f"/api/works/{JUNK_DIR}")
    check(r.status_code in (400, 404), f"也无法直接访问（{r.status_code}）")
    cleanup()


def test_shelf_reflects_disk() -> None:
    print("书架反映磁盘真实状态")
    out = import_test_work()
    check(out["chapters"] == 8, f"导入 8 章（实际 {out['chapters']}）")

    # 书架上是真实作品 + 本测试作品，不能假设只剩测试作品一个。
    # 按名字找，而不是按位置或总数。
    works = client.get("/api/works").json()
    mine = [w for w in works if w["name"] == TEST_WORK]
    check(len(mine) == 1, f"书架上能找到测试作品（实际 {len(mine)} 部同名）")
    w = mine[0]
    check(w["chapters"] == 8, "章数与导入结果一致")
    check(w["integrity_passed"] is True, "完整性标记为通过")
    check(w["annotated"] == 0, "尚无标注")
    check(w["open_foreshadows"] == 0, "尚无伏笔")


def test_work_detail_matches_manifest() -> None:
    print("概览与清单一致")
    import_test_work()
    d = client.get(f"/api/works/{TEST_WORK}").json()

    manifest = json.loads(
        (WS / TEST_WORK / "00-ingest" / "manifest.json").read_text(
            encoding="utf-8-sig"
        )
    )
    check(d["statistics"]["chapters"] == len(manifest["chapters"]), "章数与清单一致")
    check(d["source"]["chars"] == manifest["source"]["total_chars_raw"], "字数与清单一致")
    check(d["integrity"]["reconstruction_ok"] is True, "重建校验通过")
    check(isinstance(d["counts"], dict), "返回了异常分类")
    check(len(d["volumes"]) == len(manifest["volumes"]), "分段数与清单一致")


def test_chapters_paging_and_filter() -> None:
    print("章节列表：分页与筛选")
    import_test_work()

    page = client.get(f"/api/works/{TEST_WORK}/chapters?offset=0&limit=5").json()
    check(page["total"] == 8, f"总数正确（{page['total']}）")
    check(len(page["items"]) == 5, "分页生效")
    check("unnumbered" in page["items"][0], "返回了无编号标记（前端要用它渲染）")

    page2 = client.get(f"/api/works/{TEST_WORK}/chapters?offset=5&limit=5").json()
    check(len(page2["items"]) == 3, "第二页 3 条")
    check(page["items"][0]["id"] != page2["items"][0]["id"], "两页内容不重叠")

    only = client.get(f"/api/works/{TEST_WORK}/chapters?only=unnumbered").json()
    check(only["total"] == 0, "该作品没有无编号章节")
    check(
        client.get(f"/api/works/{TEST_WORK}/chapters?limit=9999").status_code == 422,
        "limit 超上限被拒",
    )
    check(
        client.get(f"/api/works/{TEST_WORK}/chapters?only=乱写").status_code == 422,
        "非法筛选值被拒",
    )


def test_unknown_work_404() -> None:
    print("不存在的作品")
    check(client.get("/api/works/根本没有这部").status_code == 404, "返回 404")


def test_path_traversal_blocked() -> None:
    print("路径穿越被挡住")

    from server.main import safe_name

    checks = 0
    for bad in ("..", "../..", "a/b", "a\\b", "", "   ", "a:b", "a*b", 'a"b', "a<b"):
        try:
            safe_name(bad)
        except Exception:
            checks += 1
    check(checks == 10, f"校验函数挡住了全部 10 种非法形态（实际 {checks}）")

    try:
        check(safe_name("  示例作品  ") == "示例作品", "合法名字被正确清理")
        check(safe_name("示例作品 1-1064") == "示例作品 1-1064", "短横线与空格允许")
    except Exception as exc:  # noqa: BLE001
        check(False, f"合法名字被误拒：{exc}")

    for encoded in ("..evil", "a%5Cb", "a%2Fb"):
        r = client.get(f"/api/works/{encoded}")
        check(r.status_code in (400, 404), f"「{encoded}」被拒绝（{r.status_code}）")


# ── 移出书架 ────────────────────────────────────────────────


def test_archive_moves_not_deletes() -> None:
    """移出书架必须是**移动**。

    「我不想在书架上看到它」是个可逆诉求，「删掉它」是不可逆操作——
    两者不该用同一个动作。整个工作区要原样保留在 _archive/ 下，可恢复。
    """
    print("移出书架＝移动，可恢复")

    out = import_test_work()
    check(out["ok"] is True, "先导入一部")

    work_dir = WS / TEST_WORK
    marker = work_dir / "10-annotations" / "v001-c0001.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"chapter_id":"v001-c0001"}', encoding="utf-8")

    r = client.post(f"/api/works/{TEST_WORK}/archive", json={})
    check(r.status_code == 200, f"移出成功（{r.status_code}）")
    moved_to = Path(r.json()["moved_to"])
    check(not work_dir.exists(), "原位置已不在")
    check(moved_to.exists(), "归档位置存在")

    # 关键：内容一个都不能少
    check((moved_to / "00-ingest" / "manifest.json").exists(), "清单还在")
    check((moved_to / "10-annotations" / "v001-c0001.json").exists(), "标注也一起搬走了")
    check(
        len(list((moved_to / "00-ingest" / "chapters").glob("*.md"))) == 8,
        "章节文件一个不少",
    )

    names = {w["name"] for w in client.get("/api/works").json()}
    check(TEST_WORK not in names, "书架上看不到了")

    # 恢复：搬回去就回来
    shutil.move(str(moved_to), str(work_dir))
    names = {w["name"] for w in client.get("/api/works").json()}
    check(TEST_WORK in names, "搬回 workspaces/ 后重新出现")
    cleanup()


def test_archive_unknown_work_404() -> None:
    print("移出不存在的作品")
    check(client.post("/api/works/根本没有这部/archive", json={}).status_code == 404, "返回 404")


def test_archive_restore_via_api() -> None:
    """归档恢复走 API：列出 → 恢复，数据原样回来；同名时拒绝覆盖。"""
    print("归档区列出一键恢复（API）")
    out = import_test_work()
    check(out["ok"] is True, "先导入一部")

    work_dir = WS / TEST_WORK
    marker = work_dir / "10-annotations" / "v001-c0001.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"chapter_id":"v001-c0001"}', encoding="utf-8")

    # 移出
    r = client.post(f"/api/works/{TEST_WORK}/archive", json={})
    check(r.status_code == 200, "移出成功")

    # 归档列表里能看到，带标注数
    archived = client.get("/api/archive").json()
    names = [a["name"] for a in archived["items"]]
    check(TEST_WORK in names, "归档列表里有它")
    entry = next(a for a in archived["items"] if a["name"] == TEST_WORK)
    check(entry["chapters"] == 8, f"列出章数（{entry['chapters']}）")
    check(entry["annotated"] == 1, "列出标注数")

    # 恢复
    r = client.post("/api/archive/restore", json={"name": TEST_WORK})
    check(r.status_code == 200 and r.json()["ok"], "恢复成功")
    check(work_dir.exists(), "搬回了原位置")
    check((work_dir / "10-annotations" / "v001-c0001.json").exists(), "标注原样回来")
    names = {w["name"] for w in client.get("/api/works").json()}
    check(TEST_WORK in names, "书架重新出现")
    archived = client.get("/api/archive").json()
    check(TEST_WORK not in [a["name"] for a in archived["items"]], "归档区已清走")

    # 同名时拒绝覆盖
    r2 = client.post("/api/archive/restore", json={"name": TEST_WORK})
    check(r2.status_code == 404, "归档区已没有它，恢复给 404")

    # 造一个同名的归档副本，验证「书架上已有同名」时拒绝。
    # 归档区重名副本是「作品名.时间戳」形态（数字后缀），恢复会还原成基础名。
    shutil.rmtree(WS / "_archive", ignore_errors=True)
    (WS / "_archive").mkdir(parents=True, exist_ok=True)
    shutil.copytree(work_dir, WS / "_archive" / f"{TEST_WORK}.20260924T151222")
    r3 = client.post("/api/archive/restore", json={"name": f"{TEST_WORK}.20260924T151222"})
    check(r3.status_code == 400, "书架上已有同名时拒绝恢复（400）")
    check("不会覆盖" in json.dumps(r3.json(), ensure_ascii=False), "拒绝理由说明不覆盖")
    cleanup()


# ── 导入向导 ────────────────────────────────────────────────


def test_import_preview_does_not_write() -> None:
    """导入是两步：预览不落盘，确认才落盘。"""
    print("预览不落盘（两步导入）")
    cleanup()

    files = {"file": ("合成测试.txt", build_synthetic_novel().encode("utf-8"), "text/plain")}
    r = client.post("/api/import/upload", files=files, data={"work": TEST_WORK})
    check(r.status_code == 200, f"预览返回 200（实际 {r.status_code}）")

    d = r.json()
    check(bool(d.get("token")), "返回了 token")
    check(d["counts"]["chapters"] == 8, f"识别 8 章（实际 {d['counts']['chapters']}）")
    check(d["integrity"]["passed"] is True, "完整性核验通过")
    check(len(d["chapters_sample"]) > 0, "返回了章节样例")
    check(not (WS / TEST_WORK).exists(), "预览阶段没有写入作品目录")

    blob = json.dumps(d, ensure_ascii=False)
    check("刀是断的" not in blob, "预览不返回整章正文")


def test_import_commit_writes() -> None:
    print("确认导入才落盘")
    out = import_test_work()
    check(out["chapters"] == 8, "写入 8 章")
    check(out["integrity_passed"] is True, "完整性核验通过")

    work_dir = WS / TEST_WORK
    check((work_dir / "00-ingest" / "manifest.json").exists(), "清单已落盘")
    check(len(list((work_dir / "00-ingest" / "chapters").glob("*.md"))) == 8, "章节文件 8 个")
    check((work_dir / "00-ingest" / "raw-chapters").exists(), "清洗前副本也写了")
    cleanup()


def test_upload_token_single_use() -> None:
    print("确认导入后 token 失效")
    cleanup()
    files = {"file": ("合成测试.txt", build_synthetic_novel().encode("utf-8"), "text/plain")}
    token = client.post("/api/import/upload", files=files, data={"work": TEST_WORK}).json()["token"]

    first = client.post("/api/import/commit", json={"token": token, "work": TEST_WORK})
    check(first.status_code == 200, "第一次成功")

    again = client.post("/api/import/commit", json={"token": token, "work": TEST_WORK})
    check(again.status_code == 404, "第二次被拒绝（临时文件已清理）")

    uploads = TMP_ROOT / ".tmp" / "uploads" / token
    check(not uploads.exists(), "临时上传已清理")
    cleanup()


def test_import_rejects_bad_token() -> None:
    print("非法 token 被拒绝")
    for bad in ("../../etc/passwd", "zzzz", "", "0123456789abcdef", "GGGGGGGGGGGGGGGG"):
        r = client.post("/api/import/commit", json={"token": bad, "work": TEST_WORK})
        check(r.status_code in (400, 404), f"「{bad or '(空)'}」被拒绝（{r.status_code}）")


def test_import_rejects_empty_file() -> None:
    print("空文件被拒绝")
    files = {"file": ("空.txt", b"", "text/plain")}
    r = client.post("/api/import/upload", files=files, data={"work": TEST_WORK})
    check(r.status_code == 400, f"返回 400（实际 {r.status_code}）")


def test_import_rejects_bad_pattern() -> None:
    print("坏模板被拒绝")
    work_dir = WS / TEST_WORK
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "work.yaml").write_text(
        "work: 接口自检作品\ningest:\n  chapter_patterns:\n    - '不是[未闭合'\n", encoding="utf-8"
    )
    files = {"file": ("合成测试.txt", build_synthetic_novel().encode("utf-8"), "text/plain")}
    r = client.post("/api/import/upload", files=files, data={"work": TEST_WORK})
    check(r.status_code == 400, f"返回 400（实际 {r.status_code}）")
    check("无法编译" in json.dumps(r.json(), ensure_ascii=False), "错误说明是模板编译失败")
    cleanup()


# ── 章节阅读 ────────────────────────────────────────────────


def test_chapter_detail_reads_text() -> None:
    """章节正文要能读到。

    「不泄露正文」那条约束是针对**导入预览**的（文件还没确认归属时只给统计）。
    作品导入完成后它就是用户自己的本地数据，读正文是这台机器上最正当的用法。
    """
    print("章节正文")
    import_test_work()
    d = client.get(f"/api/works/{TEST_WORK}/chapters/v001-c0001").json()
    check(d["id"] == "v001-c0001", "章节 id 正确")
    check(len(d["text"]) > 0, "返回了正文")
    check("夜行" in d["text"], "正文内容对得上")
    check(d["chapter_no"] == 1, "章号正确")
    check(d["total"] == 8, f"总章数正确（{d['total']}）")
    check(d["prev"] is None, "第一章没有上一章")
    check(d["next"]["id"] == "v001-c0002", "下一章指向正确")
    check(d["fields_source"] == "script_live", "没标注时现算脚本轨")
    check("char_count" in d["fields"], "带上了字数等脚本轨字段")


def test_chapter_raw_variant() -> None:
    print("清洗前后两份都能看")
    import_test_work()
    cleaned = client.get(f"/api/works/{TEST_WORK}/chapters/v001-c0001").json()
    raw = client.get(f"/api/works/{TEST_WORK}/chapters/v001-c0001?raw=true").json()
    check(cleaned["variant"] == "cleaned", "默认给清洗后")
    check(raw["variant"] == "raw", "raw=true 给清洗前原文")
    check(len(raw["text"]) >= len(cleaned["text"]), "原文不短于清洗后（清洗只删不增）")


def test_chapter_unknown_id_404() -> None:
    print("不存在的章节 id")
    import_test_work()
    # 不放裸的「..」：客户端会把它规范化成 /chapters（列表接口），命中 200 是对的，
    # 不是校验失效。要测的是「清单里查不到的 id 一律 404」。
    for bad in ("v001-c9999", "../../etc/passwd", "v001-c0001/../../secrets"):
        r = client.get(f"/api/works/{TEST_WORK}/chapters/{bad}")
        check(r.status_code == 404, f"「{bad}」返回 404（实际 {r.status_code}）")
    # 校验的关键：不是靠黑名单挡字符，而是 id 必须在清单里查得到
    check(
        client.get(f"/api/works/{TEST_WORK}/chapters/v001-c0001").status_code == 200,
        "清单里有的 id 正常返回",
    )
    cleanup()


def _fake_annotation(work_dir: Path, chapter_id: str, *, status: str = "ok") -> None:
    """造一条能通过幂等判定的标注，用来验证列表筛选与勾选范围。"""
    from workshop.annotate import strip_front_matter, text_sha256

    raw = (work_dir / "00-ingest" / "chapters" / f"{chapter_id}.md").read_text(encoding="utf-8")
    _meta, body = strip_front_matter(raw)
    record = {
        "chapter_id": chapter_id,
        "status": status,
        "fields": {"chapter_summary": "一句话梗概"},
        "provenance": {"model_fields_filled": 14, "model_fields_total": 14},
        "source": {"sha256": text_sha256(body)},
    }
    target = work_dir / "10-annotations" / f"{chapter_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def test_chapter_list_shows_annotation_state() -> None:
    """已标注的章节要在列表里看得见，而且要能按标注状态筛。"""
    print("章节列表的标注状态与筛选")
    import_test_work()
    work_dir = WS / TEST_WORK
    _fake_annotation(work_dir, "v001-c0001")
    _fake_annotation(work_dir, "v001-c0002", status="needs_review")

    data = client.get(f"/api/works/{TEST_WORK}/chapters?limit=50").json()
    check(data["annotated_total"] == 2, f"汇总了已标注章数（{data['annotated_total']}）")
    states = {c["id"]: c["annot_state"] for c in data["items"]}
    check(states.get("v001-c0001") == "已标注", "已标注的章标出来")
    check(states.get("v001-c0002") == "待复核", "待复核的章标出来")
    check(states.get("v001-c0003") == "未标注", "没跑的章也标出来（不是空白）")

    for only, expect in (("annotated", 2), ("unannotated", 6), ("review", 1), ("all", 8)):
        r = client.get(f"/api/works/{TEST_WORK}/chapters?limit=50&only={only}").json()
        check(r["total"] == expect, f"only={only} → {expect} 章（实际 {r['total']}）")

    check(
        client.get(f"/api/works/{TEST_WORK}/chapters?only=乱写").status_code == 422,
        "非法筛选值被拒",
    )
    cleanup()


def test_annotate_selected_chapters() -> None:
    """勾选出来的章节要能单独出计划；force 才能重跑已标注的。"""
    print("按勾选范围出计划")
    import_test_work()
    work_dir = WS / TEST_WORK
    _fake_annotation(work_dir, "v001-c0001")
    _fake_annotation(work_dir, "v001-c0002")

    url = f"/api/works/{TEST_WORK}/annotate/plan/selection"

    # 两条已标注且原文未变 → 不重跑就没什么可跑的
    r = client.post(url, json={"chapter_ids": ["v001-c0001", "v001-c0002"]}).json()
    check(r["pending"] == 0, f"默认跳过已标注（待跑 {r['pending']}）")
    check(r["skipped"] == 8, f"其余记为已跳过（{r['skipped']}）")

    # force 才会重跑
    r = client.post(url, json={"chapter_ids": ["v001-c0001", "v001-c0002"], "force": True}).json()
    check(r["pending"] == 2, f"force 时重跑（待跑 {r['pending']}）")

    # 混进不存在的 id 与路径穿越形态 → 一律忽略，不报错也不执行
    r = client.post(
        url, json={"chapter_ids": ["v001-c0003", "../evil", "v001-c9999", ""], "force": True}
    ).json()
    check(r["pending"] == 1, f"非法 id 被丢掉，只留合法的 1 章（实际 {r['pending']}）")
    cleanup()


def test_error_shape_is_uniform() -> None:
    print("错误形状统一（前端只需处理一种结构）")
    body = client.get("/api/works/根本没有这部").json()
    check("error" in body, "错误体含 error")
    check("detail" in body, "错误体含 detail")


# ── 模型任务：计划与启动 ────────────────────────────────────


def test_annotate_plan_is_free() -> None:
    """看计划不能产生任何模型调用，也不能产出任何标注文件。"""
    print("标注计划不调模型")
    import_test_work()
    r = client.get(f"/api/works/{TEST_WORK}/annotate/plan")
    check(r.status_code == 200, f"返回 200（实际 {r.status_code}）")
    d = r.json()
    check(d["pending"] == 8, f"待跑 8 章（实际 {d['pending']}）")
    check(d["estimate"]["total_tokens"] > 0, "给出了 token 估算")
    check(isinstance(d["estimate"].get("breakdown"), dict), "估算带计算过程")
    check(
        not (WS / TEST_WORK / "10-annotations").exists(),
        "看计划不会产出标注目录",
    )
    # 计划里必须说清密钥在不在，但不能把密钥本身带出来
    body = json.dumps(d, ensure_ascii=False)
    check("has_api_key" in d, "带 has_api_key 标记")
    check("sk-" not in body, "响应体里不含密钥")


def test_annotate_plan_limit() -> None:
    print("试跑范围生效")
    import_test_work()
    d = client.get(f"/api/works/{TEST_WORK}/annotate/plan?limit=3").json()
    check(d["pending"] == 3, f"limit=3 时只跑 3 章（实际 {d['pending']}）")


def test_annotate_status_before_run() -> None:
    print("没跑过时的状态")
    import_test_work()
    d = client.get(f"/api/works/{TEST_WORK}/annotate/status").json()
    check(d.get("running") is False, "不在运行")
    check(d.get("status") == "never", "标记为从未运行")


def test_report_without_annotations() -> None:
    """零标注也要能出报告：脚本轨小节照常，模型轨小节明写无数据。"""
    print("零标注也能出报告")
    import_test_work()
    r = client.get(f"/api/works/{TEST_WORK}/report")
    check(r.status_code == 200, f"返回 200（实际 {r.status_code}）")
    d = r.json()
    check(d["coverage"]["annotated"] == 0, "标注数为 0")
    check(bool(d["coverage"]["note"]), "明确说明了还没有标注")
    check(d["sections"]["length"]["available"] is True, "章节长度小节有数据")
    check(len(d["sections"]["length"]["points"]) == 8, "长度曲线 8 个点")
    check(
        d["sections"]["emotion_conflict"]["available"] is False,
        "情绪/冲突小节为无数据",
    )
    check(bool(d["sections"]["emotion_conflict"].get("reason")), "无数据要写明原因，不能只给空数组")
    check(
        (WS / TEST_WORK / "30-reports" / "latest.json").exists(),
        "报告已落盘",
    )
    cleanup()


def test_entities_plan_shows_progress_and_running() -> None:
    """实体统计界面靠这两个字段决定「还剩几块」和「要不要接回停止按钮」。

    分次跑是这一轮的改动：跑了一半、刷新页面之后，界面必须还能看出
    哪些块已完成、哪些没跑，否则用户只能靠再点一次「生成」去猜。
    """
    print("实体统计计划带进度与运行态")
    import_test_work()
    r = client.get(f"/api/works/{TEST_WORK}/entities/plan")
    check(r.status_code == 200, f"返回 200（实际 {r.status_code}）")
    d = r.json()
    prog = d.get("progress") or {}
    check(
        prog.get("total") == d.get("blocks") and bool(d.get("blocks")),
        f"进度里的总块数与计划一致（{prog.get('total')} / {d.get('blocks')}）",
    )
    check(prog.get("done") == 0, "还没跑过时已完成 0 块")
    check(prog.get("pending") == prog.get("total"), "还没跑过时全部记为「还没跑」")
    check(d.get("running") is False, "没在跑时 running 为 false")
    cleanup()


def test_entities_stop_without_job() -> None:
    """没有任务在跑时按停止：要如实说「没有任务」，不能假装停成功了。"""
    print("没任务时的停止请求")
    import_test_work()
    r = client.post(f"/api/works/{TEST_WORK}/entities/stop")
    check(r.status_code == 200, f"返回 200（实际 {r.status_code}）")
    d = r.json()
    check(d.get("ok") is False, "ok 为 false")
    check(bool(d.get("message")), "说清了为什么没停下来")
    cleanup()


def main() -> int:
    print("=" * 58)
    print("工作台接口自检")
    print("=" * 58)

    for fn in (
        test_health,
        test_index_served,
        test_shelf_does_not_fabricate_works,
        test_underscore_dirs_are_not_works,
        test_shelf_reflects_disk,
        test_work_detail_matches_manifest,
        test_chapters_paging_and_filter,
        test_unknown_work_404,
        test_path_traversal_blocked,
        test_archive_moves_not_deletes,
        test_archive_unknown_work_404,
        test_archive_restore_via_api,
        test_import_preview_does_not_write,
        test_import_commit_writes,
        test_upload_token_single_use,
        test_import_rejects_bad_token,
        test_import_rejects_empty_file,
        test_import_rejects_bad_pattern,
        test_error_shape_is_uniform,
        test_annotate_plan_is_free,
        test_annotate_plan_limit,
        test_annotate_status_before_run,
        test_report_without_annotations,
        test_chapter_detail_reads_text,
        test_chapter_raw_variant,
        test_chapter_unknown_id_404,
        test_chapter_list_shows_annotation_state,
        test_annotate_selected_chapters,
        test_entities_plan_shows_progress_and_running,
        test_entities_stop_without_job,
    ):
        fn()
        print()

    cleanup()
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
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
