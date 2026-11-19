from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).parents[2] / "src" / "drive-diagnostics" / "engine_client.py"
SPEC = importlib.util.spec_from_file_location("drive_optional_client", MODULE)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules["drive_optional_client"] = module
SPEC.loader.exec_module(module)
Config = module.Config
DriveClient = module.DriveClient
DriveClientError = module.DriveClientError


OPTIONAL_CONTRACTS = {
    "operations": {
        "browse": {"method": "POST", "path": "/engine/v1/cloud/media:browse"},
        "acquire": {"method": "POST", "path": "/engine/v1/cloud/media:acquire-retained"},
        "revoke": {"method": "POST", "path": "/engine/v1/cloud/connections/{connectionId}/revoke"},
        "inspect": {"method": "GET", "path": "/engine/v1/media/artifacts/{artifactId}"},
        "verify": {"method": "POST", "path": "/engine/v1/media/artifacts/{artifactId}:verify"},
        "graphPrepareBrowse": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/prepare/browse"},
        "graphPrepareAcquire": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/prepare/acquire-retained"},
        "graphSubmitBrowse": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/browse"},
        "graphSubmitAcquire": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/acquire-retained"},
        "graphStatus": {"method": "GET", "path": "/engine/v1/graph-jobs/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/status"},
        "graphCancel": {"method": "POST", "path": "/engine/v1/graph-jobs/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/cancel"},
    }
}


class OptionalDriveCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.contracts = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(OPTIONAL_CONTRACTS, self.contracts)
        self.contracts.close()
        self.addCleanup(lambda: Path(self.contracts.name).unlink(missing_ok=True))
        self.client = DriveClient(
            Config("http://127.0.0.1:8765", "token", "product", "env", "tenant", "workspace", "consumer", user_id="user"),
            self.contracts.name,
        )
        self.client._connection_ids = {"conn-1"}

    def test_missing_projection_is_unavailable_without_network_or_legacy_import(self):
        contracts = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"operations": {}}, contracts)
        contracts.close()
        self.addCleanup(lambda: Path(contracts.name).unlink(missing_ok=True))
        client = DriveClient(self.client.config, contracts.name)
        with mock.patch.object(client, "_request") as request:
            result = client.execute("browse", {"connectionId": "conn-1", "view": "my-drive"})
        self.assertEqual(["capability_unavailable"], result["reasonCodes"])
        request.assert_not_called()

    def test_browse_is_explicit_metadata_only_and_continuation_stays_bound(self):
        response = {
            "isSuccess": True,
            "items": [{
                "selectionRef": "opaque-selection",
                "assetId": "legacy-asset",
                "displayName": "study.dcm",
                "mimeType": "application/dicom",
                "resourceKey": "provider-secret",
                "rawRef": "provider-file-id",
                "localPath": "/private/path",
            }],
            "continuationToken": "opaque-cursor",
        }
        with mock.patch.object(self.client, "_request", return_value=response) as request:
            result = self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "shared_with_me"})
            self.assertTrue(result["isSuccess"])
            self.assertEqual("opaque-cursor", result["continuationToken"])
            self.assertNotIn("resourceKey", json.dumps(result))
            self.assertNotIn("rawRef", json.dumps(result))
            self.assertNotIn("localPath", json.dumps(result))
            self.client.execute("browse", {
                "connectionId": "conn-1", "locationKind": "shared_with_me", "continuationToken": result["continuationToken"]
            })
        self.assertEqual("opaque-cursor", request.call_args.kwargs["body"]["location"]["continuationToken"])
        self.assertEqual("shared_with_me", request.call_args.kwargs["body"]["location"]["locationKind"])

    def test_acquire_requires_asset_returned_by_browse_and_never_auto_acquires(self):
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True, "items": [{"selectionRef": "selection-1"}]}) as request:
            self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            self.assertEqual(1, request.call_count)
            with self.assertRaisesRegex(DriveClientError, "unknown_asset_reference"):
                self.client.execute("acquire", {"connectionId": "conn-1", "assetId": "other"})
            self.assertEqual(1, request.call_count)
            acquired = self.client.execute("acquire", {
                "connectionId": "conn-1", "selectionRef": "selection-1", "artifactId": "artifact-1",
                "maximumBytes": "1000", "publication": {
                    "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                    "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                    "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                    "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-1",
                }
            })
        self.assertEqual("/engine/v1/cloud/media:acquire-retained", request.call_args.args[1])
        self.assertEqual("selection-1", request.call_args.kwargs["body"]["selectionRef"])
        self.assertEqual({"value": "artifact-1"}, request.call_args.kwargs["body"]["artifactId"])
        self.assertNotIn("assetId", request.call_args.kwargs["body"])
        self.assertTrue(acquired["isSuccess"])

    def test_revoke_and_artifact_routes_replace_only_bound_opaque_refs(self):
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True, "retainedArtifactId": "artifact-1"}) as request:
            result = self.client.execute("revoke", {"connectionId": "conn-1"})
            self.assertTrue(result["isSuccess"])
            self.client._artifact_ids = {"conn-1": {"artifact-1"}}
            self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "artifact-1"})
        self.assertEqual("/engine/v1/media/artifacts/artifact-1", request.call_args.args[1])
        self.assertIsNone(request.call_args.kwargs["body"])

        self.client._connection_ids = {"other-account-connection"}
        with self.assertRaisesRegex(DriveClientError, "unknown_connection_reference"):
            self.client.execute("revoke", {"connectionId": "conn-1"})

    def test_artifact_inspect_uses_query_context_and_no_json_body(self):
        self.client._artifact_ids = {"conn-1": {"artifact-1"}}
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True}) as request:
            result = self.client.execute("inspect", {
                "connectionId": "conn-1",
                "retainedArtifactId": "artifact-1",
                "purpose": "vault-diagnostics",
                "sessionId": "session-1",
                "correlationId": "correlation-1",
                "traceparent": "00-abc-def-01",
            })
        self.assertTrue(result["isSuccess"])
        self.assertEqual(
            {"purpose": "vault-diagnostics", "sessionId": "session-1", "correlationId": "correlation-1", "traceparent": "00-abc-def-01"},
            request.call_args.kwargs["query"],
        )
        self.assertIsNone(request.call_args.kwargs["body"])
        self.assertEqual("/engine/v1/media/artifacts/artifact-1", request.call_args.args[1])

    def test_artifact_post_routes_send_only_caller_metadata_and_bind_id_in_path(self):
        self.client._artifact_ids = {"conn-1": {"artifact-1"}}
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True}) as request:
            self.client.execute("verify", {
                "connectionId": "conn-1", "artifactId": "artifact-1", "purpose": "diagnostic"
            })
        self.assertEqual("/engine/v1/media/artifacts/artifact-1:verify", request.call_args.args[1])
        self.assertEqual({"purpose": "diagnostic"}, request.call_args.kwargs["body"])

    def test_browse_rejects_shared_folder_without_location_ref(self):
        with self.assertRaisesRegex(DriveClientError, "browse_location_reference_required"):
            self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "folder"})

    def test_graph_prepare_submit_status_requires_reconciliation_before_new_attempt(self):
        prepare = {
            "isSuccess": True, "invocationRef": "a" * 64, "scopeId": "scope-1",
            "rootJobId": "job-1", "graphHash": "b" * 64, "expiresUtc": "2026-11-19T20:00:00Z",
        }
        submit = {"isSuccess": True, "invocationRef": "a" * 64, "scopeId": "scope-1",
                  "rootJobId": "job-1", "graphHash": "b" * 64, "status": "Running"}
        running_status = {"isSuccess": True, "invocationRef": "a" * 64,
                          "status": {"status": "Running", "nextCursor": {"scope": {"tenantId": "tenant", "scopeId": "scope-1"}}}}
        terminal_status = {"isSuccess": True, "invocationRef": "a" * 64,
                           "status": {"status": "Failed", "nextCursor": {"scope": {"tenantId": "tenant", "scopeId": "scope-1"}}}}
        with mock.patch.object(self.client, "_request", side_effect=[prepare, submit, running_status, terminal_status]) as request:
            self.client.execute("graphPrepareBrowse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
            with self.assertRaisesRegex(DriveClientError, "graph_reconciliation_required"):
                self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
            self.client.execute("graphStatus", {"graphKind": "browse"})
            # A terminal status is not permission to replay the old ticket;
            # the caller must explicitly prepare a fresh graph first.
            self.client.execute("graphStatus", {"graphKind": "browse"})
            with self.assertRaisesRegex(DriveClientError, "graph_retry_requires_new_prepare"):
                self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
        self.assertEqual(
            "/engine/v1/graph-jobs/tenants/tenant/scopes/scope-1/invocations/" + "a" * 64 + "/status",
            request.call_args_list[3].args[1],
        )

    def test_graph_prepare_strips_private_fields_and_rejects_unresolved_replacement(self):
        prepare = {
            "isSuccess": True, "invocationRef": "p" * 64, "scopeId": "scope-prepare",
            "rootJobId": "job-prepare", "graphHash": "q" * 64,
            "expiresUtc": "2026-11-19T20:00:00Z",
        }
        with mock.patch.object(self.client, "_request", return_value=prepare) as request:
            self.client.execute("graphPrepareBrowse", {
                "connectionId": "conn-1", "locationKind": "my_drive_root",
            })
            sent_body = request.call_args.kwargs["body"]
            self.assertNotIn("__graphKind", sent_body)
            self.assertNotIn("__graphTenantId", sent_body)
            self.assertNotIn("__graphScopeId", sent_body)
            self.assertNotIn("__graphInvocationRef", sent_body)

            # A submitted ticket with no terminal status is an unknown outcome;
            # preparing over it would discard the reconciliation handle.
            self.client._graph_bindings["browse"]["submitted"] = True
            self.client._graph_bindings["browse"]["statusTerminal"] = False
            with self.assertRaisesRegex(DriveClientError, "graph_reconciliation_required"):
                self.client.execute("graphPrepareBrowse", {
                    "connectionId": "conn-1", "locationKind": "my_drive_root",
                })
        self.assertEqual(1, request.call_count)

    def test_graph_submit_transport_failure_consumes_ticket_until_reconciliation(self):
        prepare = {
            "isSuccess": True, "invocationRef": "s" * 64, "scopeId": "scope-submit",
            "rootJobId": "job-submit", "graphHash": "t" * 64,
            "expiresUtc": "2026-11-19T20:00:00Z",
        }
        with mock.patch.object(
            self.client, "_request", side_effect=[prepare, DriveClientError("engine_unreachable")]
        ) as request:
            self.client.execute("graphPrepareBrowse", {
                "connectionId": "conn-1", "locationKind": "my_drive_root",
            })
            with self.assertRaisesRegex(DriveClientError, "engine_unreachable"):
                self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
            with self.assertRaisesRegex(DriveClientError, "graph_reconciliation_required"):
                self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
        self.assertEqual(2, request.call_count)

    def test_graph_submit_only_completed_status_is_success_and_failed_acquire_never_recovers(self):
        base = {
            "invocationRef": "u" * 64, "scopeId": "scope-status", "rootJobId": "job-status",
            "graphHash": "v" * 64, "output": {"artifactId": "must-not-bind"}, "httpStatus": 200,
        }
        for execution_status in ("Failed", "Cancelled", "CompletedWithFailures", "UnknownStatus"):
            with self.subTest(execution_status=execution_status):
                client = DriveClient(self.client.config, self.contracts.name)
                client._connection_ids = {"conn-1"}
                client._selection_refs["conn-1"] = {"selection-1"}
                prepare = {
                    "isSuccess": True, "invocationRef": "u" * 64, "scopeId": "scope-status",
                    "rootJobId": "job-status", "graphHash": "v" * 64,
                    "expiresUtc": "2026-11-19T20:00:00Z",
                }
                with mock.patch.object(client, "_request", side_effect=[prepare, {**base, "status": execution_status}]):
                    client.execute("graphPrepareAcquire", {
                        "connectionId": "conn-1", "selectionRef": "selection-1", "artifactId": "requested",
                        "maximumBytes": 1000, "publication": {
                            "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                            "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                            "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                            "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-status",
                        },
                    })
                    result = client.execute("graphSubmitAcquire", {"graphKind": "acquire"})
                self.assertFalse(result["isSuccess"])
                self.assertNotIn("retainedArtifactId", result)
                self.assertNotIn("must-not-bind", client._artifact_ids.get("conn-1", set()))

        completed = DriveClient._normalize_published_shape(
            "graphSubmitBrowse", {
                "httpStatus": 200, "invocationRef": "w" * 64, "scopeId": "scope-status",
                "rootJobId": "job-status", "graphHash": "x" * 64, "status": "Completed",
            }
        )
        self.assertTrue(completed["isSuccess"])

    def test_graph_cancel_sends_only_idempotency_intent_and_uses_bound_ticket(self):
        prepare = {"isSuccess": True, "invocationRef": "c" * 64, "scopeId": "scope-2",
                   "rootJobId": "job-2", "graphHash": "d" * 64, "expiresUtc": "2026-11-19T20:00:00Z"}
        response = {"isSuccess": True, "operation": "cancel", "status": "Cancelling"}
        with mock.patch.object(self.client, "_request", side_effect=[prepare, response]) as request:
            self.client.execute("graphPrepareBrowse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            cancel_result = self.client.execute("graphCancel", {"graphKind": "browse", "idempotencyKey": "cancel-1"})
        self.assertEqual("/engine/v1/graph-jobs/tenants/tenant/scopes/scope-2/invocations/" + "c" * 64 + "/cancel", request.call_args_list[1].args[1])
        self.assertEqual({"idempotencyKey": "cancel-1"}, request.call_args_list[1].kwargs["body"])
        self.assertEqual("cancel", cancel_result["operation"])
        self.assertEqual(["Cancelling"], cancel_result["graphStatuses"])

    def test_graph_cancel_requires_bounded_idempotency_intent(self):
        self.client._graph_bindings["browse"] = {"scopeId": "scope", "invocationRef": "e" * 64}
        with self.assertRaisesRegex(DriveClientError, "graph_cancel_idempotency_key_required"):
            self.client.execute("graphCancel", {"graphKind": "browse"})

    def test_graph_browse_output_projects_bound_page_and_remembers_selection(self):
        invocation = "f" * 64
        prepare = {"isSuccess": True, "invocationRef": invocation, "scopeId": "scope-browse",
                   "rootJobId": "job-browse", "graphHash": "a" * 64, "expiresUtc": "2026-11-19T20:00:00Z"}
        submit = {
            "isSuccess": True, "invocationRef": invocation, "scopeId": "scope-browse",
            "rootJobId": "job-browse", "graphHash": "a" * 64, "status": "Completed",
            "output": {
                "isSuccess": True, "connectionId": "conn-1",
                "result": {
                    "isSuccess": True,
                    "collections": [{"collectionRef": "folder-1", "displayName": "Shared", "resourceKey": "secret"}],
                    "assets": [{"selectionRef": "selection-graph", "displayName": "study.dcm",
                                "mimeType": "application/dicom", "sizeBytes": 64, "rawRef": "provider-id"}],
                    "continuationToken": "cursor-graph",
                    "providerPath": "/private/provider/path",
                },
            },
        }
        with mock.patch.object(self.client, "_request", side_effect=[prepare, submit]):
            self.client.execute("graphPrepareBrowse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            result = self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
        self.assertTrue(result["isSuccess"])
        self.assertEqual("cursor-graph", result["continuationToken"])
        self.assertEqual("selection-graph", result["assets"][0]["selectionRef"])
        self.assertNotIn("resourceKey", json.dumps(result))
        self.assertNotIn("rawRef", json.dumps(result))
        self.assertNotIn("providerPath", json.dumps(result))
        self.assertIn("selection-graph", self.client._selection_refs["conn-1"])

    def test_graph_acquire_output_remembers_only_matching_bound_artifact(self):
        invocation = "1" * 64
        self.client._selection_refs["conn-1"] = {"selection-graph"}
        prepare = {"isSuccess": True, "invocationRef": invocation, "scopeId": "scope-acquire",
                   "rootJobId": "job-acquire", "graphHash": "b" * 64, "expiresUtc": "2026-11-19T20:00:00Z"}
        submit = {"isSuccess": True, "invocationRef": invocation, "scopeId": "scope-acquire",
                  "rootJobId": "job-acquire", "graphHash": "b" * 64, "status": "Completed",
                  "output": {"artifactId": "retained-graph"}}
        payload = {"connectionId": "conn-1", "selectionRef": "selection-graph", "artifactId": "requested-artifact",
                   "maximumBytes": 1000, "publication": {"fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                   "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                   "consentRequiredPurposes": [], "retentionPolicyId": "policy:test", "retentionClass": "Tier1_Operational",
                   "idempotencyKey": "attempt-graph"}}
        with mock.patch.object(self.client, "_request", side_effect=[prepare, submit, {"isSuccess": True}]) as request:
            self.client.execute("graphPrepareAcquire", payload)
            result = self.client.execute("graphSubmitAcquire", {"graphKind": "acquire"})
            self.assertTrue(result["isSuccess"])
            self.assertEqual("retained-graph", result["retainedArtifactId"])
            self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "retained-graph"})
        self.assertEqual("/engine/v1/media/artifacts/retained-graph", request.call_args.args[1])

        self.client = DriveClient(self.client.config, self.contracts.name)
        self.client._connection_ids = {"conn-1"}
        self.client._graph_bindings["acquire"] = {"connectionId": "conn-1", "tenantId": "tenant",
                                                   "scopeId": "scope-acquire", "invocationRef": invocation,
                                                   "requestBody": {"connectionId": "conn-1"},
                                                   "submitted": False, "statusTerminal": False}
        mismatched = {"isSuccess": True, "invocationRef": "2" * 64, "scopeId": "scope-acquire",
                      "status": "Completed", "output": {"artifactId": "wrong-artifact"}}
        with mock.patch.object(self.client, "_request", return_value=mismatched):
            result = self.client.execute("graphSubmitAcquire", {"graphKind": "acquire"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
        self.assertNotIn("retainedArtifactId", result)
        self.assertFalse(self.client._graph_bindings["acquire"]["statusTerminal"])
        with self.assertRaisesRegex(DriveClientError, "unknown_artifact_reference"):
            self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "wrong-artifact"})

    def test_graph_status_recovers_bound_retained_handle_and_redacts_job_tree(self):
        invocation = "3" * 64
        self.client._graph_bindings["acquire"] = {"connectionId": "conn-1", "tenantId": "tenant",
                                                   "scopeId": "scope-status", "invocationRef": invocation,
                                                   "submitted": True, "statusTerminal": False}
        response = {"httpStatus": 200, "invocationRef": invocation, "status": {"status": "Completed",
            "projection": {"tenantId": "tenant", "scopeId": "scope-status", "jobs": [{"currentState": "succeeded", "artifacts": [{"payloadSchemaId": "external-media.retained-handle.v1",
                "artifactId": "recovered-artifact", "contentHash": "sha256:abc", "byteSize": 4096,
                "providerPath": "/must-not-escape"}]}]},
            "nextCursor": {"scope": {"tenantId": "tenant", "scopeId": "scope-status"}}}}
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("graphStatus", {"graphKind": "acquire"})
        self.assertTrue(result["isSuccess"])
        self.assertEqual("recovered-artifact", result["retainedArtifactId"])
        self.assertEqual(4096, result["retainedArtifact"]["byteSize"])
        self.assertNotIn("providerPath", json.dumps(result))
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True}):
            self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "recovered-artifact"})

    def test_graph_status_wrong_scope_does_not_recover_artifact(self):
        invocation = "4" * 64
        self.client._graph_bindings["acquire"] = {"connectionId": "conn-1", "tenantId": "tenant",
                                                   "scopeId": "scope-status", "invocationRef": invocation,
                                                   "submitted": True, "statusTerminal": False}
        response = {"httpStatus": 200, "invocationRef": invocation, "status": {"status": "Completed",
            "projection": {"tenantId": "another-tenant", "scopeId": "scope-status", "jobs": [{"artifacts": [{"payloadSchemaId": "external-media.retained-handle.v1",
                "artifactId": "cross-account-artifact", "contentHash": "sha256:bad", "byteSize": 1}]}]},
            "nextCursor": {"scope": {"tenantId": "another-tenant", "scopeId": "scope-status"}}}}
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("graphStatus", {"graphKind": "acquire"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
        self.assertNotIn("retainedArtifactId", result)
        with self.assertRaisesRegex(DriveClientError, "unknown_artifact_reference"):
            self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "cross-account-artifact"})

    def test_graph_status_failed_terminal_never_recovers_retained_handle(self):
        for terminal_status in ("Failed", "Cancelled", "CompletedWithFailures"):
            with self.subTest(terminal_status=terminal_status):
                invocation = "5" * 64
                client = DriveClient(self.client.config, self.contracts.name)
                client._graph_bindings["acquire"] = {
                    "connectionId": "conn-1", "tenantId": "tenant", "scopeId": "scope-status",
                    "invocationRef": invocation, "submitted": True, "statusTerminal": False,
                }
                response = {
                    "httpStatus": 200, "invocationRef": invocation,
                    "status": {
                        "status": terminal_status,
                        "projection": {
                            "tenantId": "tenant", "scopeId": "scope-status",
                            "jobs": [{"currentState": "failed", "artifacts": [{
                                "payloadSchemaId": "external-media.retained-handle.v1",
                                "artifactId": "must-not-recover", "contentHash": "sha256:bad", "byteSize": 1,
                            }]}],
                        },
                        "nextCursor": {"scope": {"tenantId": "tenant", "scopeId": "scope-status"}},
                    },
                }
                with mock.patch.object(client, "_request", return_value=response):
                    result = client.execute("graphStatus", {"graphKind": "acquire"})
                self.assertFalse(result["isSuccess"])
                self.assertNotIn("retainedArtifactId", result)
                self.assertNotIn("must-not-recover", client._artifact_ids.get("conn-1", set()))

    def test_wrong_scope_failed_status_cannot_settle_ticket(self):
        invocation = "6" * 64
        client = DriveClient(self.client.config, self.contracts.name)
        client._graph_bindings["browse"] = {
            "connectionId": "conn-1", "tenantId": "tenant", "scopeId": "scope-status",
            "invocationRef": invocation, "submitted": True, "statusTerminal": False,
            "requestBody": {"connectionId": "conn-1", "locationKind": "my_drive_root"},
        }
        response = {
            "httpStatus": 200, "invocationRef": invocation,
            "status": {
                "status": "Failed",
                "projection": {
                    "tenantId": "attacker-tenant", "scopeId": "scope-status",
                    "jobs": [{"currentState": "failed", "artifacts": []}],
                },
                "nextCursor": {"scope": {"tenantId": "attacker-tenant", "scopeId": "scope-status"}},
            },
        }
        with mock.patch.object(client, "_request", return_value=response):
            result = client.execute("graphStatus", {"graphKind": "browse"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
        self.assertTrue(client._graph_bindings["browse"]["submitted"])
        self.assertFalse(client._graph_bindings["browse"]["statusTerminal"])
        with self.assertRaisesRegex(DriveClientError, "graph_reconciliation_required"):
            client.execute("graphPrepareBrowse", {
                "connectionId": "conn-1", "locationKind": "my_drive_root",
            })


if __name__ == "__main__":
    unittest.main()
