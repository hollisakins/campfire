import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';
import type { RequestPrincipal } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/service';
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
 * per-program counts are head counts on the frozen tables, also under RLS.
 * The export ledger is admin-only by policy; it is read with the service
 * client and filtered with the same `fileVisible` rule the
 * /api/v1/dr/<tag>/files route applies, so a reader sees exactly the files
 * that route would hand them (never the storage keys).
 */

const RELEASE_COLUMNS =
  'tag, state, components, dr_format_version, counts, frozen_at, published_at, withdrawn_at, doi, notes, known_issues';

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

export interface ReleaseProgramCount {
  slug: string;
  name: string | null;
  spectra: number;
  objects: number;
}

export interface ReleaseFile {
  stream: string;
  scope: string;
  rows: number | null;
  size: number | null;
  sha256: string | null;
}

export interface ReleaseDetail {
  release: DrReleaseRow;
  programs: ReleaseProgramCount[];
  files: ReleaseFile[];
  /** Ledger rows exist (the release was exported). */
  exported: boolean;
  /** Every ledger file is ready. */
  complete: boolean;
}

const COUNT_CONCURRENCY = 8;

async function mapLimited<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

async function headCount(query: PromiseLike<{ count: number | null; error: { message: string } | null }>): Promise<number> {
  const { count, error } = await query;
  if (error) throw new Error(`release count failed: ${error.message}`);
  return count ?? 0;
}

/**
 * Per-program counts of a release for this viewer: spectra by row-local
 * program, objects whose frozen programs include it (an object observed by
 * several programs counts in each; dr_objects RLS shows a reader only the
 * objects all of whose programs they hold).
 */
async function programCounts(
  supabase: SupabaseClient,
  tag: string,
  slugs: string[],
): Promise<ReleaseProgramCount[]> {
  const counted = await mapLimited(slugs, COUNT_CONCURRENCY, async (slug) => {
    const [spectra, objects] = await Promise.all([
      headCount(
        supabase.from('dr_spectra').select('spectrum_id', { count: 'exact', head: true })
          .eq('tag', tag).eq('program_slug', slug),
      ),
      headCount(
        supabase.from('dr_objects').select('object_id', { count: 'exact', head: true })
          .eq('tag', tag).contains('programs', [slug]),
      ),
    ]);
    return { slug, name: null as string | null, spectra, objects };
  });
  const present = counted.filter((p) => p.spectra > 0 || p.objects > 0);
  if (present.length === 0) return present;
  const { data } = await supabase
    .from('programs')
    .select('slug, program_name')
    .in('slug', present.map((p) => p.slug));
  const names = new Map((data ?? []).map((r: { slug: string; program_name: string | null }) => [r.slug, r.program_name]));
  return present
    .map((p) => ({ ...p, name: names.get(p.slug) ?? null }))
    .sort((a, b) => b.spectra - a.spectra || a.slug.localeCompare(b.slug));
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
  const [programs, ledger] = await Promise.all([
    programCounts(principal.supabase, tag, [...accessible]),
    fetchLedger(createServiceClient(), tag),
  ]);
  return {
    release,
    programs,
    files: visibleFiles(ledger, accessible, release.components),
    exported: ledger.length > 0,
    complete: ledger.length > 0 && ledger.every((r) => r.state === 'ready'),
  };
}
