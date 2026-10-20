# -*- coding: utf-8 -*-
"""隔离式 PCB 路由任务合同、奖励函数和失败回退测试。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.routing_job import (  # noqa: E402
    RoutingJobError,
    compute_reward,
    create_job,
    fallback_result,
    run_rule_baseline,
    validate_job,
    write_json_atomic,
)


def test_create_and_validate_contract():
    job = create_job(
        board_path="boards/demo.kicad_pcb",
        output_dir="outputs/demo",
        job_id="demo-001",
        seed=20260919,
    )
    normalized = validate_job(job)
    assert normalized["schema_version"] == "1.0"
    assert normalized["job_id"] == "demo-001"
    assert normalized["constraints"] == {
        "layers": 2,
        "clearance_mm": 0.2,
        "max_vias": 20,
        "max_steps": 500,
        "timeout_s": 300,
    }
    assert normalized["policy"]["name"] == "ppo-small-v1"
    assert normalized["random_seed"] == 20260919


def test_contract_rejects_out_of_scope_and_unsafe_values():
    base = create_job("a.kicad_pcb", "out", job_id="safe-job")
    invalid = []
    for field, value in [
        ("layers", 4),
        ("clearance_mm", 0),
        ("max_vias", -1),
        ("max_steps", 0),
        ("timeout_s", 0),
    ]:
        job = json.loads(json.dumps(base))
        job["constraints"][field] = value
        invalid.append(job)
    bad_path = json.loads(json.dumps(base))
    bad_path["board_path"] = "board.txt"
    invalid.append(bad_path)
    for job in invalid:
        try:
            validate_job(job)
        except RoutingJobError as exc:
            assert exc.errors and all(isinstance(item, str) for item in exc.errors)
        else:
            raise AssertionError(f"非法任务未被拒绝：{job}")


def test_reward_formula_is_explicit_and_deterministic():
    weights = {
        "completion_delta": 10.0,
        "drc_violation": 2.0,
        "trace_length_mm": 0.01,
        "via": 0.5,
        "invalid_action": 1.0,
        "failure": 20.0,
    }
    reward = compute_reward(
        completion_delta=0.5,
        drc_violations=1,
        trace_length_mm=30,
        via_count=2,
        invalid_actions=1,
        failed=False,
        weights=weights,
    )
    assert reward == 0.7  # 5 - 2 - 0.3 - 1 - 1
    assert compute_reward(0, 0, 0, 0, 0, True, weights) == -20.0


def test_fallback_result_has_all_required_metrics_without_fake_measurements():
    job = create_job("missing.kicad_pcb", "out", job_id="missing-board", seed=7)
    result = fallback_result(job, "PCBWorld 服务不可用", elapsed_ms=12.5)
    required = {
        "fully_routed", "net_completion_rate", "drc_violations",
        "total_trace_length_mm", "via_count", "inference_time_ms",
        "fallback_triggered", "model_version", "random_seed",
    }
    assert required <= set(result["metrics"])
    assert result["status"] == "fallback"
    assert result["metrics"]["fallback_triggered"] is True
    assert result["metrics"]["net_completion_rate"] is None
    assert result["metrics"]["drc_violations"] is None
    assert result["reason"] == "PCBWorld 服务不可用"


def test_rule_baseline_parses_only_observable_board_metrics():
    board = """(kicad_pcb
      (version 20240108)
      (net 0 \"\")
      (net 1 \"VCC\")
      (segment (start 0 0) (end 3 4) (width 0.2) (layer \"F.Cu\") (net 1))
      (via (at 3 4) (size 0.8) (drill 0.4) (layers \"F.Cu\" \"B.Cu\") (net 1))
    )"""
    with tempfile.TemporaryDirectory() as tmp:
        board_path = os.path.join(tmp, "demo.kicad_pcb")
        with open(board_path, "w", encoding="utf-8") as handle:
            handle.write(board)
        job = create_job(board_path, os.path.join(tmp, "out"),
                         job_id="baseline", seed=9)
        result = run_rule_baseline(job)
    assert result["status"] == "experimental_fallback"
    assert result["metrics"]["via_count"] == 1
    assert result["metrics"]["total_trace_length_mm"] == 5.0
    assert result["metrics"]["net_completion_rate"] is None
    assert result["metrics"]["drc_violations"] is None
    assert result["measurement_status"]["trace_geometry"] == "measured"
    assert result["measurement_status"]["drc"] == "not_run"


def test_atomic_json_write_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "routing_job.json")
        payload = create_job("demo.kicad_pcb", "out", job_id="roundtrip")
        write_json_atomic(target, payload)
        with open(target, encoding="utf-8") as handle:
            loaded = json.load(handle)
        assert loaded == payload
        assert not os.path.exists(target + ".tmp")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {test.__name__}: {exc}")
    print(f"结果：{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
