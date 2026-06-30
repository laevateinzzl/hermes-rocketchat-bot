"""Tests that README.md contains complete documentation."""

from pathlib import Path


def test_readme_documents_install_path():
    text = Path("README.md").read_text()
    assert "~/.hermes/plugins/rocketchat" in text


def test_readme_documents_required_env_vars():
    text = Path("README.md").read_text()
    assert "ROCKETCHAT_SERVER_URL" in text
    assert "ROCKETCHAT_AUTH_MODE" in text


def test_readme_documents_allowlist():
    text = Path("README.md").read_text()
    assert "ROCKETCHAT_ALLOWED_USERS" in text


def test_readme_documents_hermes_gateway():
    text = Path("README.md").read_text()
    assert "hermes gateway" in text.lower()


def test_readme_documents_token_auth():
    text = Path("README.md").read_text()
    assert "ROCKETCHAT_USER_ID" in text
    assert "ROCKETCHAT_ACCESS_TOKEN" in text


def test_readme_documents_transport_choice():
    text = Path("README.md").read_text()
    assert "websocket" in text.lower() or "polling" in text.lower()


def test_readme_documents_mention_behavior():
    text = Path("README.md").read_text()
    assert "mention" in text.lower()


def test_readme_documents_thread_behavior():
    text = Path("README.md").read_text()
    assert "thread" in text.lower()


def test_readme_documents_install_method():
    text = Path("README.md").read_text()
    assert "cp -r" in text or "symlink" in text or "git clone" in text


def test_readme_section_markup():
    """README should use Markdown headings for structure."""
    text = Path("README.md").read_text()
    # Should have at least one heading
    assert "# " in text
