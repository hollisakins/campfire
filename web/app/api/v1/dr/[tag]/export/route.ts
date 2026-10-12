import { NextRequest, NextResponse } from 'next/server';
import { validateAuth } from '@/lib/api-auth';
import { isAdminUser } from '@/lib/api-helpers';
import { createServiceClient } from '@/lib/supabase/service';
import { getOsnWriteBucket, getOsnWriteClient } from '@/lib/storage';
import { fetchRelease, runExportBudget } from '@/lib/server/dr-exports';

/**
 * POST /api/v1/dr/<tag>/export   (admin; `campfire dr export` loops it)
 *
 * Builds the export files of a frozen data release (lib/server/dr-exports):
 * seeds the dr_exports ledger, then builds pending / failed partitions one
 * after another until the time budget is spent, uploading each gzip JSONL
 * file to dr/<tag>/exports/ on the data bucket and recording sha256 / size /
 * rows in the ledger. Answers what was built, what failed and how many files
 * are still not ready; the caller repeats the call until `remaining` is 0.
 *
 * Body (optional): { budget_seconds?: number }  default 240, max 280 -- under
 * the 300 s function ceiling with room for the last upload.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export const maxDuration = 300;

const DEFAULT_BUDGET_S = 240;
const MAX_BUDGET_S = 280;
const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$/;

export async function POST(request: NextRequest, context: { params: Promise<{ tag: string }> }) {
  const userId = await validateAuth(request);
  if (!userId) {
    return NextResponse.json({ error: 'unauthorized', error_description: 'Valid authentication required' }, { status: 401 });
  }
  if (!(await isAdminUser(userId))) {
    return NextResponse.json({ error: 'forbidden', error_description: 'Admin access required' }, { status: 403 });
  }

  const { tag } = await context.params;
  if (!TAG_RE.test(tag)) {
    return NextResponse.json({ error: 'invalid_tag' }, { status: 400 });
  }

  let budgetS = DEFAULT_BUDGET_S;
  try {
    const body = (await request.json().catch(() => ({}))) as { budget_seconds?: unknown };
    if (typeof body.budget_seconds === 'number' && Number.isFinite(body.budget_seconds)) {
      budgetS = Math.min(MAX_BUDGET_S, Math.max(1, body.budget_seconds));
    }
  } catch {
    // no body: defaults
  }

  try {
    const supabase = createServiceClient();
    const release = await fetchRelease(supabase, tag);
    if (!release) return NextResponse.json({ error: 'not_found' }, { status: 404 });
    if (!['frozen', 'exporting', 'exported'].includes(release.state)) {
      return NextResponse.json(
        { error: 'invalid_state', error_description: `release is ${release.state}; exports need a frozen, unpublished release` },
        { status: 409 },
      );
    }
    const result = await runExportBudget(
      supabase, getOsnWriteClient(), getOsnWriteBucket(), tag, budgetS * 1000,
    );
    return NextResponse.json(result, { headers: { 'Cache-Control': 'private, no-store' } });
  } catch (error) {
    console.error(`Error in POST /api/v1/dr/${tag}/export:`, error);
    const message = error instanceof Error ? error.message : 'export failed';
    return NextResponse.json({ error: 'export_failed', error_description: message }, { status: 500 });
  }
}
