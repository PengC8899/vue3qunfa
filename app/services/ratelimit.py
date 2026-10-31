"""按账号隔离的发送限速。

Telegram 对每个账号的发送节奏是分别打分的，所以桶也必须按账号分：
一个账号被 FloodWait 拖住时，别的账号不该跟着一起停。

和原来 `send_service` 里那段 `base ± jitter` 的区别：
- 原先是所有账号共用同一个固定间隔，账号之间零差异，是明显的机器节奏；
- 这里是每账号一个令牌桶，速率可以按账号梯队不同（新号必须比老号慢），
  桶本身还允许一定突发（真人不会均匀到毫秒）。

FloodWait 仍然按服务端给的秒数等待，本模块不试图绕过服务端限流。
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict


class TokenBucket:
    """令牌桶：稳态 rate 个/秒，最多攒 capacity 个突发额度。"""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(float(rate), 0.0)
        # 默认不许突发：发消息这种事，攒一把令牌连发正是要避免的形态，
        # 所以容量就是 1，每个令牌都得等出来。
        self.capacity = float(capacity) if capacity is not None else 1.0
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0) -> None:
        if self.rate <= 0:
            # 速率 0 表示这个账号现在不许发，直接返回让调用方自己决定怎么处理
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                await asyncio.sleep((amount - self.tokens) / self.rate)


class RateLimiter:
    """按账号维度隔离的限速器。"""

    def __init__(self, default_rate: float) -> None:
        self.default_rate = max(float(default_rate), 0.0)
        self._rates: dict[str, float] = {}
        self._buckets: dict[str, TokenBucket] = defaultdict(lambda: TokenBucket(self.default_rate))

    def set_rate(self, account: str, rate: float) -> None:
        """给某个账号单独定速率（梯队差异走这里）。"""
        rate = max(float(rate), 0.0)
        self._rates[account] = rate
        if rate > 0:
            self._buckets[account] = TokenBucket(rate)

    def rate_for(self, account: str) -> float:
        return self._rates.get(account, self.default_rate)

    async def wait(self, account: str, rate: float | None = None) -> None:
        """发一条之前调用：保证这个账号自己的节奏不超速。"""
        bucket_rate = self.rate_for(account) if rate is None else max(float(rate), 0.0)
        if bucket_rate <= 0:
            return
        bucket = self._buckets.get(account)
        if bucket is None or abs(bucket.rate - bucket_rate) > 1e-9:
            bucket = TokenBucket(bucket_rate)
            self._buckets[account] = bucket
        await bucket.acquire()

    def forget(self, account: str) -> None:
        """账号删了就把桶一起丢掉，别让内存里攒一堆死账号的桶。"""
        self._rates.pop(account, None)
        self._buckets.pop(account, None)


async def human_delay(min_s: float, max_s: float) -> None:
    """在区间内随机等一会儿。两端一样就是固定等待。"""
    lo = max(float(min_s), 0.0)
    hi = max(float(max_s), lo)
    await asyncio.sleep(random.uniform(lo, hi))
