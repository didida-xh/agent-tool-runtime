"""模型调用薄封装：全项目唯一与 OpenAI SDK 打交道的地方。

单独成文件的两个理由：
1. 测试可以把 call_model 换掉，不必打真实 API；
2. Day3 的熔断/主备只改这里，循环引擎不用动。
"""

from __future__ import annotations

import os
import time
from enum import Enum

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

MODEL = os.getenv("MODEL_NAME", "deepseek-chat")
FALLBACK_MODEL = os.getenv("MODEL_FALLBACK", "deepseek-reasoner")

FAIL_THRESHOLD = 3          # 连续失败几次触发熔断
COOLDOWN_SECONDS = 30.0     # 熔断后多久允许试探

_client = AsyncOpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    max_retries=0,  # 重试由我们自己控制，SDK 内部重试会掩盖熔断计数
)

class CircuitState(str, Enum):
    CLOSED = "closed"        # 正常：主模型照常调用
    OPEN = "open"            # 熔断：跳过主模型，直接走备模型
    HALF_OPEN = "half_open"  # 半开：冷却结束，放行一次试探

_state = CircuitState.CLOSED
_fail_count = 0
_opened_at = 0.0

def get_circuit_state() -> str:
    """给 observe 的 llm span 用，让熔断动作在 trace 里看得见。"""
    _refresh_state()
    return _state.value

def _refresh_state() -> None:
    """OPEN 待满冷却时间就转 HALF_OPEN，放行一次试探。"""
    global _state
    if _state is CircuitState.OPEN and time.monotonic() - _opened_at >= COOLDOWN_SECONDS:
        _state = CircuitState.HALF_OPEN

def _on_success() -> None:
    global _state, _fail_count
    _state = CircuitState.CLOSED
    _fail_count = 0

def _on_failure() -> None:
    global _state, _fail_count, _opened_at
    _fail_count += 1
    # 半开试探失败要立刻重新熔断；否则累计到阈值才熔断
    if _state is CircuitState.HALF_OPEN or _fail_count >= FAIL_THRESHOLD:
        _state = CircuitState.OPEN
        _opened_at = time.monotonic()

async def _ask(model: str, messages: list, tools: list | None):
    """一次裸调用，不做任何熔断判断。"""
    resp = await _client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or None,  # 空列表统一转 None：本轮不允许调工具
        timeout=60,
    )
    return resp.choices[0].message

async def call_model(messages: list, tools: list | None = None):
    """一次模型调用，返回 assistant message（可能带 tool_calls）。

    熔断策略：熔断期间不抛错，而是跳过主模型直接走备模型——
    熔断是为了保护上游，不是为了拒绝用户。
    """
    _refresh_state()

    primary_error = None
    if _state is not CircuitState.OPEN:
        try:
            msg = await _ask(MODEL, messages, tools)
            _on_success()
            return msg
        except Exception as exc:
            primary_error = exc
            _on_failure()

    try:
        return await _ask(FALLBACK_MODEL, messages, tools)
    except Exception as exc:
        # 主备都挂：抛主模型的错，方便定位问题到底出在哪边
        raise primary_error or exc