import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Keep signed-in pages restorable from the back/forward cache (#540).
 *
 * auth-js opens a `BroadcastChannel` per client to mirror auth events across
 * tabs, and posts on it for every event — including the `INITIAL_SESSION` /
 * `SIGNED_IN` pair each new document emits while booting. Chromium keeps a
 * document with an open channel in bfcache but evicts it the moment a message
 * arrives (`notRestoredReasons: broadcastchannel-message`), so navigating from
 * any signed-in page to any other signed-in page evicted the first one, and
 * the back button always reloaded (measured in the M0 trace, #496).
 *
 * Fix: close the channel on `pagehide` (the document is either parked, where
 * no message may reach it, or gone) and reopen it on a `persisted` `pageshow`,
 * wired the way the auth-js constructor wires it. Messages sent while the
 * page was parked are lost, so after reopening the session in cookie storage
 * is compared with the last one this document saw; if the user changed
 * (signed out or switched in another tab) the page reloads rather than run
 * on a stale identity.
 *
 * Relies on two `protected` members of GoTrueClient (`broadcastChannel`,
 * `_notifyAllSubscribers`); if either is missing the guard installs nothing.
 */

type AuthInternals = {
  broadcastChannel?: BroadcastChannel | null;
  storageKey?: string;
  _notifyAllSubscribers?: (event: string, session: unknown, broadcast?: boolean) => Promise<void>;
};

type ChannelLike = Pick<BroadcastChannel, 'close' | 'addEventListener' | 'postMessage'>;

export interface BfcacheGuardOptions {
  /** Where `pagehide` / `pageshow` are listened for. Default: `window`. */
  target?: EventTarget;
  /** Creates the replacement channel. Default: `new BroadcastChannel(name)`. */
  createChannel?: (name: string) => ChannelLike;
  /** Called when the session changed while the page was parked. Default: reload. */
  onSessionChanged?: () => void;
}

/** Returns an uninstall function, or a no-op if the guard could not install. */
export function installAuthChannelBfcacheGuard(
  client: SupabaseClient,
  options: BfcacheGuardOptions = {}
): () => void {
  const auth = client.auth as unknown as AuthInternals;
  const target = options.target ?? (typeof window !== 'undefined' ? window : undefined);
  const createChannel =
    options.createChannel ??
    ((name: string) => new BroadcastChannel(name) as unknown as ChannelLike);
  const onSessionChanged = options.onSessionChanged ?? (() => window.location.reload());

  if (
    !target ||
    !('broadcastChannel' in auth) ||
    typeof auth._notifyAllSubscribers !== 'function' ||
    !auth.storageKey
  ) {
    return () => {};
  }
  const storageKey = auth.storageKey;
  const notify = auth._notifyAllSubscribers.bind(auth);

  // The user this document last saw, for the resync after a restore.
  let lastUserId: string | null = null;
  const {
    data: { subscription },
  } = client.auth.onAuthStateChange((_event, session) => {
    lastUserId = session?.user?.id ?? null;
  });

  let paused = false;

  const onPageHide = () => {
    const channel = auth.broadcastChannel;
    if (!channel) return;
    try {
      channel.close();
    } catch {
      /* already closed */
    }
    auth.broadcastChannel = null;
    paused = true;
  };

  const onPageShow = (event: Event) => {
    if (!(event as PageTransitionEvent).persisted || !paused) return;
    paused = false;
    try {
      const channel = createChannel(storageKey);
      channel.addEventListener('message', (e) => {
        const data = (e as MessageEvent).data as { event: string; session: unknown } | undefined;
        if (!data) return;
        // broadcast = false: the message came from another tab, do not echo it
        void notify(data.event, data.session, false);
      });
      auth.broadcastChannel = channel as BroadcastChannel;
    } catch (e) {
      console.error('Failed to reopen the auth BroadcastChannel after a bfcache restore', e);
    }
    // Only the identity is compared here. A restore also flips the document
    // hidden → visible, and auth-js's own `visibilitychange` handler then runs
    // `_recoverAndRefresh()`, which re-reads the session from cookie storage
    // and emits it (`SIGNED_IN` / `TOKEN_REFRESHED`) to this tab's
    // subscribers — so a same-user token refresh or `USER_UPDATED` that
    // happened while parked reaches `AuthContext` without our help. What that
    // handler does not do is emit anything when the session is gone or belongs
    // to someone else; that is the case handled by the reload.
    void client.auth.getSession().then(({ data }) => {
      const nowUserId = data.session?.user?.id ?? null;
      if (nowUserId !== lastUserId) onSessionChanged();
    });
  };

  target.addEventListener('pagehide', onPageHide);
  target.addEventListener('pageshow', onPageShow);
  return () => {
    target.removeEventListener('pagehide', onPageHide);
    target.removeEventListener('pageshow', onPageShow);
    subscription.unsubscribe();
  };
}
