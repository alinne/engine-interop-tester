[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$selfPath = $PSCommandPath

function Get-RepoFiles {
  param(
    [Parameter(Mandatory)] [string]$Root,
    [Parameter(Mandatory)] [string[]]$IncludePatterns
  )

  Get-ChildItem -Path $Root -Recurse -File -Include $IncludePatterns |
    Where-Object {
      $_.FullName -notlike '*\bin\*' -and
      $_.FullName -notlike '*\obj\*' -and
      $_.FullName -notlike '*\.build\*' -and
      $_.FullName -ne $selfPath
    }
}

$sourceFiles = @(Get-RepoFiles -Root (Join-Path $repoRoot 'src') -IncludePatterns @('*.cs', '*.swift', '*.py', '*.js'))
$sourceFiles += @(Get-RepoFiles -Root (Join-Path $repoRoot 'scripts') -IncludePatterns @('*.ps1'))
$routeMatches = @(Select-String -Path $sourceFiles.FullName -Pattern '"/v1/[^"]+' | ForEach-Object {
  '{0}:{1}:{2}' -f $_.Path, $_.LineNumber, $_.Line.Trim()
})
$disallowedRoutes = @()
foreach ($match in $routeMatches) {
  if ($match -match '"/v1/(interop/|auth/bootstrap/)') {
    continue
  }

  $disallowedRoutes += $match
}

$projectFiles = Get-RepoFiles -Root (Join-Path $repoRoot 'src') -IncludePatterns @('*.csproj', 'Package.swift', '*.slnx')
$engineReferenceMatches = @(Select-String -Path $projectFiles.FullName -Pattern 'ProjectReference|PackageReference.+(Linnaeus\.Engine|Provinode\.Engine)' | ForEach-Object {
  '{0}:{1}:{2}' -f $_.Path, $_.LineNumber, $_.Line.Trim()
})

if ($disallowedRoutes.Count -gt 0 -or $engineReferenceMatches.Count -gt 0) {
  if ($disallowedRoutes.Count -gt 0) {
    Write-Error ("Found non-canonical tester route usage:`n" + ($disallowedRoutes -join "`n"))
  }

  if ($engineReferenceMatches.Count -gt 0) {
    Write-Error ("Found direct engine implementation references in tester projects:`n" + ($engineReferenceMatches -join "`n"))
  }

  exit 1
}

# New Drive calls come only from the committed Engine projection, never a
# second handwritten route list in the presentation/client implementation.
$driveFiles = @(Get-ChildItem (Join-Path $repoRoot 'src/drive-diagnostics') -Recurse -File -Include '*.py','*.js')
$manualDriveRoutes = @(Select-String -Path $driveFiles.FullName -Pattern '/engine/v1/')
if ($manualDriveRoutes.Count -gt 0) { throw 'Drive client contains handwritten Engine routes.' }
$bindings = Get-Content (Join-Path $repoRoot 'src/drive-diagnostics/contracts.json') -Raw | ConvertFrom-Json
if ($bindings.sourceCommit -notmatch '^[a-f0-9]{40}$' -or @($bindings.operations.PSObject.Properties).Count -ne 6) {
    throw 'Drive route projection provenance is invalid.'
}
foreach ($entry in $bindings.operations.PSObject.Properties) {
    if ($entry.Value.path -notmatch '^/engine/v1/cloud/' -or $entry.Value.method -notin @('GET', 'POST')) {
        throw 'Drive projection escapes its Engine cloud capability.'
    }
}
Write-Host 'Engine interop tester shell boundary verification passed.'
