"""Rocket.Chat platform adapter for Hermes Agent gateway."""

try:
    from .adapter import register  # Hermes loads this as a package
except ImportError:
    from adapter import register  # fallback for direct execution / testing

__all__ = ["register"]
