"""Capture current environment and source hashes without reading local secrets."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.image_detector import RUNTIME_ROOT as VISION_RUNTIME_ROOT

runner_path = ROOT / "tests" / "run_tests.py"
runner_spec = importlib.util.spec_from_file_location("_local_run_tests", runner_path)
if runner_spec is None or runner_spec.loader is None:
    raise RuntimeError(f"Cannot load local test runner: {runner_path}")
runner = importlib.util.module_from_spec(runner_spec)
sys.modules[runner_spec.name] = runner
runner_spec.loader.exec_module(runner)
SUITES = runner.SUITES
detect_capabilities = runner.detect_capabilities


def capture() -> dict:
    configured_install_root = VISION_RUNTIME_ROOT
    capabilities = detect_capabilities()
    files = []
    for folder in ("core", "kicad", "web", "tests", "evaluation", "tools"):
        for path in sorted((ROOT / folder).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".js", ".mjs", ".html", ".css", ".ps1"}:
                continue
            raw = path.read_bytes()
            files.append({"path": path.relative_to(ROOT).as_posix(), "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()})
    for name in ("server.py", "ai2kicad.py", "VERSION", "data/circuit_templates.json"):
        path = ROOT / name
        raw = path.read_bytes()
        files.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    tool_names = ("KICAD_CLI_PATH", "KICAD_SYMBOL_DIR", "KICAD_FOOTPRINT_DIR",
                  "NGSPICE_PATH", "KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME")
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_kind": "current_working_source; initial evidence remains separately preserved",
        "scope": "5V/3.3V low-voltage MCU and I2C interface boards",
        "python": platform.python_version(), "python_executable": sys.executable,
        "node": shutil.which("node"), "git_repository": (ROOT / ".git").is_dir(),
        "capabilities": capabilities, "registered_suites": len(SUITES),
        "toolchain": {name: os.environ.get(name) for name in tool_names},
        "software_install_root": str(configured_install_root),
        "install_root_present": configured_install_root.exists(),
        "current_tool_capabilities": capabilities,
        "secret_files_read": False, "source_files": files,
    }


if __name__ == "__main__":
    output = ROOT / "evidence/upgrade/preflight_current.json"
    output.write_text(json.dumps(capture(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
