"""Day1 验收测试：注册表「校验 → 执行 → 结构化返回」全链路。

被测实现是 app/tools/registry.py 的类式 API（REGISTRY.execute 返回 ToolResult dataclass）。
两层用例，职责分开：
- TestRegistryMechanics：用临时注册的假工具验证注册表机制，不依赖真实工具与数据库，必须常年全绿；
- TestDay1ToolContract：四个真实工具的契约与数据，工具未实现时显式 skip（不静默通过）。

约定（改实现前先看这里）：
- 失败是返回值不是异常；status ∈ success / invalid / timeout / denied / error
- content 是回填给模型的正文（JSON 字符串），data 是未截断的原始业务数据
- 业务上"查不到"不是工具故障：get_order / get_inventory 返回 success + data=None
"""

import asyncio
import json
import os
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from app.tools.registry import (
    MAX_RESULT_CHARS,
    REGISTRY,
    Permission,
    ToolRegistry,
    ToolStatus,
    tool,
)


# ---------- 测试工具装置 ----------

class EchoArgs(BaseModel):
    text: str = Field(description="任意文本")


class ColorArgs(BaseModel):
    color: Literal["red", "blue"] = Field(description="颜色")


def make_registry() -> ToolRegistry:
    """独立注册表，避免测试工具污染全局 REGISTRY。"""
    reg = ToolRegistry()

    @tool(name="echo", description="回显文本", args_model=EchoArgs, registry=reg)
    async def echo(text: str):
        return {"echo": text}

    @tool(name="slow", description="故意很慢", args_model=EchoArgs, timeout=0.05, registry=reg)
    async def slow(text: str):
        await asyncio.sleep(1)
        return {"echo": text}

    @tool(name="boom", description="总是抛异常", args_model=EchoArgs, registry=reg)
    async def boom(text: str):
        raise RuntimeError("模拟数据库连接断开")

    @tool(
        name="pay",
        description="模拟写操作",
        args_model=EchoArgs,
        permission=Permission.WRITE,
        registry=reg,
    )
    async def pay(text: str):
        return {"paid": text}

    return reg


def err(result) -> dict:
    """把 ToolResult.content 当 JSON 解析，方便断言错误结构。"""
    return json.loads(result.content)


# ---------- 第一层：注册表机制 ----------

class TestRegistryMechanics:
    async def test_schema_shape(self):
        spec = {t["function"]["name"]: t["function"] for t in make_registry().openai_tools()}
        params = spec["echo"]["parameters"]
        assert params["type"] == "object"
        assert params["required"] == ["text"]
        # title 只浪费 token，字段名本身已经是 key
        assert "title" not in params
        assert "title" not in params["properties"]["text"]
        assert params["properties"]["text"]["description"] == "任意文本"

    async def test_enum_in_schema(self):
        reg = ToolRegistry()

        @tool(name="paint", description="上色", args_model=ColorArgs, registry=reg)
        async def paint(color: str):
            return {"color": color}

        props = reg.openai_tools()[0]["function"]["parameters"]["properties"]
        assert props["color"]["enum"] == ["red", "blue"]

    async def test_success(self):
        r = await make_registry().execute("echo", '{"text": "你好"}')
        assert r.status is ToolStatus.SUCCESS
        assert r.data == {"echo": "你好"}
        assert json.loads(r.content) == {"echo": "你好"}
        assert r.duration_ms >= 0
        assert r.error_code == ""

    async def test_unknown_tool(self):
        r = await make_registry().execute("nope", "{}")
        assert r.status is ToolStatus.INVALID
        assert r.error_code == "unknown_tool"
        assert "echo" in err(r)["hint"]

    async def test_invalid_json(self):
        r = await make_registry().execute("echo", '{"text": "没闭合')
        assert r.status is ToolStatus.INVALID
        assert err(r)["error_code"] == "invalid_json"

    async def test_json_not_object(self):
        r = await make_registry().execute("echo", '["text"]')
        assert r.status is ToolStatus.INVALID
        assert err(r)["error_code"] == "invalid_json"

    async def test_missing_required_field(self):
        r = await make_registry().execute("echo", "{}")
        assert r.status is ToolStatus.INVALID
        body = err(r)
        assert body["error_code"] == "invalid_arguments"
        assert body["fields"][0]["field"] == "text"
        assert body["fields"][0]["expected"] == "string"

    async def test_invalid_enum_carries_allowed_values(self):
        reg = ToolRegistry()

        @tool(name="paint", description="上色", args_model=ColorArgs, registry=reg)
        async def paint(color: str):
            return {"color": color}

        r = await reg.execute("paint", '{"color": "green"}')
        assert r.status is ToolStatus.INVALID
        expected = err(r)["fields"][0]["expected"]
        assert "red" in expected and "blue" in expected

    async def test_denied_without_confirm_token(self):
        r = await make_registry().execute("pay", '{"text": "100"}')
        assert r.status is ToolStatus.DENIED
        assert r.error_code == "need_human_confirm"
        # 文案必须禁止模型自行重试，否则 5 轮会被同一个工具烧完
        assert "不要自行重试" in err(r)["hint"]

    async def test_write_with_confirm_token(self):
        r = await make_registry().execute("pay", '{"text": "100"}', confirm_token="demo")
        assert r.status is ToolStatus.SUCCESS

    async def test_timeout(self):
        r = await make_registry().execute("slow", '{"text": "x"}')
        assert r.status is ToolStatus.TIMEOUT
        assert r.error_code == "timeout"
        assert r.duration_ms >= 50

    async def test_tool_exception_becomes_error(self):
        r = await make_registry().execute("boom", '{"text": "x"}')
        assert r.status is ToolStatus.ERROR
        assert r.error_code == "tool_error"
        assert "RuntimeError" in err(r)["message"]

    async def test_long_result_truncated(self):
        r = await make_registry().execute("echo", json.dumps({"text": "x" * (MAX_RESULT_CHARS * 2)}))
        assert r.status is ToolStatus.SUCCESS
        assert len(r.content) <= MAX_RESULT_CHARS + 40
        assert "已截断" in r.content
        assert len(r.data["echo"]) == MAX_RESULT_CHARS * 2  # data 不截断

    async def test_duplicate_registration_rejected(self):
        reg = ToolRegistry()

        @tool(name="dup", description="第一次", args_model=EchoArgs, registry=reg)
        async def first(text: str):
            return text

        with pytest.raises(ValueError):
            @tool(name="dup", description="第二次", args_model=EchoArgs, registry=reg)
            async def second(text: str):
                return text


# ---------- 第二层：四个真实工具的契约 ----------

class TestDay1ToolContract:
    @pytest.fixture(autouse=True)
    def _load_tools(self):
        try:
            import app.tools.order  # noqa: F401
            import app.tools.refund  # noqa: F401
            import app.tools.weather  # noqa: F401
        except ImportError as exc:
            pytest.skip(f"四个工具尚未实现：{exc}")

    async def test_all_four_registered(self):
        assert {"get_weather", "get_order", "get_inventory", "refund"} <= set(REGISTRY.names())
        spec = {t["function"]["name"]: t["function"] for t in REGISTRY.openai_tools()}
        assert set(spec["get_order"]["parameters"]["required"]) == {"order_id"}
        assert set(spec["get_inventory"]["parameters"]["required"]) == {"sku"}

    async def test_get_order_success(self):
        r = await REGISTRY.execute("get_order", '{"order_id": "A1001"}')
        assert r.status is ToolStatus.SUCCESS
        assert r.data["city"] == "杭州"
        assert r.data["amount"] == 299.0

    async def test_get_order_not_found_is_not_error(self):
        """查不到是业务结果：必须 success + data=None，否则模型会反复重试。"""
        r = await REGISTRY.execute("get_order", '{"order_id": "NOPE-999"}')
        assert r.status is ToolStatus.SUCCESS
        assert r.data is None

    async def test_get_inventory_success(self):
        r = await REGISTRY.execute("get_inventory", '{"sku": "SKU-001"}')
        assert r.status is ToolStatus.SUCCESS
        assert r.data["stock"] == 120

    async def test_get_inventory_not_found_is_not_error(self):
        r = await REGISTRY.execute("get_inventory", '{"sku": "SKU-NONE"}')
        assert r.status is ToolStatus.SUCCESS
        assert r.data is None

    async def test_refund_denied_without_token(self):
        args = '{"order_id": "A1001", "amount": 50, "reason": "no_longer_needed"}'
        r = await REGISTRY.execute("refund", args)
        assert r.status is ToolStatus.DENIED
        assert err(r)["error_code"] == "need_human_confirm"

    async def test_refund_success_with_token(self):
        args = '{"order_id": "A1001", "amount": 50, "reason": "no_longer_needed"}'
        try:
            r = await REGISTRY.execute("refund", args, confirm_token="demo-token")
            assert r.status is ToolStatus.SUCCESS
        finally:
            # 种子数据复位，保证用例可重复执行
            from app.db import get_pool

            pool = await get_pool()
            await pool.execute("UPDATE orders SET status = 'paid' WHERE order_id = 'A1001'")

    @pytest.mark.skipif(not os.getenv("RUN_NETWORK_TESTS"), reason="需要外网（Open-Meteo）")
    async def test_weather_success(self):
        r = await REGISTRY.execute("get_weather", '{"city": "杭州", "date": "today"}')
        assert r.status is ToolStatus.SUCCESS
        assert "temperature_max" in r.data
