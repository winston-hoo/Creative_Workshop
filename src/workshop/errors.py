"""错误分类。

把 HTTP 状态码与网络异常统一映射为可读的错误类别，并给出面向用户的中文提示。
界面上的提示文案按此表给，不要直接抛原始报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ErrorKind(str, Enum):
    AUTH_FAILED = "auth_failed"
    ENDPOINT_NOT_FOUND = "endpoint_not_found"
    MODEL_NOT_FOUND = "model_not_found"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_UNREACHABLE = "network_unreachable"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    BAD_REQUEST = "bad_request"
    USAGE_MISSING = "usage_missing"
    STRUCTURED_OUTPUT_UNSUPPORTED = "structured_output_unsupported"
    OUTPUT_BUDGET_CONSUMED_BY_REASONING = "output_budget_consumed_by_reasoning"
    RESPONSE_UNPARSABLE = "response_unparsable"
    EMPTY_RESPONSE = "empty_response"
    """模型返回了空白正文。重试同一请求通常无效，要先查输出预算/思考模式。"""
    TRUNCATED = "truncated"
    """续写到了轮数上限仍被 max_tokens 截断，这一章没写完。"""
    CANCELLED = "cancelled"
    """作者主动取消。不是故障，不重试。"""
    INTERRUPTED = "interrupted"
    """服务重启导致任务中断。可查看候选恢复点或从断点继续。"""
    UNKNOWN = "unknown"


class GenerationCancelled(Exception):
    """作者点了取消。**不是错误**：不要当失败上报，要落成 cancelled 状态。

    `partial_text` 是取消前已经收到的正文。必须带出来——否则取消一次，
    这一轮已经生成的字就白花了，作者连「从断点继续」都没得选。
    """

    def __init__(self, message: str = "已取消", partial_text: str = "") -> None:
        self.partial_text = partial_text or ""
        super().__init__(message)


_HINTS: dict[ErrorKind, str] = {
    ErrorKind.AUTH_FAILED: "检查 api_key_ref 对应的密钥是否正确、是否已过期",
    ErrorKind.ENDPOINT_NOT_FOUND: "检查 base_url 是否缺少版本路径",
    ErrorKind.MODEL_NOT_FOUND: "模型名有误，请从 models_discovered 里选一个正确的名字",
    ErrorKind.NETWORK_TIMEOUT: "请求超时，检查网络质量或适当提高 timeout_sec",
    ErrorKind.NETWORK_UNREACHABLE: "网络不可达，检查代理与防火墙设置",
    ErrorKind.RATE_LIMITED: "触发限流，降低 max_concurrency 或错峰执行",
    ErrorKind.SERVER_ERROR: "服务端错误，稍后重试；若持续出现请联系服务商",
    ErrorKind.BAD_REQUEST: "请求参数被拒绝，检查 max_tokens 等参数是否超出该模型限制",
    ErrorKind.USAGE_MISSING: "该服务商不返回用量，费用只能估算，无法核对",
    ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED: "该模型不输出结构化内容，将走降级路径（提示词约束 + 解析重试）",
    ErrorKind.OUTPUT_BUDGET_CONSUMED_BY_REASONING: (
        "输出预算被思维链占满，正文为空。这类短输出任务建议关闭思考模式"
    ),
    ErrorKind.RESPONSE_UNPARSABLE: "响应无法解析，可能是服务端返回了非预期格式",
    ErrorKind.EMPTY_RESPONSE: "模型返回空正文，先检查输出预算与思考模式，再重试",
    ErrorKind.TRUNCATED: "续写到轮数上限仍被截断，这一章没写完；可调高 max_tokens 或从候选继续",
    ErrorKind.CANCELLED: "已按你的取消请求停止，没有继续发起模型调用",
    ErrorKind.INTERRUPTED: "服务重启导致任务中断；候选恢复点已保留，可继续或重新生成",
    ErrorKind.UNKNOWN: "未知错误，请查看原始报错",
}

# B3/E6：每类失败都给出 **错误码 + 可读标题 + 建议动作 + 是否值得重试**。
# 界面直接显示 code，文案与动作从这里取，不在前端另写一套。
_ERROR_ACTIONS: dict[ErrorKind, tuple[str, str, bool]] = {
    ErrorKind.AUTH_FAILED: ("密钥或权限失效", "到设置页检查密钥是否过期、是否有该模型权限。修好前不要重试。", False),
    ErrorKind.ENDPOINT_NOT_FOUND: ("接口地址不对", "检查 base_url 是否缺少版本路径（如 /v1）。", False),
    ErrorKind.MODEL_NOT_FOUND: ("模型名不存在", "从服务商返回的模型列表里选一个正确的名字。", False),
    ErrorKind.NETWORK_TIMEOUT: ("请求超时", "检查网络或代理；也可以调高超时。已写内容在候选里，可从断点继续。", True),
    ErrorKind.NETWORK_UNREACHABLE: ("网络不可达", "检查代理、防火墙与网络连接。已写内容在候选里。", True),
    ErrorKind.RATE_LIMITED: ("被上游限流", "等一会儿再重试，或降低并发。已写内容在候选里。", True),
    ErrorKind.SERVER_ERROR: ("服务商故障", "稍后重试；持续出现就联系服务商。已写内容在候选里。", True),
    ErrorKind.BAD_REQUEST: ("请求参数被拒", "检查 max_tokens 是否超出模型上限。", False),
    ErrorKind.USAGE_MISSING: ("缺少用量", "费用只能估算。不影响继续写。", False),
    ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED: ("不支持结构化输出", "走提示词约束的降级路径。", False),
    ErrorKind.OUTPUT_BUDGET_CONSUMED_BY_REASONING: ("输出预算被思维链占满", "关闭思考模式，或调高 max_tokens。", True),
    ErrorKind.RESPONSE_UNPARSABLE: ("响应无法解析", "重试一次；持续出现检查服务商端点。", True),
    ErrorKind.EMPTY_RESPONSE: ("模型返回空正文", "先关闭思考模式或调高 max_tokens，再重试。", True),
    ErrorKind.TRUNCATED: ("续写上限仍被截断", "调高 max_tokens；或从候选恢复点继续写。", True),
    ErrorKind.CANCELLED: ("已取消", "没有继续发起调用。需要时点「继续上次」从候选接上。", False),
    ErrorKind.INTERRUPTED: ("任务中断", "查看候选恢复点，点「从断点继续」或重新生成。", True),
    ErrorKind.UNKNOWN: ("未知错误", "看原始报错定位；已写内容会保留在候选里。", True),
}


def describe_error(kind: ErrorKind | str, detail: str = "") -> dict[str, object]:
    """把一个错误类别摊成界面能直接用的结构。

    返回 `code` 原样给作者看——他要把日志和界面上的那句话对上；
    `action` 是可执行建议；`retryable` 决定界面要不要给「重试」按钮。
    """
    if isinstance(kind, str):
        try:
            kind = ErrorKind(kind)
        except ValueError:
            kind = ErrorKind.UNKNOWN
    title, action, retryable = _ERROR_ACTIONS.get(
        kind, ("未知错误", "看原始报错定位。", True)
    )
    return {
        "code": kind.value,
        "title": title,
        "message": detail or _HINTS.get(kind, ""),
        "action": action,
        "retryable": retryable,
        "fatal": is_fatal(kind),
    }

# 这些类别不阻止启用服务商，只影响使用方式
NON_BLOCKING: set[ErrorKind] = {
    ErrorKind.USAGE_MISSING,
    ErrorKind.STRUCTURED_OUTPUT_UNSUPPORTED,
    ErrorKind.OUTPUT_BUDGET_CONSUMED_BY_REASONING,
}

# 这些错误重试再多次也不会好——它们是配置问题，不是抖动。
#
# 存在的理由很实在：实测密钥失效时，批量任务把 20 章各重试 3 次，白跑 8 秒；
# 照这个速度跑完 933 章要白等十几分钟，而且日志里会堆满同一个 401。
# 撞上这类错误应当立刻停，把原因摆在最前面。
FATAL_KINDS: set[ErrorKind] = {
    ErrorKind.AUTH_FAILED,
    ErrorKind.ENDPOINT_NOT_FOUND,
    ErrorKind.MODEL_NOT_FOUND,
}


def hint_for(kind: ErrorKind | str) -> str:
    """给用户看的处置建议。界面上的提示一律从这里取，不要直接抛原始报错。"""
    if isinstance(kind, str):
        try:
            kind = ErrorKind(kind)
        except ValueError:
            return _HINTS[ErrorKind.UNKNOWN]
    return _HINTS.get(kind, _HINTS[ErrorKind.UNKNOWN])


def is_fatal(kind: ErrorKind | str) -> bool:
    if isinstance(kind, str):
        try:
            kind = ErrorKind(kind)
        except ValueError:
            return False
    return kind in FATAL_KINDS


@dataclass
class ProbeError:
    """一条探测错误记录。"""

    step: str
    kind: ErrorKind
    hint: str = ""
    http_status: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.hint:
            self.hint = _HINTS.get(self.kind, "")

    @property
    def blocking(self) -> bool:
        return self.kind not in NON_BLOCKING

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "kind": self.kind.value,
            "blocking": self.blocking,
            "http_status": self.http_status,
            "hint": self.hint,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        status = f" HTTP {self.http_status}" if self.http_status else ""
        return f"[{self.step}] {self.kind.value}{status} — {self.hint}"


@dataclass
class ErrorCollector:
    """错误收集器，便于一次跑完所有探测步骤后统一汇报。"""

    items: list[ProbeError] = field(default_factory=list)

    def add(
        self,
        step: str,
        kind: ErrorKind,
        http_status: int | None = None,
        detail: str = "",
    ) -> ProbeError:
        err = ProbeError(step=step, kind=kind, http_status=http_status, detail=detail)
        self.items.append(err)
        return err

    @property
    def blocking_items(self) -> list[ProbeError]:
        return [e for e in self.items if e.blocking]

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self.items]


def classify_http_status(status: int, body: str = "") -> ErrorKind:
    """按 HTTP 状态码分类，body 用于细分 400 类错误。"""
    if status in (401, 403):
        return ErrorKind.AUTH_FAILED
    if status == 404:
        lowered = body.lower()
        if "model" in lowered:
            return ErrorKind.MODEL_NOT_FOUND
        return ErrorKind.ENDPOINT_NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if 400 <= status < 500:
        lowered = body.lower()
        if "model" in lowered and ("not found" in lowered or "不存在" in body or "invalid" in lowered):
            return ErrorKind.MODEL_NOT_FOUND
        return ErrorKind.BAD_REQUEST
    if status >= 500:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.UNKNOWN
