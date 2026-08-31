"""Browser acceptance for installed DSH client bundle activation and disposal."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from studio_e2e_support import studio_server

PLUGIN = "@example/studio-workspace"


def _write_profile(root: Path) -> tuple[Path, Path]:
    home = root / "dsh-home"
    profile = home / "profiles" / "studio"
    package = profile / "node_modules" / "@example" / "studio-workspace"
    package.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {PLUGIN: "1.0.0"},
                "dsh": {"profile": {"bundles": [PLUGIN]}},
            }
        ),
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": PLUGIN,
                "version": "1.0.0",
                "exports": {"./client": "./lib/client.js"},
                "dsh": {
                    "bundle": {"patch": "./cordis.patch.yml"},
                    "client": {"platform": "web", "external": ["react"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (package / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    client = package / "lib" / "client.js"
    client.parent.mkdir()
    client.write_text(
        """window.__ModuleLoader__.load({
id:'@example/studio-workspace',
factory:(require)=>{
  const React=require('react');
  const Workspace=(props)=>React.createElement(
    'section', {'data-testid':'installed-dsh-workspace'}, `Agent ${props.currentAgentId||'none'}`
  );
  return {inject:['studio'],apply(ctx){
    ctx.studio.ui.register(ctx,'studio.sidebar.navigation',{
      id:'fixture.navigation',label:'DSH 扩展',path:'/extensions/dsh-fixture',order:30
    });
    ctx.studio.ui.register(ctx,'studio.route',{
      id:'fixture.route',path:'/extensions/dsh-fixture',title:'DSH 扩展',
      workspaceTabId:'fixture.tab'
    });
    ctx.studio.ui.register(ctx,'studio.workspace.tab',{
      id:'fixture.tab',label:'DSH 扩展',component:Workspace
    });
  }};
}});\n""",
        encoding="utf-8",
    )
    executable = root / "dsh-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.2-alpha.1;;\n"
        "  *--dump-config*) echo 'graph: fixture';;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return home, executable


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-dsh-client-e2e-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        home, executable = _write_profile(root)
        environment = {
            "KSADK_DSH_HOME": str(home),
            "KSADK_DSH_BIN": str(executable),
        }
        with (
            patch.dict("os.environ", environment),
            studio_server(workspace) as base_url,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(base_url, wait_until="networkidle")

                extension = page.get_by_role("button", name="DSH 扩展", exact=True)
                expect(extension).to_be_visible()
                extension.click()
                expect(page.get_by_test_id("installed-dsh-workspace")).to_be_visible()

                page.get_by_role("button", name="插件", exact=True).click()
                expect(page.get_by_role("button", name="停用", exact=True)).to_be_visible()
                page.get_by_role("button", name="停用", exact=True).click()
                expect(extension).to_be_hidden()
                expect(page.get_by_test_id("installed-dsh-workspace")).to_be_hidden()
                assert page_errors == [], page_errors
            finally:
                browser.close()


if __name__ == "__main__":
    main()
