'use server';

import { createClient } from '@/lib/supabase/server';

export interface ProgramOverview {
  slug: string;
  program_name: string | null;
  pi_name: string | null;
  description: string | null;
  is_public: boolean;
  cycle: number | null;
  object_count: number;
  gratings: string[];
  fields: string[];
  observations: string[];
  jwst_pids: number[];
}

export interface ObservationStat {
  observation: string;
  program_slug: string;
  program_name: string;
  field: string;
  object_count: number;
  spectrum_count: number;
  total_size_bytes: number;
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
        object_count: Number(p.object_count) || 0,
        gratings: p.gratings || [],
        fields: p.fields || [],
        observations: p.observations || [],
        jwst_pids: p.jwst_pids || [],
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
      object_count: Number(programData.object_count) || 0,
      gratings: programData.gratings || [],
      fields: programData.fields || [],
      observations: programData.observations || [],
      jwst_pids: programData.jwst_pids || [],
    };

    const observations: ObservationStat[] = (obsResult.data || []).map(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (o: any) => ({
        observation: o.observation,
        program_slug: o.program_slug,
        program_name: o.program_name,
        field: o.field,
        object_count: Number(o.object_count) || 0,
        spectrum_count: Number(o.spectrum_count) || 0,
        total_size_bytes: Number(o.total_size_bytes) || 0,
      })
    );

    return { program, observations, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching program detail:', err);
    return { program: null, observations: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}
