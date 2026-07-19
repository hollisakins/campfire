'use client';

/**
 * StretchHistogram (epic #337, Phase 4.5) — the FitsExplorer-style "fine adjust"
 * (`docs/design-fitsgl-map-ux.md` §4, locked decision 6), rebuilt in CAMPFIRE
 * chrome. Draws one band's precomputed display histogram (`ExplorerBand.histogram`,
 * 128 bins over `[lo, hi]`) and overlays draggable black/white-point handles that
 * live-drive the viewer's stretch (`setStretch` / `setChannelStretch`). Controlled:
 * the parent holds the `[min, max]` and pushes each change to the GPU imperatively.
 *
 * The handle range is tied to the precomputed `[lo, hi]` domain (a robust wide
 * percentile), which is where any useful black/white point sits; a value that
 * auto-stretch placed outside the domain simply pins a handle to the edge.
 */

import { useCallback, useRef } from 'react';

const W = 256;
const H = 40;

interface StretchHistogramProps {
  histogram: { counts: number[] | Float32Array; lo: number; hi: number };
  min: number;
  max: number;
  /** Bar/fill tint (e.g. an RGB channel colour); defaults to the accent. */
  color?: string;
  onChange: (min: number, max: number) => void;
}

/** Filled-area SVG path for the histogram outline; sqrt-scaled so faint tails show. */
function histPath(counts: number[] | Float32Array): string {
  const n = counts.length;
  if (n === 0) return '';
  let peak = 0;
  for (let i = 0; i < n; i++) peak = Math.max(peak, counts[i]);
  if (peak <= 0) return `M0,${H} L${W},${H} Z`;
  const dx = W / n;
  let d = `M0,${H}`;
  for (let i = 0; i < n; i++) {
    const h = Math.sqrt(counts[i] / peak) * H;
    const x = i * dx;
    d += ` L${x.toFixed(2)},${(H - h).toFixed(2)} L${(x + dx).toFixed(2)},${(H - h).toFixed(2)}`;
  }
  d += ` L${W},${H} Z`;
  return d;
}

export function StretchHistogram({ histogram, min, max, color, onChange }: StretchHistogramProps) {
  const { counts, lo, hi } = histogram;
  const span = hi - lo || 1;
  const tint = color ?? 'var(--primary)';
  const containerRef = useRef<HTMLDivElement | null>(null);
  // Latest range through a ref so a drag started on one handle reads the other's
  // current value without a stale closure.
  const rangeRef = useRef({ min, max });
  rangeRef.current = { min, max };

  const frac = (v: number) => Math.min(1, Math.max(0, (v - lo) / span));
  const loPct = frac(min) * 100;
  const hiPct = frac(max) * 100;

  const startDrag = useCallback(
    (handle: 'lo' | 'hi') => (e: React.PointerEvent) => {
      e.preventDefault();
      const el = containerRef.current;
      if (!el) return;
      const move = (ev: PointerEvent) => {
        const rect = el.getBoundingClientRect();
        const f = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        const v = lo + f * span;
        const cur = rangeRef.current;
        const EPS = span * 1e-3;
        if (handle === 'lo') onChange(Math.min(v, cur.max - EPS), cur.max);
        else onChange(cur.min, Math.max(v, cur.min + EPS));
      };
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    },
    [lo, span, onChange],
  );

  return (
    <div ref={containerRef} className="relative select-none" style={{ touchAction: 'none' }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="block h-10 w-full"
        aria-hidden
      >
        <path d={histPath(counts)} fill={tint} fillOpacity={0.35} />
        {/* Clipped-out regions (below black / above white) dimmed. */}
        <rect x={0} y={0} width={(loPct / 100) * W} height={H} fill="var(--background)" fillOpacity={0.55} />
        <rect
          x={(hiPct / 100) * W}
          y={0}
          width={W - (hiPct / 100) * W}
          height={H}
          fill="var(--background)"
          fillOpacity={0.55}
        />
      </svg>

      {/* Black-point handle. */}
      <div
        role="slider"
        aria-label="Black point"
        aria-valuenow={min}
        tabIndex={0}
        onPointerDown={startDrag('lo')}
        className="absolute top-0 h-10 w-2 -translate-x-1/2 cursor-ew-resize"
        style={{ left: `${loPct}%` }}
      >
        <div className="mx-auto h-full w-px bg-[var(--text-primary)]" />
        <div className="absolute -bottom-0.5 left-1/2 h-2 w-2 -translate-x-1/2 rounded-full bg-white shadow" />
      </div>
      {/* White-point handle. */}
      <div
        role="slider"
        aria-label="White point"
        aria-valuenow={max}
        tabIndex={0}
        onPointerDown={startDrag('hi')}
        className="absolute top-0 h-10 w-2 -translate-x-1/2 cursor-ew-resize"
        style={{ left: `${hiPct}%` }}
      >
        <div className="mx-auto h-full w-px bg-[var(--text-primary)]" />
        <div className="absolute -top-0.5 left-1/2 h-2 w-2 -translate-x-1/2 rounded-full bg-white shadow" />
      </div>
    </div>
  );
}
