import { describe, expect, it, vi } from 'vitest';
import { AuthApiError, AuthRetryableFetchError, type SupabaseClient } from '@supabase/supabase-js';
import { installAuthChannelBfcacheGuard } from './bfcache-auth-channel';

/** A BroadcastChannel stand-in: a real EventTarget (so `dispatchEvent` reaches listeners) plus `close()`. */
function fakeChannel(name = 'sb-test-auth-token') {
  const target = new EventTarget();
  const channel = {
    name,
    closed: false,
    close() {
      channel.closed = true;
    },
    addEventListener: target.addEventListener.bind(target),
    dispatchEvent: target.dispatchEvent.bind(target),
    /** Simulate a post from another tab arriving on this channel. */
    receive(data: unknown) {
      target.dispatchEvent(new MessageEvent('message', { data }));
    },
  };
  return channel;
}

const sessionFor = (id: string | null) => (id ? { user: { id } } : null);

/** `storedUserId` is what cookie storage holds at restore time. */
function fakeClient(storedUserId: string | null, { channel = true } = {}) {
  const original = channel ? fakeChannel() : null;
  // What the auth-js constructor attached: turns a post into auth events.
  const delivered = vi.fn();
  original?.addEventListener('message', (e) => delivered((e as MessageEvent).data));
  const auth = {
    broadcastChannel: original,
    getSession: vi.fn(async () => ({ data: { session: sessionFor(storedUserId) }, error: null })),
  };
  return { auth, original, delivered };
}

const asClient = (c: unknown) => c as SupabaseClient;

function pageshow(persisted: boolean): Event {
  return Object.assign(new Event('pageshow'), { persisted });
}

async function flush() {
  await new Promise((r) => setTimeout(r, 0));
}

/** Install with a page whose app currently holds `appUserId`. */
function install(
  client: ReturnType<typeof fakeClient>,
  appUserId: string | null | undefined,
  extra: Partial<Parameters<typeof installAuthChannelBfcacheGuard>[1]> = {}
) {
  const target = new EventTarget();
  const created: ReturnType<typeof fakeChannel>[] = [];
  const onIdentityChanged = vi.fn();
  const uninstall = installAuthChannelBfcacheGuard(asClient(client), {
    getUserId: () => appUserId,
    onIdentityChanged,
    target,
    createChannel: (name) => {
      const c = fakeChannel(name);
      created.push(c);
      return c;
    },
    ...extra,
  });
  return { target, created, onIdentityChanged, uninstall };
}

describe('installAuthChannelBfcacheGuard', () => {
  it('closes the channel on pagehide and reopens one with the same name on a persisted pageshow', async () => {
    const client = fakeClient('u1');
    const { target, created, onIdentityChanged } = install(client, 'u1');

    target.dispatchEvent(new Event('pagehide'));
    expect(client.original!.closed).toBe(true);
    expect(client.auth.broadcastChannel).toBeNull();

    target.dispatchEvent(pageshow(true));
    expect(created).toHaveLength(1);
    expect(created[0].name).toBe('sb-test-auth-token');
    expect(client.auth.broadcastChannel).toBe(created[0]);

    await flush();
    expect(onIdentityChanged).not.toHaveBeenCalled();
  });

  it("forwards posts on the reopened channel to the original's listener (auth-js's wiring), without copying it", () => {
    const client = fakeClient('u1');
    const { target, created } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));

    const msg = { event: 'SIGNED_OUT', session: null };
    created[0].receive(msg);
    expect(client.delivered).toHaveBeenCalledTimes(1);
    expect(client.delivered).toHaveBeenCalledWith(msg);
  });

  it('does nothing on a non-persisted pageshow, or on one without a preceding pagehide', () => {
    const client = fakeClient('u1');
    const { target, created } = install(client, 'u1');
    target.dispatchEvent(pageshow(false));
    target.dispatchEvent(pageshow(true));
    expect(created).toHaveLength(0);
    expect(client.auth.getSession).not.toHaveBeenCalled();
  });

  it('same user on restore: no callback, no synthetic events', async () => {
    const client = fakeClient('u1');
    const { target, onIdentityChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(client.auth.getSession).toHaveBeenCalledTimes(1);
    expect(onIdentityChanged).not.toHaveBeenCalled();
  });

  it('signed out while parked: reports a null session', async () => {
    const client = fakeClient(null);
    const { target, onIdentityChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onIdentityChanged).toHaveBeenCalledWith(null);
  });

  it('different user while parked: reports the new session, compared against the park-time snapshot', async () => {
    // auth-js's visibilitychange recovery emits SIGNED_IN(u2) before pageshow,
    // so a live "current user" already reads u2; the snapshot must win.
    const client = fakeClient('u2');
    let appUserId: string | null = 'u1';
    const { target, onIdentityChanged } = install(client, 'u1', { getUserId: () => appUserId });
    target.dispatchEvent(new Event('pagehide'));
    appUserId = 'u2';
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onIdentityChanged).toHaveBeenCalledWith(sessionFor('u2'));
  });

  it('parked before the app finished its own boot: skips the comparison', async () => {
    const client = fakeClient('u1');
    const { target, onIdentityChanged } = install(client, undefined);
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(client.auth.getSession).not.toHaveBeenCalled();
    expect(onIdentityChanged).not.toHaveBeenCalled();
  });

  it('a retryable refresh failure on restore is not a sign-out', async () => {
    const client = fakeClient('u1');
    client.auth.getSession.mockResolvedValueOnce({
      data: { session: null },
      error: new AuthRetryableFetchError('fetch failed', 0),
    } as never);
    const { target, onIdentityChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onIdentityChanged).not.toHaveBeenCalled();
  });

  it('a non-retryable refresh failure (session removed by the library) is a sign-out', async () => {
    const client = fakeClient('u1');
    client.auth.getSession.mockResolvedValueOnce({
      data: { session: null },
      error: new AuthApiError('refresh token revoked', 400, 'refresh_token_not_found'),
    } as never);
    const { target, onIdentityChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onIdentityChanged).toHaveBeenCalledWith(null);
  });

  it('forwarding stays one hop across park/restore cycles (each replacement is dropped, not chained)', () => {
    const client = fakeClient('u1');
    const { target, created } = install(client, 'u1');
    for (let i = 0; i < 3; i++) {
      target.dispatchEvent(new Event('pagehide'));
      target.dispatchEvent(pageshow(true));
    }
    expect(created).toHaveLength(3);
    expect(created[0].closed).toBe(true);
    expect(created[1].closed).toBe(true);
    expect(created[2].closed).toBe(false);
    const spyOnFirstProxy = vi.fn();
    created[0].addEventListener('message', spyOnFirstProxy);
    created[2].receive({ event: 'SIGNED_OUT', session: null });
    expect(client.delivered).toHaveBeenCalledTimes(1);
    expect(spyOnFirstProxy).not.toHaveBeenCalled();
  });

  it("a real closed BroadcastChannel still dispatches to its listeners (the property the reopen relies on)", () => {
    const real = new BroadcastChannel('sb-closed-dispatch-check');
    const seen = vi.fn();
    real.addEventListener('message', (e) => seen((e as MessageEvent).data));
    real.close();
    real.dispatchEvent(new MessageEvent('message', { data: { event: 'x' } }));
    expect(seen).toHaveBeenCalledWith({ event: 'x' });
  });

  it('a rejected session read is logged, not treated as a change', async () => {
    const client = fakeClient('u1');
    client.auth.getSession.mockRejectedValueOnce(new Error('corrupt cookie chunk'));
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { target, onIdentityChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onIdentityChanged).not.toHaveBeenCalled();
    expect(errorSpy).toHaveBeenCalled();
    errorSpy.mockRestore();
  });

  it('a superseded session read (fast back → forward → back) does not act', async () => {
    let resolveFirst!: (v: unknown) => void;
    const client = fakeClient(null);
    client.auth.getSession.mockImplementationOnce(() => new Promise((r) => (resolveFirst = r)) as never);
    const { target, onIdentityChanged } = install(client, 'u1');

    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true)); // first restore, read still pending
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true)); // second restore
    resolveFirst({ data: { session: null }, error: null });
    await flush();
    // only the second restore's read may act
    expect(onIdentityChanged).toHaveBeenCalledTimes(1);
  });

  it('still runs the identity check when there was no channel to close', async () => {
    const client = fakeClient(null, { channel: false });
    const { target, created, onIdentityChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(created).toHaveLength(0);
    expect(onIdentityChanged).toHaveBeenCalledWith(null);
  });

  it('a failed reopen is retried on the next restore and never disables the identity check', async () => {
    const client = fakeClient(null);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    let attempts = 0;
    const { target, onIdentityChanged } = install(client, 'u1', {
      createChannel: (name) => {
        attempts += 1;
        if (attempts === 1) throw new Error('no BroadcastChannel');
        return fakeChannel(name);
      },
    });
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(client.auth.broadcastChannel).toBeNull();
    expect(onIdentityChanged).toHaveBeenCalledTimes(1);

    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(attempts).toBe(2);
    expect(client.auth.broadcastChannel).not.toBeNull();
    expect(onIdentityChanged).toHaveBeenCalledTimes(2);
    errorSpy.mockRestore();
  });

  it('installs nothing, with a warning, when the auth internals are not there', () => {
    const client = { auth: {} };
    const target = new EventTarget();
    const spy = vi.spyOn(target, 'addEventListener');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const uninstall = installAuthChannelBfcacheGuard(asClient(client), {
      getUserId: () => null,
      onIdentityChanged: () => {},
      target,
    });
    expect(spy).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
    uninstall();
  });

  it('uninstall removes the listeners', () => {
    const client = fakeClient('u1');
    const { target, uninstall } = install(client, 'u1');
    uninstall();
    target.dispatchEvent(new Event('pagehide'));
    expect(client.original!.closed).toBe(false);
  });
});
