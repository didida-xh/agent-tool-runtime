"""FastAPI 入口：POST /chat 跑完整循环，任务与调用链落库。

启动：uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app import cache, chain, db, observe
from app.agent_loop import run_agent

app = FastAPI(title="agent-tool-runtime")
app.include_router(chain.router)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, description="用户问题")
    confirm_token: Optional[str] = Field(default=None, description="写操作确认凭证")


async def _replay(task_id: str):
    """幂等命中：直接返回上次任务的结果，不再执行一遍。"""
    task = await db.fetch_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="幂等记录指向的任务不存在")
    return {
        "task_id": task["id"],
        "answer": task["final_answer"],
        "rounds": task["rounds"],
        "status": task["status"],
        "replayed": True,
    }


@app.post("/chat")
async def chat(
    req: ChatRequest,
    x_idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
):
    # 退款等写操作重试代价高：同一个 key 只真正执行一次
    if x_idempotency_key:
        existing = await cache.lookup(x_idempotency_key)
        if existing:
            return await _replay(existing)

    task_id = uuid.uuid4().hex
    if x_idempotency_key and not await cache.claim(x_idempotency_key, task_id):
        existing = await cache.lookup(x_idempotency_key)
        if existing:
            return await _replay(existing)

    await db.create_task(task_id, req.question)
    async with observe.trace(task_id, req.question) as tr:
        try:
            result = await run_agent(
                req.question,
                task_id=task_id,
                confirm_token=req.confirm_token,
                tracer=tr,
            )
        except Exception as exc:
            # 循环本身不抛业务异常，走到这里只剩基础设施故障（PG/模型挂了）
            tr.finish("error", str(exc))
            await db.finish_task(task_id, "failed", str(exc), 0)
            raise HTTPException(status_code=500, detail=f"任务执行失败: {exc}")
        tr.finish(result.status, result.answer)
        await db.finish_task(task_id, result.status, result.answer, result.rounds)
        return {
            "task_id": task_id,
            "answer": result.answer,
            "rounds": result.rounds,
            "status": result.status,
        }


@app.get("/health")
async def health():
    return {"ok": True}
