"""Tests for coherent deploy auth-mode resolution, Supabase-token refresh, and
the admin gate that runs through the actual write client.

All offline: env/TOML are controlled via monkeypatch/tmp_path, and the login
``TokenManager`` and Supabase client are faked.
"""
import base64
import json
import time
from types import SimpleNamespace

import pytest

from campfire.auth.credentials import StoredCredentials
from campfire.auth.tokens import TokenManager
from campfire.deploy.config import load_config
from campfire.deploy.cli import _gate_admin

_SUPABASE_ENV = (
    "CAMPFIRE_SUPABASE_URL", "CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY",
    "CAMPFIRE_R2_ACCOUNT_ID", "CAMPFIRE_R2_ACCESS_KEY_ID",
    "CAMPFIRE_R2_SECRET_ACCESS_KEY", "CAMPFIRE_R2_BUCKET_NAME",
)


def _clear_env(monkeypatch):
    monkeypatch.delenv("CAMPFIRE_ROOT", raising=False)
    for var in _SUPABASE_ENV:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# Supabase-token refresh keyed on the token's own exp
# --------------------------------------------------------------------------

def _jwt_with_exp(exp):
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u", "exp": exp}).encode()
    ).decode().rstrip("=")
    return f"hdr.{payload}.sig"


def _oauth_tm(supabase_token):
    creds = StoredCredentials(
        type="oauth", access_token="a", refresh_token="r",
        supabase_token=supabase_token, supabase_url="https://x.supabase.co",
        supabase_anon_key="anon", expires_at=None, user_email="e@x",
    )
    return TokenManager("https://api", credentials_manager=SimpleNamespace(load=lambda: creds))


def test_supabase_refresh_true_inside_buffer():
    tm = _oauth_tm(_jwt_with_exp(int(time.time()) + 60))      # expires in 1 min
    assert tm.supabase_token_needs_refresh(buffer_minutes=10) is True


def test_supabase_refresh_false_outside_buffer():
    tm = _oauth_tm(_jwt_with_exp(int(time.time()) + 3600))    # 1 h out
    assert tm.supabase_token_needs_refresh(buffer_minutes=10) is False


def test_supabase_refresh_decode_failure_falls_back():
    # Malformed token -> exp None -> falls back to needs_refresh() (expires_at
    # None -> True) without raising.
    tm = _oauth_tm("not-a-jwt")
    assert tm.supabase_token_needs_refresh() is True


# --------------------------------------------------------------------------
# Auth-mode resolution
# --------------------------------------------------------------------------

class _LoginTM:
    """Fake of a logged-in user's TokenManager."""

    def __init__(self, *args, **kwargs):
        pass

    def is_oauth(self):
        return True

    def get_supabase_token(self, auto_refresh=True):
        return "login.jwt"

    @property
    def _cached_creds(self):
        return SimpleNamespace(
            supabase_url="https://login.supabase.co",
            supabase_anon_key="login-anon",
        )

    def get_user_email(self):
        return "e@x"


def test_env_service_role_skips_login(monkeypatch):
    _clear_env(monkeypatch)
    for var in _SUPABASE_ENV:
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("CAMPFIRE_SUPABASE_URL", "https://prod.supabase.co")
    monkeypatch.setenv("CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY", "svc-key")
    monkeypatch.setattr("campfire.auth.tokens.TokenManager", _LoginTM)

    sb = load_config()["supabase"]
    assert sb["_auth_mode"] == "service_role"
    assert sb["service_role_key"] == "svc-key"
    assert "supabase_token" not in sb
    assert "_token_manager" not in sb


def test_local_mode(monkeypatch):
    _clear_env(monkeypatch)
    sb = load_config(local=True)["supabase"]
    assert sb["_auth_mode"] == "local"
    assert sb["service_role_key"]
    assert "supabase_token" not in sb
    assert "_token_manager" not in sb


def test_login_overwrites_foreign_toml_url(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr("campfire.auth.tokens.TokenManager", _LoginTM)
    toml = tmp_path / "deploy.toml"
    toml.write_text('[supabase]\nurl = "https://foreign.supabase.co"\n')

    sb = load_config(str(toml))["supabase"]
    assert sb["_auth_mode"] == "login"
    assert sb["url"] == "https://login.supabase.co"   # foreign url overwritten
    assert sb["anon_key"] == "login-anon"
    assert sb["supabase_token"] == "login.jwt"
    assert sb["_token_manager"].__class__ is _LoginTM


def test_toml_service_role_skips_login(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr("campfire.auth.tokens.TokenManager", _LoginTM)
    toml = tmp_path / "deploy.toml"
    toml.write_text(
        '[supabase]\nurl = "https://prod.supabase.co"\n'
        'service_role_key = "svc-key"\n'
    )

    sb = load_config(str(toml))["supabase"]
    assert sb["_auth_mode"] == "service_role"
    assert "supabase_token" not in sb
    assert "_token_manager" not in sb


# --------------------------------------------------------------------------
# Admin gate through the write client
# --------------------------------------------------------------------------

class _FakeRPC:
    def __init__(self, data=None, exc=None):
        self._data, self._exc = data, exc

    def execute(self):
        if self._exc:
            raise self._exc
        return SimpleNamespace(data=self._data)


class _FakeClient:
    def __init__(self, data=None, exc=None):
        self._data, self._exc = data, exc
        self.rpc_called = False

    def rpc(self, name):
        assert name == "is_admin"
        self.rpc_called = True
        return _FakeRPC(self._data, self._exc)


def test_gate_skips_service_role_and_local(monkeypatch):
    def _boom(_cfg):
        raise AssertionError("service-role/local must not build a client to gate")
    monkeypatch.setattr("campfire.deploy.supabase.get_supabase_client", _boom)
    _gate_admin({"supabase": {"_auth_mode": "service_role"}})
    _gate_admin({"supabase": {"_auth_mode": "local"}})


def test_gate_passes_for_admin(monkeypatch):
    client = _FakeClient(data=True)
    monkeypatch.setattr(
        "campfire.deploy.supabase.get_supabase_client", lambda _cfg: client
    )
    _gate_admin({"supabase": {"_auth_mode": "login"}})
    assert client.rpc_called


def test_gate_blocks_non_admin(monkeypatch):
    client = _FakeClient(data=False)
    monkeypatch.setattr(
        "campfire.deploy.supabase.get_supabase_client", lambda _cfg: client
    )
    with pytest.raises(SystemExit):
        _gate_admin({"supabase": {
            "_auth_mode": "login",
            "_token_manager": SimpleNamespace(get_user_email=lambda: "e@x"),
        }})


def test_gate_handles_expired_jwt(monkeypatch):
    from postgrest.exceptions import APIError
    err = APIError({"message": "JWT expired", "code": "PGRST303",
                    "hint": None, "details": None})
    client = _FakeClient(exc=err)
    monkeypatch.setattr(
        "campfire.deploy.supabase.get_supabase_client", lambda _cfg: client
    )
    with pytest.raises(SystemExit):
        _gate_admin({"supabase": {"_auth_mode": "login"}})
