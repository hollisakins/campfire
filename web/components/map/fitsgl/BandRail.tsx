'use client';

/**
 * Band rail (epic #337, Phase 4.5) — the top-center glass pill: a field dropdown
 * and a band dropdown, nothing else (revised decision 1). `RGB` rides as the
 * FIRST option of the band dropdown; picking it enters composite mode (tuned in
 * the Display panel), picking a band name drops to single-band mode on it.
 *
 * Purely presentational — the parent owns the `ExplorerState` and derives the
 * `ViewerConfig`; this only renders the model and calls back on intent.
 */

import type { ExplorerBand, ExplorerState } from '@fitsgl/core/react';
import { GLASS_PILL, INSET } from './glass';

/** Sentinel option value for the composite entry (no band may collide with it). */
export const RGB_OPTION = '__rgb__';

interface BandRailProps {
  fields: string[];
  selectedField: string;
  onFieldChange: (field: string) => void;
  bands: ExplorerBand[];
  state: ExplorerState;
  /** Whether an RGB composite is possible (≥2 co-gridded bands). */
  canComposite: boolean;
  /** `RGB_OPTION` → composite mode; a band name → single mode on that band. */
  onSelectDisplay: (value: string) => void;
}

export function BandRail({
  fields,
  selectedField,
  onFieldChange,
  bands,
  state,
  canComposite,
  onSelectDisplay,
}: BandRailProps) {
  const value = state.mode === 'rgb' ? RGB_OPTION : state.band;
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
          <select
            value={value}
            onChange={(e) => onSelectDisplay(e.target.value)}
            className={`${INSET} font-mono`}
            aria-label="Band"
          >
            {canComposite && <option value={RGB_OPTION}>RGB</option>}
            {bands.map((b) => (
              <option key={b.name} value={b.name}>{b.label ?? b.name}</option>
            ))}
          </select>
        </>
      )}
    </div>
  );
}
