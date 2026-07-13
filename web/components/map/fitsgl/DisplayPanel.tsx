'use client';

/**
 * Display panel (epic #337, Phase 4.5; RGB rework per the map-RGB-defaults
 * follow-up) — the docked-right control for how the imagery is rendered
 * (`docs/design-fitsgl-map-ux.md` §4). ALL RGB construction lives here (revised
 * decision 1 — the band rail stays a simple band/RGB switch):
 *
 *  - **single mode**: Scale chips + Limits presets/histogram + colormap dropdown
 *    (locked decision 3: a dropdown, not a swatch row).
 *  - **RGB · simple**: pick the three channel bands, one SHARED limit range
 *    (bands share flux units, so shared cuts keep the composite interpretable —
 *    deliberately no per-band stretch), a shared transfer curve, plus contrast
 *    and color-saturation sliders.
 *  - **RGB · trilogy**: the faithful weighted composite — FitsGL's own weight
 *    matrix (per-band R/G/B knobs + rainbow) and trilogy tuning sliders
 *    (noiselum / saturate % / noise σ / black σ), levels derived live from the
 *    precomputed per-band stats.
 *
 * No north-up control — it is forced on (decision 4). Controlled: stretch mode,
 * channel assignment, weights and trilogy params flow up to the parent's
 * `ExplorerState` (→ `deriveViewerConfig`); black/white points, contrast and
 * saturation are pushed imperatively (see `useDisplayStretch`).
 */

import { COLORMAP_NAMES, type BandWeight, type ColormapName, type StretchMode, type TrilogyParams } from '@fitsgl/core';
import { TrilogyControls, TrilogyWeightMatrix } from '@fitsgl/core/react';
import type { ExplorerBand, ExplorerState } from '@fitsgl/core/react';
import { StretchHistogram } from './StretchHistogram';
import { ColormapControl } from './ColormapControl';
import { PANEL_CAP, chipClass, INSET } from './glass';
import type { DisplayChannel, LimitPreset } from './useDisplayStretch';

/** Transfer curves offered as chips (zscale is a *limit* preset, not a curve;
 *  trilogy is the RGB sub-mode toggle, not a chip). */
const SCALE_MODES: StretchMode[] = ['asinh', 'log', 'sqrt', 'linear'];

type RgbRole = 'r' | 'g' | 'b';
const RGB_ROLES: readonly RgbRole[] = ['r', 'g', 'b'];
const ROLE_LABEL: Record<RgbRole, string> = { r: 'R', g: 'G', b: 'B' };
const ROLE_DOT: Record<RgbRole, string> = { r: '#ef4444', g: '#22c55e', b: '#3b82f6' };

interface DisplayPanelProps {
  state: ExplorerState;
  bands: ExplorerBand[];
  channels: DisplayChannel[];
  hasZscale: boolean;
  hasTrilogy: boolean;
  colormap: ColormapName;
  colormapReversed: boolean;
  /** Simple-RGB shared black/white points (null outside RGB mode). */
  sharedRange: { min: number; max: number } | null;
  /** Simple-RGB contrast about the shared midpoint (1 = neutral). */
  contrast: number;
  /** Composite color saturation (1 = neutral, 0 = grayscale). */
  saturation: number;
  onSetStretch: (mode: StretchMode) => void;
  /** Switch between the simple 3-band composite and the weighted trilogy. */
  onSetRgbSubMode: (mode: 'simple' | 'trilogy') => void;
  onSetRgbRole: (role: RgbRole, band: string) => void;
  onApplyWeights: (entries: Array<{ band: string; weight: BandWeight }>) => void;
  onRainbowWeights: () => void;
  onSetTrilogyParams: (patch: Partial<TrilogyParams>) => void;
  onSetColormap: (name: ColormapName) => void;
  onToggleReverseColormap: (reversed: boolean) => void;
  onSetHandle: (key: DisplayChannel['key'], min: number, max: number) => void;
  onSetSharedRange: (min: number, max: number) => void;
  onSetContrast: (contrast: number) => void;
  onSetSaturation: (saturation: number) => void;
  onApplyPreset: (preset: LimitPreset) => void;
}

/** A labeled slider row in campfire chrome (contrast / saturation). */
function SliderRow({
  label,
  value,
  min,
  max,
  step,
  neutral,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  /** Double-click resets to this. */
  neutral: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex items-center gap-2">
      <span className="w-16 shrink-0 font-mono text-[10px] text-text-tertiary">{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        onDoubleClick={() => onChange(neutral)}
        className="h-1 flex-1 accent-[var(--primary)]"
        title={`${label} (double-click to reset)`}
      />
      <span className="w-9 shrink-0 text-right font-mono text-[10px] tabular-nums text-text-secondary">
        {value.toFixed(2)}
      </span>
    </label>
  );
}

export function DisplayPanel({
  state,
  bands,
  channels,
  hasZscale,
  hasTrilogy,
  colormap,
  colormapReversed,
  sharedRange,
  contrast,
  saturation,
  onSetStretch,
  onSetRgbSubMode,
  onSetRgbRole,
  onApplyWeights,
  onRainbowWeights,
  onSetTrilogyParams,
  onSetColormap,
  onToggleReverseColormap,
  onSetHandle,
  onSetSharedRange,
  onSetContrast,
  onSetSaturation,
  onApplyPreset,
}: DisplayPanelProps) {
  const single = state.mode === 'single';
  const trilogy = !single && state.stretch === 'trilogy';

  // The shared simple-RGB histogram backdrop: the middle (G) channel is the most
  // representative single band; the handles carry the SHARED range regardless.
  const sharedHist = channels.find((c) => c.key === 'g')?.histogram ?? channels[0]?.histogram;

  const presetRow = (
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
  );

  const scaleRow = (
    <div className="space-y-1.5">
      <span className={PANEL_CAP}>Scale</span>
      <div className="flex flex-wrap gap-1">
        {SCALE_MODES.map((m) => (
          <button key={m} type="button" onClick={() => onSetStretch(m)} className={chipClass(state.stretch === m)}>
            {m}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className={PANEL_CAP}>Display</h4>
        <span className="font-mono text-[10px] text-text-tertiary">
          {single ? state.band : trilogy ? 'RGB · trilogy' : 'RGB · simple'}
          {trilogy ? '' : ` · ${state.stretch}`}
        </span>
      </div>

      {/* RGB sub-mode: simple 3-band composite vs faithful weighted trilogy. */}
      {!single && (
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => onSetRgbSubMode('simple')}
            className={`${chipClass(!trilogy)} flex-1`}
            title="Three bands, one shared stretch — easy to read quantitatively"
          >
            simple
          </button>
          <button
            type="button"
            onClick={() => onSetRgbSubMode('trilogy')}
            disabled={!hasTrilogy}
            className={`${chipClass(trilogy)} flex-1 disabled:cursor-not-allowed disabled:opacity-40`}
            title={
              hasTrilogy
                ? 'Weighted multi-band trilogy — the publication-style composite'
                : 'Needs precomputed trilogy stats (rebuild the dataset)'
            }
          >
            trilogy
          </button>
        </div>
      )}

      {single ? (
        <>
          {scaleRow}
          <div className="space-y-2">
            {presetRow}
            {channels.map((c) =>
              c.histogram ? (
                <StretchHistogram
                  key={c.key}
                  histogram={c.histogram}
                  min={c.min}
                  max={c.max}
                  color={c.color}
                  onChange={(min, max) => onSetHandle(c.key, min, max)}
                />
              ) : null,
            )}
          </div>
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
        </>
      ) : trilogy ? (
        /* RGB · trilogy — FitsGL's own weight matrix + tuning sliders, embedded
           in campfire chrome (`fgl-embed` carries the explorer design tokens). */
        <div className="fgl-embed space-y-1">
          <TrilogyWeightMatrix
            bands={bands}
            state={state}
            onApply={onApplyWeights}
            onRainbow={onRainbowWeights}
          />
          <TrilogyControls params={state.trilogyParams} missing={!hasTrilogy} onChange={onSetTrilogyParams} />
        </div>
      ) : (
        /* RGB · simple — 3 channel picks + ONE shared stretch. */
        <>
          <div className="space-y-1.5">
            <span className={PANEL_CAP}>Channels</span>
            <div className="space-y-1">
              {RGB_ROLES.map((role) => (
                <label key={role} className="flex items-center gap-1.5" title={`${ROLE_LABEL[role]} channel`}>
                  <span
                    className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
                    style={{ background: ROLE_DOT[role] }}
                    aria-hidden
                  />
                  <select
                    value={state.rgb[role]}
                    onChange={(e) => onSetRgbRole(role, e.target.value)}
                    className={`${INSET} w-full font-mono`}
                    aria-label={`${ROLE_LABEL[role]} channel band`}
                  >
                    {bands.map((b) => (
                      <option key={b.name} value={b.name}>{b.label ?? b.name}</option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
          </div>

          {scaleRow}

          <div className="space-y-2">
            {presetRow}
            {sharedHist && sharedRange && (
              <StretchHistogram
                histogram={sharedHist}
                min={sharedRange.min}
                max={sharedRange.max}
                onChange={onSetSharedRange}
              />
            )}
            <p className="text-[10px] italic leading-snug text-text-tertiary">
              One shared range for all three bands — relative channel brightness
              stays physical.
            </p>
          </div>

          <div className="space-y-1.5">
            <span className={PANEL_CAP}>Color</span>
            <SliderRow label="Contrast" value={contrast} min={0.25} max={4} step={0.05} neutral={1} onChange={onSetContrast} />
            <SliderRow label="Saturation" value={saturation} min={0} max={2} step={0.05} neutral={1} onChange={onSetSaturation} />
          </div>
        </>
      )}
    </div>
  );
}
