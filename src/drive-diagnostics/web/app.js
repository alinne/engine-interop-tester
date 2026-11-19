"use strict";
let csrf = "";
let selected = null;
let retainedArtifact = null;
let continuationToken = null;
let browseContext = null;
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
  if (clearArtifact) retainedArtifact = null;
}
function resetFolders({clearResult: resetResult = false} = {}) {
  byId("folder").replaceChildren(new Option("Load folders for this connection", ""));
  clearFiles({clearArtifact: true});
  retainedArtifact = null;
  if (resetResult) clearResult();
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
function render(action, data) {
  // HTTP success is transport evidence only; Engine's result controls the status.
  const success = data.isSuccess === true;
  byId("status").dataset.failed = String(!success);
  byId("status").textContent = success ? "Engine request succeeded." : "Request did not complete successfully. See reason codes below.";
  byId("result").textContent = JSON.stringify(data, null, 2);
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
  if (action === "browse") {
    clearFiles({resetBrowse: false, clearArtifact: true});
    const collections = success ? data.collections || [] : [];
    fillSelect("folder", collections, "collectionRef", "displayName");
    if (success && browseContext?.collectionRef) byId("folder").value = browseContext.collectionRef;
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
    retainedArtifact = data.retainedArtifactId || data.artifactId || data.artifacts?.[0]?.retainedArtifactId || data.artifacts?.[0]?.artifactId || null;
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
  browseContext = {
    connectionId: connectionId(),
    view: byId("view").value,
    ...(byId("folder").value ? {collectionRef: byId("folder").value} : {})
  };
  run("browse", browseContext);
});
byId("next-page").addEventListener("click", () => {
  if (!browseContext || !continuationToken) return;
  run("browse", {...browseContext, continuationToken});
});
byId("acquire").addEventListener("click", () => run("acquire", {connectionId: connectionId(), assetId: selected?.assetId || ""}));
byId("inspect").addEventListener("click", () => run("inspect", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || ""}));
byId("verify").addEventListener("click", () => run("verify", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || ""}));
byId("materialize").addEventListener("click", () => run("materialize", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || ""}));
byId("release").addEventListener("click", () => run("release", {connectionId: connectionId(), retainedArtifactId: retainedArtifact || ""}));
byId("revoke").addEventListener("click", () => {
  if (connectionId() && window.confirm("Revoke this selected Google Drive connection?")) run("revoke", {connectionId: connectionId()});
});
byId("graph").addEventListener("click", () => run("graph", {connectionId: connectionId()}));
fetch("/session", {credentials: "same-origin"}).then(response => {
  if (!response.ok) throw new Error();
  return response.json();
}).then(session => csrf = session.csrf).catch(() => render("session", {isSuccess: false, reasonCodes: ["diagnostic_session_unavailable"]}));
