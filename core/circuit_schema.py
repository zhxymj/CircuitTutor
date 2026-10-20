# -*- coding: utf-8 -*-
"""CircuitJSON v1/v2 的稳定数据合同（JSON Schema Draft 2020-12）。

Schema 只描述跨工具共享的数据形状；元件目录、引脚存在性和电气规则仍由
``core.llm.validate_circuit_json`` 以及 ERC 检查器负责。v1 保持历史模板
兼容；v2 在相同 parts/connections 结构上增加工程元数据，不推测缺失值。
"""

from copy import deepcopy
import math
from typing import Any, Dict


_DRAFT = "https://json-schema.org/draft/2020-12/schema"


CIRCUIT_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": _DRAFT,
    "$id": "https://zhihui-circuit.local/schema/circuitjson-v1.json",
    "title": "CircuitJSON",
    "description": "智绘电路的元件与连接数据合同，版本 1。",
    "type": "object",
    "required": ["parts", "connections"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "title": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "parts": {"type": "array", "items": {"$ref": "#/$defs/part"}},
        "connections": {
            "type": "array",
            "items": {"$ref": "#/$defs/connection"},
        },
    },
    # 模板和前端可能携带展示元数据；Schema 不应因此拒绝既有电路。
    "additionalProperties": True,
    "$defs": {
        "part": {
            "type": "object",
            "required": ["type", "id"],
            "properties": {
                "type": {"type": "string", "minLength": 1},
                "id": {"type": "string", "minLength": 1},
                "top": {"type": "number"},
                "left": {"type": "number"},
                "rotate": {"type": "number", "enum": [0, 90, 180, 270]},
                "attrs": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "endpoint": {
            "type": "string",
            "minLength": 3,
            "pattern": r"^[^:\s]+:[^:\s]+$",
        },
        "path_point": {
            "type": "array",
            "prefixItems": [{"type": "number"}, {"type": "number"}],
            "items": False,
            "minItems": 2,
            "maxItems": 2,
        },
        "connection": {
            "type": "array",
            "prefixItems": [
                {"$ref": "#/$defs/endpoint"},
                {"$ref": "#/$defs/endpoint"},
                {"type": "string"},
                {
                    "oneOf": [
                        # _flatten_connection_paths 的规范化结果：扁平坐标序列。
                        {"type": "array", "items": {"type": "number"}},
                        # 保留模型/外部工具可能提供的 [[x, y], ...] 形式。
                        {"type": "array", "items": {"$ref": "#/$defs/path_point"}},
                    ]
                },
            ],
            "items": False,
            "minItems": 2,
            "maxItems": 4,
        },
    },
}


CIRCUIT_JSON_SCHEMA_V2: Dict[str, Any] = deepcopy(CIRCUIT_JSON_SCHEMA)
CIRCUIT_JSON_SCHEMA_V2["$id"] = "https://zhihui-circuit.local/schema/circuitjson-v2.json"
CIRCUIT_JSON_SCHEMA_V2["description"] = "智绘电路的元件、连接及工程元数据合同，版本 2。"
CIRCUIT_JSON_SCHEMA_V2["required"] = ["version", "parts", "connections"]
CIRCUIT_JSON_SCHEMA_V2["properties"]["version"] = {"type": "integer", "const": 2}

_V2_PROPERTIES = CIRCUIT_JSON_SCHEMA_V2["properties"]
_V2_PART_PROPERTIES = CIRCUIT_JSON_SCHEMA_V2["$defs"]["part"]["properties"]

_V2_PART_PROPERTIES["component"] = {
    "type": "object",
    "description": "已核实的器件身份与原理图引脚到 PCB 焊盘的映射。",
    "properties": {
        "manufacturer": {"type": "string"},
        "mpn": {"type": "string"},
        "symbol": {"type": "string"},
        "footprint": {"type": "string"},
        "datasheet": {"type": "string"},
        "pin_map_source": {"type": "string"},
        "pin_map": {
            "type": "object",
            "description": "键为 CircuitJSON 引脚名，值为封装焊盘号。",
            "additionalProperties": {"type": "string", "minLength": 1},
        },
        "pin_types": {
            "type": "object",
            "description": "按数据手册核实的具名引脚电气类型；真实有源器件源原理图要求完整提供。",
            "additionalProperties": {"type": "string", "enum": [
                "input", "output", "bidirectional", "tri_state", "passive", "free",
                "unspecified", "power_in", "power_out", "open_collector", "open_emitter", "no_connect",
            ]},
        },
    },
    "additionalProperties": True,
}
_V2_PART_PROPERTIES["supply_domain"] = {"type": "string", "minLength": 1}
_V2_PART_PROPERTIES["pcb"] = {
    "type": "object",
    "properties": {
        "x_mm": {"type": "number"},
        "y_mm": {"type": "number"},
        "rotation_deg": {"type": "number"},
        "side": {"type": "string", "enum": ["front", "back"]},
    },
    "additionalProperties": True,
}
_V2_PROPERTIES["voltage_domains"] = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "nominal_v": {"type": "number"},
            "min_v": {"type": "number"},
            "max_v": {"type": "number"},
            "nets": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "additionalProperties": True,
    },
}
_V2_PROPERTIES["board"] = {
    "type": "object",
    "properties": {
        "width_mm": {"type": "number", "exclusiveMinimum": 0},
        "height_mm": {"type": "number", "exclusiveMinimum": 0},
        "layers": {"type": "integer", "enum": [2]},
        "min_clearance_mm": {"type": "number", "exclusiveMinimum": 0},
        "min_trace_width_mm": {"type": "number", "exclusiveMinimum": 0},
    },
    "additionalProperties": True,
}
_V2_PROPERTIES["provenance"] = {
    "type": "object",
    "properties": {
        "origin": {
            "type": "string",
            "enum": ["prompt", "template", "image", "import", "manual", "legacy"],
        },
        "source_id": {"type": "string"},
        "model_id": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "source_version": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": True,
}


def get_circuit_json_schema(version: int = 1) -> Dict[str, Any]:
    """返回指定版本 Schema 的独立副本；旧调用默认仍返回 v1。"""
    if type(version) is not int or version not in (1, 2):
        raise ValueError("CircuitJSON Schema 仅支持版本 1 或 2")
    return deepcopy(CIRCUIT_JSON_SCHEMA if version == 1 else CIRCUIT_JSON_SCHEMA_V2)


def migrate_v1_to_v2(circuit: Dict[str, Any]) -> Dict[str, Any]:
    """无损复制 v1 图并标注来源；未知器件、封装和电压绝不猜测。"""
    if not isinstance(circuit, dict):
        raise ValueError("CircuitJSON 必须是对象")
    version = circuit.get("version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("只支持迁移 CircuitJSON v1 或复制 v2")
    if not isinstance(circuit.get("parts"), list) or not isinstance(circuit.get("connections"), list):
        raise ValueError("CircuitJSON 必须包含 parts 和 connections 数组")
    result = deepcopy(circuit)
    result["version"] = 2
    if version == 1 and "provenance" not in result:
        result["provenance"] = {"origin": "legacy", "source_version": 1}
    return result


def validate_v2_metadata(circuit: Any) -> list[str]:
    """在无第三方 JSON Schema 运行库时检查 v2 新增字段的基本类型。"""
    if not isinstance(circuit, dict) or circuit.get("version") != 2:
        return []
    errors: list[str] = []

    def number(value: Any) -> bool:
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False

    parts = circuit.get("parts") if isinstance(circuit.get("parts"), list) else []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        path = f"parts[{index}]"
        component = part.get("component")
        if component is not None:
            if not isinstance(component, dict):
                errors.append(f"{path}.component 必须是对象")
            else:
                for key in ("manufacturer", "mpn", "symbol", "footprint", "datasheet",
                            "pin_map_source"):
                    if key in component and not isinstance(component[key], str):
                        errors.append(f"{path}.component.{key} 必须是字符串")
                pin_map = component.get("pin_map")
                if pin_map is not None and (not isinstance(pin_map, dict) or
                        any(not isinstance(k, str) or not k or not isinstance(v, str)
                            or not v for k, v in pin_map.items())):
                    errors.append(f"{path}.component.pin_map 必须是非空字符串到焊盘号的映射")
                pin_types = component.get("pin_types")
                allowed_types = set(_V2_PART_PROPERTIES["component"]["properties"]["pin_types"]["additionalProperties"]["enum"])
                if pin_types is not None and (not isinstance(pin_types, dict) or
                        any(not isinstance(k, str) or not k or not isinstance(v, str)
                            or v not in allowed_types for k, v in pin_types.items())):
                    errors.append(f"{path}.component.pin_types 必须是具名引脚到 KiCad 电气类型的映射")
        if "supply_domain" in part and (not isinstance(part["supply_domain"], str)
                                        or not part["supply_domain"]):
            errors.append(f"{path}.supply_domain 必须是非空字符串")
        pcb = part.get("pcb")
        if pcb is not None:
            if not isinstance(pcb, dict):
                errors.append(f"{path}.pcb 必须是对象")
            else:
                for key in ("x_mm", "y_mm", "rotation_deg"):
                    if key in pcb and not number(pcb[key]):
                        errors.append(f"{path}.pcb.{key} 必须是有限数值")
                if "side" in pcb and pcb["side"] not in ("front", "back"):
                    errors.append(f"{path}.pcb.side 只能是 front 或 back")

    board = circuit.get("board")
    if board is not None:
        if not isinstance(board, dict):
            errors.append("board 必须是对象")
        else:
            for key in ("width_mm", "height_mm", "min_clearance_mm", "min_trace_width_mm"):
                if key in board and (not number(board[key]) or board[key] <= 0):
                    errors.append(f"board.{key} 必须是正有限数值")
            if "layers" in board and (type(board["layers"]) is not int or board["layers"] != 2):
                errors.append("board.layers 目前只能是 2")

    domains = circuit.get("voltage_domains")
    if domains is not None:
        if not isinstance(domains, list):
            errors.append("voltage_domains 必须是数组")
        else:
            for index, domain in enumerate(domains):
                path = f"voltage_domains[{index}]"
                if not isinstance(domain, dict) or not isinstance(domain.get("id"), str) or not domain["id"]:
                    errors.append(f"{path}.id 必须是非空字符串")
                    continue
                for key in ("nominal_v", "min_v", "max_v"):
                    if key in domain and not number(domain[key]):
                        errors.append(f"{path}.{key} 必须是有限数值")
                if "nets" in domain and (not isinstance(domain["nets"], list) or
                                         any(not isinstance(net, str) or not net for net in domain["nets"])):
                    errors.append(f"{path}.nets 必须是非空网络名数组")

    provenance = circuit.get("provenance")
    if provenance is not None:
        if not isinstance(provenance, dict):
            errors.append("provenance 必须是对象")
        else:
            if "origin" in provenance and provenance["origin"] not in (
                    "prompt", "template", "image", "import", "manual", "legacy"):
                errors.append("provenance.origin 未知")
            for key in ("source_id", "model_id"):
                if key in provenance and not isinstance(provenance[key], str):
                    errors.append(f"provenance.{key} 必须是字符串")
            if "confidence" in provenance and (not number(provenance["confidence"])
                                               or not 0 <= provenance["confidence"] <= 1):
                errors.append("provenance.confidence 必须在 0 到 1 之间")
            if "source_version" in provenance and (type(provenance["source_version"]) is not int
                                                   or provenance["source_version"] < 1):
                errors.append("provenance.source_version 必须是正整数")
    return errors


# 便于调用方按常见命名导入，均指向同一份只读约定（请使用 getter 修改副本）。
CIRCUITJSON_SCHEMA = CIRCUIT_JSON_SCHEMA


__all__ = [
    "CIRCUIT_JSON_SCHEMA", "CIRCUIT_JSON_SCHEMA_V2", "CIRCUITJSON_SCHEMA",
    "get_circuit_json_schema", "migrate_v1_to_v2",
    "validate_v2_metadata",
]
