# Engine Interop Tester AGENTS

- Repo rules for `engine-interop-tester`.
- Interop diagnostics and test harnesses must extend the canonical compliance and evidence backbone instead of creating parallel event formats.

## Engine Control Platform Governance

- This repo is governed by `D:/workbench/docs/architecture/engine-control-platform-boundary-spec-v1.md`, `D:/workbench/docs/contracts/engine-lifecycle-state-machine-v1.md`, `D:/workbench/docs/contracts/engine-sdk-compatibility-policy-v1.md`, `D:/workbench/docs/contracts/sdk-surface-governance-v1.md`, `D:/workbench/docs/architecture/control-plane-transport-strategy-v1.md`, and `D:/workbench/docs/contracts/control-plane-contract-startup-policy-v1.md`. Treat those workbench docs as authoritative here.
- New work must align to the single Control Plane model. Do not introduce parallel control-plane semantics or alternate control-plane-like surfaces.
- Lifecycle state is the only readiness truth. `SessionReady` is the canonical app-ready state unless a narrower contract is explicitly defined later.
- Do not add new polling-based readiness logic except as explicitly labeled temporary compatibility bridging.
- New startup, discovery, or readiness semantics must not be introduced in app/UI code, repo-local convenience layers, or adapters; keep that behavior in the Control Plane, App Host / Platform Shell, and Control SDK model.
- App/UI layers in or consuming this repo must not gain new direct dependencies on engine implementation projects; move consumption toward Control SDK or stable contract boundaries.
- Stable platform behavior must not be defined by transport-specific quirks or assumptions.
- For a given scope, assume one canonical engine authority shared by multiple clients, not one engine per app by default.
- Legacy mechanisms may be bridged temporarily but must not be expanded.
- Conflicting changes are architecture defects and must be called out explicitly in review.

## Compliance & Evidence Requirements

- Every meaningful action must produce a `ComplianceEventRecord`.
- All identifiers must use canonical types: `ActorId`, `TenantId`, `SessionId`, `DeploymentId`, `EnvironmentId`, `ResourceId`, `ArtifactId`, `PolicyDecisionId`, and `ComplianceEventId`.
- Attach `traceparent` and `correlation_id` when they are available.
- No new logging systems or logging formats may be introduced outside the canonical event spine.
- Do not create new audit systems, identity or tenant models, or evidence storage formats when the canonical backbone can be extended instead.
- Policy decisions must be recorded using `PolicyDecisionRecord`.
- Evidence must be referenced, not duplicated.
- Large payloads must never be written on hot paths.
- Async and non-blocking event emission is required for runtime event producers.
- Review checklist: compliance event, canonical IDs, policy decision, trace linkage, hot-path safety.
- If a feature cannot comply yet, document the gap and propose a compliant alternative before completion.
- Non-compliant code is considered incomplete.
