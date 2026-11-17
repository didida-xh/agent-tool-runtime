"""Redis 幂等状态位：只服务退款这类"重复提交代价高"的请求。

设计取舍：
- 用 SET NX 原子占位，第一个请求拿到 True，重复请求拿到 False；
- TTL 24h，避免状态位无限堆积；
- Redis 不可用时降级为"不做幂等"，业务照常执行——可观测性和幂等都不该阻断主流程。
"""

from __future__ import annotations

import os
from typing import Optional

import redis.asyncio as redis

_client: Optional[redis.Redis] = None
TTL_SECONDS = 24 * 60 * 60


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    return _client


def _key(key: str) -> str:
    return f"idem:{key}"


async def claim(key: str, task_id: str) -> bool:
    """原子占位。返回 True 表示本次请求是第一次，应该真正执行任务。"""
    try:
        return bool(await get_redis().set(_key(key), task_id, nx=True, ex=TTL_SECONDS))
    except Exception:
        return True


async def lookup(key: str) -> Optional[str]:
    """取上次占位时写入的 task_id；没有或 Redis 故障都返回 None。"""
    try:
        return await get_redis().get(_key(key))
    except Exception:
        return None
