'use client';

import dynamic from 'next/dynamic';
import { Loader2 } from 'lucide-react';
import type { ComponentType } from 'react';
import type { PlotParams } from 'react-plotly.js';

/**
 * The single lazily-loaded Plotly component for the whole app.
 *
 * Built via the react-plotly.js factory against the app's own partial bundle
 * (components/plot/plotly-custom.ts: core + scatter + heatmap) instead of a
 * plotly.js distribution. All plot components must import this instead of
 * 'react-plotly.js' — importing the latter directly would put the full
 * plotly.js bundle back in the client chunk.
 *
 * Loading starts at module evaluation, not first render (#500): this module
 * is part of the object route's first-load JS, so the Plotly chunk request
 * now goes out while the page is still hydrating instead of after the first
 * <LazyPlot> mounts. The promise is shared, so `dynamic()` never triggers a
 * second download. Only the object page (MultiSpectrumViewer, SpectrumPlot,
 * PhotometrySED) imports this — keep it that way, or the eager load lands on
 * routes that never plot.
 */
type PlotComponent = ComponentType<PlotParams>;

let plotlyPromise: Promise<PlotComponent> | null = null;

/** Start (or join) the Plotly chunk download; safe to call repeatedly. */
export function preloadPlotly(): Promise<PlotComponent> {
  if (!plotlyPromise) {
    plotlyPromise = Promise.all([
      import('react-plotly.js/factory'),
      import('@/components/plot/plotly-custom'),
    ]).then(([{ default: createPlotlyComponent }, { default: Plotly }]) =>
      createPlotlyComponent(Plotly as Parameters<typeof createPlotlyComponent>[0]),
    );
  }
  return plotlyPromise;
}

if (typeof window !== 'undefined') {
  void preloadPlotly();
}

export const LazyPlot = dynamic(() => preloadPlotly(), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full min-h-[400px] bg-card">
      <Loader2 className="w-6 h-6 animate-spin text-primary" />
    </div>
  ),
});
