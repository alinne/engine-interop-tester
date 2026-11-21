"""Exercise sign-in UI state without a provider account or browser dependency."""
from pathlib import Path
import subprocess
import unittest


class WebStateTests(unittest.TestCase):
    def test_start_clears_previous_account_before_network_completes(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: 'old-account', hidden: false, href: 'https://accounts.google.com/old',
    textContent: 'old selection', children: [], dataset: {},
    addEventListener() {}, add(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    removeAttribute(name) { delete this[name]; }
  });
  return elements.get(id);
}
let actionRequested = false;
const context = vm.createContext({
  document: { getElementById: element, querySelectorAll: () => [] },
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: url => {
    if (url === '/session') return Promise.resolve({ok: true, json: async () => ({csrf: 'test'})});
    actionRequested = true;
    return new Promise(() => {}); // Inspect state while the new attempt is pending.
  }, URL
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
element('assets').children = ['old-file'];
context.run('start');
assert.equal(actionRequested, true);
assert.equal(element('consent').hidden, true);
assert.equal(element('consent').href, undefined);
assert.equal(element('connection').value, '');
assert.equal(element('folder').value, '');
assert.equal(element('assets').children.length, 0);
assert.equal(element('selection').textContent, 'Select one downloadable file.');
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_browse_cursor_is_invalidated_when_view_changes(self):
        source = (Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js").read_text(encoding="utf-8")
        page = (Path(__file__).parents[2] / "src/drive-diagnostics/web/index.html").read_text(encoding="utf-8")
        self.assertIn('byId("view").addEventListener("change", changeBrowseView)', source)
        self.assertIn('byId("folder").replaceChildren(new Option("Browse to load folders", ""))', source)
        self.assertIn('byId("open-folder").addEventListener("click", openSelectedFolder)', source)
        self.assertIn('id="open-folder"', page)
        self.assertNotIn('id="files"', page)
        self.assertNotIn('shared-drive-folder', page)
        self.assertIn('run("browse", {...browseContext, continuationToken})', source)
        self.assertIn('clearFiles({resetBrowse: false, clearArtifact: true})', source)
        self.assertIn('clearFiles({clearArtifact: true});', source)

    def test_retained_actions_support_reload_reference_and_explicit_delete(self):
        source = (Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js").read_text(encoding="utf-8")
        self.assertIn('if (retainedArtifact) return {retainedArtifactId: retainedArtifact};', source)
        self.assertIn('return {artifactId: explicit};', source)
        self.assertIn('runRetained("delete", {purpose, idempotencyKey});', source)
        self.assertIn('Permanently delete this retained artifact? This cannot be undone.', source)
        page = (Path(__file__).parents[2] / "src/drive-diagnostics/web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="delete-purpose"', page)
        self.assertIn('id="delete-idempotency"', page)
        self.assertIn('id="delete"', page)
        self.assertIn("Engine checks access against the authenticated owner scope.", page)
        self.assertNotIn("selected connection and owner scope", page)

    def test_editing_artifact_reference_clears_remembered_binding(self):
        source = (Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js").read_text(encoding="utf-8")
        self.assertIn('byId("artifact-id").addEventListener("input"', source)
        self.assertIn('retainedArtifact = null;', source)
        self.assertIn('retainedArtifactDetails = null;', source)
        self.assertIn('if (retainedArtifact && typed !== retainedArtifact)', source)

    def test_open_selected_folder_uses_current_engine_folder_binding(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const listeners = new Map();
const requests = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, href: '', textContent: '', children: [], dataset: {},
    addEventListener(type, callback) { listeners.set(`${id}:${type}`, callback); },
    add(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    removeAttribute(name) { delete this[name]; },
  });
  return elements.get(id);
}
const context = vm.createContext({
  document: { getElementById: element, querySelectorAll: () => [] },
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: (url, options) => url === '/session'
    ? Promise.resolve({ok: true, json: async () => ({csrf: 'test'})})
    : (requests.push(JSON.parse(options.body)), Promise.resolve({ok: true, json: async () => ({isSuccess: false})})),
  URL,
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  const folderRef = 'cloud-selection:connection-1:folder-1';
  context.render('browse', {
    isSuccess: true,
    collections: [{collectionRef: folderRef, displayName: 'Shared study', kind: 'folder', driveId: 'drive-1'}],
    assets: [],
  });
  element('connection').value = 'connection-1';
  element('folder').value = folderRef;
  const graphPayload = context.graphBrowsePayload();
  assert.equal(graphPayload.connectionId, 'connection-1');
  assert.equal(graphPayload.locationKind, 'my_drive_root');
  assert.equal(graphPayload.locationRef, undefined);
  await listeners.get('open-folder:click')();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].action, 'browse');
  assert.deepEqual(requests[0].payload, {
    connectionId: 'connection-1',
    locationKind: 'folder',
    locationRef: folderRef,
    driveId: 'drive-1',
  });
  const driveRef = 'cloud-selection:connection-1:drive-1';
  context.render('browse', {
    isSuccess: true,
    collections: [{collectionRef: driveRef, displayName: 'Shared drive', kind: 'shared_drive', driveId: 'drive-1'}],
    assets: [],
  });
  element('folder').value = driveRef;
  await listeners.get('open-folder:click')();
  assert.equal(requests[1].payload.locationKind, 'shared_drive');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_view_and_account_changes_clear_folder_binding_before_open(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const listeners = new Map();
const requests = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, href: '', textContent: '', children: [], dataset: {},
    addEventListener(type, callback) { listeners.set(`${id}:${type}`, callback); },
    add(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    removeAttribute(name) { delete this[name]; },
  });
  return elements.get(id);
}
const context = vm.createContext({
  document: { getElementById: element, querySelectorAll: () => [] },
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: (url, options) => url === '/session'
    ? Promise.resolve({ok: true, json: async () => ({csrf: 'test'})})
    : (requests.push(JSON.parse(options.body)), Promise.resolve({ok: true, json: async () => ({isSuccess: false})})),
  URL,
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  const folderRef = 'cloud-selection:connection-1:folder-1';
  context.render('browse', {
    isSuccess: true,
    collections: [{collectionRef: folderRef, displayName: 'Shared study', driveId: 'drive-1'}],
    assets: [],
  });
  element('connection').value = 'connection-1';
  element('folder').value = folderRef;
  listeners.get('view:change')();
  assert.equal(element('folder').value, '');
  assert.equal(element('folder').children[0].text, 'Browse to load folders');
  element('folder').value = folderRef;
  await listeners.get('open-folder:click')();
  assert.equal(requests.length, 0);

  element('connection').value = 'connection-2';
  listeners.get('connection:change')();
  assert.equal(element('folder').value, '');
  assert.equal(element('assets').children.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_asset_selection_allows_downloadable_binary_files_but_not_workspace_files(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, href: '', textContent: '', children: [], dataset: {},
    addEventListener() {}, add(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    removeAttribute(name) { delete this[name]; },
  });
  return elements.get(id);
}
const context = vm.createContext({
  document: {
    getElementById: element,
    querySelectorAll: () => [],
    createElement: tag => ({
      tagName: tag,
      children: [],
      disabled: false,
      append(...children) { this.children.push(...children); },
      setAttribute(name, value) { this[name] = value; },
      addEventListener() {},
    }),
  },
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: url => url === '/session'
    ? Promise.resolve({ok: true, json: async () => ({csrf: 'test'})})
    : Promise.reject(new Error('unexpected request')),
  URL,
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.renderAssets([
  {displayName: 'scan.bin', mimeType: 'application/octet-stream', canDownload: true},
  {displayName: 'native-slides', mimeType: 'application/vnd.google-apps.presentation', canDownload: true},
  {displayName: 'blocked.bin', mimeType: 'application/octet-stream', canDownload: false},
  {displayName: 'unknown.bin', mimeType: 'application/octet-stream'},
  {displayName: 'metadata-blocked.bin', mimeType: 'application/octet-stream', metadata: {driveCanDownload: 'false'}},
]);
const rows = element('assets').children;
assert.equal(rows.length, 5);
assert.equal(rows[0].children[0].children[0].disabled, false);
assert.equal(rows[1].children[0].children[0].disabled, true);
assert.equal(rows[2].children[0].children[0].disabled, true);
assert.equal(rows[3].children[0].children[0].disabled, false);
assert.equal(rows[4].children[0].children[0].disabled, true);
assert.equal(rows[0].children[4].textContent, 'Downloadable');
assert.equal(rows[1].children[4].textContent, 'Unavailable');
assert.equal(rows[2].children[4].textContent, 'Unavailable');
assert.equal(rows[3].children[4].textContent, 'Checked on acquisition');
assert.equal(rows[4].children[4].textContent, 'Unavailable');
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_canonical_cloud_selection_asset_id_is_used_by_direct_and_graph_acquire(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const listeners = new Map();
const requests = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, href: '', textContent: '', children: [], dataset: {},
    addEventListener(type, callback) { listeners.set(`${id}:${type}`, callback); },
    add(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    removeAttribute(name) { delete this[name]; },
  });
  return elements.get(id);
}
function node(tag) {
  return {
    tagName: tag, children: [], disabled: false,
    addEventListener(type, callback) { listeners.set(`${tag}:${type}`, callback); this.listener = callback; },
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
  };
}
const context = vm.createContext({
  document: {
    getElementById: element,
    querySelectorAll: () => [],
    createElement: node,
  },
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: (url, options) => url === '/session'
    ? Promise.resolve({ok: true, json: async () => ({csrf: 'test'})})
    : (requests.push(JSON.parse(options.body)), Promise.resolve({ok: true, json: async () => ({isSuccess: false})})),
  URL,
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  const cloudSelection = 'cloud-selection:connection-1:asset-1';
  context.renderAssets([{assetId: cloudSelection, displayName: 'study.dcm', mimeType: 'application/dicom'}]);
  const choose = element('assets').children[0].children[0].children[0];
  choose.listener();
  await listeners.get('acquire:click')();
  await listeners.get('graph-prepare-acquire:click')();
  assert.equal(requests[0].payload.selectionRef, cloudSelection);
  assert.equal(requests[1].payload.selectionRef, cloudSelection);

  element('assets').replaceChildren();
  context.renderAssets([{assetId: 'provider-file-id', displayName: 'raw.bin', mimeType: 'application/octet-stream'}]);
  element('assets').children[0].children[0].children[0].listener();
  await listeners.get('acquire:click')();
  assert.equal(requests[2].payload.selectionRef, '');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_revoke_clears_connection_scoped_state_only_after_success(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, href: '', textContent: '', children: [], dataset: {},
    addEventListener() {}, add(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    removeAttribute(name) { delete this[name]; },
  });
  return elements.get(id);
}
const context = vm.createContext({
  document: { getElementById: element, querySelectorAll: () => [] },
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: url => url === '/session'
    ? Promise.resolve({ok: true, json: async () => ({csrf: 'test'})})
    : Promise.reject(new Error('unexpected request')),
  URL,
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
function seed() {
  element('connection').value = 'connection-1';
  element('folder').value = 'folder-1';
  element('folder').children = ['folder'];
  element('assets').children = ['asset'];
  element('selection').textContent = 'scan.bin';
  element('artifact-id').value = 'artifact-1';
  element('graph-scope-id').value = 'scope-1';
  element('graph-invocation-ref').value = 'invocation-1';
  element('graph-cursor').value = '{"scope":"scope-1"}';
  element('retained-artifact-status').textContent = 'Bound artifact: artifact-1';
}
seed();
context.render('revoke', {isSuccess: false, reasonCodes: ['revoke_failed']});
assert.equal(element('connection').value, 'connection-1');
assert.equal(element('folder').value, 'folder-1');
assert.equal(element('artifact-id').value, 'artifact-1');
assert.equal(element('graph-scope-id').value, 'scope-1');
assert.equal(element('assets').children.length, 1);
seed();
context.render('revoke', {isSuccess: true});
assert.equal(element('connection').value, '');
assert.equal(element('folder').value, '');
assert.equal(element('artifact-id').value, '');
assert.equal(element('graph-scope-id').value, '');
assert.equal(element('graph-invocation-ref').value, '');
assert.equal(element('graph-cursor').value, '');
assert.equal(element('assets').children.length, 0);
assert.equal(element('selection').textContent, 'Select one downloadable file.');
assert.match(element('retained-artifact-status').textContent, /No retained artifact/);
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
