param(
  [ValidateSet('dotnet', 'swift')]
  [string]$Language = 'dotnet',
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path,
  [string]$OutputPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = (Resolve-Path (Join-Path $RepoRoot '../..')).Path
$engineScript = Join-Path $workspaceRoot 'linnaeus/linnaeus-engine/scripts/contracts/Generate-PlaneAClientStubs.ps1'
if (-not (Test-Path -LiteralPath $engineScript)) {
  throw "Plane A client stub generator not found at $engineScript"
}

$resolvedOutputPath = $OutputPath
if (-not [string]::IsNullOrWhiteSpace($resolvedOutputPath) -and -not [System.IO.Path]::IsPathRooted($resolvedOutputPath)) {
  $resolvedOutputPath = Join-Path $RepoRoot ($resolvedOutputPath -replace '\\', '/')
}

$arguments = @{
  Language = $Language
  RepoRoot = (Join-Path $workspaceRoot 'linnaeus/linnaeus-engine')
}
if (-not [string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
  $arguments.OutputPath = $resolvedOutputPath
}

& pwsh -NoProfile -File $engineScript @arguments
if ($LASTEXITCODE -ne 0) {
  throw 'Plane A contract sync failed.'
}
