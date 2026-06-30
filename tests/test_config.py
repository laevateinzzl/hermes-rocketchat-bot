"""Tests for configuration parsing (RocketChatConfig, parse_config, env_enablement)."""

from adapter import RocketChatConfig, env_enablement, parse_config


def test_token_config_from_env(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_TRANSPORT", "websocket")
    monkeypatch.setenv("ROCKETCHAT_MENTION_NAMES", "hermes,assistant")

    cfg = parse_config({})

    assert cfg.server_url == "https://chat.example.com"
    assert cfg.auth_mode == "token"
    assert cfg.user_id == "u1"
    assert cfg.access_token == "tok"
    assert cfg.transport == "websocket"
    assert cfg.mention_names == ["hermes", "assistant"]


def test_password_config_from_env(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.internal")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "password")
    monkeypatch.setenv("ROCKETCHAT_USERNAME", "hermesbot")
    monkeypatch.setenv("ROCKETCHAT_PASSWORD", "secret")

    cfg = parse_config({})

    assert cfg.server_url == "https://chat.internal"
    assert cfg.auth_mode == "password"
    assert cfg.username == "hermesbot"
    assert cfg.password == "secret"


def test_default_transport_is_polling(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")

    cfg = parse_config({})

    assert cfg.transport == "polling"


def test_extra_overrides_env_for_explicit_keys(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://env.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "env_user")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "env_tok")

    extra = {
        "server_url": "https://extra.example.com",
        "user_id": "extra_user",
    }

    cfg = parse_config(extra)

    # extra should win for explicitly-set keys
    assert cfg.server_url == "https://extra.example.com"
    assert cfg.user_id == "extra_user"
    # auth_mode and access_token only come from env
    assert cfg.auth_mode == "token"
    assert cfg.access_token == "env_tok"


def test_env_enablement_returns_seed_when_minimally_configured(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_HOME_CHANNEL", "room1")

    seed = env_enablement()

    assert seed is not None
    assert seed["server_url"] == "https://chat.example.com"
    assert seed["home_channel"]["chat_id"] == "room1"


def test_env_enablement_returns_none_when_missing_required(monkeypatch):
    # No required env vars set
    seed = env_enablement()
    assert seed is None


def test_env_enablement_with_allow_all(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_ALLOW_ALL_USERS", "true")

    seed = env_enablement()

    assert seed is not None
    assert seed["allow_all"] == True
    assert seed["allowed_users"] == []


def test_env_enablement_with_allowed_users(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_ALLOWED_USERS", "alice,bob")

    seed = env_enablement()

    assert seed is not None
    assert seed["allowed_users"] == ["alice", "bob"]


def test_parse_config_mention_names_default_empty(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")

    cfg = parse_config({})

    assert cfg.mention_names == []


def test_parse_config_allowed_users(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_ALLOWED_USERS", "u1,u2")

    cfg = parse_config({})

    assert cfg.allowed_users == ["u1", "u2"]


def test_parse_config_allow_all(monkeypatch):
    monkeypatch.setenv("ROCKETCHAT_SERVER_URL", "https://chat.example.com")
    monkeypatch.setenv("ROCKETCHAT_AUTH_MODE", "token")
    monkeypatch.setenv("ROCKETCHAT_USER_ID", "u1")
    monkeypatch.setenv("ROCKETCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ROCKETCHAT_ALLOW_ALL_USERS", "1")

    cfg = parse_config({})

    assert cfg.allow_all == True
