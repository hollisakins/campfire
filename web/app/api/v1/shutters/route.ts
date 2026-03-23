import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import {
  SHUTTER_WIDTH_ARCSEC,
  SHUTTER_HEIGHT_ARCSEC,
} from '@/lib/utils/shutter-overlay';

/**
 * GET /api/v1/shutters?object_id=<id>&fov=<arcsec>
 *
 * Returns nearby NIRSpec shutter geometry as JSON for client-side rendering.
 * Looks up the object's position and field, then queries for nearby shutters.
 *
 * Query parameters:
 * - object_id (required): Object identifier
 * - fov (optional, default 5): Search radius in arcseconds, clamped to 1–30
 */
export async function GET(request: NextRequest) {
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing API key' },
      { status: 401 }
    );
  }

  try {
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Access control
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);
    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json(
        { error: 'No accessible programs' },
        { status: 403 }
      );
    }

    const params = request.nextUrl.searchParams;
    const objectId = params.get('object_id');

    if (!objectId) {
      return NextResponse.json(
        { error: 'Missing required parameter: object_id' },
        { status: 400 }
      );
    }

    const parsedFov = parseFloat(params.get('fov') || '5');
    if (!Number.isFinite(parsedFov)) {
      return NextResponse.json(
        { error: 'Invalid parameter: fov must be a finite number' },
        { status: 400 }
      );
    }
    const radiusArcsec = Math.min(30, Math.max(1, parsedFov));

    // Look up object
    const { data: obj, error: objErr } = await supabase
      .from('objects')
      .select('ra, dec, field, program_slug')
      .eq('object_id', objectId)
      .single();

    if (objErr || !obj) {
      return NextResponse.json(
        { error: 'Object not found' },
        { status: 404 }
      );
    }

    if (!accessibleProgramSlugs.includes(obj.program_slug)) {
      return NextResponse.json(
        { error: 'Access denied to this object' },
        { status: 403 }
      );
    }

    // Fetch nearby shutters
    const { data: shutterData, error: shutterErr } = await supabase.rpc('get_nearby_shutters', {
      p_ra: obj.ra,
      p_dec: obj.dec,
      p_radius_arcsec: radiusArcsec,
      p_field: obj.field,
    });

    if (shutterErr) {
      console.error('Error fetching shutters:', shutterErr);
      return NextResponse.json(
        { error: 'Failed to fetch shutter data' },
        { status: 500 }
      );
    }

    return NextResponse.json({
      shutters: shutterData || [],
      meta: {
        shutter_width_arcsec: SHUTTER_WIDTH_ARCSEC,
        shutter_height_arcsec: SHUTTER_HEIGHT_ARCSEC,
        center_ra: obj.ra,
        center_dec: obj.dec,
        radius_arcsec: radiusArcsec,
        field: obj.field,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/shutters:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
