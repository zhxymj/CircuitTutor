[CmdletBinding(DefaultParameterSetName = 'Check')]
param(
    [string]$ProjectRoot = (Join-Path $PSScriptRoot '..\evidence\upgrade\open_source_integration\runtime\worktree\CircuitVision'),

    [Parameter(Mandatory = $true, ParameterSetName = 'Check')]
    [switch]$CheckOnly,

    [Parameter(Mandatory = $true, ParameterSetName = 'Start')]
    [switch]$Start,

    [string]$Python = (Join-Path $PSScriptRoot '..\..\..\智绘电路工具链\open-source\circuitvision\venv\Scripts\python.exe'),
    [string]$BindAddress = '127.0.0.1',
    [int]$Port = 8501
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$required = @(
    (Join-Path $ProjectRoot 'app.py'),
    (Join-Path $ProjectRoot 'classes.json'),
    (Join-Path $ProjectRoot 'src\circuit_analyzer.py'),
    (Join-Path $ProjectRoot 'static\css\main.css'),
    (Join-Path $ProjectRoot 'static\images\CircuitVision-nobg_dark.png'),
    (Join-Path $ProjectRoot 'static\images\sdp_banner_black.png'),
    (Join-Path $ProjectRoot 'models\configs\sam2.1_hiera_l.yaml'),
    (Join-Path $ProjectRoot 'models\YOLO\best_large_model_yolo.pt'),
    (Join-Path $ProjectRoot 'models\SAM2\sam2.1_hiera_large.pt'),
    (Join-Path $ProjectRoot 'models\SAM2\best_miou_model_SAM_latest.pth')
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
$pythonPath = (Resolve-Path -LiteralPath $Python -ErrorAction SilentlyContinue).Path
if (-not $pythonPath) { $missing += "Python venv interpreter: $Python" }

if ($missing.Count -gt 0) {
    Write-Output 'INCOMPLETE: required runtime files are missing:'
    $missing | ForEach-Object { Write-Output " - $_" }
    exit 2
}

$dependencyProbe = & $pythonPath -B -c "import streamlit, ultralytics, sam2; print('streamlit=' + streamlit.__version__); print('ultralytics=' + ultralytics.__version__); print('sam2=importable')"
if ($LASTEXITCODE -ne 0) {
    Write-Output 'INCOMPLETE: isolated Python dependency import check failed.'
    $dependencyProbe | ForEach-Object { Write-Output $_ }
    exit 2
}

Write-Output "ProjectRoot: $ProjectRoot"
Write-Output "Python: $pythonPath"
Write-Output 'Path and import checks passed. CheckOnly does not run model inference or claim recognition readiness.'
$dependencyProbe | ForEach-Object { Write-Output $_ }
if ($CheckOnly) { exit 0 }

$savedApiVars = @{}
foreach ($name in @('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'GROQ_API_KEY')) {
    $savedApiVars[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$env:PYTHON_DOTENV_DISABLED = '1'
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = 'false'
Remove-Item Env:GEMINI_API_KEY,Env:GOOGLE_API_KEY,Env:OPENAI_API_KEY,Env:GROQ_API_KEY -ErrorAction SilentlyContinue
Push-Location $ProjectRoot
try {
    & $pythonPath -B -m streamlit run app.py --server.headless true --server.address $BindAddress --server.port $Port
    exit $LASTEXITCODE
}
finally {
    Pop-Location
    foreach ($name in $savedApiVars.Keys) {
        if ($null -eq $savedApiVars[$name]) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            [Environment]::SetEnvironmentVariable($name, $savedApiVars[$name], 'Process')
        }
    }
}
