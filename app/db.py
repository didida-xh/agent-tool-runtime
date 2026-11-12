"""PG 访问层：连接池 + 工具用到的三个查询/写操作。"""
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
