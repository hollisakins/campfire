'use client';

import React, { useEffect, useState } from 'react';

export interface QuickRange {
  label: string;
  min: number | null;
  max: number | null;
}

interface RangeEditorProps {
  min: number | null;
  max: number | null;
  /** The bounds leave on Apply or Enter; both null clears. */
  onApply: (min: number | null, max: number | null) => void;
  minBound?: number;
  maxBound?: number;
  precision?: number;
  quickRanges?: QuickRange[];
  unit?: string;
  /** Which field gets focus first: the bound this quantity is usually cut on. */
  side?: 'min' | 'max';
}

/** A typed number: trimmed text, decimal or scientific; empty is no bound. */
export function parseBound(text: string): { value: number | null; ok: boolean } {
  const s = text.trim();
  if (s === '') return { value: null, ok: true };
  const n = Number(s);
  return Number.isFinite(n) ? { value: n, ok: true } : { value: null, ok: false };
}

const FIELD = 'w-full px-3 py-2 text-sm border rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent font-mono';

/**
 * The one range editor of the filter UI (a chip's dropdown, a measurement
 * cell's popover): labelled min and max text fields that take decimals and
 * scientific notation (no spinners, nothing changes under a scroll wheel),
 * quick picks, Clear and Apply.
 */
export function RangeEditor({ min, max, onApply, minBound, maxBound, precision = 2, quickRanges, unit, side = 'min' }: RangeEditorProps) {
  const show = (n: number | null) => (n == null ? '' : String(n));
  const [localMin, setLocalMin] = useState(show(min));
  const [localMax, setLocalMax] = useState(show(max));
  useEffect(() => { setLocalMin(show(min)); setLocalMax(show(max)); }, [min, max]);
  const pMin = parseBound(localMin);
  const pMax = parseBound(localMax);
  const valid = pMin.ok && pMax.ok;
  const apply = () => { if (valid) onApply(pMin.value, pMax.value); };
  const onKey = (e: React.KeyboardEvent) => { if (e.key === 'Enter') apply(); };
  const hint = (n: number | undefined) => (n === undefined ? 'any' : String(+n.toFixed(precision)));
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <div className="flex-1">
          <label className="block text-xs text-text-secondary mb-1">Min{unit ? ` (${unit})` : ''}</label>
          <input type="text" inputMode="decimal" autoFocus={side === 'min'} value={localMin} onChange={(e) => setLocalMin(e.target.value)} onKeyDown={onKey}
            placeholder={hint(minBound)} aria-label="Minimum" aria-invalid={!pMin.ok}
            className={`${FIELD} ${pMin.ok ? 'border-border-strong' : 'border-red-500'}`} />
        </div>
        <span className="text-text-secondary mt-5">–</span>
        <div className="flex-1">
          <label className="block text-xs text-text-secondary mb-1">Max{unit ? ` (${unit})` : ''}</label>
          <input type="text" inputMode="decimal" autoFocus={side === 'max'} value={localMax} onChange={(e) => setLocalMax(e.target.value)} onKeyDown={onKey}
            placeholder={hint(maxBound)} aria-label="Maximum" aria-invalid={!pMax.ok}
            className={`${FIELD} ${pMax.ok ? 'border-border-strong' : 'border-red-500'}`} />
        </div>
      </div>
      {quickRanges && quickRanges.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {quickRanges.map((preset) => (
            <button key={preset.label} type="button" onClick={() => onApply(preset.min, preset.max)}
              className="px-2.5 py-1 text-xs rounded-full border border-border text-text-secondary hover:border-primary hover:text-primary transition-colors">
              {preset.label}
            </button>
          ))}
        </div>
      )}
      <div className="flex gap-2 pt-2 border-t border-border">
        <button type="button" onClick={() => { setLocalMin(''); setLocalMax(''); onApply(null, null); }}
          className="flex-1 px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors">
          Clear
        </button>
        <button type="button" onClick={apply} disabled={!valid}
          className="flex-1 px-3 py-1.5 text-sm bg-primary text-on-primary rounded-md hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
          Apply
        </button>
      </div>
    </div>
  );
}

/** "≥ 3", "≤ 27", "5 – 8", or `empty` for no bounds. */
export function rangeText(min: number | null, max: number | null, precision = 2, empty = 'any'): string {
  const f = (n: number) => (Number.isInteger(n) ? String(n) : Math.abs(n) >= 1e-3 && Math.abs(n) < 1e6 ? String(+n.toFixed(precision)) : n.toExponential(2));
  if (min !== null && max !== null) return `${f(min)} – ${f(max)}`;
  if (min !== null) return `≥ ${f(min)}`;
  if (max !== null) return `≤ ${f(max)}`;
  return empty;
}
