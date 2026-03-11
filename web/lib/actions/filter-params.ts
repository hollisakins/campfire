/**
 * Shared filter-to-RPC parameter transformation.
 *
 * Every consumer of filtered object data (table, map, CSV download, FITS download,
 * inspection queue, navigation, Python API) MUST use this function to build RPC
 * params. This ensures all consumers apply identical filtering logic and prevents
 * desync when new filters are added.
 */

import type { AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';

/**
 * Typed RPC params shared by all filter-based Supabase RPCs
 * (get_filtered_objects_paginated, get_csv_export, get_adjacent_objects).
 *
 * Consumer-specific params (pagination, sorting, thumbnails) are NOT included —
 * each caller spreads these base params and adds its own.
 */
export interface FilterRpcParams {
  p_program_ids: number[];
  p_filter_programs: number[] | null;
  p_fields: string[] | null;
  p_gratings: string[] | null;
  p_gratings_mode: string;
  p_observations: string[] | null;
  p_redshift_quality: number[] | null;
  p_redshift_min: number | null;
  p_redshift_max: number | null;
  p_max_snr_min: number | null;
  p_max_snr_max: number | null;
  p_max_exposure_time_min: number | null;
  p_max_exposure_time_max: number | null;
  p_spectral_features_include_any: number | null;
  p_spectral_features_include_all: number | null;
  p_spectral_features_exclude: number | null;
  p_object_flags_include_any: number | null;
  p_object_flags_include_all: number | null;
  p_object_flags_exclude: number | null;
  p_dq_flags_include_any: number | null;
  p_dq_flags_include_all: number | null;
  p_dq_flags_exclude: number | null;
  p_search: string | null;
  p_inspected_only: boolean | null;
  p_coord_ra: number | null;
  p_coord_dec: number | null;
  p_radius_degrees: number | null;
  p_comment_search: string | null;
  p_comment_search_scope: string | null;
  p_comment_user_id: string | null;
}

/**
 * Transform UI filter state into RPC parameters.
 *
 * @param filters - Filter state from the UI (may be partial/undefined)
 * @param accessibleProgramIds - Programs the current user can access (resolved by caller)
 * @param userId - Current user's ID (needed for comment search scoping)
 */
export function buildFilterParams(
  filters: Partial<AdvancedFilterOptions> | undefined,
  accessibleProgramIds: number[],
  userId?: string
): FilterRpcParams {
  // Bitmask combining: OR individual flag values into a single mask
  const spectralFeaturesMask = filters?.spectral_features && filters.spectral_features.length > 0
    ? filters.spectral_features.reduce((acc, val) => acc | val, 0)
    : null;

  const objectFlagsMask = filters?.object_flags && filters.object_flags.length > 0
    ? filters.object_flags.reduce((acc, val) => acc | val, 0)
    : null;

  const dqFlagsMask = filters?.dq_flags && filters.dq_flags.length > 0
    ? filters.dq_flags.reduce((acc, val) => acc | val, 0)
    : null;

  // Route bitmask to include_any / include_all / exclude based on mode
  const sfMode = filters?.spectral_features_mode || 'any';
  const ofMode = filters?.object_flags_mode || 'any';
  const dqMode = filters?.dq_flags_mode || 'any';

  // Coordinate search conversion
  let coordRa: number | null = null;
  let coordDec: number | null = null;
  let radiusDegrees: number | null = null;

  if (filters?.coordinate_search) {
    coordRa = filters.coordinate_search.ra;
    coordDec = filters.coordinate_search.dec;
    radiusDegrees = convertRadiusToDegrees(
      filters.coordinate_search.radius,
      filters.coordinate_search.radius_unit
    );
  }

  // Search routing based on scope
  const searchText = filters?.search?.trim() || null;
  const searchScope = filters?.search_scope || 'object_id';
  const isCommentSearch = searchScope === 'my_comments' || searchScope === 'all_comments';

  const objectIdSearch = searchScope === 'object_id' ? searchText : null;
  const commentSearch = isCommentSearch ? searchText : null;
  const commentSearchScope = isCommentSearch
    ? (searchScope === 'my_comments' ? 'just_me' : 'everyone')
    : null;
  const commentUserId = isCommentSearch && userId ? userId : null;

  return {
    p_program_ids: accessibleProgramIds,
    p_filter_programs: filters?.programs && filters.programs.length > 0 ? filters.programs : null,
    p_fields: filters?.fields && filters.fields.length > 0 ? filters.fields : null,
    p_gratings: filters?.gratings && filters.gratings.length > 0 ? filters.gratings : null,
    p_gratings_mode: filters?.gratings_mode || 'any',
    p_observations: filters?.observations && filters.observations.length > 0 ? filters.observations : null,
    p_redshift_quality: filters?.redshift_quality && filters.redshift_quality.length > 0 ? filters.redshift_quality : null,
    p_redshift_min: filters?.redshift_min ?? null,
    p_redshift_max: filters?.redshift_max ?? null,
    p_max_snr_min: filters?.max_snr_min ?? null,
    p_max_snr_max: filters?.max_snr_max ?? null,
    p_max_exposure_time_min: filters?.max_exposure_time_min ?? null,
    p_max_exposure_time_max: filters?.max_exposure_time_max ?? null,
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
    p_inspected_only: filters?.inspected_only ?? null,
    p_coord_ra: coordRa,
    p_coord_dec: coordDec,
    p_radius_degrees: radiusDegrees,
    p_comment_search: commentSearch,
    p_comment_search_scope: commentSearchScope,
    p_comment_user_id: commentUserId,
  };
}
