"""退款：唯一的写操作，权限等级 WRITE，调用必须带 X-Confirm-Token。"""

from typing import Literal

from pydantic import BaseModel, Field

from app.db import fetch_order, mark_refunding
from app.tools.registry import Permission, tool

REFUNDABLE = ("paid", "shipped", "delivered")


class RefundArgs(BaseModel):
    order_id: str = Field(min_length=1, description="订单号")
    amount: float = Field(gt=0, description="退款金额（元），不得超过订单金额")
    reason: Literal["no_longer_needed", "quality_issue", "damaged"] = Field(
        description="退款原因"
    )


@tool(
    name="refund",
    description="对已支付订单发起退款，会真实改库。高危操作：必须先向用户说明金额并取得明确同意，"
    "不要对未支付/已退款的订单调用。",
    args_model=RefundArgs,
    timeout=5.0,
    permission=Permission.WRITE,
)
async def refund(order_id: str, amount: float, reason: str) -> dict:
    # 业务拒绝一律用 success + refunded=False 表达：
    # 抛异常会被注册表归为 ERROR（"服务端问题，不要重试"），模型就没有自愈机会了。
    order = await fetch_order(order_id)
    if order is None:
        return {"refunded": False, "code": "order_not_found", "message": f"订单 {order_id} 不存在"}

    if order["status"] not in REFUNDABLE:
        return {
            "refunded": False,
            "code": "status_not_refundable",
            "message": f"订单当前状态为 {order['status']}，不可退款",
        }

    if amount > order["amount"]:
        return {
            "refunded": False,
            "code": "amount_exceeds_order",
            "message": f"退款金额 {amount} 超过订单金额 {order['amount']}，最多可退 {order['amount']}",
        }

    if not await mark_refunding(order_id):
        return {
            "refunded": False,
            "code": "status_changed",
            "message": "订单状态已变化，未执行，请重新查询后确认",
        }

    return {"refunded": True, "order_id": order_id, "amount": amount, "reason": reason}
