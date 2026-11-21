[CmdletBinding()]
param([string]$EngineRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
$priorBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    python3 -m unittest discover -s tests/drive-diagnostics -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Drive diagnostic tests failed.' }
    node --check src/drive-diagnostics/web/app.js
    if ($LASTEXITCODE -ne 0) { throw 'Drive browser script syntax check failed.' }
    if (-not $EngineRoot) { $EngineRoot = $env:ENGINE_CONTRACT_SOURCE_ROOT }
    if (-not $EngineRoot) {
        $common = (& git rev-parse --path-format=absolute --git-common-dir).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Cannot locate canonical repository.' }
        $canonicalRepo = Split-Path -Parent $common
        $workspace = [IO.Path]::GetFullPath((Join-Path $canonicalRepo '../..'))
        $EngineRoot = Join-Path $workspace 'linnaeus/linnaeus-engine'
    }
    if (-not (Test-Path -LiteralPath $EngineRoot -PathType Container)) {
        throw 'Engine source is required: supply -EngineRoot or ENGINE_CONTRACT_SOURCE_ROOT.'
    }
    $bindings = Get-Content src/drive-diagnostics/contracts.json -Raw | ConvertFrom-Json
    if ($bindings.sourceCommit -notmatch '^[a-f0-9]{40}$') { throw 'Invalid Engine source commit.' }
    python3 scripts/ops/Sync-DriveContracts.py --engine-root $EngineRoot --source-commit $bindings.sourceCommit --check
    if ($LASTEXITCODE -ne 0) { throw 'Engine contract projection is stale.' }
    & "$PSScriptRoot/verify-shell-boundary.ps1"
    if ($LASTEXITCODE -ne 0) { throw 'Consumer boundary verification failed.' }
    foreach ($range in @(@('--check'), @('--cached', '--check'), @('--check', 'origin/main...HEAD'))) {
        & git diff @range
        if ($LASTEXITCODE -ne 0) { throw 'Whitespace verification failed.' }
    }
    & "$PSScriptRoot/ops/Assert-InternalApiArchitecture.ps1" -BaseRef origin/main -Strict
    if ($LASTEXITCODE -ne 0) { throw 'Internal API architecture verification failed.' }
    Write-Host 'PASS: Drive diagnostic transport and UI checks. Live Google qualification is separate.'
} finally {
    $env:PYTHONDONTWRITEBYTECODE = $priorBytecode
    Pop-Location
}
