import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { authenticateStorageDownloadRequest } from '@/lib/api-auth';
import { generateDownloadUrl } from '@/lib/r2';
import { isKnownKey } from '@/lib/layout';

// Long enough that one multi-GB mosaic on a slow link finishes before the
// signature it started under lapses (the store checks the signature when the
// request arrives, not while the body streams). The NIRCam bulk script asks
// for a fresh url per file, so the bulk download as a whole never depends on
// this window — that was the point of the route.
const URL_TTL_SECONDS = 21600; // 6 hours

/**
 * GET /api/v1/storage/download?key=<storage key>[&redirect=false]
 *
 * One-key download primitive for long-running scripted downloads: the
 * generated NIRCam bulk-download script (components/nircam/CurlScriptGenerator)
 * calls this once per file, at the moment it fetches that file, instead of
 * carrying presigned urls that expire ~6 h after the script was generated —
 * a whole-field download can run longer than that.
 *
 * Credentials: an API key or access token like every /api/v1 route, and —
 * only here — the download token a generated script carries
 * (authenticateStorageDownloadRequest; lib/auth/tokens.ts explains why the
 * script does not embed the account's API key). A request with a credential
 * but no key answers 400, which the script uses as its up-front credential
 * check (401 = rejected, anything else = accepted) without touching a file.
 *
 * Authorizes exactly like POST /api/v1/storage/presign (layout allowlist via
 * isKnownKey, then filter_accessible_storage_keys under the caller's program
 * scope; admins see unpublished rows too), then answers with a 302 to a fresh
 * presigned url on the object's home backend, so `curl -L` downloads the
 * bytes directly from the store. curl drops the Authorization header when it
 * follows a redirect to another host, which is what the store needs: a
 * presigned url must arrive without a second credential on the request.
 *
 * `redirect=false` returns `{ url, expires_in }` instead, for callers that
 * want the url itself (mirrors GET /api/v1/spectra).
 *
 * A key the caller may not read and a key that does not exist both answer
 * 404 — the presign batch route omits them silently for the same reason (no
 * oracle for which keys exist outside the caller's scope).
 */
export async function GET(request: NextRequest) {
  const principal = await authenticateStorageDownloadRequest(request);
  if (!principal) {
    return NextResponse.json({ error: 'Invalid or missing authentication' }, { status: 401 });
  }

  const key = request.nextUrl.searchParams.get('key');
  if (!key || !isKnownKey(key)) {
    return NextResponse.json(
      { error: 'Query parameter "key" must be a storage key of a known product' },
      { status: 400 },
    );
  }

  try {
    const supabase = createServiceClient();
    const { data: allowedRows, error } = await supabase.rpc('filter_accessible_storage_keys', {
      p_keys: [key],
      p_program_slugs: principal.access.accessibleSlugs,
      p_include_unpublished: principal.access.isAdmin,
    });
    if (error) {
      console.error('Error authorizing storage download key:', error);
      return NextResponse.json({ error: 'Failed to authorize key' }, { status: 500 });
    }
    if (!allowedRows || allowedRows.length === 0) {
      return NextResponse.json({ error: 'Not found or not accessible' }, { status: 404 });
    }

    const url = await generateDownloadUrl(key, URL_TTL_SECONDS);

    // The url is a bearer credential for the bytes: never let a shared cache
    // hand it to the next caller.
    const headers = { 'Cache-Control': 'no-store' };
    if (request.nextUrl.searchParams.get('redirect') === 'false') {
      return NextResponse.json({ url, expires_in: URL_TTL_SECONDS }, { headers });
    }
    return NextResponse.redirect(url, { status: 302, headers });
  } catch (err) {
    console.error('Error in API /v1/storage/download:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
