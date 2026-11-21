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
4. Select a connection and browse My Drive, Shared with me, Shared drives, or a selected shared-drive
   folder through the optional canonical browse projection. Continuation cursors are sent back only
   with the same connection and view. The legacy folder/file routes remain available for comparison.
5. Select one ISO/DICOM candidate and explicitly acquire it through the retained-source operation,
   then inspect, verify, materialize, tombstone, or permanently delete the returned opaque artifact
   reference when those optional routes are projected. After a reload, the retained-artifact ID
   field can be used to request the same lifecycle operations; the client validates the opaque ID
   before constructing a route, and Engine checks access against the authenticated owner scope.
   The selected Drive connection is the browse/acquisition context, not a retained-artifact
   authorization binding. Tombstone and delete require an explicit purpose and idempotency key;
   permanent delete additionally requires the fixed UI confirmation. The UI never auto-acquires,
   opens bytes, or falls back to the legacy public import path. Provider resource keys, raw refs,
   local paths and content are removed from the browser projection.
6. Prepare and explicitly submit browse/acquire graphs using Engine-issued invocation handles.
   Reconcile status, watch from an Engine cursor, request cancellation with an idempotency key, and
   recover the opaque retained artifact descriptor from durable job status. After a reload, status
   and watch accept only the explicitly entered scope and invocation handle; watch cursors are
   scope-bound, bounded, and held only in process memory. A retry requires status reconciliation
   and a fresh prepare; the tester never automatically resubmits an uncertain import. Sanitized
   transport failures remain failures even when a host returns HTTP 200 with an unsuccessful
   semantic result. A successful response with a mismatched invocation or connection binding is
   rejected and cannot restore a retained artifact; an HTTP failure without an invocation binding
   preserves the original Engine reason instead of being rewritten as a local binding mismatch.
7. Revoke a selected connection only after an explicit confirmation.

The pinned committed Engine contract projects 23 operations, including graph prepare, submit,
status, watch, cancel and retained-artifact access. Refresh the projection only from the reviewed
committed Engine source and keep its source commit and hashes together. An unavailable host
capability remains `capability_unavailable`;
projected routes do not prove runtime qualification. Verified reads are consumed server-side with
bounded streaming and return only a byte count and SHA-256 hash. The diagnostic never renders medical content or
implements ISO/DICOM parsing; those remain consumer application responsibilities.

## Catalog handoff boundary

The Engine `media-device dicom-catalog` command is the canonical catalog handoff for an
authorized retained artifact. Its request binds the artifact ID, revision and source SHA-256;
the CLI validates the response with Engine's catalog contract before returning metadata.
This command exists in the committed Engine source pinned by this tester. It requires an
explicitly selected, already-built CLI, a pinned HTTPS Engine origin and an Engine token file.
It does not start Engine, build a CLI, acquire Google credentials or grant artifact access.

The standalone POSIX adapter projects only counts and status, discards private metadata,
and removes its private temporary request document after confirmed process-group cessation:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 src/drive-diagnostics/catalog_cli.py \
  --executable /approved/path/engine --executable-sha256 APPROVED_BINARY_SHA256 \
  --base-url https://engine.example --certificate-sha256 ENGINE_CERTIFICATE_SHA256 \
  --token-file /approved/private/token-file --artifact-id RETAINED_ARTIFACT_ID \
  --revision 1 --source-sha256 RETAINED_SOURCE_SHA256
```

Use the actual retained artifact revision and digest, and the approved binary's digest from
its qualified publication. The executable checksum is an invocation pin, not proof of its
dependency closure or source provenance. The existing token file must be private and remains
owned by its credential provider. Tokens never enter arguments, output or the child environment.
Only the explicit executable runs; no shell, PATH search, automatic build or host startup occurs.
The adapter caps stdout at 16 MiB, discards stderr and has a finite process deadline plus a
five-second settlement bound. It requires POSIX `waitid/WNOWAIT` and default `SIGCHLD` handling
to retain the leader identity until the last group signal. It joins that child and observes group
absence before deleting the request. This is custody of the trusted executable's process group;
executables that detach children into other groups/sessions require a supported host supervisor
and are outside this adapter's qualification. It does not prove arbitrary descendant closure.
An `engine_cli_close_uncertain` failure takes precedence over success, timeout or cancellation.
It retains the `engine-catalog-request-*` private directory (`0700`, request `0600`) in the
system temporary root for the invoking operator's recovery; it does not print private paths.
In-process callers receive the directory and original process object on the exception. Preserve
that custody and review within 24 hours; retire through its supported owner only after actual
closure is established. No automatic retry or cleanup is authorized by the error.
An interrupted exit observation retries within the invocation deadline. If the observer fails
irrecoverably, the adapter refuses further numeric group signals because ownership may be lost.
The standalone CLI cannot retain its in-memory process owner after exiting in that state;
recovery requires the invoking host's supported owner/watchdog. Use the in-process interface
with that owner when recoverable lifetime custody is required. Request retention alone is not
proof of a joined process, and this diagnostic is not a production process supervisor.
Synthetic
adapter tests exercise that boundary; running them does not qualify a real Engine/Google/medical
host. The browser UI does not automatically invoke this standalone command.

Catalog acceptance is metadata evidence only. It does not prove pixel decoding, display,
successful ISO opening or live Google Drive qualification. The medical owner retains those
execution and qualification boundaries. A failed command, timeout or malformed response must
remain a failed handoff; no fallback parser or local file opening is permitted here.

## Contract authority and proof

Before acquisition, an Engine administrator must register a retention policy in the selected tenant
through Engine's authenticated privacy retention-policy API. Enter its exact policy ID and matching
retention class in the form; a policy name in this UI does not create a policy. Publication requires
the canonical `storage` purpose as well as the purpose used for subsequent inspection and reads.
The form includes both purposes by default and leaves the policy ID empty to avoid implying that
an unprovisioned policy exists. Keep diagnostic retention finite and separate from production policy.

For download proof, record the successful acquisition's byte size and SHA-256, then click
**Verified read (discard bytes)**. Its complete streamed byte count and SHA-256 must match the
publication. This proves the bytes crossed both Drive-to-Engine and Engine-to-consumer boundaries
without displaying content or saving a second copy. HTTP success alone is insufficient. A failed
graph must be reconciled before preparing a new attempt; use a fresh publication idempotency key
when changing publication metadata.

`src/drive-diagnostics/contracts.json` is a generated consumer projection of the seven baseline Engine
route declarations plus any optional browse/retained-artifact declarations present in the selected
committed source. It retains the selected source commit and SHA-256 for every projected source file.
Refresh only from committed Engine source, review the projection, then run:

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
remains a failure. The Python consumer uses generated contract bindings. Engine owns the corresponding typed .NET SDK
and CLI operations; this tester does not recreate their shared semantics.

The local action schema is presentation-only. Requests inject the fixed configured owner scope;
connections and collections must have been returned for that scope and connection. Starting a new
sign-in clears previous OAuth state, consent links and account selections before any fallible work;
failed sign-in attempts cannot reuse the previous account bindings. Browser input
cannot select a different tenant, Engine origin, bearer token, OAuth state, route or HTTP method.
The UI displays allowlisted fields without HTML interpolation. No request/response files are saved.

**Compliance dependency:** meaningful Engine operations use the canonical event and policy sinks in
Engine. The current Engine readiness candidate adds validated actor/tenant/session identity and
scope-decision linkage to the existing HTTP completion event, plus cloud-media allow/deny events.
Custody operations retain their existing canonical lifecycle evidence. This consumer adds no local
event schema or logging backbone and must not record tokens, provider bodies or private content.
The metadata-only live acceptance audit confirmed seven cloud-access events linked to matching
actor, tenant, session, resource, correlation and policy decisions. Engine publication and reviewed
production qualification remain pending; this local synthetic-profile observation does not qualify
retained file acquisition or production conformance.

Synthetic HTTP tests prove the harness only. They do not qualify Engine's provider logic or a live
Google account. Live proof still requires configured Google OAuth consent and personal/shared test
locations, synthetic provider failure/recovery tests in Engine, and successful ISO/DICOM opening.

## Ownership and retention

External Connections owns this consumer-only source slice and its isolated Engine readiness candidate
on Mac Studio. Vault integration and the DICOM-to-image handoff remain separate; no Native, Platform
or Merges queue is dispatched from this diagnostic work. This app creates no build, dependency cache,
evidence export or retained download.
Tests disable Python bytecode writes and use short-lived loopback servers. Budget: 10 MB mutable test
output, expected near zero. No persistent consumer pins or runtime database. Stop the process to
release sockets and in-memory state. Review the temporary branch on November 20, 2026; retire the
worktree through its supported Git owner only after publication and proof of no unique/unrelated work.
Source recovery is the named committed Git branch, not a disposable output directory.

## Execution boundary

This is a single-user local diagnostic, not an unattended production service. Python owns only
presentation and bounded HTTP orchestration; it implements no media or performance-sensitive Engine
path. Clock authority, scheduling and provider budgets remain Engine-owned; this client makes no LAN
clock or WAN synchronization claim. GPU and NPU execution are not applicable. Each click makes at
most one synchronous Engine request. JSON responses have a 10-second network timeout and a 1 MiB
response ceiling. Verified binary reads require a declared length of at most 64 GiB, stream in
64 KiB chunks, and have a cooperative ten-minute total deadline. Short and overlong responses fail;
bytes are hashed and discarded rather than accumulated. There are no background polls or automatic
retries. The diagnostic never stores file bytes and has
no paid-service execution route. Operation evidence remains subject to the Engine compliance dependency above.

## November 18 baseline checks

The focused verifier passed 48 synthetic tests, JavaScript syntax, consumer-boundary checks and
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


## November 19 readiness candidate checks

The current focused verifier record passes 98 synthetic tests plus 60 subtests, JavaScript syntax, committed-source route projection,
shell boundary and API-boundary checks. Source review confirms account/scope validation precedes
terminal settlement, uncertain submits cannot be blindly replayed, active tickets cannot be replaced,
and only successful bound graph output may restore retained artifacts. The 1,000-line transport
adapter remains one cohesive local diagnostic state machine during qualification; extract graph
state and verified streaming helpers before adding another provider or operation family. This
bounded disposition does not authorize a parallel provider, storage or audit implementation.

The projection is pinned to Engine `61f7a6345fe940153a51bbaa037071c9d60aed0f`. Browser acceptance
renewed the existing read-only Google authorization, prepared and explicitly submitted a browse
graph, observed successful root and node settlement, and recovered the same successful invocation
after a page reload and an Engine restart without resubmission. A stale browser session was denied
until refreshed. An expired connection correctly reported refresh and graph failure. This is live
metadata-only proof under a local synthetic admission profile; no Drive content was downloaded or
modified. Representative non-patient ISO/DICOM acquisition and production qualification remain open.

## November 19 live download proof

The alternate approved Google account successfully acquired an accessible shared ISO into Engine
retained storage: **82,982,912 bytes**, with SHA-256
`ca9dce01d3df49dea8ef143fb56290f9833571e0549853d7f4c3868fe3e34160`.
The diagnostic then streamed all **82,982,912 bytes** back through Engine's verified-read route,
computed the same SHA-256, and discarded the transferred bytes. No content was rendered and the
Google source was not modified. Selected proof is retained in the owning External Connections
evidence collection; credentials and runtime databases remain in the private runtime store.

This was a local diagnostic using the candidate Engine build and synthetic admission profile.
It proves live retained download and complete consumer read; it does not establish production
qualification, ISO/DICOM parsing, or Vault integration. The run also exposed missing local retention
policy configuration, a missing publication `storage` purpose, and consumer graph/verification
projection issues, which must remain distinct from provider-download failures.
