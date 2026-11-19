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
                "assetId": "opaque-asset",
                "displayName": "study.dcm",
                "mimeType": "application/dicom",
                "resourceKey": "provider-secret",
                "rawRef": "provider-file-id",
                "localPath": "/private/path",
            }],
            "continuationToken": "opaque-cursor",
        }
        with mock.patch.object(self.client, "_request", return_value=response) as request:
            result = self.client.execute("browse", {"connectionId": "conn-1", "view": "shared-with-me"})
            self.assertTrue(result["isSuccess"])
            self.assertEqual("opaque-cursor", result["continuationToken"])
            self.assertNotIn("resourceKey", json.dumps(result))
            self.assertNotIn("rawRef", json.dumps(result))
            self.assertNotIn("localPath", json.dumps(result))
            self.client.execute("browse", {
                "connectionId": "conn-1", "view": "shared-with-me", "continuationToken": result["continuationToken"]
            })
        self.assertEqual("opaque-cursor", request.call_args.kwargs["body"]["continuationToken"])

    def test_acquire_requires_asset_returned_by_browse_and_never_auto_acquires(self):
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True, "items": [{"assetId": "asset-1"}]}) as request:
            self.client.execute("browse", {"connectionId": "conn-1", "view": "my-drive"})
            self.assertEqual(1, request.call_count)
            with self.assertRaisesRegex(DriveClientError, "unknown_asset_reference"):
                self.client.execute("acquire", {"connectionId": "conn-1", "assetId": "other"})
            self.assertEqual(1, request.call_count)
            acquired = self.client.execute("acquire", {"connectionId": "conn-1", "assetId": "asset-1"})
        self.assertEqual("/engine/v1/cloud/media:acquire-retained", request.call_args.args[1])
        self.assertEqual("asset-1", request.call_args.kwargs["body"]["assetId"])
        self.assertTrue(acquired["isSuccess"])

    def test_revoke_and_artifact_routes_replace_only_bound_opaque_refs(self):
        with mock.patch.object(self.client, "_request", return_value={"isSuccess": True, "retainedArtifactId": "artifact-1"}) as request:
            result = self.client.execute("revoke", {"connectionId": "conn-1"})
            self.assertTrue(result["isSuccess"])
            self.client._artifact_ids = {"conn-1": {"artifact-1"}}
            self.client.execute("inspect", {"connectionId": "conn-1", "retainedArtifactId": "artifact-1"})
        self.assertEqual("/engine/v1/media/artifacts/artifact-1", request.call_args.args[1])

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


if __name__ == "__main__":
    unittest.main()
