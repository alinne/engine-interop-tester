"""Thin consumer adapter for the canonical Engine DICOM catalog CLI.

The Engine ``media-device dicom-catalog`` command owns authentication,
source-binding, and DICOM validation. This module only invokes an explicitly
configured prebuilt executable and projects bounded metadata counts. It does
not parse DICOM semantics, read pixels, display frames, or open an ISO.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit


MAX_CATALOG_BYTES = 16 * 1024 * 1024
_CLOSE_TIMEOUT_SECONDS = 5.0
_MAX_COUNT = 1_000_000
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class CatalogCliError(ValueError):
    """Fixed, non-sensitive adapter failure code."""


class CatalogCliUncertainClose(CatalogCliError):
    """The invocation failed to prove group cessation; retain request custody."""

    def __init__(self):
        super().__init__("engine_cli_close_uncertain")
        self.request_directory: Path | None = None
        self.process: subprocess.Popen | None = None


@dataclass(frozen=True)
class CatalogRequest:
    executable: Path
    executable_sha256: str
    base_url: str
    certificate_sha256: str
    token_file: Path
    artifact_id: str
    revision: int
    source_sha256: str
    timeout_seconds: int = 30

    def validate(self) -> None:
        executable = Path(self.executable)
        if not executable.is_absolute() or executable.resolve() != executable or not executable.is_file():
            raise CatalogCliError("engine_cli_executable_invalid")
        if not os.access(executable, os.X_OK):
            raise CatalogCliError("engine_cli_executable_invalid")
        if not _SHA256.fullmatch(self.executable_sha256):
            raise CatalogCliError("engine_cli_executable_pin_invalid")
        if _file_sha256(executable) != self.executable_sha256.lower():
            raise CatalogCliError("engine_cli_executable_pin_mismatch")
        try:
            parsed = urlsplit(self.base_url)
        except ValueError:
            raise CatalogCliError("engine_endpoint_invalid") from None
        if (any(c.isspace() or ord(c) < 32 for c in self.base_url)
                or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise CatalogCliError("engine_endpoint_invalid")
        try:
            port = parsed.port
        except ValueError as exc:
            raise CatalogCliError("engine_endpoint_invalid") from exc
        if port is not None and not 1 <= port <= 65535:
            raise CatalogCliError("engine_endpoint_invalid")
        if not _SHA256.fullmatch(self.certificate_sha256):
            raise CatalogCliError("engine_certificate_pin_invalid")
        token_file = Path(self.token_file)
        if (not token_file.is_absolute() or token_file.resolve() != token_file
                or not token_file.is_file()):
            raise CatalogCliError("token_file_invalid")
        if stat.S_IMODE(token_file.stat().st_mode) & 0o077:
            raise CatalogCliError("token_file_not_private")
        if (not self.artifact_id or self.artifact_id in (".", "..") or len(self.artifact_id) > 160
                or any(c in self.artifact_id for c in "/\\")
                or any(c.isspace() or ord(c) < 0x20 for c in self.artifact_id)):
            raise CatalogCliError("artifact_id_invalid")
        if type(self.revision) is not int or not 1 <= self.revision <= 9223372036854775807:
            raise CatalogCliError("revision_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise CatalogCliError("source_sha256_invalid")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300:
            raise CatalogCliError("timeout_invalid")


Runner = Callable[[Sequence[str], float], tuple[int, bytes]]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _group_absent(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def _retire_process(process: subprocess.Popen) -> bool:
    # WNOWAIT preserves the leader identity until this one owned-group signal.
    # Never signal a numeric PGID after reaping its leader (possible ID reuse).
    deadline = time.monotonic() + _CLOSE_TIMEOUT_SECONDS
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        # A platform may refuse a signal to an already-exited leader. Still
        # join it; only the subsequent absence observation can prove closure.
        pass
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        return False
    while True:
        if _group_absent(process.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _run(argv: Sequence[str], timeout: float) -> tuple[int, bytes]:
    # This diagnostic runner uses POSIX process groups. Other hosts must supply
    # their supported process owner instead of silently losing tree cancellation.
    if (os.name != "posix"
            or not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED"))
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        raise CatalogCliError("engine_cli_platform_unsupported")
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               env={"LANG": "C.UTF-8"}, cwd="/")
    captured = bytearray()
    deadline = time.monotonic() + timeout
    can_signal = True
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                if not selector.select(min(remaining, 0.1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                if len(captured) + len(chunk) > MAX_CATALOG_BYTES:
                    raise CatalogCliError("catalog_output_too_large")
                captured.extend(chunk)
        # Observe exit without reaping: EOF alone does not settle a child, and
        # reaping here would make a later group cancellation vulnerable to reuse.
        while True:
            try:
                observation = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except InterruptedError:
                if time.monotonic() >= deadline:
                    raise TimeoutError() from None
                continue
            except OSError:
                # If ownership observation fails, never guess that the leader
                # remains unreaped and signal a possibly reused numeric PGID.
                can_signal = False
                raise CatalogCliUncertainClose() from None
            if observation is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError()
            time.sleep(0.01)
        result = bytes(captured)
    finally:
        closed = False
        try:
            if can_signal:
                closed = _retire_process(process)
        except BaseException:
            # Even an interrupt during settlement must preserve request custody.
            pass
        finally:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except BaseException:
                closed = False
            captured.clear()
        if not closed:
            error = CatalogCliUncertainClose()
            error.process = process
            raise error from None
    return process.returncode, result


def _summary(raw: bytes, request: CatalogRequest) -> dict[str, object]:
    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogCliError("catalog_output_too_large")
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=unique)
        # Python equates True and 1.0 with 1; identity revisions require the
        # exact JSON integer representation, including beyond float precision.
        if type(value["source"]["revision"]) is not int:
            raise ValueError()
        if (value["contractVersion"] != "dicom-verified-catalog-v1"
                or value["source"] != {"artifactId": request.artifact_id,
                                      "revision": request.revision, "sha256": request.source_sha256}):
            raise ValueError()
        inventory, inspection, acceptance = value["inventory"], value["inspection"], value["acceptance"]
        status = inventory["status"]
        if status not in ("Supported", "Invalid", "Unsupported", "LimitExceeded"):
            raise ValueError()
        if (type(acceptance["metadataComplete"]) is not bool or type(inspection["isComplete"]) is not bool
                or acceptance["pixelsDecoded"] is not False or acceptance["displayVerified"] is not False):
            raise ValueError()
        result = {"status": status, "metadataComplete": acceptance["metadataComplete"],
                  "isComplete": inspection["isComplete"], "pixelsDecoded": False, "displayVerified": False}
        for name, items in (("inventoryEntryCount", inventory["entries"]), ("memberCount", value["members"]),
                            ("instanceCount", inspection["discoveredInstances"]), ("studyCount", inspection["studies"])):
            if not isinstance(items, list) or len(items) > _MAX_COUNT:
                raise ValueError()
            result[name] = len(items)
        return result
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CatalogCliError("catalog_output_invalid") from None


def read_catalog(request: CatalogRequest, *, runner: Runner = _run) -> dict[str, object]:
    try:
        request.validate()
        # Only the source binding enters this private temporary document. The
        # existing token file remains owned by its credential provider.
        root = Path(tempfile.mkdtemp(prefix="engine-catalog-request-")).resolve()
        retain_request = False
        try:
            os.chmod(root, 0o700)
            path = root / "request.json"
            with path.open("x", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                json.dump({"revision": request.revision, "sourceSha256": request.source_sha256}, stream)
            argv = [str(request.executable), "media-device", "dicom-catalog", "--base-url", request.base_url,
                    "--tls-certificate-sha256", request.certificate_sha256, "--api-token-file", str(request.token_file),
                    "--artifact-id", request.artifact_id, "--request-file", str(path),
                    "--timeout-seconds", str(request.timeout_seconds)]
            code, raw = runner(argv, float(request.timeout_seconds))
            if code != 0:
                raise CatalogCliError("engine_cli_failed")
            return _summary(raw, request)
        except CatalogCliUncertainClose as error:
            retain_request = True
            error.request_directory = root
            raise
        finally:
            if not retain_request:
                shutil.rmtree(root)
    except TimeoutError:
        raise CatalogCliError("engine_cli_timeout") from None
    except OSError:
        raise CatalogCliError("engine_cli_io_failed") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("executable", "executable-sha256", "base-url", "certificate-sha256", "token-file",
                 "artifact-id", "source-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = vars(parser.parse_args())
    args["executable"], args["token_file"] = Path(args["executable"]), Path(args["token_file"])
    try:
        print(json.dumps(read_catalog(CatalogRequest(**args)), sort_keys=True))
        return 0
    except CatalogCliError as error:
        print(json.dumps({"reasonCode": str(error)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('{"reasonCode":"engine_cli_cancelled"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
