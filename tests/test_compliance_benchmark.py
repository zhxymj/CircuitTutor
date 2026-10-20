# -*- coding: utf-8 -*-
"""A 主线合规评测：固定数据集、指标口径与证据文件回归。"""

import csv
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.compliance_benchmark import (  # noqa: E402
    DEFAULT_SEED,
    build_case_dataset,
    compute_metrics,
    evaluate_dataset,
    write_evidence,
)


def test_metric_formula_and_zero_division():
    records = [
        {"expected_rules": ["R"], "detected_rules": ["R"]},       # TP
        {"expected_rules": ["R"], "detected_rules": []},          # FN
        {"expected_rules": [], "detected_rules": ["R"]},          # FP
        {"expected_rules": [], "detected_rules": []},             # TN
    ]
    metrics = compute_metrics(records, ["R"])
    assert metrics["aggregate"]["tp"] == 1
    assert metrics["aggregate"]["fp"] == 1
    assert metrics["aggregate"]["fn"] == 1
    assert metrics["aggregate"]["tn"] == 1
    assert metrics["aggregate"]["precision"] == 0.5
    assert metrics["aggregate"]["recall"] == 0.5
    assert metrics["aggregate"]["f1"] == 0.5
    assert metrics["aggregate"]["false_positive_rate"] == 0.5

    empty = compute_metrics([], [])
    assert empty["aggregate"]["precision"] is None
    assert empty["aggregate"]["false_positive_rate"] is None


def test_dataset_schema_pairs_and_determinism():
    first = build_case_dataset(seed=DEFAULT_SEED)
    second = build_case_dataset(seed=DEFAULT_SEED)
    assert first == second
    assert first["metadata"]["seed"] == DEFAULT_SEED
    assert first["metadata"]["dataset_kind"] == "constructed_fault_injection"
    assert len(first["cases"]) >= 20

    ids = [case["case_id"] for case in first["cases"]]
    assert len(ids) == len(set(ids))
    required = {
        "case_id", "base_circuit_id", "source", "sample_role",
        "defect_type", "injection_location", "expected_rules",
        "expected_severity", "auto_fixable", "expected_after_repair",
        "circuit",
    }
    assert all(required <= set(case) for case in first["cases"])

    positives = [c for c in first["cases"] if c["sample_role"] == "injected"]
    controls = {c["paired_case_id"] for c in positives}
    assert controls <= set(ids)
    assert all(c["expected_rules"] for c in positives)
    assert all(not c["expected_rules"] for c in first["cases"]
               if c["sample_role"] == "control")

    coverage = {item["defect_type"]: item["status"]
                for item in first["coverage"]}
    assert coverage["I2C缺上拉"] == "implemented"
    assert coverage["总线地址冲突"] == "implemented"
    assert coverage["电源轨不兼容"] == "implemented"


def test_evaluation_is_reproducible_and_traceable():
    dataset = build_case_dataset(seed=DEFAULT_SEED)
    result1 = evaluate_dataset(dataset)
    result2 = evaluate_dataset(dataset)
    assert result1["metrics"] == result2["metrics"]
    assert result1["dataset_fingerprint"] == result2["dataset_fingerprint"]
    assert result1["metrics"]["aggregate"]["tp"] > 0
    assert result1["metrics"]["aggregate"]["tn"] > 0
    # PWR_IN 是负载而不是电源。若把芯片自身供电脚当成供电来源，
    # “接到无源网络但没有电源驱动”的缺陷会被漏掉。
    assert result1["metrics"]["per_rule"]["芯片未供电"]["tp"] == 1
    assert result1["metrics"]["per_rule"]["芯片未供电"]["fn"] == 0
    for rule in ("I2C缺上拉", "总线地址冲突", "电源轨不兼容"):
        assert result1["metrics"]["per_rule"][rule]["tp"] == 1
        assert result1["metrics"]["per_rule"][rule]["fn"] == 0
    assert len(result1["records"]) == len(dataset["cases"])
    assert all("elapsed_ms" in record for record in result1["records"])
    assert result1["run_status"] in {"pass", "fail"}


def test_evidence_files_are_machine_generated_and_consistent():
    with tempfile.TemporaryDirectory() as tmp:
        manifest = write_evidence(tmp, seed=DEFAULT_SEED)
        expected = {
            "compliance_cases.json",
            "compliance_results.json",
            "compliance_summary.csv",
            "project_facts.json",
        }
        assert expected == set(manifest)
        for name in expected:
            assert os.path.isfile(os.path.join(tmp, name)), name

        with open(os.path.join(tmp, "compliance_results.json"), encoding="utf-8") as fh:
            results = json.load(fh)
        with open(os.path.join(tmp, "compliance_cases.json"), encoding="utf-8") as fh:
            cases = json.load(fh)
        assert results["dataset_fingerprint"] == cases["metadata"]["fingerprint"]

        with open(os.path.join(tmp, "compliance_summary.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        aggregate = next(row for row in rows if row["scope"] == "aggregate")
        assert int(aggregate["tp"]) == results["metrics"]["aggregate"]["tp"]
        assert int(aggregate["fp"]) == results["metrics"]["aggregate"]["fp"]


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
