# Google Drive diagnostic app

This local browser app consumes Engine's connection routes. It is separate from Vault and does
not host Engine, implement Google OAuth, retain downloads, or define shared connection behavior.
Start it with Python 3.11 or newer; there are no dependencies to install:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 src/drive-diagnostics/server.py
```

Open `http://127.0.0.1:8765`. Without Engine configuration, the UI opens but every action returns
`diagnostic_configuration_required`; this is layout inspection, not a simulated connection.

Supply configuration through the launcher's environment, never URL parameters or browser storage:

- `ENGINE_BASE_URL`: the selected Engine origin, HTTPS for remote hosts; local HTTP is limited to
  explicit loopback addresses. No credentials, path, query or fragment in the origin.
- `ENGINE_CERTIFICATE_SHA256`: required for remote HTTPS; the approved Engine TLS leaf certificate
  SHA-256 pin. Normal CA and hostname validation also remain enabled.
- `ENGINE_ACCESS_TOKEN`: an authorized Engine access token, not a Google access token.
- `ENGINE_CONSUMER_ID`: the authorized tester consumer identity registered with Engine.
- `ENGINE_PRODUCT_ID`, `ENGINE_ENVIRONMENT_ID`: the configured Engine OAuth app registration.
- `ENGINE_TENANT_ID`, `ENGINE_WORKSPACE_ID`, `ENGINE_USER_ID`: the existing authorized owner scope.

Do not paste secrets into this document, Git, screenshots, shell command history or chat. Use the
existing secret manager or launcher. The app never registers raw Google credentials. TLS validation
remains enabled, redirects are rejected, environment proxies are disabled, and responses are bounded.

## Current executable scope

1. Check Google OAuth application readiness through Engine.
2. Start Engine-owned authorization, follow the validated Google consent link, return here and check
   the Engine-owned callback result. A fixed loopback return relays the callback to Engine;
   this app performs no provider token exchange.
3. Load the owner's operational Google Drive connections. A callback's connected record alone is not
   treated as proof that the operational connection registry is ready.
4. Select a connection and list folders and files using Engine's currently available legacy routes.
   Returned continuation cursors are shown as incomplete listings; no complete-inventory claim follows.
5. Select one ISO/DICOM candidate and inspect acquisition availability. Acquisition fails explicitly
   until a qualified Engine retained-source operation is exposed. The unsafe legacy public import path
   is deliberately not invoked to manufacture a successful file-opening demonstration.

Dedicated My Drive, Shared with me and Shared drives views, continuation, verified acquisition,
progress/cancel/retry/recovery, source release, and ISO/DICOM opening remain Engine integration gaps.
Those disabled/unavailable paths are requirements, not completed implementation.

## Contract authority and proof

`src/drive-diagnostics/contracts.json` is a generated consumer projection of seven Engine route
declarations. It retains the selected source commit and SHA-256 for the three source files. Refresh
only from committed Engine source, review the projection, then run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/ops/Sync-DriveContracts.py --engine-root /path/to/engine
PYTHONDONTWRITEBYTECODE=1 python3 scripts/ops/Sync-DriveContracts.py --engine-root /path/to/engine --check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/drive-diagnostics
node --check src/drive-diagnostics/web/app.js
```

The repository verifier always compares the projection against its recorded immutable Engine commit.
Supply `-EngineRoot` or `ENGINE_CONTRACT_SOURCE_ROOT` outside the normal Workbench layout. It does
not change or read uncommitted Engine source to verify that pinned input.

Browse operations are catalogued **internal** by the selected Engine contract. This internal tester
does not promote them to a stable consumer SDK. Use only an Engine host that deliberately exposes
them to the authorized scope. A missing route, failed authorization or HTTP-200 semantic failure
remains a failure. The new consumer uses contract bindings because the selected Control SDK lacks
these operations; SDK parity remains Engine-owned work.

The local action schema is presentation-only. Requests inject the fixed configured owner scope;
connections and collections must have been returned for that scope and connection. Starting a new
sign-in clears previous OAuth state, consent links and account selections before any fallible work;
failed sign-in attempts cannot reuse the previous account bindings. Browser input
cannot select a different tenant, Engine origin, bearer token, OAuth state, route or HTTP method.
The UI displays allowlisted fields without HTML interpolation. No request/response files are saved.

**Compliance gap:** the current Drive browse/OAuth route set does not expose a qualified canonical
`ComplianceEventRecord` delivery contract to this consumer. Do not add a local event schema or logging
backbone. Engine must emit canonical events and policy decisions at its existing authorization and
operation boundaries and expose references/trace linkage before this harness can claim conformance.
This baseline is incomplete under the repository's compliance requirement. The compliant next slice
belongs to Engine; it must include canonical actor/tenant/session IDs and correlate the actual
operation, without recording tokens, provider bodies or private file content.

Synthetic HTTP tests prove the harness only. They do not qualify Engine's provider logic or a live
Google account. Live proof still requires configured Google OAuth consent and personal/shared test
locations, synthetic provider failure/recovery tests in Engine, and successful ISO/DICOM opening.

## Ownership and retention

External Connections owns this consumer-only source slice on Mac Studio. Native remains the shared
Engine writer, Platform its boundary reviewer and Merges its publisher; the DICOM-to-image integration
hold is unchanged. This app creates no build, dependency cache, evidence export or retained download.
Tests disable Python bytecode writes and use short-lived loopback servers. Budget: 10 MB mutable test
output, expected near zero. No persistent consumer pins or runtime database. Stop the process to
release sockets and in-memory state. Review the temporary branch on November 19, 2026; retire the
worktree through its supported Git owner only after publication and proof of no unique/unrelated work.
Source recovery is the named committed Git branch, not a disposable output directory.

## Execution boundary

This is a single-user local diagnostic, not an unattended production service. Python owns only
presentation and bounded HTTP orchestration; it implements no media or performance-sensitive Engine
path. Clock authority, scheduling and provider budgets remain Engine-owned; this client makes no LAN
clock or WAN synchronization claim. GPU and NPU execution are not applicable. Each click makes at
most one synchronous Engine request, with a 10-second network timeout and a 1 MiB response ceiling;
there are no background polls or automatic retries. The diagnostic never stores file bytes and has
no paid-service execution route. Operation evidence/replay remains the compliance gap above.

## November 18 baseline checks

The focused verifier passed 32 synthetic tests, JavaScript syntax, consumer-boundary checks and
projection comparison against the selected committed Engine source. Desktop (1280 px) and mobile
(390 px) browser inspection passed, including an actual click returning
`diagnostic_configuration_required` with no configured Engine. No Google account or credentials were
used. This proves the diagnostic baseline, not the unfinished Engine capabilities or live readiness.

## Reusable Google test registration

Use a dedicated Google Cloud project and an External/Testing consent configuration, separate from
Vault's eventual production registration. Enable Drive API and declare only
`https://www.googleapis.com/auth/drive.readonly` for complete accessible-folder browsing and downloads.
Add approved testers to the test-user list. Google Testing authorizations expire after seven days;
re-authorize when required. Do not publish the consent app as part of local testing.

Create a **Web application** OAuth client named `External Connections Local Diagnostic` with exactly
`http://127.0.0.1:8765/oauth/callback/google-drive` as its authorized redirect URI. No JavaScript origin
is needed: the browser never holds the client secret. Keep the downloaded credentials in the existing
private runtime configuration store, outside Git and evidence. Bind Engine's `CloudConnections:OAuthApps`
registration to product `engine-interop-tester`, environment `local`, provider `google-drive`, this
client ID/secret, the exact redirect URI and read-only scope. Vault must use its own registration later.

The HTTP return is restricted to loopback. Ordinary UI/action routes retain strict cookie, Origin,
CSRF and cross-site checks. Only the fixed callback path accepts a cross-site top-level navigation;
it checks the exact Host, bounds and de-duplicates query fields, and passes only state/code/error to
the current Engine connection. Engine-issued state must match the current in-memory sign-in attempt
and is consumed before forwarding, including on an unknown transport outcome. Starting another
sign-in invalidates the previous callback. A failed/unknown callback requires a new sign-in; it is
never retried automatically. Engine still validates provider state, PKCE, expiry and credentials.
The callback sets no session cookie and returns fixed text without provider data. Access logs are
suppressed. Engine transport retains normal TLS validation and any configured certificate pin.

Google permits HTTP loopback redirects for web-app testing:
<https://developers.google.com/identity/protocols/oauth2/web-server>.
Testing limits: <https://support.google.com/cloud/answer/15549945?hl=en>.
This registration and synthetic callback proof do not establish live Drive qualification.

Use HTTPS with normal certificate validation and an explicit certificate pin for live OAuth
communication with Engine. HTTP loopback support exists for synthetic local tests only.
