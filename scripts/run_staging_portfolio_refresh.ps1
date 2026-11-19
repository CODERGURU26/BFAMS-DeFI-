param()
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$referenceRunner = Join-Path $PSScriptRoot "refresh_reference_data.py"
$snapshotRunner = Join-Path $PSScriptRoot "create_daily_portfolio_snapshots.py"
$logDirectory = Join-Path $projectRoot "data_private\logs"

foreach ($requiredFile in @($python, $referenceRunner, $snapshotRunner)) {
    if (-not (Test-Path $requiredFile)) {
        throw "Required staging refresh file not found: $requiredFile"
    }
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logFile = Join-Path $logDirectory (
    "staging-portfolio-refresh-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss")
)

Push-Location $projectRoot
try {
    & $python $referenceRunner --target main *>&1 | Tee-Object -FilePath $logFile
    $referenceExitCode = $LASTEXITCODE

    & $python $snapshotRunner --target main *>&1 |
        Tee-Object -FilePath $logFile -Append
    $snapshotExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($referenceExitCode -ne 0) {
    exit $referenceExitCode
}
exit $snapshotExitCode
