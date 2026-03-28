'use server';

import { createClient } from '@/lib/supabase/server';
import { paginateRpc, paginateQuery } from '@/lib/supabase/paginate';
import type { WCSParams } from '@/lib/utils/wcs';
import type { FilterOptions } from './filter-params';
import { buildFilterParams } from './filter-params';

// ============================================
// Types
// ============================================

export interface MapLayer {
  id: number;
  field: string;
  filter: string;
  tile_base_url: string;
  min_zoom: number;
  max_zoom: number;
  tile_size: number;
  ra_min: number;
  ra_max: number;
  dec_min: number;
  dec_max: number;
  wcs_params: WCSParams;
  image_width: number;
  image_height: number;
  total_tiles: number | null;
  total_size_bytes: number | null;
  tile_version: number;
  is_default: boolean;
  created_at: string;
}

export interface MapMarker {
  target_id: string;
  ra: number;
  dec: number;
  redshift: number | null;
  redshift_quality: number;
  field: string;
  program_slug: string;
  observation: string | null;
}

export interface MapLayersResult {
  layers: MapLayer[];
  error?: string;
  isAuthenticated: boolean;
}

export interface MapMarkersResult {
  markers: MapMarker[];
  error?: string;
}

export interface SlitRegion {
  center_ra: number;
  center_dec: number;
  position_angle: number;
  object_id: string;
  observation: string;
  shutter_idx: number;
}

export interface SlitRegionsResult {
  slits: SlitRegion[];
  error?: string;
}

export interface Shutter {
  object_id: string;
  source_id: number;
  center_ra: number;
  center_dec: number;
  position_angle: number;
  shutter_idx: number;
  dither_id: number;
  shutter_state: 'source' | 'open' | 'stuck_closed';
  observation: string;
}

export interface ShuttersResult {
  shutters: Shutter[];
  error?: string;
}

// ============================================
// Server Actions
// ============================================

/**
 * Fetch all map layers, optionally filtered by field.
 */
export async function getMapLayers(
  field?: string
): Promise<MapLayersResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { layers: [], isAuthenticated: false };
  }

  let query = supabase
    .from('map_layers')
    .select('*')
    .order('field')
    .order('filter');

  if (field) {
    query = query.eq('field', field);
  }

  const { data, error } = await query;

  if (error) {
    return { layers: [], error: error.message, isAuthenticated: true };
  }

  return { layers: data || [], isAuthenticated: true };
}

/**
 * Fetch all targets for a field, paginating to get past Supabase row limits.
 */
export async function getFieldMarkers(
  field: string
): Promise<MapMarkersResult> {
  const supabase = await createClient();

  const { data, error } = await paginateQuery<MapMarker>(
    () => supabase
      .from('targets')
      .select('target_id, ra, dec, redshift, redshift_quality, field, program_slug, observation')
      .eq('field', field)
      .order('target_id'),
    1000,
  );

  if (error) {
    return { markers: data, error: error.message };
  }

  return { markers: data };
}

/**
 * Fetch all slit regions for a field, paginating to get past Supabase row limits.
 */
export async function getFieldSlits(
  field: string
): Promise<SlitRegionsResult> {
  const supabase = await createClient();

  const { data, error } = await paginateQuery<SlitRegion>(
    () => supabase
      .from('slit_regions')
      .select('center_ra, center_dec, position_angle, object_id, observation, shutter_idx')
      .eq('field', field)
      .order('object_id'),
    1000,
  );

  if (error) {
    return { slits: data, error: error.message };
  }

  return { slits: data };
}

/**
 * Fetch object IDs matching the given filters (for map marker filtering).
 * Reuses the same RPC function as the spectra table but only extracts IDs.
 */
export async function getFilteredTargetIds(
  filters: FilterOptions
): Promise<{ targetIds: string[]; error?: string }> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { targetIds: [], error: 'Not authenticated' };
  }

  try {
    // Determine accessible programs (same as getSpectra)
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_slug')
      .eq('user_id', user.id);

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);

    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('slug')
      .eq('is_public', true);

    const publicProgramSlugs = (publicPrograms || []).map(p => p.slug);
    const accessibleProgramSlugs = [...new Set([...publicProgramSlugs, ...explicitAccessSlugs])];

    if (accessibleProgramSlugs.length === 0) {
      return { targetIds: [] };
    }

    const rpcParams = buildFilterParams(filters, accessibleProgramSlugs, user.id);

    const fullRpcParams = {
      ...rpcParams,
      p_sort_column: 'target_id',
      p_sort_direction: 'asc',
    };

    const { data: allRows, error: rpcError } = await paginateRpc<{ target_id: string }>(
      supabase, 'get_filtered_target_ids', fullRpcParams,
    );

    if (rpcError) {
      console.error('Error fetching filtered target IDs:', rpcError);
      return { targetIds: [], error: rpcError.message };
    }

    const targetIds = allRows.map(row => row.target_id);

    return { targetIds };
  } catch (err) {
    console.error('Unexpected error fetching filtered target IDs:', err);
    return { targetIds: [], error: 'An unexpected error occurred' };
  }
}

/**
 * Fetch nearby shutters using the get_nearby_shutters RPC.
 * Used by the tile-thumbnail API route for shutter overlays.
 */
export async function getNearbyShutters(
  ra: number,
  dec: number,
  field: string,
  radiusArcsec: number = 5.0,
): Promise<ShuttersResult> {
  const supabase = await createClient();

  const { data, error } = await supabase.rpc('get_nearby_shutters', {
    p_ra: ra,
    p_dec: dec,
    p_radius_arcsec: radiusArcsec,
    p_field: field,
  });

  if (error) {
    return { shutters: [], error: error.message };
  }

  return { shutters: (data || []) as Shutter[] };
}

/**
 * Fetch all shutters for a field, paginating to get past Supabase row limits.
 * Used by the full map viewer.
 */
export async function getFieldShutters(
  field: string
): Promise<ShuttersResult> {
  const supabase = await createClient();

  const { data, error } = await paginateQuery<Shutter>(
    () => supabase
      .from('shutters')
      .select('object_id, source_id, center_ra, center_dec, position_angle, shutter_idx, dither_id, shutter_state, observation')
      .eq('field', field)
      .order('object_id'),
    1000,
  );

  if (error) {
    return { shutters: data, error: error.message };
  }

  return { shutters: data };
}
