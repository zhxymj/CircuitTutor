# -*- coding: utf-8 -*-
"""Build, verify, and atomically promote a quality-gated release archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.version import read_version  # noqa: E402


MANIFEST_REL = Path("evidence/artifact_manifest.json")
VALIDATION_REL = Path("evidence/validation_results.json")
AUDIT_REL = Path("evidence/audit_report.json")
FORMAL_EVIDENCE = (VALIDATION_REL, AUDIT_REL, MANIFEST_REL)
REQUIRED_ARCHIVE_FILES = frozenset({"VERSION", "README.md", "server.py"})
EXCLUSIONS = [
    "__pycache__、.pytest_cache、*.pyc 与 *.pyo",
    "运行日志、临时文件、partial 文件与 demo_out",
    "data/history 与可再生批量导入草稿",
    "data/external_references/fetch_cache 上游下载缓存（保留有来源和许可证的选定原文件）",
    "docs/superpowers 内部规格与工作计划",
    "可再生测试清单、ERC 结果与 render_*.svg/symbols_preview.svg",
    "evidence/_audit_test_temp.json",
    ".env、.env.local 与其他本机敏感配置（保留 .env.template）",
]

EvidenceOverride = Path | bytes
GateRunner = Callable[[Path, Path, Path], object]
Verifier = Callable[[Path, dict], object]
Promoter = Callable[[Path, Path], object]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _load_json_bytes(data: bytes, label: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}不是有效的 UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}顶层必须是 JSON 对象")
    return value


def is_excluded(relative: Path) -> bool:
    posix = relative.as_posix()
    lower = posix.lower()
    parts_lower = tuple(part.lower() for part in relative.parts)
    name_lower = relative.name.lower()

    if any(part in {"__pycache__", ".pytest_cache", "demo_out"} for part in parts_lower):
        return True
    if relative.suffix.lower() in {".pyc", ".pyo", ".log", ".tmp", ".partial"}:
        return True
    if name_lower in {".env", ".env.local"} or (
        name_lower.startswith(".env.") and name_lower != ".env.template"
    ):
        return True
    if lower.startswith("data/history/") or lower == "data/history":
        return True
    if lower.startswith("data/external_references/fetch_cache/") or lower == "data/external_references/fetch_cache":
        return True
    if lower == "data/kicad_batch_draft.json":
        return True
    if posix == "docs/官方库批量导入草稿.md":
        return True
    if lower.startswith("docs/superpowers/") or lower == "docs/superpowers":
        return True
    if lower == "evidence/_audit_test_temp.json":
        return True
    if lower in {"tests/erc_results.json", "tests/frontend_manifest.json"}:
        return True
    if lower == "tests/symbols_preview.svg":
        return True
    if lower.startswith("tests/render_") and lower.endswith(".svg"):
        return True
    if lower.startswith("tests/") and any(
        part in {"output", "outputs", "rendered", "artifacts", "tmp", "temp"}
        for part in parts_lower
    ):
        return True
    return False


def _normalise_overrides(
    evidence_overrides: Mapping[str | Path, EvidenceOverride] | None,
) -> dict[Path, EvidenceOverride]:
    result: dict[Path, EvidenceOverride] = {}
    for raw_relative, source in (evidence_overrides or {}).items():
        relative = Path(raw_relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"证据覆盖路径必须是安全的项目相对路径：{relative}")
        result[relative] = source
    return result


def _source_bytes(
    root: Path,
    relative: Path,
    evidence_overrides: Mapping[Path, EvidenceOverride],
) -> bytes:
    source = evidence_overrides.get(relative)
    if isinstance(source, bytes):
        return source
    if source is not None:
        return Path(source).read_bytes()
    return (root / relative).read_bytes()


def package_files(
    root: Path,
    evidence_overrides: Mapping[str | Path, EvidenceOverride] | None = None,
) -> list[Path]:
    root = root.resolve()
    overrides = _normalise_overrides(evidence_overrides)
    files: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative == MANIFEST_REL or is_excluded(relative):
            continue
        files.add(relative)
    for relative in overrides:
        if relative != MANIFEST_REL and not is_excluded(relative):
            files.add(relative)
    return sorted(files, key=lambda item: item.as_posix())


def _validate_quality_reports(validation: dict, audit: dict) -> None:
    summary = validation.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError("验证报告缺少 summary")
    if summary.get("status") != "pass" or summary.get("fail") != 0 or summary.get("skip") != 0:
        raise RuntimeError("完整回归未达到 pass/fail=0/skip=0")
    kicad = validation.get("kicad_official_validation")
    if not isinstance(kicad, dict) or kicad.get("enabled") is not True:
        raise RuntimeError("KiCad 官方校验未启用")
    capabilities = validation.get("capabilities")
    if isinstance(capabilities, dict) and (
        capabilities.get("kicad_cli") is not True
        or capabilities.get("kicad_symbols") is not True
    ):
        raise RuntimeError("KiCad CLI 或官方符号库能力不可用")

    required_audit = {
        "schema_version",
        "status",
        "severity_counts",
        "assertions",
        "metrics",
        "findings",
    }
    if not required_audit.issubset(audit):
        raise RuntimeError("审计报告缺少必需字段")
    severity = audit.get("severity_counts")
    if not isinstance(severity, dict):
        raise RuntimeError("审计报告 severity_counts 无效")
    if audit.get("status") != "pass" or severity.get("P0") != 0 or severity.get("P1") != 0:
        raise RuntimeError("审计报告包含 P0/P1 或状态非 pass")
    assertions = audit.get("assertions")
    if not isinstance(assertions, dict) or assertions.get("fail") != 0:
        raise RuntimeError("审计断言存在失败")

    embedded_audit = validation.get("quality_audit")
    if isinstance(embedded_audit, dict) and embedded_audit.get("selected") is True:
        if embedded_audit.get("status") != "pass":
            raise RuntimeError("验证报告中的质量审计未通过")
        embedded_severity = embedded_audit.get("severity_counts")
        if isinstance(embedded_severity, dict) and (
            embedded_severity.get("P0") != 0 or embedded_severity.get("P1") != 0
        ):
            raise RuntimeError("验证报告中的质量审计包含 P0/P1")


def run_release_gate(root: Path, validation_out: Path, audit_out: Path) -> dict:
    """Run the mandatory all-profile regression and validate both reports."""

    root = Path(root).resolve()
    validation_out = Path(validation_out).resolve()
    audit_out = Path(audit_out).resolve()
    validation_out.parent.mkdir(parents=True, exist_ok=True)
    audit_out.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-B",
        str(root / "tests" / "run_tests.py"),
        "--profile",
        "all",
        "--require-kicad",
        "--json-out",
        str(validation_out),
        "--audit-json-out",
        str(audit_out),
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"正式发布门禁失败（退出码 {completed.returncode}）")
    if not validation_out.is_file() or not audit_out.is_file():
        raise RuntimeError("正式发布门禁未生成完整验证/审计报告")
    validation = _load_json_bytes(validation_out.read_bytes(), "验证报告")
    audit = _load_json_bytes(audit_out.read_bytes(), "审计报告")
    _validate_quality_reports(validation, audit)
    return {
        "status": "pass",
        "validation": validation,
        "audit": audit,
        "command": command,
    }


def make_manifest(
    root: Path,
    files: list[Path],
    evidence_overrides: Mapping[str | Path, EvidenceOverride],
) -> dict:
    """Build schema-1.1 manifest in memory, bound to validated evidence bytes."""

    root = Path(root).resolve()
    overrides = _normalise_overrides(evidence_overrides)
    if VALIDATION_REL not in overrides or AUDIT_REL not in overrides:
        raise RuntimeError("发布清单必须使用本次门禁生成的验证与审计证据")
    validation_bytes = _source_bytes(root, VALIDATION_REL, overrides)
    audit_bytes = _source_bytes(root, AUDIT_REL, overrides)
    validation = _load_json_bytes(validation_bytes, "验证报告")
    audit = _load_json_bytes(audit_bytes, "审计报告")
    _validate_quality_reports(validation, audit)

    entries = []
    total_bytes = 0
    for relative in sorted(files, key=lambda item: item.as_posix()):
        data = _source_bytes(root, relative, overrides)
        total_bytes += len(data)
        entries.append(
            {
                "path": relative.as_posix(),
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    entry_digest_input = "".join(
        f"{item['path']}\0{item['size_bytes']}\0{item['sha256']}\n" for item in entries
    ).encode("utf-8")
    entry_digest = sha256_bytes(entry_digest_input)
    generated_at = datetime.now(timezone.utc)
    evidence_hashes = {
        VALIDATION_REL.as_posix(): sha256_bytes(validation_bytes),
        AUDIT_REL.as_posix(): sha256_bytes(audit_bytes),
    }
    return {
        "schema_version": "1.1",
        "product_version": read_version(root),
        "build_id": f"{generated_at.strftime('%Y%m%dT%H%M%SZ')}-{entry_digest[:12]}",
        "generated_at": generated_at.isoformat(),
        "package_root": root.name,
        "manifest_self_excluded_from_hash_list": True,
        "quality_gate": {
            "status": "pass",
            "validation_report": VALIDATION_REL.as_posix(),
            "audit_report": AUDIT_REL.as_posix(),
            "evidence_sha256": evidence_hashes,
            "kicad_required": True,
        },
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "exclusions": EXCLUSIONS,
        "entries": entries,
    }


def _checked_entry_bytes(
    root: Path,
    entry: dict,
    overrides: Mapping[Path, EvidenceOverride],
) -> bytes:
    relative = Path(entry["path"])
    data = _source_bytes(root, relative, overrides)
    if len(data) != entry.get("size_bytes") or sha256_bytes(data) != entry.get("sha256"):
        raise RuntimeError(f"归档前源文件发生变化：{relative.as_posix()}")
    return data


def write_candidate_archive(
    root: Path,
    candidate: Path,
    manifest: dict,
    archive_format: str,
    evidence_overrides: Mapping[str | Path, EvidenceOverride],
) -> None:
    """Write a ZIP or tar.gz candidate without touching the formal output."""

    root = Path(root).resolve()
    candidate = Path(candidate)
    overrides = _normalise_overrides(evidence_overrides)
    prefix = manifest["package_root"] + "/"
    manifest_data = _json_bytes(manifest)
    if archive_format == "zip":
        with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry in manifest["entries"]:
                data = _checked_entry_bytes(root, entry, overrides)
                archive.writestr(prefix + entry["path"], data)
            archive.writestr(prefix + MANIFEST_REL.as_posix(), manifest_data)
        return
    if archive_format == "tar.gz":
        with tarfile.open(candidate, "w:gz") as archive:
            for entry in manifest["entries"]:
                data = _checked_entry_bytes(root, entry, overrides)
                info = tarfile.TarInfo(prefix + entry["path"])
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo(prefix + MANIFEST_REL.as_posix())
            info.size = len(manifest_data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(manifest_data))
        return
    raise ValueError(f"不支持的发布包格式：{archive_format}")


def _read_archive_entries(archive_path: Path) -> tuple[dict[str, bytes], list[str]]:
    archive_path = Path(archive_path)
    entries: dict[str, bytes] = {}
    ordered_names: list[str] = []
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                ordered_names.append(info.filename)
                entries[info.filename] = archive.read(info)
        return entries, ordered_names
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError(f"无法读取归档条目：{member.name}")
                ordered_names.append(member.name)
                entries[member.name] = extracted.read()
        return entries, ordered_names
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError("发布包既不是有效 ZIP，也不是有效 tar.gz") from exc


def _validate_archive_name(name: str) -> None:
    path = Path(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"发布包含不安全路径：{name}")


def verify_archive_from_disk(archive_path: Path) -> dict:
    """Independently verify an archive using only its embedded manifest."""

    archive_path = Path(archive_path).resolve()
    entries, ordered_names = _read_archive_entries(archive_path)
    if len(ordered_names) != len(set(ordered_names)):
        raise RuntimeError("发布包包含重复条目")
    for name in ordered_names:
        _validate_archive_name(name)
    manifest_suffix = "/" + MANIFEST_REL.as_posix()
    manifest_names = [name for name in ordered_names if name.endswith(manifest_suffix)]
    if len(manifest_names) != 1:
        raise RuntimeError("发布包必须且只能包含一份 artifact_manifest.json")
    manifest_name = manifest_names[0]
    manifest = _load_json_bytes(entries[manifest_name], "artifact_manifest.json")
    if manifest.get("schema_version") != "1.1":
        raise RuntimeError("发布清单 schema_version 必须为 1.1")
    package_root = manifest.get("package_root")
    if not isinstance(package_root, str) or not package_root:
        raise RuntimeError("发布清单缺少 package_root")
    prefix = package_root + "/"
    if manifest_name != prefix + MANIFEST_REL.as_posix():
        raise RuntimeError("发布清单不在唯一包根目录内")

    raw_expected = manifest.get("entries")
    if not isinstance(raw_expected, list):
        raise RuntimeError("发布清单 entries 无效")
    expected: dict[str, dict] = {}
    for entry in raw_expected:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuntimeError("发布清单包含无效条目")
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative == MANIFEST_REL:
            raise RuntimeError(f"发布清单包含不安全条目：{entry.get('path')}")
        if entry["path"] in expected:
            raise RuntimeError(f"发布清单包含重复条目：{entry['path']}")
        expected[entry["path"]] = entry

    expected_archive_names = {prefix + path for path in expected}
    expected_archive_names.add(manifest_name)
    if set(ordered_names) != expected_archive_names:
        raise RuntimeError("发布包条目集合与清单不一致")
    total_bytes = 0
    for relative, entry in expected.items():
        data = entries[prefix + relative]
        total_bytes += len(data)
        if len(data) != entry.get("size_bytes"):
            raise RuntimeError(f"文件大小不一致：{relative}")
        if sha256_bytes(data) != entry.get("sha256"):
            raise RuntimeError(f"SHA-256 不一致：{relative}")
    if manifest.get("entry_count") != len(expected):
        raise RuntimeError("发布清单 entry_count 不一致")
    if manifest.get("total_bytes") != total_bytes:
        raise RuntimeError("发布清单 total_bytes 不一致")
    missing_required = sorted(REQUIRED_ARCHIVE_FILES - set(expected))
    if missing_required:
        raise RuntimeError("发布包缺少必需运行文件：" + ", ".join(missing_required))

    version_bytes = entries[prefix + "VERSION"]
    try:
        archived_version = version_bytes.decode("utf-8").strip()
    except UnicodeError as exc:
        raise RuntimeError("归档 VERSION 不是 UTF-8") from exc
    if manifest.get("product_version") != archived_version:
        raise RuntimeError("发布清单 product_version 与归档 VERSION 不一致")
    if not isinstance(manifest.get("build_id"), str) or not manifest["build_id"]:
        raise RuntimeError("发布清单缺少 build_id")

    quality = manifest.get("quality_gate")
    if not isinstance(quality, dict) or quality.get("status") != "pass":
        raise RuntimeError("发布清单质量门禁状态无效")
    if quality.get("kicad_required") is not True:
        raise RuntimeError("发布清单未绑定 KiCad 必需校验")
    evidence_hashes = quality.get("evidence_sha256")
    if not isinstance(evidence_hashes, dict):
        raise RuntimeError("发布清单缺少质量证据哈希")
    for relative in (VALIDATION_REL.as_posix(), AUDIT_REL.as_posix()):
        if relative not in expected:
            raise RuntimeError(f"发布包缺少质量证据：{relative}")
        actual_hash = sha256_bytes(entries[prefix + relative])
        if evidence_hashes.get(relative) != actual_hash:
            raise RuntimeError(f"质量证据 SHA-256 不一致：{relative}")

    validation = _load_json_bytes(entries[prefix + VALIDATION_REL.as_posix()], "验证报告")
    audit = _load_json_bytes(entries[prefix + AUDIT_REL.as_posix()], "审计报告")
    _validate_quality_reports(validation, audit)
    return manifest


def verify_archive(candidate: Path, manifest: dict) -> dict:
    """Verify a candidate and ensure its embedded manifest is the expected one."""

    embedded = verify_archive_from_disk(candidate)
    if embedded != manifest:
        raise RuntimeError("候选包内发布清单与构建清单不一致")
    return embedded


def _archive_format(output: Path) -> str:
    lower = output.name.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "tar.gz"
    raise ValueError("发布包扩展名必须是 .zip、.tar.gz 或 .tgz")


def default_output_path(root: Path) -> Path:
    root = Path(root).resolve()
    return root.parent / f"{root.name}-{read_version(root)}.zip"


def _output_is_inside_root(root: Path, output: Path) -> bool:
    try:
        output.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".partial")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _capture_evidence(root: Path) -> dict[Path, bytes | None]:
    return {
        relative: (root / relative).read_bytes() if (root / relative).is_file() else None
        for relative in FORMAL_EVIDENCE
    }


def _restore_evidence(root: Path, captured: Mapping[Path, bytes | None]) -> None:
    errors: list[str] = []
    for relative, old_bytes in captured.items():
        path = root / relative
        try:
            if old_bytes is None:
                if path.exists():
                    path.unlink()
            else:
                _atomic_write_bytes(path, old_bytes)
        except OSError as exc:
            errors.append(f"{relative.as_posix()}: {exc}")
    if errors:
        raise RuntimeError("恢复旧发布证据失败：" + "; ".join(errors))


def _gate_result_is_pass(result: object) -> bool:
    return isinstance(result, dict) and result.get("status") == "pass"


def promote_archive(
    root: Path,
    output: Path,
    gate_runner: GateRunner = run_release_gate,
    verifier: Verifier = verify_archive,
    promoter: Promoter = os.replace,
) -> dict:
    """Gate, build, verify, then atomically promote archive and formal evidence."""

    root = Path(root).resolve()
    output = Path(output).resolve()
    if _output_is_inside_root(root, output):
        raise ValueError("正式发布包不得位于项目根目录内")
    archive_format = _archive_format(output)

    with tempfile.TemporaryDirectory(prefix="zhihui-release-gate-") as gate_dir_name:
        gate_dir = Path(gate_dir_name)
        validation_out = gate_dir / "validation_results.json"
        audit_out = gate_dir / "audit_report.json"
        gate_result = gate_runner(root, validation_out, audit_out)
        if not _gate_result_is_pass(gate_result):
            raise RuntimeError("正式发布门禁返回非 pass 状态")
        if not validation_out.is_file() or not audit_out.is_file():
            raise RuntimeError("正式发布门禁未生成完整验证/审计报告")
        validation = _load_json_bytes(validation_out.read_bytes(), "验证报告")
        audit = _load_json_bytes(audit_out.read_bytes(), "审计报告")
        _validate_quality_reports(validation, audit)
        evidence_overrides: dict[Path, EvidenceOverride] = {
            VALIDATION_REL: validation_out,
            AUDIT_REL: audit_out,
        }
        files = package_files(root, evidence_overrides)
        manifest = make_manifest(root, files, evidence_overrides)

        output.parent.mkdir(parents=True, exist_ok=True)
        fd, candidate_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=output.name + ".",
            suffix=".partial",
        )
        os.close(fd)
        candidate = Path(candidate_name)
        try:
            write_candidate_archive(
                root,
                candidate,
                manifest,
                archive_format,
                evidence_overrides,
            )
            verifier(candidate, manifest)
            captured = _capture_evidence(root)
            try:
                _atomic_write_bytes(root / VALIDATION_REL, validation_out.read_bytes())
                _atomic_write_bytes(root / AUDIT_REL, audit_out.read_bytes())
                _atomic_write_bytes(root / MANIFEST_REL, _json_bytes(manifest))
                promoter(candidate, output)
            except BaseException:
                _restore_evidence(root, captured)
                raise
            return manifest
        finally:
            if candidate.exists():
                candidate.unlink()


def build_archive(root: Path, output: Path) -> dict:
    """Backward-compatible public entry that still enforces the real gate."""

    return promote_archive(root, output)


def main() -> None:
    parser = argparse.ArgumentParser(description="构建或独立验证智绘电路正式发布包")
    parser.add_argument("root", nargs="?", help="项目根目录；默认使用脚本所在项目")
    parser.add_argument("output", nargs="?", help="输出 .zip/.tar.gz；默认使用版本化 ZIP")
    parser.add_argument(
        "--verify-only",
        metavar="ARCHIVE",
        help="只按包内清单校验已有 ZIP/tar.gz，不运行构建门禁",
    )
    args = parser.parse_args()

    if args.verify_only:
        if args.root is not None or args.output is not None:
            parser.error("--verify-only 不能与 root/output 位置参数同时使用")
        archive = Path(args.verify_only).resolve()
        manifest = verify_archive_from_disk(archive)
        print(
            json.dumps(
                {
                    "status": "pass",
                    "archive": str(archive),
                    "product_version": manifest["product_version"],
                    "build_id": manifest["build_id"],
                    "entry_count": manifest["entry_count"] + 1,
                },
                ensure_ascii=False,
            )
        )
        return

    root = Path(args.root).resolve() if args.root else PROJECT_ROOT
    output = Path(args.output).resolve() if args.output else default_output_path(root)
    manifest = promote_archive(root, output)
    print(
        json.dumps(
            {
                "status": "pass",
                "archive": str(output),
                "product_version": manifest["product_version"],
                "build_id": manifest["build_id"],
                "entry_count": manifest["entry_count"] + 1,
                "hashed_file_count": manifest["entry_count"],
                "total_bytes": manifest["total_bytes"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
