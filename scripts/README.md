# Tester Scripts

- `pwsh ./scripts/issue-dev-token.ps1 -BaseUrl https://127.0.0.1:5109 -TlsPin <SHA256_HEX>`
- `pwsh ./scripts/get-cluster-capabilities.ps1 -BaseUrl https://127.0.0.1:5109 -TlsPin <SHA256_HEX> -BearerToken <JWT> [-Capability interop.node.control]`
- `pwsh ./scripts/clock-authority-smoke.ps1 -BaseUrl https://127.0.0.1:5109 -TlsPin <SHA256_HEX> -BearerToken <JWT> -PeerId <peer> -ExecutionSessionId <session>`
- `pwsh ./scripts/cluster-capability-matrix.ps1 -BaseUrls @('https://127.0.0.1:5109','https://127.0.0.1:5110') -TlsPin <SHA256_HEX> -BearerToken <JWT>`
