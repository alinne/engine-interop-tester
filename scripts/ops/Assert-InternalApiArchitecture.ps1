param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$BaseRef = "HEAD~1",
    [string[]]$ChangedFiles,
    [switch]$Strict
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) { $PSNativeCommandUseErrorActionPreference = $false }
function Fail { param([string]$Message) Write-Host "FAIL: $Message" -ForegroundColor Red; exit 1 }
function Warn { param([string]$Message) Write-Host "WARN: $Message" -ForegroundColor Yellow }
function To-SlashPath { param([string]$PathText) (($PathText ?? "") -replace "\\", "/").Trim().TrimStart("./").TrimEnd("/") }
function Get-ChangedFilesFromGit { param([string]$Root, [string]$Ref) $diffOutput = & git -C $Root diff --name-only --diff-filter=ACMR $Ref --; if ($LASTEXITCODE -ne 0) { throw "Unable to compute changed files from git diff against '$Ref'." }; $untrackedOutput = & git -C $Root ls-files --others --exclude-standard; if ($LASTEXITCODE -ne 0) { throw "Unable to compute untracked files from git." }; return @($diffOutput + $untrackedOutput | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }) }
function Is-ContractArtifactPath { param([string]$RelativePath) return $RelativePath -match "(^|/)(contracts/proto/.+\.proto)$" -or $RelativePath -match "(^|/)(specs/openapi/.+\.(json|ya?ml))$" -or $RelativePath -match "(^|/)(specs/asyncapi/.+\.(json|ya?ml))$" -or $RelativePath -match "(^|/)(specs/arazzo/.+\.(json|ya?ml))$" -or $RelativePath -match "(^|/)(schemas/json/.+\.(json|ya?ml))$" }
function Is-ApiImplementationCandidatePath { param([string]$RelativePath) $fileName = [System.IO.Path]::GetFileName($RelativePath); if ([string]::IsNullOrWhiteSpace($fileName)) { return $false }; $extension = [System.IO.Path]::GetExtension($RelativePath).ToLowerInvariant(); if ($extension -notin @(".cs",".swift",".ps1",".ts",".tsx",".js",".jsx",".py",".go",".kt")) { return $false }; if ($RelativePath -match "(^|/)(tests?|test)/") { return $false }; if ($fileName -eq "Program.cs") { return $true }; return $fileName -match "(?i)(endpoint|endpoints|api|apphost|controlserver|routebuilder|routes|apihost|hostservice)" }
function Test-ApiImplementationContent { param([string]$Content) $patterns = @("Map(Get|Post|Put|Delete|Patch|Group)\(","WebApplication\.Create","CreateSlimBuilder\(","IEndpointRouteBuilder","RouteHandlerBuilder","new\s+HttpListener","HttpListenerContext","listener\.Prefixes\.Add","UseUrls\(","path\.(Equals|StartsWith)\(""/v1/","path\.(Equals|StartsWith)\(""/engine/v1/","Results\.(Ok|Json|File|Unauthorized|Conflict|NotFound)\("); foreach ($pattern in $patterns) { if ($Content -match $pattern) { return $true } }; return $false }
$allChanged = @(); $providedChangedFiles = @($ChangedFiles | Where-Object { $null -ne $_ }); if ($providedChangedFiles.Count -gt 0) { $allChanged = @($providedChangedFiles) } else { try { $allChanged = @(Get-ChangedFilesFromGit -Root $RepoRoot -Ref $BaseRef) } catch { Fail $_.Exception.Message } }
$normalizedChanged = @($allChanged | ForEach-Object { To-SlashPath -PathText ([string]$_) } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
if ($normalizedChanged.Count -eq 0) { Write-Host "PASS: No changed files detected." -ForegroundColor Green; exit 0 }
$contractArtifactChanges = @($normalizedChanged | Where-Object { Is-ContractArtifactPath -RelativePath $_ }); $apiImplementationChanges = New-Object System.Collections.Generic.List[string]
foreach ($relativePath in $normalizedChanged) { if (-not (Is-ApiImplementationCandidatePath -RelativePath $relativePath)) { continue }; $absolutePath = Join-Path $RepoRoot $relativePath; if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) { continue }; $content = Get-Content -LiteralPath $absolutePath -Raw; if (Test-ApiImplementationContent -Content $content) { $apiImplementationChanges.Add($relativePath) } }
if ($apiImplementationChanges.Count -eq 0) { Write-Host "PASS: No API implementation changes detected in changed files." -ForegroundColor Green; exit 0 }
if ($contractArtifactChanges.Count -eq 0) { foreach ($path in $apiImplementationChanges) { Write-Host "API: $path" -ForegroundColor Yellow }; Fail "API implementation changes require at least one changed contract artifact under contracts/proto, specs/openapi, specs/asyncapi, specs/arazzo, or schemas/json." }
Write-Host "PASS: API implementation changes are accompanied by contract artifact changes." -ForegroundColor Green
foreach ($path in $apiImplementationChanges) { Write-Host "API:  $path" }
foreach ($path in $contractArtifactChanges) { Write-Host "SPEC: $path" }
if ($Strict.IsPresent) { foreach ($path in $apiImplementationChanges) { Warn "Strict mode detected interop harness surface work: $path" } }
exit 0
