import 'server-only';

import { cache } from 'react';
import { getRequestIdentity } from '@/lib/auth/identity';
import { getAccessContext } from '@/lib/auth/access-context';
import { createServiceClient } from '@/lib/supabase/service';
import type { ObjectHeader, ObjectMemberTarget, ObjectPhotometry, Spectrum } from '@/lib/types';

/**
 * The object page's data, in the order the page needs it (perf T2-E, #510):
 *
 *   loadObjectRow      — identity, accessible slugs and the `objects` row
 *                        (request-memoized: generateMetadata and the page
 *                        share one lookup)
 *   loadObjectHeader   — the row plus its access-scoped member targets and
 *                        spectra; what the HTML is rendered from
 *   loadObjectPhotometry — the object_photometry row, streamed behind a
 *                        Suspense boundary; never rejects
 *   loadObjectMetadata — the Open Graph fields, from the memoized row when
 *                        the viewer is signed in, else a service-role read
 *
 * lib/actions/spectra.ts's getObjectById joins header and photometry for the
 * inspection-mode callers that fetch client-side.
 */

type ObjectRow = {
  id: number;
  object_id: string;
  field: string;
  ra: number;
  dec: number;
  programs: string[] | null;
  redshift: number | null;
  redshift_quality: number | null;
  redshift_inspected: number | null;
  redshift_auto: number | null;
  inspected_used_auto: boolean | null;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  last_data_change_at: string | null;
  staleness_reason: ObjectHeader['staleness_reason'];
  version: number | null;
  is_active: boolean | null;
  photo_z: number | null;
  photo_z_err_lo: number | null;
  photo_z_err_hi: number | null;
  has_photometry: boolean | null;
  created_at: string;
};

type ObjectRowResult =
  | { status: 'anonymous' }
  | { status: 'missing'; error: string }
  | { status: 'denied' }
  | { status: 'ok'; row: ObjectRow; accessibleSlugs: string[] };

/**
 * Identity → (accessible slugs ∥ objects row) → access check. Memoized per
 * request with React cache(), so generateMetadata and the page body — which
 * Next runs concurrently — cost one lookup between them.
 */
const loadObjectRow = cache(async (objectId: string): Promise<ObjectRowResult> => {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return { status: 'anonymous' };

  // Accessible-slug list (SQL authority — see web/lib/auth/access-context.ts)
  // and the object row in parallel.
  const [accessibleSlugs, { data: row, error }] = await Promise.all([
    getAccessContext(user.id).then(a => a.accessibleSlugs),
    supabase.from('objects').select('*').eq('object_id', objectId).single(),
  ]);

  if (error || !row) {
    return { status: 'missing', error: error?.code === 'PGRST116' ? 'Object not found' : (error?.message ?? 'Object not found') };
  }

  // Access: the object's programs must overlap the accessible programs.
  const objPrograms: string[] = row.programs || [];
  if (!objPrograms.some(p => accessibleSlugs.includes(p))) return { status: 'denied' };

  return { status: 'ok', row: row as ObjectRow, accessibleSlugs };
});

/**
 * Display-only access scoping. The objects row stores aggregate columns
 * (programs, gratings, counts, max_snr/exposure) computed across ALL member
 * programs at deploy time. The object is visible because the viewer can
 * access at least one member program, but the stored aggregates would leak
 * metadata about proprietary members they cannot access, so they are
 * recomputed from the access-filtered member targets. Mirrors the SQL helper
 * object_scoped_aggregates() and the deploy-time builder in
 * python/campfire/deploy/objects.py. Object-level science (redshift,
 * photometry) intentionally stays visible.
 */
export function scopeObjectAggregates(memberTargets: ObjectMemberTarget[]): Pick<
  ObjectHeader,
  'n_targets' | 'n_spectra' | 'programs' | 'gratings' | 'max_snr' | 'max_exposure_time'
> {
  const spectra = memberTargets.flatMap(m => m.spectra || []);
  const snr = spectra.map(s => s.signal_to_noise).filter((v): v is number => v != null);
  const exp = spectra.map(s => s.exposure_time).filter((v): v is number => v != null);
  return {
    n_targets: memberTargets.length,
    n_spectra: spectra.length,
    programs: [...new Set(memberTargets.map(m => m.program_slug))].sort(),
    gratings: [...new Set(spectra.map(s => s.grating).filter(Boolean))].sort(),
    max_snr: snr.length ? Math.max(...snr) : null,
    max_exposure_time: exp.length ? Math.max(...exp) : null,
  };
}

/**
 * The object row and its member targets with their spectra, scoped to the
 * programs the viewer can access. Same contract as the former all-in-one
 * getObjectById minus photometry.
 */
export async function loadObjectHeader(objectId: string): Promise<{
  object: ObjectHeader | null;
  error?: string;
  isAuthenticated: boolean;
}> {
  try {
    const r = await loadObjectRow(objectId);
    if (r.status === 'anonymous') return { object: null, isAuthenticated: false };
    if (r.status === 'missing') return { object: null, error: r.error, isAuthenticated: true };
    if (r.status === 'denied') return { object: null, error: 'Object not found or access denied', isAuthenticated: true };
    const { row, accessibleSlugs } = r;

    // Columns are enumerated: `spectra (*)` dragged the two pre-rendered
    // thumbnail SVGs (~1.5 kB each, 84 % of the row's bytes) through detoast
    // → wire → RSC payload for every spectrum, and nothing on this page
    // renders them (#500).
    const { supabase } = await getRequestIdentity();
    const { data: members, error: membersError } = await supabase
      .from('targets')
      .select(`
        *,
        programs:program_slug (program_name),
        spectra (id, spectrum_id, target_id, grating, fits_path, cfpipe_version, signal_to_noise, exposure_time, created_at, updated_at, redshift_auto, chi2_min, confidence, dq_flags, deploy_status)
      `)
      .eq('object_id', row.id)
      .in('program_slug', accessibleSlugs);

    if (membersError) return { object: null, error: membersError.message, isAuthenticated: true };

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
      spectra: (m.spectra || []) as Spectrum[],
    })).sort((a: ObjectMemberTarget, b: ObjectMemberTarget) => (b.max_snr || 0) - (a.max_snr || 0));

    const object: ObjectHeader = {
      id: row.id,
      object_id: row.object_id,
      field: row.field,
      ra: row.ra,
      dec: row.dec,
      ...scopeObjectAggregates(memberTargets),
      redshift: row.redshift ?? null,
      redshift_quality: row.redshift_quality ?? 0,
      redshift_inspected: row.redshift_inspected ?? null,
      redshift_auto: row.redshift_auto ?? null,
      inspected_used_auto: row.inspected_used_auto ?? false,
      last_inspected_at: row.last_inspected_at ?? null,
      last_inspected_by: row.last_inspected_by ?? null,
      last_data_change_at: row.last_data_change_at ?? null,
      staleness_reason: row.staleness_reason ?? null,
      version: row.version ?? 1,
      is_active: row.is_active ?? true,
      photo_z: row.photo_z ?? null,
      photo_z_err_lo: row.photo_z_err_lo ?? null,
      photo_z_err_hi: row.photo_z_err_hi ?? null,
      has_photometry: row.has_photometry ?? false,
      created_at: row.created_at,
      member_targets: memberTargets,
    };
    return { object, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching object:', err);
    return { object: null, error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * The object's photometry row, or null. Under the viewer's RLS (row-local
 * since T2-A), for an object loadObjectHeader has already admitted. Never
 * rejects: the page passes this promise to the client tree, where a
 * rejection would surface as a page-level error instead of a missing SED.
 */
export async function loadObjectPhotometry(objectDbId: number): Promise<ObjectPhotometry | null> {
  try {
    const { supabase } = await getRequestIdentity();
    const { data, error } = await supabase
      .from('object_photometry')
      .select('catalog_name, catalog_id, match_distance_arcsec, photometry, photo_z, photo_z_err_lo, photo_z_err_hi, has_pz')
      .eq('object_id', objectDbId)
      .limit(1)
      .maybeSingle();
    if (error) {
      console.error('Error fetching object photometry:', error);
      return null;
    }
    if (!data) return null;
    return {
      catalog_name: data.catalog_name,
      catalog_id: data.catalog_id,
      match_distance_arcsec: data.match_distance_arcsec,
      photometry: data.photometry,
      photo_z: data.photo_z,
      photo_z_err_lo: data.photo_z_err_lo,
      photo_z_err_hi: data.photo_z_err_hi,
      has_pz: data.has_pz ?? false,
    };
  } catch (err) {
    console.error('Unexpected error fetching object photometry:', err);
    return null;
  }
}

/**
 * The photometry of an object named by object_id, for a caller that wants it
 * alongside loadObjectHeader without waiting for it: the memoized row lookup
 * is shared, so the members and photometry queries run in parallel as they
 * did in the all-in-one loader. Null for an object the viewer cannot see.
 */
export async function loadObjectPhotometryOf(objectId: string): Promise<ObjectPhotometry | null> {
  try {
    const r = await loadObjectRow(objectId);
    if (r.status !== 'ok' || !r.row.has_photometry) return null;
    return loadObjectPhotometry(r.row.id);
  } catch {
    return null;
  }
}

export interface ObjectMetadata {
  object_id: string;
  redshift: number | null;
  field: string;
}

/**
 * Minimal object metadata for Open Graph tags. A signed-in viewer's page
 * render already holds the row (loadObjectRow is request-memoized), so this
 * costs nothing on top of it; anyone else — link-preview bots, signed-out
 * visitors, a viewer the object is hidden from — gets the service-role read
 * gated on has_published_spectrum, so draft objects return null.
 */
export async function loadObjectMetadata(objectId: string): Promise<ObjectMetadata | null> {
  try {
    const r = await loadObjectRow(objectId);
    if (r.status === 'ok') {
      return { object_id: r.row.object_id, redshift: r.row.redshift, field: r.row.field };
    }

    const supabase = createServiceClient();
    const { data, error } = await supabase
      .from('objects')
      .select('object_id, redshift, field')
      .eq('object_id', objectId)
      .eq('has_published_spectrum', true)
      .single();
    if (error || !data) return null;
    return { object_id: data.object_id, redshift: data.redshift, field: data.field };
  } catch {
    return null;
  }
}
