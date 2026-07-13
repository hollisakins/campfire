'use client';

/**
 * Band rail (epic #337, Phase 4.5) — the top-center glass pill that merges the
 * field selector with band selection (`docs/design-fitsgl-map-ux.md` §4). The
 * rail stays deliberately simple (revised decision 1): one chip per band plus an
 * `RGB` toggle. Clicking a band chip always selects single-band mode on that
 * band; everything about *how* an RGB composite is built (channel assignment,
 * simple vs trilogy, weights, stretch) lives in the Display panel.
 *
 * Purely presentational — the parent owns the `ExplorerState` and derives the
 * `ViewerConfig`; this only renders the model and calls back on intent.
 */

import type { ExplorerBand, ExplorerState } from '@fitsgl/core/react';
import { GLASS_PILL, chipClass, INSET } from './glass';

interface BandRailProps {
  fields: string[];
  selectedField: string;
  onFieldChange: (field: string) => void;
  bands: ExplorerBand[];
  state: ExplorerState;
  /** Whether an RGB composite is possible (≥2 co-gridded bands). */
  canComposite: boolean;
  onSelectBand: (name: string) => void;
  onToggleRgb: () => void;
}

export function BandRail({
  fields,
  selectedField,
  onFieldChange,
  bands,
  state,
  canComposite,
  onSelectBand,
  onToggleRgb,
}: BandRailProps) {
  const rgb = state.mode === 'rgb';
  const showBands = bands.length > 1;

  return (
    <div
      className={`absolute left-1/2 top-3 z-[500] flex -translate-x-1/2 items-center gap-1.5 ${GLASS_PILL} px-2 py-1.5`}
    >
      {/* Field selector — merged in (locked decision 2). */}
      {fields.length > 1 ? (
        <select
          value={selectedField}
          onChange={(e) => onFieldChange(e.target.value)}
          className={`${INSET} font-mono`}
          aria-label="Field"
        >
          {fields.map((f) => (
            <option key={f} value={f}>{f}</option>
          ))}
        </select>
      ) : (
        <span className="px-1.5 font-mono text-xs text-text-secondary">{selectedField}</span>
      )}

      {showBands && (
        <>
          <span className="mx-0.5 h-5 w-px bg-border" aria-hidden />

          {/* One chip per band — a chip is active only in single mode; clicking
              one always drops out of RGB onto that band. */}
          <div className="flex flex-wrap items-center gap-1">
            {bands.map((b) => (
              <button
                key={b.name}
                type="button"
                onClick={() => onSelectBand(b.name)}
                className={`${chipClass(!rgb && state.band === b.name)} font-mono`}
              >
                {b.label ?? b.name}
              </button>
            ))}
          </div>

          {canComposite && (
            <>
              <span className="mx-0.5 h-5 w-px bg-border" aria-hidden />
              <button
                type="button"
                onClick={onToggleRgb}
                className={chipClass(rgb)}
                title={rgb ? 'Back to single-band' : 'Compose an RGB image (tune it in the Display panel)'}
              >
                RGB
              </button>
            </>
          )}
        </>
      )}
    </div>
  );
}
