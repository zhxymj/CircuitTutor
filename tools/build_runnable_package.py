#!/usr/bin/env python3
"""Build a relocatable Windows x64 runnable package from the frozen workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ARCHIVE_ROOT = "智绘电路可运行包"
ENTERPRISE_DIR = "智绘电路-企业命题演示包"
PROJECT_DIR = "智绘电路"
TOOLCHAIN_DIR = "智绘电路工具链"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[3] / "交付输出" / "智绘电路-可运行项目包.zip"
EVIDENCE_RELATIVE = Path("evidence/upgrade/open_source_integration/package.json")

SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_NAMES = {
    ".env", "desktop.ini", "thumbs.db", ".ds_store", "credentials.json", "secrets.json",
    "secret.json", "id_rsa", "id_ed25519", "api_key.json",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".part", ".chunk", ".crdownload", ".download"}
COMPRESSED_SUFFIXES = {
    ".7z", ".avi", ".bz2", ".docx", ".gz", ".jpeg", ".jpg", ".mp3", ".mp4",
    ".pdf", ".png", ".pptx", ".pt", ".pth", ".rar", ".svgz", ".wasm", ".webp",
    ".whl", ".xz", ".zip",
}


@dataclass(frozen=True)
class ManifestEntry:
    source: Path
    archive_path: str
    size: int


EXCLUDED_STATS: dict[str, dict[str, int]] = {}
PRUNED_DIRECTORY_COUNT = 0


def _record_excluded(category: str, path: Path, size: int | None = None) -> None:
    record = EXCLUDED_STATS.setdefault(category, {"file_count": 0, "byte_count": 0})
    record["file_count"] += 1
    record["byte_count"] += path.stat().st_size if size is None and path.exists() else (size or 0)


def _safe_file(path: Path) -> bool:
    lowered = path.name.casefold()
    private_suffix = path.suffix.casefold() in {".pfx", ".key", ".kdbx"} or (
        path.suffix.casefold() == ".pem" and lowered != "cacert.pem"
    )
    allowed = (
        path.name.casefold() not in SKIP_NAMES
        and not (lowered.startswith(".env.") and lowered != ".env.template")
        and path.suffix.casefold() not in SKIP_SUFFIXES
        and not (lowered.startswith("client_secret") and path.suffix.casefold() in {".json", ".txt"})
        and not (lowered.endswith("_credentials.json") or lowered.endswith("_private_key.json"))
        and not private_suffix
    )
    if not allowed:
        category = "partial_or_bytecode" if path.suffix.casefold() in SKIP_SUFFIXES else "secrets_and_private_keys"
        _record_excluded(category, path)
    return allowed


def _is_excluded(relative: Path, excluded_paths: set[tuple[str, ...]]) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    return any(parts[:len(excluded)] == excluded for excluded in excluded_paths)


def _walk(root: Path, exclude_paths: set[tuple[str, ...]] | None = None) -> Iterable[Path]:
    if not root.exists():
        return
    excluded_paths = {tuple(part.casefold() for part in path) for path in (exclude_paths or set())}
    for directory, child_dirs, files in os.walk(root):
        base = Path(directory)
        relative_base = base.relative_to(root)
        kept_dirs = []
        for name in child_dirs:
            relative_dir = relative_base / name
            if name.casefold() in SKIP_DIRS or _is_excluded(relative_dir, excluded_paths):
                global PRUNED_DIRECTORY_COUNT
                PRUNED_DIRECTORY_COUNT += 1
                continue
            kept_dirs.append(name)
        child_dirs[:] = sorted(kept_dirs)
        for name in sorted(files):
            path = base / name
            if _safe_file(path) and not path.is_symlink() and not _is_excluded(path.relative_to(root), excluded_paths):
                yield path


def _entry(source: Path, relative: str) -> ManifestEntry:
    return ManifestEntry(source.resolve(), f"{ARCHIVE_ROOT}/{relative.replace(os.sep, '/')}", source.stat().st_size)


def collect_manifest(project_root: Path, toolchain_root: Path) -> list[ManifestEntry]:
    """Collect explicit runtime/source roots; never traverses excluded HCD/cache roots."""
    global PRUNED_DIRECTORY_COUNT
    EXCLUDED_STATS.clear()
    PRUNED_DIRECTORY_COUNT = 0
    project_root = Path(project_root).resolve()
    toolchain_root = Path(toolchain_root).resolve()
    if project_root.name != PROJECT_DIR or project_root.parent.name != ENTERPRISE_DIR:
        raise ValueError(f"Expected {ENTERPRISE_DIR}/{PROJECT_DIR}, got {project_root}")

    entries: dict[str, ManifestEntry] = {}

    def add_file(source: Path, archive_relative: str) -> None:
        if source.is_file() and _safe_file(source) and not source.is_symlink():
            entry = _entry(source, archive_relative)
            entries[entry.archive_path] = entry

    def add_tree(source: Path, archive_relative: str, exclude_paths: set[tuple[str, ...]] | None = None) -> None:
        if not source.is_dir():
            return
        for path in _walk(source, exclude_paths):
            relative = path.relative_to(source)
            if "hcd" in {part.casefold() for part in relative.parts} and path.suffix.casefold() in {".pt", ".pth", ".ckpt", ".safetensors", ".bin"}:
                _record_excluded("unbundled_hcd_checkpoints", path)
                continue
            add_file(path, str(Path(archive_relative) / relative))

    project_relative = Path(ENTERPRISE_DIR) / PROJECT_DIR
    # Ship current source, docs, tests, test data, and frozen evidence. Keep caches
    # and the duplicated runtime asset mirror out of the project evidence tree.
    add_tree(project_root, str(project_relative), exclude_paths={
        ("data", "history"),
        ("tmp",),
        ("evidence", "upgrade", "open_source_integration", "runtime", "assets"),
        ("evidence", "upgrade", "open_source_integration", "runtime", "cache"),
        ("evidence", "upgrade", "open_source_integration", "runtime", "temp"),
        ("evidence", "upgrade", "open_source_integration", "runtime", "downloads"),
        ("evidence", "upgrade", "open_source_integration", "package.json"),
    })

    evidence_root = project_root / "evidence" / "upgrade" / "open_source_integration"

    runtime_root = evidence_root / "runtime" / "worktree" / "CircuitVision"
    runtime_rel = Path(ENTERPRISE_DIR) / PROJECT_DIR / "evidence/upgrade/open_source_integration/runtime/worktree/CircuitVision"
    add_tree(runtime_root, str(runtime_rel), exclude_paths={("assets",), ("hcd",), ("checkpoints",)})

    # Copy model payload only from the canonical worktree location, once.
    for relative in (
        Path("models/configs"), Path("models/YOLO"), Path("models/SAM2"),
    ):
        add_tree(runtime_root / relative, str(runtime_rel / relative))

    tool_rel = Path(TOOLCHAIN_DIR)
    for name in ("kicad", "vision-python", "vision-ocr", "ngspice", "kicad-config", "kicad-documents"):
        add_tree(toolchain_root / name, str(tool_rel / name))
    add_tree(
        toolchain_root / "open-source" / "circuitvision" / "venv",
        str(tool_rel / "open-source/circuitvision/venv"),
    )
    tscircuit = toolchain_root / "open-source" / "tscircuit"
    add_tree(tscircuit / "node_modules", str(tool_rel / "open-source/tscircuit/node_modules"))
    add_tree(tscircuit / "state", str(tool_rel / "open-source/tscircuit/state"))
    for name in ("package.json", "package-lock.json"):
        add_file(tscircuit / name, str(tool_rel / "open-source/tscircuit" / name))

    packaged_node = toolchain_root / "open-source" / "tscircuit" / "node.exe"
    node_exe = packaged_node if packaged_node.is_file() else (shutil.which("node.exe") or shutil.which("node"))
    if node_exe:
        add_file(Path(node_exe), str(tool_rel / "open-source/tscircuit/node.exe"))

    # Include only the enterprise handoff reports and control docs, not unrelated Desktop files.
    enterprise_root = project_root.parent
    for name in ("START_DEMO.bat", "start_demo.ps1", "报告书.pdf", "智绘电路.pptx", "总说明.md", "改造任务清单.md", "执行进度.md"):
        add_file(enterprise_root / name, str(Path(ENTERPRISE_DIR) / name))

    # These two validation records are the only temporary tree items kept in the handoff.
    for relative in (Path("tmp/pdfs/integrity.json"), Path("tmp/upgrade_deck/validation.json")):
        add_file(enterprise_root / relative, str(Path(ENTERPRISE_DIR) / relative))

    return [entries[name] for name in sorted(entries)]


def summarize_manifest(manifest: list[ManifestEntry]) -> dict:
    counts: dict[str, int] = {}
    for entry in manifest:
        relative = entry.archive_path.removeprefix(f"{ARCHIVE_ROOT}/")
        category = relative.split("/", 1)[0]
        counts[category] = counts.get(category, 0) + 1
    longest = max((entry.archive_path for entry in manifest), key=len, default="")
    return {
        "file_count": len(manifest),
        "byte_count": sum(entry.size for entry in manifest),
        "manifest_counts": dict(sorted(counts.items())),
        "longest_zip_entry_path": longest,
        "longest_zip_entry_path_chars": len(longest),
        "exclusion_counts": {
            "observed_excluded_files": sum(value["file_count"] for value in EXCLUDED_STATS.values()),
            "observed_excluded_bytes": sum(value["byte_count"] for value in EXCLUDED_STATS.values()),
            "by_category": dict(sorted((name, dict(value)) for name, value in EXCLUDED_STATS.items())),
            "pruned_directory_count": PRUNED_DIRECTORY_COUNT,
            "pruned_directory_contents_measured": False,
        },
    }


def render_bootstrap_ps1() -> str:
    return "\ufeff" + r'''[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$NoBrowser,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$packageRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$enterpriseRoot = Join-Path $packageRoot '智绘电路-企业命题演示包'
$projectRoot = Join-Path $enterpriseRoot '智绘电路'
$toolchainRoot = Join-Path $packageRoot '智绘电路工具链'
$pythonHome = Join-Path $toolchainRoot 'kicad\bin'
$pythonExe = Join-Path $toolchainRoot 'vision-python\Scripts\python.exe'
$cvPythonExe = Join-Path $toolchainRoot 'open-source\circuitvision\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Toolchain Python not found: $pythonExe" }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'server.py') -PathType Leaf)) { throw "Project server not found: $projectRoot" }

$venvs = @(
    @{ Path = (Join-Path $toolchainRoot 'vision-python\pyvenv.cfg'); Shared = $false },
    @{ Path = (Join-Path $toolchainRoot 'open-source\circuitvision\venv\pyvenv.cfg'); Shared = $true }
)
foreach ($venv in $venvs) {
    $cfg = $venv.Path
    if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) { throw "Virtual environment config not found: $cfg" }
    $versionLine = Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^version\s*=' } | Select-Object -First 1
    if (-not $versionLine) { throw "Python version missing from $cfg" }
    $shared = if ($venv.Shared) { 'true' } else { 'false' }
    $content = @("home = $pythonHome", "include-system-site-packages = $shared", $versionLine, "executable = $(Join-Path $pythonHome 'python.exe')") -join "`n"
    [IO.File]::WriteAllText($cfg, $content + "`n", [Text.UTF8Encoding]::new($false))
}
$sharedPth = Join-Path $toolchainRoot 'open-source\circuitvision\venv\Lib\site-packages\shared-vision-site.pth'
$sharedPthParent = Split-Path -Parent $sharedPth
New-Item -ItemType Directory -Path $sharedPthParent -Force | Out-Null
'import sys,pathlib; sys.path.append(str(pathlib.Path(sys.prefix).parents[2]/"vision-python"/"Lib"/"site-packages"))' |
    Set-Content -LiteralPath $sharedPth -Encoding ascii

$env:PATH = "$(Join-Path $toolchainRoot 'open-source\tscircuit');$(Join-Path $toolchainRoot 'vision-python\Scripts');$(Join-Path $toolchainRoot 'kicad\bin');$env:PATH"
$env:TSCIRCUIT_PREFIX = Join-Path $toolchainRoot 'open-source\tscircuit'
$env:CIRCUIT_VISION_RUNTIME_ROOT = $toolchainRoot
$env:KICAD_CLI_PATH = Join-Path $toolchainRoot 'kicad\bin\kicad-cli.exe'
$env:KICAD_SYMBOL_DIR = Join-Path $toolchainRoot 'kicad\share\kicad\symbols'
$env:KICAD_FOOTPRINT_DIR = Join-Path $toolchainRoot 'kicad\share\kicad\footprints'
$env:KICAD_CONFIG_HOME = Join-Path $toolchainRoot 'kicad-config'
$env:KICAD_DOCUMENTS_HOME = Join-Path $toolchainRoot 'kicad-documents'
$env:KICAD_CACHE_HOME = Join-Path $toolchainRoot 'kicad-cache'
$env:NGSPICE_PATH = Join-Path $toolchainRoot 'ngspice\Spice64\bin\ngspice_con.exe'
$env:ZH_VISION_ROOT = Join-Path $toolchainRoot 'vision-ocr'
$env:VISION_OCR_HOME = $env:ZH_VISION_ROOT
New-Item -ItemType Directory -Path $env:KICAD_CACHE_HOME -Force | Out-Null
. (Join-Path $projectRoot 'tools\activate_toolchain.ps1')
$env:LSQC_HOST = '127.0.0.1'
$listener = $null
$selectedPort = $null
for ($offset = 0; $offset -lt 16 -and -not $selectedPort; $offset++) {
    $candidate = (($Port + $offset - 1) % 65535) + 1
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidate)
        $listener.Start()
        $selectedPort = $candidate
    }
    catch { }
    finally { if ($listener) { $listener.Stop(); $listener = $null } }
}
if (-not $selectedPort) { throw "No free localhost port found in the range beginning at $Port; choose another -Port." }
$Port = $selectedPort
$env:LSQC_PORT = [string]$Port
$env:PYTHONUTF8 = '1'
foreach ($name in @('PYTHONHOME', 'PYTHONPATH')) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$savedApi = @{}
foreach ($name in @('GEMINI_API_KEY','GOOGLE_API_KEY','OPENAI_API_KEY','GROQ_API_KEY','DEEPSEEK_API_KEY')) {
    $savedApi[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$env:PYTHON_DOTENV_DISABLED = '1'
$actualPrefix = (& $pythonExe -B -c "import sys; print(sys.prefix)" | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Packaged Python failed to start.' }
$expectedPrefix = [IO.Path]::GetFullPath((Join-Path $toolchainRoot 'vision-python'))
if ([IO.Path]::GetFullPath($actualPrefix) -ne $expectedPrefix) {
    throw "Python resolved outside this package. Expected $expectedPrefix, got $actualPrefix"
}
$cvPrefix = (& $cvPythonExe -B -c "import sys; print(sys.prefix)" | Select-Object -Last 1).Trim()
$expectedCvPrefix = [IO.Path]::GetFullPath((Join-Path $toolchainRoot 'open-source\circuitvision\venv'))
if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($cvPrefix) -ne $expectedCvPrefix) {
    throw "CircuitVision Python resolved outside this package. Expected $expectedCvPrefix, got $cvPrefix"
}
if ($CheckOnly) {
    Push-Location $projectRoot
    try { & $pythonExe -B -c "import server,sys; print('project sys.prefix=' + sys.prefix)" }
    finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $cvPythonExe -B -c "import streamlit,ultralytics,sam2,sys; print('CircuitVision sys.prefix=' + sys.prefix)"
    if ($LASTEXITCODE -eq 0) { Write-Output 'CHECK_OK' }
    exit $LASTEXITCODE
}
if (-not $NoBrowser) {
    $browserUrl = "http://127.0.0.1:$Port"
    Start-Job -ArgumentList $browserUrl, $Port -ScriptBlock {
        param($url, $port)
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            $probe = $null
            try {
                $probe = [System.Net.Sockets.TcpClient]::new()
                $probe.Connect('127.0.0.1', [int]$port)
                Start-Process $url
                return
            }
            catch { Start-Sleep -Milliseconds 500 }
            finally { if ($probe) { $probe.Dispose() } }
        }
    } | Out-Null
}
Write-Output "Listening URL: http://127.0.0.1:$Port"
Push-Location $projectRoot
try {
    & $pythonExe -B server.py
}
finally {
    Pop-Location
    foreach ($name in $savedApi.Keys) {
        if ($null -eq $savedApi[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($name, $savedApi[$name], 'Process') }
    }
}
exit $LASTEXITCODE
'''


def render_readme() -> str:
    return """# 智绘电路可运行包

本包面向 Windows x64 本机使用，保留 `智绘电路-企业命题演示包` 与 `智绘电路工具链` 的同级目录关系。程序、依赖和随包 CircuitVision 模型均从解压目录读取；不需要安装系统级 Python，也不会设置系统环境变量。本次迁移检查只在打包主机执行过，其他电脑首次运行前请用 `-CheckOnly` 检查本机解压结果。

## 首次运行

1. 使用支持 ZIP64 与长路径的解压工具，将 `智绘电路可运行包` 整个目录解压到可写位置。Windows 自带解压对长路径可能有限制，建议使用 7-Zip。目录结构不要拆开。
2. 可先运行 `首次解压.bat -CheckOnly` 检查本包 Python、服务模块和 CircuitVision 依赖；该检查不运行模型推理。需要自定义参数时运行 `首次解压.bat -NoBrowser -Port 9000`。
3. 双击 `首次解压.bat` 启动本机服务。启动器支持 `-NoBrowser` 关闭自动打开浏览器，以及 `-Port 9000` 指定端口。

首次启动会修正本包副本中的两个 `pyvenv.cfg` 和共享 site-packages 路径。修复只写入解压副本。服务只监听 `127.0.0.1`。

## 包含范围与限制

- 包含应用源码、测试与所需项目资料，KiCad、ngspice、OCR/Python/Node运行时，以及 CircuitVision 所需模型。
- 主业务仍保留原有离线模板路径；CircuitVision 支持离线使用。默认启动无需 API key，也不发起在线模型请求。
- HCD 未授权分发的检查点不随包提供；完整 HCD 多阶段运行环境也不在此包内。
- 主页面“本机 CircuitVision”运行项目内置检测/分割代码，并叠加 HCD 六源算法生成的导线几何候选；几何点和线不自动成为电气网络。
- 主页面“代码建电路”使用随包本机 tscircuit 运行 TSX，预览后进入现有审核、校验、应用、编辑与撤销；外部格式适配功能仍然可用。
- 随包 PDF/PPT 是 2026-10-14 状态快照，当前验收应以文档和 acceptance 证据为准。
- 图片识别请先选择本地 CircuitVision，生成候选前逐项人工确认参数值和每个引脚的网络；未知参数不会被猜测补齐。
- 包内模型与静态资源用于本机离线运行。`-CheckOnly` 只检查路径和导入，不能代表识别准确率或推理验收。
- ZIP 内 `manifest.sha256.json` 列出每个归档成员的 SHA-256，可用于解压后核对文件。
"""


def render_bootstrap_bat() -> str:
    return ('@echo off\r\nsetlocal\r\nchcp 65001>nul\r\n'
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0首次解压.ps1" %*\r\n'
            'set "launch_exit=%errorlevel%"\r\n'
            'if not "%launch_exit%"=="0" (\r\n'
            '  echo.\r\n'
            '  echo Launch failed ^(exit %launch_exit%^).\r\n'
            '  if "%~1"=="" pause\r\n'
            ')\r\n'
            'exit /b %launch_exit%\r\n')


def write_archive(manifest: list[ManifestEntry], output_path: Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        for entry in manifest:
            info = zipfile.ZipInfo(entry.archive_path)
            info.compress_type = zipfile.ZIP_STORED if entry.source.suffix.casefold() in COMPRESSED_SUFFIXES else zipfile.ZIP_DEFLATED
            info._compresslevel = 1  # Python 3.11 ZipInfo does not inherit ZipFile.compresslevel in open().
            info.create_system = 0
            digest = hashlib.sha256()
            with entry.source.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
            hashes[entry.archive_path] = digest.hexdigest()
        generated = (
            ("README.md", render_readme()),
            ("首次解压.ps1", render_bootstrap_ps1()),
            ("首次解压.bat", render_bootstrap_bat()),
        )
        for name, content in generated:
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{name}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 1
            info.create_system = 0
            with archive.open(info, "w", force_zip64=True) as target:
                target.write(content.encode("utf-8"))
            hashes[info.filename] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/manifest.sha256.json")
        info.compress_type = zipfile.ZIP_DEFLATED
        info._compresslevel = 1
        info.create_system = 0
        payload = (json.dumps(dict(sorted(hashes.items())), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with archive.open(info, "w", force_zip64=True) as target:
            target.write(payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_refresh_manifest(project_root: Path, include_paths: Iterable[str]) -> list[ManifestEntry]:
    """Read only explicitly selected project files/directories, never toolchains."""
    project_root = Path(project_root).resolve()
    if project_root.name != PROJECT_DIR or project_root.parent.name != ENTERPRISE_DIR:
        raise ValueError(f"Expected {ENTERPRISE_DIR}/{PROJECT_DIR}, got {project_root}")
    if isinstance(include_paths, (str, bytes)):
        raise ValueError("Refresh paths must be a list of project-relative paths")
    selections = list(include_paths)
    if not selections:
        raise ValueError("Refresh requires at least one explicit project-relative path")
    EXCLUDED_STATS.clear()
    entries: dict[str, ManifestEntry] = {}

    def add_file(path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(project_root):
            raise ValueError(f"Refresh file resolves outside project: {path}")
        relative = path.relative_to(project_root)
        if any(part.casefold() in SKIP_DIRS for part in relative.parts) or path.is_symlink():
            return
        if _safe_file(path):
            entry = _entry(path, str(Path(ENTERPRISE_DIR) / PROJECT_DIR / relative))
            entries[entry.archive_path] = entry

    for value in selections:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Each refresh path must be a nonempty relative string")
        relative = Path(value)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError(f"Refresh path must stay inside project: {value}")
        if any(part.casefold() in SKIP_DIRS for part in relative.parts):
            raise ValueError(f"Refresh path is excluded from packages: {value}")
        source = project_root / relative
        if not source.resolve().is_relative_to(project_root) or source.is_symlink():
            raise ValueError(f"Refresh path resolves outside project or is a symlink: {value}")
        if source.is_dir():
            for path in _walk(source):
                add_file(path)
        elif source.is_file():
            if not _safe_file(source):
                raise ValueError(f"Refresh file is excluded from packages: {value}")
            add_file(source)
        else:
            raise FileNotFoundError(f"Refresh source does not exist: {value}")
    if not entries:
        raise ValueError("Refresh selection contains no packageable files")
    return [entries[name] for name in sorted(entries)]


def _find_7zip(toolchain_root: Path) -> Path:
    for candidate in (
        Path(toolchain_root) / "7zip/mingw64/bin/7z.exe",
        Path(toolchain_root) / "7zip/7z.exe",
    ):
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"Existing 7-Zip executable not found below {toolchain_root}")


def refresh_archive(project_root: Path, output_path: Path, include_paths: Iterable[str],
                    toolchain_root: Path | None = None) -> dict:
    """Replace selected ZIP members with 7-Zip; copy untouched compressed data.

    Reads the central directory and existing hash list once. Only selected
    project sources and generated launch files are staged/hashed/recompressed.
    The original runtime/model source trees are neither walked nor opened.
    """
    output_path = Path(output_path).resolve()
    if not output_path.is_file():
        raise FileNotFoundError(f"Refresh requires an existing ZIP: {output_path}")
    manifest = collect_refresh_manifest(project_root, include_paths)
    if any(entry.source == output_path for entry in manifest):
        raise ValueError("The output archive cannot be included in its own refresh")
    seven_zip = _find_7zip(toolchain_root if toolchain_root is not None else _defaults()[1])
    hashes_name = f"{ARCHIVE_ROOT}/manifest.sha256.json"
    with zipfile.ZipFile(output_path) as archive:
        file_names = [info.filename for info in archive.infolist() if not info.is_dir()]
        if len(file_names) != len(set(name.casefold() for name in file_names)):
            raise ValueError("Existing ZIP has duplicate or case-colliding members")
        if hashes_name not in file_names:
            raise ValueError("Existing ZIP has no manifest.sha256.json")
        try:
            hashes = json.loads(archive.read(hashes_name))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Existing ZIP hash manifest is invalid") from exc
        if (not isinstance(hashes, dict) or set(hashes) != set(file_names) - {hashes_name}
                or not all(isinstance(digest, str) and len(digest) == 64 for digest in hashes.values())):
            raise ValueError("Existing ZIP hash manifest does not describe every file member")
    before_size = output_path.stat().st_size
    updated_hashes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="zhihui-package-refresh-") as temporary:
        staging = Path(temporary)
        for entry in manifest:
            target = staging / Path(entry.archive_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry.source, target)
            updated_hashes[entry.archive_path] = _sha256(target)
        for name, text in (
            ("README.md", render_readme()),
            ("首次解压.ps1", render_bootstrap_ps1()),
            ("首次解压.bat", render_bootstrap_bat()),
        ):
            archive_name = f"{ARCHIVE_ROOT}/{name}"
            target = staging / Path(archive_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = text.encode("utf-8")
            target.write_bytes(payload)
            updated_hashes[archive_name] = hashlib.sha256(payload).hexdigest()
        hashes.update(updated_hashes)
        (staging / Path(hashes_name)).write_text(
            json.dumps(dict(sorted(hashes.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        member_names = sorted([*updated_hashes, hashes_name])
        file_list = staging / "refresh-members.txt"
        file_list.write_text("\n".join(member_names) + "\n", encoding="utf-8")
        # p/q copy unmatched archive entries; r/x/y/z always compress selected
        # disk files, including equal size/time and archive-newer timestamps.
        command = [str(seven_zip), "a", "-tzip", "-mx=1", "-mcu=on", "-up1q1r2x2y2z2",
                   "-scsUTF-8", "-sccUTF-8", "-spd", "-y", "-bd", "-bsp0", "-bb0",
                   str(output_path), "@" + str(file_list)]
        completed = subprocess.run(command, cwd=staging, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", check=False)
        if completed.returncode:
            raise RuntimeError(f"7-Zip refresh failed ({completed.returncode}): {completed.stdout}\n{completed.stderr}")
    # Validate only replaced payloads; unmodified model/library data is not read.
    with zipfile.ZipFile(output_path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        if len(names) != len(set(name.casefold() for name in names)):
            raise RuntimeError("7-Zip refresh produced duplicate members")
        if set(names) != set(hashes) | {hashes_name}:
            raise RuntimeError("7-Zip refresh member names differ from its hash manifest")
        for name, expected in updated_hashes.items():
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError(f"Refreshed ZIP payload hash mismatch: {name}")
        if json.loads(archive.read(hashes_name)) != hashes:
            raise RuntimeError("Refreshed ZIP hash manifest mismatch")
    return {
        "mode": "refresh", "output": str(output_path), "seven_zip": str(seven_zip),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_source_files": len(manifest), "selected_source_bytes": sum(entry.size for entry in manifest),
        "updated_member_paths": member_names, "updated_members": len(member_names),
        "untouched_member_count": len(set(file_names) - set(member_names)),
        "archive_member_count": len(names), "archive_bytes_before": before_size,
        "archive_bytes_after": output_path.stat().st_size,
        "untouched_payloads": "7-Zip copies compressed archive members; no runtime/model source reads",
        "sha256_scope": "Updated members validated; unchanged member hashes reused from existing manifest",
    }


def write_package_evidence(project_root: Path, output_path: Path, manifest: list[ManifestEntry]) -> dict:
    summary = summarize_manifest(manifest)
    evidence = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "build_command": f'python tools/build_runnable_package.py --output "{output_path}"',
        "output": str(output_path.resolve()),
        "sha256": _sha256(output_path),
        **summary,
    }
    destination = Path(project_root) / EVIDENCE_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


def _defaults() -> tuple[Path, Path]:
    project = Path(__file__).resolve().parents[1]
    toolchain = project.parent.parent / TOOLCHAIN_DIR
    return project, toolchain


def main(argv: list[str] | None = None) -> int:
    project_default, toolchain_default = _defaults()
    parser = argparse.ArgumentParser(description="Build a relocatable Windows x64 runnable package.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="Show bounded manifest counts without building.")
    mode.add_argument("--refresh", action="store_true", help="Update explicit project paths in an existing ZIP using installed 7-Zip.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="ZIP output path.")
    parser.add_argument("--include", action="append", default=[], help="Project-relative file/directory to refresh; repeat as needed.")
    parser.add_argument("--paths-file", type=Path, help="UTF-8 JSON array of project-relative paths to refresh.")
    parser.add_argument("--project-root", type=Path, default=project_default, help=argparse.SUPPRESS)
    parser.add_argument("--toolchain-root", type=Path, default=toolchain_default, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.refresh:
        selected = list(args.include)
        if args.paths_file is not None:
            payload = json.loads(args.paths_file.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, list):
                parser.error("--paths-file must contain a JSON array of project-relative paths")
            selected.extend(payload)
        if not selected:
            parser.error("--refresh requires --include or --paths-file")
        result = refresh_archive(args.project_root, args.output, selected, args.toolchain_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.include or args.paths_file is not None:
        parser.error("--include and --paths-file require --refresh")
    manifest = collect_manifest(args.project_root, args.toolchain_root)
    summary = summarize_manifest(manifest)
    if args.list:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    write_archive(manifest, args.output)
    evidence = write_package_evidence(args.project_root, args.output, manifest)
    print(json.dumps({key: value for key, value in evidence.items() if key != "output"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
