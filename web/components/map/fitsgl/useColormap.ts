'use client';

/**
 * useColormap (epic #337, Phase 4.5) — applies the single-band colormap
 * imperatively. Colormap is deliberately kept OFF the controlled
 * `deriveViewerConfig` path (which can only express a `ColormapName`, not a
 * reversed LUT), so `viewState.colormap` stays pinned to `'gray'` and this hook is
 * the sole driver: a forward colormap passes the bundled name (grayscale → null),
 * and a reversed one passes a reversed `ColormapLUT`. Re-applies on band/source
 * change (setSource keeps the viewer-level colormap, but re-applying is cheap and
 * safe).
 */

import { useEffect } from 'react';
import { colormapRGB, type ColormapName } from '@fitsgl/core';
import type { FitsViewerHandle } from '@fitsgl/core/react';

/** Reverse a colormap LUT by flipping the order of its RGB triplets. */
function reverseLut(lut: Uint8Array): Uint8Array {
  const n = lut.length / 3;
  const out = new Uint8Array(lut.length);
  for (let i = 0; i < n; i++) {
    const src = (n - 1 - i) * 3;
    const dst = i * 3;
    out[dst] = lut[src];
    out[dst + 1] = lut[src + 1];
    out[dst + 2] = lut[src + 2];
  }
  return out;
}

interface UseColormapArgs {
  handleRef: React.RefObject<FitsViewerHandle | null>;
  name: ColormapName;
  reversed: boolean;
  /** Only single-band mode uses a colormap (an RGB composite carries its colour). */
  single: boolean;
  /** Identity of the active source; re-applies after a band switch. */
  sourceKey: string;
  /** Bumped on viewer (re)ready. */
  readyTick: number;
}

export function useColormap({ handleRef, name, reversed, single, sourceKey, readyTick }: UseColormapArgs) {
  useEffect(() => {
    if (!single) return;
    const v = handleRef.current?.getViewer();
    if (!v) return;
    if (!reversed) {
      v.setColormap(name === 'gray' ? null : name);
    } else {
      v.setColormap(reverseLut(colormapRGB(name)));
    }
  }, [handleRef, name, reversed, single, sourceKey, readyTick]);
}
