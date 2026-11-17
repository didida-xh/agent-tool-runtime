"""Day2 循环引擎测试：fake call_model 定义契约，agent_loop 的 4 个空填完即转绿。

现在跑应该是 4 failed（NotImplementedError），这是有意的红灯。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent_loop import FORCE_SUMMARY, LoopResult, run_agent


def make_msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def make_tc(id_, name, arguments):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))


class FakeModel:
    """按顺序返回固定消息，并记录每次调用时的消息数与 tools 参数。"""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def __call__(self, messages, tools=None):
        self.calls.append(
            {"n_messages": len(messages), "tools": tools, "messages": list(messages)}
        )
        return self.replies[len(self.calls) - 1]


class FakeRecorder:
    def __init__(self):
        self.rows = []

    async def __call__(self, **kw):
        self.rows.append(kw)


async def test_direct_answer():
    """无 tool_calls：一轮直答，不落任何调用记录。"""
    fake = FakeModel([make_msg(content="你好，我可以帮你")])
    rec = FakeRecorder()
    out = await run_agent("你好", task_id="t1", call_model=fake, record_tool_call=rec)
    assert out == LoopResult(answer="你好，我可以帮你", rounds=0, status="success")
    assert fake.calls[0]["tools"] is not None  # 首轮必须带工具
    assert rec.rows == []


async def test_two_rounds_with_real_tool():
    """调一个真实只读工具后收敛：消息序列与落库字段都要对。"""
    fake = FakeModel(
        [
            make_msg(tool_calls=[make_tc("c1", "get_inventory", '{"sku": "SKU-001"}')]),
            make_msg(content="有货"),
        ]
    )
    rec = FakeRecorder()
    out = await run_agent("查库存", task_id="t2", call_model=fake, record_tool_call=rec)
    assert out.status == "success" and out.rounds == 1

    # 第二次调用时 messages = system + user + assistant + tool
    assert fake.calls[1]["n_messages"] == 4
    assistant_msg = fake.calls[1]["messages"][2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "c1"
    tool_msg = fake.calls[1]["messages"][3]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"

    row = rec.rows[0]
    assert row["task_id"] == "t2" and row["round_no"] == 1
    assert row["tool_name"] == "get_inventory"
    assert row["arguments"] == {"sku": "SKU-001"}
    assert row["status"] == "success"
    assert row["duration_ms"] >= 0


async def test_tool_error_backfilled_not_raised():
    """工具失败（不存在的工具名）不中断循环，错误文本回填给模型。"""
    fake = FakeModel(
        [
            make_msg(tool_calls=[make_tc("c1", "no_such_tool", '{"a": 1}')]),
            make_msg(content="换个方式回答你"),
        ]
    )
    rec = FakeRecorder()
    out = await run_agent("查个东西", task_id="t3", call_model=fake, record_tool_call=rec)
    assert out.answer == "换个方式回答你"

    assert rec.rows[0]["status"] == "invalid"
    assert rec.rows[0]["error"] == "unknown_tool"
    assert rec.rows[0]["arguments"] == {"a": 1}

    tool_msg = fake.calls[1]["messages"][3]
    assert "unknown_tool" in tool_msg["content"]  # 模型能读到错误码


async def test_max_rounds_forces_summary():
    """连续 5 轮都在调工具：第 6 次调用必须不带 tools，且注入了停止指令。"""
    replies = [
        make_msg(tool_calls=[make_tc(f"c{i}", "no_such_tool", "{}")]) for i in range(5)
    ] + [make_msg(content="基于已有信息的总结")]
    fake = FakeModel(replies)
    rec = FakeRecorder()
    out = await run_agent("一直查", task_id="t4", call_model=fake, record_tool_call=rec)

    assert out.status == "max_rounds" and out.rounds == 5
    assert out.answer == "基于已有信息的总结"
    assert len(fake.calls) == 6
    assert fake.calls[5]["tools"] is None  # 收尾调用不许带工具
    assert any(m.get("content") == FORCE_SUMMARY for m in fake.calls[5]["messages"])
    assert len(rec.rows) == 5  # 5 轮各落一条
