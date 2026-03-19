param(
  [string]$BaseUrl = 'https://127.0.0.1:5109',
  [string]$TlsPin,
  [string]$Principal = 'developer'
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'lib\interop-http.ps1')
. (Join-Path $scriptDir 'lib\plane-a-routes.ps1')

$client = New-InteropHttpClient -BaseUrl $BaseUrl -TlsPin $TlsPin
$issuePath = Get-PlaneADeveloperBootstrapIssuePath
$issue = Invoke-InteropJson -Client $client -Method POST -Path $issuePath -Body @{ principal = $Principal }
if (-not $issue.bootstrapToken) { throw "bootstrap token missing from $issuePath response" }

$exchangePath = Get-PlaneABootstrapExchangePath
$exchange = Invoke-InteropJson -Client $client -Method POST -Path $exchangePath -Body @{ bootstrapToken = $issue.bootstrapToken }
if (-not $exchange.accessToken) { throw "access token missing from $exchangePath response" }

[pscustomobject]@{
  baseUrl = $BaseUrl
  principal = $Principal
  bootstrapToken = $issue.bootstrapToken
  accessToken = $exchange.accessToken
  expiresAtUtc = $exchange.expiresAtUtc
} | ConvertTo-Json -Depth 6
