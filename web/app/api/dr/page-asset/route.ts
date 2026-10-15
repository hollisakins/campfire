import { NextRequest, NextResponse } from 'next/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { drPagesAccess, drPagesMode } from '@/lib/dr-pages';
import { parseDrTag } from '@/lib/dr/tag';
import { listReleaseMembers } from '@/lib/server/dr-files';
import { pageAssetName } from '@/lib/server/dr-releases';
import { getBucketNameForBackend, getS3ClientForBackend } from '@/lib/storage';

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const URL_TTL_SECONDS = 3600;

/**
 * GET /api/dr/page-asset?tag=<tag>&name=<file>
 *
 * A figure of a release's page document (P6c-2). Authorizes through the
 * viewer's `dr_manifest` rows (component `page`: readable by anyone who can
 * see the release) and 302s to a presigned url of the archived object under
 * `dr/<tag>/page/<sha16>/<name>`. The redirect is private and short-lived;
 * the object itself is immutable (content-addressed key).
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  const access = drPagesAccess(drPagesMode(), {
    signedIn: principal !== null,
    isAdmin: principal?.access.isAdmin ?? false,
    isLinkAccount: principal?.access.isLinkAccount ?? false,
  });
  if (!principal || access !== 'show') {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
  }
  const tag = parseDrTag(request.nextUrl.searchParams.get('tag'));
  const name = request.nextUrl.searchParams.get('name') ?? '';
  if (!tag || !NAME_RE.test(name)) {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
  }
  try {
    const members = await listReleaseMembers(principal.supabase, tag, { component: 'page' });
    const member = members.find((m) => pageAssetName(m.storageKey) === name);
    if (!member) return NextResponse.json({ error: 'Not found' }, { status: 404 });
    const url = await getSignedUrl(
      getS3ClientForBackend(member.backend),
      new GetObjectCommand({ Bucket: getBucketNameForBackend(member.backend), Key: member.archiveKey }),
      { expiresIn: URL_TTL_SECONDS },
    );
    return NextResponse.redirect(url, {
      status: 302,
      headers: { 'Cache-Control': 'private, max-age=300', Vary: 'Cookie' },
    });
  } catch (err) {
    console.error('page asset error:', err);
    return NextResponse.json({ error: 'Failed to resolve the asset' }, { status: 500 });
  }
}
