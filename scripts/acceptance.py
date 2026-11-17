"""D5 一键验收：把 Day4 的评测结果对照五项指标，输出 PASS / FAIL。

用法：
    .venv/bin/python -m scripts.acceptance

前置：先跑过
    .venv/bin/python -m scripts.run_eval --reset
    .venv/bin/python -m scripts.run_selfheal
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "data/eval/report.json"
SELFHEAL_PATH = ROOT / "data/eval/selfheal.json"
TRACE_DIR = ROOT / "data/traces"


def _load(path: Path, hint: str) -> Dict[str, Any]:
    if not path.exists():
        print(f"缺少 {path.relative_to(ROOT)}，请先执行：{hint}")
        raise SystemExit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def _trace_coverage(results: List[Dict[str, Any]]) -> Tuple[int, int]:
    """有 trace 文件的任务数 / 总任务数。退款类有两轮，存在任一轮即算覆盖。"""
    covered = sum(
        1 for item in results if any(TRACE_DIR.glob(f"eval-{item['id']}-t*.json"))
    )
    return covered, len(results)


def main() -> int:
    report = _load(REPORT_PATH, "python -m scripts.run_eval --reset")
    selfheal = _load(SELFHEAL_PATH, "python -m scripts.run_selfheal")

    summary = report["summary"]
    heal = selfheal["summary"]
    covered, total = _trace_coverage(report["results"])
    coverage = covered / total if total else 0.0

    checks: List[Tuple[str, bool, str, str]] = [
        (
            "端到端成功率",
            summary["pass_rate"] >= 0.95,
            f'{summary["pass_rate"] * 100:.1f}% ({summary["passed"]}/{summary["total"]})',
            "≥95%",
        ),
        (
            "平均工具调用轮次",
            summary["avg_rounds"] <= 3,
            f'{summary["avg_rounds"]:.2f}',
            "≤3",
        ),
        (
            "工具超时率",
            summary["timeout_rate"] <= 0.01,
            f'{summary["timeout_rate"] * 100:.2f}%',
            "≤1%",
        ),
        (
            "参数缺字段自愈率",
            (heal["heal_rate"] or 0) >= 0.7,
            f'{heal["heal_rate"] * 100:.1f}% ({heal["healed"]}/{heal["invalid_triggered"]})',
            "≥70%",
        ),
        (
            "调用链可追溯",
            coverage >= 1.0,
            f"{coverage * 100:.1f}% ({covered}/{total})",
            "100%",
        ),
    ]

    print("\n===== 五项验收指标 =====")
    for name, ok, actual, target in checks:
        print(f'[{"PASS" if ok else "FAIL"}] {name:<10} {actual:<22} 目标 {target}')

    print(f'\n评测集 {summary["total"]} 条 · 工具调用 {summary["tool_calls"]} 次')
    failed = [name for name, ok, _, _ in checks if not ok]
    if failed:
        print(f"未通过：{'、'.join(failed)}")
        return 1
    print("全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
