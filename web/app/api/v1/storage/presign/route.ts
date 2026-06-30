import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { generateDownloadUrls } from '@/lib/r2';
import { isKnownKey } from '@/lib/layout';

const MAX_KEYS = 200;
const URL_TTL_SECONDS = 21600; // 6 hours, matches the observation manifest

/**
 * POST /api/v1/storage/presign
 *
 * The single presign primitive for the client's storage_objects download
 * engine. The client computes its download plan locally against the mirror,
 * then asks for signed URLs for the exact keys it intends to fetch.
 *
 * Body: { keys: string[] }  (max 200/batch)
 * Returns: { urls: { [key]: signedUrl } }  — only for keys the caller may
 * download. Each key is first allowlisted via the layout contract (is_known_key)
 * and then authorized by filter_accessible_storage_keys (same program/publish
 * scope as the sync RPC). Unauthorized or unknown keys are omitted, never erroring
 * the whole batch.
 */
export async function POST(request: NextRequest) {
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }

  try {
    const body = await request.json().catch(() => null);
    const rawKeys: unknown = body?.keys;
    if (!Array.isArray(rawKeys) || rawKeys.length === 0) {
      return NextResponse.json(
        { error: 'Body must include a non-empty "keys" array' },
        { status: 400 }
      );
    }
    if (rawKeys.length > MAX_KEYS) {
      return NextResponse.json(
        { error: `Too many keys (max ${MAX_KEYS} per request)` },
        { status: 400 }
      );
    }

    // De-dupe + drop keys that don't parse to a known cloud product (defense in
    // depth: presign only ever mints URLs for layout-recognized keys).
    const requested = Array.from(
      new Set(rawKeys.filter((k): k is string => typeof k === 'string' && isKnownKey(k)))
    );
    if (requested.length === 0) {
      return NextResponse.json({ urls: {} });
    }

    const accessibleProgramSlugs = await getAccessiblePrograms(userId);
    const admin = await isAdminUser(userId);

    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    // Authorize each key against the caller's scope (admins → all active rows).
    const { data: allowedRows, error } = await supabase.rpc('filter_accessible_storage_keys', {
      p_keys: requested,
      p_program_slugs: accessibleProgramSlugs,
      p_include_unpublished: admin,
    });

    if (error) {
      console.error('Error authorizing presign keys:', error);
      return NextResponse.json(
        { error: 'Failed to authorize keys', details: error.message },
        { status: 500 }
      );
    }

    const allowed: string[] = (allowedRows || []).map(
      (r: { storage_key: string }) => r.storage_key
    );

    // Batched dual-read presign: one registry lookup resolves every key's backend.
    const urls: Record<string, string> = {};
    const signedUrls = await generateDownloadUrls(allowed, URL_TTL_SECONDS);
    allowed.forEach((key, i) => {
      urls[key] = signedUrls[i];
    });

    return NextResponse.json({ urls });
  } catch (error) {
    console.error('Error in API /v1/storage/presign:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
