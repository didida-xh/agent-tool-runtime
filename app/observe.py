"""可观测层：Langfuse 优先，失败自动降级为本地 JSON trace。

铁律：可观测性不能成为可用性的单点。Langfuse 未配置、SDK 缺失、网络挂掉、
接口报错 —— 任何一种情况都只降级、不抛异常，主流程照常跑完。

用法：
    async with observe.trace(task_id, question) as tr:
        with tr.span("llm", {"round": 1, "model": MODEL}) as sp:
            sp.set(output={"tool_calls": 1})
        with tr.span("tool", {"name": "get_order"}) as sp:
            sp.set(output={"status": "success"})
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

TRACE_DIR = Path(os.getenv("TRACE_DIR", "data/traces"))

_langfuse = None
_langfuse_ready: Optional[bool] = None


def _get_langfuse():
    """懒加载 Langfuse 客户端；任何异常都返回 None（降级）。"""
    global _langfuse, _langfuse_ready
    if _langfuse_ready is not None:
        return _langfuse
    _langfuse_ready = False
    try:
        if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
            return None
        from langfuse import Langfuse  # 可选依赖

        _langfuse = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        _langfuse_ready = True
    except Exception:
        _langfuse = None
    return _langfuse


class Span:
    """一次操作（LLM 调用 / 工具执行）的计时与属性记录。"""

    def __init__(self, name: str, trace: "Trace", metadata: Optional[Dict[str, Any]] = None):
        self.name = name
        self.trace = trace
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self.output: Any = None
        self.started = 0.0
        self.duration_ms = 0
        self._lf_span = None

    def set(self, **kwargs: Any) -> None:
        """记录 span 的输出等附加信息，例如 sp.set(output=..., error=...)。"""
        if "output" in kwargs:
            self.output = kwargs["output"]
        for key, value in kwargs.items():
            if key != "output":
                self.metadata[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": self.duration_ms,
            "metadata": _safe(self.metadata),
            "output": _safe(self.output),
        }


class Trace:
    """一个任务的完整调用链。"""

    def __init__(self, task_id: str, question: str):
        self.task_id = task_id
        self.question = question
        self.spans: List[Span] = []
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.status = "running"
        self._lf_trace = None

    @contextmanager
    def span(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> Iterator[Span]:
        sp = Span(name, self, metadata)
        lf = _get_langfuse()
        if lf is not None:
            try:
                sp._lf_span = lf.start_span(name=name, metadata=sp.metadata)
            except Exception:
                sp._lf_span = None
        sp.started = time.perf_counter()
        try:
            yield sp
        finally:
            sp.duration_ms = int((time.perf_counter() - sp.started) * 1000)
            self.spans.append(sp)
            if sp._lf_span is not None:
                try:
                    sp._lf_span.end(output=_safe(sp.output))
                except Exception:
                    pass

    def finish(self, status: str, answer: str = "") -> None:
        self.status = status
        payload = {
            "task_id": self.task_id,
            "question": self.question,
            "status": status,
            "answer": answer,
            "started_at": self.started_at,
            "spans": [s.to_dict() for s in self.spans],
        }
        self._dump_local(payload)
        if self._lf_trace is not None:
            try:
                self._lf_trace.update(output=_safe(answer), metadata={"status": status})
                lf = _get_langfuse()
                if lf is not None:
                    lf.flush()
            except Exception:
                pass

    def _dump_local(self, payload: Dict[str, Any]) -> None:
        """本地兜底落盘，Langfuse 不可用时这是唯一的链路证据。"""
        try:
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            path = TRACE_DIR / f"{self.task_id}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


@asynccontextmanager
async def trace(task_id: str, question: str):
    """任务级 trace。用法：async with observe.trace(tid, q) as tr: ..."""
    tr = Trace(task_id, question)
    lf = _get_langfuse()
    if lf is not None:
        try:
            tr._lf_trace = lf.trace(name="agent-task", input=_safe(question), id=task_id)
        except Exception:
            tr._lf_trace = None
    try:
        yield tr
    finally:
        if tr.status == "running":
            tr.finish("unknown")


def _safe(value: Any) -> Any:
    """保证可 JSON 序列化，避免 trace 反向把主流程搞挂。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)
