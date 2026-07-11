// Shared request handling for the science cutout routes (epic #337, Phase 5):
// `/api/v1/cutout/fits` and `/api/v1/cutout/figure`. Both are coordinate+field
// addressed (unlike the object_id display route), accept EITHER a Bearer
// API-key/JWT or the browser's cookie session (so the cutout GUI hits the same
// canonical endpoints), and resolve bands through `resolveFieldScienceSource`.

import { NextRequest, NextResponse } from 'next/server';
import { validateAuth } from '@/lib/api-auth';
import { createClient } from '@/lib/supabase/server';

/** Authenticated user id via Bearer token (API key / JWT) or cookie session. */
export async function resolveRequestUser(request: NextRequest): Promise<string | null> {
  if (request.headers.get('authorization')) {
    return validateAuth(request);
  }
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  return user?.id ?? null;
}

export interface ScienceParams {
  field: string;
  ra: number;
  dec: number;
  fovArcsec: number;
  /** Requested band subset (lower-cased), or undefined for all. */
  bands?: string[];
  /** Output pixel scale (arcsec/px) → level selection; undefined for native. */
  targetScaleArcsec?: number;
}

/** Parse + validate the shared params; returns a 400 response on failure. */
export function parseScienceParams(request: NextRequest): ScienceParams | NextResponse {
  const params = request.nextUrl.searchParams;
  const bad = (error: string) => NextResponse.json({ error }, { status: 400 });

  const field = params.get('field');
  if (!field) return bad('Missing required parameter: field');

  const ra = parseFloat(params.get('ra') ?? '');
  const dec = parseFloat(params.get('dec') ?? '');
  if (!Number.isFinite(ra) || !Number.isFinite(dec)) {
    return bad('Missing/invalid parameters: ra and dec must be finite degrees');
  }
  if (ra < 0 || ra >= 360 || dec < -90 || dec > 90) {
    return bad('Invalid coordinates: ra in [0, 360), dec in [-90, 90]');
  }

  const parsedFov = parseFloat(params.get('fov') || '10');
  if (!Number.isFinite(parsedFov)) return bad('Invalid parameter: fov must be a finite number');
  const fovArcsec = Math.min(600, Math.max(0.5, parsedFov));

  let bands: string[] | undefined;
  const bandsParam = params.get('bands');
  if (bandsParam !== null) {
    bands = bandsParam.split(',').map((b) => b.trim().toLowerCase()).filter(Boolean);
    if (bands.length === 0) return bad('Invalid parameter: bands must be a comma-separated list');
  }

  let targetScaleArcsec: number | undefined;
  const scaleParam = params.get('scale');
  if (scaleParam !== null) {
    targetScaleArcsec = parseFloat(scaleParam);
    if (!Number.isFinite(targetScaleArcsec) || targetScaleArcsec <= 0) {
      return bad('Invalid parameter: scale must be a positive number (arcsec/px)');
    }
  }

  return { field, ra, dec, fovArcsec, bands, targetScaleArcsec };
}

/** Filesystem/header-safe token for Content-Disposition filenames. */
export function safeToken(s: string): string {
  return s.replace(/[^a-zA-Z0-9_.-]+/g, '-');
}
