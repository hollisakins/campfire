import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';

/**
 * GET /api/photometry-pz?object_id=<object_id>
 *
 * Generates a signed URL for downloading a P(z) JSON sidecar from R2.
 * Requires authentication and checks user access to the object.
 *
 * P(z) sidecars are stored at: photometry/{field}/{object_id}_pz.json
 * The field is derived from the DB record, not from user input.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

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

    // Construct P(z) sidecar path using DB-derived field
    const pzPath = `photometry/${obj.field}/${objectId}_pz.json`;

    // Generate signed URL (expires in 1 hour)
    const signedUrl = await generateDownloadUrl(pzPath, 3600);

    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured.' },
        { status: 503 }
      );
    }

    return NextResponse.json({
      url: signedUrl,
      path: pzPath,
    });
  } catch (error) {
    console.error('Error generating P(z) sidecar URL:', error);
    return NextResponse.json(
      { error: 'Failed to generate P(z) URL' },
      { status: 500 }
    );
  }
}
