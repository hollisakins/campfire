'use client';

/**
 * FitsGL packaging demo (epic #337, Phase 1 / #338).
 *
 * A throwaway harness that proves `@fitsgl/core`'s `<FitsViewer>` builds and
 * renders inside a Next.js page — the web half of the packaging milestone. Paste
 * a band `manifest.json` URL from a locally-served dataset (`fitsgl serve`) or,
 * later, a deployed `campfire-tiles` prefix, and load it. This is not the
 * production map (that arrives in Phase 4); it deliberately carries no overlays.
 */

import dynamic from 'next/dynamic';
import { useState } from 'react';

// WebGL2 + real DOM only — mirror the Leaflet map's dynamic ssr:false import.
const FitsGLViewerDemo = dynamic(
  () => import('@/components/fits/FitsGLViewerDemo').then((m) => m.FitsGLViewerDemo),
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center h-[600px] rounded-lg border border-border bg-surface-2">
        <div className="text-text-secondary">Loading viewer…</div>
      </div>
    ),
  }
);

export default function FitsGLDemoPage() {
  const [input, setInput] = useState('');
  const [manifestUrl, setManifestUrl] = useState<string | null>(null);

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="text-2xl font-bold text-text-primary">FitsGL viewer demo</h1>
        <p className="mt-2 text-text-secondary">
          Phase 1 packaging check (epic #337): render a served FitsGL dataset with{' '}
          <code className="px-1 py-0.5 bg-surface-2 rounded text-xs">@fitsgl/core</code>{' '}
          inside a Next page. Serve a dataset locally with{' '}
          <code className="px-1 py-0.5 bg-surface-2 rounded text-xs">fitsgl serve</code>{' '}
          and paste one band&apos;s{' '}
          <code className="px-1 py-0.5 bg-surface-2 rounded text-xs">manifest.json</code>{' '}
          URL below.
        </p>
      </div>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setManifestUrl(input.trim() || null);
        }}
      >
        <input
          type="url"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="http://localhost:8000/demo/band/manifest.json"
          className="flex-1 px-3 h-10 rounded-lg border border-border bg-card text-sm text-text-primary placeholder:text-text-tertiary"
        />
        <button
          type="submit"
          className="px-4 h-10 rounded-lg bg-primary text-on-primary text-sm font-medium hover:bg-primary-hover transition-colors disabled:opacity-50"
          disabled={!input.trim()}
        >
          Load
        </button>
      </form>

      {manifestUrl ? (
        <FitsGLViewerDemo manifestUrl={manifestUrl} />
      ) : (
        <div className="flex items-center justify-center h-[600px] rounded-lg border border-dashed border-border bg-surface-2 text-text-secondary text-sm">
          Enter a manifest URL to render a dataset.
        </div>
      )}
    </div>
  );
}
