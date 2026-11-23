import { isAuthRetryableFetchError, type Session, type SupabaseClient } from '@supabase/supabase-js';
import { parkBroadcastChannel } from '@/lib/park-broadcast-channel';

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
 * posts. The receiver has to stop listening while parked. Two alternatives
 * were weighed and not taken: closing the channel for good (or hiding the
 * `BroadcastChannel` global while the client is constructed) would drop the
 * live delivery of a sign-out to the other open tabs, which works today.
 *
 * So the channel is parked with `parkBroadcastChannel`
 * (`lib/park-broadcast-channel.ts`): closed on `pagehide`, reopened on a
 * `persisted` `pageshow`, with the reopened channel's posts re-dispatched on
 * the closed original, which still carries the listener the auth-js
 * constructor attached, so nothing of the library's wiring is copied. The
 * app's current user id is snapshotted at park time.
 * Then the session in cookie storage is compared with the snapshot and the
 * caller is told when the identity changed while parked (sign-out, or a
 * different user in another tab). Same user: nothing to do — auth-js's own
 * `visibilitychange` recovery has already re-emitted the stored session to
 * this tab, and the profile row was never cross-tab-synced anyway.
 *
 * The snapshot is taken at park time on purpose: that recovery runs before
 * `pageshow` and would already have moved a live "current user" to the new
 * identity. A snapshot of `undefined` means the app had not finished its own
 * boot when the page parked; the comparison is skipped, since the pending
 * boot resumes on restore and lands on the right user by itself. (In Chromium
 * and Firefox a document is not restorable while that boot holds its Web
 * Lock, so this window is narrow.)
 *
 * The only library internal touched is the `protected` `broadcastChannel`
 * member of GoTrueClient; if it is absent the guard installs nothing and
 * warns outside production. `bfcache-auth-channel.real.test.ts` exercises a
 * real `GoTrueClient` so a rename fails a test rather than silently bringing
 * the eviction back.
 */

type AuthInternals = { broadcastChannel?: BroadcastChannel | null };

type ChannelLike = Pick<BroadcastChannel, 'name' | 'close' | 'addEventListener' | 'dispatchEvent'>;

export interface BfcacheGuardOptions {
  /**
   * The user id the app currently holds (`null` = signed out), or `undefined`
   * while the app has not finished its initial session load.
   */
  getUserId: () => string | null | undefined;
  /**
   * The stored session no longer matches the user this page was showing when
   * it parked. `session` is what cookie storage holds now (`null` = signed out).
   */
  onIdentityChanged: (session: Session | null) => void;
  /** Where `pagehide` / `pageshow` are listened for. Default: `window`. */
  target?: EventTarget;
  /** Creates the replacement channel. Default: `new BroadcastChannel(name)`. */
  createChannel?: (name: string) => ChannelLike;
}

/** Returns an uninstall function, or a no-op if the guard could not install. */
export function installAuthChannelBfcacheGuard(
  client: SupabaseClient,
  options: BfcacheGuardOptions
): () => void {
  const auth = client.auth as unknown as AuthInternals;
  const target = options.target ?? (typeof window !== 'undefined' ? window : undefined);
  const createChannel =
    options.createChannel ?? ((name: string) => new BroadcastChannel(name) as unknown as ChannelLike);

  if (!target) return () => {};
  if (!('broadcastChannel' in auth)) {
    if (process.env.NODE_ENV !== 'production') {
      console.warn(
        'bfcache guard not installed: GoTrueClient has no `broadcastChannel` member (auth-js changed?). Signed-in pages will be evicted from bfcache again (#540).'
      );
    }
    return () => {};
  }

  let parkedUserId: string | null | undefined;
  let generation = 0;

  const onRestore = () => {
    const before = parkedUserId;
    if (before === undefined) return; // parked mid-boot: the boot's own load settles it
    const gen = ++generation;
    client.auth.getSession().then(
      ({ data, error }) => {
        if (gen !== generation) return; // superseded by a later restore
        // A retryable refresh failure (offline, 5xx) answers `session: null`
        // with an error and keeps the cookie session: not a sign-out. Any
        // other error has already removed the session, which is one.
        if (error && isAuthRetryableFetchError(error)) return;
        const now = data.session?.user?.id ?? null;
        if (now !== before) options.onIdentityChanged(data.session);
      },
      (e) => {
        console.error('Could not re-read the session after a bfcache restore', e);
      }
    );
  };

  return parkBroadcastChannel<ChannelLike>({
    get: () => auth.broadcastChannel as ChannelLike | null | undefined,
    set: (channel) => {
      auth.broadcastChannel = channel as unknown as BroadcastChannel | null;
    },
    // Hand each post to the listener auth-js attached to its own channel.
    wire: (fresh, original) => {
      fresh.addEventListener('message', (e) => {
        original.dispatchEvent(new MessageEvent('message', { data: (e as MessageEvent).data }));
      });
    },
    onPark: () => {
      parkedUserId = options.getUserId();
    },
    onRestore,
    label: 'auth',
    target,
    createChannel,
  });
}
