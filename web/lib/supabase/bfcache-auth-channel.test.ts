import { describe, expect, it, vi } from 'vitest';
import type { SupabaseClient } from '@supabase/supabase-js';
import { installAuthChannelBfcacheGuard } from './bfcache-auth-channel';

type Listener = (e: Event) => void;

function fakeChannel() {
  const listeners: Listener[] = [];
  return {
    closed: false,
    close() {
      this.closed = true;
    },
    addEventListener(_type: string, cb: Listener) {
      listeners.push(cb);
    },
    postMessage: vi.fn(),
    /** Simulate a message from another tab. */
    receive(data: unknown) {
      for (const cb of listeners) cb({ data } as unknown as Event);
    },
  };
}

function fakeClient(initialUserId: string | null) {
  let authCallback: ((event: string, session: unknown) => void) | null = null;
  const auth = {
    broadcastChannel: fakeChannel(),
    storageKey: 'sb-test-auth-token',
    _notifyAllSubscribers: vi.fn(async () => {}),
    onAuthStateChange: vi.fn((cb: (event: string, session: unknown) => void) => {
      authCallback = cb;
      // auth-js emits INITIAL_SESSION synchronously on subscribe
      cb('INITIAL_SESSION', initialUserId ? { user: { id: initialUserId } } : null);
      return { data: { subscription: { unsubscribe: vi.fn() } } };
    }),
    getSession: vi.fn(async () => ({ data: { session: initialUserId ? { user: { id: initialUserId } } : null } })),
    /** Simulate a later auth event in this tab. */
    emit(event: string, userId: string | null) {
      authCallback?.(event, userId ? { user: { id: userId } } : null);
    },
  };
  return { auth };
}

const asClient = (c: unknown) => c as SupabaseClient;

function pageshow(persisted: boolean): Event {
  return Object.assign(new Event('pageshow'), { persisted });
}

async function flush() {
  await new Promise((r) => setTimeout(r, 0));
}

describe('installAuthChannelBfcacheGuard', () => {
  it('closes the channel on pagehide and reopens it on a persisted pageshow', async () => {
    const client = fakeClient('u1');
    const original = client.auth.broadcastChannel;
    const target = new EventTarget();
    const created: ReturnType<typeof fakeChannel>[] = [];
    const onSessionChanged = vi.fn();
    installAuthChannelBfcacheGuard(asClient(client), {
      target,
      createChannel: (name) => {
        expect(name).toBe('sb-test-auth-token');
        const c = fakeChannel();
        created.push(c);
        return c;
      },
      onSessionChanged,
    });

    target.dispatchEvent(new Event('pagehide'));
    expect(original.closed).toBe(true);
    expect(client.auth.broadcastChannel).toBeNull();

    target.dispatchEvent(pageshow(true));
    expect(created).toHaveLength(1);
    expect(client.auth.broadcastChannel).toBe(created[0]);

    // the reopened channel forwards other tabs' messages without echoing them
    created[0].receive({ event: 'SIGNED_OUT', session: null });
    expect(client.auth._notifyAllSubscribers).toHaveBeenCalledWith('SIGNED_OUT', null, false);

    await flush();
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it('does nothing on a non-persisted pageshow (a normal load)', () => {
    const client = fakeClient('u1');
    const target = new EventTarget();
    const createChannel = vi.fn();
    installAuthChannelBfcacheGuard(asClient(client), { target, createChannel });
    target.dispatchEvent(pageshow(false));
    expect(createChannel).not.toHaveBeenCalled();
    // and a pageshow without a preceding pagehide is ignored too
    target.dispatchEvent(pageshow(true));
    expect(createChannel).not.toHaveBeenCalled();
  });

  it('reloads when the session changed while the page was parked', async () => {
    const client = fakeClient('u1');
    const target = new EventTarget();
    const onSessionChanged = vi.fn();
    installAuthChannelBfcacheGuard(asClient(client), { target, createChannel: () => fakeChannel(), onSessionChanged });

    target.dispatchEvent(new Event('pagehide'));
    // signed out in another tab while parked: cookie storage now has no session
    client.auth.getSession.mockResolvedValueOnce({ data: { session: null } });
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onSessionChanged).toHaveBeenCalledTimes(1);
  });

  it('tracks the latest user seen in this tab before comparing', async () => {
    const client = fakeClient(null);
    const target = new EventTarget();
    const onSessionChanged = vi.fn();
    installAuthChannelBfcacheGuard(asClient(client), { target, createChannel: () => fakeChannel(), onSessionChanged });
    client.auth.emit('SIGNED_IN', 'u2');
    client.auth.getSession.mockResolvedValueOnce({ data: { session: { user: { id: 'u2' } } } });

    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it('installs nothing when the auth internals are not there', () => {
    const client = { auth: { onAuthStateChange: vi.fn() } };
    const target = new EventTarget();
    const spy = vi.spyOn(target, 'addEventListener');
    const uninstall = installAuthChannelBfcacheGuard(asClient(client), { target });
    expect(spy).not.toHaveBeenCalled();
    uninstall();
  });

  it('uninstall removes the listeners', () => {
    const client = fakeClient('u1');
    const target = new EventTarget();
    const uninstall = installAuthChannelBfcacheGuard(asClient(client), { target, createChannel: () => fakeChannel() });
    uninstall();
    target.dispatchEvent(new Event('pagehide'));
    expect(client.auth.broadcastChannel.closed).toBe(false);
  });
});
