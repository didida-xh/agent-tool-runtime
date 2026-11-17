"""熔断状态机测试：用 fake _ask 驱动，不联网、不真睡 30 秒。"""

from types import SimpleNamespace

import pytest

from app import llm


@pytest.fixture(autouse=True)
def reset_circuit():
    """每条用例从干净状态开始，避免全局变量互相污染。"""
    llm._state = llm.CircuitState.CLOSED
    llm._fail_count = 0
    llm._opened_at = 0.0
    yield


def make_ask(primary_ok, fallback_ok=True):
    """造一个假的 _ask：按模型名返回成功或抛错，并记录调用顺序。"""
    calls = []
    primary_seen = 0

    async def fake_ask(model, messages, tools):
        nonlocal primary_seen
        calls.append(model)
        if model == llm.MODEL:
            ok = primary_ok
            if isinstance(primary_ok, (list, tuple)):
                ok = primary_ok[min(primary_seen, len(primary_ok) - 1)]
            primary_seen += 1
        else:
            ok = fallback_ok
        if not ok:
            raise RuntimeError(f"{model} down")
        return SimpleNamespace(content="ok", tool_calls=None)

    return fake_ask, calls


async def test_opens_after_three_failures(monkeypatch):
    """主模型连败 3 次 → 熔断；期间备模型兜住请求。"""
    fake_ask, _ = make_ask(primary_ok=False)
    monkeypatch.setattr(llm, "_ask", fake_ask)

    for _ in range(3):
        msg = await llm.call_model([], None)
        assert msg.content == "ok"  # 备模型兜住了

    assert llm.get_circuit_state() == "open"
    assert llm._fail_count == 3


async def test_open_skips_primary(monkeypatch):
    """熔断期间不再请求主模型，只走备模型。"""
    fake_ask, calls = make_ask(primary_ok=False)
    monkeypatch.setattr(llm, "_ask", fake_ask)

    for _ in range(3):
        await llm.call_model([], None)
    calls.clear()

    await llm.call_model([], None)
    assert calls == [llm.FALLBACK_MODEL]  # 主模型被跳过


async def test_half_open_after_cooldown(monkeypatch):
    """冷却期满后状态从 open 转 half_open。"""
    llm._state = llm.CircuitState.OPEN
    llm._fail_count = 3
    llm._opened_at = llm.time.monotonic() - llm.COOLDOWN_SECONDS - 1

    assert llm.get_circuit_state() == "half_open"


async def test_half_open_success_closes(monkeypatch):
    """半开试探成功 → 回到 closed 且失败计数清零。"""
    llm._state = llm.CircuitState.HALF_OPEN
    llm._fail_count = 3
    fake_ask, calls = make_ask(primary_ok=True)
    monkeypatch.setattr(llm, "_ask", fake_ask)

    await llm.call_model([], None)

    assert calls == [llm.MODEL]  # 试探打的是主模型
    assert llm.get_circuit_state() == "closed"
    assert llm._fail_count == 0


async def test_half_open_failure_reopens(monkeypatch):
    """半开试探失败 → 立刻重新熔断，不用再等累计 3 次。"""
    llm._state = llm.CircuitState.HALF_OPEN
    llm._fail_count = 0
    fake_ask, _ = make_ask(primary_ok=False)
    monkeypatch.setattr(llm, "_ask", fake_ask)

    await llm.call_model([], None)

    assert llm._state is llm.CircuitState.OPEN


async def test_both_down_raises_primary_error(monkeypatch):
    """主备都挂：抛主模型的错误，方便定位根因。"""
    fake_ask, _ = make_ask(primary_ok=False, fallback_ok=False)
    monkeypatch.setattr(llm, "_ask", fake_ask)

    with pytest.raises(RuntimeError, match=f"{llm.MODEL} down"):
        await llm.call_model([], None)
