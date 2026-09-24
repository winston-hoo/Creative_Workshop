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
    UNKNOWN = "unknown"


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
    ErrorKind.UNKNOWN: "未知错误，请查看原始报错",
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
