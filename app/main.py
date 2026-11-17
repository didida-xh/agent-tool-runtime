"""FastAPI 入口：POST /chat 跑完整循环，任务与调用链落库。

启动：uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import db, observe
from app.agent_loop import run_agent

app = FastAPI(title="agent-tool-runtime")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, description="用户问题")
    confirm_token: Optional[str] = Field(default=None, description="写操作确认凭证")


@app.post("/chat")
async def chat(req: ChatRequest):
    task_id = uuid.uuid4().hex
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
            await db.finish_task(task_id, "error", str(exc), 0)
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
