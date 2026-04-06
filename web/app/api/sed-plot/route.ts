import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';

/**
 * GET /api/sed-plot?target_id=<target_id>
 *
 * Generates a signed URL for viewing/downloading a SED plot PDF from R2.
 * Requires authentication and checks user access to the target.
 *
 * SED plots are stored at: sed/{observation}/{target_id}_sed.pdf
 * where observation is extracted from the target_id pattern.
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

  // Get the target_id from query parameters
  const searchParams = request.nextUrl.searchParams;
  const targetId = searchParams.get('target_id');

  if (!targetId) {
    return NextResponse.json(
      { error: 'Missing target_id parameter' },
      { status: 400 }
    );
  }

  try {
    // Verify user has access to this target by checking program access
    const { data: target, error: targetError } = await supabase
      .from('targets')
      .select('id, observation, program_slug')
      .eq('target_id', targetId)
      .single();

    if (targetError || !target) {
      return NextResponse.json(
        { error: 'Target not found or access denied' },
        { status: 404 }
      );
    }

    // Extract observation name from target_id if observation column doesn't exist
    // Pattern: {observation}_{srcid} e.g., "ember_cosmos_p1_12345"
    const observation = target.observation || targetId.substring(0, targetId.lastIndexOf('_'));

    // Construct SED plot path in R2
    const sedPlotPath = `sed/${observation}/${targetId}_sed.pdf`;

    // Generate signed URL (expires in 1 hour)
    const signedUrl = await generateDownloadUrl(sedPlotPath, 3600);

    // Check if it's a placeholder URL (R2 not configured)
    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured. Please contact administrator.' },
        { status: 503 }
      );
    }

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
