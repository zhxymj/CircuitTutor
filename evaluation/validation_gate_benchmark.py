"""Run the strict candidate gate on all internal templates and preserve evidence."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from core.templates import BUILTIN_TEMPLATES
from core.validation_gate import validate_candidate
from kicad.export import kicad_cli_version


def run(output_dir):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    records = []
    for key, circuit in BUILTIN_TEMPLATES.items():
        before = time.perf_counter()
        report = validate_candidate(circuit, source="template", name=key,
                                    work_dir=output / "artifacts" / key)
        records.append({"template_id": key,
                        "fixture_role": "intentional_negative" if key == "led_no_limit" else "production_candidate",
                        "circuit_sha256": hashlib.sha256(json.dumps(circuit, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                        "elapsed_ms": round((time.perf_counter() - before) * 1000, 3),
                        "validation": report})
        print(f"{key}: {report['verdict']} {report['blocking_gates']}", flush=True)
    production = [r for r in records if r["fixture_role"] == "production_candidate"]
    result = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "40 internal templates; not independent accuracy or manufacturing approval",
              "kicad_version": kicad_cli_version(),
              "summary": {"templates": len(records), "production_candidates": len(production),
                          "production_accepted": sum(r["validation"]["accepted"] for r in production),
                          "negative_rejected": sum(not r["validation"]["accepted"] for r in records if r not in production),
                          "spice_runtime_passed": sum(r["validation"]["gates"]["spice_runtime"]["status"] == "pass" for r in records),
                          "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)},
              "records": records}
    (output / "validation_gate_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if result["summary"]["production_accepted"] == len(production) and result["summary"]["negative_rejected"] == 1 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="evidence/upgrade/strict_templates")
    raise SystemExit(run(parser.parse_args().output_dir))
