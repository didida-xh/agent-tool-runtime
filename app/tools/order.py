"""订单与库存查询：只读工具，数据来自 PG。"""

from typing import Optional

from pydantic import BaseModel, Field

from app.db import fetch_inventory, fetch_order
from app.tools.registry import tool


class OrderArgs(BaseModel):
    order_id: str = Field(min_length=1, description="订单号，如 A1001")


class InventoryArgs(BaseModel):
    sku: str = Field(min_length=1, description="商品 SKU，如 SKU-001")


@tool(
    name="get_order",
    description="按订单号查订单详情（客户、收货城市、状态、金额）。查商品库存请用 get_inventory。",
    args_model=OrderArgs,
    timeout=3.0,
)
async def get_order(order_id: str) -> Optional[dict]:
    # 查不到返回 None：注册表会转成 success + data=None，模型据此答复"无此订单"，不会重试
    return await fetch_order(order_id)


@tool(
    name="get_inventory",
    description="按 SKU 查库存数量与所在仓城市。需要 SKU，不要拿商品名当 SKU。",
    args_model=InventoryArgs,
    timeout=3.0,
)
async def get_inventory(sku: str) -> Optional[dict]:
    return await fetch_inventory(sku)
