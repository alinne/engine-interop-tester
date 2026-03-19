function Get-PlaneABootstrapExchangePath {
  '/v1/auth/bootstrap/exchange'
}

function Get-PlaneADeveloperBootstrapIssuePath {
  '/v1/auth/bootstrap/dev/issue'
}

function Get-PlaneAClusterCapabilitiesPath {
  '/v1/interop/cluster/capabilities'
}

function Get-PlaneAClockAuthorityRequestPath {
  param([Parameter(Mandatory)] [string]$PeerId)

  "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/authority/request"
}

function Get-PlaneAClockAuthorityReleasePath {
  param([Parameter(Mandatory)] [string]$PeerId)

  "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/authority/release"
}

function Get-PlaneAClockAuthorityRespondPath {
  param([Parameter(Mandatory)] [string]$PeerId)

  "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/authority/respond"
}

function Get-PlaneAClockAuthorityStatusPath {
  param([Parameter(Mandatory)] [string]$PeerId)

  "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/authority/status"
}

function Get-PlaneAClockSyncApplyPath {
  param([Parameter(Mandatory)] [string]$PeerId)

  "/v1/interop/peers/$([System.Uri]::EscapeDataString($PeerId))/clock/sync/apply"
}
