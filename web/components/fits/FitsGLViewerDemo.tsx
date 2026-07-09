'use client';

/**
 * Throwaway `<FitsViewer>` harness (epic #337, Phase 1 / #338).
 *
 * The single job of this component is to prove that `@fitsgl/core`'s React tier
 * mounts, resolves its decode worker, and paints a served FitsGL dataset inside a
 * Next.js page — the packaging milestone, ahead of the real map swap (Phase 4).
 * It is NOT the production map: no object markers, no shutter overlay, no field
 * chrome. Point it at a `manifest.json` from a locally-served dataset
 * (`fitsgl serve`) or, later, a deployed `campfire-tiles` prefix.
 *
 * Loaded via `next/dynamic` with `ssr: false` (see the page) — the viewer needs
 * WebGL2 + a real DOM, exactly like the Leaflet map's `MapViewerWrapper`.
 */

import { useMemo, useState } from 'react';
import { FitsViewer } from '@fitsgl/core/react';
import type { ViewerConfig } from '@fitsgl/core';
import { makeFitsglWorker } from '@/lib/fits/fitsglWorker';

/** A minimal single-band config pointing at one band manifest URL. */
function singleBandConfig(manifestUrl: string): ViewerConfig {
  return {
    bands: [{ name: 'demo', tiles: [manifestUrl] }],
    view: { mode: 'single', band: 'demo' },
  };
}

export function FitsGLViewerDemo({ manifestUrl }: { manifestUrl: string }) {
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  // Rebuild the config (and reset status) whenever the target manifest changes.
  const config = useMemo(() => {
    setError(null);
    setReady(false);
    return singleBandConfig(manifestUrl);
  }, [manifestUrl]);

  return (
    <div className="relative w-full h-[600px] rounded-lg overflow-hidden border border-border bg-black">
      <FitsViewer
        config={config}
        tileOptions={{ workerFactory: makeFitsglWorker }}
        className="absolute inset-0"
        onReady={() => setReady(true)}
        onError={(e) => setError(e instanceof Error ? e.message : String(e))}
      />
      <div className="absolute top-2 left-2 px-2 py-1 rounded bg-black/60 text-xs font-mono text-white/80 pointer-events-none">
        {error ? `error: ${error}` : ready ? 'ready' : 'loading…'}
      </div>
    </div>
  );
}

export default FitsGLViewerDemo;
