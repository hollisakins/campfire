'use client';

/**
 * TrilogyWeights — campfire-chrome per-band weight matrix for the trilogy
 * composite (replaces the embedded `@fitsgl/core/react` TrilogyWeightMatrix so
 * the copy, minimize behavior, and styling are ours). Each participating band
 * gets three rotary `Knob`s (its R/G/B contribution); the matrix is MINIMIZED
 * by default — only active (participating) bands show — and a single +/− button
 * expands it to reveal the rest of the co-gridded group with participation
 * checkboxes. Pure logic (composite/toggle/cap) comes from the core helpers, so
 * this stays a thin view over the same `ExplorerState` the viewer renders.
 */

import { useState } from 'react';
import { MAX_BANDS, type BandWeight } from '@fitsgl/core';
import {
  Knob,
  groupBands,
  rgbActiveGroup,
  trilogyComposite,
  type ExplorerBand,
  type ExplorerState,
} from '@fitsgl/core/react';
import { PANEL_CAP, chipClass } from './glass';

const CH_COLOR = { r: '#ef4444', g: '#22c55e', b: '#3b82f6' } as const;

/** CSS `rgb(...)` for a band's (R,G,B) weight — the row swatch shows its tint. */
function weightSwatch(w: BandWeight): string {
  const c = (x: number): number => Math.round(Math.min(1, Math.max(0, x)) * 255);
  return `rgb(${c(w[0])},${c(w[1])},${c(w[2])})`;
}

interface TrilogyWeightsProps {
  bands: ExplorerBand[];
  state: ExplorerState;
  onApply: (entries: Array<{ band: string; weight: BandWeight }>) => void;
  onRainbow: () => void;
}

export function TrilogyWeights({ bands, state, onApply, onRainbow }: TrilogyWeightsProps) {
  const [expanded, setExpanded] = useState(false);

  const activeGroup = rgbActiveGroup(bands, state.rgb);
  const rows = groupBands(bands, activeGroup);
  const comp = new Map(trilogyComposite(state).map((e) => [e.band, e.weight] as const));
  const atCap = comp.size >= MAX_BANDS;
  const visible = expanded ? rows : rows.filter((b) => comp.has(b.name));
  const hiddenCount = rows.length - visible.length;

  const toggle = (name: string): void => {
    const cur = trilogyComposite(state);
    const has = cur.some((e) => e.band === name);
    if (!has && cur.length >= MAX_BANDS) return; // cap: checkbox is disabled anyway
    onApply(
      has
        ? cur.filter((e) => e.band !== name)
        : [...cur, { band: name, weight: state.weights[name] ?? ([1, 1, 1] as BandWeight) }],
    );
  };

  const editWeight = (name: string, ch: 0 | 1 | 2, value: number): void => {
    const v = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;
    const cur = trilogyComposite(state);
    const idx = cur.findIndex((e) => e.band === name);
    const prev = idx >= 0 ? cur[idx].weight : state.weights[name] ?? ([0, 0, 0] as BandWeight);
    const w: BandWeight = ch === 0 ? [v, prev[1], prev[2]] : ch === 1 ? [prev[0], v, prev[2]] : [prev[0], prev[1], v];
    if (idx < 0 && atCap) return;
    onApply(idx >= 0 ? cur.map((e, i) => (i === idx ? { band: name, weight: w } : e)) : [...cur, { band: name, weight: w }]);
  };

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-1">
        <span className={PANEL_CAP}>Weights</span>
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            onClick={onRainbow}
            className={`${chipClass(false)} whitespace-nowrap`}
            title="Spread the group's bands blue→red across B→R"
          >
            ✦ rainbow
          </button>
          {hiddenCount > 0 || expanded ? (
            <button
              type="button"
              onClick={() => setExpanded((e) => !e)}
              className={chipClass(expanded)}
              aria-expanded={expanded}
              aria-label={expanded ? 'Show active bands only' : 'Show all bands'}
              title={expanded ? 'Show active bands only' : `Show all bands (${hiddenCount} hidden)`}
            >
              {expanded ? '−' : '+'}
            </button>
          ) : null}
        </div>
      </div>

      {/* Column header aligned with the knob triplets. */}
      <div className="flex items-center justify-end gap-1 pr-0.5">
        {(['r', 'g', 'b'] as const).map((c) => (
          <span
            key={c}
            className="w-[30px] text-center font-mono text-[9px] font-semibold"
            style={{ color: CH_COLOR[c] }}
          >
            {c.toUpperCase()}
          </span>
        ))}
      </div>

      <div className="space-y-0.5">
        {visible.map((b) => {
          const on = comp.has(b.name);
          const w = comp.get(b.name) ?? ([0, 0, 0] as BandWeight);
          return (
            <div key={b.name} className={`flex items-center gap-1.5 ${on ? '' : 'opacity-50'}`}>
              {expanded && (
                <input
                  type="checkbox"
                  checked={on}
                  disabled={!on && atCap}
                  aria-label={`include ${b.label ?? b.name}`}
                  title={!on && atCap ? `Composite is capped at ${MAX_BANDS} bands` : undefined}
                  onChange={() => toggle(b.name)}
                  className="h-3 w-3 shrink-0 accent-[var(--primary)]"
                />
              )}
              <span className="flex min-w-0 flex-1 items-center gap-1.5 font-mono text-[10px] text-text-secondary">
                <span
                  className="inline-block h-2 w-2 shrink-0 rounded-sm border border-border"
                  style={{ background: weightSwatch(w) }}
                  aria-hidden
                />
                <span className="truncate">{b.label ?? b.name}</span>
              </span>
              {/* Knobs are core components; the fgl-embed wrapper carries their tokens. */}
              <span className="fgl-embed flex shrink-0 gap-1">
                {([0, 1, 2] as const).map((ch) => (
                  <Knob
                    key={ch}
                    value={w[ch]}
                    color={CH_COLOR[(['r', 'g', 'b'] as const)[ch]]}
                    label={`${b.label ?? b.name} ${'RGB'[ch]} weight`}
                    onChange={(next) => editWeight(b.name, ch, next)}
                  />
                ))}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
