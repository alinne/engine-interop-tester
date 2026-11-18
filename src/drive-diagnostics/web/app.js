"use strict";
let csrf = "";
let selected = null;
const byId = id => document.getElementById(id);
const connectionId = () => byId("connection").value;
function clearFiles() {
  selected = null;
  byId("selection").textContent = "Select one ISO or DICOM file.";
  byId("assets").replaceChildren();
  byId("pagination").hidden = true;
}
function resetFolders() {
  byId("folder").replaceChildren(new Option("Load folders for this connection", ""));
  clearFiles();
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
  if (action === "collections") {
    clearFiles();
    fillSelect("folder", success ? data.collections || [] : [], "collectionId", "displayName");
  }
  if (action === "assets") {
    clearFiles();
    for (const asset of success ? data.assets || [] : []) {
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
  if (action === "assets" || action === "collections") byId("pagination").hidden = !(data.hasMore || data.changeCursor || data.nextPageToken || data.continuationToken || data.incomplete);
}
async function run(action, payload = {}) {
  if (action === "start") {
    resetFolders();
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
byId("connection").addEventListener("change", resetFolders);
byId("folder").addEventListener("change", clearFiles);
byId("folders").addEventListener("click", () => run("collections", {connectionId: connectionId()}));
byId("files").addEventListener("click", () => run("assets", {connectionId: connectionId(), collectionRef: byId("folder").value}));
byId("acquire").addEventListener("click", () => run("acquire", {connectionId: connectionId(), assetId: selected?.assetId || ""}));
fetch("/session", {credentials: "same-origin"}).then(response => {
  if (!response.ok) throw new Error();
  return response.json();
}).then(session => csrf = session.csrf).catch(() => render("session", {isSuccess: false, reasonCodes: ["diagnostic_session_unavailable"]}));
