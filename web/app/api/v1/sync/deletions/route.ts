import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';

/**
 * GET /api/v1/sync/deletions?since=<ISO timestamp>
 *
 * Ids hard-deleted from the synced tables after `since`, per sync stream
 * (`objects`, `spectra`, `storage`, `photometry`, `line_fits`). A client that
 * bootstrapped from a sync snapshot applies these after its catch-up walk: a
 * hard delete after the snapshot was built (a photometry supersede, `deploy
 * remove`) leaves nothing for the catch-up to return, so without this the
 * snapshot's copy of the row would outlive it. See get_sync_deletions.
 *
 * Only integer ids, unscoped -- the same contract as the sync routes'
 * `deleted_ids` tombstones: nothing about a deleted row is disclosed, and an
 * id the client never mirrored deletes nothing.
 *
 * The journal is trimmed to the oldest snapshot the builder keeps; a `since`
 * older than that is answered 410 (the client then walks live).
 */

const STREAMS = ['objects', 'spectra', 'storage', 'photometry', 'line_fits'] as const;

export async function GET(request: NextRequest) {
  const userId = await validateAuth(request);
  if (!userId) {
    return NextResponse.json({ error: 'Invalid or missing authentication' }, { status: 401 });
  }

  const sinceRaw = request.nextUrl.searchParams.get('since');
  const since = sinceRaw ? new Date(sinceRaw) : null;
  if (!since || Number.isNaN(since.getTime())) {
    return NextResponse.json({ error: 'since must be an ISO 8601 timestamp' }, { status: 400 });
  }

  try {
    const supabase = createServiceClient();

    // Retention floor: the builder trims the journal to the oldest ready
    // snapshot it keeps, so completeness is only promised from there on.
    const { data: oldest, error: floorError } = await supabase
      .from('sync_snapshots')
      .select('started_at')
      .eq('status', 'ready')
      .order('started_at', { ascending: true })
      .limit(1)
      .maybeSingle();
    if (floorError) throw new Error(`sync_snapshots lookup failed: ${floorError.message}`);
    if (!oldest || since < new Date((oldest as { started_at: string }).started_at)) {
      return NextResponse.json(
        { error: 'deletions before the oldest kept snapshot are not retained' },
        { status: 410 },
      );
    }

    const { data, error } = await supabase.rpc('get_sync_deletions', {
      p_since: since.toISOString(),
    });
    if (error) throw new Error(`get_sync_deletions failed: ${error.message}`);

    const deleted: Record<string, number[]> = Object.fromEntries(STREAMS.map((s) => [s, []]));
    for (const row of (data ?? []) as { stream: string; row_ids: number[] }[]) {
      if (row.stream in deleted) deleted[row.stream] = row.row_ids;
    }
    return NextResponse.json({ deleted }, { headers: { 'Cache-Control': 'private, no-store' } });
  } catch (error) {
    console.error('Error in API /v1/sync/deletions:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
