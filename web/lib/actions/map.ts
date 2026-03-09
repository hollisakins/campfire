'use server';

import { createClient } from '@/lib/supabase/server';
import type { WCSParams } from '@/lib/utils/wcs';
import type { AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';

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
      .select('object_id, ra, dec, redshift, redshift_quality, field, program_id, observation')
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

/**
 * Fetch all slit regions for a field, paginating to get past Supabase row limits.
 */
export async function getFieldSlits(
  field: string
): Promise<SlitRegionsResult> {
  const supabase = await createClient();
  const PAGE_SIZE = 1000;
  const allSlits: SlitRegion[] = [];
  let offset = 0;

  while (true) {
    const { data, error } = await supabase
      .from('slit_regions')
      .select('center_ra, center_dec, position_angle, object_id, observation, shutter_idx')
      .eq('field', field)
      .range(offset, offset + PAGE_SIZE - 1);

    if (error) {
      return { slits: allSlits, error: error.message };
    }

    if (!data || data.length === 0) break;
    allSlits.push(...data);
    if (data.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
  }

  return { slits: allSlits };
}

/**
 * Fetch object IDs matching the given filters (for map marker filtering).
 * Reuses the same RPC function as the spectra table but only extracts IDs.
 */
export async function getFilteredObjectIds(
  filters: AdvancedFilterOptions
): Promise<{ objectIds: string[]; error?: string }> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { objectIds: [], error: 'Not authenticated' };
  }

  try {
    // Determine accessible programs (same as getSpectra)
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_id')
      .eq('user_id', user.id);

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('program_id')
      .eq('is_public', true);

    const publicProgramIds = (publicPrograms || []).map(p => p.program_id);
    const accessibleProgramIds = [...new Set([...publicProgramIds, ...explicitAccessIds])];

    if (accessibleProgramIds.length === 0) {
      return { objectIds: [] };
    }

    // Prepare bitmask filters
    const spectralFeaturesMask = filters.spectral_features?.length
      ? filters.spectral_features.reduce((acc, val) => acc | val, 0)
      : null;
    const objectFlagsMask = filters.object_flags?.length
      ? filters.object_flags.reduce((acc, val) => acc | val, 0)
      : null;
    const dqFlagsMask = filters.dq_flags?.length
      ? filters.dq_flags.reduce((acc, val) => acc | val, 0)
      : null;

    const sfMode = filters.spectral_features_mode || 'any';
    const ofMode = filters.object_flags_mode || 'any';
    const dqMode = filters.dq_flags_mode || 'any';

    // Coordinate search
    let coordRa: number | null = null;
    let coordDec: number | null = null;
    let radiusDegrees: number | null = null;
    if (filters.coordinate_search) {
      coordRa = filters.coordinate_search.ra;
      coordDec = filters.coordinate_search.dec;
      radiusDegrees = convertRadiusToDegrees(
        filters.coordinate_search.radius,
        filters.coordinate_search.radius_unit
      );
    }

    // Search
    const searchText = filters.search?.trim() || null;
    const searchScope = filters.search_scope || 'object_id';
    const isCommentSearch = searchScope === 'my_comments' || searchScope === 'all_comments';
    const objectIdSearch = searchScope === 'object_id' ? searchText : null;
    const commentSearch = isCommentSearch ? searchText : null;
    const commentSearchScope = isCommentSearch ? (searchScope === 'my_comments' ? 'just_me' : 'everyone') : null;
    const commentUserId = isCommentSearch ? user.id : null;

    // Use a very large page size to get all matching IDs in one call
    const { data, error } = await supabase.rpc('get_filtered_objects_paginated', {
      p_program_ids: accessibleProgramIds,
      p_filter_programs: filters.programs?.length ? filters.programs : null,
      p_fields: filters.fields?.length ? filters.fields : null,
      p_gratings: filters.gratings?.length ? filters.gratings : null,
      p_gratings_mode: filters.gratings_mode || 'any',
      p_observations: filters.observations?.length ? filters.observations : null,
      p_redshift_quality: filters.redshift_quality?.length ? filters.redshift_quality : null,
      p_redshift_min: filters.redshift_min ?? null,
      p_redshift_max: filters.redshift_max ?? null,
      p_max_snr_min: filters.max_snr_min ?? null,
      p_max_snr_max: filters.max_snr_max ?? null,
      p_max_exposure_time_min: filters.max_exposure_time_min ?? null,
      p_max_exposure_time_max: filters.max_exposure_time_max ?? null,
      p_spectral_features_include_any: sfMode === 'any' ? spectralFeaturesMask : null,
      p_spectral_features_include_all: sfMode === 'all' ? spectralFeaturesMask : null,
      p_spectral_features_exclude: sfMode === 'none' ? spectralFeaturesMask : null,
      p_object_flags_include_any: ofMode === 'any' ? objectFlagsMask : null,
      p_object_flags_include_all: ofMode === 'all' ? objectFlagsMask : null,
      p_object_flags_exclude: ofMode === 'none' ? objectFlagsMask : null,
      p_dq_flags_include_any: dqMode === 'any' ? dqFlagsMask : null,
      p_dq_flags_include_all: dqMode === 'all' ? dqFlagsMask : null,
      p_dq_flags_exclude: dqMode === 'none' ? dqFlagsMask : null,
      p_search: objectIdSearch,
      p_inspected_only: filters.inspected_only ?? null,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_comment_search: commentSearch,
      p_comment_search_scope: commentSearchScope,
      p_comment_user_id: commentUserId,
      p_sort_column: 'object_id',
      p_sort_direction: 'asc',
      p_page: 1,
      p_page_size: 100000, // Get all matching IDs
      p_include_thumbnails: false,
    });

    if (error) {
      console.error('Error fetching filtered object IDs:', error);
      return { objectIds: [], error: error.message };
    }

    const result = data?.[0] || { objects: [], total_count: 0 };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const objectIds = (result.objects || []).map((obj: any) => obj.object_id as string);

    return { objectIds };
  } catch (err) {
    console.error('Unexpected error fetching filtered object IDs:', err);
    return { objectIds: [], error: 'An unexpected error occurred' };
  }
}

/**
 * Fetch nearby shutters using the get_nearby_shutters RPC.
 * Used by the spectra detail page TileCutout component.
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
  const PAGE_SIZE = 1000;
  const allShutters: Shutter[] = [];
  let offset = 0;

  while (true) {
    const { data, error } = await supabase
      .from('shutters')
      .select('object_id, source_id, center_ra, center_dec, position_angle, shutter_idx, dither_id, shutter_state, observation')
      .eq('field', field)
      .range(offset, offset + PAGE_SIZE - 1);

    if (error) {
      return { shutters: allShutters, error: error.message };
    }

    if (!data || data.length === 0) break;
    allShutters.push(...(data as Shutter[]));
    if (data.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
  }

  return { shutters: allShutters };
}
