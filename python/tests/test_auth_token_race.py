"""Concurrent refresh against a rotating refresh token (issue #474).

The server rotates the refresh token on every successful refresh, so a thread
that presents a superseded token gets 400 ``invalid_grant`` back. Deploy runs
16 concurrent upload workers off one TokenManager, so before the fix any long
transfer that crossed the refresh threshold had most of its workers die with a
spurious "Session expired. Please run 'campfire login' again."

The fake server sleeps *before* validating. That detail is what gives these
tests teeth: without it the refresh completes so fast that no two threads ever
hold the same cached token, and the suite passes against the broken code.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from campfire.auth.tokens import TokenManager
from campfire.exceptions import AuthenticationError

SERVER_LATENCY_S = 0.05
N_WORKERS = 16


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:  # pragma: no cover - guard only
            raise AssertionError("raise_for_status called on an error response")


class RotatingServer:
    """Accepts only the current refresh token; rotates it on every success."""

    def __init__(self, latency=SERVER_LATENCY_S):
        self.current = "refresh-0"
        self.n = 0
        self.rejections = 0
        self.latency = latency
        self._lock = threading.Lock()

    def post(self, url, json=None, **kwargs):
        presented = json["refresh_token"]
        # Latency BEFORE validation: this is what lets several threads read the
        # same cached token and get their requests in flight concurrently.
        time.sleep(self.latency)
        with self._lock:
            if presented != self.current:
                self.rejections += 1
                return FakeResponse(400, {"error": "invalid_grant"})
            self.n += 1
            self.current = f"refresh-{self.n}"
            return FakeResponse(
                200,
                {
                    "access_token": f"access-{self.n}",
                    "refresh_token": self.current,
                    "expires_in": 3600,
                    "supabase_token": f"sb-{self.n}",
                    "supabase_url": "https://example.supabase.co",
                    "supabase_anon_key": "anon",
                },
            )


class _Creds:
    def __init__(self, access, refresh, supabase, expires_at):
        self.access_token = access
        self.refresh_token = refresh
        self.supabase_token = supabase
        self.expires_at = expires_at
        self.api_key = None
        self.user_email = "test@example.com"

    def is_oauth(self):
        return True

    def is_api_key(self):
        return False


class FakeCredManager:
    """Stands in for on-disk credential storage."""

    def __init__(self):
        self.access_token = "access-0"
        self.refresh_token = "refresh-0"
        self.supabase_token = "sb-0"
        self.expires_at = _in(3600)
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            return _Creds(self.access_token, self.refresh_token,
                          self.supabase_token, self.expires_at)

    def update_oauth_tokens(self, access, refresh, expires_in,
                            supabase_token=None, supabase_url=None,
                            supabase_anon_key=None):
        with self._lock:
            self.access_token = access
            self.refresh_token = refresh
            self.expires_at = _in(expires_in)
            if supabase_token:
                self.supabase_token = supabase_token


def _in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def manager():
    server = RotatingServer()
    creds = FakeCredManager()
    tm = TokenManager("https://example.com/api/v1", credentials_manager=creds)
    tm.session = server
    return tm, server, creds


def test_concurrent_refresh_does_not_raise_session_expired(manager):
    """16 threads refreshing at once must all succeed (issue #474)."""
    tm, server, _ = manager
    errors, results = [], []
    barrier = threading.Barrier(N_WORKERS)

    def worker():
        barrier.wait()
        try:
            results.append(tm.refresh_tokens())
        except AuthenticationError as exc:  # pragma: no cover - failure path
            errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"{len(errors)} thread(s) failed: {errors[0]}"
    assert len(results) == N_WORKERS
    # Only the winner should have gone to the network; the rest reuse its token.
    assert server.n == 1, f"expected 1 refresh round-trip, got {server.n}"
    assert server.rejections == 0


def test_concurrent_refresh_returns_the_current_token(manager):
    """Every caller ends up holding the token that is actually current."""
    tm, _, creds = manager
    results = []
    barrier = threading.Barrier(N_WORKERS)

    def worker():
        barrier.wait()
        results.append(tm.refresh_tokens())

    threads = [threading.Thread(target=worker) for _ in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(access == creds.access_token for access, _, _ in results)
    assert all(refresh == creds.refresh_token for _, refresh, _ in results)


def test_refresh_recovers_when_another_process_rotated_the_token(manager):
    """A stale in-memory token is retried once against what is on disk."""
    tm, server, creds = manager
    # Another process refreshed: disk and server moved on, our cache did not.
    server.current = "refresh-1"
    creds.refresh_token = "refresh-1"
    tm._cached_creds.refresh_token = "refresh-0"

    access, refresh, _ = tm.refresh_tokens()

    assert access == "access-1"
    assert refresh == "refresh-1"
    assert server.rejections == 1, "expected exactly one rejected attempt"


def test_refresh_still_reports_a_genuinely_dead_session(manager):
    """A refresh token that is dead everywhere must still surface as expired."""
    tm, server, creds = manager
    server.current = "refresh-nobody-has"

    with pytest.raises(AuthenticationError, match="Session expired"):
        tm.refresh_tokens()
