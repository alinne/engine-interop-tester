"use strict";
let csrf = "";
let selected = null;
let retainedArtifact = null;
let retainedArtifactDetails = null;
let continuationToken = null;
let browseContext = null;
let folderRecords = new Map();
let graphPreparedKind = null;
const byId = id => document.getElementById(id);
const connectionId = () => byId("connection").value;
function clearResult(message = "Selection changed. Run an action to refresh the diagnostic result.") {
  byId("status").dataset.failed = "false";
  byId("status").textContent = message;
  byId("result").textContent = "No current result.";
}
function clearFiles({resetBrowse = true, clearArtifact = false} = {}) {
  selected = null;
  byId("selection").textContent = "Select one ISO or DICOM file.";
  byId("assets").replaceChildren();
  byId("pagination").hidden = true;
  byId("next-page").hidden = true;
  continuationToken = null;
  if (resetBrowse) browseContext = null;
  if (clearArtifact) {
    retainedArtifact = null;
    retainedArtifactDetails = null;
    renderRetainedArtifact(null);
  }
  if (resetBrowse) graphPreparedKind = null;
}
function resetFolders({clearResult: resetResult = false} = {}) {
  byId("folder").replaceChildren(new Option("Load folders for this connection", ""));
  folderRecords = new Map();
  clearFiles({clearArtifact: true});
  retainedArtifact = null;
  if (resetResult) clearResult();
}
function renderRetainedArtifact(record) {
  const element = byId("retained-artifact-status");
  if (!element) return;
  if (!record || typeof record !== "object" || typeof record.artifactId !== "string") {
    element.textContent = "No retained artifact is bound to this connection yet.";
    return;
  }
  retainedArtifactDetails = record;
  retainedArtifact = record.artifactId;
  const details = ["Bound artifact: " + record.artifactId];
  if (typeof record.contentHash === "string") details.push("hash " + record.contentHash);
  if (Number.isInteger(record.byteSize)) details.push(record.byteSize + " bytes");
  element.textContent = details.join(" · ");
}
function invalidateBrowseContext() {
  clearFiles({clearArtifact: true});
  clearResult();
}
function changeBrowseView() {
  // A folder reference is scoped to the previous view; retaining it would
  // send a valid opaque ID with the wrong location and create a stale listing.
  byId("folder").replaceChildren(new Option("Browse to load folders", ""));
  invalidateBrowseContext();
}
function fillSelect(id, items, key, label) {
  const element = byId(id);
  element.replaceChildren(new Option("Select…", ""));
  for (const item of items) element.add(new Option(String(item[label] || item[key]), String(item[key])));
}
const LOCATION_KINDS = {
  "my-drive": "my_drive_root",
  "shared-with-me": "shared_with_me",
  "shared-drives": "shared_drives",
  "shared-drive-folder": "folder"
};
function publicationPayload() {
  const list = value => value.split(",").map(item => item.trim()).filter(Boolean);
  return {
    fileName: byId("publication-file-name").value.trim(),
    role: byId("publication-role").value.trim(),
    kind: byId("publication-kind").value,
    classification: byId("publication-classification").value,
    subjectIds: list(byId("publication-subject-ids").value),
    allowedPurposes: list(byId("publication-allowed-purposes").value),
    consentRequiredPurposes: list(byId("publication-consent-purposes").value),
    retentionPolicyId: byId("publication-retention-policy").value.trim(),
    retentionClass: byId("publication-retention-class").value,
    idempotencyKey: byId("publication-idempotency").value.trim()
  };
}
function render(action, data) {
  // HTTP success is transport evidence only; Engine's result controls the status.
  const success = data.isSuccess === true;
  byId("status").dataset.failed = String(!success);
  byId("status").textContent = success ? "Engine request succeeded." : "Request did not complete successfully. See reason codes below.";
  byId("result").textContent = JSON.stringify(data, null, 2);
  if (data.retainedArtifact && typeof data.retainedArtifact === "object") renderRetainedArtifact(data.retainedArtifact);
  if (action === "start") {
    const link = byId("consent");
    link.hidden = true;
    link.removeAttribute("href");
    if (success && data.authorizationUrl) {
      const url = new URL(data.authorizationUrl);
      if (url.protocol === "https:" && url.hostname === "accounts.google.com" && !url.username && !url.password && (!url.port || url.port === "443")) {
        link.href = url.href;
        link.hidden = false;
      }
    }
  }
  if (action === "connections") {
    resetFolders();
    fillSelect("connection", success ? data.connections || [] : [], "connectionId", "connectionId");
  }
  if (action === "browse" || action === "graphSubmitBrowse") {
    clearFiles({resetBrowse: false, clearArtifact: true});
    const collections = success ? data.collections || [] : [];
    folderRecords = new Map(collections.map(item => [String(item.collectionRef || item.collectionId || ""), item]));
    const folderSelect = byId("folder");
    folderSelect.replaceChildren(new Option("Select…", ""));
    for (const item of collections) {
      const ref = item.collectionRef || item.collectionId;
      if (ref) folderSelect.add(new Option(String(item.displayName || item.name || ref), String(ref)));
    }
    if (success && browseContext?.locationRef) byId("folder").value = browseContext.locationRef;
    renderAssets(success ? (data.assets || data.items || []) : []);
    continuationToken = success ? (data.continuationToken || data.nextPageToken || null) : null;
    if (!success) browseContext = null;
  }
  if (action === "collections") {
    clearFiles();
    fillSelect("folder", success ? data.collections || [] : [], "collectionId", "displayName");
  }
  if (action === "assets") {
    clearFiles({clearArtifact: true});
    renderAssets(success ? data.assets || [] : []);
  }
  if (["acquire", "materialize"].includes(action) && success) {
    const publicationArtifact = data.publication?.artifactId;
    const id = data.retainedArtifactId || data.artifactId || publicationArtifact || data.artifacts?.[0]?.retainedArtifactId || data.artifacts?.[0]?.artifactId || null;
    if (id) renderRetainedArtifact({artifactId: id});
  }
  if (["graphSubmitAcquire", "graphStatus"].includes(action) && success && data.retainedArtifact) {
    renderRetainedArtifact(data.retainedArtifact);
  }
  if (["graphPrepareBrowse", "graphPrepareAcquire"].includes(action) && success) {
    graphPreparedKind = action === "graphPrepareBrowse" ? "browse" : "acquire";
    byId("graph-kind").value = graphPreparedKind;
  }
  if (action === "graphStatus" && success) {
    byId("status").textContent = "Graph status inspected. Reconcile the result before starting another attempt.";
  }
  if (["browse", "assets"].includes(action)) {
    byId("pagination").hidden = !continuationToken;
    byId("next-page").hidden = !continuationToken;
  }
}
function renderAssets(assets) {
  for (const asset of assets) {
      const row = document.createElement("tr");
      const choose = document.createElement("input");
      choose.type = "radio";
      choose.name = "asset";
      choose.setAttribute("aria-label", "Select " + (asset.displayName || asset.assetId));
      choose.disabled = !(/\.(iso|dcm|dicom)$/i.test(asset.displayName || "") || asset.mimeType === "application/dicom");
      choose.addEventListener("change", () => {
        selected = asset;
        byId("selection").textContent = asset.displayName || asset.assetId;
      });
      const first = document.createElement("td");
      first.append(choose); row.append(first);
      for (const value of [asset.displayName, asset.mimeType, asset.sizeBytes]) {
        const cell = document.createElement("td");
        cell.textContent = value == null ? "—" : String(value);
        row.append(cell);
      }
      byId("assets").append(row);
  }
}
async function run(action, payload = {}) {
  if (action === "start") {
    resetFolders({clearResult: true});
    fillSelect("connection", [], "connectionId", "connectionId");
    byId("consent").hidden = true;
    byId("consent").removeAttribute("href");
  }
  const buttons = document.querySelectorAll("button, select");
  buttons.forEach(button => button.disabled = true);
  byId("status").textContent = "Waiting for Engine…";
  try {
    const response = await fetch("/action", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Drive-CSRF": csrf},
      body: JSON.stringify({action, payload})});
    const data = await response.json();
    render(action, data);
  } catch (_) {
    render(action, {isSuccess: false, reasonCodes: ["diagnostic_transport_failed"]});
  } finally {
    buttons.forEach(button => button.disabled = false);
  }
}
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => run(button.dataset.action)));
byId("connection").addEventListener("change", () => resetFolders({clearResult: true}));
byId("view").addEventListener("change", changeBrowseView);
byId("folder").addEventListener("change", invalidateBrowseContext);
byId("files").addEventListener("click", () => run("assets", {connectionId: connectionId(), collectionRef: byId("folder").value}));
byId("folders").addEventListener("click", () => {
  const kind = LOCATION_KINDS[byId("view").value];
  const folder = folderRecords.get(byId("folder").value);
  browseContext = {
    connectionId: connectionId(),
    locationKind: kind,
    ...(folder?.collectionRef || folder?.collectionId ? {locationRef: folder.collectionRef || folder.collectionId} : {}),
    ...(folder?.driveId ? {driveId: folder.driveId} : {})
  };
  run("browse", browseContext);
});
byId("next-page").addEventListener("click", () => {
  if (!browseContext || !continuationToken) return;
  run("browse", {...browseContext, continuationToken});
});
byId("acquire").addEventListener("click", () => run("acquire", {
  connectionId: connectionId(),
  selectionRef: selected?.selectionRef || selected?.opaqueSelectionRef || "",
  artifactId: byId("artifact-id").value.trim(),
  maximumBytes: byId("maximum-bytes").value.trim(),
  maximumDurationSeconds: byId("maximum-duration").value.trim(),
  publication: publicationPayload()
}));
byId("inspect").addEventListener("click", () => run("inspect", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || ""}));
byId("verify").addEventListener("click", () => run("verify", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || ""}));
byId("verified-read").addEventListener("click", () => run("verifiedRead", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || "", purpose: byId("access-purpose").value.trim()}));
byId("materialize").addEventListener("click", () => run("materialize", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || "", purpose: byId("access-purpose").value.trim(), relativeOutputPath: byId("relative-output-path").value.trim()}));
byId("release").addEventListener("click", () => {
  if (retainedArtifact && window.confirm("Tombstone this retained artifact?")) run("release", {connectionId: connectionId(), retainedArtifactId: retainedArtifact, purpose: byId("access-purpose").value.trim(), idempotencyKey: byId("release-idempotency").value.trim()});
});
byId("revoke").addEventListener("click", () => {
  if (connectionId() && window.confirm("Revoke this selected Google Drive connection?")) run("revoke", {connectionId: connectionId()});
});
function graphBrowsePayload() {
  const folder = folderRecords.get(byId("folder").value);
  return {
    connectionId: connectionId(),
    locationKind: browseContext?.locationKind || LOCATION_KINDS[byId("view").value] || "my_drive_root",
    ...(browseContext?.locationRef || folder?.collectionRef || folder?.collectionId
      ? {locationRef: browseContext?.locationRef || folder.collectionRef || folder.collectionId} : {}),
    ...(browseContext?.driveId || folder?.driveId ? {driveId: browseContext?.driveId || folder.driveId} : {})
  };
}
function graphAcquirePayload() {
  return {
    connectionId: connectionId(),
    selectionRef: selected?.selectionRef || selected?.opaqueSelectionRef || "",
    artifactId: byId("artifact-id").value.trim(),
    maximumBytes: byId("maximum-bytes").value.trim(),
    maximumDurationSeconds: byId("maximum-duration").value.trim(),
    publication: publicationPayload()
  };
}
byId("graph-prepare-browse").addEventListener("click", () => run("graphPrepareBrowse", graphBrowsePayload()));
byId("graph-prepare-acquire").addEventListener("click", () => run("graphPrepareAcquire", graphAcquirePayload()));
byId("graph-submit").addEventListener("click", () => {
  if (!graphPreparedKind) return render("graphSubmit", {isSuccess: false, reasonCodes: ["graph_not_prepared"]});
  run(graphPreparedKind === "browse" ? "graphSubmitBrowse" : "graphSubmitAcquire", {graphKind: graphPreparedKind});
});
byId("graph-status").addEventListener("click", () => run("graphStatus", {graphKind: byId("graph-kind").value}));
byId("graph-cancel").addEventListener("click", () => {
  const idempotencyKey = byId("graph-cancel-idempotency").value.trim();
  if (!idempotencyKey) return render("graphCancel", {isSuccess: false, reasonCodes: ["graph_cancel_idempotency_key_required"]});
  run("graphCancel", {graphKind: byId("graph-kind").value, idempotencyKey});
});
fetch("/session", {credentials: "same-origin"}).then(response => {
  if (!response.ok) throw new Error();
  return response.json();
}).then(session => csrf = session.csrf).catch(() => render("session", {isSuccess: false, reasonCodes: ["diagnostic_session_unavailable"]}));
