'use client';

import { preload } from 'react-dom';
import { workAsyncStorage } from 'next/dist/server/app-render/work-async-storage.external';
import PLOTLY_LOADABLE_IDS from './plotly-loadable-ids.json';

/**
 * Preload hints for the Plotly chunks, emitted during the server render so
 * the download starts with the HTML rather than after first-load JS has run
 * (perf T2-E #510; the module-eval preload in LazyPlot.tsx stays as the
 * client-side start for routes reached by client navigation).
 *
 * `next/dynamic` with `ssr: false` never emits these itself — only its SSR
 * path renders its PreloadChunks — and Plotly cannot be SSR'd (`self is not
 * defined` at import). This mirrors that PreloadChunks: the chunk files come
 * from the react-loadable manifest on the request's work store, under the
 * ids the SWC transform assigns to LazyPlot's two `import()` calls
 * (`<file> -> <specifier>`; see .next/react-loadable-manifest.json). Scripts
 * only — the manifest also lists a stylesheet a shared vendor chunk carries,
 * which Next's version would hoist into the head as render-blocking CSS.
 *
 * The ids are read by scripts/check-bundle-budget.mjs too, which fails CI
 * when a build's manifest no longer carries them (a Next upgrade or a
 * LazyPlot refactor), since a stale id preloads nothing, silently. Renders
 * nothing; place it in a server component on routes that plot above the fold.
 */
export function PlotlyPreload() {
  // Server render only: the browser has the hints from the HTML.
  if (typeof window !== 'undefined') return null;
  const store = workAsyncStorage.getStore();
  const manifest = store?.reactLoadableManifest;
  if (!manifest) return null;

  const files = new Set<string>();
  for (const id of PLOTLY_LOADABLE_IDS) {
    for (const file of manifest[id]?.files ?? []) {
      if (file.endsWith('.js')) files.add(file);
    }
  }
  const dpl = process.env.NEXT_DEPLOYMENT_ID ? `?dpl=${process.env.NEXT_DEPLOYMENT_ID}` : '';
  for (const file of files) {
    const href = `${store.assetPrefix ?? ''}/_next/${file.split('/').map(encodeURIComponent).join('/')}${dpl}`;
    // Low priority, as Next's own hints: the page's first-load scripts come
    // first, Plotly right behind them.
    preload(href, { as: 'script', fetchPriority: 'low' });
  }
  return null;
}
