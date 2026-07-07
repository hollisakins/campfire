"""Regression tests for the deploy Supabase client auth wiring.

Guards the supabase-py 2.x failure mode where authenticating via
``client.postgrest.auth(token)`` *after* construction silently leaves the
anon key on the wire: ``Client.postgrest`` is a lazily-built, cached client
constructed from ``options.headers``, and the GoTrue auth-state listener
resets that cache, reverting ``Authorization`` to the anon key. Requests then
run as role ``anon`` -> ``auth.uid()`` NULL -> ``is_admin()`` false -> admin
writes (e.g. the ``observations`` upsert) fail intermittently with a 42501 RLS
error. The fix bakes the user JWT into the client options headers at
construction so every (re)build carries it.
"""
import pytest

from campfire.deploy.supabase import (
    _make_user_client,
    get_supabase_client,
    AutoRefreshClient,
)

URL = "https://example.supabase.co"
ANON = "anon-key"
JWT = "user-jwt-token"
JWT2 = "refreshed-jwt-token"


def _auth_header(client):
    """Effective Authorization header the postgrest client will send."""
    return client.postgrest.session.headers.get("Authorization")


def test_make_user_client_sends_user_jwt():
    c = _make_user_client(URL, ANON, JWT)
    assert _auth_header(c) == f"Bearer {JWT}"


def test_make_user_client_rejects_empty_token():
    # A falsy token would yield "Bearer None"/"Bearer " (effectively anon).
    with pytest.raises(ValueError):
        _make_user_client(URL, ANON, None)
    with pytest.raises(ValueError):
        _make_user_client(URL, ANON, "")


def test_login_mode_requires_token_and_anon():
    # Missing anon_key or token must raise, never silently build an anon client.
    with pytest.raises(ValueError):
        get_supabase_client({"supabase": {"url": URL, "supabase_token": JWT,
                                          "_auth_mode": "login"}})
    with pytest.raises(ValueError):
        get_supabase_client({"supabase": {"url": URL, "anon_key": ANON,
                                          "_auth_mode": "login"}})


def test_user_jwt_survives_postgrest_cache_reset():
    # Reproduces the GoTrue auth-event reset (Client sets _postgrest=None);
    # the user JWT must persist across the rebuild, not revert to the anon key.
    c = _make_user_client(URL, ANON, JWT)
    c._postgrest = None
    assert _auth_header(c) == f"Bearer {JWT}"


def test_get_supabase_client_user_path_is_not_anon():
    config = {"supabase": {"url": URL, "anon_key": ANON, "supabase_token": JWT}}
    c = get_supabase_client(config)
    assert _auth_header(c) == f"Bearer {JWT}"
    assert ANON not in (_auth_header(c) or "")


class _FakeTokenManager:
    def __init__(self, new_token):
        self._new_token = new_token
        self.refresh_calls = 0
        self._needs = True

    def supabase_token_needs_refresh(self, *args, **kwargs):
        return self._needs

    def force_refresh_supabase_token(self):
        self.refresh_calls += 1
        self._needs = False
        return self._new_token


def test_autorefresh_rebuilds_with_refreshed_token():
    tm = _FakeTokenManager(JWT2)
    client = AutoRefreshClient(URL, ANON, JWT, tm)
    assert _auth_header(client._client) == f"Bearer {JWT}"
    # First table() call detects expiry and rebuilds the client carrying JWT2.
    client.table("observations")
    assert tm.refresh_calls == 1
    assert _auth_header(client._client) == f"Bearer {JWT2}"


def test_factory_wraps_in_autorefresh_when_token_manager_present():
    tm = _FakeTokenManager(JWT2)
    config = {"supabase": {"url": URL, "anon_key": ANON,
                           "supabase_token": JWT, "_token_manager": tm}}
    c = get_supabase_client(config)
    assert isinstance(c, AutoRefreshClient)
    assert _auth_header(c._client) == f"Bearer {JWT}"
