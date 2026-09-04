// Server-only: per-viewer visibility filtering for the Updates feed.
//
// An update with no `programs` is public. A program-tagged update is visible
// only to viewers who can access at least one of its programs — mirroring how
// targets/spectra visibility works via `accessible_program_slugs()`.

import { getRequestPrincipal } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/service';
import { getAllUpdates } from './loader';
import type { UpdateEntry } from './types';

/** Program slugs the current viewer may see:
 *  - signed-in: the memoized access context (mirrors accessible_program_slugs())
 *  - anonymous: public programs only (follows each program's `is_public` flag)
 *
 *  Fails closed (empty set) on any error, so gated entries are hidden rather
 *  than leaked. */
export async function getAccessibleProgramSlugs(): Promise<Set<string>> {
  try {
    const principal = await getRequestPrincipal();
    if (principal) return new Set(principal.access.accessibleSlugs);

    // Anonymous: RLS blocks the `anon` role from reading `programs`, so use the
    // service client for this read-only lookup of public slugs (public info).
    const admin = createServiceClient();
    const { data, error } = await admin
      .from('programs')
      .select('slug')
      .eq('is_public', true);
    if (error) throw error;
    return new Set((data ?? []).map((p: { slug: string }) => p.slug));
  } catch {
    return new Set();
  }
}

/** All updates the current viewer may see, in display order. */
export async function getVisibleUpdates(): Promise<UpdateEntry[]> {
  const all = getAllUpdates();

  // Fast path: if nothing is gated, skip per-request auth/DB work entirely so
  // the page can stay statically rendered until a gated update is published.
  if (!all.some((e) => e.programs.length > 0)) return all;

  const accessible = await getAccessibleProgramSlugs();
  return all.filter(
    (e) => e.programs.length === 0 || e.programs.some((s) => accessible.has(s))
  );
}
