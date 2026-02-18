'use server';

import { createClient } from '@/lib/supabase/server';
import type { WCSParams } from '@/lib/utils/wcs';

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
  object_id: string;
  ra: number;
  dec: number;
  redshift: number | null;
  redshift_quality: number;
  field: string;
  program_id: number;
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
 * Fetch objects within a viewport bounding box for map markers.
 */
export async function getMapMarkers(
  raMin: number,
  raMax: number,
  decMin: number,
  decMax: number,
  field?: string,
  limit: number = 5000
): Promise<MapMarkersResult> {
  const supabase = await createClient();

  const { data, error } = await supabase.rpc('get_objects_in_viewport', {
    p_ra_min: raMin,
    p_ra_max: raMax,
    p_dec_min: decMin,
    p_dec_max: decMax,
    p_field: field ?? null,
    p_limit: limit,
  });

  if (error) {
    return { markers: [], error: error.message };
  }

  return { markers: data || [] };
}
