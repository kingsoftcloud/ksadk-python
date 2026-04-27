import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "deploy" / "openclaw-user-template"
BUNDLED_FEISHU_EXAMPLE_ROOT = (
    TEMPLATE_ROOT / "examples" / "bundled-feishu-plugin-skills"
)
EXAMPLE_ROOT = TEMPLATE_ROOT / "examples" / "minimal-skill-plugin-deps"
LATEST_OPENCLAW_BASE_IMAGE = (
    "ghcr.io/openclaw/openclaw:2026.4.24@"
    "sha256:7c4370ff8777555d4c9fe5ab821aaaad7c87188d389a6cf761270725d96ec3e9"
)
LATEST_OPENCLAW_KCR_IMAGE = (
    "hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/openclaw:2026.4.24"
)


def test_openclaw_user_template_is_minimal_direct_start_bundle():
    assert TEMPLATE_ROOT.is_dir()
    assert (TEMPLATE_ROOT / "Dockerfile").is_file()
    assert (TEMPLATE_ROOT / "Makefile").is_file()
    assert (TEMPLATE_ROOT / "README.md").is_file()
    assert not (TEMPLATE_ROOT / "bootstrap-user.sh").exists()
    assert not (TEMPLATE_ROOT / "scripts").exists()
    assert not (TEMPLATE_ROOT / "safe-bin").exists()


def test_openclaw_user_template_contains_only_minimal_custom_area():
    assert (TEMPLATE_ROOT / "custom").is_dir()
    assert (TEMPLATE_ROOT / "custom" / "config").is_dir()
    assert (TEMPLATE_ROOT / "custom" / "extensions").is_dir()
    assert (TEMPLATE_ROOT / "custom" / "skills").is_dir()
    assert (TEMPLATE_ROOT / "custom" / "config" / "openclaw.json").is_file()
    assert not (TEMPLATE_ROOT / "custom" / "requirements.txt").exists()
    assert not (TEMPLATE_ROOT / "custom" / "npm-global.txt").exists()
    assert not (TEMPLATE_ROOT / "custom" / "packages.txt").exists()


def test_openclaw_user_template_default_config_seeds_trusted_proxy_runtime():
    config = json.loads(
        (TEMPLATE_ROOT / "custom" / "config" / "openclaw.json").read_text(
            encoding="utf-8"
        )
    )

    assert config["gateway"]["auth"]["mode"] == "trusted-proxy"
    assert (
        config["gateway"]["auth"]["trustedProxy"]["userHeader"]
        == "x-forwarded-user"
    )
    assert config["gateway"]["trustedProxies"] == [
        "127.0.0.1",
        "::1",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "35.0.0.0/8",
    ]


def test_openclaw_user_template_dockerfile_uses_official_startup_path():
    dockerfile = (TEMPLATE_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert f"ARG OPENCLAW_BASE_IMAGE={LATEST_OPENCLAW_BASE_IMAGE}" in dockerfile
    assert "FROM ${OPENCLAW_BASE_IMAGE}" in dockerfile
    assert "COPY custom/extensions /opt/openclaw-template/extensions" in dockerfile
    assert "COPY custom/skills /opt/openclaw-template/skills" in dockerfile
    assert "COPY custom/config/openclaw.json /opt/openclaw-template/config/openclaw.json" in dockerfile
    assert "bootstrap-user.sh" not in dockerfile
    assert "safe-bin" not in dockerfile
    assert "ENTRYPOINT" not in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert 'OPENCLAW_GATEWAY_PORT=8080' in dockerfile
    assert 'OPENCLAW_GATEWAY_AUTH_MODE=trusted-proxy' in dockerfile
    assert 'NPM_CONFIG_REGISTRY=https://registry.npmmirror.com' in dockerfile
    assert 'PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple' in dockerfile
    assert 'CLAWHUB_SITE=https://cn.clawhub-mirror.com' in dockerfile
    assert 'CLAWHUB_REGISTRY=https://cn.clawhub-mirror.com' in dockerfile
    assert 'CMD ["sh","-lc"' in dockerfile
    assert '/home/node/.openclaw' in dockerfile
    assert 'trusted-proxy|token|none' in dockerfile
    assert 'secrets.json' in dockerfile


def test_openclaw_user_template_readme_mentions_direct_build_and_run():
    readme = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "docker build" in readme
    assert "docker run" in readme
    assert "不修改任何文件" in readme
    assert "8080" in readme
    assert "自定义环境变量" in readme
    assert "不会自动写进 openclaw.json" in readme
    assert "/home/node/.openclaw" in readme
    assert "trusted-proxy" in readme
    assert "OPENCLAW_GATEWAY_TOKEN" in readme
    assert "none" in readme
    assert "CLAWHUB_SITE" in readme
    assert "CLAWHUB_REGISTRY" in readme
    assert "https://cn.clawhub-mirror.com" in readme
    assert "Linux x86-64" in readme or "amd64" in readme


def test_openclaw_user_template_makefile_provides_basic_commands():
    makefile = (TEMPLATE_ROOT / "Makefile").read_text(encoding="utf-8")

    assert f"OPENCLAW_BASE_IMAGE ?= {LATEST_OPENCLAW_BASE_IMAGE}" in makefile
    assert "build:" in makefile
    assert "run:" in makefile
    assert "push:" in makefile
    assert "linux/amd64" in makefile


def test_openclaw_user_template_links_examples():
    readme = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "examples/bundled-feishu-plugin-skills/" in readme
    assert "内置飞书" in readme
    assert "examples/minimal-skill-plugin-deps/" in readme
    assert "自定义 plugin" in readme


def test_openclaw_user_template_bundled_feishu_example_contains_expected_files():
    assert BUNDLED_FEISHU_EXAMPLE_ROOT.is_dir()
    assert (BUNDLED_FEISHU_EXAMPLE_ROOT / "Dockerfile").is_file()
    assert (BUNDLED_FEISHU_EXAMPLE_ROOT / "Makefile").is_file()
    assert (BUNDLED_FEISHU_EXAMPLE_ROOT / "README.md").is_file()
    assert not (BUNDLED_FEISHU_EXAMPLE_ROOT / "custom").exists()


def test_openclaw_user_template_bundled_feishu_example_reuses_official_bootstrap():
    dockerfile = (BUNDLED_FEISHU_EXAMPLE_ROOT / "Dockerfile").read_text(
        encoding="utf-8"
    )
    readme = (BUNDLED_FEISHU_EXAMPLE_ROOT / "README.md").read_text(
        encoding="utf-8"
    )

    assert "FROM ${OPENCLAW_BASE_IMAGE}" in dockerfile
    assert LATEST_OPENCLAW_KCR_IMAGE in dockerfile
    assert "ENTRYPOINT" not in dockerfile
    assert 'CMD [' not in dockerfile
    assert 'CMD "' not in dockerfile
    assert "OPENCLAW_GATEWAY_PORT=8080" in dockerfile
    assert "CLAWHUB_SITE=https://cn.clawhub-mirror.com" in dockerfile
    assert "CLAWHUB_REGISTRY=https://cn.clawhub-mirror.com" in dockerfile
    assert "openclaw-lark" in readme
    assert "OPENCLAW_CHANNEL_BOOTSTRAP_JSON" in readme
    assert "feishu-bitable" in readme
    assert "feishu-task" in readme


def test_openclaw_user_template_advanced_example_contains_expected_files():
    assert EXAMPLE_ROOT.is_dir()
    assert (EXAMPLE_ROOT / "Dockerfile").is_file()
    assert (EXAMPLE_ROOT / "Makefile").is_file()
    assert (EXAMPLE_ROOT / "README.md").is_file()
    assert (
        EXAMPLE_ROOT
        / "custom"
        / "extensions"
        / "demo-now-plugin"
        / "openclaw.plugin.json"
    ).is_file()
    assert (
        EXAMPLE_ROOT / "custom" / "extensions" / "demo-now-plugin" / "package.json"
    ).is_file()
    assert (
        EXAMPLE_ROOT / "custom" / "extensions" / "demo-now-plugin" / "index.ts"
    ).is_file()
    assert (
        EXAMPLE_ROOT / "custom" / "skills" / "demo-plugin-now" / "SKILL.md"
    ).is_file()


def test_openclaw_user_template_advanced_example_enables_custom_plugin():
    config = json.loads(
        (EXAMPLE_ROOT / "custom" / "config" / "openclaw.json").read_text(
            encoding="utf-8"
        )
    )

    assert config["plugins"]["entries"]["demo-now"]["enabled"] is True
    assert config["gateway"]["auth"]["trustedProxy"]["userHeader"] == "x-forwarded-user"


def test_openclaw_user_template_readme_mentions_optional_token_mode():
    readme = (TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "token" in readme
    assert "OPENCLAW_GATEWAY_PASSWORD" in readme


def test_openclaw_user_template_advanced_example_pins_latest_official_base_image():
    dockerfile = (EXAMPLE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    makefile = (EXAMPLE_ROOT / "Makefile").read_text(encoding="utf-8")

    expected = LATEST_OPENCLAW_BASE_IMAGE

    assert expected in dockerfile
    assert expected in makefile


def test_openclaw_user_template_advanced_example_builds_plugin_deps():
    dockerfile = (EXAMPLE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    readme = (EXAMPLE_ROOT / "README.md").read_text(encoding="utf-8")
    package_json = json.loads(
        (
            EXAMPLE_ROOT
            / "custom"
            / "extensions"
            / "demo-now-plugin"
            / "package.json"
        ).read_text(encoding="utf-8")
    )

    assert "npm install --omit=dev" in dockerfile
    assert "OPENCLAW_NPM_REGISTRY" in dockerfile
    assert "demo_now" in readme
    assert "dayjs" in readme
    assert package_json["dependencies"]["dayjs"].startswith("^1.11.")
