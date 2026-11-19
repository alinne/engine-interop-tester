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
        "collections": {"method": "POST", "path": "/engine/v1/cloud/collections:list"},
        "assets": {"method": "POST", "path": "/engine/v1/cloud/assets:list"},
        "acquire": {"method": "POST", "path": "/engine/v1/cloud/media:acquire-retained"},
        "revoke": {"method": "POST", "path": "/engine/v1/cloud/connections/{connectionId}/revoke"},
        "inspect": {"method": "GET", "path": "/engine/v1/media/artifacts/{artifactId}"},
        "verify": {"method": "POST", "path": "/engine/v1/media/artifacts/{artifactId}:verify"},
        "delete": {"method": "POST", "path": "/engine/v1/media/artifacts/{artifactId}:delete"},
        "graphPrepareBrowse": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/prepare/browse"},
        "graphPrepareAcquire": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/prepare/acquire-retained"},
        "graphSubmitBrowse": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/browse"},
        "graphSubmitAcquire": {"method": "POST", "path": "/engine/v1/external-media/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/acquire-retained"},
        "graphStatus": {"method": "GET", "path": "/engine/v1/graph-jobs/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/status"},
        "graphWatch": {"method": "POST", "path": "/engine/v1/graph-jobs/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/watch"},
        "graphCancel": {"method": "POST", "path": "/engine/v1/graph-jobs/tenants/{tenantId}/scopes/{scopeId}/invocations/{invocationRef}/cancel"},
    }
}


def observation_cursor(scope_id: str, *, tenant_id: str = "tenant", job_id: str = "job-1", after_sequence: int = 0) -> dict:
    return {
        "contractVersion": 1,
        "scope": {"tenantId": tenant_id, "scopeId": scope_id},
        "jobCursors": [{"schemaVersion": 1, "job": {"jobId": job_id}, "afterSequence": after_sequence}],
    }


def observation_projection(scope_id: str, *, tenant_id: str = "tenant", job_id: str = "job-1",
                           state: str = "running", artifacts: list[dict] | None = None) -> dict:
    return {
        "contractVersion": 1,
        "projectionId": "projection-1",
        "tenantId": tenant_id,
        "scopeId": scope_id,
        "correlationId": "correlation-1",
        "graphSpecId": "graph-1",
        "graphFingerprint": "a" * 64,
        "asOfUtc": "2026-11-19T20:00:00Z",
        "advisoryOnly": True,
        "jobs": [{
            "contractVersion": 1,
            "job": {"jobId": job_id},
            "currentState": state,
            "artifacts": list(artifacts or []),
        }],
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

    def test_browse_projects_only_canonical_download_capability_from_metadata(self):
        response = {
            "isSuccess": True,
            "items": [
                {"displayName": "blocked.bin", "mimeType": "application/octet-stream",
                 "metadata": {"driveCanDownload": "false", "providerSecret": "must-not-leak"}},
                {"displayName": "allowed.bin", "mimeType": "application/octet-stream",
                 "metadata": {"driveCanDownload": "true"}},
                {"displayName": "unknown.bin", "mimeType": "application/octet-stream",
                 "metadata": {"driveCanDownload": "maybe"}},
            ],
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
        assets = result["items"]
        self.assertEqual(False, assets[0]["canDownload"])
        self.assertEqual(True, assets[1]["canDownload"])
        self.assertNotIn("canDownload", assets[2])
        self.assertNotIn("metadata", json.dumps(result))
        self.assertNotIn("providerSecret", json.dumps(result))

    def test_browse_normalizes_canonical_nested_result_and_preserves_inner_failure(self):
        success = {
            "httpStatus": 200,
            "isSuccess": True,
            "connectionId": "conn-1",
            "result": {
                "isSuccess": True,
                "collections": [{"collectionRef": "folder-nested", "displayName": "Shared"}],
                "assets": [{"assetId": "asset-nested", "displayName": "study.dcm"}],
                "continuationToken": "cursor-nested",
            },
        }
        with mock.patch.object(self.client, "_request", return_value=success):
            result = self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
        self.assertTrue(result["isSuccess"])
        self.assertEqual("folder-nested", result["collections"][0]["collectionRef"])
        self.assertEqual("asset-nested", result["assets"][0]["assetId"])
        self.assertEqual("cursor-nested", result["continuationToken"])

        failure = {
            "httpStatus": 200,
            "isSuccess": True,
            "connectionId": "conn-1",
            "result": {"isSuccess": False, "reasonCodes": ["cloud_collection_access_denied"]},
        }
        with mock.patch.object(self.client, "_request", return_value=failure):
            result = self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["cloud_collection_access_denied"], result["reasonCodes"])
        self.assertNotIn("collections", result)

        conflict = {
            "httpStatus": 200,
            "isSuccess": True,
            "connectionId": "conn-1",
            "result": {
                "isSuccess": True,
                "connectionId": "conn-2",
                "collections": [],
                "assets": [],
            },
        }
        with mock.patch.object(self.client, "_request", return_value=conflict):
            result = self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["engine_response_invalid"], result["reasonCodes"])

    def test_direct_canonical_browse_cloud_selection_roundtrips_with_account_binding(self):
        cloud_selection = "cloud-selection:conn-1:asset-direct"
        response = {
            "httpStatus": 200,
            "isSuccess": True,
            "connectionId": "conn-1",
            "result": {
                "isSuccess": True,
                "collections": [{"collectionRef": "folder-direct", "displayName": "Shared"}],
                "assets": [{"assetId": cloud_selection, "displayName": "study.dcm"}],
            },
        }
        with mock.patch.object(self.client, "_request", side_effect=[response, {"isSuccess": True}]) as request:
            result = self.client.execute("browse", {
                "connectionId": "conn-1", "locationKind": "my_drive_root",
            })
            self.assertTrue(result["isSuccess"])
            self.assertIn(cloud_selection, self.client._selection_refs["conn-1"])
            self.assertIn("folder-direct", self.client._collection_ids["conn-1"])
            self.client.execute("acquire", {
                "connectionId": "conn-1", "assetId": cloud_selection, "artifactId": "artifact-direct",
                "maximumBytes": 1000, "publication": {
                    "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                    "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                    "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                    "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-direct",
                },
            })
        self.assertEqual(cloud_selection, request.call_args.kwargs["body"]["selectionRef"])

        self.client._connection_ids = {"conn-1", "conn-2"}
        with self.assertRaisesRegex(DriveClientError, "unknown_asset_reference"):
            self.client.execute("acquire", {
                "connectionId": "conn-2", "assetId": cloud_selection, "artifactId": "artifact-cross-account",
                "maximumBytes": 1000, "publication": {
                    "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                    "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                    "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                    "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-cross-account",
                },
            })

    def test_browse_rejects_malformed_canonical_success_envelope(self):
        response = {
            "httpStatus": 200, "isSuccess": True, "connectionId": "conn-1", "result": {"assets": []},
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["engine_response_invalid"], result["reasonCodes"])

    def test_acquire_projects_nested_publication_record_artifact(self):
        self.client._selection_refs["conn-1"] = {"selection-1"}
        response = {
            "httpStatus": 200,
            "isSuccess": True,
            "connectionId": "conn-1",
            "publication": {
                "record": {
                    "artifactId": {"value": "artifact-nested"},
                    "contentSha256": "a" * 64,
                    "sizeBytes": 4096,
                    "contentType": "application/dicom",
                },
                "contentDeduplicated": False,
                "reasonCode": "published",
            },
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("acquire", {
                "connectionId": "conn-1", "selectionRef": "selection-1", "artifactId": "requested",
                "maximumBytes": 1000, "publication": {
                    "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                    "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                    "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                    "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-nested",
                },
            })
        self.assertTrue(result["isSuccess"])
        self.assertEqual("artifact-nested", result["retainedArtifactId"])
        self.assertEqual("artifact-nested", self.client._artifact_ids["conn-1"].pop())

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
        self.assertEqual(1, request.call_args.kwargs["body"]["publication"]["kind"])
        self.assertNotIn("assetId", request.call_args.kwargs["body"])
        self.assertTrue(acquired["isSuccess"])

    def test_publication_kind_accepts_only_source_media_numeric_wire_value(self):
        valid = {
            "connectionId": "conn-1", "selectionRef": "selection-1", "artifactId": "artifact-kind",
            "maximumBytes": 1000, "publication": {
                "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-kind",
            },
        }
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True}) as request:
            self.client._selection_refs["conn-1"] = {"selection-1"}
            self.client.execute("acquire", valid)
        self.assertEqual(1, request.call_args.kwargs["body"]["publication"]["kind"])

        for invalid_kind in ("DevelopedImage", 2, True):
            with self.subTest(invalid_kind=invalid_kind):
                invalid = {**valid, "publication": {**valid["publication"], "kind": invalid_kind}}
                with self.assertRaisesRegex(DriveClientError, "publication_metadata_invalid"):
                    self.client.execute("acquire", invalid)

    def test_revoke_and_artifact_routes_replace_only_bound_opaque_refs(self):
        self.client._artifact_ids = {"conn-1": {"artifact-1"}}
        with mock.patch.object(self.client, "_request", side_effect=[
            {"isSuccess": True, "retainedArtifactId": "artifact-1"},
            {"isSuccess": True},
        ]) as request:
            inspected = self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "artifact-1"})
            result = self.client.execute("revoke", {"connectionId": "conn-1"})
        self.assertTrue(inspected["isSuccess"])
        self.assertTrue(result["isSuccess"])
        self.assertEqual("/engine/v1/media/artifacts/artifact-1", request.call_args_list[0].args[1])
        self.assertEqual("/engine/v1/cloud/connections/conn-1/revoke", request.call_args_list[1].args[1])
        self.assertIsNone(request.call_args_list[1].kwargs["body"])

        self.client._connection_ids = {"other-account-connection"}
        with self.assertRaisesRegex(DriveClientError, "unknown_connection_reference"):
            self.client.execute("revoke", {"connectionId": "conn-1"})

    def test_successful_revoke_invalidates_all_local_connection_and_graph_refs(self):
        self.client._collection_ids = {"conn-1": {"folder-1"}}
        self.client._asset_ids = {"conn-1": {"asset-1"}}
        self.client._selection_refs = {"conn-1": {"selection-1"}}
        self.client._artifact_ids = {"conn-1": {"artifact-1"}}
        self.client._graph_bindings = {
            "browse": {"connectionId": "conn-1", "scopeId": "scope-1"},
            "acquire": {"connectionId": "conn-1", "scopeId": "scope-2"},
        }
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True}):
            result = self.client.execute("revoke", {"connectionId": "conn-1"})
        self.assertTrue(result["isSuccess"])
        self.assertEqual(set(), self.client._connection_ids)
        self.assertEqual({}, self.client._collection_ids)
        self.assertEqual({}, self.client._asset_ids)
        self.assertEqual({}, self.client._selection_refs)
        self.assertEqual({}, self.client._artifact_ids)
        self.assertEqual({}, self.client._graph_bindings)
        with self.assertRaisesRegex(DriveClientError, "unknown_connection_reference"):
            self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})

    def test_failed_revoke_preserves_local_connection_and_graph_refs(self):
        self.client._collection_ids = {"conn-1": {"folder-1"}}
        self.client._asset_ids = {"conn-1": {"asset-1"}}
        self.client._selection_refs = {"conn-1": {"selection-1"}}
        self.client._artifact_ids = {"conn-1": {"artifact-1"}}
        self.client._graph_bindings = {"browse": {"connectionId": "conn-1", "scopeId": "scope-1"}}
        with mock.patch.object(self.client, "_request", return_value={
            "isSuccess": False, "reasonCodes": ["revoke_failed"],
        }):
            result = self.client.execute("revoke", {"connectionId": "conn-1"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual({"conn-1"}, self.client._connection_ids)
        self.assertEqual({"conn-1": {"folder-1"}}, self.client._collection_ids)
        self.assertEqual({"conn-1": {"asset-1"}}, self.client._asset_ids)
        self.assertEqual({"conn-1": {"selection-1"}}, self.client._selection_refs)
        self.assertEqual({"conn-1": {"artifact-1"}}, self.client._artifact_ids)
        self.assertEqual({"browse": {"connectionId": "conn-1", "scopeId": "scope-1"}}, self.client._graph_bindings)

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

    def test_explicit_artifact_id_supports_post_reload_access_without_in_process_binding(self):
        self.client._connection_ids.clear()
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True}) as request:
            result = self.client.execute("delete", {
                "artifactId": "artifact-reloaded",
                "purpose": "vault-diagnostics.cleanup", "idempotencyKey": "delete-1",
            })
        self.assertTrue(result["isSuccess"])
        self.assertEqual("/engine/v1/media/artifacts/artifact-reloaded:delete", request.call_args.args[1])
        self.assertEqual(
            {"purpose": "vault-diagnostics.cleanup", "idempotencyKey": "delete-1"},
            request.call_args.kwargs["body"],
        )

    def test_browse_remembers_canonical_collections_for_assets_after_reload(self):
        response = {"isSuccess": True, "connectionId": "conn-1",
                    "collections": [{"collectionRef": "folder-1", "connectionId": "conn-1", "providerId": "google-drive"}],
                    "assets": []}
        with mock.patch.object(self.client, "_request", side_effect=[response, {"isSuccess": True, "assets": []}]) as request:
            self.client.execute("browse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            self.client.execute("assets", {"connectionId": "conn-1", "collectionRef": "folder-1"})
        self.assertEqual("/engine/v1/cloud/assets:list", request.call_args_list[1].args[1])
        self.assertEqual("folder-1", request.call_args_list[1].kwargs["body"]["collectionRef"])

    def test_graph_status_accepts_explicit_opaque_handle_after_restart_without_resubmit(self):
        invocation = "a" * 64
        cursor = observation_cursor("scope-recovered")
        response = {"httpStatus": 200, "invocationRef": invocation,
                    "status": {"status": "Running",
                               "projection": observation_projection("scope-recovered"),
                               "nextCursor": cursor}}
        recovered = DriveClient(self.client.config, self.contracts.name)
        with mock.patch.object(recovered, "_request", return_value=response) as request:
            result = recovered.execute("graphStatus", {
                "graphKind": "browse", "graphScopeId": "scope-recovered", "graphInvocationRef": invocation,
            })
        self.assertTrue(result["isSuccess"])
        self.assertEqual("/engine/v1/graph-jobs/tenants/tenant/scopes/scope-recovered/invocations/" + invocation + "/status", request.call_args.args[1])
        self.assertTrue(recovered._graph_bindings["browse"]["submitted"])
        self.assertEqual(1, request.call_count)

    def test_graph_watch_sends_canonical_cursor_bound_to_recovered_scope(self):
        invocation = "b" * 64
        cursor = observation_cursor("scope-watch")
        response = {"httpStatus": 200, "invocationRef": invocation,
                    "watch": {"cursor": cursor, "changedJobs": [],
                              "projection": observation_projection("scope-watch"),
                              "nextCursor": cursor}}
        recovered = DriveClient(self.client.config, self.contracts.name)
        with mock.patch.object(recovered, "_request", return_value=response) as request:
            result = recovered.execute("graphWatch", {
                "graphKind": "browse", "graphScopeId": "scope-watch", "graphInvocationRef": invocation,
                "graphCursor": cursor,
            })
        self.assertTrue(result["isSuccess"])
        self.assertEqual("/engine/v1/graph-jobs/tenants/tenant/scopes/scope-watch/invocations/" + invocation + "/watch", request.call_args.args[1])
        self.assertEqual(cursor, request.call_args.kwargs["body"])
        self.assertEqual(cursor, recovered._graph_bindings["browse"]["cursor"])

    def test_graph_watch_rejects_cursor_from_another_scope_before_network(self):
        self.client._graph_bindings["browse"] = {"scopeId": "scope-watch", "invocationRef": "c" * 64,
                                                   "submitted": True, "statusTerminal": False}
        cursor = {"contractVersion": 1, "scope": {"tenantId": "tenant", "scopeId": "other-scope"},
                  "jobCursors": [{"schemaVersion": 1, "job": {"jobId": "job-1"}, "afterSequence": 0}]}
        with mock.patch.object(self.client, "_request") as request:
            with self.assertRaisesRegex(DriveClientError, "graph_cursor_scope_mismatch"):
                self.client.execute("graphWatch", {"graphKind": "browse", "graphCursor": cursor})
        request.assert_not_called()

    def test_graph_watch_rejects_malformed_projection_without_advancing_cursor(self):
        invocation = "d" * 64
        original = observation_cursor("scope-watch")
        self.client._graph_bindings["browse"] = {
            "scopeId": "scope-watch", "invocationRef": invocation,
            "submitted": True, "statusTerminal": False, "cursor": original,
        }
        malformed_projection = observation_projection("scope-watch")
        malformed_projection.pop("contractVersion")
        response = {
            "httpStatus": 200, "invocationRef": invocation,
            "watch": {
                "cursor": original,
                "changedJobs": [],
                "projection": malformed_projection,
                "nextCursor": original,
            },
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("graphWatch", {"graphKind": "browse"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
        self.assertEqual(original, self.client._graph_bindings["browse"]["cursor"])

    def test_graph_watch_requires_exact_invocation_binding(self):
        invocation = "e" * 64
        cursor = observation_cursor("scope-watch")
        self.client._graph_bindings["browse"] = {
            "scopeId": "scope-watch", "invocationRef": invocation,
            "submitted": True, "statusTerminal": False, "cursor": cursor,
        }
        response = {
            "httpStatus": 200, "invocationRef": "f" * 64,
            "watch": {
                "cursor": cursor, "changedJobs": [],
                "projection": observation_projection("scope-watch"), "nextCursor": cursor,
            },
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("graphWatch", {"graphKind": "browse"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
        self.assertEqual(cursor, self.client._graph_bindings["browse"]["cursor"])

    def test_published_cancel_requires_control_tree_and_rows(self):
        invocation = "1" * 64
        self.client._graph_bindings["browse"] = {
            "scopeId": "scope-cancel", "invocationRef": invocation,
            "submitted": True, "statusTerminal": False,
        }
        response = {
            "httpStatus": 200,
            "operation": "cancel",
            "schemaVersion": 1,
            "tree": {"schemaVersion": 1, "rootJob": {"jobId": "root"}, "bindings": []},
            "rows": [],
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("graphCancel", {"graphKind": "browse", "idempotencyKey": "cancel-1"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["engine_response_invalid"], result["reasonCodes"])

    def test_graph_watch_projects_terminal_state_for_reconciliation(self):
        invocation = "2" * 64
        cursor = observation_cursor("scope-watch", after_sequence=0)
        next_cursor = observation_cursor("scope-watch", after_sequence=1)
        self.client._graph_bindings["browse"] = {
            "scopeId": "scope-watch", "invocationRef": invocation,
            "submitted": True, "statusTerminal": False, "cursor": cursor,
        }
        response = {
            "httpStatus": 200,
            "invocationRef": invocation,
            "watch": {
                "cursor": cursor,
                "changedJobs": [{"jobId": "job-1"}],
                "projection": observation_projection("scope-watch", state="succeeded"),
                "nextCursor": next_cursor,
            },
        }
        with mock.patch.object(self.client, "_request", return_value=response):
            result = self.client.execute("graphWatch", {"graphKind": "browse"})
        self.assertTrue(result["isSuccess"])
        self.assertEqual(["succeeded"], result["graphStatuses"])
        self.assertTrue(self.client._graph_bindings["browse"]["statusTerminal"])
        self.assertEqual(next_cursor, self.client._graph_bindings["browse"]["cursor"])

    def test_explicit_artifact_id_rejects_path_or_control_characters(self):
        with mock.patch.object(self.client, "_request") as request:
            for artifact_id in ("../secret", "artifact/child", "artifact\nsecret", ""):
                with self.subTest(artifact_id=artifact_id):
                    with self.assertRaisesRegex(DriveClientError, "artifact_id_invalid"):
                        self.client.execute("inspect", {
                            "connectionId": "conn-1", "artifactId": artifact_id,
                        })
        request.assert_not_called()

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
                          "status": {"status": "Running", "projection": observation_projection("scope-1"),
                                     "nextCursor": observation_cursor("scope-1")}}
        terminal_status = {"isSuccess": True, "invocationRef": "a" * 64,
                           "status": {"status": "Failed", "projection": observation_projection("scope-1", state="failed"),
                                      "nextCursor": observation_cursor("scope-1")}}
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

    def test_graph_browse_cloud_selection_and_folder_refs_round_trip_to_acquire(self):
        invocation = "z" * 64
        prepare = {"isSuccess": True, "invocationRef": invocation, "scopeId": "scope-roundtrip",
                   "rootJobId": "job-roundtrip", "graphHash": "q" * 64, "expiresUtc": "2026-11-19T20:00:00Z"}
        cloud_selection = "cloud-selection:conn-1:asset-1"
        submit = {
            "isSuccess": True, "invocationRef": invocation, "scopeId": "scope-roundtrip",
            "rootJobId": "job-roundtrip", "graphHash": "q" * 64, "status": "Completed",
            "output": {
                "isSuccess": True, "connectionId": "conn-1",
                "result": {
                    "isSuccess": True,
                    "collections": [{"collectionRef": "folder-1", "displayName": "Shared", "driveId": "drive-1"}],
                    "assets": [{"assetId": cloud_selection, "displayName": "study.dcm",
                                "mimeType": "application/dicom", "sizeBytes": 64}],
                },
            },
        }
        with mock.patch.object(self.client, "_request", side_effect=[prepare, submit, {"isSuccess": True}, {"isSuccess": True}]) as request:
            self.client.execute("graphPrepareBrowse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            result = self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
            self.assertTrue(result["isSuccess"])
            self.assertIn("folder-1", self.client._collection_ids["conn-1"])
            self.assertIn(cloud_selection, self.client._asset_ids["conn-1"])
            self.assertIn(cloud_selection, self.client._selection_refs["conn-1"])

            self.client.execute("assets", {"connectionId": "conn-1", "collectionRef": "folder-1"})
            acquire = self.client.execute("graphPrepareAcquire", {
                "connectionId": "conn-1", "assetId": cloud_selection, "artifactId": "artifact-roundtrip",
                "maximumBytes": 1000, "publication": {
                    "fileName": "study.dcm", "role": "source", "kind": "SourceMedia",
                    "classification": "Restricted", "subjectIds": [], "allowedPurposes": ["diagnostic"],
                    "consentRequiredPurposes": [], "retentionPolicyId": "policy:test",
                    "retentionClass": "Tier1_Operational", "idempotencyKey": "attempt-roundtrip",
                },
            })
        self.assertTrue(acquire["isSuccess"])
        self.assertEqual("folder-1", request.call_args_list[2].kwargs["body"]["collectionRef"])
        self.assertEqual(cloud_selection, request.call_args_list[3].kwargs["body"]["selectionRef"])

    def test_graph_browse_rejects_malformed_page_shape_or_conflicting_connection(self):
        invocation = "m" * 64
        prepare = {"isSuccess": True, "invocationRef": invocation, "scopeId": "scope-shape",
                   "rootJobId": "job-shape", "graphHash": "s" * 64, "expiresUtc": "2026-11-19T20:00:00Z"}
        for page in (
            {"isSuccess": True, "collections": {}, "assets": []},
            {"isSuccess": True, "connectionId": "conn-2", "collections": [], "assets": []},
        ):
            with self.subTest(page=page):
                client = DriveClient(self.client.config, self.contracts.name)
                client._connection_ids = {"conn-1"}
                submit = {
                    "isSuccess": True, "invocationRef": invocation, "scopeId": "scope-shape",
                    "rootJobId": "job-shape", "graphHash": "s" * 64, "status": "Completed",
                    "output": {"isSuccess": True, "connectionId": "conn-1", "result": page},
                }
                with mock.patch.object(client, "_request", side_effect=[prepare, submit]):
                    client.execute("graphPrepareBrowse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
                    result = client.execute("graphSubmitBrowse", {"graphKind": "browse"})
                self.assertFalse(result["isSuccess"])
                self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
                self.assertNotIn("collections", result)
                self.assertNotIn("assets", result)

    def test_graph_submit_http_failure_preserves_sanitized_reason_and_requires_reconciliation(self):
        invocation = "h" * 64
        prepare = {"isSuccess": True, "invocationRef": invocation, "scopeId": "scope-http",
                   "rootJobId": "job-http", "graphHash": "c" * 64, "expiresUtc": "2026-11-19T20:00:00Z"}
        failure = {
            "isSuccess": False,
            "reasonCodes": ["engine_http_error"],
            "httpStatus": 409,
            "retainedArtifactId": "must-not-leak",
        }
        with mock.patch.object(self.client, "_request", side_effect=[prepare, failure]):
            self.client.execute("graphPrepareBrowse", {"connectionId": "conn-1", "locationKind": "my_drive_root"})
            result = self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["engine_http_error"], result["reasonCodes"])
        self.assertEqual(409, result["httpStatus"])
        self.assertNotIn("retainedArtifactId", result)
        self.assertFalse(self.client._graph_bindings["browse"]["statusTerminal"])
        self.assertTrue(self.client._graph_bindings["browse"]["submitted"])

    def test_graph_submit_success_without_matching_invocation_still_fails_closed(self):
        invocation = "i" * 64
        self.client._graph_bindings["browse"] = {
            "connectionId": "conn-1", "tenantId": "tenant", "scopeId": "scope-success",
            "invocationRef": invocation, "requestBody": {"connectionId": "conn-1"},
            "submitted": False, "statusTerminal": False,
        }
        mismatched = {
            "isSuccess": True, "invocationRef": "j" * 64, "scopeId": "scope-success",
            "status": "Completed", "output": {"artifactId": "wrong-artifact"},
        }
        with mock.patch.object(self.client, "_request", return_value=mismatched):
            result = self.client.execute("graphSubmitBrowse", {"graphKind": "browse"})
        self.assertFalse(result["isSuccess"])
        self.assertEqual(["graph_binding_mismatch"], result["reasonCodes"])
        self.assertNotIn("retainedArtifactId", result)

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
            "projection": observation_projection("scope-status", state="succeeded", artifacts=[{"payloadSchemaId": "external-media.retained-handle.v1",
                "artifactId": "recovered-artifact", "contentHash": "sha256:abc", "byteSize": 4096,
                "providerPath": "/must-not-escape"}]),
            "nextCursor": observation_cursor("scope-status")}}
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
        self.assertNotIn("graphNextCursor", result)
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
