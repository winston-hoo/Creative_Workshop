"""模型调用层：Provider 抽象 + OpenAI 兼容实现。

国内主流服务商大多提供 OpenAI 兼容端点，所以这里只实现一个适配器，
靠 base_url + api_key + 模型名 三个参数区分服务商。
不兼容的个别情况再单独写适配器，接口签名保持一致。

这一层同时定下后续所有模型调用的基准：超时、错误分类、用量解析、脱敏。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests

from .errors import ErrorKind, classify_http_status
from .secrets import redact

logger = logging.getLogger(__name__)

JSON_PROBE_KEYS = ("ok",)

DEFAULT_MAX_TOKENS = 8000
"""单次输出预算起点。模型没声明 max_output 时用它。"""

MAX_TOKENS_CAP = 32000
"""预算升级的上限。撞上截断时逐次翻倍，但不超过它。"""


def default_max_tokens(model_cfg: dict[str, Any] | None) -> int:
    """按模型自己声明的 max_output 定输出预算，封顶 MAX_TOKENS_CAP。

    这里绝不能再用一个小常数。实测两处都栽在同一件事上：
      · 实体统计写死 4000，545 章每块 50 章时 11 块里 9 块被截断，整块数据丢掉
      · 大纲的书级归约写死 1200，正文还没写完就被砍，报出来却是「返回内容不是 JSON」
    """
    try:
        declared = int((model_cfg or {}).get("max_output") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared <= 0:
        return DEFAULT_MAX_TOKENS
    return max(2000, min(declared, MAX_TOKENS_CAP))


@dataclass
class Usage:
    """接口返回的用量。费用结算依赖它，所以解析要兼容多种字段命名。

    注意 reasoning_tokens：思考模式下思维链也计费，它包含在 completion_tokens 里。
    如果只按 completion_tokens 算「生成速度」，会把思考时间也算进去，得出偏低的假速度。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> "Usage | None":
        if not isinstance(raw, dict):
            return None
        details = raw.get("prompt_tokens_details") or {}
        cached = 0
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
        if not cached:
            # DeepSeek 等以独立字段返回缓存命中量
            cached = int(raw.get("prompt_cache_hit_tokens") or 0)

        c_details = raw.get("completion_tokens_details") or {}
        reasoning = 0
        if isinstance(c_details, dict):
            reasoning = int(c_details.get("reasoning_tokens") or 0)
        if not reasoning:
            reasoning = int(raw.get("reasoning_tokens") or 0)

        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
            cached_tokens=cached,
            reasoning_tokens=reasoning,
        )

    @property
    def present(self) -> bool:
        return (self.prompt_tokens + self.completion_tokens + self.total_tokens) > 0

    @property
    def content_tokens(self) -> int:
        """正文 token 数，扣掉思维链。用于算真实的生成速度。"""
        return max(0, self.completion_tokens - self.reasoning_tokens)

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class StreamResult:
    text: str = ""
    reasoning_text: str = ""
    ttft_ms: float | None = None
    """首个任何类型 delta（含思维链）的到达时间。衡量用户感知的等待。"""
    first_content_ms: float | None = None
    """首个正文 delta 的到达时间。思考模式下它可能远晚于 ttft_ms。"""
    total_ms: float = 0.0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    usage: Usage | None = None

    @property
    def content_tokens(self) -> int:
        if self.usage is not None:
            return self.usage.content_tokens
        return max(0, self.output_tokens - self.reasoning_tokens)

    @property
    def thinking_used(self) -> bool:
        return bool(self.reasoning_text) or self.reasoning_tokens > 0


def build_thinking_extra(thinking: str | None, effort: str | None = None) -> dict[str, Any]:
    """构造思考模式相关请求参数。

    DeepSeek 默认开启思考模式（effort 为 high）。对短输出任务来说这是纯浪费：
    思维链会占满 max_tokens，导致正文为空，而且温度参数在思考模式下不生效。
    所以在 REST 层直接传 {"thinking": {"type": "disabled"}} 关掉它。
    """
    extra: dict[str, Any] = {}
    if thinking in ("enabled", "disabled"):
        extra["thinking"] = {"type": thinking}
    if effort:
        extra["reasoning_effort"] = effort
    return extra


class ApiError(Exception):
    """统一的上游错误，携带分类与状态码。"""

    def __init__(
        self,
        kind: ErrorKind,
        status: int | None = None,
        body: str = "",
        message: str = "",
    ) -> None:
        self.kind = kind
        self.status = status
        self.body = body
        self.message = message or f"{kind.value} (HTTP {status})"
        super().__init__(self.message)

    def safe_body(self, secrets: Iterable[str] | None = None, limit: int = 500) -> str:
        """脱敏并截断的响应体，可直接进日志与报告。"""
        return redact((self.body or "")[:limit], list(secrets or []))


@dataclass
class ChatResult:
    """非流式调用的结果。把思维链一并带出来，便于区分「没输出」和「输出被思维链占了」。"""

    text: str = ""
    reasoning_text: str = ""
    usage: Usage | None = None
    total_ms: float = 0.0
    finish_reason: str = ""
    """上游给的结束原因。`length` 表示撞上 max_tokens 被截断。

    必须单独带出来：结构化任务里被截断的输出必然不是合法 JSON，
    只看文本会把「输出预算不够」误报成「返回的不是 JSON」，
    真正的病根就此被藏起来，重试也会一次次照原样失败。
    """

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    @property
    def thinking_used(self) -> bool:
        if self.reasoning_text:
            return True
        return self.usage is not None and self.usage.reasoning_tokens > 0


class OpenAICompatProvider:
    """OpenAI 兼容端点的最小实现。

    只保留探测与后续标注真正需要的三个能力：
      list_models / chat / chat_stream
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_sec: float = 60.0,
        auth_scheme: str = "bearer",
        session: requests.Session | None = None,
        secrets: Iterable[str] | None = None,
        rate_limit: dict | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 为空，无法建立调用")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_sec = float(timeout_sec)
        self.auth_scheme = (auth_scheme or "bearer").lower()
        self.secrets = list(secrets or [])
        self.session = session or requests.Session()
        # 客户端限速器：腾讯云这类服务商在控制台明示每分钟请求上限，
        # 超了直接 429。所以在这里按配置排队放行，而不是等上游报错再猜。
        from .ratelimit import build_rate_limiter

        self.rate_limiter = build_rate_limiter(rate_limit)

    # ── 基础设施 ────────────────────────────────────────────

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.auth_scheme != "none":
            if self.auth_scheme in ("bearer", "authorization"):
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers[self.auth_scheme] = self.api_key
        return headers

    def _timeout(self) -> tuple[float, float]:
        # (连接超时, 读取超时)
        return (min(10.0, self.timeout_sec), self.timeout_sec)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        # 客户端限速：在发起请求前排队拿额度。超时（等不到放行）会抛
        # RateLimitExceeded，这里转成 ApiError(RATE_LIMITED) 统一错误形状。
        if self.rate_limiter is not None:
            try:
                self.rate_limiter.acquire()
            except Exception as exc:  # noqa: BLE001
                raise ApiError(ErrorKind.RATE_LIMITED, message=str(exc)) from exc
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method, url, headers=self._headers, timeout=self._timeout(), **kwargs
            )
        except requests.exceptions.Timeout as exc:
            raise ApiError(ErrorKind.NETWORK_TIMEOUT, message=str(exc)) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ApiError(ErrorKind.NETWORK_UNREACHABLE, message=str(exc)) from exc
        except requests.exceptions.RequestException as exc:
            raise ApiError(ErrorKind.UNKNOWN, message=str(exc)) from exc
        return resp

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        raw = resp.text or ""
        kind = classify_http_status(resp.status_code, raw)
        # 429 时上游通常会带 Retry-After：服务商自己给出的等待秒数。
        # 带上它，错误信息与重试策略都更准确，而不是只给一句「触发限流」。
        retry_after: str | None = None
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
        message = ""
        if kind == ErrorKind.RATE_LIMITED and retry_after:
            message = f"上游限流，建议等待 {retry_after} 秒后重试"
        raise ApiError(kind, status=resp.status_code, body=raw, message=message)

    # ── 能力 ────────────────────────────────────────────────

    def list_models(self) -> tuple[list[str], float]:
        """P1 用：返回模型列表与往返延迟（毫秒）。"""
        start = time.perf_counter()
        resp = self._request("GET", "/models")
        elapsed_ms = (time.perf_counter() - start) * 1000
        self._raise_for_status(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ApiError(
                ErrorKind.RESPONSE_UNPARSABLE,
                status=resp.status_code,
                body=resp.text[:300],
                message="模型列表不是合法 JSON",
            ) from exc
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ApiError(
                ErrorKind.RESPONSE_UNPARSABLE,
                status=resp.status_code,
                body=str(data)[:300],
                message="模型列表结构不符合 OpenAI 兼容格式",
            )
        ids = [str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")]
        return ids, elapsed_ms

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 8,
        temperature: float = 0.0,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        """非流式对话。"""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format
        if extra:
            payload.update(extra)

        start = time.perf_counter()
        resp = self._request("POST", "/chat/completions", json=payload)
        elapsed_ms = (time.perf_counter() - start) * 1000
        self._raise_for_status(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ApiError(
                ErrorKind.RESPONSE_UNPARSABLE,
                status=resp.status_code,
                body=resp.text[:300],
            ) from exc

        choices = data.get("choices") or []
        text = ""
        reasoning = ""
        finish_reason = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            text = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or "")
            finish_reason = str(choices[0].get("finish_reason") or "")
        return ChatResult(
            text=text,
            reasoning_text=reasoning,
            usage=Usage.from_api(data.get("usage")),
            total_ms=elapsed_ms,
            finish_reason=finish_reason,
        )

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 8,
        temperature: float = 0.0,
        extra: dict[str, Any] | None = None,
    ) -> StreamResult:
        """流式对话。开流式才能测出首字延迟（TTFT）。

        思维链内容走独立的 reasoning_content 字段，与 content 同级。
        如果只盯 content，思考模式下会整段思考都统计不到，TTFT 直接变成「未测到」。
        所以这里两者都记：ttft_ms 记首个任意 delta，first_content_ms 记首个正文 delta。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # 部分服务商需要显式声明才在流式响应里返回用量
            "stream_options": {"include_usage": True},
        }
        if extra:
            payload.update(extra)

        result = StreamResult()
        start = time.perf_counter()
        resp = self._request("POST", "/chat/completions", json=payload, stream=True)
        self._raise_for_status(resp)

        pieces: list[str] = []
        reasoning_pieces: list[str] = []
        try:
            for raw_line in resp.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    parsed = json.loads(chunk)
                except ValueError:
                    continue

                usage = Usage.from_api(parsed.get("usage"))
                if usage and usage.present:
                    result.usage = usage

                choices = parsed.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                delta = choices[0].get("delta") or {}

                content = delta.get("content")
                reasoning = delta.get("reasoning_content")

                if content or reasoning:
                    if result.ttft_ms is None:
                        result.ttft_ms = (time.perf_counter() - start) * 1000
                if reasoning:
                    reasoning_pieces.append(str(reasoning))
                if content:
                    if result.first_content_ms is None:
                        result.first_content_ms = (time.perf_counter() - start) * 1000
                    pieces.append(str(content))
        finally:
            resp.close()

        result.total_ms = (time.perf_counter() - start) * 1000
        result.text = "".join(pieces)
        result.reasoning_text = "".join(reasoning_pieces)
        if result.usage is not None:
            result.output_tokens = result.usage.completion_tokens
            result.reasoning_tokens = result.usage.reasoning_tokens
        else:
            # 拿不到 usage 时退化为「按 delta 片段数粗估」
            result.output_tokens = len(reasoning_pieces) + len(pieces)
            result.reasoning_tokens = len(reasoning_pieces)
        return result


def _strip_code_fences(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [ln for ln in candidate.splitlines() if not ln.strip().startswith("```")]
        candidate = "\n".join(lines).strip()
    return candidate


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象，容忍前后有额外文字或代码围栏。"""
    if not text:
        return None
    candidate = _strip_code_fences(text)
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def looks_truncated(text: str) -> bool:
    """上游没给 finish_reason 时的兜底判断：开了 JSON 的头却没闭合。

    只看「结尾不是 }」会把「模型压根没输出 JSON、只是说了一段话」也算成截断，
    于是输出预算被无谓地翻倍，错误原因还从「不是 JSON」变成「被截断」——
    真正的病根又一次被盖住。所以先要求文本里确实出现了 `{`。
    """
    stripped = (text or "").strip()
    if "{" not in stripped:
        return False
    return not stripped.endswith("}")


def repair_truncated_json(text: str) -> dict[str, Any] | None:
    """抢救被 max_tokens 截断的 JSON：丢掉没写完的尾巴，把完整的部分闭合回来。

    截断点必然落在某个值内部，而截断之前的部分（往往是几十个人物）本身是合法的。
    所以从后往前找「某个元素刚好闭合」的位置，在那里切断并按扫描出的未闭合括号
    补上收尾，通常能保住绝大多数字段。宁可少最后一条，也不能因为最后一条没写完
    就把整块实体全丢掉——实测一次真实运行里 11 块有 9 块是这样整块丢的。

    文本本身完整（顶层括号已闭合）却解析失败时返回 None：那不是截断，抢救没有意义。
    """
    if not text:
        return None
    candidate = _strip_code_fences(text)
    start = candidate.find("{")
    if start == -1:
        return None
    candidate = candidate[start:]

    stack: list[str] = []
    checkpoints: list[tuple[int, tuple[str, ...]]] = []
    in_string = False
    escaped = False
    for index, ch in enumerate(candidate):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if not stack:
                # 顶层已经闭合，说明输出本身是完整的，只是别处不合法
                return None
            checkpoints.append((index + 1, tuple(stack)))

    closers = {"{": "}", "[": "]"}
    for end, open_stack in reversed(checkpoints[-50:]):
        head = candidate[:end].rstrip().rstrip(",")
        repaired = head + "".join(closers[ch] for ch in reversed(open_stack))
        try:
            parsed = json.loads(repaired)
        except ValueError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None
