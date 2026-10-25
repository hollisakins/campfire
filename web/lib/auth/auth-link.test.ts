import { describe, expect, it, vi } from 'vitest';
import {
  parseAuthLink,
  settleAuthLink,
  type LinkAuthClient,
  type LinkOutcome,
  type LinkSession,
  type LinkSessionResult,
} from './auth-link';

const PAGE = 'https://campfire-jwst.org/reset-password';
const session = (email = 'a@b.c'): LinkSession => ({ user: { id: 'u1', email } });
const ok = (): LinkSessionResult => ({ data: { session: session() }, error: null });

/** A fake auth client whose events the test fires by hand, mimicking auth-js's ordering. */
function fakeAuth() {
  const listeners = new Set<(event: string, s: LinkSession | null) => void>();
  const unsubscribe = vi.fn();
  const exchangeCodeForSession = vi.fn<(code: string) => Promise<LinkSessionResult>>(async () => ok());
  const setSession = vi.fn<LinkAuthClient['setSession']>(async () => ok());
  const auth: LinkAuthClient = {
    onAuthStateChange: (cb) => {
      listeners.add(cb);
      return {
        data: {
          subscription: {
            unsubscribe: () => {
              unsubscribe();
              listeners.delete(cb);
            },
          },
        },
      };
    },
    exchangeCodeForSession,
    setSession,
  };
  const emit = (event: string, s: LinkSession | null) => listeners.forEach((cb) => cb(event, s));
  return { auth, emit, unsubscribe, exchangeCodeForSession, setSession, listeners };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

function harness(href: string, auth: LinkAuthClient, expectType?: string) {
  let current = href;
  const outcomes: LinkOutcome[] = [];
  const cleanup = settleAuthLink(auth, parseAuthLink(href), {
    expectType,
    readHref: () => current,
    onSettled: (o) => outcomes.push(o),
  });
  return { outcomes, cleanup, setHref: (h: string) => (current = h) };
}

describe('parseAuthLink', () => {
  it('classifies a PKCE landing', () => {
    expect(parseAuthLink(`${PAGE}?code=28af57f4`)).toEqual({ kind: 'code', code: '28af57f4' });
  });
  it('classifies implicit-flow tokens in the hash', () => {
    expect(parseAuthLink(`${PAGE}#access_token=at&refresh_token=rt&type=recovery`)).toEqual({
      kind: 'tokens',
      accessToken: 'at',
      refreshToken: 'rt',
      type: 'recovery',
    });
  });
  it('classifies GoTrue errors in the query and in the hash', () => {
    expect(parseAuthLink(`${PAGE}?error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid`)).toEqual({
      kind: 'error',
      code: 'otp_expired',
      description: 'Email link is invalid',
    });
    expect(parseAuthLink(`${PAGE}#error=access_denied&error_code=otp_expired`)).toMatchObject({ kind: 'error', code: 'otp_expired' });
  });
  it('is none for a bare page, or a hash missing the refresh token', () => {
    expect(parseAuthLink(PAGE)).toEqual({ kind: 'none' });
    expect(parseAuthLink(`${PAGE}#access_token=at`)).toEqual({ kind: 'none' });
  });
});

describe('settleAuthLink on a ?code= landing', () => {
  it('stays pending until the client exchanges the code, then is ready (form hydrated before the exchange)', async () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}?code=c1`, f.auth, 'recovery');
    expect(h.outcomes).toEqual([]);

    // auth-js: exchange succeeds → SIGNED_IN (code still in URL), then replaceState, then PASSWORD_RECOVERY
    f.emit('SIGNED_IN', session());
    expect(h.outcomes).toEqual([{ status: 'ready', session: session() }]);
    expect(f.unsubscribe).toHaveBeenCalledTimes(1);
    expect(f.exchangeCodeForSession).not.toHaveBeenCalled();

    h.setHref(PAGE);
    f.emit('PASSWORD_RECOVERY', session());
    f.emit('INITIAL_SESSION', session());
    expect(h.outcomes).toHaveLength(1);
  });

  it('is ready from INITIAL_SESSION when the exchange finished before the form subscribed (the #641 ordering)', async () => {
    const f = fakeAuth();
    // By the time the effect runs, auth-js has already stripped `code` and saved the session.
    const h = harness(PAGE, f.auth, 'recovery');
    f.emit('INITIAL_SESSION', session());
    expect(h.outcomes).toEqual([{ status: 'ready', session: session() }]);
    expect(f.exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it('exchanges the code itself when the client finished without touching it, and is ready on success', async () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}?code=c1`, f.auth, 'recovery');
    f.emit('INITIAL_SESSION', null); // no verifier in this browser: auth-js did not exchange
    expect(f.exchangeCodeForSession).toHaveBeenCalledWith('c1');
    expect(f.unsubscribe).toHaveBeenCalledTimes(1);
    await flush();
    expect(h.outcomes).toEqual([{ status: 'ready', session: session() }]);
  });

  it('is invalid only when the explicit exchange is rejected', async () => {
    const f = fakeAuth();
    f.exchangeCodeForSession.mockResolvedValueOnce({ data: { session: null }, error: { message: 'invalid flow state' } });
    const h = harness(`${PAGE}?code=c1`, f.auth, 'recovery');
    f.emit('INITIAL_SESSION', null);
    expect(h.outcomes).toEqual([]);
    await flush();
    expect(h.outcomes).toEqual([{ status: 'invalid', reason: 'rejected' }]);
  });

  it('ignores SIGNED_OUT and TOKEN_REFRESHED while pending', () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}?code=c1`, f.auth, 'recovery');
    f.emit('SIGNED_OUT', null);
    f.emit('TOKEN_REFRESHED', session());
    expect(h.outcomes).toEqual([]);
  });

  it('is missing when there was never anything to settle', () => {
    const f = fakeAuth();
    const h = harness(PAGE, f.auth, 'recovery');
    f.emit('INITIAL_SESSION', null);
    expect(h.outcomes).toEqual([{ status: 'invalid', reason: 'missing' }]);
  });

  it('cleanup unsubscribes and drops late results', async () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}?code=c1`, f.auth, 'recovery');
    h.cleanup();
    expect(f.unsubscribe).toHaveBeenCalledTimes(1);
    expect(f.listeners.size).toBe(0);
    h.cleanup();
    expect(f.unsubscribe).toHaveBeenCalledTimes(1);
    expect(h.outcomes).toEqual([]);
  });
});

describe('settleAuthLink on a GoTrue error landing', () => {
  it('reports a used/expired link without touching the client', () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}?error=access_denied&error_code=otp_expired`, f.auth, 'recovery');
    expect(h.outcomes).toEqual([{ status: 'invalid', reason: 'expired' }]);
    expect(f.listeners.size).toBe(0);
    expect(f.exchangeCodeForSession).not.toHaveBeenCalled();
  });
  it('reports access_denied without a code the same way, other errors as rejected', () => {
    expect(harness(`${PAGE}?error=access_denied`, fakeAuth().auth).outcomes).toEqual([{ status: 'invalid', reason: 'expired' }]);
    expect(harness(`${PAGE}?error=server_error&error_description=x`, fakeAuth().auth).outcomes).toEqual([
      { status: 'invalid', reason: 'rejected' },
    ]);
  });
});

describe('settleAuthLink on implicit-flow tokens', () => {
  it('sets the session from the hash and is ready', async () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}#access_token=at&refresh_token=rt&type=recovery`, f.auth, 'recovery');
    expect(f.setSession).toHaveBeenCalledWith({ access_token: 'at', refresh_token: 'rt' });
    await flush();
    expect(h.outcomes).toEqual([{ status: 'ready', session: session() }]);
  });
  it('refuses tokens of another type before setting anything', () => {
    const f = fakeAuth();
    const h = harness(`${PAGE}#access_token=at&refresh_token=rt&type=magiclink`, f.auth, 'recovery');
    expect(h.outcomes).toEqual([{ status: 'invalid', reason: 'wrong_type' }]);
    expect(f.setSession).not.toHaveBeenCalled();
  });
  it('is rejected when setSession fails', async () => {
    const f = fakeAuth();
    f.setSession.mockResolvedValueOnce({ data: { session: null }, error: { message: 'bad token' } });
    const h = harness(`${PAGE}#access_token=at&refresh_token=rt&type=recovery`, f.auth, 'recovery');
    await flush();
    expect(h.outcomes).toEqual([{ status: 'invalid', reason: 'rejected' }]);
  });
});
