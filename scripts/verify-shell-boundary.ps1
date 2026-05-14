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

$sourceFiles = Get-RepoFiles -Root $repoRoot -IncludePatterns @('*.cs', '*.swift', '*.ps1')
$routeMatches = Select-String -Path $sourceFiles.FullName -Pattern '"/v1/[^"]+' | ForEach-Object {
  '{0}:{1}:{2}' -f $_.Path, $_.LineNumber, $_.Line.Trim()
}
$disallowedRoutes = @()
foreach ($match in $routeMatches) {
  if ($match -match '"/v1/(interop/|auth/bootstrap/)') {
    continue
  }

  $disallowedRoutes += $match
}

$projectFiles = Get-RepoFiles -Root $repoRoot -IncludePatterns @('*.csproj', 'Package.swift', '*.slnx')
$engineReferenceMatches = Select-String -Path $projectFiles.FullName -Pattern 'ProjectReference|PackageReference.+(Linnaeus\.Engine|Provinode\.Engine)' | ForEach-Object {
  '{0}:{1}:{2}' -f $_.Path, $_.LineNumber, $_.Line.Trim()
}

if ($disallowedRoutes.Count -gt 0 -or $engineReferenceMatches.Count -gt 0) {
  if ($disallowedRoutes.Count -gt 0) {
    Write-Error ("Found non-canonical tester route usage:`n" + ($disallowedRoutes -join "`n"))
  }

  if ($engineReferenceMatches.Count -gt 0) {
    Write-Error ("Found direct engine implementation references in tester projects:`n" + ($engineReferenceMatches -join "`n"))
  }

  exit 1
}

Write-Host 'Engine interop tester shell boundary verification passed.'
