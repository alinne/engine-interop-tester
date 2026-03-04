param(
  [string]$BaseUrl = 'https://127.0.0.1:5109',
  [string]$TlsPin,
  [Parameter(Mandatory)] [string]$BearerToken,
  [Parameter(Mandatory)] [string]$PeerId,
  [Parameter(Mandatory)] [string]$ExecutionSessionId,
  [long]$ExternalTick = 120
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'lib\interop-http.ps1')

$client = New-InteropHttpClient -BaseUrl $BaseUrl -TlsPin $TlsPin

$authority = Invoke-InteropJson -Client $client -Method POST -Path "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/authority/request" -BearerToken $BearerToken -Body @{
  executionSessionId = $ExecutionSessionId
  autoAccept = $true
}

$sync = Invoke-InteropJson -Client $client -Method POST -Path "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/sync/apply" -BearerToken $BearerToken -Body @{
  executionSessionId = $ExecutionSessionId
  externalTick = $ExternalTick
  externalTimeNs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000
  reasonCode = 'script_sync'
}

$status = Invoke-InteropJson -Client $client -Method POST -Path "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/authority/status" -BearerToken $BearerToken -Body @{
  executionSessionId = $ExecutionSessionId
}

[pscustomobject]@{
  baseUrl = $BaseUrl
  peerId = $PeerId
  executionSessionId = $ExecutionSessionId
  authority = $authority
  sync = $sync
  status = $status
} | ConvertTo-Json -Depth 16
