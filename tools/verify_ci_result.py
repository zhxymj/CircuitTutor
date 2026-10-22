# -*- coding: utf-8 -*-
"""校验 CI 的 fast 报告：零失败，且跳过项必须命中白名单。

退出码：0=可接受；1=存在失败或非白名单跳过；2=报告缺失/不可读。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 run_tests.py 的 JSON 报告")
    parser.add_argument("report", help="run_tests.py --json-out 生成的报告路径")
    parser.add_argument(
        "--allow-skip",
        action="append",
        default=[],
        metavar="SUITE",
        help="允许跳过的套件文件（可重复）",
    )
    args = parser.parse_args(argv)

    path = Path(args.report)
    if not path.is_file():
        print(f"缺少测试报告：{path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"测试报告不可读：{error}", file=sys.stderr)
        return 2

    summary = report.get("summary") or {}
    suites = report.get("suites") or []
    failed = [suite for suite in suites if suite.get("status") == "fail"]
    skipped = [suite for suite in suites if suite.get("status") == "skip"]
    allowed = set(args.allow_skip)
    unexpected = [
        suite.get("file", "?") for suite in skipped if suite.get("file") not in allowed
    ]
    counted = sum(int(summary.get(key, 0)) for key in ("pass", "fail", "skip"))
    total = int(summary.get("total", 0))

    print(
        f"profile={report.get('profile')} total={total} "
        f"pass={summary.get('pass')} fail={summary.get('fail')} skip={summary.get('skip')}"
    )

    problems: list[str] = []
    if total <= 0:
        problems.append("报告没有选中任何套件")
    if failed:
        problems.append("失败套件：" + "、".join(suite.get("file", "?") for suite in failed))
    if unexpected:
        problems.append("非白名单跳过：" + "、".join(unexpected))
    if total != counted:
        problems.append(f"汇总计数不一致：total={total}，分类合计={counted}")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    if skipped:
        print("允许的跳过：" + "、".join(suite.get("file", "?") for suite in skipped))
    print("CI 结果可接受")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
