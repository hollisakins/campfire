'use server';

import { createClient } from '@/lib/supabase/server';
import type { NircamExposure } from '@/lib/types';

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
// Reduction progress (aggregated view)
// ---------------------------------------------------------------------------

export interface ReductionProgress {
  field: string;
  filter: string;
  total: number;
  at_uncal: number;
  at_rate: number;
  at_cal: number;
  at_jhat: number;
  at_crf: number;
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
// Filter options (for dropdowns)
// ---------------------------------------------------------------------------

export async function getExposureFilterOptions(): Promise<{
  fields: string[];
  filters: string[];
  stages: string[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('field, filter, stage');

    if (error) {
      return { fields: [], filters: [], stages: [], error: error.message };
    }

    const rows = data || [];
    return {
      fields: [...new Set(rows.map(r => r.field))].sort(),
      filters: [...new Set(rows.map(r => r.filter))].sort(),
      stages: [...new Set(rows.map(r => r.stage))].sort(),
    };
  } catch (err) {
    return {
      fields: [],
      filters: [],
      stages: [],
      error: err instanceof Error ? err.message : 'Failed to fetch filter options',
    };
  }
}
