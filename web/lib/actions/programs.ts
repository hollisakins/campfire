'use server';

import { createClient } from '@/lib/supabase/server';

export interface ProgramOverview {
  program_id: number;
  program_name: string | null;
  pi_name: string | null;
  description: string | null;
  is_public: boolean;
  object_count: number;
  gratings: string[];
  fields: string[];
  observations: string[];
}

export interface ObservationStat {
  observation: string;
  program_id: number;
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
      supabase.from('user_program_access').select('program_id').eq('user_id', user.id),
    ]);

    const { data, error } = rpcResult;

    if (error) {
      console.error('Error fetching programs overview:', error);
      return { programs: [], error: error.message, isAuthenticated: true };
    }

    const explicitAccessIds = new Set((accessResult.data || []).map(a => a.program_id));

    const programs: ProgramOverview[] = (data || [])
      .filter((p: ProgramOverview) => p.is_public || explicitAccessIds.has(p.program_id))
      .map((p: ProgramOverview) => ({
        program_id: p.program_id,
        program_name: p.program_name,
        pi_name: p.pi_name,
        description: p.description,
        is_public: p.is_public,
        object_count: Number(p.object_count) || 0,
        gratings: p.gratings || [],
        fields: p.fields || [],
        observations: p.observations || [],
      }));

    return { programs, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching programs:', err);
    return { programs: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

export async function getProgramDetail(programId: number): Promise<ProgramDetailResult> {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return { program: null, observations: [], isAuthenticated: false };
  }

  try {
    // Fetch program overview, observation stats, and user access in parallel
    const [overviewResult, obsResult, accessResult] = await Promise.all([
      supabase.rpc('get_programs_overview'),
      supabase.rpc('get_observation_stats', { p_program_ids: [programId] }),
      supabase.from('user_program_access').select('program_id').eq('user_id', user.id),
    ]);

    if (overviewResult.error) {
      console.error('Error fetching program detail:', overviewResult.error);
      return { program: null, observations: [], error: overviewResult.error.message, isAuthenticated: true };
    }

    const explicitAccessIds = new Set((accessResult.data || []).map(a => a.program_id));

    // Find the specific program
    const programData = (overviewResult.data || []).find(
      (p: ProgramOverview) => p.program_id === programId
    );

    if (!programData) {
      return { program: null, observations: [], error: 'Program not found', isAuthenticated: true };
    }

    // Check access
    if (!programData.is_public && !explicitAccessIds.has(programId)) {
      return { program: null, observations: [], error: 'Access denied', isAuthenticated: true };
    }

    const program: ProgramOverview = {
      program_id: programData.program_id,
      program_name: programData.program_name,
      pi_name: programData.pi_name,
      description: programData.description,
      is_public: programData.is_public,
      object_count: Number(programData.object_count) || 0,
      gratings: programData.gratings || [],
      fields: programData.fields || [],
      observations: programData.observations || [],
    };

    const observations: ObservationStat[] = (obsResult.data || []).map(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (o: any) => ({
        observation: o.observation,
        program_id: o.program_id,
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
