import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Keep signed-in pages restorable from the back/forward cache (#540).
 *
 * auth-js opens a `BroadcastChannel` per client to mirror auth events across
 * tabs and posts on it from `_notifyAllSubscribers(event, session)` with
 * `broadcast = true`. The senders that matter: `_recoverAndRefresh()` emits
 * `SIGNED_IN` every time a document boots *and* every time any tab goes
 * hidden → visible, and the auto-refresh ticker emits `TOKEN_REFRESHED`.
 * (`INITIAL_SESSION` is delivered to the subscriber directly and never posted.)
 * Chromium keeps a document with an open channel in bfcache but evicts it the
 * moment a message arrives (`notRestoredReasons: broadcastchannel-message`),
 * so the next page's boot — or a refocus of any other tab — evicted every
 * parked signed-in page and the back button always reloaded (M0 trace, #496).
 * Filtering on the sender side would not help: any other tab's refocus still
 * posts. The receiver has to stop listening while parked.
 *
 * So: on `pagehide` the channel is closed (the document is either parked,
 * where no message may reach it, or gone) and the current user id is
 * snapshotted; on a `persisted` `pageshow` the channel is reopened, wired as
 * the auth-js constructor wires it, and the session in cookie storage is
 * compared with the snapshot:
 * - session gone → `SIGNED_OUT` is delivered to this tab's subscribers (so
 *   the app clears its auth state even if the reload below is vetoed by a
 *   `beforeunload` guard) and the page reloads;
 * - a different user → the page reloads (the server-rendered, access-scoped
 *   content on screen belongs to the previous user);
 * - the same user → `USER_UPDATED` is delivered so the app re-reads the
 *   profile and program access, since any change that happened while parked
 *   was never received. auth-js's own `visibilitychange` recovery re-emits
 *   the stored session on restore, but only ever as `SIGNED_IN`, which the
 *   app dedups for a known user.
 * The snapshot is taken at park time on purpose: auth-js's recovery runs
 * before `pageshow` and would otherwise have already moved a live "current
 * user" to the new identity.
 *
 * Relies on two `protected` members of GoTrueClient (`broadcastChannel`,
 * `_notifyAllSubscribers`) and `storageKey`; if any is missing the guard
 * installs nothing. The reopen wiring copies the auth-js 2.81.1 constructor;
 * `bfcache-auth-channel.test.ts` pins that version so a bump fails loudly
 * and the copy gets re-checked.
 */

type AuthInternals = {
  broadcastChannel?: BroadcastChannel | null;
  storageKey?: string;
  _notifyAllSubscribers?: (event: string, session: unknown, broadcast?: boolean) => Promise<void>;
};

type ChannelLike = Pick<BroadcastChannel, 'close' | 'addEventListener' | 'postMessage'>;

export interface BfcacheGuardOptions {
  /** The user id the app currently holds; snapshotted when the page parks. */
  getUserId: () => string | null;
  /** Where `pagehide` / `pageshow` are listened for. Default: `window`. */
  target?: EventTarget;
  /** Creates the replacement channel. Default: `new BroadcastChannel(name)`. */
  createChannel?: (name: string) => ChannelLike;
  /** Called when the identity changed while the page was parked. Default: reload. */
  onSessionChanged?: () => void;
}

/** Returns an uninstall function, or a no-op if the guard could not install. */
export function installAuthChannelBfcacheGuard(
  client: SupabaseClient,
  options: BfcacheGuardOptions
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

  let parked = false;
  let parkedUserId: string | null = null;
  let channelClosedByUs = false;

  const onPageHide = () => {
    parked = true;
    parkedUserId = options.getUserId();
    const channel = auth.broadcastChannel;
    if (!channel) return;
    try {
      channel.close();
    } catch {
      /* already closed */
    }
    auth.broadcastChannel = null;
    channelClosedByUs = true;
  };

  const reopenChannel = () => {
    channelClosedByUs = false;
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
  };

  const onPageShow = (event: Event) => {
    if (!(event as PageTransitionEvent).persisted || !parked) return;
    parked = false;
    if (channelClosedByUs) reopenChannel();
    const before = parkedUserId;
    void client.auth.getSession().then(({ data }) => {
      const session = data.session;
      const now = session?.user?.id ?? null;
      if (now === before) {
        if (session) void notify('USER_UPDATED', session, false);
        return;
      }
      if (!session) void notify('SIGNED_OUT', null, false);
      onSessionChanged();
    });
  };

  target.addEventListener('pagehide', onPageHide);
  target.addEventListener('pageshow', onPageShow);
  return () => {
    target.removeEventListener('pagehide', onPageHide);
    target.removeEventListener('pageshow', onPageShow);
  };
}
