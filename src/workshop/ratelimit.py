"""客户端限速：滑动窗口限速器。

为什么需要它（需求来自设置页「速度限制」）：

国内服务商（如腾讯云）在控制台公开每分钟请求上限（RPM），
超过后接口直接返回 429。与其等上游报错再猜，不如在客户端就按
配置的窗口 self 约束，把一批请求排队放行：

  · 没配限速 → 原样直连，行为与之前完全一致
  · 配了 RPM / TPM → 超窗的请求在窗口内排队，等旧请求滑出窗口再放行
  · 排队太久（超过 max_wait_sec）→ 拒绝并发起请求，返回明确的限速错误，
    让上层能直接告诉用户「是限速，不是网络问题」

Token 估算只能拿到请求发出前的近似值（请求体字符数 / 2），
所以 TPM 是一道粗闸，精确值仍以上游返回的 usage 为准。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimitExceeded(Exception):
    """等待超过 max_wait_sec 仍拿不到放行额度的错误。"""


class SlidingWindowRateLimiter:
    """按「每分钟 N 次请求 / N 个 token」限速的滑动窗口限速器。

    线程安全：批量任务并发 1~16，工作线程共享同一个 client 实例，
    所以所有账号记账都必须持锁。
    """

    def __init__(
        self,
        requests_per_minute: int | float | None = None,
        tokens_per_minute: int | float | None = None,
        max_wait_sec: float = 90.0,
    ) -> None:
        self.rpm = max(0, int(requests_per_minute or 0)) or None
        self.tpm = max(0, int(tokens_per_minute or 0)) or None
        self.max_wait_sec = float(max_wait_sec)
        self._lock = threading.Lock()
        self._requests: deque[float] = deque()  # 最近放行的请求时间戳（单调时钟）
        self._tokens: deque[tuple[float, float]] = deque()  # (时间戳, 估算 token)

    @property
    def limited(self) -> bool:
        return self.rpm is not None or self.tpm is not None

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._requests and self._requests[0] <= cutoff:
            self._requests.popleft()
        while self._tokens and self._tokens[0][0] <= cutoff:
            self._tokens.popleft()

    def _wait_needed(self, now: float, estimated_tokens: float) -> tuple[float, str]:
        """返回 (需要等多久, 原因)。0 表示可以直接放行。"""
        self._prune(now)
        if self.rpm is not None and len(self._requests) >= self.rpm:
            wait = max(0.0, self._requests[0] + 60.0 - now)
            return wait, f"每分钟请求上限 {self.rpm} 次"
        if self.tpm is not None:
            used = sum(t for _, t in self._tokens)
            if self.tpm - used < estimated_tokens:
                if self._tokens:
                    wait = max(0.0, self._tokens[0][0] + 60.0 - now)
                else:
                    wait = 60.0 - (now - (self._tokens[0][0] if self._tokens else now))
                    wait = max(0.0, wait)
                return wait, f"每分钟 token 上限 {int(self.tpm)}"
        return 0.0, ""

    def acquire(self, estimated_tokens: float = 0.0) -> None:
        """请求前调用。拿不到额度就等；等待超时就抛 RateLimitExceeded。"""
        if not self.limited or estimated_tokens < 0:
            return
        estimated_tokens = float(estimated_tokens)
        deadline = time.monotonic() + self.max_wait_sec

        while True:
            now = time.monotonic()
            with self._lock:
                wait, reason = self._wait_needed(now, estimated_tokens)
                if wait <= 0:
                    self._requests.append(now)
                    if self.tpm is not None:
                        self._tokens.append((now, estimated_tokens))
                    return
            if now + wait > deadline:
                raise RateLimitExceeded(
                    f"已达到{reason}，等待 {int(wait)} 秒仍无法放行 "
                    f"（超过最大等待 {int(self.max_wait_sec)} 秒）。"
                    "请降低并发，或提高服务商的每分钟限速配置。"
                )
            # 睡眠一小段再复查（不用长 sleep，避免窗口释放后干等）
            time.sleep(min(0.2, max(0.05, wait)))


def build_rate_limiter(rate_limit: dict | None) -> SlidingWindowRateLimiter | None:
    """按 providers.yaml / 自定义服务商里的 rate_limit 配置构造限速器。

    兼容三种写法：
      rate_limit:                     # 顶层字段（UI 新增服务商用这个）
        requests_per_minute: 60
        tokens_per_minute: 200000
      rate_limit:
        rate_limit:                   # 再包一层 rate_limit 的写法规整成一样的
          requests_per_minute: 60
      manual:
        rate_limit:                   # 跟随模板里的 manual 段
          requests_per_minute: 60
    """
    if not rate_limit:
        return None
    block = rate_limit
    if isinstance(rate_limit.get("rate_limit"), dict):
        block = rate_limit["rate_limit"]
    elif isinstance(rate_limit.get("manual"), dict) and isinstance(
        rate_limit["manual"].get("rate_limit"), dict
    ):
        block = rate_limit["manual"]["rate_limit"]
    if not isinstance(block, dict):
        return None
    limiter = SlidingWindowRateLimiter(
        requests_per_minute=block.get("requests_per_minute"),
        tokens_per_minute=block.get("tokens_per_minute"),
    )
    return limiter if limiter.limited else None