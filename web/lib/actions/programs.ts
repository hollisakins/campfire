'use server';

import { createClient } from '@/lib/supabase/server';
import type { Pointing } from '@/lib/types';

export interface ProgramOverview {
  slug: string;
  program_name: string | null;
  pi_name: string | null;
  description: string | null;
  is_public: boolean;
  cycle: number | null;
  target_count: number;
  gratings: string[];
  fields: string[];
  observations: string[];
  jwst_pids: number[];
  n_observations: number;
  last_reduced_at: string | null;
}

// Provenance fields shared between ObservationStat and ObservationOverview.
// Source: most recent deployments row WHERE source_ids_filter IS NULL
// (a "full" reduction). Patch deployments contribute only to n_patches_since_full.
export interface ObservationProvenance {
  reduction_version: string | null;
  crds_context: string | null;
  cfpipe_version: string | null;
  jwst_version: string | null;
  reduced_at: string | null;
  deployed_at: string | null;
  n_patches_since_full: number;
  last_patch_at: string | null;
}

export interface ObservationStat extends ObservationProvenance {
  observation: string;
  program_slug: string;
  program_name: string;
  field: string;
  target_count: number;
  spectrum_count: number;
  total_size_bytes: number;
  pointings: Pointing[] | null;
}

export interface ObservationOverview extends ObservationProvenance {
  observation: string;
  program_slug: string;
  program_name: string | null;
  field: string;
  cycle: number | null;
  gratings: string[];
  pointing_count: number;
  pointings: Pointing[] | null;
  target_count: number;
  spectrum_count: number;
  total_size_bytes: number;
}

export interface DatabaseOverview {
  n_programs: number;
  n_observations: number;
  n_pointings: number;
  n_targets: number;
  n_spectra: number;
  total_size_bytes: number;
  latest_deployed_at: string | null;
  latest_reduction_version: string | null;
}

export interface ProgramsOverviewResult {
  programs: ProgramOverview[];
  error?: string;
  isAuthenticated: boolean;
}

export interface ProgramDetailResult {
  program: ProgramOverview | null;
  observations: ObservationStat[];
  error?: string;
  isAuthenticated: boolean;
}

export interface ObservationsOverviewResult {
  observations: ObservationOverview[];
  error?: string;
  isAuthenticated: boolean;
}

export interface DatabaseOverviewResult {
  overview: DatabaseOverview | null;
  error?: string;
  isAuthenticated: boolean;
}

export async function getProgramsOverview(): Promise<ProgramsOverviewResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { programs: [], isAuthenticated: false };
  }

  try {
    // Fetch programs and user access in parallel
    const [rpcResult, accessResult] = await Promise.all([
      supabase.rpc('get_programs_overview'),
      supabase.from('user_program_access').select('program_slug').eq('user_id', user.id),
    ]);

    const { data, error } = rpcResult;

    if (error) {
      console.error('Error fetching programs overview:', error);
      return { programs: [], error: error.message, isAuthenticated: true };
    }

    const explicitAccessSlugs = new Set((accessResult.data || []).map(a => a.program_slug));

    const programs: ProgramOverview[] = (data || [])
      .filter((p: ProgramOverview) => p.is_public || explicitAccessSlugs.has(p.slug))
      .map((p: ProgramOverview) => ({
        slug: p.slug,
        program_name: p.program_name,
        pi_name: p.pi_name,
        description: p.description,
        is_public: p.is_public,
        cycle: p.cycle ?? null,
        target_count: Number(p.target_count) || 0,
        gratings: p.gratings || [],
        fields: p.fields || [],
        observations: p.observations || [],
        jwst_pids: p.jwst_pids || [],
        n_observations: Number(p.n_observations) || 0,
        last_reduced_at: p.last_reduced_at ?? null,
      }));

    return { programs, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching programs:', err);
    return { programs: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

export async function getProgramDetail(programSlug: string): Promise<ProgramDetailResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { program: null, observations: [], isAuthenticated: false };
  }

  try {
    // Fetch program overview, observation stats, and user access in parallel
    const [overviewResult, obsResult, accessResult] = await Promise.all([
      supabase.rpc('get_programs_overview'),
      supabase.rpc('get_observation_stats', { p_program_slugs: [programSlug] }),
      supabase.from('user_program_access').select('program_slug').eq('user_id', user.id),
    ]);

    if (overviewResult.error) {
      console.error('Error fetching program detail:', overviewResult.error);
      return { program: null, observations: [], error: overviewResult.error.message, isAuthenticated: true };
    }

    const explicitAccessSlugs = new Set((accessResult.data || []).map(a => a.program_slug));

    // Find the specific program
    const programData = (overviewResult.data || []).find(
      (p: ProgramOverview) => p.slug === programSlug
    );

    if (!programData) {
      return { program: null, observations: [], error: 'Program not found', isAuthenticated: true };
    }

    // Check access
    if (!programData.is_public && !explicitAccessSlugs.has(programSlug)) {
      return { program: null, observations: [], error: 'Access denied', isAuthenticated: true };
    }

    const program: ProgramOverview = {
      slug: programData.slug,
      program_name: programData.program_name,
      pi_name: programData.pi_name,
      description: programData.description,
      is_public: programData.is_public,
      cycle: programData.cycle ?? null,
      target_count: Number(programData.target_count) || 0,
      gratings: programData.gratings || [],
      fields: programData.fields || [],
      observations: programData.observations || [],
      jwst_pids: programData.jwst_pids || [],
      n_observations: Number(programData.n_observations) || 0,
      last_reduced_at: programData.last_reduced_at ?? null,
    };

    const observations: ObservationStat[] = (obsResult.data || []).map(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (o: any) => ({
        observation: o.observation,
        program_slug: o.program_slug,
        program_name: o.program_name,
        field: o.field,
        target_count: Number(o.target_count) || 0,
        spectrum_count: Number(o.spectrum_count) || 0,
        total_size_bytes: Number(o.total_size_bytes) || 0,
        pointings: (o.pointings as Pointing[] | null) ?? null,
        reduction_version: o.reduction_version ?? null,
        crds_context: o.crds_context ?? null,
        cfpipe_version: o.cfpipe_version ?? null,
        jwst_version: o.jwst_version ?? null,
        reduced_at: o.reduced_at ?? null,
        deployed_at: o.deployed_at ?? null,
        n_patches_since_full: Number(o.n_patches_since_full) || 0,
        last_patch_at: o.last_patch_at ?? null,
      })
    );

    return { program, observations, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching program detail:', err);
    return { program: null, observations: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

export async function getObservationsOverview(): Promise<ObservationsOverviewResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { observations: [], isAuthenticated: false };
  }

  try {
    // Compute the accessible slug list with two cheap queries in parallel,
    // then scope the heavy RPC server-side. Avoids hitting mv_programs_overview
    // (via get_programs_overview) just to discover public slugs.
    const [publicResult, accessResult] = await Promise.all([
      supabase.from('programs').select('slug').eq('is_public', true),
      supabase.from('user_program_access').select('program_slug').eq('user_id', user.id),
    ]);

    const accessibleSlugs = Array.from(
      new Set([
        ...(publicResult.data || []).map(p => p.slug),
        ...(accessResult.data || []).map(a => a.program_slug),
      ])
    );

    if (accessibleSlugs.length === 0) {
      return { observations: [], isAuthenticated: true };
    }

    const rpcResult = await supabase.rpc('get_observations_overview', {
      p_program_slugs: accessibleSlugs,
    });

    if (rpcResult.error) {
      console.error('Error fetching observations overview:', rpcResult.error);
      return { observations: [], error: rpcResult.error.message, isAuthenticated: true };
    }

    const observations: ObservationOverview[] = (rpcResult.data || [])
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      .map((o: any) => ({
        observation: o.observation,
        program_slug: o.program_slug,
        program_name: o.program_name ?? null,
        field: o.field,
        cycle: o.cycle ?? null,
        gratings: o.gratings || [],
        pointing_count: Number(o.pointing_count) || 0,
        pointings: (o.pointings as Pointing[] | null) ?? null,
        target_count: Number(o.target_count) || 0,
        spectrum_count: Number(o.spectrum_count) || 0,
        total_size_bytes: Number(o.total_size_bytes) || 0,
        reduction_version: o.reduction_version ?? null,
        crds_context: o.crds_context ?? null,
        cfpipe_version: o.cfpipe_version ?? null,
        jwst_version: o.jwst_version ?? null,
        reduced_at: o.reduced_at ?? null,
        deployed_at: o.deployed_at ?? null,
        n_patches_since_full: Number(o.n_patches_since_full) || 0,
        last_patch_at: o.last_patch_at ?? null,
      }));

    return { observations, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching observations overview:', err);
    return { observations: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

export async function getDatabaseOverview(): Promise<DatabaseOverviewResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { overview: null, isAuthenticated: false };
  }

  try {
    const { data, error } = await supabase.rpc('get_database_overview');

    if (error) {
      console.error('Error fetching database overview:', error);
      return { overview: null, error: error.message, isAuthenticated: true };
    }

    const row = (data || [])[0];
    if (!row) {
      return { overview: null, isAuthenticated: true };
    }

    const overview: DatabaseOverview = {
      n_programs: Number(row.n_programs) || 0,
      n_observations: Number(row.n_observations) || 0,
      n_pointings: Number(row.n_pointings) || 0,
      n_targets: Number(row.n_targets) || 0,
      n_spectra: Number(row.n_spectra) || 0,
      total_size_bytes: Number(row.total_size_bytes) || 0,
      latest_deployed_at: row.latest_deployed_at ?? null,
      latest_reduction_version: row.latest_reduction_version ?? null,
    };

    return { overview, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching database overview:', err);
    return { overview: null, error: 'An unexpected error occurred', isAuthenticated: true };
  }
}
