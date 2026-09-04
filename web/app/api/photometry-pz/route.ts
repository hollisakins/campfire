import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { storageKey } from '@/lib/layout';
import { frontUrlFor } from '@/lib/server/cdn-front';
import { streamSidecar } from '@/lib/server/sidecar-stream';

/**
 * GET /api/photometry-pz?object_id=<object_id>
 *
 * Serves an object's P(z) JSON sidecar: a 302 to its delivery-front url when
 * the front is configured, else the bytes streamed through. Requires
 * authentication and checks user access to the object.
 *
 * P(z) sidecars are stored at: photometry/{field}/{object_id}_pz.json
 * The field is derived from the DB record, not from user input.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  const searchParams = request.nextUrl.searchParams;
  const objectId = searchParams.get('object_id');

  if (!objectId) {
    return NextResponse.json(
      { error: 'Missing object_id parameter' },
      { status: 400 }
    );
  }

  try {
    // Verify user has access and get field from DB
    const { data: obj, error: objError } = await supabase
      .from('objects')
      .select('id, programs, field')
      .eq('object_id', objectId)
      .single();

    if (objError || !obj) {
      return NextResponse.json(
        { error: 'Object not found' },
        { status: 404 }
      );
    }

    // Construct P(z) sidecar key via the shared layout contract (DB-derived field)
    const pzPath = storageKey('photometry_pz', { field: obj.field, object_id: objectId });

    // Delivery front first (perf T2-D2, #508): a content-addressed url the
    // browser follows straight to the Worker (CORS `*`, edge-cached per
    // content hash). The 302 itself may sit in the browser cache for an
    // hour — front urls are stable for at least one 6 h presign window.
    const frontUrl = await frontUrlFor(pzPath);
    if (frontUrl) {
      return NextResponse.redirect(frontUrl, {
        status: 302,
        headers: { 'Cache-Control': 'private, max-age=3600', Vary: 'Cookie' },
      });
    }

    // Fallback: stream the sidecar through untouched. ~0.5 MB re-sent on
    // every object-page view before this (#497). Program-scoped → private.
    // One day, not a week: the URL is keyed by object_id with no version
    // token, and a photometry re-deploy overwrites the sidecar in place, so
    // a longer lifetime would pin stale P(z).
    const sidecar = await streamSidecar(pzPath, 'private, max-age=86400');
    if (sidecar.status === 'ok') return sidecar.response;
    return NextResponse.json(
      { error: 'P(z) sidecar not found in storage' },
      { status: sidecar.status === 'missing' ? 404 : 502 }
    );
  } catch (error) {
    console.error('Error generating P(z) sidecar URL:', error);
    return NextResponse.json(
      { error: 'Failed to generate P(z) URL' },
      { status: 500 }
    );
  }
}
