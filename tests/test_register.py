"""Tests for register(ctx) — the Hermes plugin entry point."""

from adapter import RocketChatAdapter, check_requirements, register


class FakeContext:
    """Simulates the Hermes plugin registry context."""

    def __init__(self):
        self.platforms = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


def test_register_declares_platform_hooks():
    """register(ctx) should call ctx.register_platform with all required hooks."""
    ctx = FakeContext()

    register(ctx)

    assert len(ctx.platforms) == 1
    platform = ctx.platforms[0]

    # Basic identity
    assert platform["name"] == "rocketchat"
    assert platform["label"] == "Rocket.Chat"

    # Hook presence
    assert callable(platform["adapter_factory"])
    assert callable(platform["check_fn"])
    assert callable(platform["env_enablement_fn"])
    assert callable(platform["standalone_sender_fn"])

    # Environment variable declarations
    assert "ROCKETCHAT_SERVER_URL" in platform.get("required_env", [])
    assert "ROCKETCHAT_AUTH_MODE" in platform.get("required_env", [])

    # Allowlist config
    assert platform.get("allowed_users_env") == "ROCKETCHAT_ALLOWED_USERS"
    assert platform.get("allow_all_env") == "ROCKETCHAT_ALLOW_ALL_USERS"

    # Cron / home channel
    assert platform.get("cron_deliver_env_var") == "ROCKETCHAT_HOME_CHANNEL"


def test_register_adapter_factory_creates_adapter():
    """The adapter_factory callable should return a RocketChatAdapter."""
    ctx = FakeContext()
    register(ctx)

    factory = ctx.platforms[0]["adapter_factory"]
    adapter = factory({})

    # The adapter should be importable and have expected methods
    assert hasattr(adapter, "connect")
    assert hasattr(adapter, "disconnect")
    assert hasattr(adapter, "send")
    assert hasattr(adapter, "handle_message")


def test_register_env_enablement_fn_is_callable():
    ctx = FakeContext()
    register(ctx)

    fn = ctx.platforms[0]["env_enablement_fn"]
    # Should be a callable
    assert callable(fn)


def test_register_standalone_sender_fn_is_callable():
    ctx = FakeContext()
    register(ctx)

    fn = ctx.platforms[0]["standalone_sender_fn"]
    assert callable(fn)


def test_check_requirements_returns_bool():
    """Hermes platform_registry expects check_fn() to return a plain bool."""
    assert isinstance(check_requirements(), bool)


def test_adapter_initializes_platform_identity_in_isolated_tests():
    """The local stub should preserve the same platform identity Hermes uses."""
    adapter = RocketChatAdapter({})

    assert getattr(adapter, "platform", None) == "rocketchat"


def test_register_includes_platform_metadata():
    """Platform registration should include descriptive metadata."""
    ctx = FakeContext()
    register(ctx)

    platform = ctx.platforms[0]

    # Platform hint gives AI context about the chat environment
    assert "platform_hint" in platform
    assert "Rocket.Chat" in platform["platform_hint"]

    # Emoji for UI display
    assert "emoji" in platform

    # Max message length for truncation
    assert platform.get("max_message_length", 0) > 0
