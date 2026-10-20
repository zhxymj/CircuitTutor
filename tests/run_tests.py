# -*- coding: utf-8 -*-
"""智绘电路分层回归编排器。

用法::

    python -B tests/run_tests.py [FILTER ...]
        [--profile fast|integration|eda|quality|all]
        [--require-kicad] [--json-out PATH] [--audit-json-out PATH]

退出码：0=全部通过；1=存在失败；2=能力缺失导致跳过或命令行用法错误。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_PATH = Path(ROOT)
TIMEOUT_S = 600
BASE_PROFILES = frozenset({"fast", "integration", "eda", "quality"})
VALID_PROFILES = BASE_PROFILES | {"all"}
_AUTO = object()


@dataclass(frozen=True)
class Suite:
    """A test program plus its stable profile/capability metadata."""

    file: str
    description: str
    profiles: frozenset[str]
    requires: frozenset[str] = frozenset()


def _suite(
    file: str,
    description: str,
    profiles: Iterable[str],
    requires: Iterable[str] = (),
) -> Suite:
    return Suite(file, description, frozenset(profiles), frozenset(requires))


# Registry order is the execution order. Existing suites keep their historical
# order; B0 contracts are appended after the existing browser suites.
SUITES = (
    _suite("core/erc.py", "ERC 引擎 33 规则", {"fast"}),
    _suite("core/catalog.py", "元件目录 / 中文匹配器 / ERC 知识库", {"fast"}),
    _suite("core/llm.py", "LLM 生成层 / 修复闭环", {"fast"}),
    _suite(
        "tests/test_llm_pipeline.py",
        "LLM 生成链路离线回归（mock 注入）",
        {"fast", "integration"},
    ),
    _suite("tests/test_circuit_schema_v2.py", "CircuitJSON v2 合同与 v1 无损迁移", {"fast", "quality"}),
    _suite("tests/test_intent_planning.py", "需求规划、器件证据检索与目录外型号拒绝", {"fast", "quality"}),
    _suite("tests/test_requirements_v2.py", "80 条内部需求与样本划分合同", {"fast", "quality"}),
    _suite("tests/test_intent_constraints.py", "明确电气矛盾预检与模型零调用拒绝", {"fast", "quality"}),
    _suite("tests/test_generation_preflight_http.py", "真实 HTTP 缺项澄清、无覆盖模板拒绝与显式参数保留", {"fast", "integration"}),
    _suite("tests/test_validation_gate.py", "统一结构、电气、SPICE 与 KiCad 验收门禁", {"fast", "eda", "quality"}),
    _suite("tests/test_external_import.py", "三种上游结果的无猜测导入合同", {"fast", "integration"}),
    _suite("tests/test_external_import_http.py", "外部导入随机端口 HTTP 合同", {"integration"}),
    _suite("tests/test_upstream_vision.py", "本地 CircuitVision 隔离桥接合同", {"fast", "integration"}),
    _suite("tests/test_upstream_vision_http.py", "本地 CircuitVision 随机端口 HTTP 合同", {"integration"}),
    _suite("tests/test_runnable_package.py", "可运行包文件选择、迁移启动与完整性合同", {"fast", "quality"}),
    _suite("tests/test_runnable_package_refresh.py", "交付包增量更新、压缩流保留与清单合同", {"fast", "quality"}),
    _suite("tests/test_native_model_suites.py", "内置 CircuitVision 与 HCD 数值算法独立环境回归", {"integration", "quality"}),
    _suite("tests/test_tscircuit_engine.py", "本机 tscircuit 代码生成、拒绝与进程回收", {"integration"}, {"node"}),
    _suite("core/autofix.py", "ERC 警告自动修复", {"fast"}),
    _suite("tests/test_pipeline.py", "端到端流水线", {"fast", "integration", "quality"}),
    _suite("tests/test_autofix.py", "自动修复回归", {"fast"}),
    _suite("tests/test_design_rules.py", "参数计算 E12 / 拓扑黄金模式", {"fast"}),
    _suite("tests/test_engineering.py", "工程估算（分压/上拉/LED 电流）", {"fast"}),
    _suite(
        "tests/test_kicad_lib.py",
        "KiCad 官方库检索",
        {"eda"},
        {"kicad_symbols"},
    ),
    _suite(
        "tests/test_kicad_batch.py",
        "KiCad 官方库批量导入草稿",
        {"eda"},
        {"kicad_symbols"},
    ),
    _suite("tests/test_bom_specs.py", "BOM 导出与器件规格库", {"fast"}),
    _suite("tests/test_erc_ratings.py", "ERC 额定值告警（LED/电阻/稳压）", {"fast"}),
    _suite("tests/test_spice.py", "SPICE 网表导出", {"fast", "eda"}),
    _suite("tests/test_spice_enhanced.py", "SPICE 仿真骨架（op/ac/tran + 解析解）", {"fast", "eda"}),
    _suite("tests/test_trace.py", "LLM 链路日志（request_id 贯穿）", {"fast"}),
    _suite("tests/test_footprints_pcb.py", "KiCad 封装映射 + PCB 预布局", {"eda"}),
    _suite("tests/test_eagle_footprint_repro.py", "源封装复现与焊盘几何等价性核对", {"fast", "quality"}),
    _suite("tests/test_external_reference_cases.py", "外部源完整性、划分隔离与源电气接受合同", {"fast", "quality"}),
    _suite("tests/test_pcb_real.py", "真实 KiCad 封装、焊盘映射与未布线 PCB 候选", {"eda", "quality"}),
    _suite("tests/test_pcb_manufacturing.py", "真实焊盘审计、严格 DRC 与制造候选失败回退", {"eda", "quality"}),
    _suite("tests/test_pcb_source_schematic.py", "真实引脚源原理图、官方网表与 PCB 实例一致性", {"eda", "quality"}),
    _suite("tests/test_schemes_history.py", "多方案草案与工程历史", {"integration"}),
    _suite("tests/test_http_integration.py", "HTTP 集成（零 Key 全链路）", {"integration"}),
    _suite("tests/test_llm_runtime.py", "LLM 运行时（预算/重试/取消/短路）", {"fast"}),
    _suite("tests/test_dxf.py", "DXF 兼容性优化器（CAD 互通）", {"eda"}),
    _suite("tests/test_server.py", "HTTP 服务（随机端口，零 LLM）", {"integration"}),
    _suite(
        "tests/test_kicad_export.py",
        "KiCad 导出（真机官方校验）",
        {"eda"},
        {"kicad_cli"},
    ),
    _suite(
        "tests/test_ai2kicad.py",
        "自然语言→KiCad 端到端",
        {"eda"},
        {"kicad_cli", "ngspice"},
    ),
    _suite("tests/test_symbol_import.py", "元件库扩展协议（.kicad_sym 解析器）", {"eda"}),
    _suite("tests/test_clarify.py", "需求澄清（判歧/合并/接口/CLI 追问）", {"fast"}),
    _suite("tests/test_benchmark_snapshot.py", "benchmark 离线快照（代码回归护栏）", {"quality"}),
    _suite("tests/test_compliance_benchmark.py", "A 主线固定缺陷集与指标证据", {"quality"}),
    _suite("tests/test_bus_voltage_rules.py", "I2C 上拉 / 地址冲突 / 电源轨兼容", {"fast"}),
    _suite(
        "tests/test_generation_benchmark.py",
        "模板结构 / KiCad 生成 / 官方复验证据",
        {"integration", "eda"},
    ),
    _suite("tests/test_ai_requirements_benchmark.py", "在线 AI 需求评测合同（测试不联网）", {"quality"}),
    _suite("tests/test_routing_job.py", "B 方向隔离任务合同 / 奖励 / 回退", {"fast"}),
    _suite("tests/test_render.js", "整图渲染 / 自动布局 / 主题", {"fast", "quality"}, {"node"}),
    _suite("tests/test_frontend.js", "符号库 / 交互层一致性", {"fast", "quality"}, {"node"}),
    _suite("tests/test_image_recognition.js", "图片几何识别候选与拓扑基础合同", {"fast", "quality"}, {"node"}),
    _suite("tests/test_image_import_ui.js", "图片预览、草稿应用与撤回浏览器集成", {"integration"}, {"node", "edge"}),
    _suite("tests/test_external_import_e2e.js", "真实 Edge 外部结果导入、门禁与撤回", {"integration"}, {"node", "edge"}),
    _suite("tests/test_upstream_vision_ui.js", "本地 CircuitVision 图片候选人工校正浏览器合同", {"integration"}, {"node", "edge"}),
    _suite("tests/test_native_vision_ui.js", "内置 HCD 导线几何叠加与非电气候选合同", {"fast", "integration"}, {"node"}),
    _suite("tests/test_tscircuit_ui.js", "真实 Edge 代码建电路、校验、编辑与撤销", {"integration"}, {"node", "edge"}),
    _suite("tests/test_workbench_ui.js", "专业工程台真实导航、编辑、导出与窄屏布局", {"integration"}, {"node", "edge"}),
    _suite("tests/test_quality_contract.js", "审计 JSON / 阻断合同", {"fast", "quality"}, {"node"}),
    _suite("tests/test_pin_wire_contract.js", "连接引脚 own-net 几何合同", {"fast", "quality"}, {"node"}),
    _suite("tests/test_audit.js", "三专家画图准确性审计", {"quality"}, {"node"}),
    _suite("tests/test_quality_foundation.py", "B0 分层回归与能力合同", {"quality"}),
    _suite("tests/test_version_consistency.py", "单一版本源一致性", {"quality"}),
    _suite("tests/test_release_quality.py", "原子发布与清单质量门", {"quality"}),
    _suite("tests/test_deploy_contract.py", "部署预检与回滚合同", {"quality"}),
)

# Compatibility for callers that only display the old language-specific lists.
PY_SUITES = [(s.file, s.description) for s in SUITES if s.file.endswith(".py")]
JS_SUITES = [(s.file, s.description) for s in SUITES if s.file.endswith(".js")]


def missing_suite_files() -> list[str]:
    """Registry entries whose program file is absent; a dangling entry must
    fail at registration time, not silently rot inside a profile."""

    return [suite.file for suite in SUITES if not (ROOT_PATH / suite.file).is_file()]


def select_suites(profile: str, filters: Sequence[str]) -> list[Suite]:
    """Select suites by base profile, then apply legacy substring filters."""

    normalized = str(profile).lower()
    if normalized not in VALID_PROFILES:
        raise ValueError("未知 profile：%s" % profile)
    lowered_filters = [str(item).lower() for item in filters]
    selected: list[Suite] = []
    seen: set[str] = set()
    for suite in SUITES:
        in_profile = bool(suite.profiles & BASE_PROFILES) if normalized == "all" else normalized in suite.profiles
        if not in_profile:
            continue
        if lowered_filters and not any(item in suite.file.lower() for item in lowered_filters):
            continue
        if suite.file not in seen:
            selected.append(suite)
            seen.add(suite.file)
    return selected


def detect_capabilities(
    node=_AUTO,
    kicad_cli=_AUTO,
    kicad_symbols=_AUTO,
    ngspice=_AUTO,
    edge=_AUTO,
) -> dict[str, bool]:
    """Probe optional executables/libraries, with injectable results for tests."""

    if node is _AUTO:
        node = shutil.which("node")
    if kicad_cli is _AUTO:
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from kicad.export import find_kicad_cli

            kicad_cli = find_kicad_cli()
        except Exception:
            kicad_cli = None
    if kicad_symbols is _AUTO:
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from core.kicad_lib import default_symbol_dir

            kicad_symbols = default_symbol_dir()
        except Exception:
            kicad_symbols = None
    if ngspice is _AUTO:
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from core.spice_enhanced import find_ngspice

            ngspice = find_ngspice()
        except Exception:
            ngspice = None
    if edge is _AUTO:
        edge = any(Path(p).is_file() for p in (
            os.environ.get("EDGE_TEST_BINARY", ""),
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        ) if p)
    return {
        "node": bool(node),
        "kicad_cli": bool(kicad_cli),
        "kicad_symbols": bool(kicad_symbols),
        "ngspice": bool(ngspice),
        "edge": bool(edge),
    }


def unmet_requirements(suite: Suite, capabilities: dict[str, bool]) -> list[str]:
    return sorted(name for name in suite.requires if not capabilities.get(name, False))


def build_cmd(interpreter: str, target: str) -> list[str]:
    """Build a Python command; package self-checks execute via ``-m``."""

    if target.startswith(("core/", "kicad/")) and target.endswith(".py"):
        return [interpreter, "-B", "-m", target[:-3].replace("/", ".")]
    return [interpreter, "-B", os.path.join(ROOT, target)]


def _env_utf8() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_one(cmd: list[str], label: str) -> dict:
    t0 = time.time()
    try:
        process = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env_utf8(),
            timeout=TIMEOUT_S,
        )
        ok = process.returncode == 0
        output = (process.stdout or "") + (process.stderr or "")
    except subprocess.TimeoutExpired:
        ok, output = False, "(timeout after %ss)" % TIMEOUT_S
    except OSError as error:
        ok, output = False, str(error)
    elapsed = time.time() - t0
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    tail = ""
    for line in reversed(lines):
        if any(word in line for word in ("通过", "断言", "OK", "PASS", "pass", "成功", "全部", "written", "✔")):
            tail = line[:120]
            break
    if not tail and lines:
        tail = lines[-1][:120]
    return {
        "name": label,
        "file": label,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "skipped": False,
        "missing_capabilities": [],
        "dt": elapsed,
        "tail": tail,
        "out": output,
    }


AUDIT_REQUIRED_FIELDS = frozenset(
    {"schema_version", "status", "severity_counts", "assertions", "metrics", "findings"}
)


def validate_audit_report(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """Validate audit evidence independently from the Node process exit code."""

    report_path = Path(path)
    if not report_path.is_file():
        return False, "审计报告不存在"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "审计报告不是有效 JSON"
    if not isinstance(report, dict) or not AUDIT_REQUIRED_FIELDS.issubset(report):
        return False, "审计报告缺少必需字段"
    if not isinstance(report.get("schema_version"), str) or not report["schema_version"]:
        return False, "审计报告 schema_version 无效"
    counts = report.get("severity_counts")
    if not isinstance(counts, dict) or any(
        type(counts.get(level)) is not int or counts[level] < 0 for level in ("P0", "P1")
    ):
        return False, "审计报告严重度计数无效"
    if counts["P0"] or counts["P1"]:
        return False, "审计报告包含 P0/P1"
    if report.get("status") != "pass":
        return False, "审计报告状态不是 pass"
    return True, "pass"


def execute_suite(
    suite: Suite,
    capabilities: dict[str, bool],
    audit_json_out: str | None = None,
    executor: Callable[[list[str], str], dict] = run_one,
) -> dict:
    """Run one suite, or return a structured skip without spawning a process."""

    missing = unmet_requirements(suite, capabilities)
    if missing:
        reason = "缺少运行能力：" + ", ".join(missing)
        return {
            "name": suite.file,
            "file": suite.file,
            "desc": suite.description,
            "ok": False,
            "status": "skip",
            "skipped": True,
            "missing_capabilities": missing,
            "dt": 0.0,
            "tail": reason,
            "out": "",
        }

    if suite.file.endswith(".js"):
        node = shutil.which("node") or "node"
        cmd = [node, os.path.join(ROOT, suite.file)]
    else:
        cmd = build_cmd(sys.executable, suite.file)
    if suite.file == "tests/test_audit.js" and audit_json_out:
        cmd.extend(["--json-out", audit_json_out])

    result = executor(cmd, suite.file)
    result["desc"] = suite.description
    result.setdefault("file", suite.file)
    result.setdefault("skipped", False)
    result.setdefault("missing_capabilities", [])
    result["status"] = "pass" if result.get("ok") else "fail"

    if suite.file == "tests/test_audit.js" and audit_json_out:
        audit_ok, audit_reason = validate_audit_report(audit_json_out)
        result["audit_validation"] = audit_reason
        if not audit_ok:
            result["ok"] = False
            result["status"] = "fail"
            result["tail"] = audit_reason
    return result


def _relative_evidence_path(path: str | None) -> str | None:
    if not path:
        return None
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT_PATH.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _audit_summary(selected: bool, path: str | None) -> dict:
    if not selected:
        return {"selected": False, "path": None, "status": "not_selected"}
    ok, reason = validate_audit_report(path or "")
    summary = {
        "selected": True,
        "path": _relative_evidence_path(path),
        "status": "pass" if ok else "fail",
        "validation": reason,
    }
    if path and Path(path).is_file():
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            summary["schema_version"] = payload.get("schema_version")
            summary["severity_counts"] = payload.get("severity_counts")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    return summary


def build_validation_report(
    *,
    profile: str,
    filters: Sequence[str],
    capabilities: dict[str, bool],
    results: Sequence[dict],
    elapsed_s: float,
    quality_audit: dict,
) -> dict:
    """Build the stable machine-readable aggregate report (schema 1.1)."""

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from core.version import read_version

    passed = sum(1 for result in results if result.get("status") == "pass")
    failed = sum(1 for result in results if result.get("status") == "fail")
    skipped = sum(1 for result in results if result.get("status") == "skip")
    status = "fail" if failed else "skip" if skipped else "pass"
    return {
        "schema_version": "1.1",
        "product_version": read_version(ROOT_PATH),
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "command": "python -B tests/run_tests.py",
        "profile": profile,
        "filters": list(filters),
        "capabilities": dict(capabilities),
        "summary": {
            "total": len(results),
            "pass": passed,
            "fail": failed,
            "skip": skipped,
            "elapsed_s": round(float(elapsed_s), 3),
            "status": status,
        },
        "quality_audit": quality_audit,
        "kicad_official_validation": {"enabled": bool(capabilities.get("kicad_cli"))},
        "suites": [
            {
                "file": result.get("file", result.get("name")),
                "description": result.get("desc", ""),
                "status": result.get("status"),
                "skipped": bool(result.get("skipped")),
                "missing_capabilities": list(result.get("missing_capabilities", [])),
                "elapsed_s": round(float(result.get("dt", 0.0)), 3),
                "summary_line": result.get("tail", ""),
            }
            for result in results
        ],
        "semantics": "skip 不计入 pass；status=pass 仅表示本次所选套件全部执行通过。",
    }


def _take_value(args: list[str], index: int, option: str) -> tuple[str, int]:
    if index + 1 >= len(args) or args[index + 1].startswith("--"):
        raise ValueError(f"{option} 需要跟一个值")
    return args[index + 1], index + 2


def _parse_cli(argv: Sequence[str]) -> dict:
    args = list(argv[1:])
    parsed = {
        "profile": "all",
        "filters": [],
        "require_kicad": False,
        "json_out": None,
        "audit_json_out": None,
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--profile":
            parsed["profile"], index = _take_value(args, index, arg)
        elif arg == "--json-out":
            value, index = _take_value(args, index, arg)
            parsed["json_out"] = os.path.abspath(value)
        elif arg == "--audit-json-out":
            value, index = _take_value(args, index, arg)
            parsed["audit_json_out"] = os.path.abspath(value)
        elif arg == "--require-kicad":
            parsed["require_kicad"] = True
            index += 1
        elif arg.startswith("--"):
            raise ValueError("未知选项：%s" % arg)
        else:
            parsed["filters"].append(arg.lower())
            index += 1
    parsed["profile"] = str(parsed["profile"]).lower()
    if parsed["profile"] not in VALID_PROFILES:
        raise ValueError("未知 profile：%s" % parsed["profile"])
    return parsed


def main(argv: Sequence[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        options = _parse_cli(argv)
        selected = select_suites(options["profile"], options["filters"])
    except ValueError as error:
        print("用法错误：%s" % error)
        return 2

    missing = missing_suite_files()
    if missing:
        print("套件登记指向不存在的文件：%s" % ", ".join(missing))
        return 2

    if not selected:
        print("没有匹配的套件。可用套件：")
        for suite in SUITES:
            print("  %-38s %s" % (suite.file, suite.description))
        return 1

    capabilities = detect_capabilities()
    if options["require_kicad"]:
        required = {
            requirement
            for suite in selected
            for requirement in suite.requires
            if requirement in {"kicad_cli", "kicad_symbols"}
        }
        missing = sorted(name for name in required if not capabilities.get(name, False))
        if missing:
            print("KiCad 前置能力缺失：%s" % ", ".join(missing))
            return 2

    audit_selected = any(suite.file == "tests/test_audit.js" for suite in selected)
    audit_json_out = options["audit_json_out"]
    if audit_selected and not audit_json_out:
        if options["json_out"]:
            audit_json_out = os.path.join(os.path.dirname(options["json_out"]), "audit_report.json")
        else:
            audit_json_out = os.path.join(tempfile.gettempdir(), "zhihui-audit-report-%d.json" % os.getpid())
    if audit_selected and audit_json_out:
        try:
            Path(audit_json_out).unlink(missing_ok=True)
        except OSError:
            pass

    print("=" * 86)
    print(
        " 智绘电路 CircuitTutor · %s 回归汇总（%d 个套件）"
        % (options["profile"], len(selected))
    )
    print("=" * 86)

    results: list[dict] = []
    for suite in selected:
        result = execute_suite(suite, capabilities, audit_json_out=audit_json_out)
        results.append(result)
        label = "SKIP" if result["status"] == "skip" else "PASS" if result["ok"] else "FAIL"
        print("[%s] %-38s %6.1fs  %s" % (label, suite.file, result["dt"], result["tail"]))

    n_pass = sum(1 for result in results if result["status"] == "pass")
    n_fail = sum(1 for result in results if result["status"] == "fail")
    n_skip = sum(1 for result in results if result["status"] == "skip")
    total_dt = sum(float(result["dt"]) for result in results)
    print("-" * 86)
    print(" 结果：%d 通过 / %d 失败 / %d 跳过 · 总耗时 %.1fs" % (n_pass, n_fail, n_skip, total_dt))
    print(
        " 能力：Node=%s KiCad-CLI=%s KiCad-Symbols=%s ngspice=%s"
        % tuple("可用" if capabilities[name] else "缺失" for name in ("node", "kicad_cli", "kicad_symbols", "ngspice"))
    )
    print(" 说明：退出码 0=全绿 1=有失败 2=有跳过/前置能力不足")

    quality_audit = _audit_summary(audit_selected, audit_json_out)
    if options["json_out"]:
        report = build_validation_report(
            profile=options["profile"],
            filters=options["filters"],
            capabilities=capabilities,
            results=results,
            elapsed_s=total_dt,
            quality_audit=quality_audit,
        )
        json_out = Path(options["json_out"])
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(" 测试证据：%s" % json_out)

    if n_fail:
        print("\n失败套件输出（最后 15 行）：")
        for result in results:
            if result["status"] == "fail":
                print("\n----- %s -----" % result["name"])
                for line in result["out"].splitlines()[-15:]:
                    print("  " + line)
        return 1
    return 2 if n_skip else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
