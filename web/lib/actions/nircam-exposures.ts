'use server';

import { createClient } from '@/lib/supabase/server';
import type { NircamExposure, MaskRegionsPayload } from '@/lib/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function requireAdmin() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) throw new Error('Not authenticated');

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) throw new Error('Admin access required');
  return supabase;
}

// ---------------------------------------------------------------------------
// Read
// ---------------------------------------------------------------------------

export interface ExposuresResult {
  exposures: NircamExposure[];
  error?: string;
}

export async function getNircamExposures(params?: {
  field?: string;
  filter?: string;
  detector?: string;
  reviewStatus?: string;
  stage?: string;
}): Promise<ExposuresResult> {
  try {
    const supabase = await requireAdmin();

    let query = supabase
      .from('nircam_exposures')
      .select('*')
      .order('field')
      .order('filter')
      .order('filename');

    if (params?.field) query = query.eq('field', params.field);
    if (params?.filter) query = query.eq('filter', params.filter);
    if (params?.detector) query = query.eq('detector', params.detector);
    if (params?.reviewStatus) query = query.eq('review_status', params.reviewStatus);
    if (params?.stage) query = query.eq('stage', params.stage);

    const { data, error } = await query;

    if (error) {
      return { exposures: [], error: error.message };
    }

    return { exposures: data || [] };
  } catch (err) {
    return {
      exposures: [],
      error: err instanceof Error ? err.message : 'Failed to fetch exposures',
    };
  }
}

export async function getNircamExposureById(id: number): Promise<{
  exposure: NircamExposure | null;
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('*')
      .eq('id', id)
      .single();

    if (error) {
      return { exposure: null, error: error.message };
    }

    return { exposure: data };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to fetch exposure',
    };
  }
}

// ---------------------------------------------------------------------------
// Update
// ---------------------------------------------------------------------------

export async function updateExposureReview(
  id: number,
  updates: {
    review_status?: 'pending' | 'approved' | 'excluded';
    masking?: 'none' | 'needed' | 'done';
    correction?: 'none' | 'needed' | 'done';
    notes?: string;
  },
): Promise<{ exposure: NircamExposure | null; error?: string }> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .update({
        ...updates,
        updated_at: new Date().toISOString(),
      })
      .eq('id', id)
      .select()
      .single();

    if (error) {
      return { exposure: null, error: error.message };
    }

    return { exposure: data };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to update exposure',
    };
  }
}

// ---------------------------------------------------------------------------
// Mask polygons
// ---------------------------------------------------------------------------

/**
 * Persist the polygon list for a single exposure.
 *
 * Vertices are stored as DS9 ``image`` 1-indexed coords so the same payload
 * round-trips through ``campfire deploy pull-masks`` and ``apply_masks_step``
 * without any further transform. ``masking`` is flipped to ``'done'`` iff
 * at least one polygon is present, mirroring the local-file-exists semantic
 * that the deploy code uses.
 */
export async function saveExposureMaskRegions(
  id: number,
  regions: MaskRegionsPayload,
): Promise<{ exposure: NircamExposure | null; error?: string }> {
  try {
    const supabase = await requireAdmin();
    const hasPolygons = (regions?.polygons?.length ?? 0) > 0;

    const { data, error } = await supabase
      .from('nircam_exposures')
      .update({
        mask_regions: hasPolygons ? regions : null,
        masking: hasPolygons ? 'done' : 'none',
        updated_at: new Date().toISOString(),
      })
      .eq('id', id)
      .select()
      .single();

    if (error) {
      return { exposure: null, error: error.message };
    }
    return { exposure: data };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error
        ? err.message
        : 'Failed to save mask regions',
    };
  }
}

// ---------------------------------------------------------------------------
// Reduction progress (aggregated view)
// ---------------------------------------------------------------------------

export interface ReductionProgress {
  field: string;
  filter: string;
  total: number;
  // Per-step counts (matches the columns in the nircam_reduction_progress view)
  at_uncal: number;
  at_detector1: number;
  at_persistence: number;
  at_wisp: number;
  at_striping: number;
  at_image2: number;
  at_edge: number;
  at_sky: number;
  at_diag_striping: number;
  at_variance: number;
  at_wcs_shift: number;
  at_preview: number;
  at_jhat: number;
  at_apply_mask: number;
  at_bad_pixel: number;
  at_outlier: number;
  pending_review: number;
  approved: number;
  excluded: number;
  needs_masking: number;
  needs_correction: number;
}

export async function getReductionProgress(): Promise<{
  progress: ReductionProgress[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_reduction_progress')
      .select('*')
      .order('field')
      .order('filter');

    if (error) {
      return { progress: [], error: error.message };
    }

    return { progress: data || [] };
  } catch (err) {
    return {
      progress: [],
      error: err instanceof Error ? err.message : 'Failed to fetch progress',
    };
  }
}

// ---------------------------------------------------------------------------
// Excluded exposures (copy-paste source for fields.toml skip=[])
// ---------------------------------------------------------------------------

export interface ExcludedExposure {
  field: string;
  filter: string;
  filename: string;
  notes: string | null;
}

export async function getExcludedExposures(): Promise<{
  excluded: ExcludedExposure[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('field, filter, filename, notes')
      .eq('review_status', 'excluded')
      .order('field')
      .order('filter')
      .order('filename');

    if (error) {
      return { excluded: [], error: error.message };
    }

    return { excluded: data || [] };
  } catch (err) {
    return {
      excluded: [],
      error: err instanceof Error ? err.message : 'Failed to fetch excluded exposures',
    };
  }
}

// ---------------------------------------------------------------------------
// Filter options (for dropdowns)
// ---------------------------------------------------------------------------

export async function getExposureFilterOptions(): Promise<{
  fields: string[];
  filters: string[];
  detectors: string[];
  stages: string[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('field, filter, detector, stage');

    if (error) {
      return { fields: [], filters: [], detectors: [], stages: [], error: error.message };
    }

    const rows = data || [];
    return {
      fields: [...new Set(rows.map(r => r.field))].sort(),
      filters: [...new Set(rows.map(r => r.filter))].sort(),
      detectors: [...new Set(rows.map(r => r.detector))].sort(),
      stages: [...new Set(rows.map(r => r.stage))].sort(),
    };
  } catch (err) {
    return {
      fields: [],
      filters: [],
      detectors: [],
      stages: [],
      error: err instanceof Error ? err.message : 'Failed to fetch filter options',
    };
  }
}
