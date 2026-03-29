'use server';

import { createClient, createServiceClient } from '@/lib/supabase/server';
import { paginateRpc } from '@/lib/supabase/paginate';
import type { SpectrumTarget, Program, Spectrum } from '@/lib/types';
import { buildFilterParams } from './filter-params';
import type { FilterOptions } from './filter-params';
export type { FilterOptions, FilterMode } from './filter-params';

export interface SpectraResult {
  spectra: SpectrumTarget[];
  total: number;
  error?: string;
  isAuthenticated: boolean;
}

export interface PaginatedSpectraResult {
  spectra: SpectrumTarget[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  isComplete: boolean; // true if all matching rows were returned (enables client-side sorting)
  error?: string;
  isAuthenticated: boolean;
}

// Re-export types from separate file (can't define non-async exports in "use server" file)
export type { SortDirection, SortColumn, ViewMode } from './spectra-types';
import type { SortColumn, SortDirection, ViewMode } from './spectra-types';

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
  sortColumn: SortColumn = 'target_id',
  sortDirection: SortDirection = 'asc',
  viewMode: ViewMode = 'targets'
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
      .select('program_slug')
      .eq('user_id', user.id);

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);

    // Get public programs
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('slug')
      .eq('is_public', true);

    const publicProgramSlugs = (publicPrograms || []).map(p => p.slug);

    // Combine accessible program slugs (public + explicit access)
    const accessibleProgramSlugs = [...new Set([...publicProgramSlugs, ...explicitAccessSlugs])];

    if (accessibleProgramSlugs.length === 0) {
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

    const rpcParams = buildFilterParams(filters, accessibleProgramSlugs, user.id);

    // Choose RPC based on view mode
    const rpcName = viewMode === 'spectra'
      ? 'get_filtered_spectra_paginated'
      : viewMode === 'objects'
        ? 'get_filtered_objects_paginated'
        : 'get_filtered_targets_paginated';

    // Build final params for the chosen RPC
    // Objects RPC has a smaller parameter set (no bitmask flags, observations, comments, thumbnails)
    const callParams = viewMode === 'objects'
      ? {
          p_program_slugs: rpcParams.p_program_slugs,
          p_filter_programs: rpcParams.p_filter_programs,
          p_fields: rpcParams.p_fields,
          p_gratings: rpcParams.p_gratings,
          p_gratings_mode: rpcParams.p_gratings_mode,
          p_redshift_quality: rpcParams.p_redshift_quality,
          p_redshift_min: rpcParams.p_redshift_min,
          p_redshift_max: rpcParams.p_redshift_max,
          p_max_snr_min: rpcParams.p_max_snr_min,
          p_max_snr_max: rpcParams.p_max_snr_max,
          p_max_exposure_time_min: rpcParams.p_max_exposure_time_min,
          p_max_exposure_time_max: rpcParams.p_max_exposure_time_max,
          p_search: rpcParams.p_search,
          p_inspected_only: rpcParams.p_inspected_only,
          p_coord_ra: rpcParams.p_coord_ra,
          p_coord_dec: rpcParams.p_coord_dec,
          p_radius_degrees: rpcParams.p_radius_degrees,
          p_sort_column: sortColumn,
          p_sort_direction: sortDirection,
          p_page: page,
          p_page_size: pageSize,
        }
      : {
          ...rpcParams,
          p_sort_column: sortColumn,
          p_sort_direction: sortDirection,
          p_page: page,
          p_page_size: pageSize,
          p_include_thumbnails: true,
        };

    // Call the RPC function for server-side filtering, sorting, and pagination
    const { data, error } = await supabase.rpc(rpcName, callParams);

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

    // The RPC returns a single row with targets array and total_count
    const result = data?.[0] || { targets: [], total_count: 0 };
    const targets = result.targets || [];
    const totalCount = Number(result.total_count) || 0;

    // Transform the JSONB targets to SpectrumTarget format
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const spectraTargets: SpectrumTarget[] = targets.map((obj: any) => {
      if (viewMode === 'objects') {
        // Objects mode: map object fields into SpectrumTarget shape
        return {
          id: obj.id,
          target_id: obj.object_id, // display object_id in the ID column
          field: obj.field,
          ra: obj.ra,
          dec: obj.dec,
          redshift: obj.best_redshift,
          redshift_quality: obj.best_redshift_quality ?? 0,
          distance: obj.distance ?? null,
          max_snr: obj.max_snr ?? undefined,
          max_exposure_time: obj.max_exposure_time ?? undefined,
          created_at: obj.created_at,
          spectra: [],
          // Objects-specific fields
          n_targets: obj.n_targets,
          n_spectra: obj.n_spectra,
          programs: obj.programs,
          gratings: obj.gratings,
          member_targets: obj.member_targets,
          num_gratings: obj.gratings?.length ?? 0,
          // Fields not applicable in objects mode
          program_slug: obj.programs?.[0] ?? '',
          program_name: undefined,
          observation: '',
          redshift_auto: null,
          redshift_inspected: null,
          spectral_features: 0,
          object_flags: 0,
          dq_flags: 0,
          last_inspected_at: null,
          last_inspected_by: null,
          updated_at: '',
        } as unknown as SpectrumTarget;
      }

      const spectra: Spectrum[] = obj.spectra || [];

      return {
        id: obj.id,
        target_id: obj.target_id,
        program_slug: obj.program_slug,
        program_name: obj.program_name || null,
        field: obj.field,
        observation: obj.observation,
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
        max_exposure_time: obj.max_exposure_time ?? undefined,
        num_gratings: spectra.length,
      } as SpectrumTarget;
    });

    // Determine if we have the complete dataset (all matching rows fit in one page)
    const isComplete = totalCount <= pageSize;

    return {
      spectra: spectraTargets,
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
 * Fetch a single target by target_id.
 * Checks that user has access (public program or explicit access).
 */
export async function getSpectrumById(targetId: string): Promise<{
  spectrum: SpectrumTarget | null;
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
      .select('program_slug')
      .eq('user_id', user.id);

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);

    // Get public programs
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('slug')
      .eq('is_public', true);

    const publicProgramSlugs = (publicPrograms || []).map(p => p.slug);

    // Combine accessible program slugs
    const accessibleProgramSlugs = [...new Set([...publicProgramSlugs, ...explicitAccessSlugs])];

    const { data, error } = await supabase
      .from('targets')
      .select(`
        *,
        programs:program_slug (program_name, pi_name, description, cycle),
        spectra (*)
      `)
      .eq('target_id', targetId)
      .in('program_slug', accessibleProgramSlugs)
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

    const spectrumTarget: SpectrumTarget = {
      id: data.id,
      target_id: data.target_id,
      program_slug: data.program_slug,
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
      spectrum: spectrumTarget,
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
 * Fetch minimal target metadata for Open Graph tags (no auth required).
 * Uses service role to bypass RLS since this is called by social media crawlers.
 * This is safe because it only returns basic info (target_id, redshift, program_name, field),
 * not the actual spectrum data or FITS files.
 */
export async function getTargetMetadata(targetId: string): Promise<{
  target_id: string;
  redshift: number | null;
  program_name: string | null;
  field: string;
} | null> {
  try {
    // Use service role client to bypass RLS for social media crawlers
    const supabase = createServiceClient();

    const { data, error } = await supabase
      .from('targets')
      .select(`
        target_id,
        redshift,
        field,
        programs:program_slug (program_name)
      `)
      .eq('target_id', targetId)
      .single();

    if (error || !data) {
      return null;
    }

    // Handle the programs relation - cast through unknown to handle Supabase type inference
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const programData = data.programs as any;

    return {
      target_id: data.target_id,
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
      .select('program_slug')
      .eq('user_id', user.id);

    if (accessError) {
      console.error('Error fetching program access:', accessError);
    }

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);

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
      p => p.is_public || explicitAccessSlugs.includes(p.slug)
    );

    if (accessiblePrograms.length === 0) {
      return {
        programs: [],
        fields: [],
        observations: [],
      };
    }

    // Fetch JWST PIDs from observations table for program sorting
    const { data: obsData } = await supabase
      .from('observations')
      .select('program_slug, jwst_program_id');

    const pidsBySlug: Record<string, number[]> = {};
    for (const obs of (obsData || [])) {
      if (!obs.jwst_program_id) continue;
      if (!pidsBySlug[obs.program_slug]) pidsBySlug[obs.program_slug] = [];
      if (!pidsBySlug[obs.program_slug].includes(obs.jwst_program_id)) {
        pidsBySlug[obs.program_slug].push(obs.jwst_program_id);
      }
    }

    const programsWithPids = accessiblePrograms.map(p => ({
      ...p,
      jwst_pids: pidsBySlug[p.slug]?.sort((a, b) => a - b) || [],
    }));

    // Fetch fields and observations from materialized view (cached, refreshed after deployments)
    const { data: filterData, error: filterError } = await supabase
      .from('mv_filter_options')
      .select('fields, observations')
      .single();

    if (filterError) {
      console.error('Error fetching filter options:', filterError);
      return {
        programs: programsWithPids,
        fields: [],
        observations: [],
        error: filterError.message,
      };
    }

    // Extract fields and observations from materialized view
    const uniqueFields = filterData?.fields || [];
    const uniqueObservations = filterData?.observations || [];

    return {
      programs: programsWithPids,
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
 * Fetch all matching object IDs for the inspection queue.
 * Returns a stable snapshot of IDs that won't change as targets are inspected.
 * If no redshift_quality filter is set, implicitly filters to quality=0 (uninspected).
 */
export async function getInspectionQueueIds(
  filters?: Partial<FilterOptions>,
  sortColumn: SortColumn = 'target_id',
  sortDirection: SortDirection = 'asc'
): Promise<{ ids: string[]; error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { ids: [], error: 'Not authenticated' };
  }

  try {
    // Determine which programs the user can access
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
      return { ids: [] };
    }

    // Apply implicit quality=0 filter when no quality filter is set
    const hasQualityFilter = filters?.redshift_quality && filters.redshift_quality.length > 0;
    const qualityFilter = hasQualityFilter ? filters!.redshift_quality : [0];

    const rpcParams = buildFilterParams(filters, accessibleProgramSlugs, user.id);

    // Call lightweight RPC that returns only object IDs (no JSONB, no spectra joins).
    // Paginate to avoid PostgREST max-rows truncation (5000).
    const { data: allRows, error: rpcError } = await paginateRpc<{ target_id: string }>(
      supabase,
      'get_filtered_target_ids',
      {
        ...rpcParams,
        p_redshift_quality: qualityFilter, // Override: implicit quality=0 for inspection
        p_sort_column: sortColumn,
        p_sort_direction: sortDirection,
      },
    );

    if (rpcError) {
      console.error('Error fetching inspection queue:', rpcError);
      return { ids: [], error: rpcError.message };
    }

    const ids = allRows.map(row => row.target_id);

    return { ids };
  } catch (err) {
    console.error('Unexpected error fetching inspection queue:', err);
    return { ids: [], error: 'An unexpected error occurred' };
  }
}

/**
 * Get adjacent target IDs for navigation on detail page.
 * Uses a lightweight server query optimized for finding just prev/next.
 */
export async function getAdjacentTargetIds(
  currentTargetId: string,
  filters?: Partial<FilterOptions>,
  sortColumn: SortColumn = 'target_id',
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
      .select('program_slug')
      .eq('user_id', user.id);

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);

    // Get public programs
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('slug')
      .eq('is_public', true);

    const publicProgramSlugs = (publicPrograms || []).map(p => p.slug);

    // Combine accessible program slugs (public + explicit access)
    const accessibleProgramSlugs = [...new Set([...publicProgramSlugs, ...explicitAccessSlugs])];

    if (accessibleProgramSlugs.length === 0) {
      return { prev: null, next: null, currentIndex: 0, total: 0 };
    }

    const rpcParams = buildFilterParams(filters, accessibleProgramSlugs, user.id);

    // Call the lightweight RPC function
    const { data, error } = await supabase.rpc('get_adjacent_targets', {
      p_current_target_id: currentTargetId,
      ...rpcParams,
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
      prev: result.prev_target_id || null,
      next: result.next_target_id || null,
      currentIndex: Number(result.current_index) || 0,
      total: Number(result.total_count) || 0,
    };
  } catch (err) {
    console.error('Error in getAdjacentTargetIds:', err);
    return { prev: null, next: null, currentIndex: 0, total: 0 };
  }
}
