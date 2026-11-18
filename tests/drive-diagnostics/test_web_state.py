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
assert.equal(element('selection').textContent, 'Select one ISO or DICOM file.');
"""
        app = Path(__file__).parents[2] / "src/drive-diagnostics/web/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
