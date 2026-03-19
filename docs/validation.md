# Engine Interop Tester Validation

## Standard verify path
1. Run repo-local verification:
   `pwsh ./scripts/verify.ps1`
2. On Windows, this verifies the WinUI project build, the macOS Swift package build, the strict internal API guard, and the consumer-only no-local-host rule.
3. On non-Windows hosts, it restores the WinUI project and skips the final WindowsAppSDK/XAML compile step, then continues with the Swift build and guard checks.

## Local single-machine (multi-instance)
1. Run two+ engine instances on distinct ports/runtime directories.
2. Generate auth credentials for instance A:
   `pwsh ./scripts/issue-dev-token.ps1 -BaseUrl https://127.0.0.1:5109 -TlsPin <SHA256_HEX>`
3. Open tester app and set base URL/token/TLS pin.
4. Refresh peers and verify `/v1/interop/cluster/capabilities` includes all discovered nodes and capability sets.
5. Optionally verify capability filtering:
   `pwsh ./scripts/get-cluster-capabilities.ps1 -BaseUrl https://127.0.0.1:5109 -TlsPin <SHA256_HEX> -BearerToken <JWT> -Capability interop.node.control`
6. Execute clock control smoke:
   `pwsh ./scripts/clock-authority-smoke.ps1 -BaseUrl https://127.0.0.1:5109 -TlsPin <SHA256_HEX> -BearerToken <JWT> -PeerId <peer> -ExecutionSessionId <session>`
7. Validate multi-endpoint capability inventory snapshot:
   `pwsh ./scripts/cluster-capability-matrix.ps1 -BaseUrls @('https://127.0.0.1:5109','https://127.0.0.1:5110') -TlsPin <SHA256_HEX> -BearerToken <JWT>`

## Two-machine pass (later)
- Repeat the same on LAN with pinned TLS certs and explicit pairing/consent.
