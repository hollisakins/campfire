'use client';

/**
 * SwitchVisual (epic #337, Phase 4.5) — the decorative on/off knob shared by the
 * glass panels' toggle rows (Layers toggles, the colormap "reverse"). Presentational
 * only: wrap it in a `<button>` row that owns the click + `aria`, so it never nests
 * interactive elements.
 */

export function SwitchVisual({ on }: { on: boolean }) {
  return (
    <span
      className={`relative inline-block h-4 w-7 shrink-0 rounded-full transition-colors ${on ? 'bg-primary' : 'bg-border-strong'}`}
      aria-hidden
    >
      <span
        className="absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all"
        style={{ left: on ? '0.875rem' : '0.125rem' }}
      />
    </span>
  );
}
