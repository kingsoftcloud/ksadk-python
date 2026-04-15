from pathlib import Path


def test_hermes_dockerfile_builds_and_copies_dashboard_assets():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "web_dist" in dockerfile
    assert "npm run build" in dockerfile
