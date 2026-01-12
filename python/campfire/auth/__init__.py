"""
CAMPFIRE authentication module.

This module provides authentication functionality for the CAMPFIRE Python client,
including credential storage, device flow authentication, and token management.
"""

from .credentials import CredentialManager, StoredCredentials
from .device_flow import DeviceFlowAuth
from .tokens import TokenManager

__all__ = [
    "CredentialManager",
    "StoredCredentials",
    "DeviceFlowAuth",
    "TokenManager",
]
