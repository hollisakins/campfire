import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import {
  SHUTTER_WIDTH_ARCSEC,
  SHUTTER_HEIGHT_ARCSEC,
} from '@/lib/utils/tile-compositing';

/**
 * GET /api/v1/shutters?object_id=<id>&fov=<arcsec>
 * GET /api/v1/shutters?ra=<ra>&dec=<dec>&field=<field>&radius=<arcsec>
 *
 * Returns nearby NIRSpec shutter geometry as JSON for client-side rendering.
 *
 * Query parameters (option A — by object):
 * - object_id (required): Object identifier, used to look up RA/Dec/field
 * - fov (optional, default 5): Search radius in arcseconds
 *
 * Query parameters (option B — by coordinates):
 * - ra (required): Right ascension in degrees
 * - dec (required): Declination in degrees
 * - field (required): Field name (e.g., 'cosmos')
 * - radius (optional, default 5): Search radius in arcseconds
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

    let ra: number;
    let dec: number;
    let field: string;
    let radiusArcsec: number;

    if (objectId) {
      // Look up by object_id
      radiusArcsec = Math.min(30, Math.max(1, parseFloat(params.get('fov') || '5')));

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

      ra = obj.ra;
      dec = obj.dec;
      field = obj.field;
    } else {
      // Look up by coordinates
      const raParam = params.get('ra');
      const decParam = params.get('dec');
      const fieldParam = params.get('field');

      if (!raParam || !decParam || !fieldParam) {
        return NextResponse.json(
          { error: 'Missing required parameters: either object_id, or ra + dec + field' },
          { status: 400 }
        );
      }

      ra = parseFloat(raParam);
      dec = parseFloat(decParam);
      field = fieldParam;
      radiusArcsec = Math.min(30, Math.max(1, parseFloat(params.get('radius') || '5')));
    }

    // Fetch nearby shutters
    const { data: shutterData, error: shutterErr } = await supabase.rpc('get_nearby_shutters', {
      p_ra: ra,
      p_dec: dec,
      p_radius_arcsec: radiusArcsec,
      p_field: field,
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
        center_ra: ra,
        center_dec: dec,
        radius_arcsec: radiusArcsec,
        field,
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
