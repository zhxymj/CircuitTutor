"""Live HTTP manufacturing acceptance for the frozen 3.3 V active board."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server
from core.pcb_real import _a, _children, _one, _value, dump_sexpr, parse_sexpr

ACTIVE = ROOT / "data/pcb_active/run_e34bf9d7ac6c45be85d4fb00b97e1fec"
RESULTS = ROOT / "evidence/upgrade/manufacturing_http/results.json"
CURRENT = RESULTS.parent / "current"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def active_negative_cases(circuit: dict, board_text: str) -> dict[str, dict]:
    """Mutate independent copies, never the reviewed board or its manifest."""
    original = parse_sexpr(board_text)
    unrouted = copy.deepcopy(original)
    unrouted[:] = [item for item in unrouted
                   if not (isinstance(item, list) and item and _value(item[0]) in ("segment", "via", "zone"))]
    shorted = copy.deepcopy(original)
    header = next(fp for fp in _children(shorted, "footprint")
                  if any(_value(p[1]) == "Reference" and _value(p[2]) == "J1"
                         for p in _children(fp, "property")))
    header_at = _one(header, "at")
    if header_at is None or len(header_at) != 3:
        raise ValueError("J1 must have unrotated board coordinates")
    pads = {_value(p[1]): p for p in _children(header, "pad")}
    p1, p2 = pads["1"], pads["2"]
    n1, n2 = _one(p1, "net"), _one(p2, "net")
    if n1 is None or n2 is None or _value(n1[-1]) == _value(n2[-1]):
        raise ValueError("J1 pins 1 and 2 must belong to distinct nets")

    def center(pad):
        at = _one(pad, "at")
        return [_a(float(_value(header_at[i])) + float(_value(at[i]))) for i in (1, 2)]

    shorted.append([_a("segment"), [_a("start"), *center(p1)], [_a("end"), *center(p2)],
                    [_a("width"), _a("0.25")], [_a("layer"), _a("F.Cu", True)],
                    copy.deepcopy(n1),
                    [_a("uuid"), _a(str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                 "active-http-j1-short-" + _sha(board_text.encode()))), True)]])
    missing = copy.deepcopy(circuit)
    missing["parts"] = [p for p in missing["parts"] if p["id"] != "GND1"]
    missing["connections"] = [edge for edge in missing["connections"]
                              if not any(endpoint.startswith("GND1:") for endpoint in edge)]
    return {
        "unrouted": {"circuit": copy.deepcopy(circuit), "board_text": dump_sexpr(unrouted)},
        "deliberate_short": {"circuit": copy.deepcopy(circuit), "board_text": dump_sexpr(shorted)},
        "missing_common_ground": {"circuit": missing, "board_text": board_text},
    }


def _post(port: int, circuit: dict, board_text: str):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/pcb/manufacture",
                                     data=json.dumps({"circuit": circuit, "board_text": board_text},
                                                     ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=240)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.code, response.read()


def _record(name: str, case: dict, expected: int, port: int, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    circuit = case["circuit"]
    board_text = case["board_text"]
    (folder / "circuit.json").write_text(json.dumps(circuit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    board_payload = board_text.encode("utf-8")
    (folder / "candidate.kicad_pcb").write_bytes(board_payload)
    record = {"name": name, "expected_status": expected, "status": None, "passed": False,
              "input": {"circuit": str(folder / "circuit.json"), "board": str(folder / "candidate.kicad_pcb"),
                        "circuit_sha256": _sha((folder / "circuit.json").read_bytes()),
                        "board_sha256": _sha(board_payload),
                        "board_fixture_sha256": _sha((folder / "candidate.kicad_pcb").read_bytes())}}
    runtime_root = CURRENT / "data/pcb_runs"
    before = {p.name for p in runtime_root.iterdir() if p.is_dir()} if runtime_root.is_dir() else set()
    try:
        status, blob = _post(port, circuit, board_text)
        created = [p for p in runtime_root.iterdir() if p.is_dir() and p.name not in before]
        record["runtime_case_dirs"] = [str(p) for p in created]
        record["status"] = status
        if status == 200:
            bundle = folder / "http_fabrication_candidate_for_review.zip"
            bundle.write_bytes(blob)
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = archive.namelist()
                manifest = json.loads(archive.read("manufacturing_manifest.json"))
                validation = json.loads(archive.read("source_validation.json"))
            record.update({"bundle": str(bundle), "bundle_sha256": _sha(blob), "files": names,
                           "manifest": manifest, "source_validation": validation})
            checks = {"http_status": status == expected,
                      "official_source_erc": validation["gates"]["official_erc"]["status"] == "pass" and manifest["source_erc"]["count"] == 0,
                      "official_source_netlist": validation["gates"]["official_netlist"]["status"] == "pass" and manifest["source_netlist"]["status"] == "pass",
                      "official_drc": manifest["drc"]["status"] == "pass" and manifest["drc"]["official_schematic_parity_checked"] is True and all(v == 0 for v in manifest["drc"]["counts"].values()),
                      "candidate_only": manifest["status"] == "fabrication_candidate_for_review" and manifest["manufacturing_release"] is False and set(manifest["review_required"]) == {"electronic_engineer", "fabricator"},
                      "cam_present": len([n for n in names if n.startswith("cam/")]) == 12}
        else:
            response = json.loads(blob)
            (folder / "response.json").write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            record["response"] = response
            if name == "missing_common_ground":
                validation = response.get("validation", {})
                record["downstream_manufacturing_drc"] = {
                    "status": "incomplete",
                    "reason": "blocked_by_source_rejection",
                    "counts": None,
                }
                checks = {"http_status": status == expected,
                          "source_rejected": response.get("status") == "source_rejected" and validation.get("accepted") is False,
                          "official_erc_failure": validation.get("gates", {}).get("official_erc", {}).get("status") == "fail",
                          "no_cam": len(created) == 1 and not any(created[0].rglob("cam")) and not any(created[0].rglob("fabrication_candidate.zip"))}
            else:
                manifest = response.get("manifest", {})
                drc = manifest.get("drc", {})
                counts = drc.get("counts") or {}
                checks = {"http_status": status == expected,
                          "manufacturing_gate": manifest.get("status") == "drc_failed" and manifest.get("failure", {}).get("code") == "drc_failed",
                          "official_drc_executed": drc.get("official_schematic_parity_checked") is True and any((c.get("argv") or [])[1:3] == ["pcb", "drc"] and c.get("exit_code") is not None for c in manifest.get("commands", [])),
                          "intended_fault_observed": counts.get("unconnected_items", 0) > 0 if name == "unrouted" else counts.get("violations", 0) > 0,
                          "no_cam": not manifest.get("bundle") and not (Path(manifest["run_dir"]) / "cam").exists() and not (Path(manifest["run_dir"]) / "fabrication_candidate.zip").exists()}
        record["checks"] = checks
        record["passed"] = all(checks.values())
    except Exception as exc:
        record["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    return record


def verify_retained_current(report: dict) -> dict:
    """Recheck real server output and archive bytes without another HTTP run."""
    runtime_root = CURRENT / "data/pcb_runs"
    records = {record["name"]: record for record in report["records"]}
    known = {Path((record.get("manifest") or record.get("response", {}).get("manifest"))["run_dir"]).parent.resolve()
             for record in report["records"] if record["name"] != "missing_common_ground"}
    missing_record = records["missing_common_ground"]
    if missing_record.get("runtime_case_dirs"):
        missing_dirs = [Path(path).resolve() for path in missing_record["runtime_case_dirs"]]
    else:
        missing_dirs = [p.resolve() for p in runtime_root.iterdir() if p.is_dir() and p.resolve() not in known]
    for record in report["records"]:
        name = record["name"]
        manifest = record.get("manifest") or record.get("response", {}).get("manifest")
        case_dir = missing_dirs[0] if name == "missing_common_ground" and len(missing_dirs) == 1 else (
            Path(manifest["run_dir"]).parent if manifest else None)
        checks = {"one_runtime_case_dir": case_dir is not None and case_dir.is_dir()}
        if case_dir is None or not case_dir.is_dir():
            record["retained_verification"] = checks
            record["passed"] = False
            continue
        fixture = Path(record["input"]["board"])
        checks["board_fixture_matches_http_payload"] = fixture.is_file() and _sha(fixture.read_bytes()) == record["input"]["board_sha256"]
        record["input"]["board_fixture_sha256"] = _sha(fixture.read_bytes()) if fixture.is_file() else None
        source_file = case_dir / "source_validation.json"
        checks["source_validation_retained"] = source_file.is_file()
        if source_file.is_file():
            validation = json.loads(source_file.read_text(encoding="utf-8"))
            record["retained_source_validation"] = {"path": str(source_file), "sha256": _sha(source_file.read_bytes()),
                                                     "accepted": validation.get("accepted"),
                                                     "gates": {k: v.get("status") for k, v in validation.get("gates", {}).items()}}
        if name == "active_review_candidate":
            run_dir = Path(manifest["run_dir"])
            cam = run_dir / "cam"
            artifacts = manifest.get("artifacts", [])
            files = [p for p in cam.iterdir() if p.is_file()] if cam.is_dir() else []
            checks["cam_12_of_12"] = len(artifacts) == len(files) == 12
            bundle = Path(record["bundle"])
            checks["http_archive_sha256"] = bundle.is_file() and _sha(bundle.read_bytes()) == record["bundle_sha256"]
            if bundle.is_file():
                with zipfile.ZipFile(bundle) as archive:
                    cam_names = {name for name in archive.namelist() if name.startswith("cam/")}
                    expected_names = {"cam/" + Path(item["path"]).name for item in artifacts}
                    checks["archive_cam_12_of_12"] = len(cam_names) == 12 and cam_names == expected_names
                    checks["all_cam_sha256"] = all(
                        Path(item["path"]).is_file()
                        and Path(item["path"]).resolve().parent == cam.resolve()
                        and _sha(Path(item["path"]).read_bytes()) == item["sha256"]
                        and _sha(archive.read("cam/" + Path(item["path"]).name)) == item["sha256"]
                        and Path(item["path"]).stat().st_size == item["bytes"]
                        for item in artifacts)
            else:
                checks["archive_cam_12_of_12"] = False
                checks["all_cam_sha256"] = False
            checks["official_reports_retained"] = all((run_dir / filename).is_file()
                                                       for filename in ("source-erc.json", "source.net", "drc.json"))
        elif name in ("unrouted", "deliberate_short"):
            run_dir = Path(manifest["run_dir"])
            checks["no_cam_on_disk"] = not (run_dir / "cam").exists() and not (run_dir / "fabrication_candidate.zip").exists()
            checks["official_reports_retained"] = all((run_dir / filename).is_file()
                                                       for filename in ("source-erc.json", "source.net", "drc.json"))
            checks["official_drc_executed"] = any((cmd.get("argv") or [])[1:3] == ["pcb", "drc"]
                                                  and cmd.get("exit_code") is not None
                                                  for cmd in manifest.get("commands", []))
        else:
            raw_erc = case_dir / "source/source.erc.json"
            checks["no_manufacturing_run_or_cam"] = not any(case_dir.glob("run_*")) and not any(case_dir.rglob("cam"))
            checks["official_erc_report_retained"] = raw_erc.is_file()
            checks["official_erc_rejected"] = (validation.get("gates", {}).get("official_erc", {}).get("status") == "fail"
                                               if source_file.is_file() else False)
            if raw_erc.is_file():
                record["official_erc_raw"] = {"path": str(raw_erc), "sha256": _sha(raw_erc.read_bytes())}
        record["runtime_case_dir"] = str(case_dir)
        record["retained_verification"] = checks
        record["passed"] = all(record.get("checks", {}).values()) and all(checks.values())
    report["passed"] = sum(bool(r["passed"]) for r in report["records"])
    report["failed"] = report["case_denominator"] - report["passed"]
    report["checker_revision"] = "runtime-cam-and-input-sha256-v3"
    report["verified_at"] = datetime.now(timezone.utc).isoformat()
    circuit = json.loads(Path(report["input_source"]["circuit"]).read_text(encoding="utf-8"))
    report["input_origin_and_permission"] = {
        "provenance": circuit.get("provenance"),
        "classification": circuit.get("engineering_example", {}).get("classification"),
        "external_design_sample": circuit.get("engineering_example", {}).get("independent_external_sample"),
        "use_basis": "project-internal constructed engineering example for local candidate validation",
        "component_reference_sources": circuit.get("engineering_example", {}).get("sources", []),
    }
    return report


def repair_existing_board_fixture_line_endings(history: dict) -> None:
    """Record and repair the two CRLF-translated source-board copies only."""
    report = history["current"]
    source_bytes = (ACTIVE / "candidate.kicad_pcb").read_bytes()
    source_hash = _sha(source_bytes)
    repairs = []
    for record in report["records"]:
        if record["name"] not in ("active_review_candidate", "missing_common_ground"):
            continue
        fixture = Path(record["input"]["board"])
        before = _sha(fixture.read_bytes())
        if record["input"]["board_sha256"] != source_hash:
            raise ValueError(f"Recorded HTTP input hash disagrees with frozen board: {record['name']}")
        if before == source_hash:
            continue
        fixture.write_bytes(source_bytes)
        after = _sha(fixture.read_bytes())
        if after != source_hash:
            raise ValueError(f"Fixture byte repair failed: {record['name']}")
        repairs.append({"name": record["name"], "path": str(fixture),
                        "old_sha256": before, "new_sha256": after,
                        "reason": "Windows write_text converted source LF/CRLF line endings; HTTP sent unchanged UTF-8 board_text bytes"})
    if repairs:
        report["case_fixture_line_ending_repair"] = {
            "repaired_at": datetime.now(timezone.utc).isoformat(), "records": repairs,
            "source_board_sha256": source_hash}


def main() -> None:
    circuit_bytes = (ACTIVE / "circuit.json").read_bytes()
    board_bytes = (ACTIVE / "candidate.kicad_pcb").read_bytes()
    circuit = json.loads(circuit_bytes)
    board_text = board_bytes.decode("utf-8")
    cases = {"active_review_candidate": {"circuit": circuit, "board_text": board_text},
             **active_negative_cases(circuit, board_text)}
    history = json.loads(RESULTS.read_text(encoding="utf-8"))
    CURRENT.mkdir(parents=True, exist_ok=True)
    original_root = server.ROOT
    server.ROOT = str(CURRENT)  # Only the HTTP handler's output directory changes.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "mock": False,
              "scenario": "frozen internally authored 3.3V ATmega328P plus SHT4x active candidate",
              "classification": "internal_active_engineering_example", "sample_denominator": 1,
              "case_denominator": 4, "manufacturing_release": False,
              "review_required": ["electronic_engineer", "fabricator"],
              "physical_fabrication_or_power_on_test": False,
              "input_source": {"circuit": str(ACTIVE / "circuit.json"), "board": str(ACTIVE / "candidate.kicad_pcb"),
                               "manifest": str(ACTIVE / "manifest.json"), "circuit_sha256": _sha(circuit_bytes),
                               "board_sha256": _sha(board_bytes)},
              "runtime_output_root": str(CURRENT / "data/pcb_runs"), "records": []}
    try:
        for name, expected in (("active_review_candidate", 200), ("unrouted", 422),
                               ("deliberate_short", 422), ("missing_common_ground", 422)):
            record = _record(name, cases[name], expected, httpd.server_port, CURRENT / "cases" / name)
            report["records"].append(record)
            print(json.dumps({"case": name, "status": record["status"], "passed": record["passed"],
                              "checks": record.get("checks"), "failure": record.get("failure")}, ensure_ascii=False))
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.ROOT = original_root
        verify_retained_current(report)
        history["current"] = report
        RESULTS.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["failed"]:
        raise SystemExit(f"Current active manufacturing HTTP acceptance failed: {report['passed']}/{report['case_denominator']}")


if __name__ == "__main__":
    if sys.argv[1:] in (["--verify-existing"], ["--repair-board-fixtures"]):
        history = json.loads(RESULTS.read_text(encoding="utf-8"))
        if sys.argv[1:] == ["--repair-board-fixtures"]:
            repair_existing_board_fixture_line_endings(history)
        verify_retained_current(history["current"])
        RESULTS.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": history["current"]["passed"],
                          "total": history["current"]["case_denominator"]}, ensure_ascii=False))
        if history["current"]["failed"]:
            raise SystemExit(1)
    else:
        main()
