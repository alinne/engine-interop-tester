[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
# The Python consumer lane does not change the native tester targets. Any other
# path keeps the existing native verification rather than silently skipping it.
$changed = @(& git -C $repoRoot diff --name-only origin/main...HEAD)
if ($LASTEXITCODE -ne 0) { throw 'Cannot classify integration diff.' }
$allowed = '^(src/drive-diagnostics/|tests/drive-diagnostics/|scripts/ops/Sync-DriveContracts\.py$|scripts/verify-(drive-diagnostics|workbench-local-integration|shell-boundary)\.ps1$|schemas/json/drive-diagnostics-action\.v1\.schema\.json$|docs/drive-diagnostics\.md$|README\.md$)'
$other = @($changed | Where-Object { $_ -notmatch $allowed })
if ($changed.Count -gt 0 -and $other.Count -eq 0) {
    & "$PSScriptRoot/verify-drive-diagnostics.ps1"
} else {
    & "$PSScriptRoot/verify.ps1"
}
if ($LASTEXITCODE -ne 0) { throw 'Repository integration verification failed.' }
