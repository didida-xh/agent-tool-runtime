"""D4 评测跑批：逐条跑 data/eval/tasks.jsonl，按判据算通过率。

用法：
    .venv/bin/python -m scripts.run_eval -n 10      # 先跑前 10 条
    .venv/bin/python -m scripts.run_eval            # 全量
    .venv/bin/python -m scripts.run_eval --reset    # 跑前把订单状态恢复成种子值

判据三类（任一不满足即判失败）：
    1. 工具：expect_tools 里的工具必须都被调用过，且不出现 forbid_tools；
    2. 参数：expect_args 里每个工具的指定键值，必须在某次调用参数中出现；
    3. 终答：expect_keywords 里任一关键词出现在回答中（大小写不敏感）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, observe  # noqa: E402
from app.agent_loop import run_agent  # noqa: E402

TASKS_PATH = ROOT / "data/eval/tasks.jsonl"
REPORT_PATH = ROOT / "data/eval/report.json"

def followup_text(task: Dict[str, Any]) -> str:
    """第二轮的用户确认语。

    必须连同退款原因一起复述——只给订单号和金额时，模型会停下来追问 reason，
    因为 refund 的 reason 是必填枚举。
    """
    return f'{task["question"]}。我回复：确认。请立即执行退款，不要再询问我。'

# 退款会改库，每轮跑批前必须能恢复到初始状态
SEED_STATUS = {
    "A1001": "paid",
    "A1002": "delivered",
    "A1003": "shipped",
    "A1004": "refunding",
}


class Recorder:
    """替代 db.record_tool_call：只收在内存里，不写库。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    async def __call__(self, **kw: Any) -> None:
        self.rows.append(kw)


async def reset_seed() -> None:
    pool = await db.get_pool()
    for order_id, status in SEED_STATUS.items():
        await pool.execute(
            "UPDATE orders SET status = $2 WHERE order_id = $1", order_id, status
        )


def load_tasks() -> List[Dict[str, Any]]:
    tasks = []
    with TASKS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def check(task: Dict[str, Any], answer: str, rounds: int, rows: List[Dict[str, Any]]):
    """返回 (是否通过, 失败原因列表)。"""
    actual = [r["tool_name"] for r in rows]
    problems: List[str] = []

    for name in task.get("expect_tools") or []:
        if name not in actual:
            problems.append(f"未调用 {name}（实际 {actual}）")

    forbidden = set(task.get("forbid_tools") or [])
    extra = sorted(set(actual) & forbidden)
    if extra:
        problems.append(f"误调 {extra}")

    for name, want in (task.get("expect_args") or {}).items():
        hit = any(
            r["tool_name"] == name
            and all((r["arguments"] or {}).get(k) == v for k, v in want.items())
            for r in rows
        )
        if not hit:
            problems.append(f"{name} 参数不符，期望 {want}")

    keywords = task.get("expect_keywords") or []
    if keywords and not any(k.lower() in (answer or "").lower() for k in keywords):
        problems.append(f"终答未含 {keywords}")

    # 负例的"查不到"说法太多（没有该订单 / 无此记录 / 没查到…），用正则更稳
    pattern = task.get("expect_regex")
    if pattern and not re.search(pattern, answer or ""):
        problems.append(f"终答未匹配 {pattern}")

    limit = task.get("max_rounds")
    if limit is not None and rounds > limit:
        problems.append(f"轮次 {rounds} 超过 {limit}")

    return (not problems), problems


async def run_one(task: Dict[str, Any]) -> Dict[str, Any]:
    if task.get("reset_before"):
        await reset_seed()

    recorder = Recorder()
    turns = int(task.get("confirm_turns", 1))
    expected = task.get("expect_tools") or []
    answer, rounds, status = "", 0, "success"

    try:
        # 写操作第一次只会请求确认，带 confirm_turns=2 的用例再补一轮"已确认"
        for turn in range(turns):
            question = task["question"] if turn == 0 else followup_text(task)
            task_key = f"eval-{task['id']}-t{turn + 1}"
            # 每条评测任务也落一份 trace，"链路可追溯 100%"才有物证
            async with observe.trace(task_key, question) as tr:
                out = await run_agent(
                    question,
                    task_id=task_key,
                    confirm_token="eval-token" if task.get("confirm") else None,
                    record_tool_call=recorder,
                    tracer=tr,
                )
                tr.finish(out.status, out.answer)
            answer, rounds, status = out.answer, rounds + out.rounds, out.status
            called = [r["tool_name"] for r in recorder.rows]
            if all(name in called for name in expected):
                break
    except Exception as exc:
        # 单条炸掉不中断整批，但如实记为失败
        return {
            "id": task["id"],
            "category": task.get("category", ""),
            "question": task["question"],
            "passed": False,
            "rounds": rounds,
            "status": "exception",
            "answer": answer,
            "tools": [r["tool_name"] for r in recorder.rows],
            "tool_status": [r["status"] for r in recorder.rows],
            "problems": [f"异常 {type(exc).__name__}: {exc}"],
        }

    passed, problems = check(task, answer, rounds, recorder.rows)
    return {
        "id": task["id"],
        "category": task.get("category", ""),
        "question": task["question"],
        "passed": passed,
        "rounds": out.rounds,
        "status": out.status,
        "answer": out.answer,
        "tools": [r["tool_name"] for r in recorder.rows],
        "tool_status": [r["status"] for r in recorder.rows],
        "problems": problems,
    }


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    all_calls = [s for r in results for s in r["tool_status"]]
    timeouts = sum(1 for s in all_calls if s == "timeout")

    # 自愈：本轮出现过 invalid，但任务最终通过
    healed = [r for r in results if "invalid" in r["tool_status"] and r["passed"]]
    invalid_tasks = [r for r in results if "invalid" in r["tool_status"]]

    by_cat: Dict[str, Dict[str, int]] = {}
    for r in results:
        bucket = by_cat.setdefault(r["category"] or "-", {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r["passed"] else 0

    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "avg_rounds": round(sum(r["rounds"] for r in results) / total, 2) if total else 0.0,
        "tool_calls": len(all_calls),
        "timeout_rate": round(timeouts / len(all_calls), 4) if all_calls else 0.0,
        "self_heal": {
            "tasks": len(invalid_tasks),
            "healed": len(healed),
            "rate": round(len(healed) / len(invalid_tasks), 4) if invalid_tasks else None,
        },
        "by_category": by_cat,
    }


async def amain(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        tasks = [t for t in tasks if t["id"] in wanted]
    if args.n:
        tasks = tasks[: args.n]
    if not tasks:
        print("没有匹配的用例")
        return 1

    if args.reset:
        await reset_seed()
        print("种子订单状态已重置")

    results = []
    for index, task in enumerate(tasks, 1):
        result = await run_one(task)
        results.append(result)
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"[{index:>3}/{len(tasks)}] {result['id']} {mark} rounds={result['rounds']}")
        if not result["passed"]:
            print(f"        {result['question']}")
            print(f"        {'; '.join(result['problems'])}")

    summary = summarize(results)
    print("\n===== 汇总 =====")
    print(f"总数 {summary['total']} | 通过 {summary['passed']} "
          f"({summary['pass_rate'] * 100:.1f}%)")
    print(f"平均轮次 {summary['avg_rounds']} | 工具调用 {summary['tool_calls']} 次 "
          f"| 超时率 {summary['timeout_rate'] * 100:.2f}%")
    heal = summary["self_heal"]
    heal_rate = "-" if heal["rate"] is None else f"{heal['rate'] * 100:.1f}%"
    print(f"参数自愈 {heal['healed']}/{heal['tasks']} ({heal_rate})")
    for cat, stat in sorted(summary["by_category"].items()):
        print(f"  {cat:<10} {stat['passed']}/{stat['total']}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n明细已写入 {REPORT_PATH.relative_to(ROOT)}")

    return 0 if summary["pass_rate"] >= 0.95 else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--ids", default="", help="逗号分隔的用例 id，如 e001,e007")
    parser.add_argument("--reset", action="store_true", help="跑前重置种子订单状态")
    args = parser.parse_args()
    exit_code = asyncio.run(amain(args))
    if exit_code == 2:
        print("通过率低于 95% 验收线")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
