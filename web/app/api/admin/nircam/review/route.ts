import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * POST /api/admin/nircam/review — persist a NIRCam triage decision.
 *
 * The transport target for the triage outbox (lib/nircam-review-outbox.ts).
 * This is deliberately a route handler rather than a server action: the
 * outbox needs a fetch it controls — request timeout, retry-on-drop, and
 * `keepalive` so a flush survives the page being torn down — none of which
 * the server-action transport exposes. Route handlers also run in parallel
 * with the action lane, so saves never queue behind the triage page's read
 * traffic.
 *
 * Body: {
 *   id: number,                       // nircam_exposures.id
 *   decidedAt: number,                // ms epoch of the operator's decision
 *   fields: {                         // partial — only operator-touched keys
 *     review_status?, correction?, notes?,
 *   },
 * }
 *
 * Retries make delivery at-least-once, so writes must be safe to replay and
 * to arrive out of order: the update carries a last-writer-wins guard on
 * review_decided_at. An equal stamp re-applies (idempotent retry); an older
 * stamp is skipped and the current row is returned with `superseded: true`
 * so the client treats the outrun decision as settled.
 *
 * Auth mirrors the triage server actions' requireSession fast path: confirm a
 * session exists and let nircam_exposures RLS be the authority — a non-admin
 * update matches zero rows, which (with the row invisible to the follow-up
 * read) surfaces as 404, never as data.
 */

const REVIEW_STATUS_VALUES = new Set(['pending', 'approved', 'excluded']);
const CORRECTION_VALUES = new Set(['none', 'needed', 'done']);
const MAX_NOTES_LENGTH = 20_000;
// Tolerated clock skew for the client-supplied decision stamp. A stamp far in
// the future would wedge the row (every later honest decision < the poisoned
// review_decided_at), so absurd values are rejected rather than stored.
const MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000;

interface ReviewFields {
  review_status?: 'pending' | 'approved' | 'excluded';
  correction?: 'none' | 'needed' | 'done';
  notes?: string;
}

function parseBody(body: unknown): { id: number; decidedAt: number; fields: ReviewFields } | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  const id = b.id;
  const decidedAt = b.decidedAt;
  if (typeof id !== 'number' || !Number.isInteger(id) || id <= 0) return null;
  if (typeof decidedAt !== 'number' || !Number.isFinite(decidedAt)) return null;
  if (decidedAt <= 0 || decidedAt > Date.now() + MAX_FUTURE_SKEW_MS) return null;
  const rawFields = b.fields;
  if (typeof rawFields !== 'object' || rawFields === null) return null;
  const f = rawFields as Record<string, unknown>;
  const fields: ReviewFields = {};
  if (f.review_status !== undefined) {
    if (typeof f.review_status !== 'string' || !REVIEW_STATUS_VALUES.has(f.review_status)) return null;
    fields.review_status = f.review_status as ReviewFields['review_status'];
  }
  if (f.correction !== undefined) {
    if (typeof f.correction !== 'string' || !CORRECTION_VALUES.has(f.correction)) return null;
    fields.correction = f.correction as ReviewFields['correction'];
  }
  if (f.notes !== undefined) {
    if (typeof f.notes !== 'string' || f.notes.length > MAX_NOTES_LENGTH) return null;
    fields.notes = f.notes;
  }
  if (Object.keys(fields).length === 0) return null;
  return { id, decidedAt, fields };
}

export async function POST(request: NextRequest) {
  const parsed = parseBody(await request.json().catch(() => null));
  if (!parsed) {
    return NextResponse.json({ error: 'Invalid review payload' }, { status: 400 });
  }
  const { id, decidedAt, fields } = parsed;

  const supabase = await createClient();
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const decidedIso = new Date(decidedAt).toISOString();
  const { data, error } = await supabase
    .from('nircam_exposures')
    .update({
      ...fields,
      review_decided_at: decidedIso,
      updated_at: new Date().toISOString(),
    })
    .eq('id', id)
    // The LWW guard. `.or` ANDs with `.eq` above; <= (not <) so an exact
    // retry of the same decision still succeeds instead of reporting stale.
    .or(`review_decided_at.is.null,review_decided_at.lte.${decidedIso}`)
    .select()
    .maybeSingle();

  if (error) {
    // Database/transport-level failure: retryable by the outbox.
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (data) {
    return NextResponse.json({ exposure: data });
  }

  // Zero rows: the guard rejected an older stamp, or the row doesn't exist /
  // isn't visible under RLS. Disambiguate with a plain read.
  const { data: row, error: readError } = await supabase
    .from('nircam_exposures')
    .select('*')
    .eq('id', id)
    .maybeSingle();
  if (readError) {
    return NextResponse.json({ error: readError.message }, { status: 500 });
  }
  if (!row) {
    return NextResponse.json({ error: 'Exposure not found' }, { status: 404 });
  }
  return NextResponse.json({ exposure: row, superseded: true });
}
