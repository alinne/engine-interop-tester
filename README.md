# Engine Interop Tester

Native connectivity test app for engine interop validation across platforms.

## Targets
- Windows: WinUI 3 app (`src/windows/EngineInteropTester.WinUI`)
- macOS: SwiftUI app (`src/macos/EngineInteropTester.AppleUX`)

## Primary Test Surface
- Multi-peer capability inventory (`GET /v1/interop/cluster/capabilities`)
- Peer clock authority request/respond/release
- Clock sync apply
- Capability inventory aggregation across many endpoints
- Scripted bootstrap token issuance/exchange

## Design Constraint
- Business logic remains in engine endpoints and shared engine packages.
- UX shells remain thin orchestration layers.
- Encrypted communication is required for remote connectivity lanes; HTTPS requires certificate pin entry in testers.

## Internal API Standard
- Repo-local internal API gate: `pwsh ./scripts/ops/Assert-InternalApiArchitecture.ps1`
- Prefer generated or contract-backed clients over new handwritten route knowledge.
