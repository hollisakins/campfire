import 'server-only';

import { NextResponse, type NextRequest } from 'next/server';
import type { ApiPrincipal } from '@/lib/api-auth';
import { DR_PARAM, parseDrTag } from '@/lib/dr/tag';
import { createServiceClient } from '@/lib/supabase/service';
import { fetchRelease, releaseVisible } from '@/lib/server/dr-exports';

/**
 * The `dr=<tag>` parameter of a bearer `/api/v1` request (P6c-2).
 *
 * The bearer routes run as the service role, for which the database's
 * pre-request hook accepts any post-freeze tag — so the gate is HERE, before
 * the header is ever forwarded: a release is visible once published, or in
 * any state to admins; link accounts never browse a release (and never reach
 * these routes with a credential anyway). An unknown or invisible tag is a
 * 404 that reads the same as a missing release. No parameter = the live
 * catalog, exactly as before.
 *
 * Only `createServiceClient({ dr })` with the tag this returns may send the
 * header.
 */
export type BearerRelease =
  | { tag: string | null; response?: undefined }
  | { tag?: undefined; response: NextResponse };

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
  if (!release || !releaseVisible(release, principal.access.isAdmin)) return notFound();
  return { tag };
}
