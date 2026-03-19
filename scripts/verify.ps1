param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$verifyCore = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path "scripts/lib/Invoke-RepoVerifyCore.ps1"
. $verifyCore

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$winProject = Join-Path $repoRoot "src/windows/EngineInteropTester.WinUI/EngineInteropTester.WinUI.csproj"
$macPackage = Join-Path $repoRoot "src/macos/EngineInteropTester.AppleUX"
$guardScript = Join-Path $repoRoot "scripts/ops/Assert-InternalApiArchitecture.ps1"

Invoke-RepoVerifyCore `
    -RepoRoot $repoRoot `
    -BuildStep {
        if ($IsWindows) {
            dotnet build $winProject /p:EnableWindowsTargeting=true /p:GeneratePackageOnBuild=false
        }
        else {
            dotnet restore $winProject /p:EnableWindowsTargeting=true
            Write-Host "WARN: Skipping WinUI build on non-Windows host because WindowsAppSDK XAML compilation is unavailable." -ForegroundColor Yellow
        }

        if ($IsWindows) {
            Write-Host "WARN: Skipping macOS Swift build on Windows host." -ForegroundColor Yellow
        }
        else {
            swift build --package-path $macPackage
        }
    } `
    -TestStep {
        $hostCreationHits = @()
        $rgResults = @(& rg -n "LinnaeusEngineHost\.CreateDefault\(" $repoRoot 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $hostCreationHits = @($rgResults)
        }
        elseif ($LASTEXITCODE -ne 1) {
            exit $LASTEXITCODE
        }

        $global:LASTEXITCODE = 0

        if ($hostCreationHits.Count -gt 0) {
            $hostCreationHits | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
            Write-Host "FAIL: Engine interop tester must remain consumer-only and must not spin up LinnaeusEngineHost directly." -ForegroundColor Red
            exit 1
        }
    } `
    -GuardScript $guardScript `
    -SuccessMessage "PASS: Engine interop tester verification completed."
