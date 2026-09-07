/**
 * The guard against a real GoTrueClient (the one @supabase/ssr wraps), so the
 * two assumptions it rests on are checked against the installed auth-js
 * rather than a fake: the client keeps its cross-tab channel on a member
 * named `broadcastChannel`, and a closed `BroadcastChannel` still dispatches
 * to the listener the constructor attached.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { GoTrueClient } from '@supabase/auth-js';
import type { SupabaseClient } from '@supabase/supabase-js';
import { installAuthChannelBfcacheGuard } from './bfcache-auth-channel';

const STORAGE_KEY = 'sb-bfcache-real-test';

function pageshow(persisted: boolean): Event {
  return Object.assign(new Event('pageshow'), { persisted });
}

const tick = () => new Promise((r) => setTimeout(r, 20));

describe('installAuthChannelBfcacheGuard with a real GoTrueClient', () => {
  const opened: BroadcastChannel[] = [];
  let client: GoTrueClient;

  beforeAll(() => {
    // auth-js only opens the channel when it believes it runs in a browser.
    vi.stubGlobal('window', globalThis);
    vi.stubGlobal('document', { visibilityState: 'visible' });
    client = new GoTrueClient({
      url: 'http://localhost:9999',
      storageKey: STORAGE_KEY,
      persistSession: true,
      autoRefreshToken: false,
      detectSessionInUrl: false,
    });
  });

  afterAll(() => {
    for (const c of opened) c.close();
    const own = (client as unknown as { broadcastChannel?: BroadcastChannel | null }).broadcastChannel;
    own?.close();
    vi.unstubAllGlobals();
  });

  function otherTab(): BroadcastChannel {
    const c = new BroadcastChannel(STORAGE_KEY);
    opened.push(c);
    return c;
  }

  it('closes the channel while parked and reopens it on restore, with cross-tab posts still reaching the subscriber', async () => {
    const internals = client as unknown as { broadcastChannel?: BroadcastChannel | null };
    expect(internals.broadcastChannel).toBeInstanceOf(BroadcastChannel);
    const original = internals.broadcastChannel!;

    const events: string[] = [];
    client.onAuthStateChange((event) => {
      events.push(event);
    });
    await tick();
    events.length = 0; // drop INITIAL_SESSION

    const other = otherTab();

    // Baseline: the library delivers another tab's post as an auth event.
    other.postMessage({ event: 'SIGNED_OUT', session: null });
    await tick();
    expect(events).toEqual(['SIGNED_OUT']);

    const target = new EventTarget();
    const onIdentityChanged = vi.fn();
    installAuthChannelBfcacheGuard({ auth: client } as unknown as SupabaseClient, {
      getUserId: () => null,
      onIdentityChanged,
      target,
    });

    // Parked: nothing arrives, so nothing can evict the document.
    target.dispatchEvent(new Event('pagehide'));
    expect(internals.broadcastChannel).toBeNull();
    other.postMessage({ event: 'SIGNED_OUT', session: null });
    await tick();
    expect(events).toEqual(['SIGNED_OUT']);

    // Restored: a replacement channel forwards through the closed original.
    target.dispatchEvent(pageshow(true));
    expect(internals.broadcastChannel).toBeInstanceOf(BroadcastChannel);
    expect(internals.broadcastChannel).not.toBe(original);
    other.postMessage({ event: 'SIGNED_OUT', session: null });
    await tick();
    expect(events).toEqual(['SIGNED_OUT', 'SIGNED_OUT']);

    // Second cycle: still one hop, delivered exactly once.
    target.dispatchEvent(new Event('pagehide'));
    target.dispatchEvent(pageshow(true));
    other.postMessage({ event: 'SIGNED_OUT', session: null });
    await tick();
    expect(events).toEqual(['SIGNED_OUT', 'SIGNED_OUT', 'SIGNED_OUT']);

    await tick();
    expect(onIdentityChanged).not.toHaveBeenCalled(); // null before, null now
  });
});
