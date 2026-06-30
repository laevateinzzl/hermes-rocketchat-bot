from pathlib import Path

import yaml


def test_plugin_yaml_declares_hermes_platform_plugin():
    data = yaml.safe_load(Path("plugin.yaml").read_text())

    assert data["kind"] == "platform"
    assert data["label"] == "Rocket.Chat"
    assert data["name"] == "rocketchat-platform"


def test_plugin_yaml_surfaces_required_env_vars():
    data = yaml.safe_load(Path("plugin.yaml").read_text())
    required = {
        item["name"] if isinstance(item, dict) else item
        for item in data["requires_env"]
    }

    assert "ROCKETCHAT_SERVER_URL" in required
    assert "ROCKETCHAT_AUTH_MODE" in required
