'use server';

import { createClient, createServiceClient } from '@/lib/supabase/server';
import type { SpectrumObject, Program, Spectrum } from '@/lib/types';

export interface FilterOptions {
  // Basic filters
  programs: number[];
  fields: string[];
  gratings: string[];
  observations?: string[];
  redshift_quality: number[];
  // Advanced filters
  coordinate_search?: {
    ra: number;
    dec: number;
    radius: number;
    radius_unit: 'degrees' | 'arcmin' | 'arcsec';
  } | null;
  redshift_min?: number | null;
  redshift_max?: number | null;
  max_snr_min?: number | null;
  max_snr_max?: number | null;
  spectral_features?: number[];
  object_flags?: number[];
  dq_flags?: number[];
  inspected_only?: boolean | null;
  search?: string;
  search_scope?: 'object_id' | 'my_comments' | 'all_comments';
}

export interface SpectraResult {
  spectra: SpectrumObject[];
  total: number;
  error?: string;
  isAuthenticated: boolean;
}

export interface PaginatedSpectraResult {
  spectra: SpectrumObject[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  isComplete: boolean; // true if all matching rows were returned (enables client-side sorting)
  error?: string;
  isAuthenticated: boolean;
}

// Re-export types from separate file (can't define non-async exports in "use server" file)
export type { SortDirection, SortColumn } from './spectra-types';
import type { SortColumn, SortDirection } from './spectra-types';

export interface FilterOptionsResult {
  programs: Program[];
  fields: string[];
  observations: string[];
  error?: string;
}

/**
 * Fetch spectra with optional filters, sorting, and server-side pagination.
 * Returns empty array if user is not authenticated.
 * Filters to programs that are public OR user has explicit access.
 *
 * All filtering (including bitmask filters and grating filters) is done
 * server-side via an RPC function for accurate counts and true pagination.
 *
 * Supports adaptive sorting: when pageSize is large enough to fetch all results,
 * returns isComplete=true so the client can sort locally for better UX.
 */
export async function getSpectra(
  filters?: Partial<FilterOptions>,
  page: number = 1,
  pageSize: number = 50,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
): Promise<PaginatedSpectraResult> {
  const supabase = await createClient();

  // Check if user is authenticated
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      spectra: [],
      total: 0,
      page,
      pageSize,
      totalPages: 0,
      isComplete: true,
      isAuthenticated: false,
    };
  }

  try {
    // First, determine which programs the user can access
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_id')
      .eq('user_id', user.id);

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    // Get public programs
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('program_id')
      .eq('is_public', true);

    const publicProgramIds = (publicPrograms || []).map(p => p.program_id);

    // Combine accessible program IDs (public + explicit access)
    const accessibleProgramIds = [...new Set([...publicProgramIds, ...explicitAccessIds])];

    if (accessibleProgramIds.length === 0) {
      return {
        spectra: [],
        total: 0,
        page,
        pageSize,
        totalPages: 0,
        isComplete: true,
        isAuthenticated: true,
      };
    }

    // Prepare bitmask filters (combine arrays into single mask)
    const spectralFeaturesMask = filters?.spectral_features && filters.spectral_features.length > 0
      ? filters.spectral_features.reduce((acc, val) => acc | val, 0)
      : null;

    const objectFlagsMask = filters?.object_flags && filters.object_flags.length > 0
      ? filters.object_flags.reduce((acc, val) => acc | val, 0)
      : null;

    const dqFlagsMask = filters?.dq_flags && filters.dq_flags.length > 0
      ? filters.dq_flags.reduce((acc, val) => acc | val, 0)
      : null;

    // Convert coordinate search radius to degrees
    let coordRa: number | null = null;
    let coordDec: number | null = null;
    let radiusDegrees: number | null = null;

    if (filters?.coordinate_search) {
      coordRa = filters.coordinate_search.ra;
      coordDec = filters.coordinate_search.dec;
      const { radius, radius_unit } = filters.coordinate_search;

      // Convert radius to degrees based on unit
      radiusDegrees =
        radius_unit === 'degrees' ? radius :
        radius_unit === 'arcmin' ? radius / 60 :
        radius / 3600;  // arcsec
    }

    // Determine search routing based on scope
    const searchText = filters?.search?.trim() || null;
    const searchScope = filters?.search_scope || 'object_id';
    const isCommentSearch = searchScope === 'my_comments' || searchScope === 'all_comments';

    // Only pass object_id search when scope is 'object_id'
    const objectIdSearch = searchScope === 'object_id' ? searchText : null;
    // Only pass comment search when scope is comment-based
    const commentSearch = isCommentSearch ? searchText : null;
    const commentSearchScope = isCommentSearch ? (searchScope === 'my_comments' ? 'just_me' : 'everyone') : null;
    const commentUserId = isCommentSearch ? user.id : null;

    // Call the RPC function for server-side filtering, sorting, and pagination
    const { data, error } = await supabase.rpc('get_filtered_objects_paginated', {
      p_program_ids: accessibleProgramIds,
      p_filter_programs: filters?.programs && filters.programs.length > 0 ? filters.programs : null,
      p_fields: filters?.fields && filters.fields.length > 0 ? filters.fields : null,
      p_gratings: filters?.gratings && filters.gratings.length > 0 ? filters.gratings : null,
      p_observations: filters?.observations && filters.observations.length > 0 ? filters.observations : null,
      p_redshift_quality: filters?.redshift_quality && filters.redshift_quality.length > 0 ? filters.redshift_quality : null,
      p_redshift_min: filters?.redshift_min ?? null,
      p_redshift_max: filters?.redshift_max ?? null,
      p_max_snr_min: filters?.max_snr_min ?? null,
      p_max_snr_max: filters?.max_snr_max ?? null,
      p_spectral_features: spectralFeaturesMask,
      p_object_flags: objectFlagsMask,
      p_dq_flags: dqFlagsMask,
      p_search: objectIdSearch,
      p_inspected_only: filters?.inspected_only ?? null,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_comment_search: commentSearch,
      p_comment_search_scope: commentSearchScope,
      p_comment_user_id: commentUserId,
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
      p_page: page,
      p_page_size: pageSize,
    });

    if (error) {
      console.error('Error fetching spectra:', error);
      return {
        spectra: [],
        total: 0,
        page,
        pageSize,
        totalPages: 0,
        isComplete: true,
        error: error.message,
        isAuthenticated: true,
      };
    }

    // The RPC returns a single row with objects array and total_count
    const result = data?.[0] || { objects: [], total_count: 0 };
    const objects = result.objects || [];
    const totalCount = Number(result.total_count) || 0;

    // Transform the JSONB objects to SpectrumObject format
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const spectraObjects: SpectrumObject[] = objects.map((obj: any) => {
      const spectra: Spectrum[] = obj.spectra || [];

      return {
        id: obj.id,
        object_id: obj.object_id,
        program_id: obj.program_id,
        program_name: obj.program_name || null,
        field: obj.field,
        observation: obj.observation || null,
        ra: obj.ra,
        dec: obj.dec,
        redshift: obj.redshift,
        redshift_auto: obj.redshift_auto,
        redshift_inspected: obj.redshift_inspected,
        redshift_quality: obj.redshift_quality,
        spectral_features: obj.spectral_features,
        object_flags: obj.object_flags,
        dq_flags: obj.dq_flags,
        last_inspected_at: obj.last_inspected_at,
        last_inspected_by: obj.last_inspected_by,
        created_at: obj.created_at,
        updated_at: obj.updated_at,
        distance: obj.distance ?? null,
        spectra: spectra,
        max_snr: obj.max_snr ?? undefined,
        num_gratings: spectra.length,
      } as SpectrumObject;
    });

    // Determine if we have the complete dataset (all matching rows fit in one page)
    const isComplete = totalCount <= pageSize;

    return {
      spectra: spectraObjects,
      total: totalCount,
      page,
      pageSize,
      totalPages: Math.ceil(totalCount / pageSize),
      isComplete,
      isAuthenticated: true,
    };
  } catch (err) {
    console.error('Unexpected error fetching spectra:', err);
    return {
      spectra: [],
      total: 0,
      page,
      pageSize,
      totalPages: 0,
      isComplete: true,
      error: 'An unexpected error occurred',
      isAuthenticated: true,
    };
  }
}

/**
 * Fetch a single spectrum object by object_id.
 * Checks that user has access (public program or explicit access).
 */
export async function getSpectrumById(objectId: string): Promise<{
  spectrum: SpectrumObject | null;
  error?: string;
  isAuthenticated: boolean;
}> {
  const supabase = await createClient();

  // Check if user is authenticated
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      spectrum: null,
      isAuthenticated: false,
    };
  }

  try {
    // First, determine which programs the user can access
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_id')
      .eq('user_id', user.id);

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    // Get public programs
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('program_id')
      .eq('is_public', true);

    const publicProgramIds = (publicPrograms || []).map(p => p.program_id);

    // Combine accessible program IDs
    const accessibleProgramIds = [...new Set([...publicProgramIds, ...explicitAccessIds])];

    const { data, error } = await supabase
      .from('objects')
      .select(`
        *,
        programs:program_id (program_name, pi_name, description),
        spectra (*)
      `)
      .eq('object_id', objectId)
      .in('program_id', accessibleProgramIds)
      .single();

    if (error) {
      if (error.code === 'PGRST116') {
        // No rows returned - either doesn't exist or no access
        return {
          spectrum: null,
          error: 'Spectrum not found or access denied',
          isAuthenticated: true,
        };
      }
      console.error('Error fetching spectrum:', error);
      return {
        spectrum: null,
        error: error.message,
        isAuthenticated: true,
      };
    }

    const spectra: Spectrum[] = data.spectra || [];
    const maxSnr = spectra.length > 0
      ? Math.max(...spectra.map(s => s.signal_to_noise || 0))
      : null;

    // Use has_sed_plot from database (populated during deployment)
    const hasSedPlot = data.has_sed_plot ?? false;

    const spectrumObject: SpectrumObject = {
      id: data.id,
      object_id: data.object_id,
      program_id: data.program_id,
      program_name: data.programs?.program_name || null,
      field: data.field,
      observation: data.observation,
      ra: data.ra,
      dec: data.dec,
      redshift: data.redshift,
      redshift_auto: data.redshift_auto,
      redshift_inspected: data.redshift_inspected,
      redshift_quality: data.redshift_quality,
      spectral_features: data.spectral_features,
      object_flags: data.object_flags,
      dq_flags: data.dq_flags,
      last_inspected_at: data.last_inspected_at,
      last_inspected_by: data.last_inspected_by,
      created_at: data.created_at,
      updated_at: data.updated_at,
      spectra: spectra,
      max_snr: maxSnr ?? undefined,
      num_gratings: spectra.length,
      hasSedPlot,
    };

    return {
      spectrum: spectrumObject,
      isAuthenticated: true,
    };
  } catch (err) {
    console.error('Unexpected error fetching spectrum:', err);
    return {
      spectrum: null,
      error: 'An unexpected error occurred',
      isAuthenticated: true,
    };
  }
}

/**
 * Fetch minimal object metadata for Open Graph tags (no auth required).
 * Uses service role to bypass RLS since this is called by social media crawlers.
 * This is safe because it only returns basic info (object_id, redshift, program_name, field),
 * not the actual spectrum data or FITS files.
 */
export async function getObjectMetadata(objectId: string): Promise<{
  object_id: string;
  redshift: number | null;
  program_name: string | null;
  field: string;
} | null> {
  try {
    // Use service role client to bypass RLS for social media crawlers
    const supabase = createServiceClient();

    const { data, error } = await supabase
      .from('objects')
      .select(`
        object_id,
        redshift,
        field,
        programs:program_id (program_name)
      `)
      .eq('object_id', objectId)
      .single();

    if (error || !data) {
      return null;
    }

    // Handle the programs relation - cast through unknown to handle Supabase type inference
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const programData = data.programs as any;

    return {
      object_id: data.object_id,
      redshift: data.redshift,
      program_name: programData?.program_name || null,
      field: data.field,
    };
  } catch {
    return null;
  }
}

/**
 * Fetch available filter options (programs and fields the user has access to).
 * Includes public programs + programs the user has explicit access to.
 */
export async function getFilterOptions(): Promise<FilterOptionsResult> {
  const supabase = await createClient();

  // Check if user is authenticated
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      programs: [],
      fields: [],
      observations: [],
    };
  }

  try {
    // Fetch programs the user has explicit access to
    const { data: accessData, error: accessError } = await supabase
      .from('user_program_access')
      .select('program_id')
      .eq('user_id', user.id);

    if (accessError) {
      console.error('Error fetching program access:', accessError);
    }

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    // Fetch all programs (we'll filter for public + explicit access)
    const { data: allPrograms, error: programsError } = await supabase
      .from('programs')
      .select('*');

    if (programsError) {
      console.error('Error fetching programs:', programsError);
      return {
        programs: [],
        fields: [],
        observations: [],
        error: programsError.message,
      };
    }

    // Filter to programs that are public OR user has explicit access
    const accessiblePrograms = (allPrograms || []).filter(
      p => p.is_public || explicitAccessIds.includes(p.program_id)
    );

    if (accessiblePrograms.length === 0) {
      return {
        programs: [],
        fields: [],
        observations: [],
      };
    }

    // Fetch fields and observations from materialized view (cached, refreshed after deployments)
    const { data: filterData, error: filterError } = await supabase
      .from('mv_filter_options')
      .select('fields, observations')
      .single();

    if (filterError) {
      console.error('Error fetching filter options:', filterError);
      return {
        programs: accessiblePrograms,
        fields: [],
        observations: [],
        error: filterError.message,
      };
    }

    // Extract fields and observations from materialized view
    const uniqueFields = filterData?.fields || [];
    const uniqueObservations = filterData?.observations || [];

    return {
      programs: accessiblePrograms,
      fields: uniqueFields,
      observations: uniqueObservations,
    };
  } catch (err) {
    console.error('Unexpected error fetching filter options:', err);
    return {
      programs: [],
      fields: [],
      observations: [],
      error: 'An unexpected error occurred',
    };
  }
}

/**
 * Get adjacent object IDs for navigation on detail page.
 * Uses a lightweight server query optimized for finding just prev/next.
 */
export async function getAdjacentObjectIds(
  currentObjectId: string,
  filters?: Partial<FilterOptions>,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
): Promise<{
  prev: string | null;
  next: string | null;
  currentIndex: number;
  total: number;
}> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { prev: null, next: null, currentIndex: 0, total: 0 };
  }

  try {
    // First, determine which programs the user can access
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_id')
      .eq('user_id', user.id);

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    // Get public programs
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('program_id')
      .eq('is_public', true);

    const publicProgramIds = (publicPrograms || []).map(p => p.program_id);

    // Combine accessible program IDs (public + explicit access)
    const accessibleProgramIds = [...new Set([...publicProgramIds, ...explicitAccessIds])];

    if (accessibleProgramIds.length === 0) {
      return { prev: null, next: null, currentIndex: 0, total: 0 };
    }

    // Prepare bitmask filters (combine arrays into single mask)
    const spectralFeaturesMask = filters?.spectral_features && filters.spectral_features.length > 0
      ? filters.spectral_features.reduce((acc, val) => acc | val, 0)
      : null;

    const objectFlagsMask = filters?.object_flags && filters.object_flags.length > 0
      ? filters.object_flags.reduce((acc, val) => acc | val, 0)
      : null;

    const dqFlagsMask = filters?.dq_flags && filters.dq_flags.length > 0
      ? filters.dq_flags.reduce((acc, val) => acc | val, 0)
      : null;

    // Convert coordinate search radius to degrees
    let coordRa: number | null = null;
    let coordDec: number | null = null;
    let radiusDegrees: number | null = null;

    if (filters?.coordinate_search) {
      coordRa = filters.coordinate_search.ra;
      coordDec = filters.coordinate_search.dec;
      const { radius, radius_unit } = filters.coordinate_search;

      radiusDegrees =
        radius_unit === 'degrees' ? radius :
        radius_unit === 'arcmin' ? radius / 60 :
        radius / 3600;  // arcsec
    }

    // Determine search routing based on scope
    const searchText = filters?.search?.trim() || null;
    const searchScope = filters?.search_scope || 'object_id';
    const isCommentSearch = searchScope === 'my_comments' || searchScope === 'all_comments';

    const objectIdSearch = searchScope === 'object_id' ? searchText : null;
    const commentSearch = isCommentSearch ? searchText : null;
    const commentSearchScope = isCommentSearch ? (searchScope === 'my_comments' ? 'just_me' : 'everyone') : null;
    const commentUserId = isCommentSearch ? user.id : null;

    // Call the lightweight RPC function
    const { data, error } = await supabase.rpc('get_adjacent_objects', {
      p_current_object_id: currentObjectId,
      p_program_ids: accessibleProgramIds,
      p_filter_programs: filters?.programs && filters.programs.length > 0 ? filters.programs : null,
      p_fields: filters?.fields && filters.fields.length > 0 ? filters.fields : null,
      p_gratings: filters?.gratings && filters.gratings.length > 0 ? filters.gratings : null,
      p_observations: filters?.observations && filters.observations.length > 0 ? filters.observations : null,
      p_redshift_quality: filters?.redshift_quality && filters.redshift_quality.length > 0 ? filters.redshift_quality : null,
      p_redshift_min: filters?.redshift_min ?? null,
      p_redshift_max: filters?.redshift_max ?? null,
      p_spectral_features: spectralFeaturesMask,
      p_object_flags: objectFlagsMask,
      p_dq_flags: dqFlagsMask,
      p_search: objectIdSearch,
      p_inspected_only: filters?.inspected_only ?? null,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_comment_search: commentSearch,
      p_comment_search_scope: commentSearchScope,
      p_comment_user_id: commentUserId,
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
    });

    if (error) {
      console.error('Error fetching adjacent objects:', error);
      return { prev: null, next: null, currentIndex: 0, total: 0 };
    }

    // RPC returns a single row
    const result = data?.[0];
    if (!result) {
      return { prev: null, next: null, currentIndex: 0, total: 0 };
    }

    return {
      prev: result.prev_object_id || null,
      next: result.next_object_id || null,
      currentIndex: Number(result.current_index) || 0,
      total: Number(result.total_count) || 0,
    };
  } catch (err) {
    console.error('Error in getAdjacentObjectIds:', err);
    return { prev: null, next: null, currentIndex: 0, total: 0 };
  }
}
