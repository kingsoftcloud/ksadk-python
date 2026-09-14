from pathlib import Path

from ksadk.plugins.bridges.codex import CodexPluginInventory
from ksadk.studio.api_plugin_routes import _local_plugin_artwork, _public_plugin_interface


def _inventory(root: Path, logo: Path) -> CodexPluginInventory:
    return CodexPluginInventory(plugin_id="test", name="test", marketplace_name="test", marketplace_path=str(root.parent), version="1.0.0", installed=True, enabled=True, availability="AVAILABLE", source={"type": "local", "path": str(root)}, interface={"logo": str(logo)})


def test_local_artwork_is_an_image_and_cannot_escape_plugin_root(tmp_path):
    root = tmp_path / "plugin"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text('{}')
    logo = root / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    assert _local_plugin_artwork(_inventory(root, logo)).startswith("data:image/png;base64,")
    outside = tmp_path / "private.png"
    outside.write_bytes(logo.read_bytes())
    assert _local_plugin_artwork(_inventory(root, outside)) is None
    link = root / "link.png"
    link.symlink_to(outside)
    assert _local_plugin_artwork(_inventory(root, link)) is None
    logo.write_text("not an image")
    assert _local_plugin_artwork(_inventory(root, logo)) is None
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (256 * 1024))
    assert _local_plugin_artwork(_inventory(root, logo)) is None


def test_public_interface_drops_host_paths_and_unsafe_links():
    assert _public_plugin_interface({"displayName": "Demo", "logo": "/private/key", "logoUrl": "file:///private/key", "websiteUrl": "https://user:password@example.com", "privacyPolicyUrl": "javascript:alert(1)", "defaultPrompt": ["Try demo", {"bad": "data"}]}) == {"displayName": "Demo", "defaultPrompt": ["Try demo"]}
