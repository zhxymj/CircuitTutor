"""Evaluate the internal reserved partition, preserving actual model candidates.

Ambiguous/unsupported/contradictory tasks exercise dispatch without model calls.
This partition has been inspected during development; it is not a blind test.
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from core import catalog, clarify, config, llm
from core.validation_gate import validate_candidate
from evaluation.requirements_v2 import fingerprint, load_dataset


def evaluate(output_dir):
    if not config.api_key_configured():
        raise ValueError("Online evaluation requires a locally configured model key")
    dataset = load_dataset()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    records = []
    client = llm.DeepSeekClient(timeout=30, retries=0)
    try:
        for task in (t for t in dataset["tasks"] if t["split"] == "holdout"):
            started = time.perf_counter()
            prompt, task_id = task["prompt"], task["task_id"]
            types = catalog.match_components(prompt)
            brief = llm.build_generation_brief(prompt, catalog.build_matched_lib(types))
            questions = clarify.rule_analyze(prompt)
            disposition = ("reject" if brief["unsupported_part_numbers"] or brief["blockers"] else
                           "clarify" if questions["status"] == "clarify" else "plan")
            row = {"task_id": task_id, "category": task["category"], "prompt": prompt,
                   "expected_disposition": task["expected_disposition"], "disposition": disposition,
                   "preflight": brief, "clarification": questions, "model_calls": 0,
                   "generation_path": "not_requested", "fallback": False,
                   "validation": None, "error": None, "end_to_end_pass": False}
            if disposition == "plan":
                budget = llm.CallBudget(max_calls=5)
                try:
                    candidate = llm.generate_circuit_json(prompt, catalog.build_matched_lib(types), client,
                                                          budget=budget, erc_kb=catalog.build_erc_kb())
                    candidate["provenance"] = {"origin": "prompt", "source_id": task_id, "model_id": client.model}
                    directory = output / task_id
                    directory.mkdir(exist_ok=True)
                    material = json.dumps(candidate, ensure_ascii=False, indent=2)
                    (directory / "candidate.json").write_text(material, encoding="utf-8")
                    present = {p["type"] for p in candidate["parts"]}
                    required = task.get("required_types", [])
                    missing = sorted(set(required) - present)
                    forbidden = sorted(set(task.get("forbidden_types", [])) & present)
                    validation = validate_candidate(candidate, source="ai", name=task_id, work_dir=directory)
                    row.update({"generation_path": "llm", "candidate_path": str(directory / "candidate.json"),
                                "candidate_sha256": hashlib.sha256(material.encode()).hexdigest(),
                                "present_types": sorted(present), "missing_required_types": missing,
                                "forbidden_types_found": forbidden, "validation": validation,
                                "end_to_end_pass": not missing and not forbidden and validation["accepted"]})
                except Exception as error:
                    row["generation_path"] = "error"
                    row["error"] = f"{type(error).__name__}: {error}"[:1000]
                finally:
                    row["model_calls"] = budget.used
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            records.append(row)
            print(f"{task_id}: {disposition} {row['generation_path']} accepted={row['end_to_end_pass']}", flush=True)
            report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "model": client.model,
                      "dataset_sha256": fingerprint(dataset), "split": "holdout",
                      "limitations": ["internal partition inspected during development, not blind", "type coverage is not full requirement fidelity", "no manufacturing acceptance"],
                      "summary": {"tasks": len(records), "model_requested": sum(r["disposition"] == "plan" for r in records),
                                  "llm_completed": sum(r["generation_path"] == "llm" for r in records),
                                  "dispatch_matches": sum(r["disposition"] == r["expected_disposition"] for r in records),
                                  "end_to_end_pass": sum(r["end_to_end_pass"] for r in records), "fallbacks": 0},
                      "records": records}
            (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        client.close()
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="evidence/upgrade/requirements_online")
    evaluate(parser.parse_args().output_dir)
