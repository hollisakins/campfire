/**
 * Settling an auth email link (password recovery, email change) on the page
 * it lands on, driven by the auth client's events rather than a snapshot.
 *
 * With `@supabase/ssr`'s browser client (PKCE, `detectSessionInUrl`) the
 * `?code=` exchange runs asynchronously during client initialisation: on
 * success auth-js removes `code` from the URL (`replaceState`), saves the
 * session and emits `SIGNED_IN` / `PASSWORD_RECOVERY`. A landing page that
 * reads the URL and `getSession()` once therefore races it in both
 * directions (#641): before the exchange it sees no session, after it the
 * `code` is gone and the page concludes the link was bad — while GoTrue has
 * already consumed the single-use token. `settleAuthLink` subscribes first,
 * accepts whichever event carries the session, and only exchanges the code
 * itself once the client reports (`INITIAL_SESSION`) that it did not.
 *
 * No `'use client'` directive: pure, importable from node tests.
 */

export type AuthLink =
  | { kind: 'error'; code: string; description: string | null }
  | { kind: 'code'; code: string }
  | { kind: 'tokens'; accessToken: string; refreshToken: string; type: string | null }
  | { kind: 'none' };

/** Classify the landing URL. GoTrue reports errors in the query (PKCE) or the hash (implicit). */
export function parseAuthLink(href: string): AuthLink {
  const url = new URL(href);
  const query = url.searchParams;
  const hash = new URLSearchParams(url.hash.replace(/^#/, ''));
  const pick = (name: string) => query.get(name) ?? hash.get(name);

  const errorCode = pick('error_code');
  const error = pick('error');
  const description = pick('error_description');
  if (errorCode || error || description) {
    return { kind: 'error', code: errorCode ?? error ?? 'unknown', description };
  }

  const code = query.get('code');
  if (code) return { kind: 'code', code };

  const accessToken = hash.get('access_token');
  const refreshToken = hash.get('refresh_token');
  if (accessToken && refreshToken) {
    return { kind: 'tokens', accessToken, refreshToken, type: hash.get('type') };
  }

  return { kind: 'none' };
}

export const hasAuthCode = (href: string): boolean => new URL(href).searchParams.has('code');

export interface LinkSession {
  user: { id: string; email?: string | null };
}

export interface LinkSessionResult {
  data: { session: LinkSession | null };
  error: { message: string } | null;
}

/** The slice of `SupabaseClient['auth']` the settlement needs (narrow so tests can fake it). */
export interface LinkAuthClient {
  onAuthStateChange(
    callback: (event: string, session: LinkSession | null) => void,
  ): { data: { subscription: { unsubscribe: () => void } } };
  exchangeCodeForSession(code: string): Promise<LinkSessionResult>;
  setSession(tokens: { access_token: string; refresh_token: string }): Promise<LinkSessionResult>;
}

export type LinkInvalidReason =
  /** The URL carries GoTrue's error: the single-use token was already used, or it expired. */
  | 'expired'
  /** The exchange / session set was refused. */
  | 'rejected'
  /** Implicit-flow tokens of another `type` than the page handles. */
  | 'wrong_type'
  /** Nothing to settle: no code, no tokens, and the client finished with no session. */
  | 'missing';

export type LinkOutcome =
  | { status: 'ready'; session: LinkSession }
  | { status: 'invalid'; reason: LinkInvalidReason };

export interface SettleOptions {
  /** Implicit-flow links must carry this `type` (e.g. `'recovery'`); PKCE links carry none. */
  expectType?: string;
  /** The current URL, re-read when the client reports it has finished initialising. */
  readHref: () => string;
  onSettled: (outcome: LinkOutcome) => void;
}

const SESSION_EVENTS = new Set(['PASSWORD_RECOVERY', 'SIGNED_IN', 'USER_UPDATED']);

/**
 * Settle `link` against `auth`; `onSettled` fires exactly once unless the
 * returned cleanup runs first. Returns the cleanup (effect-shaped).
 */
export function settleAuthLink(auth: LinkAuthClient, link: AuthLink, opts: SettleOptions): () => void {
  let active = true;
  let unsubscribe: (() => void) | null = null;

  const settle = (outcome: LinkOutcome) => {
    if (!active) return;
    active = false;
    unsubscribe?.();
    unsubscribe = null;
    opts.onSettled(outcome);
  };
  const fromResult = ({ data, error }: LinkSessionResult) => {
    if (error || !data.session) settle({ status: 'invalid', reason: 'rejected' });
    else settle({ status: 'ready', session: data.session });
  };
  const rejected = () => settle({ status: 'invalid', reason: 'rejected' });

  if (link.kind === 'error') {
    const used = link.code === 'otp_expired' || link.code === 'access_denied';
    settle({ status: 'invalid', reason: used ? 'expired' : 'rejected' });
  } else if (link.kind === 'tokens') {
    // Implicit flow: the PKCE client refuses the hash itself, so the page sets the session.
    if (opts.expectType && link.type !== opts.expectType) {
      settle({ status: 'invalid', reason: 'wrong_type' });
    } else {
      void auth
        .setSession({ access_token: link.accessToken, refresh_token: link.refreshToken })
        .then(fromResult, rejected);
    }
  } else {
    const {
      data: { subscription },
    } = auth.onAuthStateChange((event, session) => {
      if (!active) return;
      if (SESSION_EVENTS.has(event) && session) {
        settle({ status: 'ready', session });
        return;
      }
      if (event !== 'INITIAL_SESSION') return;
      // The client has finished its own URL handling. A successful exchange
      // removed `code` from the URL; a `code` still there was not exchanged
      // (no verifier in this browser) or the exchange failed, so run it here
      // and let the result decide — an error is then a real rejection.
      const pending = link.kind === 'code' && hasAuthCode(opts.readHref()) ? link.code : null;
      if (pending) {
        unsubscribe?.();
        unsubscribe = null;
        void auth.exchangeCodeForSession(pending).then(fromResult, rejected);
        return;
      }
      if (session) settle({ status: 'ready', session });
      else settle({ status: 'invalid', reason: 'missing' });
    });
    unsubscribe = () => subscription.unsubscribe();
    if (!active) unsubscribe();
  }

  return () => {
    if (!active) return;
    active = false;
    unsubscribe?.();
    unsubscribe = null;
  };
}
