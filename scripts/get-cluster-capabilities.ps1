param(
  [string]$BaseUrl = 'https://127.0.0.1:5109',
  [string]$TlsPin,
  [string]$BearerToken,
  [string]$Capability
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'lib\interop-http.ps1')

$client = New-InteropHttpClient -BaseUrl $BaseUrl -TlsPin $TlsPin
$payload = Invoke-InteropJson -Client $client -Method GET -Path '/v1/interop/cluster/capabilities' -BearerToken $BearerToken

if (-not [string]::IsNullOrWhiteSpace($Capability) -and $payload.items) {
  $payload.items = @($payload.items | Where-Object {
    $_.capabilities -and ($_.capabilities | Where-Object { $_ -ieq $Capability })
  })
  $payload.filteredPeers = $payload.items.Count
  $payload.capabilityFilter = $Capability
}

$payload | ConvertTo-Json -Depth 16
