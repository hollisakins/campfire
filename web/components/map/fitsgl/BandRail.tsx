'use client';

/**
 * Band rail (epic #337, Phase 4.5) — the top-center glass pill that merges the
 * field selector with band selection (`docs/design-fitsgl-map-ux.md` §4). Mode is
 * implicit in the selection (locked decision 1): a single band chip → single mode;
 * the `RGB` affordance → three R/G/B channel pickers + a rainbow auto-assign. There
 * is no `Single | RGB` segmented toggle.
 *
 * Purely presentational — the parent owns the `ExplorerState` and derives the
 * `ViewerConfig`; this only renders the model and calls back on intent.
 */

import type { ExplorerBand, ExplorerState } from '@fitsgl/core/react';
import { GLASS_PILL, chipClass, INSET } from './glass';

type RgbRole = 'r' | 'g' | 'b';
const RGB_ROLES: readonly RgbRole[] = ['r', 'g', 'b'];
const ROLE_LABEL: Record<RgbRole, string> = { r: 'R', g: 'G', b: 'B' };
const ROLE_DOT: Record<RgbRole, string> = { r: '#ef4444', g: '#22c55e', b: '#3b82f6' };

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
  onSetRgbRole: (role: RgbRole, band: string) => void;
  /** Auto-assign R/G/B by pivot wavelength (reddest→R, bluest→B). */
  onRainbow: () => void;
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
  onSetRgbRole,
  onRainbow,
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

          {rgb ? (
            // RGB mode: three channel pickers + rainbow.
            <div className="flex items-center gap-1">
              {RGB_ROLES.map((role) => (
                <label key={role} className="flex items-center gap-1" title={`${ROLE_LABEL[role]} channel`}>
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-sm"
                    style={{ background: ROLE_DOT[role] }}
                    aria-hidden
                  />
                  <select
                    value={state.rgb[role]}
                    onChange={(e) => onSetRgbRole(role, e.target.value)}
                    className={`${INSET} font-mono`}
                    aria-label={`${ROLE_LABEL[role]} channel band`}
                  >
                    {bands.map((b) => (
                      <option key={b.name} value={b.name}>{b.label ?? b.name}</option>
                    ))}
                  </select>
                </label>
              ))}
              <button
                type="button"
                onClick={onRainbow}
                className={chipClass(false)}
                title="Auto-assign channels by wavelength (reddest→R, bluest→B)"
              >
                ✦ rainbow
              </button>
            </div>
          ) : (
            // Single mode: one chip per band.
            <div className="flex flex-wrap items-center gap-1">
              {bands.map((b) => (
                <button
                  key={b.name}
                  type="button"
                  onClick={() => onSelectBand(b.name)}
                  className={`${chipClass(state.band === b.name)} font-mono`}
                >
                  {b.label ?? b.name}
                </button>
              ))}
            </div>
          )}

          {canComposite && (
            <>
              <span className="mx-0.5 h-5 w-px bg-border" aria-hidden />
              <button
                type="button"
                onClick={onToggleRgb}
                className={chipClass(rgb)}
                title={rgb ? 'Back to single-band' : 'Compose an RGB image'}
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
