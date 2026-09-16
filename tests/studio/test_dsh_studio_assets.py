"""Exercise the actual Core plugin hook across a live frontend rebuild."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_live_studio_plugin_reads_new_frontend_asset_tags(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Studio Core plugin")
    plugin = (
        Path(__file__).parents[2] / "ksadk/plugins/providers/bundles/ksadk-dsh-studio/index.mjs"
    )
    script = """
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
const { apply } = await import(process.argv[1]);
const indexPath = process.argv[2];
const page = version => `<script type="module" src="/static/${version}.js"></script>`
  + `<link rel="stylesheet" href="/static/${version}.css">`;
writeFileSync(indexPath, page('a'));
let hook;
await apply({ on(name, callback) {
  assert.equal(name, 'webserver/index-inject'); hook = callback;
} }, { indexPath });
const first = []; hook(first);
assert.match(first[0].html, /a\\.js/);
writeFileSync(indexPath, page('b'));
const second = []; hook(second);
assert.match(second[0].html, /b\\.js/);
assert.match(second[0].html, /b\\.css/);
assert.doesNotMatch(second[0].html, /a\\.(js|css)/);
assert.match(second[0].html, /__STUDIO_DSH_BOOT__/);
writeFileSync(indexPath, '<html></html>');
assert.throws(() => hook([]), /assets are missing/);
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.as_uri(), str(tmp_path / "index.html")],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
