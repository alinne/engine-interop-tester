param(
  [string]$BaseUrl = 'https://127.0.0.1:5109',
  [string]$TlsPin,
  [string]$Principal = 'developer'
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'lib\interop-http.ps1')

$client = New-InteropHttpClient -BaseUrl $BaseUrl -TlsPin $TlsPin
$issue = Invoke-InteropJson -Client $client -Method POST -Path '/v1/auth/bootstrap/dev/issue' -Body @{ principal = $Principal }
if (-not $issue.bootstrapToken) { throw 'bootstrap token missing from /v1/auth/bootstrap/dev/issue response' }

$exchange = Invoke-InteropJson -Client $client -Method POST -Path '/v1/auth/bootstrap/exchange' -Body @{ bootstrapToken = $issue.bootstrapToken }
if (-not $exchange.accessToken) { throw 'access token missing from /v1/auth/bootstrap/exchange response' }

[pscustomobject]@{
  baseUrl = $BaseUrl
  principal = $Principal
  bootstrapToken = $issue.bootstrapToken
  accessToken = $exchange.accessToken
  expiresAtUtc = $exchange.expiresAtUtc
} | ConvertTo-Json -Depth 6
