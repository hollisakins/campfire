'use client';

/**
 * Display panel (epic #337, Phase 4.5) — the docked-right control for how the
 * imagery is rendered (`docs/design-fitsgl-map-ux.md` §4). Scale (transfer curve)
 * + Limits (black/white presets) + the fine-adjust histogram + a colormap dropdown
 * (locked decision 3: a dropdown, not a swatch row). No north-up control — it is
 * forced on (decision 4). RGB channel *assignment* lives in the band rail, not
 * here (decision 1); this panel only styles whatever the active view is.
 *
 * Controlled: stretch mode + colormap flow up to the parent's `ExplorerState`
 * (→ `deriveViewerConfig`); the histogram/preset black-white points are pushed
 * imperatively by `useDisplayStretch` (see that hook).
 */

import { COLORMAP_NAMES, type ColormapName, type StretchMode } from '@fitsgl/core';
import type { ExplorerState } from '@fitsgl/core/react';
import { StretchHistogram } from './StretchHistogram';
import { ColormapControl } from './ColormapControl';
import { PANEL_CAP, chipClass } from './glass';
import type { DisplayChannel, LimitPreset, ChannelKey } from './useDisplayStretch';

/** Transfer curves offered as chips (zscale is a *limit* preset, not a curve). */
const SCALE_MODES: StretchMode[] = ['asinh', 'log', 'sqrt', 'linear'];
const ROLE_LABEL: Record<ChannelKey, string> = { single: '', r: 'R', g: 'G', b: 'B' };

interface DisplayPanelProps {
  state: ExplorerState;
  channels: DisplayChannel[];
  hasZscale: boolean;
  hasTrilogy: boolean;
  colormap: ColormapName;
  colormapReversed: boolean;
  onSetStretch: (mode: StretchMode) => void;
  onSetColormap: (name: ColormapName) => void;
  onToggleReverseColormap: (reversed: boolean) => void;
  onSetHandle: (key: ChannelKey, min: number, max: number) => void;
  onApplyPreset: (preset: LimitPreset) => void;
}

export function DisplayPanel({
  state,
  channels,
  hasZscale,
  hasTrilogy,
  colormap,
  colormapReversed,
  onSetStretch,
  onSetColormap,
  onToggleReverseColormap,
  onSetHandle,
  onApplyPreset,
}: DisplayPanelProps) {
  const trilogy = state.stretch === 'trilogy';
  const single = state.mode === 'single';

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className={PANEL_CAP}>Display</h4>
        <span className="font-mono text-[10px] text-text-tertiary">
          {single ? state.band : 'RGB'} · {state.stretch}
        </span>
      </div>

      {/* Scale (transfer curve). */}
      <div className="space-y-1.5">
        <span className={PANEL_CAP}>Scale</span>
        <div className="flex flex-wrap gap-1">
          {SCALE_MODES.map((m) => (
            <button key={m} type="button" onClick={() => onSetStretch(m)} className={chipClass(state.stretch === m)}>
              {m}
            </button>
          ))}
          {/* Trilogy is the RGB weighted composite — not offered in single-band. */}
          {!single && hasTrilogy && (
            <button
              type="button"
              onClick={() => onSetStretch('trilogy')}
              className={chipClass(trilogy)}
              title="Faithful colour-preserving trilogy stretch (precomputed levels)"
            >
              trilogy
            </button>
          )}
        </div>
      </div>

      {/* Limits + fine adjust. Trilogy sets its levels from precomputed stats. */}
      {trilogy ? (
        <p className="text-[11px] italic text-text-tertiary">
          Levels set from precomputed trilogy stats.
        </p>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className={PANEL_CAP}>Limits</span>
            <div className="flex gap-1">
              <button type="button" onClick={() => onApplyPreset('auto')} className={chipClass(false)} title="Auto-stretch to the data in view">auto</button>
              {hasZscale && (
                <button type="button" onClick={() => onApplyPreset('zscale')} className={chipClass(false)} title="Whole-image DS9/IRAF zscale cuts">zscale</button>
              )}
              <button type="button" onClick={() => onApplyPreset('minmax')} className={chipClass(false)} title="Full data min–max in view">minmax</button>
              <button type="button" onClick={() => onApplyPreset('p995')} className={chipClass(false)} title="0.25–99.75% in view">99.5%</button>
            </div>
          </div>

          {channels.map((c) =>
            c.histogram ? (
              <div key={c.key} className="space-y-0.5">
                {!single && (
                  <div className="flex items-center gap-1.5 text-[10px] font-mono text-text-tertiary">
                    <span className="inline-block h-2 w-2 rounded-sm" style={{ background: c.color }} aria-hidden />
                    {ROLE_LABEL[c.key]} · {c.band.label ?? c.band.name}
                  </div>
                )}
                <StretchHistogram
                  histogram={c.histogram}
                  min={c.min}
                  max={c.max}
                  color={c.color}
                  onChange={(min, max) => onSetHandle(c.key, min, max)}
                />
              </div>
            ) : null,
          )}
        </div>
      )}

      {/* Colormap — single mode only (an RGB composite carries its own colour). */}
      {single && !trilogy && (
        <div className="space-y-1.5">
          <span className={PANEL_CAP}>Colormap</span>
          <ColormapControl
            names={COLORMAP_NAMES}
            value={colormap}
            reversed={colormapReversed}
            onChange={onSetColormap}
            onToggleReverse={onToggleReverseColormap}
          />
        </div>
      )}
    </div>
  );
}
