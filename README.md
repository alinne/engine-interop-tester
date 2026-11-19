# Engine Interop Tester

Consumer connectivity test apps for Engine interop validation across platforms.

This repository is an internal-only validation shell. Reusable interop/session/clock/runtime logic must live in `linnaeus-engine`; this repo should stay focused on test UX, scripts, and evidence capture.

## Targets
- Google Drive: local browser diagnostic (`src/drive-diagnostics`) for owner-scoped connection,
  browse, and retained-artifact lifecycle checks; see [setup, supported actions and remaining
  qualification](docs/drive-diagnostics.md).
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

## Verification
- Run `pwsh ./scripts/verify-shell-boundary.ps1` before landing repo changes.
- The verifier preserves legacy interop/bootstrap route boundaries and requires the new Drive client to consume its generated Engine cloud route projection. Direct Engine implementation references remain prohibited.
- Run `pwsh ./scripts/verify-drive-diagnostics.ps1` for the browser diagnostic. Synthetic transport tests do not qualify live Google behavior.
