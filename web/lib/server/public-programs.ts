// Which programs are public (`programs.is_public`), memoized per instance.
//
// The cutout store (perf T2-D3, #509) keeps rendered cutouts at unsigned
// public urls on the tiles bucket, keyed by the target's coordinates. That
// is only acceptable for targets whose catalog row is itself public: a
// private program's target must never leave a 30-day public record of where
// it sits on the sky, even when the imagery under it is public (#509 asks
// for signed or gated delivery for non-public programs). Publication flips
// are rare; a minute of staleness on a warm instance is fine either way (a
// just-unpublished program's earlier renders expire under the lifecycle
// rule; a just-published one renders uncached for a minute).
import 'server-only';

import { createServiceClient } from '@/lib/supabase/server';

const PUBLIC_PROGRAMS_TTL_MS = 60_000;
let memo: { slugs: Set<string>; at: number } | null = null;

/** The set of public program slugs. Fails closed: a lookup error yields an
 * empty set (nothing is stored), never an exception. */
export async function publicProgramSlugs(): Promise<Set<string>> {
  if (memo && Date.now() - memo.at < PUBLIC_PROGRAMS_TTL_MS) return memo.slugs;
  try {
    const { data, error } = await createServiceClient()
      .from('programs')
      .select('slug')
      .eq('is_public', true);
    if (error) throw error;
    const slugs = new Set<string>((data ?? []).map((p: { slug: string }) => p.slug));
    memo = { slugs, at: Date.now() };
    return slugs;
  } catch (err) {
    console.error('public program lookup failed; treating every program as private:', err);
    return new Set();
  }
}

/** Whether a catalog row in `programs` (a target's program, an object's
 * program list) is public — i.e. its coordinates are already public. */
export function catalogIsPublic(programs: readonly string[], publicSlugs: Set<string>): boolean {
  return programs.some((p) => publicSlugs.has(p));
}

/** Test hook. */
export function _resetPublicPrograms(): void {
  memo = null;
}
