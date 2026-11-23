/**
 * Keep a page that holds an open `BroadcastChannel` restorable from the
 * back/forward cache.
 *
 * Chromium keeps a document with an open channel in bfcache but evicts it the
 * moment a message arrives (`notRestoredReasons: broadcastchannel-message`).
 * Any channel that another tab posts on while this page is parked therefore
 * turns Back into a full reload (#540 for the auth client, #544 for the NIRCam
 * PNG store). Filtering on the sender side cannot help, because the sender is
 * a different tab; the receiver has to stop listening while parked.
 *
 * So the channel is closed on `pagehide`, and on a `persisted` `pageshow` a
 * fresh one of the same name is opened and handed to `wire` to be hooked up
 * again. Posts sent while the page was parked are lost: an owner that cares
 * reconciles in `onRestore`.
 *
 * `wire` also receives the first channel that was parked (`original`). A
 * closed `BroadcastChannel` is still an `EventTarget` that keeps its
 * listeners, so a library-owned channel whose wiring we cannot reproduce can
 * be re-fed by dispatching onto the original (see `bfcache-auth-channel.ts`).
 * The original is captured once; each replacement is closed and dropped at the
 * next park, so that forwarding is always one hop.
 *
 * Every new long-lived channel in the app should go through this helper.
 */

/** The part of `BroadcastChannel` the helper itself touches. */
export type ParkableChannel = Pick<BroadcastChannel, 'name' | 'close'>;

export interface ParkBroadcastChannelOptions<C extends ParkableChannel> {
  /** The channel currently in use (`null` when there is none). */
  get: () => C | null | undefined;
  /** Store the channel to use from now on (`null` while parked). */
  set: (channel: C | null) => void;
  /** Hook up a freshly opened replacement. `original` is the first channel parked. */
  wire: (fresh: C, original: C) => void;
  /** Runs on every `pagehide`, after the channel is closed. */
  onPark?: () => void;
  /** Runs on a `persisted` `pageshow` that follows a park, after the reopen. */
  onRestore?: () => void;
  /** Names the channel in the error logged when a reopen fails. */
  label?: string;
  /** Where `pagehide` / `pageshow` are listened for. Default: `window`. */
  target?: EventTarget;
  /** Creates the replacement channel. Default: `new BroadcastChannel(name)`. */
  createChannel?: (name: string) => C;
}

/** Returns an uninstall function (a no-op outside the browser). */
export function parkBroadcastChannel<C extends ParkableChannel>(
  options: ParkBroadcastChannelOptions<C>
): () => void {
  const target = options.target ?? (typeof window !== 'undefined' ? window : undefined);
  if (!target) return () => {};
  const createChannel =
    options.createChannel ?? ((name: string) => new BroadcastChannel(name) as unknown as C);

  let parked = false;
  let original: C | null = null;

  const onPageHide = () => {
    parked = true;
    const channel = options.get();
    if (channel) {
      channel.close(); // idempotent per spec
      options.set(null);
      // First park: the owner's own channel. Later parks close one of our
      // replacements, which is simply dropped.
      if (!original) original = channel;
    }
    options.onPark?.();
  };

  const reopen = () => {
    if (!original || options.get()) return;
    try {
      const fresh = createChannel(original.name);
      options.wire(fresh, original);
      options.set(fresh);
    } catch (e) {
      // `original` is kept, so the next restore retries.
      console.error(`Failed to reopen the ${options.label ?? original.name} BroadcastChannel after a bfcache restore`, e);
    }
  };

  const onPageShow = (event: Event) => {
    if (!(event as PageTransitionEvent).persisted || !parked) return;
    parked = false;
    reopen();
    options.onRestore?.();
  };

  target.addEventListener('pagehide', onPageHide);
  target.addEventListener('pageshow', onPageShow);
  return () => {
    target.removeEventListener('pagehide', onPageHide);
    target.removeEventListener('pageshow', onPageShow);
  };
}
