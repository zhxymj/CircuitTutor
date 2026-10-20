# -*- coding: utf-8 -*-
"""
test_benchmark_snapshot.py —— benchmark 离线快照回归
==============================================================
背景：`benchmark.py` 是答辩核心数据来源，但它的结论依赖外部 LLM（模型版本、
网络、日期都会让同一份代码跑出不同结果），回归时既不可复现、也没法进 CI。

本套件消费 benchmark 的 `--snapshot` 机制：用确定性假客户端把「流水线代码
本身」的行为固定成一份黄金快照（tests/golden/benchmark_snapshot.json）。
从此任何改动只要动了流水线的成功/失败分布、阶段序列、调用次数、ERC 计数，
都会被逐条比对出来——**测的是代码回归，不是模型能力**。

覆盖：
  1. 快照可复现：连跑两次（剔除耗时字段）结果逐字段一致；
  2. 黄金对比：本次运行与仓库内黄金快照一致（代码有没有漂）；
  3. 指纹自洽：suite_hash 与 script_hash 与当前代码一致；
  4. 剧本有效：脚本指定的失败分支确实产生了失败（快照不是纯 happy path）；
  5. 对比器本身可用：注入一处改动必须被 diff_snapshots 抓到（先红后绿）。

零依赖、零网络、零 Key，可直接进 run_tests / CI。
运行：python -B tests/test_benchmark_snapshot.py
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _PROJECT_ROOT)

import json
import tempfile
from pathlib import Path

import benchmark as B
from core import catalog

GOLDEN = Path(_PROJECT_ROOT) / "tests" / "golden" / "benchmark_snapshot.json"

PASS = 0


def check(name, cond):
    global PASS
    if not cond:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


def run_snapshot(out_path: Path) -> dict:
    """在当前进程内以确定性客户端跑一遍快照（不经过命令行）。"""
    client = B.DeterministicClient()
    kb = catalog.build_erc_kb()
    records = [B.run_case(i, p, client, kb)
               for i, p in enumerate(B.BENCHMARK_CASES, 1)]
    summary = B.summarize(records, len(B.BENCHMARK_CASES))
    report = {"meta": {"model": client.model,
                       "suite_hash": B.suite_hash(),
                       "script_hash": B.script_hash(),
                       "snapshot": True},
              "summary": summary,
              "cases": records}
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return report


def strip_for_compare(report: dict) -> dict:
    """比对前统一裁剪：剔耗时字段、剔逐条 circuit（体积大且非行为指标）。"""
    r = B.strip_timing(report)
    for c in r.get("cases", []):
        c.pop("circuit", None)
    r.get("meta", {}).pop("time", None)
    r.get("meta", {}).pop("snapshot", None)   # 黄金是精简版，键集差异不算漂移
    return r


def main() -> int:
    print("== 1. 黄金快照存在且结构完整 ==")
    check("黄金快照文件存在", GOLDEN.is_file())
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    check("黄金快照含 meta/summary/cases",
          all(k in golden for k in ("meta", "summary", "cases")))
    check("黄金快照覆盖全部用例", len(golden["cases"]) == len(B.BENCHMARK_CASES))
    check("黄金快照标注为快照模式（model=deterministic-fake）",
          golden["meta"].get("model") == B._SNAPSHOT_MODEL)

    print("== 2. 快照可复现（连跑两次结果一致） ==")
    with tempfile.TemporaryDirectory() as td:
        a = run_snapshot(Path(td) / "a.json")
        b = run_snapshot(Path(td) / "b.json")
    check("两次运行剔除耗时后无差异",
          B.diff_snapshots(strip_for_compare(a), strip_for_compare(b)) == [])

    print("== 3. 黄金对比：当前代码与仓库快照一致 ==")
    diffs = B.diff_snapshots(strip_for_compare(golden), strip_for_compare(a))
    check("与黄金快照逐条一致（流水线行为无漂移）", diffs == [])

    print("== 4. 指纹自洽 ==")
    check("suite_hash 与当前用例集一致",
          golden["meta"].get("suite_hash") == B.suite_hash())
    check("script_hash 与当前剧本一致",
          golden["meta"].get("script_hash") == B.script_hash())
    check("黄金快照的 suite_size 与当前一致", golden["meta"].get("suite_size") == len(B.BENCHMARK_CASES))

    print("== 5. 剧本确实覆盖失败分支（不是纯 happy path） ==")
    expect_fail = sorted(i for i in B._SNAPSHOT_SCRIPT
                         if B._SNAPSHOT_SCRIPT[i][0] != "ok")
    actual_fail = sorted(c["index"] for c in a["cases"] if c["verdict"] == "fail")
    check("失败用例集合与剧本指定的完全一致",
          actual_fail == expect_fail and actual_fail)
    check("失败原因覆盖结构校验类（ValueError: 结构校验）",
          any("结构校验" in (c["error"] or "") for c in a["cases"]
              if c["index"] in expect_fail))
    check("失败原因覆盖 ERC 类（major ≥ 1）",
          any(c["major"] >= 1 for c in a["cases"] if c["verdict"] == "fail"))
    check("成功与失败并存（快照既能钉住通过也能钉住不通过）",
          any(c["verdict"] == "success" for c in a["cases"]) and actual_fail)
    check("no_extract 剧本触发了「抽取未命中」失败",
          any(c["index"] in B._SNAPSHOT_SCRIPT
              and B._SNAPSHOT_SCRIPT[c["index"]][0] == "no_extract"
              and "抽取未命中" in (c["error"] or "")
              for c in a["cases"]))

    print("== 6. 对比器本身有效（先红后绿） ==")
    # 注入一处改动：把第 1 条的 verdict 翻转，必须被 diff_snapshots 抓到
    tampered = json.loads(json.dumps(strip_for_compare(a)))
    tampered["cases"][0]["verdict"] = (
        "fail" if tampered["cases"][0]["verdict"] == "success" else "success")
    d1 = B.diff_snapshots(strip_for_compare(golden), tampered)
    check("对比器能抓到 verdict 翻转", any("verdict" in x for x in d1))
    # 注入 summary 改动
    tampered2 = json.loads(json.dumps(strip_for_compare(a)))
    tampered2["summary"]["success"] = tampered2["summary"]["success"] + 1
    d2 = B.diff_snapshots(strip_for_compare(golden), tampered2)
    check("对比器能抓到 summary 计数变化", any("summary.success" in x for x in d2))
    # 注入用例缺失
    tampered3 = json.loads(json.dumps(strip_for_compare(a)))
    dropped = tampered3["cases"].pop()
    d3 = B.diff_snapshots(strip_for_compare(golden), tampered3)
    check("对比器能抓到用例缺失",
          any(f"case[{dropped['index']}]" in x and "缺失" in x for x in d3))
    check("未改动时对比器返回空（无假阳性）",
          B.diff_snapshots(strip_for_compare(a), strip_for_compare(a)) == [])

    print("== 7. strip_timing 只剔时间字段、不动行为字段 ==")
    sample = {"time": "x", "elapsed_ms": 1, "avg_elapsed_ms": 2,
              "total_elapsed_ms": 3, "verdict": "success",
              "cases": [{"index": 1, "elapsed_ms": 9, "verdict": "fail"}]}
    s = B.strip_timing(sample)
    check("顶层时间字段被剔除",
          not ({"time", "elapsed_ms", "avg_elapsed_ms", "total_elapsed_ms"} & set(s)))
    check("行为字段被保留", s["verdict"] == "success")
    check("嵌套结构中的时间字段也被剔除",
          "elapsed_ms" not in s["cases"][0] and s["cases"][0]["verdict"] == "fail")
    check("非 dict/list 值原样返回", B.strip_timing(5) == 5)

    print("== 8. README 配置表与 config.py 默认值一致 ==")
    # 配置表漂移是「文档说了但代码没做」的典型受害者：此前 README 写
    # LLM_TIMEOUT_S 默认 25，实际 config 是 90——照文档调参的人会踩空。
    # 这里把「README 里出现的环境变量默认值」与「config 实际默认值」对齐比对。
    import re as _re
    from core import config as cfg
    readme_path = Path(_PROJECT_ROOT) / "README.md"
    check("README 存在", readme_path.is_file())
    readme = readme_path.read_text(encoding="utf-8")
    # 解析形如 | `VAR` | `默认` | 说明 | 的表格行
    rows = _re.findall(r"^\|\s*`([A-Z0-9_]+)`\s*\|\s*`?([^|`]*)`?\s*\|", readme, _re.M)
    table = {k: v.strip() for k, v in rows}
    check("README 配置表解析到足够多的条目", len(table) >= 12)
    # 逐项核对：仅核对「表里写了数值型默认值」且「config 有同名常量」的项
    # （描述性默认值如「（自动探测）」「（空）」不参与机器比对）
    checked = 0
    for var, doc_default in table.items():
        if not hasattr(cfg, var) or not doc_default:
            continue
        actual = getattr(cfg, var)
        if isinstance(actual, bool):
            expected = "1" if actual else "0"
        elif isinstance(actual, (int, float)):
            expected = str(int(actual)) if float(actual) == int(actual) else str(actual)
        else:
            continue            # 字符串型配置（路径等）不参与数值比对
        if doc_default != expected:
            check(f"README 默认值 {var}={doc_default} 与 config 实际一致"
                  f"（实际 {expected}）", False)
        check(f"README 默认值 {var}={doc_default} 与 config 实际一致", True)
        checked += 1
    check("至少核对了 6 项带数值默认值的环境变量", checked >= 6)
    # 反向：关键的对外环境变量不应从 README 配置表里消失
    # （注意 config 常量名与对外环境变量名可能不同，如 RATE_LIMIT ↔ LSQC_RATE_LIMIT）
    for var in ("LLM_TIMEOUT_S", "LLM_BUDGET", "LSQC_RATE_LIMIT", "MAX_QUERY_LEN"):
        check(f"README 收录了 {var}", var in table)
    check("config 常量名与 README 环境变量名映射无遗漏（LSQC_RATE_LIMIT↔RATE_LIMIT）",
          "LSQC_RATE_LIMIT" in table and hasattr(cfg, "RATE_LIMIT"))
    check("README 收录了 KiCad 导出限流项（LSQC_KICAD_RATE_LIMIT）",
          "LSQC_KICAD_RATE_LIMIT" in table)
    # PORT 语义歧义防护：PORT 是「外部环境变量名」，而 config.PORT 是「解析后的
    # 实际端口常量」（默认 8765）——两者同名不同义。若把 PORT 写进主表，比对会
    # 拿环境变量的语义去撞常量的值（默认写「（空）」就与 8765 冲突）。正确做法是
    # 在表下用独立段落说明该回退机制，本断言锁定这一约定。
    check("PORT 不在 README 主配置表（避免与环境变量语义撞车）", "PORT" not in table)
    check("README 有 PORT 回退机制的独立说明",
          "PORT" in readme and "0.0.0.0:PORT" in readme)
    check("支持 PORT 回退的 config 常量确实存在（供上表比对）",
          hasattr(cfg, "PORT") and hasattr(cfg, "HOST"))

    print("== 9. DeepSeek 模型注册表一致性（2026-09-11 对齐官方 Pricing） ==")
    # 模型名是最容易「文档升了、代码没升」的地方，且下线模型调用直接 404、
    # 整条生成链路首轮即失败。这里把三处默认值 + 预设表锁成一个闭环：
    #   llm_providers.PROVIDERS（前端目录权威）
    #   llm.DeepSeekClient.DEFAULT_MODEL（服务端兜底）
    #   server.make_llm_client 的 label（进度条展示）
    #   web/app.js 的兜底字面量（目录拉取失败时的最后一手）
    from core import llm as _llm
    from core import llm_providers as _prov

    ds = _prov.get_provider("deepseek")
    check("DeepSeek 预设在注册表中存在", ds is not None)
    ds_models = [m["id"] for m in ds["models"]]
    check("DeepSeek 预设含 deepseek-flash（官方 V4.1 Flash 现行 ID）",
          "deepseek-flash" in ds_models)
    check("DeepSeek 预设含 deepseek-v4-pro（将退役但暂可受理）",
          "deepseek-v4-pro" in ds_models)
    # 已退役遗留别名不再列入推荐位（官方仍受理但按 Flash 计费）
    for legacy in ("deepseek-v4.1-flash", "deepseek-v4-flash",
                   "deepseek-v4-flash-vision-exp"):
        check(f"遗留别名 {legacy} 未列入推荐预设", legacy not in ds_models)
    # 下线模型绝不能回归（调用 404 → 生成链路首轮失败）
    for dead in ("deepseek-chat", "deepseek-reasoner"):
        check(f"已下线模型 {dead} 未出现在预设表", dead not in ds_models)
    check("全部预设模型名通过 validate_model_name（字符集合法）",
          all(_prov.validate_model_name(m) == m for m in ds_models))
    # 三处默认值闭环：预设表首项 == 服务端常量 == 前端兜底字面量
    check("预设表首项即推荐模型（前端默认选它）", ds_models[0] == "deepseek-flash")
    check("DeepSeekClient.DEFAULT_MODEL 与预设表首项一致",
          _llm.DeepSeekClient.DEFAULT_MODEL == ds_models[0])
    appjs = (Path(_PROJECT_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
    check("app.js 兜底字面量与预设表首项一致（目录拉取失败时的最后一手）",
          f"'{ds_models[0]}'" in appjs)
    # 所有预设都必须有 base_url（custom 除外），否则 /api/llm_config 无法解析
    for p in _prov.PROVIDERS:
        if p["id"] == "custom":
            check("custom 预设 base_url 为空（由用户自填）", p["base_url"] is None)
            continue
        check(f"预设 {p['id']} 提供 base_url", bool(p.get("base_url")))
        check(f"预设 {p['id']} 至少列出一个模型", len(p["models"]) >= 1)
    # 公开目录不得泄漏 base_url（少一个伪造面）
    pub = _prov.public_catalog(False)
    check("公开目录不含 base_url 字段",
          all("base_url" not in p for p in pub["providers"]))
    check("公开目录透出 deepseek_env_key 标志",
          pub["deepseek_env_key"] is False)

    print(f"\ntest_benchmark_snapshot.py 全部 {PASS} 项断言通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
