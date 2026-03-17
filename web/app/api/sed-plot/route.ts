import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';
import { trackDownload } from '@/lib/actions/download-tracking';

/**
 * GET /api/sed-plot?object_id=<object_id>
 *
 * Generates a signed URL for viewing/downloading a SED plot PDF from R2.
 * Requires authentication and checks user access to the object.
 *
 * SED plots are stored at: sed/{observation}/{object_id}_sed.pdf
 * where observation is extracted from the object_id pattern.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  // Get the object_id from query parameters
  const searchParams = request.nextUrl.searchParams;
  const objectId = searchParams.get('object_id');

  if (!objectId) {
    return NextResponse.json(
      { error: 'Missing object_id parameter' },
      { status: 400 }
    );
  }

  try {
    // Verify user has access to this object by checking program access
    const { data: object, error: objectError } = await supabase
      .from('objects')
      .select('id, observation, program_slug')
      .eq('object_id', objectId)
      .single();

    if (objectError || !object) {
      return NextResponse.json(
        { error: 'Object not found or access denied' },
        { status: 404 }
      );
    }

    // Extract observation name from object_id if observation column doesn't exist
    // Pattern: {observation}_{srcid} e.g., "ember_cosmos_p1_12345"
    const observation = object.observation || objectId.substring(0, objectId.lastIndexOf('_'));

    // Construct SED plot path in R2
    const sedPlotPath = `sed/${observation}/${objectId}_sed.pdf`;

    // Generate signed URL (expires in 1 hour)
    const signedUrl = await generateDownloadUrl(sedPlotPath, 3600);

    // Check if it's a placeholder URL (R2 not configured)
    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured. Please contact administrator.' },
        { status: 503 }
      );
    }

    // Track SED plot download (fire-and-forget)
    trackDownload({
      userId: user.id,
      downloadType: 'sed_plot',
      objectIds: [objectId],
      objectCount: 1,
      fileCount: 1,
    });

    return NextResponse.json({
      url: signedUrl,
      path: sedPlotPath
    });
  } catch (error) {
    console.error('Error generating SED plot URL:', error);
    return NextResponse.json(
      { error: 'Failed to generate SED plot URL' },
      { status: 500 }
    );
  }
}
