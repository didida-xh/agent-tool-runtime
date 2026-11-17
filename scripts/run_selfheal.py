"""D4 参数自愈评测。

做法：在模型第一次调用某个工具时，悄悄删掉一个必填参数。
registry 会返回 invalid + 错误文本，模型必须读懂错误并补齐参数重试——
这正是"参数缺字段自愈"要考核的能力，靠自然对话很难稳定复现。

用法：
    .venv/bin/python -m scripts.run_selfheal
    .venv/bin/python -m scripts.run_selfheal -n 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent_loop import run_agent  # noqa: E402
from app.llm import call_model as real_call_model  # noqa: E402
from scripts.run_eval import Recorder, check, load_tasks  # noqa: E402

REPORT_PATH = ROOT / "data/eval/selfheal.json"

# (用例 id, 目标工具, 要删掉的必填参数)
CASES = [
    ("e001", "get_order", "order_id"),
    ("e002", "get_order", "order_id"),
    ("e003", "get_order", "order_id"),
    ("e004", "get_order", "order_id"),
    ("e005", "get_order", "order_id"),
    ("e006", "get_order", "order_id"),
    ("e007", "get_order", "order_id"),
    ("e008", "get_order", "order_id"),
    ("e021", "get_inventory", "sku"),
    ("e022", "get_inventory", "sku"),
    ("e023", "get_inventory", "sku"),
    ("e024", "get_inventory", "sku"),
    ("e025", "get_inventory", "sku"),
    ("e026", "get_inventory", "sku"),
    ("e027", "get_inventory", "sku"),
    ("e036", "get_weather", "city"),
    ("e038", "get_weather", "city"),
    ("e040", "get_weather", "city"),
    ("e042", "get_weather", "city"),
    ("e044", "get_weather", "city"),
]


class Sabotage:
    """包住真实 call_model：第一次命中目标工具时删掉一个必填参数，只做一次。"""

    def __init__(self, tool_name: str, drop_key: str) -> None:
        self.tool_name = tool_name
        self.drop_key = drop_key
        self.done = False

    async def __call__(self, messages: List[Dict[str, Any]], tools: Any = None):
        msg = await real_call_model(messages, tools)
        if self.done or not msg.tool_calls:
            return msg

        calls = []
        changed = False
        for tc in msg.tool_calls:
            arguments = tc.function.arguments
            if not changed and tc.function.name == self.tool_name:
                try:
                    payload = json.loads(arguments or "{}")
                    if isinstance(payload, dict) and self.drop_key in payload:
                        payload.pop(self.drop_key)
                        arguments = json.dumps(payload, ensure_ascii=False)
                        changed = True
                except json.JSONDecodeError:
                    pass
            calls.append(
                SimpleNamespace(
                    id=tc.id,
                    function=SimpleNamespace(name=tc.function.name, arguments=arguments),
                )
            )

        if not changed:
            return msg
        self.done = True
        return SimpleNamespace(content=msg.content, tool_calls=calls)


async def run_one(task: Dict[str, Any], tool_name: str, drop_key: str) -> Dict[str, Any]:
    recorder = Recorder()
    model = Sabotage(tool_name, drop_key)
    out = await run_agent(
        task["question"],
        task_id=f"heal-{task['id']}",
        call_model=model,
        record_tool_call=recorder,
    )

    statuses = [r["status"] for r in recorder.rows]
    injected = "invalid" in statuses
    passed, problems = check(task, out.answer, out.rounds, recorder.rows)

    return {
        "id": task["id"],
        "question": task["question"],
        "tool": tool_name,
        "dropped": drop_key,
        "injected": injected,
        "passed": passed,
        "healed": injected and passed,
        "rounds": out.rounds,
        "statuses": statuses,
        "tools": [r["tool_name"] for r in recorder.rows],
        "problems": problems,
    }


async def amain(args: argparse.Namespace) -> int:
    tasks = {t["id"]: t for t in load_tasks()}
    cases = CASES[: args.n] if args.n else CASES

    results = []
    for index, (task_id, tool_name, drop_key) in enumerate(cases, 1):
        result = await run_one(tasks[task_id], tool_name, drop_key)
        results.append(result)
        mark = "HEAL" if result["healed"] else "FAIL"
        print(f"[{index:>2}/{len(cases)}] {result['id']} {mark} "
              f"rounds={result['rounds']} 状态={result['statuses']}")
        if not result["healed"]:
            print(f"        {result['question']}")
            print(f"        {'; '.join(result['problems']) or '未触发自愈'}")

    injected = [r for r in results if r["injected"]]
    healed = [r for r in results if r["healed"]]
    rate = len(healed) / len(injected) if injected else None

    print("\n===== 自愈汇总 =====")
    print(f"注入 {len(results)} 条 | 真实触发 invalid {len(injected)} 条")
    print(f"自愈成功 {len(healed)} 条 | 自愈率 "
          f"{'-' if rate is None else f'{rate * 100:.1f}%'}（验收线 70%）")

    REPORT_PATH.write_text(
        json.dumps(
            {
                "summary": {
                    "injected_total": len(results),
                    "invalid_triggered": len(injected),
                    "healed": len(healed),
                    "heal_rate": round(rate, 4) if rate is not None else None,
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"明细已写入 {REPORT_PATH.relative_to(ROOT)}")
    return 0 if rate is not None and rate >= 0.7 else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=0, help="只跑前 N 条")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
