'use client';

/**
 * ColormapControl (epic #337, Phase 4.5) — a custom colormap picker for the
 * Display panel (`docs/design-fitsgl-map-ux.md`, locked decision 3: a dropdown,
 * not a swatch row), replacing the native `<select>`. The trigger and every option
 * render the actual colormap as a gradient bar (sampled from the engine's
 * `colormapRGB` LUT), and a "reverse" toggle lives inside the popover — reversing
 * flips the applied LUT, which the parent drives imperatively (deriveViewerConfig
 * only carries colormap *names*, so reverse can't ride the controlled path).
 */

import { useEffect, useRef, useState } from 'react';
import { colormapRGB, type ColormapName } from '@fitsgl/core';
import { INSET } from './glass';
import { SwitchVisual } from './Switch';

/** CSS `linear-gradient` sampled from a colormap's LUT (memoised per name+dir). */
const gradientCache = new Map<string, string>();
function gradientCss(name: ColormapName, reversed: boolean, stops = 12): string {
  const key = `${name}:${reversed ? 'r' : 'f'}`;
  const cached = gradientCache.get(key);
  if (cached) return cached;
  const lut = colormapRGB(name);
  const n = lut.length / 3;
  const parts: string[] = [];
  for (let j = 0; j < stops; j++) {
    const t = j / (stops - 1);
    const idx = Math.round((reversed ? 1 - t : t) * (n - 1));
    parts.push(`rgb(${lut[idx * 3]},${lut[idx * 3 + 1]},${lut[idx * 3 + 2]}) ${Math.round(t * 100)}%`);
  }
  const css = `linear-gradient(90deg, ${parts.join(',')})`;
  gradientCache.set(key, css);
  return css;
}

interface ColormapControlProps {
  names: readonly ColormapName[];
  value: ColormapName;
  reversed: boolean;
  onChange: (name: ColormapName) => void;
  onToggleReverse: (reversed: boolean) => void;
}

export function ColormapControl({ names, value, reversed, onChange, onToggleReverse }: ColormapControlProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`${INSET} flex w-full items-center gap-2`}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span
          className="h-3.5 flex-1 rounded-sm ring-1 ring-inset ring-border"
          style={{ background: gradientCss(value, reversed) }}
          aria-hidden
        />
        <span className="font-mono capitalize">{value}{reversed ? ' ⟲' : ''}</span>
        <span className="text-text-tertiary">▾</span>
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute right-0 z-[550] mt-1 w-full min-w-44 rounded-md border border-border bg-card p-1 shadow-xl"
        >
          {/* Reverse toggle (applies to the selected colormap). */}
          <button
            type="button"
            onClick={() => onToggleReverse(!reversed)}
            className="mb-1 flex w-full items-center justify-between rounded px-2 py-1 text-xs text-text-secondary hover:bg-card-hover"
          >
            <span>Reverse</span>
            <SwitchVisual on={reversed} />
          </button>
          <div className="my-1 border-t border-border" />
          {names.map((name) => (
            <button
              key={name}
              type="button"
              role="option"
              aria-selected={name === value}
              onClick={() => { onChange(name); setOpen(false); }}
              className={`flex w-full items-center gap-2 rounded px-1.5 py-1 text-xs hover:bg-card-hover ${name === value ? 'text-text-primary' : 'text-text-secondary'}`}
            >
              <span
                className="h-3.5 flex-1 rounded-sm ring-1 ring-inset ring-border"
                style={{ background: gradientCss(name, reversed) }}
                aria-hidden
              />
              <span className="w-14 text-right font-mono capitalize">{name}</span>
              <span className="w-3 text-primary-text">{name === value ? '✓' : ''}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
