"""Shared API layer for CAMPFIRE.

Provides authenticated session management and typed endpoint methods
used by both the CLI and the Python client.
"""

from .client import APIClient
from .session import APISession, create_download_session, resolve_base_url

__all__ = [
    "APIClient",
    "APISession",
    "create_download_session",
    "resolve_base_url",
]
