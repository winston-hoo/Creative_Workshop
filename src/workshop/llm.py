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
    ) -> None:
        if not base_url:
            raise ValueError("base_url 为空，无法建立调用")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_sec = float(timeout_sec)
        self.auth_scheme = (auth_scheme or "bearer").lower()
        self.secrets = list(secrets or [])
        self.session = session or requests.Session()

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
        raise ApiError(kind, status=resp.status_code, body=raw)

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
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            text = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or "")
        return ChatResult(
            text=text,
            reasoning_text=reasoning,
            usage=Usage.from_api(data.get("usage")),
            total_ms=elapsed_ms,
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


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象，容忍前后有额外文字或代码围栏。"""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [ln for ln in candidate.splitlines() if not ln.strip().startswith("```")]
        candidate = "\n".join(lines).strip()
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
