/**
 * Legacy OFFSET retirement for the /api/v1/sync/* catalog endpoints (perf
 * T2-F, #511, decision D-F).
 *
 * The four sync RPCs have been keyset-only (`after=`) on the client side since
 * #103 (July 2026), but the server kept honouring `offset=` for clients that
 * predate it — behind a 120 s statement_timeout exemption, because a deep
 * OFFSET page re-reads everything before it. That path is gone: the RPCs no
 * longer take `p_offset`, and a request that still asks for a positional page
 * is answered with 400 and an upgrade pointer instead of a silently wrong
 * first page. `offset=0` (what an old client sends for page one) is harmless
 * and ignored, so the failure lands on page two with a clear message rather
 * than on the very first call.
 */
import { NextResponse } from 'next/server';

/** Minimum Python client release whose sync walk is keyset-only. */
export const SYNC_CLIENT_FLOOR = '0.5.0';

export function rejectLegacyOffset(searchParams: URLSearchParams): NextResponse | null {
  const raw = searchParams.get('offset');
  if (raw === null) return null;
  const offset = parseInt(raw, 10);
  if (!Number.isFinite(offset) || offset <= 0) return null;
  if (searchParams.get('after')) return null; // keyset client that also echoed offset
  return NextResponse.json(
    {
      error: 'offset pagination is no longer supported on /api/v1/sync/*',
      details:
        `Page with after=<last row's cursor> instead. The campfire Python client does this ` +
        `automatically from v${SYNC_CLIENT_FLOOR}: upgrade with "git pull && python3 install.py" ` +
        `(repo checkout) or "pip install -U campfire-layout@git+https://github.com/hollisakins/campfire.git#subdirectory=layout campfire@git+https://github.com/hollisakins/campfire.git#subdirectory=python".`,
      minimum_client_version: SYNC_CLIENT_FLOOR,
    },
    { status: 400, headers: { 'X-Campfire-Minimum-Client': SYNC_CLIENT_FLOOR } },
  );
}
