"""调用链页面：GET /chain?task_id= 返回自包含 HTML 时间瀑布。

两条安全约定：
- 工具返回内容不可信，所有动态字段一律 html.escape；
- 不引入前端框架、外部 CSS 或 JS，单个 HTML 直接可看。

数据来源优先取本地 trace 文件（llm + tool span 齐全、顺序准确），
没有 trace 时退化为 tool_calls 表，保证老任务也能看。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app import db, observe

router = APIRouter()

STATUS_COLOR = {
    "success": "#16a34a",
    "invalid": "#d97706",
    "timeout": "#ea580c",
    "denied": "#6b7280",
    "error": "#dc2626",
    "running": "#2563eb",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _load_trace(task_id: str) -> Optional[Dict[str, Any]]:
    path = Path(observe.TRACE_DIR) / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _items_from_trace(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    for span in trace.get("spans", []):
        meta = span.get("metadata") or {}
        items.append(
            {
                "name": span.get("name", ""),
                "detail": f"round {meta.get('round', '-')} · {meta.get('name') or meta.get('model') or ''}",
                "duration_ms": int(span.get("duration_ms") or 0),
                "status": meta.get("status", "success"),
                "payload": json.dumps(span.get("output"), ensure_ascii=False, indent=2),
                "error": "",
            }
        )
    return items


def _items_from_calls(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": call["tool_name"],
            "detail": f"round {call['round']}",
            "duration_ms": int(call["duration_ms"] or 0),
            "status": call["status"],
            "payload": f"入参 {call['arguments']}\n\n结果 {call['result_truncated'] or ''}",
            "error": call["error"] or "",
        }
        for call in calls
    ]


def _render(task: Dict[str, Any], items: List[Dict[str, Any]], trace_path: Optional[Path]) -> str:
    max_ms = max((item["duration_ms"] for item in items), default=0) or 1

    rows = []
    for index, item in enumerate(items, 1):
        color = STATUS_COLOR.get(item["status"], "#6b7280")
        width = max(2, round(item["duration_ms"] / max_ms * 100))
        error = (
            f'<div class="err">错误：{_esc(item["error"])}</div>' if item["error"] else ""
        )
        rows.append(
            f"""
        <div class="row">
          <div class="idx">{index}</div>
          <div class="name">{_esc(item["name"])}<span class="detail">{_esc(item["detail"])}</span></div>
          <div class="track"><div class="bar" style="width:{width}%;background:{color}"></div></div>
          <div class="ms">{item["duration_ms"]} ms</div>
          <div class="badge" style="color:{color};border-color:{color}">{_esc(item["status"])}</div>
        </div>
        <details><summary>查看入参与输出</summary><pre>{_esc(item["payload"])}</pre>{error}</details>"""
        )

    trace_line = (
        f'<div class="hint">本地 trace：{_esc(str(trace_path))}</div>'
        if trace_path
        else '<div class="hint">本地无 trace 文件，数据来自 tool_calls 表</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>调用链 {_esc(task["id"])}</title>
<style>
  body {{ margin:0; padding:32px; background:#f6f7f9; color:#111827;
          font-family:-apple-system,"PingFang SC",sans-serif; }}
  .wrap {{ max-width:980px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 6px; }}
  .meta {{ color:#6b7280; font-size:13px; margin-bottom:20px; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px;
           padding:18px 20px; margin-bottom:16px; }}
  .q {{ font-size:15px; line-height:1.6; }}
  .row {{ display:flex; align-items:center; gap:12px; padding:8px 0; }}
  .idx {{ width:20px; color:#9ca3af; font-size:12px; text-align:right; }}
  .name {{ width:190px; font-size:14px; font-weight:600; }}
  .detail {{ display:block; font-weight:400; color:#9ca3af; font-size:11px; margin-top:2px; }}
  .track {{ flex:1; height:14px; background:#f3f4f6; border-radius:4px; overflow:hidden; }}
  .bar {{ height:100%; border-radius:4px; }}
  .ms {{ width:72px; text-align:right; font-size:12px; color:#4b5563;
         font-variant-numeric:tabular-nums; }}
  .badge {{ width:70px; text-align:center; font-size:11px; padding:2px 0;
            border:1px solid; border-radius:10px; }}
  details {{ margin:2px 0 10px 240px; font-size:12px; color:#6b7280; }}
  summary {{ cursor:pointer; }}
  pre {{ white-space:pre-wrap; word-break:break-all; background:#f9fafb;
         border:1px solid #eef0f2; border-radius:6px; padding:10px;
         font-size:12px; color:#374151; margin:6px 0 0; }}
  .err {{ color:#dc2626; margin-top:6px; }}
  .answer {{ white-space:pre-wrap; line-height:1.7; font-size:14px; }}
  .hint {{ color:#6b7280; font-size:12px; margin-top:8px; }}
</style>
</head>
<body><div class="wrap">
  <h1>任务调用链</h1>
  <div class="meta">
    {_esc(task["id"])} · 状态 {_esc(task["status"])} · 工具轮次 {_esc(task["rounds"])}
    · 创建于 {_esc(task["created_at"])}
  </div>
  <div class="card"><div class="q">{_esc(task["request"])}</div>{trace_line}</div>
  <div class="card">{"".join(rows) or '<div class="hint">本次任务没有工具调用</div>'}</div>
  <div class="card"><div class="meta" style="margin:0 0 8px">最终回答</div>
    <div class="answer">{_esc(task["final_answer"] or "（无）")}</div></div>
</div></body></html>"""


@router.get("/chain", response_class=HTMLResponse)
async def chain(task_id: str):
    task = await db.fetch_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    calls = await db.fetch_tool_calls(task_id)
    trace = _load_trace(task_id)

    if trace and trace.get("spans"):
        items = _items_from_trace(trace)
    else:
        items = _items_from_calls(calls)

    trace_path = Path(observe.TRACE_DIR) / f"{task_id}.json"
    return HTMLResponse(_render(task, items, trace_path if trace else None))


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str):
    """页面同源 JSON 接口，方便脚本核对页面数据。"""
    task = await db.fetch_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task, "calls": await db.fetch_tool_calls(task_id)}
