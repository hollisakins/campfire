import { NextRequest } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { EXPOSURE_SORT_KEYS } from '@/lib/admin/sort-keys';
import { filenameSearchPattern } from '@/lib/admin/exposure-search';

/**
 * GET /api/nircam-png/manifest?field=..&filter=..&detector=..&review=..&stage=..&correction=..&search=..&sort=..&dir=..
 *
 * The PNG pre-download manifest for a filtered set: which exposures have a
 * downloadable display PNG (full-res when deployed, else preview — the byte
 * /api/nircam-png serves), in the list's inspection order, each with its
 * exact size from the storage_objects registry. Exposures with no PNG are
 * omitted — they can't download, so they must not count toward the
 * pre-download button. Sizes are registry truth, not a per-image heuristic
 * (full-res PNG size is content-dependent and varies ~2x between fields);
 * keys the registry doesn't know come back bytes:null and the client's hint
 * degrades to a lower bound.
 *
 * A plain GET on purpose, NOT a server action: Next serializes server
 * actions per client, so this scan (row paging + chunked size lookups) as an
 * action queued AHEAD of the list page's own table fetch on every filter
 * change and froze the table until the manifest landed. As a route handler
 * it runs alongside the actions, and the client aborts it when the filters
 * change again. Reads run under the caller's RLS session (nircam_exposures
 * and storage_objects are admin-gated at the database), so a non-admin gets
 * an empty manifest, never data — same posture as the hot-path actions.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) return new Response('Unauthorized', { status: 401 });

  const sp = request.nextUrl.searchParams;
  const sortParam = sp.get('sort');
  const sortCol =
    sortParam && (EXPOSURE_SORT_KEYS as readonly string[]).includes(sortParam)
      ? sortParam
      : 'filename';
  const asc = sp.get('dir') !== 'desc';
  const eq: [string, string][] = [];
  for (const [param, column] of [
    ['field', 'field'], ['filter', 'filter'], ['detector', 'detector'],
    ['review', 'review_status'], ['stage', 'stage'], ['correction', 'correction'],
  ] as const) {
    const v = sp.get(param);
    if (v) eq.push([column, v]);
  }
  // Same glob → ILIKE translation as get_admin_exposures' p_search, so the
  // manifest covers exactly the set the searched list shows.
  const searchPattern = filenameSearchPattern(sp.get('search'));

  const CHUNK = 1000;   // PostgREST caps a single response at 1000 rows
  const CAP = 20000;    // runaway guard
  // storage_objects lookups go out as GET query strings; ~100 keys per
  // request keeps the URL comfortably under proxy limits.
  const SIZE_CHUNK = 100;

  try {
    const rows: { id: number; key: string }[] = [];
    for (let off = 0; off < CAP; off += CHUNK) {
      let q = supabase.from('nircam_exposures').select('id, png_path, full_png_path');
      for (const [col, v] of eq) q = q.eq(col, v);
      if (searchPattern) q = q.ilike('filename', searchPattern);
      // Mirror get_admin_exposures' ORDER BY: 'filename' is the compound
      // (field, filter, filename) list order; everything gets the id tiebreak.
      if (sortCol === 'filename') {
        q = q
          .order('field', { ascending: asc })
          .order('filter', { ascending: asc })
          .order('filename', { ascending: asc });
      } else {
        q = q.order(sortCol, { ascending: asc, nullsFirst: false });
      }
      q = q.order('id', { ascending: true }).range(off, off + CHUNK - 1);

      const { data, error } = await q;
      if (error) {
        return Response.json({ entries: [], error: error.message }, { status: 500 });
      }
      for (const r of data ?? []) {
        const key = (r.full_png_path ?? r.png_path) as string | null;
        if (key) rows.push({ id: r.id as number, key });
      }
      if (!data || data.length < CHUNK) break;
    }

    const keys = [...new Set(rows.map((r) => r.key))];
    const sizeByKey = new Map<string, number>();
    const chunks: string[][] = [];
    for (let i = 0; i < keys.length; i += SIZE_CHUNK) {
      chunks.push(keys.slice(i, i + SIZE_CHUNK));
    }
    // Best-effort: a failed size lookup leaves those entries at bytes:null
    // (hint degrades to a lower bound) rather than failing the manifest.
    await Promise.all(chunks.map(async (chunk) => {
      const { data } = await supabase
        .from('storage_objects')
        .select('storage_key, size_bytes')
        .eq('status', 'active')
        .in('storage_key', chunk);
      for (const r of data ?? []) {
        if (typeof r.size_bytes === 'number') sizeByKey.set(r.storage_key, r.size_bytes);
      }
    }));

    return Response.json(
      { entries: rows.map((r) => ({ id: r.id, bytes: sizeByKey.get(r.key) ?? null })) },
      { headers: { 'Cache-Control': 'private, no-store' } },
    );
  } catch (err) {
    console.error('nircam-png manifest error:', err);
    return Response.json({ entries: [], error: 'Manifest failed' }, { status: 500 });
  }
}
