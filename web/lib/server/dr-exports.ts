import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Data-release exports (P4b, docs/dr-format-v1.md).
 *
 * A frozen release is exported as one gzip JSONL file per (stream, scope:
 * program slug or field-level component) under `dr/<tag>/exports/` on the
 * private data bucket, recorded in the `dr_exports` ledger. The files are built
 * by `campfire dr export` (python/campfire/dr_export.py); this module is the
 * reader side: which files a viewer may fetch.
 *
 * Like the sync snapshots, the keys are not layout products: never registered
 * in storage_objects, never presignable through the storage routes; readers
 * get them only through /api/v1/dr/<tag>/files, which hands out the files the
 * caller may read.
 */

/** Bump when the row shapes or the file layout change; recorded on dr_releases. */
export const DR_FORMAT_VERSION = 1;

export const DR_EXPORT_STREAMS = ['objects', 'spectra', 'line_fits', 'photometry', 'manifest'] as const;
export type DrExportStream = (typeof DR_EXPORT_STREAMS)[number];

export interface DrExportLedgerRow {
  tag: string;
  stream: DrExportStream;
  partition: string;
  storage_key: string | null;
  sha256: string | null;
  size_bytes: number | null;
  rows: number | null;
  state: 'pending' | 'building' | 'ready' | 'failed';
  built_at: string | null;
  error: string | null;
}

export interface DrReleaseRow {
  tag: string;
  state: string;
  components: unknown;
  dr_format_version: number;
  counts: Record<string, unknown> | null;
  frozen_at: string | null;
  published_at: string | null;
  withdrawn_at: string | null;
  doi: string | null;
  notes: string | null;
  known_issues: string | null;
  /** The release page document (P6b-3), when the row was read with it. */
  page_md?: string | null;
}

/** A field-level component name (nircam/<field>, photometry/<field>/<catalog>). */
export function isComponentScope(scope: string): boolean {
  return scope.startsWith('nircam/') || scope.startsWith('photometry/');
}

/**
 * The scopes open to every account: the release's own field-level
 * components, as frozen in `dr_releases.components`. Judged from the release,
 * never from a scope's name alone: program slugs carry no format
 * constraint, so a program could be named like a component (Codex on #600;
 * `dr_export_begin` also refuses a release where one is).
 */
export function openComponentScopes(components: unknown): ReadonlySet<string> {
  const list = Array.isArray(components) ? components : [];
  return new Set(list.filter((c): c is string => typeof c === 'string' && isComponentScope(c)));
}

/**
 * Whether a viewer holding `accessible` programs may fetch a (stream, scope)
 * file: program files of any stream within their programs; a field-level
 * component file only on the `manifest` stream, and only when it is one of the
 * release's components (published field deployments are visible to every
 * account, as live).
 */
export function fileVisible(
  stream: string,
  scope: string,
  accessible: ReadonlySet<string>,
  openComponents: ReadonlySet<string>,
): boolean {
  if (stream === 'manifest' && openComponents.has(scope)) return true;
  return accessible.has(scope);
}

/** Readers see a published release; admins see every state (the shadow run). */
export function releaseVisible(release: Pick<DrReleaseRow, 'state'>, isAdmin: boolean): boolean {
  return isAdmin || release.state === 'published';
}

/** The `dr_releases` row for a tag, or null. */
export async function fetchRelease(
  supabase: SupabaseClient,
  tag: string,
): Promise<DrReleaseRow | null> {
  const { data, error } = await supabase
    .from('dr_releases')
    .select(
      'tag, state, components, dr_format_version, counts, frozen_at, published_at, withdrawn_at, doi, notes, known_issues',
    )
    .eq('tag', tag)
    .maybeSingle();
  if (error) throw new Error(`dr_releases lookup failed: ${error.message}`);
  return (data as DrReleaseRow | null) ?? null;
}

/**
 * The releases a caller may read, newest frozen first (GET /api/v1/dr,
 * `campfire dr list`): published ones; every state to admins (the shadow run).
 */
export async function fetchVisibleReleases(
  supabase: SupabaseClient,
  isAdmin: boolean,
): Promise<DrReleaseRow[]> {
  let query = supabase
    .from('dr_releases')
    .select(
      'tag, state, components, dr_format_version, counts, frozen_at, published_at, withdrawn_at, doi, notes, known_issues',
    )
    .order('frozen_at', { ascending: false, nullsFirst: false })
    .order('tag');
  if (!isAdmin) query = query.eq('state', 'published');
  const { data, error } = await query;
  if (error) throw new Error(`dr_releases listing failed: ${error.message}`);
  return ((data as DrReleaseRow[] | null) ?? []).filter((r) => releaseVisible(r, isAdmin));
}

/** The export ledger of a tag. */
export async function fetchLedger(
  supabase: SupabaseClient,
  tag: string,
): Promise<DrExportLedgerRow[]> {
  const { data, error } = await supabase
    .from('dr_exports')
    .select('tag, stream, partition, storage_key, sha256, size_bytes, rows, state, built_at, error')
    .eq('tag', tag)
    .order('stream')
    .order('partition');
  if (error) throw new Error(`dr_exports lookup failed: ${error.message}`);
  return (data as DrExportLedgerRow[] | null) ?? [];
}

/**
 * The newest published release (by `published_at`), or null: the `latest_dr`
 * of /api/v1/auth/whoami (P5-3), which the sync tail announces.
 */
export async function fetchLatestPublishedRelease(
  supabase: SupabaseClient,
): Promise<Pick<DrReleaseRow, 'tag' | 'doi' | 'published_at'> | null> {
  const { data, error } = await supabase
    .from('dr_releases')
    .select('tag, doi, published_at')
    .eq('state', 'published')
    .order('published_at', { ascending: false, nullsFirst: false })
    .limit(1)
    .maybeSingle();
  if (error) throw new Error(`dr_releases lookup failed: ${error.message}`);
  return (data as Pick<DrReleaseRow, 'tag' | 'doi' | 'published_at'> | null) ?? null;
}

/** One program's entry in a release's roster (dr_program_roster). */
export interface DrProgramRosterEntry {
  is_public: boolean;
  spectra: number;
  cfpipe_versions: string[];
  crds_contexts: string[];
}

/**
 * The per-program roster of a release as THIS caller may see it (P5-3,
 * `campfire cite`): every program public at the freeze plus the caller's own
 * among the proprietary ones. Computed by `dr_program_roster` per call and
 * never stored on `dr_releases`, which every account reads whole.
 */
export async function fetchProgramRoster(
  supabase: SupabaseClient,
  tag: string,
  accessibleSlugs: readonly string[],
): Promise<Record<string, DrProgramRosterEntry>> {
  const { data, error } = await supabase.rpc('dr_program_roster', {
    p_tag: tag,
    p_program_slugs: [...accessibleSlugs],
  });
  if (error) throw new Error(`dr_program_roster failed: ${error.message}`);
  return (data as Record<string, DrProgramRosterEntry> | null) ?? {};
}
