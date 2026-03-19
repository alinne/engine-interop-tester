param(
  [string]$BaseUrl = 'https://127.0.0.1:5109',
  [string]$TlsPin,
  [Parameter(Mandatory)] [string]$BearerToken,
  [Parameter(Mandatory)] [string]$PeerId,
  [Parameter(Mandatory)] [string]$ExecutionSessionId,
  [long]$ExternalTick = 120,
  [switch]$ManualReview,
  [switch]$Reject
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'lib\interop-http.ps1')
. (Join-Path $scriptDir 'lib\plane-a-routes.ps1')

$client = New-InteropHttpClient -BaseUrl $BaseUrl -TlsPin $TlsPin

$authority = Invoke-InteropJson -Client $client -Method POST -Path (Get-PlaneAClockAuthorityRequestPath -PeerId $PeerId) -BearerToken $BearerToken -Body @{
  executionSessionId = $ExecutionSessionId
  autoAccept = -not $ManualReview.IsPresent
}

$decision = $null
if ($ManualReview.IsPresent) {
  $decision = Invoke-InteropJson -Client $client -Method POST -Path (Get-PlaneAClockAuthorityRespondPath -PeerId $PeerId) -BearerToken $BearerToken -Body @{
    executionSessionId = $ExecutionSessionId
    accepted = -not $Reject.IsPresent
    reasonCode = if ($Reject.IsPresent) { 'script_reject' } else { 'script_accept' }
  }
}

$sync = Invoke-InteropJson -Client $client -Method POST -Path (Get-PlaneAClockSyncApplyPath -PeerId $PeerId) -BearerToken $BearerToken -Body @{
  executionSessionId = $ExecutionSessionId
  externalTick = $ExternalTick
  externalTimeNs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000
  reasonCode = 'script_sync'
}

$status = Invoke-InteropJson -Client $client -Method POST -Path (Get-PlaneAClockAuthorityStatusPath -PeerId $PeerId) -BearerToken $BearerToken -Body @{
  executionSessionId = $ExecutionSessionId
}

[pscustomobject]@{
  baseUrl = $BaseUrl
  peerId = $PeerId
  executionSessionId = $ExecutionSessionId
  authority = $authority
  decision = $decision
  sync = $sync
  status = $status
} | ConvertTo-Json -Depth 16
