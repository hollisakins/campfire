'use client';

import dynamic from 'next/dynamic';
import { Loader2 } from 'lucide-react';

/**
 * The single lazily-loaded Plotly component for the whole app.
 *
 * Built via the react-plotly.js factory against the *cartesian* partial
 * bundle instead of the default export, which pulls in the full plotly.js
 * distribution (~4.5 MB minified: WebGL, geo, 3D, finance traces). The app
 * renders only scatter + heatmap, both included in the cartesian bundle.
 *
 * All plot components must import this instead of 'react-plotly.js' —
 * importing the latter directly would put the full bundle back in the
 * client chunk.
 */
export const LazyPlot = dynamic(
  async () => {
    const [{ default: createPlotlyComponent }, { default: Plotly }] = await Promise.all([
      import('react-plotly.js/factory'),
      import('plotly.js-cartesian-dist-min'),
    ]);
    return createPlotlyComponent(Plotly);
  },
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center h-full min-h-[400px] bg-card">
        <Loader2 className="w-6 h-6 animate-spin text-primary" />
      </div>
    ),
  }
);
