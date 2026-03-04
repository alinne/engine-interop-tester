function Normalize-Hex([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
  return -join ($Value.ToCharArray() | Where-Object { [Uri]::IsHexDigit($_) } | ForEach-Object { [char]::ToUpperInvariant($_) })
}

function New-InteropHttpClient {
  param(
    [Parameter(Mandatory)] [string]$BaseUrl,
    [string]$TlsPin
  )

  $baseUri = [System.Uri]::new($BaseUrl, [System.UriKind]::Absolute)
  $handler = [System.Net.Http.HttpClientHandler]::new()

  if ($baseUri.Scheme -ieq [System.Uri]::UriSchemeHttps) {
    $normalizedPin = Normalize-Hex $TlsPin
    if ([string]::IsNullOrWhiteSpace($normalizedPin)) {
      throw 'HTTPS requests require -TlsPin (SHA-256 hex certificate fingerprint).'
    }

    $handler.ServerCertificateCustomValidationCallback = {
      param($requestMessage, $certificate, $chain, $errors)
      if ($null -eq $certificate) { return $false }
      $cert2 = [System.Security.Cryptography.X509Certificates.X509Certificate2]$certificate
      $hashBytes = $cert2.GetCertHash([System.Security.Cryptography.HashAlgorithmName]::SHA256)
      $presented = [System.Convert]::ToHexString($hashBytes)
      return [string]::Equals($presented, $normalizedPin, [System.StringComparison]::Ordinal)
    }
  }

  $client = [System.Net.Http.HttpClient]::new($handler)
  $client.BaseAddress = $baseUri
  $client.Timeout = [System.TimeSpan]::FromSeconds(20)
  return $client
}

function Invoke-InteropJson {
  param(
    [Parameter(Mandatory)] [System.Net.Http.HttpClient]$Client,
    [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string]$Method,
    [Parameter(Mandatory)] [string]$Path,
    [string]$BearerToken,
    [object]$Body
  )

  $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::$Method, $Path)
  if (-not [string]::IsNullOrWhiteSpace($BearerToken)) {
    $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $BearerToken)
  }

  if ($Method -eq 'POST' -and $null -ne $Body) {
    $json = $Body | ConvertTo-Json -Depth 16
    $request.Content = [System.Net.Http.StringContent]::new($json, [System.Text.Encoding]::UTF8, 'application/json')
  }

  $response = $Client.Send($request)
  $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
  if (-not $response.IsSuccessStatusCode) {
    throw "HTTP $([int]$response.StatusCode): $content"
  }

  if ([string]::IsNullOrWhiteSpace($content)) {
    return $null
  }

  return $content | ConvertFrom-Json -Depth 16
}
