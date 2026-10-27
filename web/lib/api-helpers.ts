import type { SupabaseClient } from '@supabase/supabase-js';
import { isFilterableLine } from '@/lib/linelist';
import { getAccessContext, type LinkScope } from '@/lib/auth/access-context';

/** Parse a comma-separated query-string value into a non-empty string list, or null. */
export function parseCSV(value: string | null): string[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => s.trim()).filter(s => s.length > 0);
  return items.length > 0 ? items : null;
}

/** Parse a comma-separated query-string value into a non-empty int list, or null. */
export function parseIntCSV(value: string | null): number[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
  return items.length > 0 ? items : null;
}

/**
 * Resolve a list of object-list slugs to DB IDs, keeping only the lists
 * `viewerId` may see. Returns null if no slugs were supplied (meaning: don't
 * filter by list); returns [] if none match.
 *
 * The bearer routes pass the service-role client, and the list RPCs then test
 * membership without RLS too, so an unscoped lookup would let any API key
 * filter by (and so enumerate) the members of someone else's private list.
 * The test mirrors viewable_list_ids() (supabase/schemas/functions.sql) for an
 * explicit user: owner, public_read / public_edit, or a share. A list the
 * viewer can't see resolves like an unknown slug. A failed shares lookup
 * fails closed (owned and public lists only).
 */
export async function resolveListIds(
  supabase: SupabaseClient,
  slugs: string[] | null,
  viewerId: string,
): Promise<number[] | null> {
  if (!slugs || slugs.length === 0) return null;
  const [lists, shares] = await Promise.all([
    supabase.from('object_lists').select('id, created_by, visibility').in('slug', slugs),
    supabase.from('object_list_shares').select('list_id').eq('user_id', viewerId),
  ]);
  const shared = new Set((shares.data ?? []).map((r: { list_id: number }) => r.list_id));
  return ((lists.data ?? []) as { id: number; created_by: string | null; visibility: string }[])
    .filter(l =>
      l.created_by === viewerId
      || l.visibility === 'public_read'
      || l.visibility === 'public_edit'
      || shared.has(l.id))
    .map(l => l.id);
}

/**
 * Whether the user is a CAMPFIRE admin.
 *
 * Admin-ness is derived ONLY from user_profiles.is_admin — NEVER inferred
 * from program slugs. Used to gate visibility of unpublished spectra
 * (deploy_status != 'published') and their parent objects/targets
 * (has_published_spectrum = false). Fail-closed: any lookup failure → false.
 *
 * Memoized per user (lib/auth/access-context.ts): after validateAuth() /
 * getRequestPrincipal() this is a Map hit, not a query.
 */
export async function isAdminUser(userId: string): Promise<boolean> {
  return (await getAccessContext(userId)).isAdmin;
}

export type { LinkScope } from '@/lib/auth/access-context';

/**
 * The share-link scope for a link account, or null for an ordinary user.
 *
 * The API layer's mirror of the SQL helpers (is_link_account() + link_*()):
 * routes that authorize through service-role queries instead of RLS must
 * consult this, or a link session/bearer JWT walks straight past the RLS
 * narrowing (docs/design-public-mirror.md §5). An inactive (revoked or
 * expired) link resolves to active: false, which callers must treat as
 * "sees nothing" — never as "ordinary user". Fail-closed: a profile lookup
 * failure counts as a link account with no scope. Memoized (see isAdminUser).
 */
export async function getLinkScope(userId: string): Promise<LinkScope | null> {
  return (await getAccessContext(userId)).linkScope;
}

/**
 * All program slugs the user may read, per the SQL authority
 * accessible_program_slugs(): grants ∪ public for ordinary users, every
 * program for admins, only the scoped observation's program for link
 * accounts. Memoized (see isAdminUser).
 */
export async function getAccessiblePrograms(userId: string): Promise<string[]> {
  return (await getAccessContext(userId)).accessibleSlugs;
}


/**
 * Emission-line filter query parameters of the v1 list endpoints
 * (`line`, `line_snr_min`, `line_snr_max`, `line_include_stale`) as RPC
 * params — present only while `line` names a filterable catalog entry (a
 * doublet total or a stand-alone line, lib/linelist.ts), so a request without
 * one sends nothing and keeps working against a database that predates the
 * parameters. An unknown line name is a 400 from the caller's point of view
 * (`error` set) rather than an empty result that hides a typo.
 */
export function lineFilterRpcParams(searchParams: URLSearchParams): {
  p_line?: string;
  p_line_snr_min?: number | null;
  p_line_snr_max?: number | null;
  p_line_include_stale?: boolean;
} {
  const line = searchParams.get('line')?.trim();
  if (!line) return {};
  const num = (key: string): number | null => {
    const raw = searchParams.get(key);
    if (raw === null || raw === '') return null;
    const v = parseFloat(raw);
    return Number.isFinite(v) ? v : null;
  };
  return {
    p_line: line,
    p_line_snr_min: num('line_snr_min'),
    p_line_snr_max: num('line_snr_max'),
    p_line_include_stale: searchParams.get('line_include_stale') === 'true',
  };
}

/**
 * Photometry band filter query parameters of the v1 list endpoints
 * (`band`, `band_mag_min`, `band_mag_max`, `band_snr_min`, `band_snr_max`) as
 * RPC params — present only while `band` is set, so a request without one
 * sends nothing and keeps working against a database that predates the
 * parameters. There is no validation counterpart to invalidLineParam: band
 * names are per-field catalog configuration, not a fixed catalog, so the
 * server has nothing to check a name against and an unknown one simply
 * matches no photometry.
 */
export function bandFilterRpcParams(searchParams: URLSearchParams): {
  p_band?: string;
  p_band_mag_min?: number | null;
  p_band_mag_max?: number | null;
  p_band_snr_min?: number | null;
  p_band_snr_max?: number | null;
} {
  const band = searchParams.get('band')?.trim();
  if (!band) return {};
  const num = (key: string): number | null => {
    const raw = searchParams.get(key);
    if (raw === null || raw === '') return null;
    const v = parseFloat(raw);
    return Number.isFinite(v) ? v : null;
  };
  return {
    p_band: band,
    p_band_mag_min: num('band_mag_min'),
    p_band_mag_max: num('band_mag_max'),
    p_band_snr_min: num('band_snr_min'),
    p_band_snr_max: num('band_snr_max'),
  };
}

/**
 * The several-columns parameters of the v1 list endpoints (phase 3c, #660):
 * `line_columns=Halpha,OIII5007` and `band_columns=f444w,f150w` add
 * `line_columns` / `band_columns` maps to every row (display only; the
 * scalar `line` / `band` columns stay), and `sort=line_snr:<line>` /
 * `band_mag:<band>` / `band_snr:<band>` sorts on one of them. Present only
 * while asked for, like the line and band parameters.
 */
export function measurementColumnsRpcParams(searchParams: URLSearchParams): { p_line_columns?: Record<string, object>; p_band_columns?: Record<string, object> } {
  const lines = (parseCSV(searchParams.get('line_columns')) ?? []).filter(isFilterableLine);
  const bands = parseCSV(searchParams.get('band_columns')) ?? [];
  // a JSON map of the asked keys; the API has no per-key window, so every value counts
  return {
    ...(lines.length ? { p_line_columns: Object.fromEntries(lines.map((l) => [l, {}])) } : {}),
    ...(bands.length ? { p_band_columns: Object.fromEntries(bands.map((b) => [b, {}])) } : {}),
  };
}

/** A sort column of the v1 list endpoints: a fixed name, or 'line_snr:<line>' / 'band_mag:<band>' / 'band_snr:<band>'. */
export function isMeasurementSort(column: string): boolean {
  return /^(line_snr|band_mag|band_snr):[^,]+$/.test(column);
}

/** The `line` query parameter, when set, must name a filterable catalog entry. */
export function invalidLineParam(searchParams: URLSearchParams): string | null {
  const line = searchParams.get('line')?.trim();
  if (!line || isFilterableLine(line)) return null;
  return `Unknown emission line '${line}'. Use a doublet total (e.g. CIII1908, OII3727, SII6725) or a stand-alone line name (e.g. Halpha, OIII5007).`;
}

/**
 * The predicate tree a request carries as `q=` (Catalog Read Model phase 3):
 * JSON, at most 20 kB, an object. Only the shape is checked here; the
 * database compiles it against its whitelist and answers 22023 with a message
 * naming what it refused, which the routes turn into a 400.
 */
export function parseTreeParam(searchParams: URLSearchParams): { tree: Record<string, unknown> | null; error?: string } {
  const raw = searchParams.get('q');
  if (raw == null || raw.trim() === '') return { tree: null };
  if (raw.length > 20000) return { tree: null, error: 'q is larger than 20 kB' };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { tree: null, error: 'q is not valid JSON' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { tree: null, error: 'q must be a JSON object: a predicate tree' };
  }
  return { tree: parsed as Record<string, unknown> };
}

/** The 400 body for a tree the database refused (SQLSTATE 22023), or null. */
export function treeRejection(error: { code?: string; message?: string } | null): string | null {
  if (error && error.code === '22023' && error.message) return error.message;
  return null;
}
