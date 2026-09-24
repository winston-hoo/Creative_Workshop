"""本地 Web 工作台的服务端。

只绑 127.0.0.1：作品原文不出本机。这一条不是可选项。

接口契约（UI-P1 + UI-P2：书架、导入向导、作品概览、模型任务、体检报告）：

  GET    /api/health                     健康检查
  GET    /api/works                      书架列表
  GET    /api/works/{name}               作品概览
  GET    /api/works/{name}/chapters      章节列表（分页 / 筛选）
  GET    /api/works/{name}/chapters/{id} 章节正文（?raw=true 给清洗前副本）
  GET    /api/works/{name}/anomalies     异常与复核队列
  POST   /api/works/{name}/archive       移出书架（移动，不是删除）
  GET    /api/settings                   服务商与密钥设置（密钥只回掩码）
  PUT    /api/settings/providers/{id}/key    保存密钥
  DELETE /api/settings/providers/{id}/key    清除密钥
  POST   /api/settings/providers/{id}/test   测试连接（只发合成测试句）
  POST   /api/settings/providers/{id}/probe  完整探测并存档
  GET    /api/works/{name}/annotate/plan     标注计划与成本估算（不调模型）
  POST   /api/works/{name}/annotate/start    启动批量标注（用户点按钮才调用）
  POST   /api/works/{name}/annotate/stop     请求中止
  GET    /api/works/{name}/annotate/status   运行进度
  GET    /api/works/{name}/outline/plan      大纲计划（不调模型）
  POST   /api/works/{name}/outline/start     生成大纲（逐层归约，会花钱）
  GET    /api/works/{name}/entities/plan     实体统计计划（不调模型）
  POST   /api/works/{name}/entities/start    生成实体统计（会花钱）
  GET    /api/works/{name}/entities          最近一次实体统计
  GET    /api/works/{name}/outline           最近一次大纲
  GET    /api/works/{name}/report            生成并落盘体检报告
  POST   /api/import/upload              上传文件并返回切分预览（不落盘）
  POST   /api/import/commit              确认导入（落盘）
  DELETE /api/import/{token}             丢弃这次上传

设计上刻意不做的事：
  · 不引入数据库——文件系统是唯一真相源，manifest 现读现算
  · 不在接口里返回整章正文——预览只需要让人确认「切得对不对」
  · 不用内存缓存——避免出现「界面显示的」和「脚本读到的」不一致
  · 不做「一键跑完所有模型任务」——那会破坏「模型调用必须显式」这条原则
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from server import services  # noqa: E402
from workshop import __version__  # noqa: E402
from workshop.entities import DEFAULT_BLOCK_SIZE as _ENTITY_BLOCK  # noqa: E402
from workshop.outline import DEFAULT_BLOCK_SIZE as DEFAULT_BLOCK_SIZE  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
PATHS = services.Paths(root=ROOT)

app = FastAPI(title="小说创作工坊", version=__version__, docs_url="/api/docs")


@app.middleware("http")
async def no_cache_for_ui(request, call_next):
    """界面文件强制重新校验。

    没有这个头，改了 app.js 之后浏览器可能继续用缓存里的旧版本，
    表现成「代码明明改了但界面没变」——这个问题排查起来极费时间，
    因为两边看起来都对。`no-cache` 不是不缓存，而是每次回源校验，
    配合 ETag 命中时仍是 304，开销可以忽略。
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ── 入参校验 ────────────────────────────────────────────────


def safe_name(name: str) -> str:
    """作品名会拼进路径，必须挡住路径穿越；同时统一约定「非作品」目录。

    允许中文、字母、数字、空格、下划线与短横线；其余一律拒绝。

    拒绝 `_` / `.` 开头：这两个前缀是「非作品目录」的约定（归档、试验产物）。
    必须在这里统一挡——只在列表里过滤是不够的，那样书架上看不到但能直接访问，
    两边不一致反而更让人困惑。
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(400, "作品名不能为空")
    if len(cleaned) > 80:
        raise HTTPException(400, "作品名过长")
    if cleaned.startswith(("_", ".")):
        raise HTTPException(400, "作品名不能以下划线或点开头（这两个前缀保留给归档与临时目录）")
    if any(bad in cleaned for bad in ("/", "\\", "..", ":", "*", "?", '"', "<", ">", "|")):
        raise HTTPException(400, "作品名含非法字符")
    return cleaned


class CommitRequest(BaseModel):
    token: str = Field(..., description="上传时返回的 token")
    work: str = Field(..., description="作品名")
    strip_ads: bool = False
    strip_repeated: bool = False


class AnnotateStartRequest(BaseModel):
    limit: int | None = Field(None, ge=1, description="只跑前 N 章待跑章节，试跑用")
    concurrency: int | None = Field(None, ge=1, le=16)
    ledger_enabled: bool = True
    allow_over_budget: bool = False
    provider: str | None = None
    model: str | None = None
    # 只跑勾选出来的这几章。给了它，limit 就不再生效。
    chapter_ids: list[str] | None = None
    # 连已标注的也重跑。改了提示词或作品配置之后需要它——
    # 否则新旧口径的标注会混在一个数据集里。
    force: bool = False


def _clean_chapter_ids(raw: list[str] | None) -> list[str] | None:
    """章节 id 会拼进路径，按命名规则收一下，别的形态一律丢掉。"""
    if not raw:
        return None
    import re

    pattern = re.compile(r"^v\d{3}-(c|x)\d{4}[a-z]?$")
    kept = [x.strip() for x in raw if isinstance(x, str) and pattern.match(x.strip())]
    return kept or None


# ── 书架 ────────────────────────────────────────────────────


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "root": str(ROOT)}


@app.get("/api/works")
def api_works() -> list[dict]:
    return services.list_works(PATHS)


@app.get("/api/works/{name}")
def api_work_detail(name: str) -> dict:
    detail = services.work_detail(PATHS, safe_name(name))
    if detail is None:
        raise HTTPException(404, f"找不到作品「{name}」，或它还没完成导入")
    return detail


@app.get("/api/works/{name}/chapters")
def api_chapters(
    name: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    only: str = Query(
        "all",
        pattern="^(all|unnumbered|repaired|annotated|unannotated|review)$",
    ),
) -> dict:
    return services.list_chapters(PATHS, safe_name(name), offset=offset, limit=limit, only=only)


@app.get("/api/works/{name}/chapters/{chapter_id}")
def api_chapter_detail(name: str, chapter_id: str, raw: bool = Query(False)) -> dict:
    """读一章的完整内容。

    `raw=true` 给清洗前的原文副本。清洗前后各存一份，
    所以两者都能看到——对照着看才能确认清洗有没有动过正文。
    """
    detail = services.chapter_detail(PATHS, safe_name(name), chapter_id, raw=raw)
    if detail is None:
        raise HTTPException(404, f"找不到章节「{chapter_id}」")
    return detail


@app.get("/api/works/{name}/anomalies")
def api_anomalies(name: str) -> list[dict]:
    return services.list_anomalies(PATHS, safe_name(name))


@app.post("/api/works/{name}/archive")
def api_archive_work(name: str) -> dict:
    """把作品移出书架。

    移动而不是删除：整个工作区原样搬到 workspaces/_archive/，想恢复搬回来即可。
    界面上这一步有二次确认。
    """
    try:
        return services.archive_work(PATHS, safe_name(name))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"移动失败：{exc}") from exc


# ── 模型任务：批量标注 ──────────────────────────────────────
#
# 「模型调用必须显式」落在接口形状上就是：计划与执行是两个端点。
# 看计划永远免费，执行只在用户点了按钮之后发生。


@app.get("/api/providers")
def api_providers() -> dict:
    return services.providers_overview(PATHS)


@app.get("/api/works/{name}/annotate/plan")
def api_annotate_plan(
    name: str,
    limit: int | None = Query(None, ge=1),
    concurrency: int | None = Query(None, ge=1, le=16),
    ledger_enabled: bool = Query(True),
    force: bool = Query(False),
    provider: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """标注计划与成本估算。**这里不发起任何模型调用。**"""
    try:
        return services.annotation_plan(
            PATHS,
            safe_name(name),
            limit=limit,
            concurrency=concurrency,
            ledger_enabled=ledger_enabled,
            provider_id=provider,
            model_id=model,
            force=force,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/annotate/plan/selection")
def api_annotate_plan_selection(name: str, req: AnnotateStartRequest) -> dict:
    """按勾选的章节出计划。GET 装不下 id 列表，所以走 POST。仍然不调模型。"""
    try:
        return services.annotation_plan(
            PATHS,
            safe_name(name),
            concurrency=req.concurrency,
            ledger_enabled=req.ledger_enabled,
            provider_id=req.provider,
            model_id=req.model,
            only_ids=_clean_chapter_ids(req.chapter_ids),
            force=req.force,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/annotate/start")
def api_annotate_start(name: str, req: AnnotateStartRequest) -> dict:
    """启动批量标注。会真的产生费用，所以只能由用户主动触发。"""
    try:
        return services.start_annotation(
            PATHS,
            safe_name(name),
            limit=req.limit,
            concurrency=req.concurrency,
            ledger_enabled=req.ledger_enabled,
            allow_over_budget=req.allow_over_budget,
            provider_id=req.provider,
            model_id=req.model,
            only_ids=_clean_chapter_ids(req.chapter_ids),
            force=req.force,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/annotate/stop")
def api_annotate_stop(name: str) -> dict:
    return services.stop_annotation(safe_name(name))


@app.get("/api/works/{name}/annotate/status")
def api_annotate_status(name: str) -> dict:
    return services.annotation_status(PATHS, safe_name(name))


# ── 设置：服务商与密钥 ──────────────────────────────────────


class KeyRequest(BaseModel):
    value: str = Field(..., description="密钥明文。只写不读——接口永远不会把它回显出来")


class TestRequest(BaseModel):
    model: str | None = None


@app.get("/api/settings")
def api_settings() -> dict:
    """服务商配置概览。**只返回密钥的状态与掩码，不返回明文。**"""
    return services.settings_overview(PATHS)


@app.put("/api/settings/providers/{provider_id}/key")
def api_save_key(provider_id: str, req: KeyRequest) -> dict:
    try:
        outcome = services.save_provider_key(PATHS, safe_name(provider_id), req.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not outcome.get("ok"):
        # 写入被拒（值太短、被环境变量占用等）就用 400。
        # 一律回 200 + ok:false 会让「保存失败」看起来像「保存成功」。
        raise HTTPException(400, str(outcome.get("message") or "保存失败"))
    return outcome


@app.delete("/api/settings/providers/{provider_id}/key")
def api_clear_key(provider_id: str) -> dict:
    try:
        return services.clear_provider_key(PATHS, safe_name(provider_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/settings/providers/{provider_id}/test")
def api_test_provider(provider_id: str, req: TestRequest) -> dict:
    """测试连接：端点、鉴权、模型名。只发合成测试句，不碰作品原文。"""
    try:
        return services.test_provider(PATHS, safe_name(provider_id), model_id=req.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/settings/providers/{provider_id}/probe")
def api_probe_provider(provider_id: str, req: TestRequest) -> dict:
    """完整探测并存档（P1-P5 + 基准测）。比测试连接慢，但会留下可对比的报告。"""
    try:
        return services.probe_provider(PATHS, safe_name(provider_id), model_id=req.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 标注总览 ────────────────────────────────────────────────


@app.get("/api/works/{name}/annotations")
def api_annotations(
    name: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    only: str = Query("all", pattern="^(all|review|ok)$"),
) -> dict:
    """已标注章节的一览表。每行是「这一章标成了什么样」，不是原文。"""
    return services.list_annotations(PATHS, safe_name(name), offset=offset, limit=limit, only=only)


# ── 实体统计 ────────────────────────────────────────────────


@app.get("/api/works/{name}/entities/plan")
def api_entities_plan(
    name: str,
    block_size: int = Query(DEFAULT_BLOCK_SIZE, ge=5, le=200),
    provider: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """实体统计计划。免费，不调模型。"""
    try:
        return services.entities_plan(
            PATHS, safe_name(name), block_size=block_size, provider_id=provider, model_id=model
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/entities/start")
def api_entities_start(
    name: str,
    block_size: int = Query(DEFAULT_BLOCK_SIZE, ge=5, le=200),
) -> dict:
    try:
        return services.start_entities(PATHS, safe_name(name), block_size=block_size)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/entities/status")
def api_entities_status(name: str) -> dict:
    return services.entities_status(PATHS, safe_name(name))


@app.get("/api/works/{name}/entities")
def api_entities(name: str) -> dict:
    latest = services.entities_latest(PATHS, safe_name(name))
    if latest is None:
        # 「还没生成过」是正常状态，用 200 + exists:false，不用 404——
        # 否则浏览器会在控制台留一条假错误。
        return {"exists": False, "merged": {}, "blocks": [], "errors": []}
    latest["exists"] = True
    return latest


# ── 大纲 ────────────────────────────────────────────────────


@app.get("/api/works/{name}/outline/plan")
def api_outline_plan(
    name: str,
    block_size: int = Query(DEFAULT_BLOCK_SIZE, ge=5, le=200),
    provider: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """大纲计划。免费，不调模型。"""
    try:
        return services.outline_plan(
            PATHS, safe_name(name), block_size=block_size, provider_id=provider, model_id=model
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/outline/start")
def api_outline_start(
    name: str,
    block_size: int = Query(DEFAULT_BLOCK_SIZE, ge=5, le=200),
    model: str | None = Query(None),
) -> dict:
    try:
        return services.start_outline(
            PATHS, safe_name(name), block_size=block_size, model_id=model
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/outline/status")
def api_outline_status(name: str) -> dict:
    return services.outline_status(PATHS, safe_name(name))


@app.get("/api/works/{name}/outline")
def api_outline(name: str) -> dict:
    """最近一次大纲。

    「还没生成过」用 200 + exists=false 表达，不用 404——
    它是这个接口的正常状态之一，不是错误。用 404 的话浏览器会在控制台
    留一条红色报错，而假错误会训练人忽略真错误。
    """
    latest = services.outline_latest(PATHS, safe_name(name))
    if latest is None:
        return {"exists": False, "outline": None, "blocks": [], "errors": []}
    latest["exists"] = True
    return latest


# ── 结构体检报告 ────────────────────────────────────────────


@app.get("/api/works/{name}/report")
def api_report(
    name: str, stale_threshold: int = Query(30, ge=1, le=500)
) -> dict:
    """生成体检报告并落盘。纯本地聚合，不调模型。

    零标注也能出报告：脚本轨小节照常计算，模型轨小节会明写「无数据」。
    """
    try:
        return services.health_report(PATHS, safe_name(name), stale_threshold=stale_threshold)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 导入向导 ────────────────────────────────────────────────


@app.post("/api/import/upload")
async def api_upload(
    file: UploadFile = File(...),
    work: str = Form(""),
    strip_ads: bool = Form(False),
    strip_repeated: bool = Form(False),
) -> dict:
    """上传并切分预览。**不落盘**——导入流程的第 2 步必须让人先看到结果再确认。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的")
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "文件超过 200MB，请先拆分")

    work_name = safe_name(work or Path(file.filename or "未命名").stem)
    token, path = services.stage_upload(PATHS, file.filename or "original.txt", data)

    try:
        patterns = services.load_work_patterns(PATHS, work_name)
        result = services.do_ingest(
            PATHS,
            path,
            work_name=work_name,
            strip_ads=strip_ads,
            strip_repeated=strip_repeated,
            extra_patterns=patterns,
        )
    except ValueError as exc:
        services.cleanup_upload(PATHS, token)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        services.cleanup_upload(PATHS, token)
        raise HTTPException(500, f"切分失败：{exc}") from exc

    payload = services.preview_payload(result)
    payload["token"] = token
    payload["work"] = work_name
    payload["patterns_loaded"] = len(patterns)
    return payload


@app.post("/api/import/commit")
def api_commit(req: CommitRequest) -> dict:
    """确认导入，落盘。必须先经过 upload 拿到 token。"""
    work_name = safe_name(req.work)
    found = services.resolve_upload(PATHS, req.token)
    if found is None:
        raise HTTPException(404, "上传已过期或被丢弃，请重新上传")
    source_path, _meta = found

    patterns = services.load_work_patterns(PATHS, work_name)
    try:
        result = services.do_ingest(
            PATHS,
            source_path,
            work_name=work_name,
            strip_ads=req.strip_ads,
            strip_repeated=req.strip_repeated,
            extra_patterns=patterns,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"导入失败：{exc}") from exc

    integrity = result.get("integrity") or {}
    if not integrity.get("passed"):
        # 门禁不通过就不落盘。任何「勉强导入」都会把问题带到下游。
        raise HTTPException(
            409,
            {
                "message": "完整性核验未通过，已阻止导入",
                "integrity": integrity,
            },
        )

    outcome = services.commit_ingest(PATHS, result, work_name)
    services.cleanup_upload(PATHS, req.token)
    return outcome


@app.delete("/api/import/{token}")
def api_discard(token: str) -> dict:
    services.cleanup_upload(PATHS, token)
    return {"ok": True}


# ── 静态页面 ────────────────────────────────────────────────


@app.get("/")
def index() -> FileResponse:
    target = STATIC_DIR / "index.html"
    if not target.exists():
        raise HTTPException(500, "界面文件缺失")
    return FileResponse(target)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException) -> JSONResponse:
    """统一错误形状，前端只需处理一种结构。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if isinstance(exc.detail, str) else "请求失败", "detail": exc.detail},
    )
