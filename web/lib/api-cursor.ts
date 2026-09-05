/**
 * Opaque keyset cursors for the /api/v1 list endpoints (perf T2-F, #511,
 * decision D-F).
 *
 * A cursor is the position of the last row of a page under one specific
 * (filter set, sort column, sort direction): the row's sort value in a typed
 * slot plus the ORDER BY tiebreak, exactly as the list RPCs
 * (`get_filtered_objects_paginated` / `get_filtered_spectra_paginated`) hand
 * it back in `next_sort_text` / `next_sort_num` / `next_tiebreak`. The route
 * never derives it from the JSON payload — the RPC computes it from the same
 * columns the ORDER BY sorts on.
 *
 * The encoding is base64url(JSON). It is not signed: a forged cursor can only
 * move the window inside the caller's own access-scoped result set, and the
 * RPC re-applies every filter and the access scope on each page. What it does
 * carry is a fingerprint of the filter/sort the cursor was minted under, so a
 * client that changes filters mid-walk gets a 400 instead of a silently
 * wrong page.
 */
import { createHash } from 'node:crypto';

export interface CursorPayload {
  /** Schema version. */
  v: 1;
  /** Fingerprint of (filters, sort, direction) the cursor is valid for. */
  f: string;
  /** Sort value when the resolved sort column is textual; else null. */
  t: string | null;
  /** Sort value when the resolved sort column is numeric; else null. */
  n: number | null;
  /** ORDER BY tiebreak tail (objects: [object_id]; spectra: [target_id, grating, spectrum_id]). */
  k: string[];
}

/** Shape of the RPC's next-cursor columns. */
export interface RpcCursorRow {
  has_more?: boolean | null;
  next_sort_text?: string | null;
  next_sort_num?: number | string | null;
  next_tiebreak?: string[] | null;
}

/** Query parameters that describe the page, not the result set. */
const PAGE_PARAMS = new Set(['cursor', 'limit', 'offset', 'count', 'include_count']);

/**
 * Which list endpoint a cursor belongs to. The two endpoints accept the same
 * filter vocabulary, so without this a cursor minted by one would pass the
 * fingerprint check on the other and the RPC would fall back to an
 * unpaginated first page instead of erroring.
 */
export interface CursorScope {
  /** Endpoint tag mixed into the fingerprint (e.g. 'objects', 'spectra'). */
  endpoint: string;
  /** Length of the ORDER BY tiebreak tail the endpoint's RPC expects. */
  tiebreakLength: number;
}

export const OBJECTS_CURSOR_SCOPE: CursorScope = { endpoint: 'objects', tiebreakLength: 1 };
export const SPECTRA_CURSOR_SCOPE: CursorScope = { endpoint: 'spectra', tiebreakLength: 3 };

/**
 * Fingerprint of everything that defines the ordered result set: the endpoint
 * plus every query parameter except the paging ones, canonicalized (sorted by
 * key, then value). `sort` / `sort_dir` are included by construction, so a
 * cursor minted under one sort is rejected under another, and the endpoint
 * tag rejects a cursor minted by the other list endpoint.
 */
export function cursorFingerprint(searchParams: URLSearchParams, scope: CursorScope): string {
  const pairs: string[] = [];
  for (const [key, value] of searchParams.entries()) {
    if (PAGE_PARAMS.has(key)) continue;
    pairs.push(`${key}=${value}`);
  }
  pairs.sort();
  return createHash('sha1')
    .update(`${scope.endpoint}\n${pairs.join('&')}`)
    .digest('hex')
    .slice(0, 12);
}

function toBase64Url(s: string): string {
  return Buffer.from(s, 'utf8').toString('base64url');
}

function fromBase64Url(s: string): string | null {
  try {
    // Reject anything that is not base64url before decoding, so a garbage
    // cursor is reported as invalid rather than as random bytes.
    if (!/^[A-Za-z0-9_-]+$/.test(s)) return null;
    return Buffer.from(s, 'base64url').toString('utf8');
  } catch {
    return null;
  }
}

/**
 * Build the `next_cursor` for a page from the RPC's cursor columns, or null
 * when there is no next page. PostgREST serializes double precision as a JSON
 * number, but a bigint-typed value would arrive as a string; accept both.
 */
export function encodeNextCursor(row: RpcCursorRow, fingerprint: string): string | null {
  if (!row.has_more) return null;
  const tiebreak = row.next_tiebreak;
  if (!Array.isArray(tiebreak) || tiebreak.length === 0) return null;
  let num: number | null = null;
  if (row.next_sort_num !== null && row.next_sort_num !== undefined) {
    const parsed = typeof row.next_sort_num === 'number' ? row.next_sort_num : Number(row.next_sort_num);
    num = Number.isFinite(parsed) ? parsed : null;
  }
  const payload: CursorPayload = {
    v: 1,
    f: fingerprint,
    t: row.next_sort_text ?? null,
    n: num,
    k: tiebreak.map(String),
  };
  return toBase64Url(JSON.stringify(payload));
}

export type DecodedCursor =
  | { ok: true; cursor: CursorPayload }
  | { ok: false; error: string };

/**
 * Decode and validate a client-supplied cursor against the current request's
 * fingerprint and endpoint scope. Every failure is a client error (400): the
 * cursor is malformed, from another schema version, minted under a different
 * filter/sort or endpoint, or carries a tiebreak of the wrong arity for this
 * endpoint's RPC (which would otherwise treat the cursor as absent and serve
 * an unpaginated first page rather than an error).
 */
export function decodeCursor(raw: string, fingerprint: string, scope: CursorScope): DecodedCursor {
  const json = fromBase64Url(raw);
  if (json === null) return { ok: false, error: 'Invalid cursor' };
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    return { ok: false, error: 'Invalid cursor' };
  }
  if (!parsed || typeof parsed !== 'object') return { ok: false, error: 'Invalid cursor' };
  const c = parsed as Record<string, unknown>;
  if (c.v !== 1) return { ok: false, error: 'Unsupported cursor version' };
  if (typeof c.f !== 'string') return { ok: false, error: 'Invalid cursor' };
  if (c.t !== null && typeof c.t !== 'string') return { ok: false, error: 'Invalid cursor' };
  if (c.n !== null && (typeof c.n !== 'number' || !Number.isFinite(c.n))) return { ok: false, error: 'Invalid cursor' };
  if (!Array.isArray(c.k) || c.k.length === 0 || !c.k.every((x) => typeof x === 'string')) {
    return { ok: false, error: 'Invalid cursor' };
  }
  if (c.f !== fingerprint) {
    return {
      ok: false,
      error: 'Cursor does not match the request endpoint, filters or sort; restart the walk without a cursor',
    };
  }
  if (c.k.length !== scope.tiebreakLength) {
    return { ok: false, error: 'Invalid cursor' };
  }
  return {
    ok: true,
    cursor: { v: 1, f: c.f, t: c.t as string | null, n: c.n as number | null, k: c.k as string[] },
  };
}

/** The RPC arguments a decoded cursor maps to (null when paging by offset). */
export function cursorRpcParams(cursor: CursorPayload | null): {
  p_after_sort_text: string | null;
  p_after_sort_num: number | null;
  p_after_tiebreak: string[] | null;
} {
  return {
    p_after_sort_text: cursor?.t ?? null,
    p_after_sort_num: cursor?.n ?? null,
    p_after_tiebreak: cursor?.k ?? null,
  };
}

/**
 * Parse the `count` flag. Default: count on offset/first pages (the legacy
 * contract, and what a cursor walk's first request needs to size its
 * progress), skip it once a cursor is supplied — the caller already has the
 * total from page one, and the count is a second full pass over the filter.
 * `include_count` is accepted as an alias.
 */
export function parseIncludeCount(searchParams: URLSearchParams, hasCursor: boolean): boolean {
  const raw = searchParams.get('count') ?? searchParams.get('include_count');
  if (raw === null) return !hasCursor;
  return raw.toLowerCase() !== 'false' && raw !== '0';
}

/**
 * Pagination block of a list response. `total` is -1 when the count was
 * skipped. `offset` is present only on the deprecated offset path.
 */
export interface ListPagination {
  total: number;
  limit: number;
  offset?: number;
  next_cursor: string | null;
  has_more: boolean;
}

/**
 * Response headers for the deprecated `offset=` path (accepted for one
 * client release, see docs/api/rest). RFC 9745 `Deprecation` plus a `Link`
 * to the successor contract.
 */
export function deprecationHeaders(docsUrl: string): Record<string, string> {
  return {
    Deprecation: 'true',
    Link: `<${docsUrl}>; rel="deprecation"; type="text/html"`,
    'X-Campfire-Deprecated': 'offset pagination; use cursor=<next_cursor> from the previous page',
  };
}
