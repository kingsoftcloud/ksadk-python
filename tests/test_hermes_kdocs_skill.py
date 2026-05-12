from pathlib import Path


def test_hermes_dockerfile_clones_official_kdocs_skill():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "ARG KDOCS_SKILL_REPO=https://github.com/kdocs-app/kdocs-skill.git" in dockerfile
    assert 'git clone --depth 1 "${KDOCS_SKILL_REPO}" /tmp/kdocs-skill' in dockerfile
    assert 'cp -R /tmp/kdocs-skill /app/skills/kdocs' in dockerfile


def test_hermes_dockerfile_adds_kdocs_setup_compat_shim():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' \\' in dockerfile
    assert 'exec bash "${SCRIPT_DIR}/scripts/setup.sh" "$@"' in dockerfile
