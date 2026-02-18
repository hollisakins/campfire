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
 * Fetch all objects for a field, paginating to get past Supabase row limits.
 */
export async function getFieldMarkers(
  field: string
): Promise<MapMarkersResult> {
  const supabase = await createClient();
  const PAGE_SIZE = 1000;
  const allMarkers: MapMarker[] = [];
  let offset = 0;

  while (true) {
    const { data, error } = await supabase
      .from('objects')
      .select('object_id, ra, dec, redshift, redshift_quality, field, program_id')
      .eq('field', field)
      .range(offset, offset + PAGE_SIZE - 1);

    if (error) {
      return { markers: allMarkers, error: error.message };
    }

    if (!data || data.length === 0) break;
    allMarkers.push(...data);
    if (data.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
  }

  return { markers: allMarkers };
}
