'use server';

import { createServiceClient } from '@/lib/supabase/server';
import { getAccessContext } from '@/lib/auth/access-context';
import { getRequestIdentity } from '@/lib/auth/identity';
import { paginateRpc } from '@/lib/supabase/paginate';
import type { SpectrumTarget, Spectrum, ObjectDetail, ObjectMemberTarget, PinnedObjectMetadata } from '@/lib/types';
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
  viewMode: ViewMode = 'objects',
  options: {
    /**
     * Ask the RPC for the exact total (a second full pass over the filter).
     * The list hook passes false once it knows the count for a filter set;
     * the result then carries total = -1 / totalPages = -1 (#501).
     */
    includeCount?: boolean;
  } = {}
): Promise<PaginatedSpectraResult> {
  const includeCount = options.includeCount ?? true;
  const { user, supabase } = await getRequestIdentity();

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
    // Which programs can this user access? One RPC to the SQL authority
    // (accessible_program_slugs) rather than a hand-rolled grants + public
    // union: the union is wrong for link accounts (scoped program only, no
    // is_public) and admins (every program). See web/lib/auth/access-context.ts.
    const accessibleProgramSlugs = (await getAccessContext(user.id)).accessibleSlugs;

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

    const rpcName = viewMode === 'spectra'
      ? 'get_filtered_spectra_paginated'
      : 'get_filtered_objects_paginated';

    // Build final params for the chosen RPC
    // Objects RPC has a smaller parameter set (no bitmask flags, thumbnails)
    let callParams: Record<string, unknown>;
    if (viewMode === 'objects') {
      callParams = {
        p_program_slugs: rpcParams.p_program_slugs,
        p_filter_programs: rpcParams.p_filter_programs,
        p_fields: rpcParams.p_fields,
        p_gratings: rpcParams.p_gratings,
        p_gratings_mode: rpcParams.p_gratings_mode,
        p_observations: rpcParams.p_observations,
        p_redshift_quality: rpcParams.p_redshift_quality,
        p_redshift_min: rpcParams.p_redshift_min,
        p_redshift_max: rpcParams.p_redshift_max,
        p_max_snr_min: rpcParams.p_max_snr_min,
        p_max_snr_max: rpcParams.p_max_snr_max,
        p_max_exposure_time_min: rpcParams.p_max_exposure_time_min,
        p_max_exposure_time_max: rpcParams.p_max_exposure_time_max,
        p_search: rpcParams.p_search,
        p_inspected_only: rpcParams.p_inspected_only,
        p_needs_review: rpcParams.p_needs_review,
        p_has_photometry: rpcParams.p_has_photometry,
        p_list_ids: rpcParams.p_list_ids,
        p_list_ids_mode: rpcParams.p_list_ids_mode,
        p_coord_ra: rpcParams.p_coord_ra,
        p_coord_dec: rpcParams.p_coord_dec,
        p_radius_degrees: rpcParams.p_radius_degrees,
        p_comment_search: rpcParams.p_comment_search,
        p_comment_search_scope: rpcParams.p_comment_search_scope,
        p_comment_user_id: rpcParams.p_comment_user_id,
        p_sort_column: sortColumn,
        p_sort_direction: sortDirection,
        p_page: page,
        p_page_size: pageSize,
        p_include_count: includeCount,
      };
    } else {
      callParams = {
        ...rpcParams,
        p_sort_column: sortColumn,
        p_sort_direction: sortDirection,
        p_page: page,
        p_page_size: pageSize,
        p_include_thumbnails: true,
        p_include_count: includeCount,
      };
    }

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
    // -1 = count skipped (p_include_count false); the caller fills it in.
    const rawCount = Number(result.total_count);
    const totalCount = Number.isFinite(rawCount) ? rawCount : 0;

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
          redshift: obj.redshift,
          redshift_quality: obj.redshift_quality ?? 0,
          redshift_inspected: obj.redshift_inspected ?? null,
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
          photo_z: obj.photo_z ?? null,
          has_photometry: obj.has_photometry ?? false,
          member_targets: obj.member_targets,
          lists: obj.lists,
          num_gratings: obj.gratings?.length ?? 0,
          last_inspected_at: obj.last_inspected_at ?? null,
          last_inspected_by: obj.last_inspected_by ?? null,
          last_data_change_at: obj.last_data_change_at ?? null,
          staleness_reason: obj.staleness_reason ?? null,
          // Fields not applicable in objects mode
          program_slug: obj.programs?.[0] ?? '',
          program_name: undefined,
          observation: '',
          redshift_auto: obj.redshift_auto ?? null,
          dq_flags: 0,
          updated_at: '',
        } as unknown as SpectrumTarget;
      }

      const spectra: Spectrum[] = obj.spectra || [];

      return {
        id: obj.id,
        target_id: obj.target_id,
        parent_object_id: obj.parent_object_id ?? undefined,
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
    const countKnown = totalCount >= 0;
    const isComplete = countKnown && totalCount <= pageSize;

    return {
      spectra: spectraTargets,
      total: countKnown ? totalCount : -1,
      page,
      pageSize,
      totalPages: countKnown ? Math.ceil(totalCount / pageSize) : -1,
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
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return {
      spectrum: null,
      isAuthenticated: false,
    };
  }

  try {
    // Which programs can this user access? One RPC to the SQL authority
    // (accessible_program_slugs) rather than a hand-rolled grants + public
    // union: the union is wrong for link accounts (scoped program only, no
    // is_public) and admins (every program). See web/lib/auth/access-context.ts.
    const accessibleProgramSlugs = (await getAccessContext(user.id)).accessibleSlugs;

    const { data, error } = await supabase
      .from('targets')
      .select(`
        *,
        programs:program_slug (program_name, pi_name, description, cycle),
        spectra (id, spectrum_id, target_id, grating, fits_path, cfpipe_version, signal_to_noise, exposure_time, created_at, updated_at, redshift_auto, chi2_min, confidence, dq_flags, deploy_status),
        parent_object:object_id (object_id)
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
      dq_flags: data.dq_flags,
      last_inspected_at: data.last_inspected_at,
      last_inspected_by: data.last_inspected_by,
      created_at: data.created_at,
      updated_at: data.updated_at,
      spectra: spectra,
      max_snr: maxSnr ?? undefined,
      num_gratings: spectra.length,
      parent_object_id: data.parent_object?.object_id ?? undefined,
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

    // Service-role read with no auth (serves OG/social metadata). Gate on
    // has_published_spectrum so draft targets return null. No-op in B1.
    const { data, error } = await supabase
      .from('targets')
      .select(`
        target_id,
        redshift,
        field,
        programs:program_slug (program_name)
      `)
      .eq('target_id', targetId)
      .eq('has_published_spectrum', true)
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
 * Fetch a single object by object_id with full member targets and their spectra.
 * Checks that user has access (at least one member program is accessible).
 */
export async function getObjectById(objectId: string): Promise<{
  object: ObjectDetail | null;
  error?: string;
  isAuthenticated: boolean;
}> {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return { object: null, isAuthenticated: false };
  }

  try {
    // Fetch the accessible-slug list (SQL authority — see
    // web/lib/auth/access-context.ts) and the object row in parallel.
    const [accessibleProgramSlugs, { data: obj, error: objError }] = await Promise.all([
      getAccessContext(user.id).then(a => a.accessibleSlugs),
      supabase.from('objects').select('*').eq('object_id', objectId).single(),
    ]);

    if (objError || !obj) {
      return {
        object: null,
        error: objError?.code === 'PGRST116' ? 'Object not found' : objError?.message,
        isAuthenticated: true,
      };
    }

    // Check access: object programs must overlap with accessible programs
    const objPrograms: string[] = obj.programs || [];
    const hasAccess = objPrograms.some(p => accessibleProgramSlugs.includes(p));
    if (!hasAccess) {
      return {
        object: null,
        error: 'Object not found or access denied',
        isAuthenticated: true,
      };
    }

    // Fetch member targets and photometry in parallel. Columns are enumerated:
    // `spectra (*)` dragged the two pre-rendered thumbnail SVGs (~1.5 kB each,
    // 84 % of the row's bytes) through detoast → wire → RSC payload for every
    // spectrum, and nothing on this page renders them (#500).
    const [{ data: members, error: membersError }, { data: photData }] = await Promise.all([
      supabase
        .from('targets')
        .select(`
          *,
          programs:program_slug (program_name),
          spectra (id, spectrum_id, target_id, grating, fits_path, cfpipe_version, signal_to_noise, exposure_time, created_at, updated_at, redshift_auto, chi2_min, confidence, dq_flags, deploy_status)
        `)
        .eq('object_id', obj.id)
        .in('program_slug', accessibleProgramSlugs),
      supabase
        .from('object_photometry')
        .select('catalog_name, catalog_id, match_distance_arcsec, photometry, photo_z, photo_z_err_lo, photo_z_err_hi, has_pz')
        .eq('object_id', obj.id)
        .limit(1)
        .maybeSingle(),
    ]);

    if (membersError) {
      return {
        object: null,
        error: membersError.message,
        isAuthenticated: true,
      };
    }

    // Member targets are stateless provenance — inspection lives on the parent object.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const memberTargets: ObjectMemberTarget[] = (members || []).map((m: any) => ({
      id: m.id,
      target_id: m.target_id,
      program_slug: m.program_slug,
      program_name: m.programs?.program_name || m.program_slug,
      observation: m.observation,
      ra: m.ra,
      dec: m.dec,
      redshift_auto: m.redshift_auto,
      max_snr: m.max_snr,
      max_exposure_time: m.max_exposure_time,
      spectra: m.spectra || [],
    })).sort((a: ObjectMemberTarget, b: ObjectMemberTarget) =>
      (b.max_snr || 0) - (a.max_snr || 0)
    );

    // Display-only access scoping. The objects row stores aggregate columns
    // (programs, gratings, counts, max_snr/exposure) computed across ALL member
    // programs at deploy time. This object is visible because the viewer can
    // access at least one member program (checked above), but the stored
    // aggregates would leak metadata about proprietary members they cannot
    // access. Recompute them from the already access-filtered member targets
    // (members are fetched with .in('program_slug', accessibleProgramSlugs)).
    // Mirrors the SQL helper object_scoped_aggregates() and the deploy-time
    // builder in python/campfire/deploy/objects.py. Object-level science
    // (redshift, photometry) intentionally stays visible.
    const scopedSpectra = memberTargets.flatMap(m => m.spectra || []);
    const scopedSnr = scopedSpectra.map(s => s.signal_to_noise).filter((v): v is number => v != null);
    const scopedExp = scopedSpectra.map(s => s.exposure_time).filter((v): v is number => v != null);
    const scoped = {
      n_targets: memberTargets.length,
      n_spectra: scopedSpectra.length,
      programs: [...new Set(memberTargets.map(m => m.program_slug))].sort(),
      gratings: [...new Set(scopedSpectra.map(s => s.grating).filter(Boolean))].sort(),
      max_snr: scopedSnr.length ? Math.max(...scopedSnr) : null,
      max_exposure_time: scopedExp.length ? Math.max(...scopedExp) : null,
    };

    const objectDetail: ObjectDetail = {
      id: obj.id,
      object_id: obj.object_id,
      field: obj.field,
      ra: obj.ra,
      dec: obj.dec,
      n_targets: scoped.n_targets,
      n_spectra: scoped.n_spectra,
      programs: scoped.programs,
      gratings: scoped.gratings,
      max_snr: scoped.max_snr,
      max_exposure_time: scoped.max_exposure_time,
      redshift: obj.redshift ?? null,
      redshift_quality: obj.redshift_quality ?? 0,
      redshift_inspected: obj.redshift_inspected ?? null,
      redshift_auto: obj.redshift_auto ?? null,
      inspected_used_auto: obj.inspected_used_auto ?? false,
      last_inspected_at: obj.last_inspected_at ?? null,
      last_inspected_by: obj.last_inspected_by ?? null,
      last_data_change_at: obj.last_data_change_at ?? null,
      staleness_reason: obj.staleness_reason ?? null,
      version: obj.version ?? 1,
      is_active: obj.is_active ?? true,
      photo_z: obj.photo_z ?? null,
      photo_z_err_lo: obj.photo_z_err_lo ?? null,
      photo_z_err_hi: obj.photo_z_err_hi ?? null,
      has_photometry: obj.has_photometry ?? false,
      created_at: obj.created_at,
      member_targets: memberTargets,
      photometry: photData ? {
        catalog_name: photData.catalog_name,
        catalog_id: photData.catalog_id,
        match_distance_arcsec: photData.match_distance_arcsec,
        photometry: photData.photometry,
        photo_z: photData.photo_z,
        photo_z_err_lo: photData.photo_z_err_lo,
        photo_z_err_hi: photData.photo_z_err_hi,
        has_pz: photData.has_pz ?? false,
      } : null,
    };

    return { object: objectDetail, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching object:', err);
    return {
      object: null,
      error: 'An unexpected error occurred',
      isAuthenticated: true,
    };
  }
}

/**
 * Fetch minimal object metadata for Open Graph tags (no auth required).
 * Uses service role to bypass RLS.
 */
export async function getObjectMetadata(objectId: string): Promise<{
  object_id: string;
  redshift: number | null;
  field: string;
} | null> {
  try {
    const supabase = createServiceClient();

    // Service-role read with no auth (serves OG/social metadata). Gate on
    // has_published_spectrum so draft objects return null. No-op in B1.
    const { data, error } = await supabase
      .from('objects')
      .select('object_id, redshift, field')
      .eq('object_id', objectId)
      .eq('has_published_spectrum', true)
      .single();

    if (error || !data) {
      return null;
    }

    return {
      object_id: data.object_id,
      redshift: data.redshift,
      field: data.field,
    };
  } catch {
    return null;
  }
}

/**
 * Resolve display metadata (field, redshift, quality) for the user's pinned
 * objects. Pins are stored in user_profiles.preferences as bare references
 * (id + route) because that column is readable by all authenticated users;
 * this action resolves the metadata under the caller's own RLS scope, so
 * pins pointing at programs the caller can't access simply don't resolve.
 */
export async function getPinnedObjectsMetadata(
  pins: { target_id: string; route: 'objects' | 'targets' }[]
): Promise<{ metadata: Record<string, PinnedObjectMetadata>; isAuthenticated: boolean }> {
  const { user, supabase } = await getRequestIdentity();
  if (!user) {
    return { metadata: {}, isAuthenticated: false };
  }

  // Defensive cap — the UI limits pins to MAX_PINNED_OBJECTS, but the action
  // shouldn't trust its input size.
  const bounded = pins.slice(0, 50);
  const objectIds = bounded.filter(p => p.route === 'objects').map(p => p.target_id);
  const targetIds = bounded.filter(p => p.route === 'targets').map(p => p.target_id);

  const metadata: Record<string, PinnedObjectMetadata> = {};

  try {
    const [objectsRes, targetsRes] = await Promise.all([
      objectIds.length > 0
        ? supabase
            .from('objects')
            .select('object_id, field, redshift, redshift_quality')
            .in('object_id', objectIds)
        : Promise.resolve({ data: [] as { object_id: string; field: string; redshift: number | null; redshift_quality: number }[], error: null }),
      targetIds.length > 0
        ? supabase
            .from('targets')
            .select('target_id, field, redshift, redshift_quality')
            .in('target_id', targetIds)
        : Promise.resolve({ data: [] as { target_id: string; field: string; redshift: number | null; redshift_quality: number }[], error: null }),
    ]);

    for (const row of objectsRes.data || []) {
      metadata[row.object_id] = {
        field: row.field,
        redshift: row.redshift,
        redshift_quality: row.redshift_quality,
      };
    }
    for (const row of targetsRes.data || []) {
      metadata[row.target_id] = {
        field: row.field,
        redshift: row.redshift,
        redshift_quality: row.redshift_quality,
      };
    }

    return { metadata, isAuthenticated: true };
  } catch {
    return { metadata: {}, isAuthenticated: true };
  }
}

/**
 * Fetch all matching object IDs (IAU names) for the inspection queue.
 * Returns a stable snapshot ordered by the requested sort column (defaulting
 * to object_id ascending) so inspection mode steps through targets in the
 * same order as the table view it was launched from.
 * If no redshift_quality filter is set, implicitly filters to quality=0 (uninspected).
 *
 * Backed by `get_filtered_object_ids`; feature/DQ filters aren't supported at
 * this lightweight tier (they require per-spectrum joins). Observation filtering
 * is supported via `p_observations` (the objects table carries an aggregated
 * `observations` array), so the inspection queue stays scoped to the same
 * observation filter as the table view it was launched from.
 */
export async function getInspectionQueueIds(
  filters?: Partial<FilterOptions>,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc',
): Promise<{ ids: string[]; error?: string }> {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return { ids: [], error: 'Not authenticated' };
  }

  try {
    // Which programs can this user access? One RPC to the SQL authority
    // (accessible_program_slugs) rather than a hand-rolled grants + public
    // union: the union is wrong for link accounts (scoped program only, no
    // is_public) and admins (every program). See web/lib/auth/access-context.ts.
    const accessibleProgramSlugs = (await getAccessContext(user.id)).accessibleSlugs;

    if (accessibleProgramSlugs.length === 0) {
      return { ids: [] };
    }

    const hasQualityFilter = filters?.redshift_quality && filters.redshift_quality.length > 0;
    const qualityFilter = hasQualityFilter ? filters!.redshift_quality : [0];

    const baseRpcParams = buildFilterParams(filters, accessibleProgramSlugs, user.id);

    // Strip params not accepted by get_filtered_object_ids (per-spectrum DQ
    // filters). p_observations IS accepted and must pass through so the queue
    // respects the observation filter.
    /* eslint-disable @typescript-eslint/no-unused-vars */
    const {
      p_dq_flags_include_any: _dqa,
      p_dq_flags_include_all: _dqb,
      p_dq_flags_exclude: _dqc,
      ...objectParams
    } = baseRpcParams;
    /* eslint-enable @typescript-eslint/no-unused-vars */

    const { data: allRows, error: rpcError } = await paginateRpc<{ object_id: string }>(
      supabase,
      'get_filtered_object_ids',
      {
        ...objectParams,
        p_redshift_quality: qualityFilter,
        p_sort_column: sortColumn,
        p_sort_direction: sortDirection,
      },
    );

    if (rpcError) {
      console.error('Error fetching inspection queue:', rpcError);
      return { ids: [], error: rpcError.message };
    }

    const ids = allRows.map(row => row.object_id);
    return { ids };
  } catch (err) {
    console.error('Unexpected error fetching inspection queue:', err);
    return { ids: [], error: 'An unexpected error occurred' };
  }
}
