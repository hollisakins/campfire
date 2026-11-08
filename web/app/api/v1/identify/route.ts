import { NextRequest, NextResponse } from 'next/server';
import { rejectReleaseParam } from '@/lib/server/bearer-release';
import { authenticateApiRequest } from '@/lib/api-auth';
import { createServiceClient } from '@/lib/supabase/service';

/**
 * POST /api/v1/identify   (bearer; `campfire identify FILE...`, P5-2)
 *
 * What the archive knows about files by their bytes (design §4.6): for each
 * {sha256, science_hash?} the live spectra, the release members and the
 * archive tombstones that match, under the caller's program scope
 * (`identify_files`, explicit scope: the route runs as the service role).
 * The client phrases the answer. Live-only by contract: a `dr` parameter is
 * a 400 (releases are part of the answer, not a mode).
 *
 * Body:     { files: [{ sha256: "sha256:<hex>", science_hash?: "sha256:<hex>" }] }  (≤ 200)
 * Response: { results: [{ sha256, science_hash, live: [...], releases: [...], collected: [...] }] }
 */
const MAX_FILES = 200;
const HASH_RE = /^sha256:[0-9a-f]{64}$/;

export async function POST(request: NextRequest) {
  const dr400 = rejectReleaseParam(request.nextUrl.searchParams);
  if (dr400) return dr400;
  const principal = await authenticateApiRequest(request);
  if (!principal) {
    return NextResponse.json({ error: 'Invalid or missing authentication' }, { status: 401 });
  }
  const noStore = { 'Cache-Control': 'private, no-store' };

  let body: { files?: unknown } | null = null;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Body must be JSON' }, { status: 400, headers: noStore });
  }
  const raw = body?.files;
  if (!Array.isArray(raw) || raw.length === 0) {
    return NextResponse.json({ error: 'Body must include a non-empty "files" array' }, { status: 400, headers: noStore });
  }
  if (raw.length > MAX_FILES) {
    return NextResponse.json({ error: `Too many files (max ${MAX_FILES} per request)` }, { status: 400, headers: noStore });
  }
  const files = raw.map((f) => {
    const o = (f && typeof f === 'object' ? f : {}) as Record<string, unknown>;
    const sha = typeof o.sha256 === 'string' && HASH_RE.test(o.sha256) ? o.sha256 : null;
    const sci = typeof o.science_hash === 'string' && HASH_RE.test(o.science_hash) ? o.science_hash : null;
    return { sha256: sha, science_hash: sci };
  });
  if (files.every((f) => !f.sha256 && !f.science_hash)) {
    return NextResponse.json({ error: 'No file carries a sha256:<hex> hash' }, { status: 400, headers: noStore });
  }

  try {
    const supabase = createServiceClient();
    const { data, error } = await supabase.rpc('identify_files', {
      p_files: files,
      p_program_slugs: principal.access.accessibleSlugs,
      p_include_unpublished: principal.access.isAdmin,
    });
    if (error) {
      console.error('identify_files failed:', error);
      return NextResponse.json({ error: 'Failed to identify files', details: error.message }, { status: 500, headers: noStore });
    }
    return NextResponse.json({ results: data ?? [] }, { headers: noStore });
  } catch (error) {
    console.error('Error in POST /api/v1/identify:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
