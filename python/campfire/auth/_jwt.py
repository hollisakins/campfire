"""Lightweight JWT payload decoding (no signature verification).

Used to read claims (``sub``, ``exp``) from CAMPFIRE access tokens and
Supabase JWTs for routing/expiry decisions — NOT for trust decisions. The
server verifies signatures; here we only need to read the payload locally.

Kept in ``auth/`` (with no ``deploy/`` imports) so both layers can share it.
"""

import base64
import json
from typing import Optional


def decode_payload(token: str) -> Optional[dict]:
    """Return the decoded JWT payload (middle segment), or None on failure.

    Does not verify the signature — for local claim inspection only.
    """
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to a multiple of 4
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def get_sub(token: str) -> Optional[str]:
    """Return the ``sub`` (subject / user_id) claim, or None."""
    payload = decode_payload(token)
    return payload.get("sub") if payload else None


def get_exp(token: str) -> Optional[int]:
    """Return the ``exp`` (expiry, Unix seconds) claim as int, or None."""
    payload = decode_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    try:
        return int(exp) if exp is not None else None
    except (ValueError, TypeError):
        return None
