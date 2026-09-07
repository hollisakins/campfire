import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
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

const sessionFor = (id: string | null) => (id ? { user: { id } } : null);

/** `storedUserId` is what cookie storage holds at restore time. */
function fakeClient(storedUserId: string | null, { channel = true } = {}) {
  const auth = {
    broadcastChannel: channel ? fakeChannel() : null,
    storageKey: 'sb-test-auth-token',
    _notifyAllSubscribers: vi.fn(async () => {}),
    getSession: vi.fn(async () => ({ data: { session: sessionFor(storedUserId) } })),
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

/** Install with a page whose app currently holds `appUserId`. */
function install(client: ReturnType<typeof fakeClient>, appUserId: string | null, extra: Partial<Parameters<typeof installAuthChannelBfcacheGuard>[1]> = {}) {
  const target = new EventTarget();
  const created: ReturnType<typeof fakeChannel>[] = [];
  const names: string[] = [];
  const onSessionChanged = vi.fn();
  const uninstall = installAuthChannelBfcacheGuard(asClient(client), {
    getUserId: () => appUserId,
    target,
    createChannel: (name) => {
      names.push(name);
      const c = fakeChannel();
      created.push(c);
      return c;
    },
    onSessionChanged,
    ...extra,
  });
  return { target, created, names, onSessionChanged, uninstall };
}

describe('installAuthChannelBfcacheGuard', () => {
  it('closes the channel on pagehide and reopens it on a persisted pageshow', async () => {
    const client = fakeClient('u1');
    const original = client.auth.broadcastChannel!;
    const { target, created, names, onSessionChanged } = install(client, 'u1');

    target.dispatchEvent(new Event('pagehide'));
    expect(original.closed).toBe(true);
    expect(client.auth.broadcastChannel).toBeNull();

    target.dispatchEvent(pageshow(true));
    expect(names).toEqual(['sb-test-auth-token']);
    expect(client.auth.broadcastChannel).toBe(created[0]);

    // the reopened channel forwards other tabs' messages without echoing them
    created[0].receive({ event: 'TOKEN_REFRESHED', session: sessionFor('u1') });
    expect(client.auth._notifyAllSubscribers).toHaveBeenCalledWith('TOKEN_REFRESHED', sessionFor('u1'), false);

    await flush();
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it('does nothing on a non-persisted pageshow, or on one without a preceding pagehide', () => {
    const client = fakeClient('u1');
    const { target, created } = install(client, 'u1');
    target.dispatchEvent(pageshow(false));
    target.dispatchEvent(pageshow(true));
    expect(created).toHaveLength(0);
    expect(client.auth.getSession).not.toHaveBeenCalled();
  });

  it('same user on restore: delivers USER_UPDATED so the app re-reads the profile, no reload', async () => {
    const client = fakeClient('u1');
    const { target, onSessionChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(client.auth._notifyAllSubscribers).toHaveBeenCalledWith('USER_UPDATED', sessionFor('u1'), false);
    expect(onSessionChanged).not.toHaveBeenCalled();
  });

  it('signed out while parked: delivers SIGNED_OUT to the app and reloads', async () => {
    const client = fakeClient(null);
    const { target, onSessionChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(client.auth._notifyAllSubscribers).toHaveBeenCalledWith('SIGNED_OUT', null, false);
    expect(onSessionChanged).toHaveBeenCalledTimes(1);
  });

  it('different user while parked: reloads even though the app has already moved to the new user', async () => {
    // auth-js's visibilitychange recovery emits SIGNED_IN(u2) before pageshow,
    // so a live "current user" would already read u2; the park-time snapshot
    // (u1) is what the comparison must use.
    const client = fakeClient('u2');
    let appUserId: string | null = 'u1';
    const { target, onSessionChanged } = install(client, 'u1', { getUserId: () => appUserId });
    target.dispatchEvent(new Event('pagehide'));
    appUserId = 'u2'; // the app switched during recovery, before pageshow
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(onSessionChanged).toHaveBeenCalledTimes(1);
    expect(client.auth._notifyAllSubscribers).not.toHaveBeenCalledWith('SIGNED_OUT', null, false);
  });

  it('still runs the identity check when there was no channel to close', async () => {
    const client = fakeClient(null, { channel: false });
    const { target, created, onSessionChanged } = install(client, 'u1');
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(created).toHaveLength(0); // never ours to reopen
    expect(onSessionChanged).toHaveBeenCalledTimes(1);
  });

  it('a failed reopen does not disable later restores', async () => {
    const client = fakeClient(null);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    let attempts = 0;
    const { target, onSessionChanged } = install(client, 'u1', {
      createChannel: () => {
        attempts += 1;
        throw new Error('no BroadcastChannel');
      },
    });
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(attempts).toBe(1);
    expect(client.auth.broadcastChannel).toBeNull();
    expect(onSessionChanged).toHaveBeenCalledTimes(1);

    // second park/restore: nothing to close, identity check still runs
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    await flush();
    expect(attempts).toBe(1);
    expect(onSessionChanged).toHaveBeenCalledTimes(2);
    errorSpy.mockRestore();
  });

  it('installs nothing when the auth internals are not there', () => {
    const client = { auth: {} };
    const target = new EventTarget();
    const spy = vi.spyOn(target, 'addEventListener');
    const uninstall = installAuthChannelBfcacheGuard(asClient(client), { getUserId: () => null, target });
    expect(spy).not.toHaveBeenCalled();
    uninstall();
  });

  it('uninstall removes the listeners', () => {
    const client = fakeClient('u1');
    const { target, uninstall } = install(client, 'u1');
    uninstall();
    target.dispatchEvent(new Event('pagehide'));
    expect(client.auth.broadcastChannel!.closed).toBe(false);
  });

  it('pins the auth-js version whose constructor wiring the reopen path copies', () => {
    // The reopened channel's message listener replicates GoTrueClient's
    // constructor (2.81.1). On a bump, re-read that constructor, update the
    // copy if it changed, then update this pin.
    const require = createRequire(import.meta.url);
    const pkg = JSON.parse(readFileSync(require.resolve('@supabase/auth-js/package.json'), 'utf8'));
    expect(pkg.version).toBe('2.81.1');
  });
});
