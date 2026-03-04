param(
  [Parameter(Mandatory)] [string[]]$BaseUrls,
  [string]$TlsPin,
  [string]$BearerToken,
  [string]$OutputPath = ''
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'lib\interop-http.ps1')

$rows = @()
foreach ($baseUrl in $BaseUrls) {
  $client = New-InteropHttpClient -BaseUrl $baseUrl -TlsPin $TlsPin
  $payload = Invoke-InteropJson -Client $client -Method GET -Path '/v1/interop/cluster/capabilities' -BearerToken $BearerToken
  $rows += [pscustomobject]@{
    baseUrl = $baseUrl
    localPeerId = $payload.localPeerId
    totalPeers = $payload.totalPeers
    discoveredPeers = @($payload.items).Count
    uniqueCapabilities = @($payload.capabilityUniverse).Count
    capturedAtUtc = [DateTimeOffset]::UtcNow.ToString('O')
  }
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
  $OutputPath = Join-Path $scriptDir ("capability-matrix_{0}.json" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
}

$rows | ConvertTo-Json -Depth 6 | Set-Content $OutputPath
$rows
Write-Host "Saved capability matrix: $OutputPath"
