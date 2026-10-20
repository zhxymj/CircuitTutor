# -*- coding: utf-8 -*-
"""CircuitJSON 的单轮 AI 增量编辑：候选图校验、差异与 ERC 质量门禁。"""

import copy
import json
from collections import Counter
from typing import Any, Dict, Optional

from core import catalog, erc
from core.llm import CallBudget, extract_json_object, validate_circuit_json


EDIT_SYSTEM_PROMPT = """你是严谨的电子原理图编辑助手。用户会提供当前 CircuitJSON 和一条中文修改指令。
只返回修改后的完整 CircuitJSON JSON 对象，不要 Markdown、解释或省略字段。
保留与指令无关的元件和连接；只能使用给出的标准元件目录中的 type 和其合法引脚。
每条 connection 维持 [起点引脚, 终点引脚, 颜色, 路径] 格式。"""


class CircuitEditError(ValueError):
    """改图失败且调用方不得返回候选电路。"""


class CircuitEditModelUnavailable(CircuitEditError):
    """没有可调用的模型。"""


class CircuitEditStructureError(CircuitEditError):
    """模型候选图未满足 CircuitJSON 结构和目录门禁。"""


class CircuitEditQualityError(CircuitEditError):
    """模型候选图使 fatal 或 major ERC 问题增加。"""


def _erc_counts(report: Any) -> Dict[str, int]:
    counts = report.counts()
    return {severity: int(counts.get(severity, 0))
            for severity in ("fatal", "major", "minor", "warning")}


def _part_map(circuit: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(part["id"]): part for part in circuit.get("parts", [])
            if isinstance(part, dict) and isinstance(part.get("id"), str)}


def _connection_counter(circuit: Dict[str, Any]) -> Counter:
    # 使用规范 JSON 字符串保留连接的顺序和附带颜色/路径语义，且支持重复计数。
    return Counter(json.dumps(conn, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                   for conn in circuit.get("connections", []))


def _focused_pin_catalog(circuit: Dict[str, Any], instruction: str) -> Dict[str, Dict[str, Any]]:
    """构造编辑所需的最小 type→pins 视图，避免把完整目录塞进提示词。"""
    current_types = {
        part.get("type") for part in circuit.get("parts", [])
        if isinstance(part, dict) and isinstance(part.get("type"), str)
    }
    requested_types = set(catalog.match_components(instruction))
    focused: Dict[str, Dict[str, Any]] = {}
    for component_type in sorted(current_types | requested_types):
        entry = catalog.CATALOG.get(component_type)
        if entry is not None:
            focused[component_type] = {"pins": sorted(entry.get("pins", {}).keys())}
    return focused


def summarize_changes(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """返回供 UI 展示的稳定、轻量改动摘要，不改写候选电路。"""
    old_parts, new_parts = _part_map(before), _part_map(after)
    old_ids, new_ids = set(old_parts), set(new_parts)
    changed_parts = sorted(pid for pid in old_ids & new_ids
                           if old_parts[pid] != new_parts[pid])
    old_connections, new_connections = _connection_counter(before), _connection_counter(after)
    added_connections = sum((new_connections - old_connections).values())
    removed_connections = sum((old_connections - new_connections).values())
    return {
        "parts_added": sorted(new_ids - old_ids),
        "parts_removed": sorted(old_ids - new_ids),
        "parts_modified": changed_parts,
        "connections_added": added_connections,
        "connections_removed": removed_connections,
    }


def edit_circuit(circuit: Dict[str, Any], instruction: str, client: Any,
                 erc_kb: Optional[Dict[str, Dict[str, str]]] = None,
                 max_calls: int = 1) -> Dict[str, Any]:
    """请求完整候选图，并在任何不安全结果上抛出不携带电路的异常。

    输入和候选都用完整 ``catalog.CATALOG`` 校验，防止模型凭空制造元件或引脚。
    只有候选没有新增 fatal/major ERC 条目时，才返回可以替换前端当前电路的数据。
    """
    if client is None:
        raise CircuitEditModelUnavailable("未配置可用模型，无法修改当前电路")
    if not isinstance(instruction, str) or not instruction.strip():
        raise CircuitEditError("缺少修改指令")

    source_errors = validate_circuit_json(circuit, catalog.CATALOG)
    if source_errors:
        raise CircuitEditStructureError("当前电路结构非法：" + "；".join(source_errors[:3]))

    user_prompt = json.dumps({
        "instruction": instruction.strip(),
        "circuit": circuit,
    }, ensure_ascii=False, separators=(",", ":"))
    focused_catalog = _focused_pin_catalog(circuit, instruction)
    system_prompt = (
        EDIT_SYSTEM_PROMPT
        + "\n本次可用元件及其引脚白名单（只能使用这些 type/pins）：\n"
        + json.dumps(focused_catalog, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    )
    budget = CallBudget(max_calls=max_calls)
    budget.spend()
    try:
        raw = client.chat(system_prompt, user_prompt, temperature=0.0,
                          json_mode=True)
    except Exception as exc:  # 外部客户端不应让 HTTP Handler 裸崩
        raise CircuitEditError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

    candidate = extract_json_object(raw)
    candidate_errors = validate_circuit_json(candidate, catalog.CATALOG)
    if candidate_errors:
        raise CircuitEditStructureError("候选电路结构非法：" + "；".join(candidate_errors[:3]))

    kb = erc_kb if erc_kb is not None else catalog.build_erc_kb()
    before_report = erc.run_erc(circuit, kb)
    after_report = erc.run_erc(candidate, kb)
    before_counts, after_counts = _erc_counts(before_report), _erc_counts(after_report)
    increased = [sev for sev in ("fatal", "major")
                 if after_counts[sev] > before_counts[sev]]
    if increased:
        detail = "、".join(f"{sev} {before_counts[sev]}→{after_counts[sev]}"
                            for sev in increased)
        raise CircuitEditQualityError(f"候选电路使 ERC 严重问题增加（{detail}）")

    # 防御性拷贝：调用方可以安全地把返回值交给响应编码，不能反向修改输入引用。
    return {
        "circuit": copy.deepcopy(candidate),
        "changes": summarize_changes(circuit, candidate),
        "erc": after_report.to_dict(),
    }
