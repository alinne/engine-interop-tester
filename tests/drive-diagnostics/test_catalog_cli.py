from __future__ import annotations

import importlib.util
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE = Path(__file__).parents[2] / "src" / "drive-diagnostics" / "catalog_cli.py"
SPEC = importlib.util.spec_from_file_location("drive_catalog_cli", MODULE)
assert SPEC and SPEC.loader
catalog_cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalog_cli
SPEC.loader.exec_module(catalog_cli)


CATALOG = {
    "contractVersion": "dicom-verified-catalog-v1",
    "source": {"artifactId": "opaque-artifact", "revision": 7, "sha256": "a" * 64},
    "inventory": {"status": "Supported", "profile": "private-profile", "entries": [{"path": "private.dcm"}]},
    "members": [{"path": "private.dcm"}],
    "inspection": {
        "discoveredInstances": [{"path": "private.dcm", "studyInstanceUid": "private"}],
        "directoryReports": [], "fileReports": [],
        "studies": [{"studyInstanceUid": "private", "series": []}],
        "isComplete": True,
    },
    "acceptance": {"metadataComplete": True, "pixelsDecoded": False, "displayVerified": False},
}


class CatalogCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="catalog-cli-test-")).resolve()
        self.token_file = self.root / "token"
        self.token_file.write_text("secret-token", encoding="utf-8")
        self.token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.executable = self.root / "media-device-cli"
        self.executable.write_text("placeholder", encoding="utf-8")
        self.executable.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.request = catalog_cli.CatalogRequest(
            executable=self.executable, base_url="https://engine.example.test",
            executable_sha256=hashlib.sha256(b"placeholder").hexdigest(),
            certificate_sha256="b" * 64, token_file=self.token_file,
            artifact_id="opaque-artifact", revision=7, source_sha256="a" * 64,
            timeout_seconds=5,
        )

    def tearDown(self):
        for path in self.root.iterdir():
            path.unlink()
        self.root.rmdir()

    def test_summary_is_bounded_and_does_not_claim_opening(self):
        seen: dict[str, object] = {}

        def runner(argv, timeout):
            seen["argv"] = tuple(argv)
            seen["timeout"] = timeout
            request_path = Path(argv[argv.index("--request-file") + 1])
            self.assertEqual({"revision": 7, "sourceSha256": "a" * 64},
                             json.loads(request_path.read_text(encoding="utf-8")))
            return 0, json.dumps(CATALOG).encode("utf-8")

        summary = catalog_cli.read_catalog(self.request, runner=runner)
        self.assertEqual("Supported", summary["status"])
        self.assertEqual(1, summary["inventoryEntryCount"])
        self.assertEqual(1, summary["memberCount"])
        self.assertEqual(1, summary["instanceCount"])
        self.assertEqual(1, summary["studyCount"])
        self.assertTrue(summary["metadataComplete"])
        self.assertTrue(summary["isComplete"])
        self.assertFalse(summary["pixelsDecoded"])
        self.assertFalse(summary["displayVerified"])
        self.assertNotIn("private.dcm", json.dumps(summary))
        self.assertNotIn("private", json.dumps(summary))
        argv = seen["argv"]
        self.assertEqual(str(self.executable), argv[0])
        self.assertIn("media-device", argv)
        self.assertIn("dicom-catalog", argv)
        self.assertIn("--api-token-file", argv)
        self.assertNotIn("secret-token", argv)
        self.assertEqual(5.0, seen["timeout"])
        request_path = Path(argv[argv.index("--request-file") + 1])
        self.assertFalse(request_path.exists())

    def test_nonzero_cli_failure_is_sanitized(self):
        def runner(_argv, _timeout):
            return 9, b"private provider stderr and patient payload"

        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "engine_cli_failed") as raised:
            catalog_cli.read_catalog(self.request, runner=runner)
        self.assertNotIn("patient", str(raised.exception))

    def test_malformed_output_is_rejected_without_revalidating_dicom(self):
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_invalid"):
            catalog_cli.read_catalog(self.request, runner=lambda _argv, _timeout: (0, b"[]"))

    def test_stdout_cap_is_enforced(self):
        oversized = b"{" + b"x" * catalog_cli.MAX_CATALOG_BYTES + b"}"
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_too_large"):
            catalog_cli.read_catalog(self.request, runner=lambda _argv, _timeout: (0, oversized))

    def test_timeout_is_sanitized(self):
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "engine_cli_timeout"):
            catalog_cli.read_catalog(self.request, runner=mock.Mock(side_effect=TimeoutError()))

    def test_endpoint_and_inputs_are_bound(self):
        for value in ("http://engine.example.test", "https://engine.example.test/path?secret=1", "https://["):
            bad = self.request.__class__(**{**self.request.__dict__, "base_url": value})
            with self.assertRaises(catalog_cli.CatalogCliError):
                catalog_cli.read_catalog(bad, runner=lambda _argv, _timeout: (0, b"{}"))
        for revision in (0, -1, True, 9223372036854775808):
            bad = self.request.__class__(**{**self.request.__dict__, "revision": revision})
            with self.assertRaises(catalog_cli.CatalogCliError):
                catalog_cli.read_catalog(bad, runner=lambda _argv, _timeout: (0, b"{}"))

    def test_token_file_must_be_private(self):
        self.token_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "token_file_not_private"):
            catalog_cli.read_catalog(self.request, runner=lambda _argv, _timeout: (0, b"{}"))

    def test_mismatched_source_or_display_claim_is_rejected(self):
        for change in (lambda v: v["source"].update(revision=8),
                       lambda v: v["acceptance"].update(displayVerified=True),
                       lambda v: v.update(contractVersion="unknown")):
            value = json.loads(json.dumps(CATALOG))
            change(value)
            with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_invalid"):
                catalog_cli.read_catalog(self.request, runner=lambda _a, _t: (0, json.dumps(value).encode()))

    def test_executable_pin_mismatch_prevents_dispatch(self):
        bad = self.request.__class__(**{**self.request.__dict__, "executable_sha256": "0" * 64})
        runner = mock.Mock()
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "engine_cli_executable_pin_mismatch"):
            catalog_cli.read_catalog(bad, runner=runner)
        runner.assert_not_called()

    def test_source_revision_requires_an_exact_json_integer(self):
        request = self.request.__class__(**{**self.request.__dict__, "revision": 1})
        for revision in (True, 1.0, "1"):
            value = json.loads(json.dumps(CATALOG))
            value["source"]["revision"] = revision
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_invalid"):
                    catalog_cli.read_catalog(request, runner=lambda _a, _t: (0, json.dumps(value).encode()))

    def test_large_int64_source_revision_is_preserved_without_float_roundtrip(self):
        for revision in (9007199254740993, 9223372036854775807):
            request = self.request.__class__(**{**self.request.__dict__, "revision": revision})
            value = json.loads(json.dumps(CATALOG))
            value["source"]["revision"] = revision
            def runner(argv, _timeout):
                document = json.loads(Path(argv[argv.index("--request-file") + 1]).read_text())
                self.assertIs(type(document["revision"]), int)
                self.assertEqual(revision, document["revision"])
                return 0, json.dumps(value).encode()
            with self.subTest(revision=revision):
                self.assertEqual("Supported", catalog_cli.read_catalog(request, runner=runner)["status"])
                value["source"]["revision"] -= 1
                with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_invalid"):
                    catalog_cli.read_catalog(request, runner=runner)

    def test_artifact_and_hash_mismatch_are_refused(self):
        for key, replacement in (("artifactId", "different-artifact"), ("sha256", "b" * 64)):
            value = json.loads(json.dumps(CATALOG))
            value["source"][key] = replacement
            with self.subTest(key=key):
                with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_invalid"):
                    catalog_cli.read_catalog(self.request, runner=lambda _a, _t: (0, json.dumps(value).encode()))

    def test_real_runner_caps_output_and_times_out(self):
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "catalog_output_too_large"):
            with mock.patch.object(catalog_cli, "MAX_CATALOG_BYTES", 1024):
                catalog_cli._run([sys.executable, "-c", "import os; os.write(1, b'x'*2048)"], 2)
        with self.assertRaises(TimeoutError):
            catalog_cli._run([sys.executable, "-c", "import time; time.sleep(30)"], 0.1)

    def test_request_document_is_removed_on_failure_or_cancel(self):
        for error in (TimeoutError(), KeyboardInterrupt()):
            observed = []
            def runner(argv, _timeout):
                observed.append(Path(argv[argv.index("--request-file") + 1]))
                raise error
            with self.assertRaises((catalog_cli.CatalogCliError, KeyboardInterrupt)):
                catalog_cli.read_catalog(self.request, runner=runner)
            self.assertFalse(observed[0].exists())
            self.assertTrue(self.token_file.exists())

    def test_symlink_executable_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.executable)
        bad = self.request.__class__(**{**self.request.__dict__, "executable": alias})
        with self.assertRaisesRegex(catalog_cli.CatalogCliError, "engine_cli_executable_invalid"):
            catalog_cli.read_catalog(bad)


if __name__ == "__main__":
    unittest.main()
