'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getFieldShutters, getFieldSlits } from '@/lib/actions/map';
import type { SlitRegion, Shutter, SkyBbox } from '@/lib/actions/map';

/** Pad a view box by `frac` of its size on every side, rounded so keys are stable. */
function expand(b: SkyBbox, frac: number): SkyBbox {
  const dra = (b.raMax - b.raMin) * frac;
  const ddec = (b.decMax - b.decMin) * frac;
  const r = (v: number) => Math.round(v * 1e4) / 1e4;
  return {
    raMin: r(b.raMin - dra),
    raMax: r(b.raMax + dra),
    decMin: r(Math.max(-90, b.decMin - ddec)),
    decMax: r(Math.min(90, b.decMax + ddec)),
  };
}

function contains(outer: SkyBbox, inner: SkyBbox): boolean {
  return inner.raMin >= outer.raMin && inner.raMax <= outer.raMax
    && inner.decMin >= outer.decMin && inner.decMax <= outer.decMax;
}

/**
 * The box to FETCH for a given VIEW box: the view padded by 50 % on each side,
 * kept while the view stays inside it, so panning within the padding and
 * zooming in never refetch; only leaving the padded box does. `null` view ⇒
 * whole field.
 */
function useStickyBbox(view: SkyBbox | null): SkyBbox | null {
  const [fetchBbox, setFetchBbox] = useState<SkyBbox | null>(null);
  useEffect(() => {
    if (!view) {
      setFetchBbox(null);
      return;
    }
    setFetchBbox((prev) => (prev && contains(prev, view) ? prev : expand(view, 0.5)));
  }, [view]);
  return fetchBbox;
}

/**
 * Shutters (or legacy slit regions) for the map's slit overlay.
 *
 * Perf T1-6 (#502): fetched only while the overlay is enabled (it is off by
 * default, yet every map load used to download the whole field — 20 MB for
 * COSMOS), keyset-paged inside the RPC, and scoped to the current view box
 * (padded, sticky) so a zoomed-in map pulls hundreds of shutters, not tens of
 * thousands. Results are cached per (field, fetch box).
 */
export function useFieldSlits(
  field: string | undefined,
  opts: { enabled?: boolean; bbox?: SkyBbox | null } = {},
) {
  const { enabled = true, bbox = null } = opts;
  const fetchBbox = useStickyBbox(bbox);
  return useQuery<(SlitRegion | Shutter)[]>({
    queryKey: ['fieldSlits', field, fetchBbox],
    queryFn: async () => {
      // Try shutters table first, fall back to legacy slit_regions — with the
      // same box, so an empty viewport on a shutter-bearing field costs one
      // small indexed query rather than the whole field's legacy slits.
      const shuttersResult = await getFieldShutters(field!, fetchBbox);
      if (shuttersResult.error) throw new Error(shuttersResult.error);
      if (shuttersResult.shutters.length > 0) return shuttersResult.shutters;

      const slitsResult = await getFieldSlits(field!, fetchBbox);
      if (slitsResult.error) throw new Error(slitsResult.error);
      return slitsResult.slits;
    },
    enabled: !!field && enabled,
    staleTime: 10 * 60 * 1000, // 10 minutes — shutter data rarely changes
    // Keep the last box's shutters on screen while the next box loads — but
    // only within the same field; another field's shutters must not linger.
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === field ? prev : undefined),
  });
}
