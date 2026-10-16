import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';
import type { RequestPrincipal } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/service';
import { listReleaseMembers } from '@/lib/server/dr-files';
import {
  fetchLedger,
  fileVisible,
  openComponentScopes,
  type DrExportLedgerRow,
  type DrReleaseRow,
} from '@/lib/server/dr-exports';

/**
 * Reads behind the /releases pages (P6a). The release rows come through the
 * viewer's cookie client, so the dr_releases policy decides what exists for
 * them (published to every non-link account, every state to admins). The
 * export ledger is admin-only by policy; it is read with the service
 * client and filtered with the same `fileVisible` rule the
 * /api/v1/dr/<tag>/files route applies, so a reader sees exactly the files
 * that route would hand them (never the storage keys).
 */

const RELEASE_COLUMNS =
  'tag, state, components, dr_format_version, counts, frozen_at, published_at, withdrawn_at, doi, notes, known_issues, page_md';

/**
 * Releases visible to this client. The public pages pass `publishedOnly`
 * so they read the same for admins and readers; the admin page lists every
 * state RLS shows it.
 */
export async function listReleases(supabase: SupabaseClient, opts: { publishedOnly?: boolean } = {}): Promise<DrReleaseRow[]> {
  let q = supabase.from('dr_releases').select(RELEASE_COLUMNS);
  if (opts.publishedOnly) q = q.eq('state', 'published');
  const { data, error } = await q
    .order('published_at', { ascending: false, nullsFirst: true })
    .order('frozen_at', { ascending: false, nullsFirst: true })
    .order('tag', { ascending: false });
  if (error) throw new Error(`dr_releases list failed: ${error.message}`);
  return (data as DrReleaseRow[] | null) ?? [];
}

export async function getRelease(supabase: SupabaseClient, tag: string): Promise<DrReleaseRow | null> {
  const { data, error } = await supabase
    .from('dr_releases')
    .select(RELEASE_COLUMNS)
    .eq('tag', tag)
    .maybeSingle();
  if (error) throw new Error(`dr_releases lookup failed: ${error.message}`);
  return (data as DrReleaseRow | null) ?? null;
}

export interface ReleaseFile {
  stream: string;
  scope: string;
  rows: number | null;
  size: number | null;
  sha256: string | null;
}

/** A figure of the release page document (dr/<tag>/page/<sha16>/<name>). */
export interface ReleasePageAsset {
  name: string;
}

export interface ReleaseDetail {
  release: DrReleaseRow;
  files: ReleaseFile[];
  /** The page document's assets the viewer may fetch (component `page`, archived). */
  pageAssets: ReleasePageAsset[];
  /** Ledger rows exist (the release was exported). */
  exported: boolean;
  /** Every ledger file is ready. */
  complete: boolean;
}

/** The ledger files this viewer may fetch, in stream / scope order. */
export function visibleFiles(
  ledger: DrExportLedgerRow[],
  accessible: ReadonlySet<string>,
  components: unknown,
): ReleaseFile[] {
  const open = openComponentScopes(components);
  return ledger
    .filter((r) => r.state === 'ready' && r.storage_key && fileVisible(r.stream, r.partition, accessible, open))
    .map((r) => ({ stream: r.stream, scope: r.partition, rows: r.rows, size: r.size_bytes, sha256: r.sha256 }));
}

/** Everything /releases/<tag> shows, or null when the tag is not visible to the viewer. */
export async function loadReleaseDetail(principal: RequestPrincipal, tag: string): Promise<ReleaseDetail | null> {
  const release = await getRelease(principal.supabase, tag);
  if (!release) return null;

  const accessible = new Set(principal.access.accessibleSlugs);
  const [ledger, pageMembers] = await Promise.all([
    fetchLedger(createServiceClient(), tag),
    listReleaseMembers(principal.supabase, tag, { component: 'page' }).catch(() => []),
  ]);
  return {
    release,
    pageAssets: pageMembers.map((m) => ({ name: pageAssetName(m.storageKey) })).filter((a) => a.name),
    files: visibleFiles(ledger, accessible, release.components),
    exported: ledger.length > 0,
    complete: ledger.length > 0 && ledger.every((r) => r.state === 'ready'),
  };
}

/** `dr/<tag>/page/<sha16>/<name>` → `<name>` (empty for any other shape). */
export function pageAssetName(storageKey: string): string {
  const m = /^dr\/[^/]+\/page\/[0-9a-f]{16}\/([^/]+)$/.exec(storageKey);
  return m ? m[1] : '';
}
