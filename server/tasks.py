"""生成任务注册表：进度、取消、重启后的中断标记。

## 为什么不用 Celery/Redis

本地单用户、一次只写一章。引入队列服务只会让「换个环境就起不来」，
换来的收益一个都没有。这里要的只是：一个能查进度的 task id、一个能真正
让服务端停下的取消信号、以及重启之后别把上次没跑完当成还在跑。

## 取消为什么必须传进模型调用层

前端断掉连接不代表上游停止：请求还在跑，token 还在烧。所以 `cancel_event`
一路传到 `llm.chat_stream`，每读到一行检查一次，置位就关掉响应并抛
`GenerationCancelled`。这是「真取消」与「前端假停」的唯一区别。

## 持久化

每个任务一份 JSON，落在 `workspaces/<作品>/90-draft/.tasks/<id>.json`。
没有内存数据库，进程被杀之后重启能读出上次状态，并把 running/cancelling
统一标成 `interrupted`——**不能让它看起来还在写**。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from workshop.errors import describe_error

TASK_SCHEMA = "generation-task-v1"
TERMINAL = {"done", "failed", "cancelled", "interrupted"}

_registry: dict[str, "Task"] = {}
_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def tasks_dir(work_dir: str | Path) -> Path:
    return Path(work_dir) / "90-draft" / ".tasks"


@dataclass
class Task:
    task_id: str
    work: str
    chapter_id: str
    kind: str
    path: Path
    status: str = "running"
    stage: str = "排队"
    stage_index: int = 0
    total_stages: int = 4
    round: int = 0
    max_rounds: int = 0
    message: str = ""
    chars: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    beats_count: int = 0
    candidate_id: str = ""
    error: str = ""
    error_code: str = ""
    action: str = ""
    retryable: bool = False
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    finished_at: str = ""
    started_ms: float = 0.0
    elapsed_ms: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_SCHEMA,
            "task_id": self.task_id,
            "work": self.work,
            "chapter_id": self.chapter_id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "total_stages": self.total_stages,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "message": self.message,
            "chars": self.chars,
            "calls": self.calls,
            "beats_count": self.beats_count,
            "candidate_id": self.candidate_id,
            "error": self.error,
            "error_code": self.error_code,
            "action": self.action,
            "retryable": self.retryable,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "result": self.result,
            "cancellable": self.status in ("running", "cancelling"),
        }


def _persist(task: Task) -> None:
    task.updated_at = _now_iso()
    task.path.parent.mkdir(parents=True, exist_ok=True)
    payload = task.public()
    payload["schema_version"] = TASK_SCHEMA
    tmp = task.path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(task.path)


def create(
    paths: Any,
    *,
    work: str,
    chapter_id: str,
    kind: str = "draft",
    total_stages: int = 4,
    max_rounds: int = 0,
) -> Task:
    task_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    work_dir = paths.work_dir(work)
    task = Task(
        task_id=task_id,
        work=work,
        chapter_id=chapter_id,
        kind=kind,
        path=tasks_dir(work_dir) / f"{task_id}.json",
        total_stages=total_stages,
        max_rounds=max_rounds,
        stage="排队",
        stage_index=0,
    )
    with _lock:
        _registry[task_id] = task
        _persist(task)
    return task


def update(task: Task, **fields: Any) -> Task:
    if "error_code" in fields and fields["error_code"]:
        info = describe_error(fields["error_code"], str(fields.get("error") or ""))
        fields.setdefault("action", str(info["action"]))
        fields.setdefault("retryable", bool(info["retryable"]))
    if task.started_ms and "elapsed_ms" not in fields:
        import time

        # 进度面板要在运行中显示耗时，不能等到 finish 才第一次有值。
        fields["elapsed_ms"] = round((time.monotonic() - task.started_ms) * 1000, 1)
    with _lock:
        for key, value in fields.items():
            setattr(task, key, value)
        _persist(task)
    return task


def get(paths: Any, task_id: str) -> Task | None:
    with _lock:
        task = _registry.get(task_id)
        if task is not None:
            return task
    # 重启后内存里没有：按 id 到各作品目录里找
    entries = list(paths.workspaces.iterdir()) if paths.workspaces.exists() else []
    for entry in entries:
        if not entry.is_dir():
            continue
        path = tasks_dir(entry) / f"{task_id}.json"
        if path.exists():
            data = _load(path)
            if data:
                task = _from_disk(data, path)
                with _lock:
                    _registry[task_id] = task
                return task
    return None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _from_disk(data: dict[str, Any], path: Path) -> Task:
    task = Task(
        task_id=str(data.get("task_id") or path.stem),
        work=str(data.get("work") or ""),
        chapter_id=str(data.get("chapter_id") or ""),
        kind=str(data.get("kind") or "draft"),
        path=path,
    )
    for key in (
        "status", "stage", "stage_index", "total_stages", "round", "max_rounds",
        "message", "chars", "beats_count", "candidate_id", "error", "error_code",
        "action", "retryable", "created_at", "updated_at", "finished_at",
        "elapsed_ms", "result", "calls",
    ):
        if key in data:
            setattr(task, key, data[key])
    return task


def latest(paths: Any, work: str, chapter_id: str, *, kind: str = "draft") -> dict[str, Any] | None:
    """这一章最近一次任务（含已结束的）。页面刷新后靠它恢复进度或中断提示。"""
    directory = tasks_dir(paths.work_dir(work))
    if not directory.exists():
        return None
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in directory.glob("*.json"):
        data = _load(path)
        if not data:
            continue
        if str(data.get("chapter_id")) != chapter_id:
            continue
        if kind and str(data.get("kind")) != kind:
            continue
        rows.append((str(data.get("created_at") or path.name), data))
    if not rows:
        return None
    rows.sort(key=lambda kv: kv[0], reverse=True)
    data = rows[0][1]
    data["cancellable"] = data.get("status") in ("running", "cancelling")
    return data


def request_cancel(paths: Any, task_id: str) -> dict[str, Any]:
    task = get(paths, task_id)
    if not task:
        raise ValueError(f"找不到任务「{task_id}」")
    if task.status in TERMINAL:
        return task.public()
    task.cancel_event.set()
    update(task, status="cancelling", message="已请求取消，正在等模型调用层停手…")
    return task.public()


def finish(
    task: Task,
    *,
    status: str,
    message: str = "",
    result: dict[str, Any] | None = None,
    **fields: Any,
) -> Task:
    elapsed = 0.0
    if task.started_ms:
        import time

        elapsed = round((time.monotonic() - task.started_ms) * 1000, 1)
    update(
        task,
        status=status,
        message=message or task.message,
        finished_at=_now_iso(),
        elapsed_ms=elapsed,
        result=result or {},
        **fields,
    )
    return task


def spawn(target: Callable[[], Any], *, name: str = "") -> threading.Thread:
    """跑一个守护线程。本地单用户，不引任务队列。"""
    thread = threading.Thread(target=target, name=name or "workshop-task", daemon=True)
    thread.start()
    return thread


def mark_interrupted(paths: Any) -> int:
    """服务启动时调用：上次还在跑的任务统一标成 interrupted。

    **不能只改内存**：进程已经死了，内存里什么都没有。必须扫盘上的 task 文件。
    """
    count = 0
    if not paths.workspaces.exists():
        return 0
    for work_dir in paths.workspaces.iterdir():
        if not work_dir.is_dir() or work_dir.name.startswith((".", "_")):
            continue
        directory = tasks_dir(work_dir)
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            data = _load(path)
            if not data:
                continue
            if data.get("status") not in ("running", "cancelling"):
                continue
            # 必须以内存里那份为准：`get` 会返回注册表对象，
            # 如果拿新解析的对象去更新，内存里那位还停在 running，界面就会继续装成在跑。
            task = _registry.get(str(data.get("task_id") or path.stem))
            if task is None:
                task = _from_disk(data, path)
                _registry[task.task_id] = task
            info = describe_error("interrupted")
            update(
                task,
                status="interrupted",
                message="服务重启，这次生成中断了",
                error_code="interrupted",
                error="服务进程在生成过程中退出，任务没有继续跑",
                action=str(info["action"]),
                retryable=True,
                finished_at=_now_iso(),
            )
            count += 1
    return count
