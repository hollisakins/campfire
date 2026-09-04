import { getRequestPrincipal } from '@/lib/auth/identity';
import type { Program } from '@/lib/types';

export interface FilterOptionsResult {
  programs: Program[];
  fields: string[];
  observations: string[];
  error?: string;
}

const NO_STORE = { 'Cache-Control': 'private, no-store' };
const EMPTY: FilterOptionsResult = { programs: [], fields: [], observations: [] };

/**
 * GET /api/filter-options
 *
 * The programs, fields and observations the viewer may filter on (list page,
 * map). Programs are the viewer's accessible set — public + granted for an
 * ordinary user, every program for an admin, only the scoped program for a
 * share link (lib/auth/access-context.ts) — each carrying its JWST PIDs for
 * sorting; fields and observations come from the mv_filter_options matview.
 *
 * A GET route rather than a server action (perf T2-C, #506): the list page
 * fires this on mount next to its table fetch, and actions serialize.
 *
 * Failures after authentication ride in the body with a 200, as the action
 * did: a matview failure still returns the computed `programs` so the
 * program filter degrades instead of disappearing, and fetchJson() would
 * throw on a non-2xx and drop that partial payload. Only anonymous is 401.
 */
export async function GET() {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const { supabase, access } = principal;

  try {
    const { data: allPrograms, error: programsError } = await supabase.from('programs').select('*');
    if (programsError) {
      console.error('Error fetching programs:', programsError);
      return Response.json({ ...EMPTY, error: programsError.message } satisfies FilterOptionsResult, { headers: NO_STORE });
    }

    // Filter to the accessible set. The old form (is_public OR explicit
    // grant) was wrong for link accounts, whose scoped program is neither.
    const accessible = new Set(access.accessibleSlugs);
    const accessiblePrograms = (allPrograms || []).filter((p) => accessible.has(p.slug));
    if (accessiblePrograms.length === 0) {
      return Response.json(EMPTY, { headers: NO_STORE });
    }

    // JWST PIDs (for program sorting) and the field/observation lists in
    // parallel — independent reads, one wall-clock hop.
    const [{ data: obsData }, { data: filterData, error: filterError }] = await Promise.all([
      supabase.from('observations').select('program_slug, jwst_program_id'),
      supabase.from('mv_filter_options').select('fields, observations').single(),
    ]);

    const pidsBySlug: Record<string, number[]> = {};
    for (const obs of obsData || []) {
      if (!obs.jwst_program_id) continue;
      if (!pidsBySlug[obs.program_slug]) pidsBySlug[obs.program_slug] = [];
      if (!pidsBySlug[obs.program_slug].includes(obs.jwst_program_id)) {
        pidsBySlug[obs.program_slug].push(obs.jwst_program_id);
      }
    }
    const programs: Program[] = accessiblePrograms.map((p) => ({
      ...p,
      jwst_pids: pidsBySlug[p.slug]?.sort((a, b) => a - b) || [],
    }));

    if (filterError) {
      console.error('Error fetching filter options:', filterError);
      return Response.json(
        { programs, fields: [], observations: [], error: filterError.message } satisfies FilterOptionsResult,
        { headers: NO_STORE },
      );
    }

    const body: FilterOptionsResult = {
      programs,
      fields: filterData?.fields || [],
      observations: filterData?.observations || [],
    };
    return Response.json(body, { headers: NO_STORE });
  } catch (err) {
    console.error('Unexpected error fetching filter options:', err);
    return Response.json({ ...EMPTY, error: 'An unexpected error occurred' } satisfies FilterOptionsResult, { headers: NO_STORE });
  }
}
