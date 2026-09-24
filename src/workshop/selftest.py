"""自检用的本地模拟服务。

目的：让探测脚本能在**不花钱、不需要密钥、不需要作品数据**的前提下被完整验证。
这是整个工坊里唯一一个可以完全独立跑通的模块，所以它应该先跑通，
后面写标注流程时直接复用同一套调用基准。

模拟一个 OpenAI 兼容端点，并且可以按开关模拟真实世界里的三种降级场景：
  --mock-no-json     模型不支持结构化输出（返回 400）
  --mock-no-usage    响应里不返回用量字段
  --mock-thinking    默认开启思考模式：思维链吃满输出预算，正文为空

第三种场景来自一次真实探测：DeepSeek 默认开启思考模式，导致正文为空、
首字延迟测不到。这里把它固化成可复现的回归场景。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

# 模拟一个中文 tokenizer 的换算系数，便于验证两点法的正确性
MOCK_TOKENS_PER_CHAR = 0.65


@dataclass
class MockState:
    supports_json: bool = True
    supports_usage: bool = True
    thinking_default: bool = False
    ttft_ms: int = 40
    per_token_ms: int = 6
    models: tuple[str, ...] = ("mock-flash", "mock-pro")
    thinking_ttft_ms: int = 120
    # 标注链路的自检用：让模拟服务返回指定的标注 JSON，
    # 或者返回一段**非 JSON 的文本**来验证「解析失败 → 重试 → 转人工」这条路径。
    annotation_payload: dict | None = None
    annotation_raw_text: str | None = None
    # 逐章应答：批量调度要验证「第 2 章能推进第 1 章埋下的伏笔」这类跨章行为，
    # 固定返回一坨同样的 JSON 是测不出来的。给一个按 messages 现算的钩子。
    responder: Callable[[list], dict | str] | None = None
    # 强制所有请求返回某个错误码。用来验证「配置类错误要立刻停」——
    # 实测密钥失效时，批量任务会拿同一个 401 把后面每一章都刷一遍。
    force_status: int | None = None
    force_message: str = "Authentication Fails, Your api key: ****1234 is invalid"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: MockState = MockState()

    def log_message(self, *_args) -> None:  # 静音
        return

    # ── 工具 ────────────────────────────────────────────

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": code}})

    @staticmethod
    def _count_tokens(text: str) -> int:
        return max(1, int(len(text) * MOCK_TOKENS_PER_CHAR)) if text else 0

    def _thinking_on(self, payload: dict) -> bool:
        field = payload.get("thinking")
        if isinstance(field, dict):
            kind = field.get("type")
            if kind == "enabled":
                return True
            if kind == "disabled":
                return False
        return self.state.thinking_default

    # ── 路由 ────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            payload = {
                "object": "list",
                "data": [{"id": m, "object": "model"} for m in self.state.models],
            }
            self._send_json(200, payload)
            return
        self._error(404, "unknown endpoint")

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._error(404, "unknown endpoint")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            self._error(400, "invalid json body")
            return

        if self.state.force_status:
            code = (
                "authentication_error"
                if self.state.force_status in (401, 403)
                else "invalid_request_error"
            )
            self._error(self.state.force_status, self.state.force_message, code=code)
            return

        model = payload.get("model")
        if model not in self.state.models:
            self._error(404, f"model {model} not found", code="model_not_found")
            return

        response_format = payload.get("response_format") or {}
        wants_json = response_format.get("type") in ("json_object", "json_schema")
        if wants_json and not self.state.supports_json:
            self._error(
                400,
                "response_format json_object is not supported by this model",
                code="unsupported_parameter",
            )
            return

        messages = payload.get("messages") or []
        prompt_tokens = sum(
            self._count_tokens(str(m.get("content") or ""))
            for m in messages
            if isinstance(m, dict)
        )
        max_tokens = int(payload.get("max_tokens") or 8)
        thinking_on = self._thinking_on(payload)

        if thinking_on:
            # 还原真实行为：思维链吃满输出预算，正文为空
            reasoning_text = "思考推演" * max(1, max_tokens)
            content = ""
            completion_tokens = max_tokens
            reasoning_tokens = max_tokens
        else:
            reasoning_text = ""
            if self.state.responder is not None:
                answer = self.state.responder(messages)
                content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
            elif self.state.annotation_raw_text is not None:
                content = self.state.annotation_raw_text
            elif self.state.annotation_payload is not None:
                content = json.dumps(self.state.annotation_payload, ensure_ascii=False)
            elif wants_json:
                content = '{"ok": true}'
            else:
                content = "好" * max_tokens
            completion_tokens = self._count_tokens(content)
            reasoning_tokens = 0

        if payload.get("stream"):
            self._stream_response(
                model, content, reasoning_text, prompt_tokens, completion_tokens, reasoning_tokens
            )
        else:
            self._plain_response(
                model, content, reasoning_text, prompt_tokens, completion_tokens, reasoning_tokens
            )

    # ── 响应构造 ────────────────────────────────────────

    def _usage(self, prompt_tokens: int, completion_tokens: int, reasoning_tokens: int) -> dict | None:
        if not self.state.supports_usage:
            return None
        usage: dict = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if reasoning_tokens:
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
        return usage

    def _plain_response(
        self,
        model: str,
        content: str,
        reasoning_text: str,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        message: dict = {"role": "assistant", "content": content}
        if reasoning_text:
            message["reasoning_content"] = reasoning_text
        payload: dict = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "length"}],
        }
        usage = self._usage(prompt_tokens, completion_tokens, reasoning_tokens)
        if usage:
            payload["usage"] = usage
        self._send_json(200, payload)

    def _stream_response(
        self,
        model: str,
        content: str,
        reasoning_text: str,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def write_chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> None:
            chunk: dict = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage is not None:
                chunk["usage"] = usage
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        def emit(text: str, field: str, first_delay_ms: int) -> None:
            if not text:
                return
            if first_delay_ms:
                time.sleep(first_delay_ms / 1000.0)
            for i in range(0, len(text), 2):
                write_chunk({field: text[i : i + 2]})
                time.sleep(self.state.per_token_ms / 1000.0)

        if reasoning_text:
            # 思维链先来，且只有它：正文为空，正文首字永远不会出现
            emit(reasoning_text, "reasoning_content", self.state.thinking_ttft_ms)
        else:
            emit(content, "content", self.state.ttft_ms)

        usage = self._usage(prompt_tokens, completion_tokens, reasoning_tokens)
        write_chunk({}, finish="length", usage=usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class MockServer:
    """上下文管理器形式的模拟服务。"""

    def __init__(self, state: MockState | None = None) -> None:
        self.state = state or MockState()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> "MockServer":
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
