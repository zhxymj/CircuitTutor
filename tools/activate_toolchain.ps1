# Dot-source before CLI/tests: . .\tools\activate_toolchain.ps1
# Resolve the local desktop installation; changes only this PowerShell process.
# 2026-10-14 起项目与工具链同在 D://智绘电路// 下；按“项目兄弟目录 → Desktop”顺序解析，
# 整体搬迁后无需改脚本。需要固定位置时用 CIRCUIT_VISION_RUNTIME_ROOT 之类环境变量覆盖。
$zhScriptDir = Split-Path $PSScriptRoot -Parent      # ...\智绘电路
$zhProjectRoot = Split-Path $zhScriptDir -Parent      # ...\智绘电路-企业命题演示包
$zhOuterRoot = Split-Path $zhProjectRoot -Parent      # ...\智绘电路
$zhToolchainCandidates = @()
$zhConfiguredToolchain = [Environment]::GetEnvironmentVariable('CIRCUIT_VISION_RUNTIME_ROOT', 'Process')
if ($zhConfiguredToolchain) {
    $zhToolchainCandidates += [IO.Path]::GetFullPath($zhConfiguredToolchain)
}
$zhToolchainCandidates += @(
    (Join-Path $zhOuterRoot '智绘电路工具链'),
    (Join-Path $zhProjectRoot '智绘电路工具链'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) '智绘电路工具链')
)
$zhToolchainRoot = $null
foreach ($zhCandidate in $zhToolchainCandidates) {
    if (Test-Path -LiteralPath $zhCandidate -PathType Container) { $zhToolchainRoot = $zhCandidate; break }
}
if (-not $zhToolchainRoot) {
    throw "工具链目录不存在（已找：$($zhToolchainCandidates -join ' ; ')）"
}
function Set-ZhToolDefault([string]$Name, [string]$Value) {
    if (-not [Environment]::GetEnvironmentVariable($Name, 'Process')) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    }
}
$zhKicadExe = Join-Path $zhToolchainRoot 'kicad\bin\kicad-cli.exe'
$zhSymbolDir = Join-Path $zhToolchainRoot 'kicad\share\kicad\symbols'
$zhFootprintDir = Join-Path $zhToolchainRoot 'kicad\share\kicad\footprints'
$zhSpiceExe = Join-Path $zhToolchainRoot 'ngspice\Spice64\bin\ngspice_con.exe'
$zhVisionRoot = Join-Path $zhToolchainRoot 'vision-ocr'
foreach ($zhRequired in @($zhKicadExe, $zhSymbolDir, $zhFootprintDir, $zhSpiceExe, $zhVisionRoot,
        (Join-Path $zhVisionRoot 'node_modules\tesseract.js'),
        (Join-Path $zhVisionRoot 'node_modules\jpeg-js'))) {
    if (-not (Test-Path -LiteralPath $zhRequired)) { throw "工具链缺少：$zhRequired" }
}
Set-ZhToolDefault 'KICAD_CLI_PATH' $zhKicadExe
Set-ZhToolDefault 'KICAD_SYMBOL_DIR' $zhSymbolDir
Set-ZhToolDefault 'KICAD_FOOTPRINT_DIR' $zhFootprintDir
Set-ZhToolDefault 'KICAD_CONFIG_HOME' (Join-Path $zhToolchainRoot 'kicad-config')
Set-ZhToolDefault 'KICAD_DOCUMENTS_HOME' (Join-Path $zhToolchainRoot 'kicad-documents')
Set-ZhToolDefault 'KICAD_CACHE_HOME' (Join-Path $zhToolchainRoot 'kicad-cache')
Set-ZhToolDefault 'NGSPICE_PATH' $zhSpiceExe
Set-ZhToolDefault 'ZH_VISION_ROOT' $zhVisionRoot
Set-ZhToolDefault 'VISION_OCR_HOME' $zhVisionRoot
Set-ZhToolDefault 'PYTHONUTF8' '1'
