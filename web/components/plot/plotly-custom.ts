// The app's Plotly build: core + the two trace types CAMPFIRE renders
// (scatter for 1D spectra / models / emission lines / χ², heatmap for the 2D
// S/N image). Assembled from plotly.js's partial modules instead of the
// `plotly.js-cartesian-dist-min` bundle, which also carried bar, box,
// histogram, pie, contour, violin, … — ~30 % of the chunk (perf audit
// 2026-09, #500; recipe in docs/plotly_audit_2026-07-23.md §5).
//
// Adding a trace type: import its `plotly.js/lib/<type>` module and add it
// to the register() call. Nothing outside this file may import plotly.js —
// LazyPlot is the single entry point (see components/plot/LazyPlot.tsx).

import * as Plotly from 'plotly.js/lib/core';
import * as scatter from 'plotly.js/lib/scatter';
import * as heatmap from 'plotly.js/lib/heatmap';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
(Plotly as any).register([scatter, heatmap]);

export default Plotly;
