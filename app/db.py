"""PG 访问层：连接池 + 工具用到的三个查询/写操作。"""
import json
import os

import asyncpg

_pool = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    return _pool


async def fetch_order(order_id: str):
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT order_id, customer, city, status, amount::float8 AS amount "
        "FROM orders WHERE order_id = $1",
        order_id,
    )
    return dict(row) if row else None


async def fetch_inventory(sku: str):
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT sku, name, stock, warehouse_city FROM inventory WHERE sku = $1",
        sku,
    )
    return dict(row) if row else None


async def mark_refunding(order_id: str) -> bool:
    """把订单置为退款中（仅 paid/shipped/delivered 可退），返回是否改动了行。"""
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE orders SET status = 'refunding' "
        "WHERE order_id = $1 AND status IN ('paid','shipped','delivered') "
        "RETURNING order_id",
        order_id,
    )
    return row is not None



async def create_task(task_id: str, request: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO tasks (id, request, status) VALUES ($1, $2, 'running')",
        task_id,
        request,
    )


async def finish_task(task_id: str, status: str, answer: str, rounds: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE tasks SET status = $2, final_answer = $3, rounds = $4 WHERE id = $1",
        task_id,
        status,
        answer,
        rounds,
    )


async def record_tool_call(
    *,
    task_id: str,
    round_no: int,
    tool_name: str,
    arguments: dict,
    result: str,
    status: str,
    duration_ms: int,
    error: str = "",
) -> None:
    """每执行完一次工具立刻写一条，不等任务结束——中途崩了链路也还在。"""
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO tool_calls "
        "(task_id, round, tool_name, arguments, result_truncated, status, duration_ms, error) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)",
        task_id,
        round_no,
        tool_name,
        json.dumps(arguments, ensure_ascii=False, default=str),
        (result or "")[:512],  # 入库截断 512，回填给模型的另按 2000 截
        status,
        duration_ms,
        error or None,
    )


async def fetch_task(task_id: str):
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, request, status, final_answer, rounds, trace_id, created_at "
        "FROM tasks WHERE id = $1",
        task_id,
    )
    return dict(row) if row else None


async def fetch_tool_calls(task_id: str):
    """调用链页面用：按执行顺序取回全部调用记录。

    arguments 取 ::text——asyncpg 返回 jsonb 时是字符串，直接给前端展示更省事。
    """
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT round, tool_name, arguments::text AS arguments, result_truncated, "
        "status, duration_ms, error, created_at FROM tool_calls "
        "WHERE task_id = $1 ORDER BY id",
        task_id,
    )
    return [dict(r) for r in rows]
