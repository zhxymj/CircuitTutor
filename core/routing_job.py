# -*- coding: utf-8 -*-
"""PCBWorld 隔离接口：任务合同、奖励函数、规则基线和安全回退。

核心程序不导入 PCBWorld/PyTorch/Gym。外部路由服务只能通过本模块定义的
JSON 文件交换任务与结果；因此 WSL、模型或第三方依赖失败时，A 主线仍可运行。
当前规则基线只报告从 ``.kicad_pcb`` 文本可直接测得的走线几何与过孔数，
不会把“未运行 DRC/未判断连通性”伪装成 0 违规或 100% 布通。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


SCHEMA_VERSION = "1.0"
DEFAULT_CONSTRAINTS = {
    "layers": 2,
    "clearance_mm": 0.2,
    "max_vias": 20,
    "max_steps": 500,
    "timeout_s": 300,
}
DEFAULT_POLICY = {"name": "ppo-small-v1", "checkpoint": ""}
DEFAULT_REWARD_WEIGHTS = {
    "completion_delta": 10.0,
    "drc_violation": 2.0,
    "trace_length_mm": 0.01,
    "via": 0.5,
    "invalid_action": 1.0,
    "failure": 20.0,
}
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_SEGMENT_RE = re.compile(
    rf"\(segment\s+\(start\s+({_NUMBER})\s+({_NUMBER})\)"
    rf"\s+\(end\s+({_NUMBER})\s+({_NUMBER})\)",
    re.IGNORECASE | re.MULTILINE,
)


class RoutingJobError(ValueError):
    """任务合同不合法；``errors`` 保留全部可执行修正提示。"""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("；".join(self.errors))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _plain_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def create_job(board_path: str, output_dir: str, *, job_id: str,
               seed: int = 1550, constraints: Optional[Mapping[str, Any]] = None,
               policy: Optional[Mapping[str, Any]] = None) -> dict:
    """创建并验证一个 1.0 任务；不访问文件系统、不启动外部服务。"""
    merged_constraints = dict(DEFAULT_CONSTRAINTS)
    if constraints:
        merged_constraints.update(dict(constraints))
    merged_policy = dict(DEFAULT_POLICY)
    if policy:
        merged_policy.update(dict(policy))
    job = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "board_path": board_path,
        "output_dir": output_dir,
        "constraints": merged_constraints,
        "policy": merged_policy,
        "random_seed": seed,
    }
    return validate_job(job)


def validate_job(job: Any, *, require_board_exists: bool = False) -> dict:
    """校验边界与类型，返回深拷贝后的规范对象；一次报告全部错误。"""
    if not isinstance(job, dict):
        raise RoutingJobError(["任务必须是 JSON 对象"])
    errors = []
    if job.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {SCHEMA_VERSION!r}")

    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        errors.append("job_id 只能含字母、数字、点、下划线或短横线，长度 1~64")

    board_path = job.get("board_path")
    if not isinstance(board_path, str) or not board_path.strip():
        errors.append("board_path 必须是非空字符串")
    elif "\x00" in board_path:
        errors.append("board_path 不得包含 NUL 字符")
    elif not board_path.lower().endswith(".kicad_pcb"):
        errors.append("board_path 必须指向 .kicad_pcb 文件")
    elif require_board_exists and not os.path.isfile(board_path):
        errors.append(f"输入 PCB 不存在：{board_path}")

    output_dir = job.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        errors.append("output_dir 必须是非空字符串")
    elif "\x00" in output_dir:
        errors.append("output_dir 不得包含 NUL 字符")

    constraints = job.get("constraints")
    if not isinstance(constraints, dict):
        errors.append("constraints 必须是 JSON 对象")
        constraints = {}
    layers = constraints.get("layers")
    if layers != 2:
        errors.append("当前实验范围只支持双层 PCB，constraints.layers 必须为 2")
    clearance = constraints.get("clearance_mm")
    if not _plain_number(clearance) or not 0.05 <= float(clearance) <= 1.0:
        errors.append("clearance_mm 必须在 0.05~1.0 mm 范围内")
    max_vias = constraints.get("max_vias")
    if not isinstance(max_vias, int) or isinstance(max_vias, bool) or not 0 <= max_vias <= 1000:
        errors.append("max_vias 必须是 0~1000 的整数")
    max_steps = constraints.get("max_steps")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 100000:
        errors.append("max_steps 必须是 1~100000 的整数")
    timeout_s = constraints.get("timeout_s")
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or not 1 <= timeout_s <= 3600:
        errors.append("timeout_s 必须是 1~3600 的整数")

    policy = job.get("policy")
    if not isinstance(policy, dict):
        errors.append("policy 必须是 JSON 对象")
        policy = {}
    policy_name = policy.get("name")
    if not isinstance(policy_name, str) or not policy_name.strip() or len(policy_name) > 80:
        errors.append("policy.name 必须是长度 1~80 的非空字符串")
    checkpoint = policy.get("checkpoint", "")
    if not isinstance(checkpoint, str) or "\x00" in checkpoint:
        errors.append("policy.checkpoint 必须是不含 NUL 的字符串")

    seed = job.get("random_seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 0xFFFFFFFF:
        errors.append("random_seed 必须是 0~4294967295 的整数")

    if errors:
        raise RoutingJobError(errors)
    normalized = copy.deepcopy(job)
    normalized["board_path"] = os.path.normpath(board_path.strip())
    normalized["output_dir"] = os.path.normpath(output_dir.strip())
    normalized["constraints"] = {
        "layers": int(layers),
        "clearance_mm": float(clearance),
        "max_vias": int(max_vias),
        "max_steps": int(max_steps),
        "timeout_s": int(timeout_s),
    }
    normalized["policy"] = {
        "name": policy_name.strip(),
        "checkpoint": checkpoint.strip(),
    }
    return normalized


def compute_reward(completion_delta: float, drc_violations: int,
                   trace_length_mm: float, via_count: int,
                   invalid_actions: int, failed: bool,
                   weights: Optional[Mapping[str, float]] = None) -> float:
    """显式奖励函数；所有输入由调用方测量，本函数不猜测缺失值。"""
    values = [completion_delta, drc_violations, trace_length_mm,
              via_count, invalid_actions]
    if not all(_plain_number(value) for value in values):
        raise ValueError("奖励函数输入必须是有限数值")
    if any(float(value) < 0 for value in values[1:]):
        raise ValueError("DRC、线长、过孔和无效动作数不得为负")
    merged = dict(DEFAULT_REWARD_WEIGHTS)
    if weights:
        merged.update({key: float(value) for key, value in weights.items()})
    missing = [key for key in DEFAULT_REWARD_WEIGHTS if key not in merged]
    if missing or not all(_plain_number(merged[key]) for key in DEFAULT_REWARD_WEIGHTS):
        raise ValueError(f"奖励权重非法或缺失：{missing}")
    reward = (
        merged["completion_delta"] * float(completion_delta)
        - merged["drc_violation"] * int(drc_violations)
        - merged["trace_length_mm"] * float(trace_length_mm)
        - merged["via"] * int(via_count)
        - merged["invalid_action"] * int(invalid_actions)
        - (merged["failure"] if failed else 0.0)
    )
    return round(reward, 6)


def _metrics_template(job: Mapping[str, Any], *, elapsed_ms: float,
                      model_version: str) -> dict:
    return {
        "fully_routed": None,
        "net_completion_rate": None,
        "drc_violations": None,
        "total_trace_length_mm": None,
        "via_count": None,
        "inference_time_ms": round(max(0.0, float(elapsed_ms)), 3),
        "fallback_triggered": True,
        "model_version": model_version,
        "random_seed": job.get("random_seed"),
    }


def fallback_result(job: Mapping[str, Any], reason: str, *,
                    elapsed_ms: float = 0.0) -> dict:
    """外部服务失败时的诚实结果；不可测项为 null，绝不填 0 冒充测量。"""
    policy = job.get("policy") if isinstance(job, Mapping) else {}
    model = policy.get("name") if isinstance(policy, Mapping) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.get("job_id") if isinstance(job, Mapping) else None,
        "status": "fallback",
        "reason": str(reason) or "外部路由器不可用",
        "generated_at_utc": _utc_now(),
        "metrics": _metrics_template(job, elapsed_ms=elapsed_ms,
                                      model_version=str(model or "unavailable")),
        "measurement_status": {
            "routing_completion": "not_measured",
            "drc": "not_run",
            "trace_geometry": "not_measured",
        },
        "candidate_board_path": None,
        "recommendation": "保留规则预布局或原始 PCB；修复外部环境后重新提交任务。",
    }


def _observable_board_metrics(text: str) -> dict:
    length = 0.0
    for match in _SEGMENT_RE.finditer(text):
        x1, y1, x2, y2 = (float(value) for value in match.groups())
        length += math.hypot(x2 - x1, y2 - y1)
    vias = len(re.findall(r"\(via\s", text, flags=re.IGNORECASE))
    net_ids = {int(value) for value in re.findall(r"\(net\s+(\d+)\s+\"", text)}
    net_ids.discard(0)
    return {
        "total_trace_length_mm": round(length, 3),
        "via_count": vias,
        "declared_net_count": len(net_ids),
    }


def run_rule_baseline(job: Mapping[str, Any]) -> dict:
    """读取现有板文件并报告可直接观测值；它不是自动布线或 DRC。"""
    started = time.perf_counter()
    try:
        normalized = validate_job(dict(job), require_board_exists=True)
    except RoutingJobError as exc:
        return fallback_result(job, str(exc),
                               elapsed_ms=(time.perf_counter() - started) * 1000.0)
    try:
        with open(normalized["board_path"], encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        observable = _observable_board_metrics(text)
    except OSError as exc:
        return fallback_result(normalized, f"读取 PCB 失败：{exc}",
                               elapsed_ms=(time.perf_counter() - started) * 1000.0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    metrics = _metrics_template(normalized, elapsed_ms=elapsed_ms,
                                model_version="rule-prelayout-v1")
    metrics["total_trace_length_mm"] = observable["total_trace_length_mm"]
    metrics["via_count"] = observable["via_count"]
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": normalized["job_id"],
        "status": "experimental_fallback",
        "reason": "仅完成规则预布局文件的静态几何统计；未执行智能布线或 KiCad DRC。",
        "generated_at_utc": _utc_now(),
        "metrics": metrics,
        "measurement_status": {
            "routing_completion": "not_measured",
            "drc": "not_run",
            "trace_geometry": "measured",
        },
        "board_observation": observable,
        "candidate_board_path": normalized["board_path"],
        "recommendation": "在独立 PCBWorld 服务产生候选板后运行 KiCad DRC，再与本基线比较。",
    }


def write_json_atomic(path: str, payload: Any) -> str:
    """同目录临时文件 + replace，避免中断留下半个 JSON。"""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = target + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass
    return target


__all__ = [
    "RoutingJobError",
    "SCHEMA_VERSION",
    "DEFAULT_CONSTRAINTS",
    "DEFAULT_POLICY",
    "DEFAULT_REWARD_WEIGHTS",
    "create_job",
    "validate_job",
    "compute_reward",
    "fallback_result",
    "run_rule_baseline",
    "write_json_atomic",
]
