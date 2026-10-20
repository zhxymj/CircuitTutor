# -*- coding: utf-8 -*-
"""统一原理图验收门禁。

模板、AI、图片识别和外部导入均调用同一入口。默认要求 KiCad 官方 ERC
及官方网表复验；工具缺席返回 incomplete，绝不把未运行项算作通过。
SPICE 仅对本项目确实有元件模型的电路适用，不能把黑盒 IC 注释当作仿真。
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core.catalog import CATALOG, build_erc_kb
from core.circuit_schema import validate_v2_metadata
from core.erc import CircuitGraph, run_erc
from core.erc_ratings import run_rating_checks
from core.llm import validate_circuit_json
from core.spice import to_cir
from core.spice_enhanced import find_ngspice
from kicad import export as kicad_export


SOURCES = frozenset({"template", "ai", "import", "image", "manual"})
_SPICE_MODELED = frozenset({
    "battery", "vcc", "gnd", "resistor", "capacitor", "electrolytic",
    "capacitor_ceramic", "capacitor_film", "capacitor_tantalum",
    "inductor", "ferrite_bead", "diode", "zener", "schottky",
    "diode_rectifier", "tvs", "led", "led_rgb", "npn", "pnp",
})
_ORDER = (
    "structure", "erc", "ratings", "spice_deck", "spice_runtime",
    "kicad_export", "official_erc", "official_netlist",
)


def _gate(status: str, *, required: bool, issues: list[dict] | None = None,
          evidence: dict | None = None, reason: str = "") -> dict:
    return {"status": status, "required": required, "issues": issues or [],
            "evidence": evidence or {}, "reason": reason}


def _issue(rule: str, message: str, *, severity: str = "error",
           pins: list[str] | None = None) -> dict:
    return {"rule": rule, "message": message, "severity": severity,
            "pins": pins or []}


def compare_official_netlist(circuit: dict, observed: dict,
                             kb: dict | None = None) -> list[dict]:
    """按导出器的引脚号/网络名合同返回逐网络差异。

    排除 KiCad 自动生成的 PWR_FLAG 虚拟节点和未连接网络，其他物理节点
    必须逐一相等。返回空列表才算一致。
    """
    kb = kb if kb is not None else build_erc_kb()
    graph = CircuitGraph(circuit, kb)
    from kicad.sch_writer import _i2c_header_layout
    part_types = {p["id"]: p["type"] for p in circuit["parts"]}
    part_pin_maps = {
        p["id"]: (_i2c_header_layout(p)[1] if p["type"] == "i2c_header"
                  else kicad_export.TYPE_MAP.get(p["type"], (None, {}, None))[1])
        for p in circuit["parts"]
    }
    expected: dict[str, set[tuple[str, str]]] = {}
    for root, members in graph.net_members.items():
        name = graph.net_names[root]
        for marker, anchor_name in kicad_export.ANCHOR_NET_NAMES.items():
            if any(part_types.get(ref.split(":", 1)[0]) == marker
                   for ref in members):
                name = anchor_name
                break
        for ref in members:
            part_id, pin = ref.split(":", 1)
            pin_map = part_pin_maps.get(part_id, {})
            if pin in pin_map:
                expected.setdefault(name, set()).add((part_id, pin_map[pin]))

    differences = []
    for name in sorted(set(expected) | set(observed)):
        if name.startswith("unconnected-"):
            continue
        actual = {(str(ref), str(pin)) for ref, pin in observed.get(name, set())
                  if not str(ref).startswith("#")}
        wanted = expected.get(name, set())
        if wanted != actual:
            differences.append({"net": name,
                                "expected": [list(n) for n in sorted(wanted)],
                                "observed": [list(n) for n in sorted(actual)]})
    return differences


def _validate_spice(circuit: dict, name: str,
                    require_runtime: bool) -> tuple[dict, dict]:
    unmodeled = sorted({str(p.get("type")) for p in circuit["parts"]
                        if p.get("type") not in _SPICE_MODELED})
    if unmodeled:
        reason = "元件缺少可执行 SPICE 模型：" + ", ".join(unmodeled)
        return (_gate("not_applicable", required=False, reason=reason,
                      evidence={"unmodeled_types": unmodeled}),
                _gate("not_applicable", required=False, reason=reason))
    try:
        deck = to_cir(circuit, title=name)
        if not deck.strip().endswith(".end"):
            raise ValueError("网表缺少 .end")
        if not any(line and line[0] in "RCLDVQ" for line in deck.splitlines()
                   if not line.startswith("*")):
            raise ValueError("网表没有可执行元件")
    except Exception as exc:
        return (_gate("fail", required=True,
                      issues=[_issue("SPICE导出", f"{type(exc).__name__}: {exc}")]),
                _gate("not_run", required=require_runtime,
                      reason="SPICE 网表导出失败"))

    deck_gate = _gate("pass", required=True,
                      evidence={"sha256": hashlib.sha256(deck.encode()).hexdigest(),
                                "line_count": len(deck.splitlines())})
    if not require_runtime:
        return deck_gate, _gate("not_run", required=False,
                                reason="未要求运行 ngspice；只验证了网表导出")
    exe = find_ngspice()
    if not exe:
        return deck_gate, _gate("unavailable", required=True,
                                reason="未找到 ngspice CLI")
    try:
        with tempfile.TemporaryDirectory(prefix="zhihui-spice-") as tmp:
            fp = Path(tmp) / "candidate.cir"
            fp.write_text(deck, encoding="utf-8")
            proc = subprocess.run([exe, "-b", str(fp)], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=30)
        output = (proc.stdout or "") + (proc.stderr or "")
        evidence = {"returncode": proc.returncode,
                    "output_excerpt": output[-1200:]}
        if proc.returncode:
            return deck_gate, _gate("fail", required=True,
                                    issues=[_issue("ngspice", "仿真进程非零退出")],
                                    evidence=evidence)
        return deck_gate, _gate("pass", required=True, evidence=evidence)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return deck_gate, _gate("unavailable", required=True,
                                reason=f"ngspice 未完成：{type(exc).__name__}")


def validate_candidate(circuit: Any, *, source: str, name: str = "candidate",
                       require_official: bool = True,
                       require_spice_runtime: bool | None = None,
                       work_dir: str | Path | None = None,
                       kb: dict | None = None) -> dict:
    """验证候选并返回可 JSON 序列化的完整门禁报告。

    `accepted` 只表示本次要求的全部适用门禁通过。默认要求 KiCad 官方
    两项复验；本机缺工具时为 `verdict=incomplete, accepted=False`。
    `require_official=False` 是明确的离线内部模式，结果带 assurance=internal。
    传入 work_dir 时保留 KiCad 工程并在 artifacts 中返回路径；否则临时
    工程在完成后清理，报告仍保留摘要与错误证据。不会修改 circuit。
    """
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError(f"source 必须为 {', '.join(sorted(SOURCES))} 之一")
    if not isinstance(require_official, bool) or (require_spice_runtime is not None
                                                and not isinstance(require_spice_runtime, bool)):
        raise ValueError("门禁开关必须是布尔值")
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("候选名称必须是安全的文件名")
    if require_spice_runtime is None:
        require_spice_runtime = require_official
    kb = kb if kb is not None else build_erc_kb()
    gates: dict[str, dict] = {}
    artifacts: dict[str, str] = {}

    try:
        errors = validate_circuit_json(circuit, CATALOG)
        errors.extend(validate_v2_metadata(circuit))
    except Exception as exc:
        errors = [f"结构检查器无法处理该输入：{type(exc).__name__}: {exc}"]
    if isinstance(circuit, dict) and circuit.get("version", 1) not in (1, 2):
        errors.append("version 只支持 1 或 2")
    gates["structure"] = _gate(
        "fail" if errors else "pass", required=True,
        issues=[_issue("CircuitJSON结构", err) for err in errors],
        evidence={"version": circuit.get("version", 1)}
        if isinstance(circuit, dict) else {})
    if errors:
        for key in _ORDER[1:]:
            gates[key] = _gate("not_run", required=(key in {"erc", "ratings", "kicad_export"}
                                                    or (require_official and key in
                                                        {"official_erc", "official_netlist"})
                                                    or (require_spice_runtime and key ==
                                                        "spice_runtime")),
                               reason="结构校验失败")
        return _result(source, name, require_official, gates, artifacts)

    erc = run_erc(circuit, kb)
    gates["erc"] = _gate(
        "pass" if erc.passed else "fail", required=True,
        issues=[_issue(i.rule, i.message, severity=i.severity, pins=i.pins)
                for i in erc.issues], evidence={"counts": erc.counts()})
    try:
        ratings = run_rating_checks(circuit)
        gates["ratings"] = _gate(
            "fail" if any(i.severity == "danger" for i in ratings) else "pass",
            required=True,
            issues=[_issue(i.rule, i.message, severity=i.severity, pins=i.pins)
                    for i in ratings],
            evidence={"danger_count": sum(i.severity == "danger" for i in ratings)})
    except Exception as exc:
        gates["ratings"] = _gate("fail", required=True,
                                  issues=[_issue("额定值检查异常",
                                                 f"{type(exc).__name__}: {exc}")])

    gates["spice_deck"], gates["spice_runtime"] = _validate_spice(
        circuit, name, require_spice_runtime)

    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="zhihui-validation-") as tmp:
            _validate_kicad(circuit, name, Path(tmp), kb, erc, require_official,
                            gates, artifacts, keep_artifacts=False)
    else:
        _validate_kicad(circuit, name, Path(work_dir), kb, erc,
                        require_official, gates, artifacts, keep_artifacts=True)
    return _result(source, name, require_official, gates, artifacts)


def _validate_kicad(circuit: dict, name: str, directory: Path, kb: dict,
                    erc, require_official: bool, gates: dict,
                    artifacts: dict, *, keep_artifacts: bool) -> None:
    try:
        sch_path, warnings = kicad_export.export_to_dir(
            circuit, name, directory, kb=kb, erc_report=erc)
        raw = sch_path.read_bytes()
        if not raw.startswith(b"(kicad_sch"):
            raise ValueError("导出文件缺少 kicad_sch 根节点")
        gates["kicad_export"] = _gate(
            "fail" if warnings else "pass", required=True,
            issues=[_issue("KiCad导出", w) for w in warnings],
            evidence={"sha256": hashlib.sha256(raw).hexdigest(),
                      "bytes": len(raw)})
        if keep_artifacts:
            artifacts["schematic"] = str(sch_path.resolve())
            artifacts["project"] = str(sch_path.with_suffix(".kicad_pro").resolve())
    except Exception as exc:
        gates["kicad_export"] = _gate(
            "fail", required=True,
            issues=[_issue("KiCad导出", f"{type(exc).__name__}: {exc}")])
        for key in ("official_erc", "official_netlist"):
            gates[key] = _gate("not_run", required=require_official,
                               reason="KiCad 导出失败")
        return

    if not require_official:
        for key in ("official_erc", "official_netlist"):
            gates[key] = _gate("not_run", required=False,
                               reason="调用方选择离线内部验证模式")
        return
    if not kicad_export.find_kicad_cli():
        for key in ("official_erc", "official_netlist"):
            gates[key] = _gate("unavailable", required=True,
                               reason="未找到 kicad-cli；未进行官方复验")
        return

    official = kicad_export.run_official_erc(sch_path)
    if official is None:
        gates["official_erc"] = _gate("unavailable", required=True,
                                      reason="kicad-cli ERC 未返回可解析报告")
    else:
        violations = official.get("violations", [])
        gates["official_erc"] = _gate(
            "fail" if violations else "pass", required=True,
            issues=[_issue(str(v.get("type", "KiCad ERC")),
                           str(v.get("description") or v.get("message") or v),
                           severity=str(v.get("severity", "error")))
                    for v in violations],
            evidence={"total": official.get("total", len(violations)),
                      "by_type": official.get("by_type", {}),
                      "violations": violations,
                      "report": official.get("report") if keep_artifacts else None})

    observed = kicad_export.export_official_netlist(sch_path)
    if observed is None:
        gates["official_netlist"] = _gate(
            "unavailable", required=True,
            reason="kicad-cli 未返回可解析网表")
    else:
        diffs = compare_official_netlist(circuit, observed, kb)
        gates["official_netlist"] = _gate(
            "fail" if diffs else "pass", required=True,
            issues=[_issue("网表不一致", f"网络 {d['net']} 与 CircuitJSON 不一致",
                           pins=[f"{ref}:{pin}" for ref, pin in d["expected"]])
                    for d in diffs],
            evidence={"differences": diffs, "net_count": len(observed)})
        if keep_artifacts:
            artifacts["netlist"] = str(sch_path.with_suffix(".net").resolve())


def _result(source: str, name: str, require_official: bool,
            gates: dict, artifacts: dict) -> dict:
    required = [g for g in gates.values() if g["required"]]
    if any(g["status"] == "fail" for g in required):
        verdict = "fail"
    elif any(g["status"] != "pass" for g in required):
        verdict = "incomplete"
    else:
        verdict = "pass"
    return {"source": source, "name": name,
            "assurance": "official" if require_official else "internal",
            "verdict": verdict, "accepted": verdict == "pass",
            "gate_order": list(_ORDER), "gates": gates,
            "blocking_gates": [key for key, gate in gates.items()
                               if gate["required"] and gate["status"] != "pass"],
            "artifacts": artifacts}


__all__ = ["validate_candidate", "compare_official_netlist", "SOURCES"]
