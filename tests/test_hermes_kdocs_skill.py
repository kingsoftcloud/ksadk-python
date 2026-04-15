from pathlib import Path


def test_kdocs_get_token_defaults_to_remote_pod_friendly_flow():
    skill_root = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "skills"
        / "kdocs"
    )

    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    get_token_js = (skill_root / "scripts" / "get-token.js").read_text(encoding="utf-8")
    get_token_sh = (skill_root / "scripts" / "get-token.sh").read_text(encoding="utf-8")

    assert "运行 `node scripts/get-token.js`" in skill
    assert "默认不会自动打开浏览器" in skill
    assert "mcporter 由 Hermes runtime 镜像预装" in skill
    assert "const SHOULD_OPEN_BROWSER =" in get_token_js
    assert "process.argv.includes(\"--open-browser\")" in get_token_js
    assert "if (SHOULD_OPEN_BROWSER && !openBrowser(loginUrl))" in get_token_js
    assert "SHOULD_OPEN_BROWSER=0" in get_token_sh
    assert "[ \"$arg\" = \"--open-browser\" ]" in get_token_sh
    assert "if [ \"$SHOULD_OPEN_BROWSER\" -eq 1 ]; then" in get_token_sh


def test_kdocs_scripts_resolve_skill_root_instead_of_scripts_dir():
    skill_root = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "skills"
        / "kdocs"
    )

    get_token_js = (skill_root / "scripts" / "get-token.js").read_text(encoding="utf-8")
    get_token_sh = (skill_root / "scripts" / "get-token.sh").read_text(encoding="utf-8")
    setup_sh = (skill_root / "scripts" / "setup.sh").read_text(encoding="utf-8")

    assert 'return path.resolve(getScriptDir(), "..", "SKILL.md");' in get_token_js
    assert 'return path.resolve(getScriptDir(), "..", ".env");' in get_token_js
    assert 'SKILL_FILE="$SCRIPT_DIR/../SKILL.md"' in get_token_sh
    assert 'LEGACY_ENV_FILE="$SCRIPT_DIR/../.env"' in get_token_sh
    assert 'SKILL_FILE="$SCRIPT_DIR/../SKILL.md"' in setup_sh
    assert 'LEGACY_ENV_FILE="$SCRIPT_DIR/../.env"' in setup_sh
