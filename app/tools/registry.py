"""app/tools/registry.py

工具注册表：一个工具 = 契约（给模型看）+ 校验模型（给代码把关）+ 执行策略（超时/权限）。

三条硬约束：
1. 任何失败都不抛异常，统一转成 tool 消息回填 —— 模型看得见失败，才谈得上自愈；
2. JSON Schema 给模型看（它是"使用说明书"），Pydantic 给代码把关（它是"安检门"），两层都不可省；
3. 写操作缺确认 token 时，拒绝文案必须写明"不要自行重试"，否则模型会反复调同一个工具把轮次耗光。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

# 回填给模型的单条工具结果上限；DB 里另按 512 字符截断（两者用途不同，别混用）
MAX_RESULT_CHARS = 2000

class Permission(str, Enum):
    READONLY = "readonly"
    WRITE = "write"

class ToolStatus(str, Enum):
    SUCCESS = "success"
    INVALID = "invalid"   # 参数解析/校验失败 —— 可自愈
    TIMEOUT = "timeout"
    DENIED = "denied"     # 权限不足（缺确认 token）—— 不可自愈，需人工
    ERROR = "error"       # 工具内部异常

@dataclass
class ToolResult:
    tool_name: str
    status: ToolStatus
    content: str                    # 直接塞进 role=tool 消息的正文
    duration_ms: int = 0
    data: Any = None                # 业务原始数据，不截断，供落库/页面使用
    error_code: str = ""

@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]      # JSON Schema
    args_model: Type[BaseModel]
    func: Callable[..., Awaitable[Any]]
    timeout: float = 5.0
    permission: Permission = Permission.READONLY

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    # ---------- 注册与查询 ----------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具名重复注册: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def openai_tools(self) -> List[Dict[str, Any]]:
        """拼成 chat.completions 的 tools 参数。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in sorted(self._tools.values(), key=lambda s: s.name)
        ]

    def contract_table(self) -> str:
        """CLI --list 用：人眼可读的契约一览。"""
        lines = [f"{'工具':<16}{'权限':<10}{'超时':<8}必填参数"]
        for spec in sorted(self._tools.values(), key=lambda s: s.name):
            required = ", ".join(spec.parameters.get("required") or []) or "-"
            lines.append(
                f"{spec.name:<16}{spec.permission.value:<10}{spec.timeout:<8}{required}"
            )
        return "\n".join(lines)

    # ---------- 执行 ----------

    async def execute(
        self,
        name: str,
        raw_arguments: str,
        *,
        confirm_token: Optional[str] = None,
    ) -> ToolResult:
        started = time.perf_counter()

        def done(status: ToolStatus, content: str, error_code: str = "", data: Any = None) -> ToolResult:
            return ToolResult(
                tool_name=name,
                status=status,
                content=content,
                duration_ms=_elapsed_ms(started),
                data=data,
                error_code=error_code,
            )

        spec = self._tools.get(name)
        if spec is None:
            return done(
                ToolStatus.INVALID,
                _error_content(
                    "unknown_tool",
                    f"不存在名为 {name} 的工具",
                    hint=f"可用工具: {', '.join(self.names())}",
                ),
                "unknown_tool",
            )

        # 1) 权限：写操作必须带确认 token。
        #    这里的 confirm_token 只判"有没有"，真正有效性校验在路由层（绑任务级 token）。
        if spec.permission is Permission.WRITE and not confirm_token:
            return done(
                ToolStatus.DENIED,
                _error_content(
                    "need_human_confirm",
                    f"{name} 属于高危写操作，缺少人工确认凭证，本次未执行",
                    hint="不要自行重试本工具。先向用户说明将要执行的操作并请求确认，拿到 X-Confirm-Token 后再调用。",
                ),
                "need_human_confirm",
            )

        # 2) JSON 解析：模型偶尔会返回截断/夹带注释的 JSON，按"可自愈错误"处理。
        try:
            payload = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return done(
                ToolStatus.INVALID,
                _error_content(
                    "invalid_json",
                    f"arguments 不是合法 JSON: {exc.msg} (第 {exc.lineno} 行第 {exc.colno} 列)",
                    hint="请重新生成完整的 JSON 对象再调用，不要带注释、尾逗号或 Markdown 代码块。",
                ),
                "invalid_json",
            )
        if not isinstance(payload, dict):
            return done(
                ToolStatus.INVALID,
                _error_content(
                    "invalid_json",
                    f"arguments 必须是 JSON 对象，收到的是 {type(payload).__name__}",
                    hint="示例形态：{\"order_id\": \"A001\"}",
                ),
                "invalid_json",
            )

        # 3) Pydantic 二次校验：Schema 是提示，这里才是硬门禁。
        try:
            args = spec.args_model.model_validate(payload)
        except ValidationError as exc:
            return done(
                ToolStatus.INVALID,
                _format_validation_error(spec, exc),
                "invalid_arguments",
            )

        # 4) 执行 + 超时
        try:
            data = await asyncio.wait_for(spec.func(**args.model_dump()), timeout=spec.timeout)
        except asyncio.TimeoutError:
            return done(
                ToolStatus.TIMEOUT,
                _error_content(
                    "timeout",
                    f"{name} 超过 {spec.timeout}s 未返回，已中止",
                    hint="可换一种更简单的调用方式再试一次；若仍超时，直接告知用户该项暂不可用。",
                ),
                "timeout",
            )
        except asyncio.CancelledError:
            raise  # 外层取消要向上传播，不要吞掉
        except Exception as exc:  # 工具内部异常兜底，绝不把栈抛给模型
            return done(
                ToolStatus.ERROR,
                _error_content(
                    "tool_error",
                    f"{name} 执行失败: {type(exc).__name__}: {exc}",
                    hint="这是服务端问题，不要重复调用同一参数；可改用只读工具核对信息。",
                ),
                "tool_error",
            )

        content = _dump(data)
        if len(content) > MAX_RESULT_CHARS:
            content = content[:MAX_RESULT_CHARS] + f"...(已截断，原长 {len(content)} 字符)"
        return done(ToolStatus.SUCCESS, content, "", data)

# ---------- 装饰器 ----------

REGISTRY = ToolRegistry()

def tool(
    *,
    name: str,
    description: str,
    args_model: Type[BaseModel],
    timeout: float = 5.0,
    permission: Permission = Permission.READONLY,
    registry: Optional[ToolRegistry] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """把 async 函数注册成工具。

    description 的写法约定（直接影响模型选哪个工具）：
      一句话用途 + 需要什么前置信息 + 什么时候不要用它。
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        target = registry if registry is not None else REGISTRY
        target.register(
            ToolSpec(
                name=name,
                description=description,
                parameters=_build_parameters(args_model),
                args_model=args_model,
                func=func,
                timeout=timeout,
                permission=permission,
            )
        )
        return func

    return decorator

def _build_parameters(args_model: Type[BaseModel]) -> Dict[str, Any]:
    schema = args_model.model_json_schema()
    schema.pop("title", None)
    for prop in (schema.get("properties") or {}).values():
        prop.pop("title", None)  # 字段名已是 key，title 纯浪费 token
    schema.setdefault("type", "object")
    schema.setdefault("required", [])
    return schema

# ---------- 错误文案 ----------

def _error_content(error_code: str, message: str, *, hint: str = "") -> str:
    payload = {"error_code": error_code, "message": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)

def _format_validation_error(spec: ToolSpec, exc: ValidationError) -> str:
    """把 Pydantic 的报错翻译成"模型能照着改"的文本。

    错误信息三要素：哪个字段错了、期望什么、实际收到什么。
    缺一不可 —— 只有一句 "Validation error" 的文案，自愈率会接近 0。
    """
    props = spec.parameters.get("properties") or {}
    fields = []
    for err in exc.errors():
        loc = [str(x) for x in err["loc"] if x != "body"]
        field_name = ".".join(loc) or "(root)"
        prop = props.get(loc[0], {}) if loc else {}
        if "enum" in prop:
            expected = "枚举之一: " + " / ".join(map(str, prop["enum"]))
        elif prop.get("type"):
            expected = str(prop["type"])
        else:
            expected = "见工具参数说明"
        fields.append(
            {
                "field": field_name,
                "problem": err["msg"],
                "expected": expected,
                "received": err.get("input", "<缺失>"),
            }
        )
    return json.dumps(
        {
            "error_code": "invalid_arguments",
            "message": f"{spec.name} 参数校验失败，工具未执行",
            "fields": fields,
            "hint": "按 expected 修正后重新调用本工具；缺失字段不要编造，可先用其他工具查证。",
        },
        ensure_ascii=False,
        default=str,  # received 可能是不可序列化对象
    )

# ---------- 小工具 ----------

def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)

def _dump(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, default=str)

if __name__ == "__main__":
    # 自测入口：必须先 import 工具模块，装饰器才会执行注册
    import argparse

    from app.tools import order, refund, weather  # noqa: F401
    # python -m 下本文件以 __main__ 运行，工具模块 import 的是 app.tools.registry，
    # 是另一个模块对象，所以要打印那一份的 REGISTRY，否则永远是空表
    from app.tools import registry as impl

    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="打印工具契约表")
    args = parser.parse_args()
    if args.list:
        print(impl.REGISTRY.contract_table())
        print(f"\n共 {len(impl.REGISTRY)} 个工具")