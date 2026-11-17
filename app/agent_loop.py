"""多轮工具调用循环引擎（Day2 核心）。

三条契约：
- run_agent 是唯一入口；call_model 与 record_tool_call 都可注入，测试不依赖网络；
- 是否继续循环的唯一判据是 msg.tool_calls 非空
  （实测 deepseek-reasoner 带 tool_calls 时 content 也非空，别用 content 判断）；
- 工具失败已被 registry 转成可回填的文本，这里只负责回填并继续，绝不抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from contextlib import nullcontext

from app import db
from app.llm import MODEL, call_model as default_call_model, get_circuit_state
from app.tools import order, refund, weather  # noqa: F401  import 即触发 @tool 注册
from app.tools.registry import REGISTRY


SYSTEM_PROMPT = (
    "你是业务助手。回答前优先用工具查证，不要编造数据。"
    "涉及退款等写操作时：先复述订单号、金额、原因并请用户确认；"
    "用户明确回复确认后立即调用工具执行，不要重复询问；"
    "用户的退款原因按语义归入最接近的枚举值，不要让用户从枚举里挑。"
)

FORCE_SUMMARY = "已达工具调用上限，请基于已有信息直接总结回答，不要再调用工具。"

@dataclass
class LoopResult:
    answer:str
    rounds:int
    status:str

def _span(tracer, name: str, metadata: dict):
    """有 tracer 就开 span，没有就给个空上下文。"""
    if tracer is None:
        return nullcontext(None)
    return tracer.span(name, metadata)

async def run_agent(
    question: str,
    *,
    task_id: str,
    confirm_token: Optional[str] = None,
    call_model: Callable[..., Awaitable[Any]] = default_call_model,
    record_tool_call: Callable[..., Awaitable[None]] = db.record_tool_call,
    max_rounds: int = 5,
    tracer: Any = None,
) -> LoopResult:
    messages=[
        {"role":"system","content":SYSTEM_PROMPT},
        {"role":"user","content":question},
    ]
    rounds=0

    while rounds<max_rounds:
        with _span(
            tracer,
            "llm",
            {"round": rounds + 1, "model": MODEL, "circuit": get_circuit_state()},
        ) as sp:
            msg=await call_model(messages,REGISTRY.openai_tools())
            if sp is not None:
                sp.set(
                    output={
                        "tool_calls": [tc.function.name for tc in (msg.tool_calls or [])],
                        "content_chars": len(msg.content or ""),
                    }
                )

        assistant = {"role":"assistant","content":msg.content}
        if msg.tool_calls:
            assistant["tool_calls"]=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,  # 保持字符串
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant)

        if not msg.tool_calls:
            return LoopResult(answer=(msg.content or "").strip(), rounds=rounds, status="success")

        rounds+=1
        for tc in msg.tool_calls:
            with _span(tracer, "tool", {"name": tc.function.name, "round": rounds}) as sp:
                result = await REGISTRY.execute(
                    tc.function.name, tc.function.arguments, confirm_token=confirm_token
                )
                if sp is not None:
                    sp.set(
                        status=result.status.value,
                        output={"content": (result.content or "")[:200]},
                    )
 # 落库用 dict；模型给的 JSON 坏了就存原文，排查"模型为什么反复犯错"全靠它
            try:
                arguments = json.loads(tc.function.arguments or "{}")
                if not isinstance(arguments, dict):
                    arguments = {"raw": tc.function.arguments}
            except json.JSONDecodeError:
                arguments = {"raw": tc.function.arguments}

            await record_tool_call(
                task_id=task_id,
                round_no=rounds,
                tool_name=tc.function.name,
                arguments=arguments,
                result=result.content,
                status=result.status.value,  # 枚举要 .value，否则 asyncpg 拒收
                duration_ms=result.duration_ms,
                error=result.error_code,
            )
 # 失败也回填——自愈全靠模型读到这段错误文本
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": result.content,
                }
            )
        # 超限收尾：注入停止指令 + tools=None，这次纯文本调用不算工具轮次
    messages.append({"role": "system", "content": FORCE_SUMMARY})
    msg = await call_model(messages, None)
    return LoopResult(answer=(msg.content or "").strip(), rounds=rounds, status="max_rounds")













