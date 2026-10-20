#!/usr/bin/env python3
"""自然语言 -> KiCad 原理图：端到端生成与校验（零第三方依赖）。

四点设计承诺（与需求一一对应）:
  1) 需求理解: DeepSeek LLM 抽取元件并生成 CircuitJSON（无 Key 或失败时
     自动回落本地模板路由，离线可用；回落会如实记录进报告）;
  2) 格式合规: 经 kicad.export 导出 KiCad 8/9 原理图（内嵌符号库 +
     配套工程 .kicad_pro / sym-lib-table / CircuitTutor.kicad_sym），
     已在 KiCad 9.0.8 真机验证：可打开、元件/连线/属性完全可编辑;
  3) 默认值: 描述缺失/模糊的参数采用元件目录约定默认值
     （resistor=10k、battery=5V、capacitor=100nF 等），报告中逐项注明
     来源（用户指定 / 约定默认 / AI 补充）;
  4) 校验: 自身 ERC + kicad-cli 官方 ERC（目标归零）+ 官方网表连通性
     逐网络比对 + “描述-电路”一致性偏差清单，全部写入报告。

用法:
  python ai2kicad.py "做一个555闪烁灯电路"
  python ai2kicad.py "两个LED并联" --out myout
  python ai2kicad.py "LED限流电路" --offline     # 强制离线模板路由
  python ai2kicad.py "..." --no-autofix          # 关闭警告自动修复
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import re
import sys
from pathlib import Path

from core import autofix
from core import catalog
from core import config
from core import clarify
from core import templates
from core import erc
from core import llm
from kicad import export as K


# ---------------------------------------------------------------------
# 描述-电路 词汇比对工具
# ---------------------------------------------------------------------

# 解析正则与词表已上移到 core.catalog（单一数据源，模板注入同源）；
# 这里保留旧名别名，既有测试/调用（A._query_params 等）不受影响
_UNIT_MAP = catalog.PARAM_UNIT_MAP
_PARAM_FULL = catalog.PARAM_FULL
_PARAM_BARE = catalog.PARAM_BARE
_MULT = catalog.PARAM_MULT


def _extract_params(text: str):
    return catalog.extract_params(text)


def _value_params(value: str):
    """从元件值字符串提取 (数值, 归一单位) 列表，如 '10kΩ'->[(10.0,'Ω')]。"""
    return _extract_params(str(value))


def _query_params(query: str):
    """从用户描述提取参数（数字+单位），如 '5V供电、1k电阻'->[(5,'V'),(1000,'Ω')]。"""
    return _extract_params(query)


def _value_mentioned(value, query: str) -> bool:
    """元件值是否（数值等价地）出现在描述中。"""
    qps = _query_params(query)
    return any(vp in qps for vp in _value_params(value))


def _type_mentioned(ctype: str, query: str) -> bool:
    """描述中是否提及该元件类型（别名命中且无负向词排除）。"""
    entry = catalog.CATALOG.get(ctype)
    if not entry:
        return False
    q = query.lower()
    negs = entry.get("negative") or []
    if any(n.lower() in q for n in negs):
        return False
    return any(a.lower() in q for a in entry.get("aliases", []))


# ---------------------------------------------------------------------
# ① 需求理解：LLM 优先，模板兜底
# ---------------------------------------------------------------------

def maybe_clarify(query: str, no_clarify: bool = False, offline: bool = False,
                  verbose: bool = True, interactive=None, input_fn=None):
    """需求澄清闸门：模糊需求先问清楚再生成（判歧→提问→合并增强需求）。

    返回 (增强后的需求, 问答对列表)。不澄清的情况（原样返回）：
    * no_clarify（--no-clarify 显式关闭）；
    * interactive=False 或 stdin 非 TTY（脚本/CI 管道中绝不阻塞等待输入）；
    * 判歧结果为 clear（需求本身清晰，直接生成，不多余确认）。
    作答约定：输入选项编号或直接输入自由文本；回车 = 该问题不作答
    （未作答的问题不补任何假设，与 Web 端语义一致）。"""
    if no_clarify:
        return query, []
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        return query, []
    if input_fn is None:
        input_fn = input

    client = None
    if not offline and config.api_key_configured():
        client = llm.DeepSeekClient()
    try:
        # 判歧单独计费：独立小预算（1 次），不占用生成流水线的预算
        res = clarify.analyze(query, client, llm.CallBudget(max_calls=1))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    questions = res.get("questions") or []
    if res.get("status") != "clarify" or not questions:
        return query, []
    if verbose:
        src = {"llm": "LLM", "rules": "本地规则"}.get(res.get("source"), "?")
        print(f"  需求有 {len(questions)} 处需要确认（判歧：{src}）：")
    qa = []
    for i, qu in enumerate(questions, 1):
        print(f"    {i}. {qu['question']}")
        if qu.get("reason"):
            print(f"       （为什么要问：{qu['reason']}）")
        opts = qu.get("options") or []
        for j, opt in enumerate(opts, 1):
            print(f"       {j}) {opt}")
        try:
            ans = input_fn("       选择编号或直接输入答案（回车跳过）：").strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""                      # 输入流结束/中断：按未作答处理
        if ans.isdigit() and 1 <= int(ans) <= len(opts):
            ans = opts[int(ans) - 1]
        if ans:
            qa.append({"question": qu["question"], "answer": ans})
    if qa and verbose:
        print(f"  已并入 {len(qa)} 条澄清答复 ✔")
    return clarify.merge_clarifications(query, qa, max_len=config.MAX_QUERY_LEN), qa


def understand(query: str, offline: bool = False, verbose: bool = True):
    """返回 (circuit, 来源说明 dict)。兜底链: LLM -> 模板路由 -> 默认模板。"""
    unsupported = catalog.unsupported_part_numbers(query)
    if unsupported:
        raise ValueError("需求含目录外器件，需先补充器件合同：" + "、".join(unsupported))
    blockers = llm.detect_requirement_blockers(query)
    if blockers:
        raise llm.IntentConstraintError(blockers)
    def progress(stage, detail=""):
        if verbose:
            print(f"  [LLM] {stage}: {detail}" if detail else f"  [LLM] {stage}")

    if not offline and config.api_key_configured():
        client = llm.DeepSeekClient()
        try:
            budget = llm.CallBudget(max_calls=config.LLM_BUDGET)
            types, source = llm.extract_components(
                query, client, budget, on_progress=progress)
            if verbose:
                print(f"  识别元件: {'、'.join(types) or '（无）'}（{source}）")
            if not types:
                raise ValueError("未识别到任何元件")
            matched_lib = catalog.build_matched_lib(types)
            circuit = llm.generate_circuit_json(
                query, matched_lib, client,
                max_retry=config.LLM_STRUCT_RETRIES,
                budget=budget, on_progress=progress,
                erc_kb=catalog.build_erc_kb())
            client.close()
            return circuit, {"path": "llm", "types": types, "note": "DeepSeek LLM 生成"}
        except llm.IntentConstraintError:
            raise
        except Exception as e:                       # 兜底：不让演示中断
            if verbose:
                print(f"  [LLM 失败 -> 模板兜底] {type(e).__name__}: {e}")
            fallback_reason = f"{type(e).__name__}: {e}"
        finally:
            try:
                client.close()
            except Exception:
                pass
    else:
        fallback_reason = "未配置 DEEPSEEK_API_KEY" if not offline else "指定 --offline"

    key = catalog.match_template(query, templates.FALLBACK_TEMPLATES)
    used = key or "led_limit"
    circuit = copy.deepcopy(templates.BUILTIN_TEMPLATES[used])
    # 采纳用户显式参数（此前模板兜底只用默认值，描述里的 470Ω/红色 等
    # 会被静默忽略，只能靠偏差报告提示——现在直接注入，报告随之归零）
    injected = catalog.inject_query_values(circuit, query)
    # 欧姆定律精确化（LED 串联电阻 → E12）；失败不影响导出
    try:
        from core import design_rules
        _, pnotes, _pw = design_rules.apply_precision(circuit, query)
        if pnotes:
            note_extra = "；参数计算: " + "、".join(pnotes)
        else:
            note_extra = ""
    except Exception:
        note_extra = ""
    note = f"本地模板兜底（{used} 模板；原因: {fallback_reason}）"
    note += note_extra
    if injected:
        note += "；已采纳描述参数: " + "、".join(
            f"{ref} {old}→{new}" for ref, old, new in injected)
    return circuit, {"path": "template", "types": [], "note": note,
                     "injected": injected}


# ---------------------------------------------------------------------
# ③ 默认值 / 来源标注
# ---------------------------------------------------------------------

def analyze_parts(circuit: dict, query: str) -> list:
    """每个元件一行: (位号, 类型, 值, 来源标签, 说明)。"""
    used_types = {p.get("type") for p in circuit.get("parts", [])}
    rows = []
    for p in circuit.get("parts", []):
        ctype = p.get("type", "?")
        ref = str(p.get("id", "?"))
        attrs = p.get("attrs") or {}
        value = attrs.get("value")
        entry = catalog.CATALOG.get(ctype, {})
        if ctype in ("gnd", "vcc"):
            label = "AI 补充"
            note = "电源/地参考（工程惯例，原理图完整性需要）"
        elif not _type_mentioned(ctype, query):
            label = "AI 补充"
            note = f"描述未直接提及，按“{entry.get('name', ctype)}”设计惯例补充"
        elif value and _value_mentioned(value, query):
            label = "用户指定"
            note = "参数在描述中明确给出"
        elif value:
            label = "约定默认值"
            note = (f"描述未给出该值，采用目录约定默认 {entry.get('default', value)}"
                    + ("" if str(value) == str(entry.get("default")) else "（按电路上下文取值）"))
        else:
            label = "结构必需"
            note = "连接/拓扑所需（无参数）"
        rows.append((ref, ctype, value if value not in (None, "") else "—", label, note))
    _ = used_types
    return rows


def consistency_check(circuit: dict, query: str) -> list:
    """描述-电路一致性偏差清单（要求 4）。"""
    devs = []
    circuit_types = {p.get("type") for p in circuit.get("parts", [])}
    # a) 描述提到、电路缺失的类型（同目录语义下检查别名归并）
    for t in catalog.CATALOG:
        if _type_mentioned(t, query) and t not in circuit_types:
            # 近义归并: 提到'灯'而电路只有 led 算已覆盖（CATALOG 别名互不冲突时不会到这里）
            devs.append(f"描述提到“{catalog.CATALOG[t]['name']}({t})”，"
                        f"但电路中未包含该类元件")
    # b) 描述给出的参数（数字+单位）没有落到任何元件值上
    qps = _query_params(query)
    for num, unit in qps:
        covered = any((num, unit) in _value_params(p.get("attrs", {}).get("value", ""))
                      for p in circuit.get("parts", []))
        if not covered:
            pretty = catalog.format_value(num, unit)
            devs.append(f"描述中的参数“{pretty}”未体现在任何元件值上"
                        f"（可能被默认值替代，请核对）")
    # c) 重复位号（LLM 偶发，导出可容忍但必须指出）
    ids = [str(p.get("id")) for p in circuit.get("parts", [])]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        devs.append(f"存在重复位号: {'、'.join(dups)}（编辑时请先重新标注）")
    return devs


# ---------------------------------------------------------------------
# 期望网络（独立重算，不信任导出器自身）
# ---------------------------------------------------------------------

def expected_nets(circuit: dict) -> dict:
    """并查集独立重算 {引脚引用: 网络名}（与第 12 套件同一套算法）。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ptype = {p["id"]: p["type"] for p in circuit.get("parts", [])}
    for conn in circuit.get("connections", []):
        refs = [r for r in conn[:2] if isinstance(r, str) and ":" in r]
        for r in refs:
            find(r)
        for r in refs[1:]:
            parent[find(refs[0])] = find(r)
    groups = {}
    for r in list(parent):
        groups.setdefault(find(r), []).append(r)
    out = {}
    ordered = sorted(groups, key=lambda r: min(groups[r]))
    for idx, root in enumerate(ordered):
        members = groups[root]
        name = f"N{idx + 1}"
        for anchor, label in (("gnd", "GND"), ("vcc", "VCC")):
            if any(ptype.get(m.split(":", 1)[0]) == anchor for m in members):
                name = label
                break
        for m in members:
            out[m] = name
    return out


def net_diffs(circuit: dict, kicad_nets: dict) -> list:
    """官方网表 vs 独立重算的期望网络：逐网络成员比对。"""
    ptype = {p["id"]: p["type"] for p in circuit.get("parts", [])}
    want = {}
    for pref, net in expected_nets(circuit).items():
        ref, pin = pref.split(":", 1)
        pm = K.TYPE_MAP.get(ptype.get(ref), (None, {}, None))[1]
        if pin in pm:
            want.setdefault(net, set()).add((ref, pm[pin]))
    diffs = []
    for net in sorted(set(want) | set(kicad_nets)):
        if net.startswith("unconnected-"):
            continue
        got = {n for n in kicad_nets.get(net, set()) if not n[0].startswith("#")}
        if got != want.get(net, set()):
            diffs.append(f"网络 {net}: KiCad={sorted(got)} 期望={sorted(want.get(net, set()))}")
    return diffs


# ---------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------

def _export_optimized_dxf(sch_path: Path, out_dir: Path) -> list:
    """kicad-cli 导出 DXF → dxf_optimize 优化（mm 单位 / 正象限 / 头补全）。

    DXF 属附加产物：任何失败仅记日志返回空表，绝不影响主流程。"""
    import logging
    import subprocess
    log = logging.getLogger("ai2kicad")
    cli = K.find_kicad_cli()
    if not cli:
        log.warning("未找到 kicad-cli，跳过 CAD 互通 DXF 导出")
        return []
    try:
        from core import dxf
        dxf_dir = out_dir / "cad_dxf"
        dxf_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [cli, "sch", "export", "dxf", "-o", str(dxf_dir), str(sch_path)],
            capture_output=True, text=True, timeout=config.KICAD_CLI_TIMEOUT_S,
            encoding="utf-8", errors="replace", check=False)
        outs = []
        for dxf in sorted(dxf_dir.glob("*.dxf")):
            if dxf.stem.endswith("_CAD导入版"):
                continue
            target = dxf.with_name(dxf.stem + "_CAD导入版.dxf")
            dxf.optimize_file(dxf, target, mm=True, shift_origin=True)
            outs.append(target)
        return outs
    except Exception as e:  # noqa: BLE001 —— 附加产物失败不阻断
        log.warning("DXF 导出/优化失败（不影响主流程）: %s: %s",
                    type(e).__name__, e)
        return []


def run(query: str, out_dir: Path, offline: bool = False,
        no_autofix: bool = False, verbose: bool = True,
        dxf: bool = False, no_clarify: bool = False) -> int:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if verbose:
        print(f"① 需求理解: “{query}”")
    # 澄清闸门：模糊需求先问再生成（非 TTY/--no-clarify 自动跳过）
    query, qa_pairs = maybe_clarify(query, no_clarify=no_clarify,
                                    offline=offline, verbose=verbose)
    name = K.slug(query, max_len=24)
    kb = catalog.build_erc_kb()

    circuit, src = understand(query, offline=offline, verbose=verbose)

    if verbose:
        print("② ERC 校验 + 工程惯例补全")
    rep = erc.run_erc(circuit, kb)
    fixes = []
    if not no_autofix and config.AUTOFIX:
        fixed, fixes = autofix.apply(circuit, kb, rep=rep)
        if fixes and not llm.validate_circuit_json(fixed):
            circuit = fixed
            rep = erc.run_erc(circuit, kb)

    # 参数精确化 + 拓扑黄金模式（与 server 路径同源）。
    # 注意：pnotes 不并入 fixes——报告里「自动修复」专指 autofix 电容附挂，
    # --no-autofix 时该节必须保持「- 无」；参数计算只打日志/verbose。
    precision_notes: list = []
    try:
        from core import design_rules
        circuit, pnotes, pwarns = design_rules.apply_precision(circuit, query)
        precision_notes = list(pnotes)
        if verbose:
            for n in precision_notes:
                print("  参数计算:", n)
            for w in pwarns:
                print("  拓扑提示:", w)
        if precision_notes:
            rep = erc.run_erc(circuit, kb)
    except Exception:
        pass
    counts = rep.counts()
    fatals = counts["fatal"]
    majors = counts["major"]
    minors = counts["minor"] + counts["warning"]

    if verbose:
        print("③ 导出 KiCad 原理图 + 配套工程")
    sch_path, warns = K.export_to_dir(circuit, name, out_dir, kb=kb, erc_report=rep)

    if verbose:
        print("④ KiCad 官方校验（kicad-cli）")
    from core.validation_gate import validate_candidate
    validation = validate_candidate(
        circuit, source="ai" if src.get("path") == "llm" else "template",
        name=name, work_dir=out_dir, require_official=True)
    validation_path = out_dir / "校验门禁.json"
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    official_gate = validation["gates"]["official_erc"]
    official = official_gate["evidence"] if official_gate["status"] in ("pass", "fail") else None
    net_gate = validation["gates"]["official_netlist"]
    netlist_verified = net_gate["status"] == "pass"
    ndiffs = ([item["message"] for item in net_gate["issues"]]
              if net_gate["status"] == "fail" else
              ([] if netlist_verified else [net_gate["reason"] or "官方网表未完成复验"]))

    dxf_files = _export_optimized_dxf(sch_path, Path(out_dir)) if dxf else []

    rows = analyze_parts(circuit, query)
    devs = consistency_check(circuit, query)
    if fatals or majors:
        devs = [f"自身 ERC 存在 fatal={fatals} major={majors}（详见下方校验节）"] + devs
    if official and official["total"]:
        devs.append(f"KiCad 官方 ERC 有 {official['total']} 条违规: "
                    f"{official['by_type']}")

    # ---- 报告 ----
    L = [f"# AI → KiCad 生成报告", "",
         f"- 需求描述：{query}",
         f"- 生成时间：{stamp}",
         f"- 需求理解：{src['note']}"]
    if qa_pairs:
        L.append(f"- 需求澄清：{len(qa_pairs)} 条问答已并入需求"
                 + "（" + "；".join(f"{qa['answer']}" for qa in qa_pairs[:4]) + "）")
    L += ["",
         "## 一、识别结果", "",
         "| 位号 | 类型 | 值 | 来源 | 说明 |",
         "|---|---|---|---|---|"]
    for ref, ctype, value, label, note in rows:
        L.append(f"| {ref} | {catalog.CATALOG.get(ctype, {}).get('name', ctype)} "
                 f"(`{ctype}`) | {value} | {label} | {note} |")
    L += ["", "### 连接关系（网络）", "",
          "| 网络 | 引脚成员 |", "|---|---|"]
    nets_map = {}
    for pref, net in sorted(expected_nets(circuit).items()):
        nets_map.setdefault(net, []).append(pref)
    for net in sorted(nets_map, key=lambda n: (n != "GND", n != "VCC", n)):
        L.append(f"| {net} | {'、'.join(nets_map[net])} |")
    if warns:
        L += ["", "> 导出警告：" + "；".join(warns)]
    if fixes:
        L += ["", "## 二、工程惯例补全（确定性）", ""]
        L += [f"- {f}" for f in fixes]
    else:
        L += ["", "## 二、工程惯例补全（确定性）", "", "- 无"]
    L += ["", "## 三、完整性与一致性校验", "",
          f"- 自身 ERC（{len(erc.ALL_CHECKERS)} 规则）：**fatal={fatals} major={majors} minor={minors}**"
          + ("，可继续编辑" if not fatals and not majors else "，请优先处理 fatal/major"),
          f"- KiCad {K.kicad_cli_version() or '（未安装，跳过官方校验）'} 官方 ERC："
          + (f"**{official['total']} 条违规** {'✔ 达标' if official and official['total'] == 0 else '✘'}"
             if official else "kicad-cli 不可用，跳过"),
          f"- KiCad 官方网表连通性："
          + ("**与预期逐网络一致** ✔" if netlist_verified else "；".join(ndiffs) or "未完成"),
          f"- 统一验收门禁：**{validation['verdict']}**；accepted={validation['accepted']}；"
          + "阻断项：" + ("、".join(validation["blocking_gates"]) or "无"),
          "", "### 一致性偏差", ""]
    L += [f"- {d}" for d in devs] if devs else ["- 未发现需求参数偏差；工程验收状态见上方门禁。"]
    L += ["", "## 四、产物与使用", "",
          f"- `{sch_path.name}` — KiCad 原理图（内嵌符号库）",
          f"- `{name}.kicad_pro` — 配套工程（双击它打开，符号库自动挂载）",
          f"- `CircuitTutor.kicad_sym` / `sym-lib-table` — 共享符号库与库表"]
    L.append("- `校验门禁.json` — 逐项校验结果、阻断原因与产物摘要")
    if dxf_files:
        L += [f"- CAD 互通 DXF ×{len(dxf_files)}（mm 单位 / 正象限 / 头变量补全）："
              + "、".join(f"`{p.name}`" for p in dxf_files),
              "- DXF 可直接导入 AutoCAD / 中望 / 浩辰 等 CAD 软件（查看/描图/二次排版）"]
    L += ["- KiCad 中可继续：移动/旋转元件、改值改位号、重新布线、"
          "工具→ERC 复查、导出网表/PCB。", ""]

    report_path = out_dir / "生成报告.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(L), encoding="utf-8")

    if verbose:
        print()
        print(f"✔ 完成：{sch_path}")
        print(f"  报告：{report_path}")
        print(f"  自身ERC fatal={fatals} major={majors} minor={minors} | "
              f"官方ERC违规={official['total'] if official else '跳过'} | "
              f"网表比对={'一致' if netlist_verified else '未通过或未完成'} | "
              f"偏差={len(devs)} 项")
    return 0 if validation["accepted"] else 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="自然语言 -> KiCad 原理图（AI 端到端）")
    ap.add_argument("query", help="中文电路需求描述")
    ap.add_argument("--out", default=None, help="输出目录（默认 ai2kicad_out/<需求摘要>）")
    ap.add_argument("--offline", action="store_true", help="强制离线模板路由（不调用 LLM）")
    ap.add_argument("--no-autofix", action="store_true",
                    help="关闭工程惯例补全（去耦电容等）")
    ap.add_argument("--no-clarify", action="store_true",
                    help="关闭需求澄清追问（模糊需求直接按默认理解生成；"
                         "非交互环境 stdin 非 TTY 时自动跳过澄清）")
    ap.add_argument("--dxf", action="store_true",
                    help="同时导出 CAD 互通 DXF（需本机 kicad-cli；mm 单位/正象限/头补全）")
    args = ap.parse_args(argv)

    # 日志配置收敛到 config.setup_logging() 单一入口（幂等）：
    # 此前此处另有一份格式不同的 basicConfig，与 core/config 的配置重复，
    # 实际生效格式取决于谁先执行（两处并存时后一次 basicConfig 静默失效）。
    config.setup_logging()
    out = Path(args.out) if args.out else Path("ai2kicad_out") / K.slug(args.query, max_len=24)
    try:
        return run(args.query, out, offline=args.offline, no_autofix=args.no_autofix,
                   dxf=args.dxf, no_clarify=args.no_clarify)
    except ValueError as exc:
        print(f"需求被拒绝：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
