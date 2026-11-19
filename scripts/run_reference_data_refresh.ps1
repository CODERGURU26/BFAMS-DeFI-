param(
    [ValidateSet("test", "main")]
    [string]$DatabaseTarget = "test"
)
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runner = Join-Path $PSScriptRoot "refresh_reference_data.py"
$logDirectory = Join-Path $projectRoot "data_private\logs"

if (-not (Test-Path $python)) {
    throw "Project virtual-environment Python not found: $python"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logFile = Join-Path $logDirectory ("reference-data-{0}-{1}.log" -f $DatabaseTarget, (Get-Date -Format "yyyyMMdd-HHmmss"))

Push-Location $projectRoot
try {
    & $python $runner --target $DatabaseTarget *>&1 | Tee-Object -FilePath $logFile
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
