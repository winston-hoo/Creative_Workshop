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
  POST   /api/works/{name}/entities/start    生成实体统计（会花钱，?limit=N 可分批）
  POST   /api/works/{name}/entities/stop     请求停止（已完成的块保留）
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

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 打包成 exe 后由 launcher.py 从环境变量指定数据根：
# exe 里的项目目录是只读的临时解包目录，作品不能落在那里。
ROOT = Path(os.environ.get("WORKSHOP_ROOT") or Path(__file__).resolve().parent.parent).resolve()
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


@app.get("/api/archive")
def api_list_archived() -> dict:
    """归档区里的作品列表（含章节数/标注数），供恢复入口使用。"""
    items = services.list_archived(PATHS)
    return {"items": items, "total": len(items)}


class RestoreRequest(BaseModel):
    name: str = Field(..., description="归档条目名（可能是 作品名 或 作品名.时间戳）")


@app.post("/api/archive/restore")
def api_restore_work(req: RestoreRequest) -> dict:
    """把归档作品搬回书架。书架上已有同名时拒绝覆盖。"""
    try:
        return services.restore_work(PATHS, req.name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"恢复失败：{exc}") from exc


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


class ProviderRequest(BaseModel):
    name: str = Field("", description="自定义名称，如「我的中转站」")
    base_url: str = Field("", description="OpenAI 兼容端点地址")
    models: list[str] = Field(default_factory=list, description="模型列表，每行「别名 | 模型id」")
    api_key: str = Field("", description="密钥明文，只写入不回显")
    requests_per_minute: int | None = Field(None, ge=0, description="每分钟请求上限，0/None 表示不限制")
    tokens_per_minute: int | None = Field(None, ge=0, description="每分钟 token 上限，0/None 表示不限制")
    enabled: bool = True


@app.post("/api/settings/providers")
def api_create_provider(req: ProviderRequest) -> dict:
    """设置页新增服务商（中转站、本地兼容端点等）。"""
    try:
        return services.create_provider(
            PATHS,
            name=req.name,
            base_url=req.base_url,
            models=req.models,
            api_key=req.api_key,
            requests_per_minute=req.requests_per_minute,
            tokens_per_minute=req.tokens_per_minute,
            enabled=req.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/settings/providers/{provider_id}")
def api_update_provider(provider_id: str, req: ProviderRequest) -> dict:
    try:
        return services.update_provider(
            PATHS,
            safe_name(provider_id),
            name=req.name,
            base_url=req.base_url,
            models=req.models,
            api_key=req.api_key,
            requests_per_minute=req.requests_per_minute,
            tokens_per_minute=req.tokens_per_minute,
            enabled=req.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/settings/providers/{provider_id}")
def api_delete_provider(provider_id: str) -> dict:
    try:
        return services.delete_provider(PATHS, safe_name(provider_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class BindingRequest(BaseModel):
    provider: str = Field(..., description="默认服务商 id")
    model: str | None = Field(None, description="默认模型 id，缺省时按服务商默认规则选")


@app.put("/api/settings/binding")
def api_save_binding(req: BindingRequest) -> dict:
    """设置「默认模型」：没单独指定模型的任务都用它。"""
    try:
        return services.save_default_binding(PATHS, provider=req.provider, model=req.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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


# ── 标注台 ────────────────────────────────────────────────
#
# UI-P3：模型先跑，人只修正低置信度章节。三栏布局的全部接口。

@app.get("/api/works/{name}/annotator/fields")
def api_annotator_fields(name: str) -> dict:
    """标注右栏的字段定义。"""
    return services.annotator_fields(PATHS)


@app.get("/api/works/{name}/annotator/chapters")
def api_annotator_chapters(name: str) -> dict:
    """标注左栏：全部章节 + 状态。"""
    try:
        return services.annotator_chapters(PATHS, safe_name(name))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/annotator/chapters/{chapter_id}")
def api_annotator_chapter(name: str, chapter_id: str) -> dict:
    """标注中栏：单章正文 + 当前字段。"""
    detail = services.annotator_chapter_detail(PATHS, safe_name(name), chapter_id)
    if detail is None:
        raise HTTPException(404, f"找不到章节「{chapter_id}」")
    return detail


class ReviewSaveRequest(BaseModel):
    fields: dict[str, Any] | None = Field(None, description="人工修正后的字段（只接受已有字段）")
    status: str | None = Field(None, pattern="^(ok|needs_review)?$")
    reason: str = Field("", description="修正说明，会记入 issues")


@app.put("/api/works/{name}/annotator/chapters/{chapter_id}")
def api_annotator_save(name: str, chapter_id: str, req: ReviewSaveRequest) -> dict:
    """标注台保存人工修正。"""
    try:
        return services.save_annotation_review(
            PATHS, safe_name(name), chapter_id, fields=req.fields, status=req.status, reason=req.reason
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 知识库 ────────────────────────────────────────────────


@app.get("/api/works/{name}/kb")
def api_kb(name: str) -> dict:
    """最近一次知识库。还没构建过用 exists:false，正常状态不是错误。"""
    latest = services.kb_latest(PATHS, safe_name(name))
    if latest is None:
        return {"exists": False}
    latest["exists"] = True
    return latest


@app.post("/api/works/{name}/kb/build")
def api_kb_build(name: str) -> dict:
    """构建知识库（K1/K2/K3）。纯脚本，不调模型，随时可点。"""
    try:
        return services.build_kb(PATHS, safe_name(name))
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 重写工坊 ────────────────────────────────────────────────


@app.get("/api/works/{name}/rewrite/plan")
def api_rewrite_plan(
    name: str,
    provider: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """改写台计划：指令类型与章节列表。免费，不调模型。"""
    try:
        return services.rewrite_plan(PATHS, safe_name(name), provider_id=provider, model_id=model)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


class RewriteStartRequest(BaseModel):
    chapter_id: str = Field(..., description="要改写的章节 id")
    directive: str = Field("调节奏", description="指令类型：改视角/扩写/精简/调节奏/强化动机")
    instruction: str = Field("", description="补充说明")
    model: str | None = None


@app.post("/api/works/{name}/rewrite/start")
def api_rewrite_start(name: str, req: RewriteStartRequest) -> dict:
    """执行一章重写（会调模型、产生费用）。"""
    try:
        return services.start_rewrite(
            PATHS, safe_name(name),
            chapter_id=req.chapter_id,
            directive=req.directive,
            instruction=req.instruction,
            model_id=req.model,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/rewrite/{chapter_id}")
def api_rewrite_record(name: str, chapter_id: str) -> dict:
    """某一章的改写记录。没有就 404。"""
    record = services.rewrite_chapter_record(PATHS, safe_name(name), chapter_id)
    if record is None:
        raise HTTPException(404, f"章节「{chapter_id}」还没有改写记录")
    return record


class RewriteAcceptRequest(BaseModel):
    force: bool = False


@app.post("/api/works/{name}/rewrite/{chapter_id}/accept")
def api_rewrite_accept(name: str, chapter_id: str, req: RewriteAcceptRequest) -> dict:
    """接受改写：写入新版本文件（原稿保留）。G2 闸门：有阻断问题先拒绝。"""
    try:
        return services.accept_rewrite_record(
            PATHS, safe_name(name), chapter_id, force=req.force
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 题材库 / 对比 / 融合 ────────────────────────────────────


@app.get("/api/genres")
def api_genres() -> dict:
    """题材库总览：作品 → 题材 映射 + 各题材规则状态。"""
    return services.genre_index(PATHS)


class GenreRequest(BaseModel):
    work: str = Field(..., description="作品名")
    genre: str = Field("", description="题材名，空串移除")


@app.put("/api/genres/assign")
def api_genre_assign(req: GenreRequest) -> dict:
    """把作品登记到某题材。"""
    try:
        return services.set_work_genre(PATHS, safe_name(req.work), req.genre)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/genres/{genre}")
def api_genre_get(genre: str) -> dict:
    """某题材的已聚合规则。没有则 404。"""
    try:
        rules = services.genre_rules(PATHS, safe_name(genre))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if rules is None:
        raise HTTPException(404, f"题材「{genre}」还没有聚合规则")
    return rules


@app.post("/api/genres/{genre}/aggregate")
def api_genre_aggregate(genre: str) -> dict:
    """聚合某题材的规则（M6）。纯脚本，不调模型。"""
    try:
        return services.aggregate_genre_rules(PATHS, safe_name(genre))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/compare")
def api_compare() -> dict:
    """最近一次对比分析。没跑过用 exists:false。"""
    latest = services.compare_latest(PATHS)
    if latest is None:
        return {"exists": False}
    latest["exists"] = True
    return latest


class CompareRequest(BaseModel):
    work: str = Field(..., description="基准作品名")
    genre: str = Field(..., description="题材名")


@app.post("/api/compare/build")
def api_compare_build(req: CompareRequest) -> dict:
    """构建三份对比报告（M8）。"""
    try:
        return services.build_compare_report(PATHS, safe_name(req.work), req.genre)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fusion")
def api_fusion() -> dict:
    """最近一次融合骨架。没生成过用 exists:false。"""
    latest = services.fusion_latest(PATHS)
    if latest is None:
        return {"exists": False}
    latest["exists"] = True
    return latest


class FusionRequest(BaseModel):
    source_works: list[str] | None = None
    seed: int | None = None


@app.post("/api/fusion/generate")
def api_fusion_generate(req: FusionRequest) -> dict:
    """生成一个新故事骨架（M5）。纯脚本，不调模型。"""
    try:
        return services.generate_fusion(PATHS, req.source_works or None, req.seed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    provider: str | None = Query(None),
    model: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=500),
) -> dict:
    """开始/接着跑实体统计。

    `limit` 限制本次最多跑几块（已完成的块直接复用，不计入），
    长任务因此可以分次做完，不必一口气跑到底。
    """
    try:
        return services.start_entities(
            PATHS, safe_name(name), block_size=block_size, provider_id=provider,
            model_id=model, limit=limit,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/entities/stop")
def api_entities_stop(name: str) -> dict:
    """请求停止。当前块跑完即停，已完成的块保留，下次接着跑不重复花钱。"""
    return services.stop_entities(safe_name(name))


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


# ── M9 创作台 ───────────────────────────────────────────────
#
# 只有读、写、校验三类接口。**故意没有"一键生成三层"**：创作要一步步调，
# 一次吐一大堆，改起来等于全废。将来接模型时也只做单份生成 + 计划预览 + 显式确认。


class CreationNewBody(BaseModel):
    name: str
    genre: str = ""
    logline: str = ""
    protagonist: str = ""
    core_motive: str = ""


class CreationWriteBody(BaseModel):
    text: str | None = None
    # 表单保存走结构化：界面给对象，服务端写 YAML。
    # 和 text 二选一——text 保留手写注释，data 是机器生成（会丢注释，文件头有说明）。
    data: dict | None = None


class AssistBody(BaseModel):
    message: str = ""
    history: list[dict] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    # none 不注入 / k3 只结构指纹 / full 全素材（默认）
    level: str = "full"
    # setting / volume / brief —— 三层共用同一个提案引擎
    layer: str = "setting"
    key: str = ""
    provider: str | None = None
    model: str | None = None
    max_tokens: int = 2000
    # 空 = 起草；writeback = 回填（拿这一章的正文提事实，message 会被忽略）
    task: str = ""


class AssistApplyBody(BaseModel):
    document: dict
    proposals: list[dict] = Field(default_factory=list)
    accepted: list[int] = Field(default_factory=list)
    layer: str = "setting"


@app.post("/api/creation/new")
def api_creation_new(body: CreationNewBody) -> dict:
    """新建原创工作区。已存在则拒绝——里面可能已经有几十万字稿子。"""
    try:
        return services.creation_new(
            PATHS,
            safe_name(body.name),
            genre=body.genre,
            logline=body.logline,
            protagonist=body.protagonist,
            core_motive=body.core_motive,
        )
    except FileExistsError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/creation")
def api_creation_overview(name: str) -> dict:
    """创作台首页：三层完成度 + 逐卷逐章清单。纯脚本。"""
    try:
        return services.creation_overview(PATHS, safe_name(name))
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/creation/assist/plan")
def api_assist_plan(
    name: str,
    refs: str = Query("", description="逗号分隔的参照作品名"),
    level: str = Query("full", description="none / k3 / full"),
    layer: str = Query("setting", description="setting / volume / brief"),
    key: str = Query("", description="volume 的卷号或 brief 的章节号"),
    provider: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """助手的免费计划：多少 token、多少钱、注入了多少素材。**不调模型。**"""
    try:
        return services.setting_assist_plan(
            PATHS, safe_name(name), refs=[r for r in refs.split(",") if r], level=level,
            layer=layer, key=key, provider_id=provider, model_id=model,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/creation/assist")
def api_assist(name: str, body: AssistBody) -> dict:
    """问一次助手（**会花钱**）。只返回提案，不写任何文件。

    `task="writeback"` 时不看 message：拿 key（章节号）那一章的正文去回填设定集。
    """
    try:
        return services.run_setting_assist(
            PATHS, safe_name(name), message=body.message, history=body.history,
            refs=body.refs, level=body.level, layer=body.layer, key=body.key,
            provider_id=body.provider, model_id=body.model, max_tokens=body.max_tokens,
            task=body.task,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/creation/assist/apply")
def api_assist_apply(name: str, body: AssistApplyBody) -> dict:
    """把勾中的提案并进草稿。纯脚本，不落盘——落盘是作者点保存之后的事。"""
    try:
        services._creation_work_dir(PATHS, safe_name(name))
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
    return services.apply_setting_proposals(
        document=body.document, proposals=body.proposals,
        accepted=body.accepted, layer=body.layer,
    )


class DraftGenerateBody(BaseModel):
    refs: list[str] = Field(default_factory=list)
    level: str = "k3"
    provider: str | None = None
    model: str | None = None
    max_tokens: int = 8000
    temperature: float = 0.8


class DraftWriteBody(BaseModel):
    text: str


@app.get("/api/works/{name}/creation/draft/{chapter_id}/plan")
def api_draft_plan(
    name: str, chapter_id: str,
    refs: str = Query(""),
    level: str = Query("k3"),
    provider: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """写这一章的免费计划。**不调模型。** 设定集或指令有阻断时 ready=False。"""
    try:
        return services.draft_plan(
            PATHS, safe_name(name), chapter_id,
            refs=[r for r in refs.split(",") if r], level=level,
            provider_id=provider, model_id=model,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/creation/draft/{chapter_id}")
def api_draft_generate(name: str, chapter_id: str, body: DraftGenerateBody) -> dict:
    """写这一章（**会花钱**）。只出一章，跑完落盘并自检，不往下串。"""
    try:
        return services.run_draft(
            PATHS, safe_name(name), chapter_id, refs=body.refs, level=body.level,
            provider_id=body.provider, model_id=body.model,
            max_tokens=body.max_tokens, temperature=body.temperature,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/creation/draft/{chapter_id}")
def api_draft_read(name: str, chapter_id: str) -> dict:
    """读已写的正文，并**按当前指令重跑一次自检**（指令改过，结论要跟着变）。"""
    try:
        return services.read_draft(PATHS, safe_name(name), chapter_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/works/{name}/creation/draft/{chapter_id}")
def api_draft_write(name: str, chapter_id: str, body: DraftWriteBody) -> dict:
    """作者手改过的正文存回去。空文本不许覆盖有内容的文件。"""
    try:
        return services.write_draft_text(PATHS, safe_name(name), chapter_id, body.text)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


class KbImportBody(BaseModel):
    ref: str
    section: str
    keys: list[str] = Field(default_factory=list)
    document: dict
    layer: str = "setting"
    subject: str = ""


@app.get("/api/works/{name}/creation/foreshadows")
def api_foreshadows(name: str) -> dict:
    """伏笔账本：埋了哪些、推到哪一步、哪些该收还没收。纯脚本，零成本。"""
    try:
        return {"ledger": services.foreshadow_ledger(PATHS, safe_name(name))}
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/creation/kb/{ref}")
def api_kb_browse(name: str, ref: str, section: str = Query("characters"),
                  q: str = Query("")) -> dict:
    """翻某一本已入库作品的知识库（人物/势力/能力/地点/术语/关系）。纯读，零成本。"""
    try:
        services._creation_work_dir(PATHS, safe_name(name))
        return services.kb_browse(PATHS, ref, section, q)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/creation/kb-import")
def api_kb_import(name: str, body: KbImportBody) -> dict:
    """把勾中的知识库条目转成提案并进草稿。**不调模型，不落盘。**"""
    try:
        services._creation_work_dir(PATHS, safe_name(name))
        return services.kb_import(
            PATHS, safe_name(name), body.ref, body.section,
            keys=body.keys, document=body.document, layer=body.layer, subject=body.subject,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/works/{name}/creation/{kind}")
def api_creation_read(name: str, kind: str, key: str = Query("")) -> dict:
    """读一份原文 + 校验。kind = setting / volume / brief。"""
    try:
        return services.creation_read(PATHS, safe_name(name), kind, key)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/works/{name}/creation/{kind}")
def api_creation_write(name: str, kind: str, body: CreationWriteBody, key: str = Query("")) -> dict:
    """保存一份原文。解析不了就不落盘，把错误回给界面。"""
    try:
        work = safe_name(name)
        if body.data is not None:
            if kind not in services.CREATION_KINDS:
                raise ValueError(f"不认识的层：{kind}")
            return services.creation_write_setting_data(PATHS, work, body.data, kind=kind, key=key)
        if body.text is None:
            raise ValueError("要么给 text（原文），要么给 data（结构化）")
        return services.creation_write(PATHS, work, kind, key, body.text)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/works/{name}/creation/seed-briefs")
def api_creation_seed_briefs(name: str, vol: int | None = Query(None, ge=1, le=9999)) -> dict:
    """按卷表播种**空白**逐章指令骨架。不调模型，不覆盖已存在的文件。"""
    try:
        return services.creation_seed_briefs(PATHS, safe_name(name), vol)
    except FileNotFoundError as exc:
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
