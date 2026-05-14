# Engine Interop Tester

Native connectivity test app for engine interop validation across platforms.

This repository is an internal-only validation shell. Reusable interop/session/clock/runtime logic must live in `linnaeus-engine`; this repo should stay focused on test UX, scripts, and evidence capture.

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

## Verification
- Run `pwsh ./scripts/verify-shell-boundary.ps1` before landing repo changes.
- The verifier enforces that this repo stays on canonical `/v1/interop/*` and `/v1/auth/bootstrap/*` routes and does not grow direct `Linnaeus.Engine*` or `Provinode.Engine*` implementation references.
