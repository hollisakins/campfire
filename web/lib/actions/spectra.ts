'use server';

import { createClient } from '@/lib/supabase/server';
import type { SpectrumObject, Program, Spectrum } from '@/lib/types';
import { sedPlotExists } from '@/lib/r2';

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
  sortDirection: SortDirection = 'asc',
  signal?: AbortSignal
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

    // Call the RPC function for server-side filtering, sorting, and pagination
    let query = supabase.rpc('get_filtered_objects_paginated', {
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
      p_search: filters?.search?.trim() || null,
      p_inspected_only: filters?.inspected_only ?? null,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
      p_page: page,
      p_page_size: pageSize,
    });

    // Add abort signal if provided
    if (signal) {
      query = query.abortSignal(signal);
    }

    const { data, error } = await query;

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

    // Check if SED plot exists for this object
    const hasSedPlot = await sedPlotExists(data.object_id);

    const spectrumObject: SpectrumObject = {
      id: data.id,
      object_id: data.object_id,
      program_id: data.program_id,
      program_name: data.programs?.program_name || null,
      field: data.field,
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

    const accessibleProgramIds = accessiblePrograms.map(p => p.program_id);

    // Fetch unique fields and observations from objects the user can access
    const { data: objectsData, error: objectsError } = await supabase
      .from('objects')
      .select('field, observation')
      .in('program_id', accessibleProgramIds);

    if (objectsError) {
      console.error('Error fetching fields:', objectsError);
      return {
        programs: accessiblePrograms,
        fields: [],
        observations: [],
        error: objectsError.message,
      };
    }

    // Get unique fields and observations
    const uniqueFields = [...new Set((objectsData || []).map(o => o.field))].sort();
    const uniqueObservations = [...new Set(
      (objectsData || [])
        .map(o => o.observation)
        .filter((obs): obs is string => obs !== null && obs !== undefined)
    )].sort();

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
 * Get adjacent object IDs for pagination on detail page.
 */
export async function getAdjacentObjects(
  currentObjectId: string,
  filters?: Partial<FilterOptions>,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
): Promise<{
  previous: string | null;
  next: string | null;
  currentIndex: number;
  total: number;
}> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { previous: null, next: null, currentIndex: 0, total: 0 };
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
      return { previous: null, next: null, currentIndex: 0, total: 0 };
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

    // Call the same RPC function but fetch all results (large page size)
    // to get the complete filtered and sorted object list
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
      p_search: filters?.search?.trim() || null,
      p_inspected_only: filters?.inspected_only ?? null,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
      p_page: 1,
      p_page_size: 1000000, // Large number to get all results
    });

    if (error || !data) {
      console.error('Error fetching adjacent objects:', error);
      return { previous: null, next: null, currentIndex: 0, total: 0 };
    }

    // The RPC returns a single row with objects array and total_count
    const result = data?.[0] || { objects: [], total_count: 0 };
    const objects = result.objects || [];
    const totalCount = Number(result.total_count) || 0;

    // Extract object_ids from the result
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const objectIds = objects.map((obj: any) => obj.object_id);

    // Find current object's index in the filtered list
    const currentIndex = objectIds.indexOf(currentObjectId);

    if (currentIndex === -1) {
      // Current object not in filtered set
      return { previous: null, next: null, currentIndex: 0, total: totalCount };
    }

    return {
      previous: currentIndex > 0 ? objectIds[currentIndex - 1] : null,
      next: currentIndex < objectIds.length - 1 ? objectIds[currentIndex + 1] : null,
      currentIndex: currentIndex + 1, // 1-indexed for display
      total: totalCount,
    };
  } catch (err) {
    console.error('Error in getAdjacentObjects:', err);
    return { previous: null, next: null, currentIndex: 0, total: 0 };
  }
}
