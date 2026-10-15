import 'server-only';

import { NextResponse, type NextRequest } from 'next/server';
import type { ApiPrincipal } from '@/lib/api-auth';
import { checkBrowseTag } from '@/lib/dr/confirm';
import { DR_PARAM, parseDrTag } from '@/lib/dr/tag';
import { createServiceClient } from '@/lib/supabase/service';
import { fetchRelease, type DrReleaseRow } from '@/lib/server/dr-exports';

/**
 * The `dr=<tag>` parameter of a bearer `/api/v1` request (P6c-2).
 *
 * The bearer routes run as the service role, for which the database's
 * pre-request hook accepts any post-freeze tag — so the gate is HERE, before
 * the header is ever forwarded: readers see a published release; admins any
 * release past its freeze (the same states the hook admits the service role
 * to: not `draft`, `freezing` or `failed`); link accounts never browse a
 * release (and never reach these routes with a credential anyway). An
 * unknown or invisible tag is a 404 that reads the same as a missing
 * release. No parameter = the live catalog, exactly as before.
 *
 * Once admitted, the mode is CONFIRMED against the database (the same
 * `dr_browse_tag` check the cookie client runs): if the hook is not active
 * the route answers 500, never live rows under a release.
 *
 * By design this gate is independent of `NEXT_PUBLIC_DR_PAGES`: that flag
 * holds the web entry points; API consumers are gated by release visibility
 * only, and a reader can only ever reach a published tag. Middleware neither
 * stamps nor rewrites `/api/v1`, so errors here stay JSON.
 *
 * Only `createServiceClient({ dr })` with the tag this returns may send the
 * header.
 */
export type BearerRelease =
  | { tag: string | null; response?: undefined }
  | { tag?: undefined; response: NextResponse };

const UNBROWSABLE_STATES = new Set(['draft', 'freezing', 'failed']);

/** Readers: published. Admins: every release past its freeze. */
export function bearerReleaseVisible(release: Pick<DrReleaseRow, 'state'>, isAdmin: boolean): boolean {
  if (release.state === 'published') return true;
  return isAdmin && !UNBROWSABLE_STATES.has(release.state);
}

export async function assertBearerReleaseAccess(
  request: NextRequest,
  principal: Pick<ApiPrincipal, 'access'>,
): Promise<BearerRelease> {
  const values = request.nextUrl.searchParams.getAll(DR_PARAM);
  if (values.length === 0) return { tag: null };
  const noStore = { 'Cache-Control': 'private, no-store' };
  const notFound = () => ({ response: NextResponse.json({ error: 'not_found' }, { status: 404, headers: noStore }) });
  if (values.length !== 1) return notFound();
  const tag = parseDrTag(values[0]);
  if (!tag) return notFound();
  if (principal.access.isLinkAccount) return notFound();
  const release = await fetchRelease(createServiceClient(), tag);
  if (!release || !bearerReleaseVisible(release, principal.access.isAdmin)) return notFound();
  // Confirm the database browses it (P6c-1 rule 1 on the bearer path).
  const check = await checkBrowseTag(createServiceClient({ dr: tag }), tag);
  if (!check.ok) {
    if (check.kind === 'not-found') return notFound();
    console.error('bearer release: mode confirmation failed:', check.message);
    return { response: NextResponse.json({ error: 'data release unavailable' }, { status: 500, headers: noStore }) };
  }
  return { tag };
}

/**
 * Routes that are live-only by contract (the sync streams, the release file
 * listing, cutouts, shutters, version, auth, deploy) refuse a `dr` parameter
 * instead of ignoring it, so a client never mistakes their live answer for
 * a release's (rule 4). Returns the 400 to send, or null.
 */
export function rejectReleaseParam(searchParams: URLSearchParams): NextResponse | null {
  if (!searchParams.has(DR_PARAM)) return null;
  return NextResponse.json(
    { error: 'dr not supported', details: 'This endpoint serves the live catalog only; data releases are browsed through the catalog and file endpoints.' },
    { status: 400, headers: { 'Cache-Control': 'private, no-store' } },
  );
}
