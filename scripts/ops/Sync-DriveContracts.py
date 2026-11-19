"""Project selected Engine route declarations; never infer or invent an endpoint."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

SELECTION = {
    "readiness": ("CloudMedia", "ProviderOAuthAppReadiness", "cloud-media.oauth-apps.readiness"),
    "start": ("CloudMedia", "OAuthStart", "cloud-media.oauth.start"),
    "status": ("CloudMedia", "OAuthStatus", "cloud-media.oauth.status"),
    "connections": ("Cloud", "Connections", "cloud.connections.list"),
    "collections": ("Cloud", "CollectionsList", "cloud.collections.list"),
    "assets": ("Cloud", "AssetsList", "cloud.assets.list"),
    "callback": ("CloudMedia", "OAuthCallbackTemplate", "cloud-media.oauth.callback"),
}
# Optional consumer capabilities are projected only when their declarations
# and catalog entries exist in the selected committed Engine source.  The
# selected source determines availability; this table cannot manufacture
# routes while a parent Engine change is uncommitted.
OPTIONAL_SELECTION = {
    "graphPrepareBrowse": ("ExternalMediaGraph", "PrepareBrowseTemplate", "external-media.graph.prepare-browse"),
    "graphPrepareAcquire": ("ExternalMediaGraph", "PrepareAcquireRetainedTemplate", "external-media.graph.prepare-acquire-retained"),
    "graphSubmitBrowse": ("ExternalMediaGraph", "BrowseTemplate", "external-media.graph.browse"),
    "graphSubmitAcquire": ("ExternalMediaGraph", "AcquireRetainedTemplate", "external-media.graph.acquire-retained"),
    "graphStatus": ("GraphJobs", "StatusTemplate", "graph-job.observation.status"),
    "graphWatch": ("GraphJobs", "WatchTemplate", "graph-job.observation.watch"),
    "graphCancel": ("GraphJobs", "CancelTemplate", "graph-job.control.cancel"),
    "browse": ("Cloud", "MediaBrowse", "cloud.media.browse"),
    "acquire": ("Cloud", "MediaAcquireRetained", "cloud.media.acquire-retained"),
    "revoke": ("Cloud", "ConnectionRevokeTemplate", "cloud.connections.revoke"),
    "inspect": ("MediaArtifactLifecycle", "InspectTemplate", "media.artifacts.inspect"),
    "verifiedRead": ("MediaArtifactLifecycle", "VerifiedReadTemplate", "media.artifacts.verified-read"),
    "verify": ("MediaArtifactLifecycle", "VerifyTemplate", "media.artifacts.verify"),
    "materialize": ("MediaArtifactLifecycle", "MaterializeTemplate", "media.artifacts.materialize"),
    "release": ("MediaArtifactLifecycle", "TombstoneTemplate", "media.artifacts.tombstone"),
    "delete": ("MediaArtifactLifecycle", "DeleteTemplate", "media.artifacts.delete"),
}
CONTRACT_ROOT = "src/Linnaeus.Engine.ModelFabric.Contracts/"
SOURCE_FILES = [CONTRACT_ROOT + "EngineControlApiRoutes" + suffix + ".cs"
                for suffix in ("", ".Media", ".Control")]
OPTIONAL_SOURCE_FILES = [CONTRACT_ROOT + "EngineControlApiRoutes" + suffix + ".cs"
                         for suffix in (".MediaArtifactLifecycle", ".ExternalMediaGraph", ".GraphJobs")]


def project(root, source_commit=None):
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--verify",
                                      (source_commit or "HEAD") + "^{commit}"], text=True).strip()
    committed_files = {name: subprocess.check_output(["git", "-C", str(root), "show", f"{commit}:{name}"])
                       for name in SOURCE_FILES}
    for name in OPTIONAL_SOURCE_FILES:
        present = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{commit}:{name}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if present:
            committed_files[name] = subprocess.check_output(["git", "-C", str(root), "show", f"{commit}:{name}"])
    files = committed_files if source_commit else {name: (root / name).read_bytes() for name in SOURCE_FILES}
    if not source_commit:
        for name in OPTIONAL_SOURCE_FILES:
            candidate = root / name
            if candidate.exists():
                files[name] = candidate.read_bytes()
    if files != committed_files:
        raise ValueError("Contract source has uncommitted changes; select a committed source.")
    catalog = files[SOURCE_FILES[0]].decode("utf-8-sig")
    all_text = "\n".join(value.decode("utf-8-sig") for value in files.values())
    operations = {}
    for action, (family, member, operation_id) in SELECTION.items():
        declaration = re.search(r"public static class " + family + r"\s*\{(.*?)\n    \}", all_text, re.S)
        if not declaration:
            raise ValueError(f"Missing canonical family: {family}")
        path = re.search(r"public const string " + member + r'\s*=\s*"([^"\r\n]+)";', declaration[1])
        entry = re.search(r'(Stable|Internal)\("' + re.escape(operation_id) + r'", "([A-Z]+)", EngineControlApiRoutes\.' + family + r"\." + member + r"[,)]", catalog)
        if not path or not entry:
            raise ValueError(f"Missing canonical operation: {operation_id}")
        operations[action] = {"operationId": operation_id, "method": entry[2], "path": path[1],
                              "visibility": entry[1].lower()}
    for action, (family, member, operation_id) in OPTIONAL_SELECTION.items():
        declaration = re.search(r"public static class " + family + r"\s*\{(.*?)\n    \}", all_text, re.S)
        if not declaration:
            continue
        path = re.search(r"public const string " + member + r'\s*=\s*"([^"\r\n]+)";', declaration[1])
        entry = re.search(r'(Stable|Internal)\("' + re.escape(operation_id) + r'", "([A-Z]+)", EngineControlApiRoutes\.' + family + r"\." + member + r"[,)]", catalog)
        if not path and not entry:
            continue
        if not path or not entry:
            raise ValueError(f"Missing canonical optional operation: {operation_id}")
        operations[action] = {"operationId": operation_id, "method": entry[2], "path": path[1],
                              "visibility": entry[1].lower()}
    return {"sourceRepository": "alinne/linnaeus-engine", "sourceCommit": commit,
            "sourceHashes": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            "operations": operations}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source-commit", help="Verify an immutable selected Engine commit, independent of its active checkout.")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[2] / "src/drive-diagnostics/contracts.json"
    generated = json.dumps(project(args.engine_root.resolve(strict=True), args.source_commit), indent=2) + "\n"
    if args.check:
        if output.read_text() != generated:
            raise SystemExit("Drive route bindings differ from selected Engine source.")
        print("Drive route bindings match committed Engine source.")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(generated)
        print(f"Projected {len(generated and json.loads(generated)['operations'])} canonical Engine operations.")
