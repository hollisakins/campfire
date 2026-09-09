'use client';

import { useEffect, useRef, useState } from 'react';
import { useTheme } from '@/lib/contexts/ThemeContext';
import type { ContinuumRow, DisperserModel } from '@/lib/etc/model';

interface Props {
  disp: DisperserModel;
  rows: (ContinuumRow | null)[];
  /** row at the wavelength of interest (marker) */
  at: ContinuumRow | null;
  totalS: number;
  /** pandeia noise curve [nJy] on the model grid and the T it was run at, when shown */
  pandeia: { curve: (number | null)[]; totalS: number; label: string } | null;
  hasSource: boolean;
}

interface Colors {
  bg: string;
  grid: string;
  text: string;
  textSecondary: string;
  noise: string;
  snr: string;
}

function readColors(): Colors {
  const s = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => s.getPropertyValue(name).trim() || fallback;
  return {
    bg: v('--plot-bg', '#ffffff'),
    grid: v('--plot-grid', '#e2e8f0'),
    text: v('--plot-text', '#0f172a'),
    textSecondary: v('--plot-text-secondary', '#64748b'),
    noise: v('--info', '#1d4ed8'),
    snr: v('--primary', '#c63f0c'),
  };
}

/** Canvas height as a fraction of its width. */
const ASPECT = 0.44;

/**
 * Two-panel canvas: 1σ noise per 1-D pixel (with the pandeia curve dashed)
 * and S/N per pixel for the source, both vs wavelength on log axes.
 */
export function EtcChart({ disp, rows, at, totalS, pandeia, hasSource }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const { resolvedTheme } = useTheme();
  const [width, setWidth] = useState(0);

  // draw at the rendered CSS width so the axis text stays legible at any layout width
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => setWidth(Math.round(entries[0].contentRect.width)));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const cv = ref.current;
    if (!cv || width < 100) return;
    const W = width;
    const H = Math.round(W * ASPECT);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = W * dpr;
    cv.height = H * dpr;
    cv.style.height = `${H}px`;
    const ctx = cv.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const c = readColors();
    ctx.fillStyle = c.bg;
    ctx.fillRect(0, 0, W, H);

    const valid = rows.filter((r): r is ContinuumRow => r !== null);
    const [wlo, whi] = disp.coverage;
    const narrow = W < 560;
    const mono = `${narrow ? 10 : 11}px var(--font-mono), ui-monospace, monospace`;
    const monoBold = `600 ${narrow ? 11 : 12}px var(--font-mono), ui-monospace, monospace`;
    const left = narrow ? 40 : 50;
    const gap = narrow ? 34 : 44;
    const half = (W - left - gap - 10) / 2;
    const panels: { x0: number; x1: number; key: 'sig1d' | 'snrPix'; label: string; color: string; etc: boolean }[] = [
      { x0: left, x1: left + half, key: 'sig1d', label: narrow ? '1σ noise / px [nJy]' : '1σ noise per 1-D pixel [nJy]', color: c.noise, etc: true },
      {
        x0: left + half + gap + left - 10,
        x1: W - 10,
        key: 'snrPix',
        label: narrow ? 'S/N per pixel' : hasSource ? 'S/N per pixel for this source' : 'S/N per pixel (no source given)',
        color: c.snr,
        etc: false,
      },
    ];
    const y0 = 28;
    const y1 = H - 26;
    for (const p of panels) {
      const scale = p.key === 'sig1d' ? 1e3 : 1;
      const vals = valid.map((r) => r[p.key] * scale).filter((v) => Number.isFinite(v) && v > 0);
      if (!vals.length) {
        ctx.fillStyle = c.textSecondary;
        ctx.font = mono;
        ctx.textAlign = 'left';
        ctx.fillText(p.label, p.x0, y0 - 10);
        continue;
      }
      let lo = Math.min(...vals);
      let hi = Math.max(...vals);
      if (p.etc && pandeia) {
        pandeia.curve.forEach((v, i) => {
          if (v && rows[i]) {
            const vs = v * Math.sqrt(pandeia.totalS / totalS);
            lo = Math.min(lo, vs);
            hi = Math.max(hi, vs);
          }
        });
      }
      lo = Math.pow(10, Math.floor(Math.log10(lo)));
      hi = Math.pow(10, Math.ceil(Math.log10(hi)));
      if (hi <= lo) hi = lo * 10;
      const X = (w: number) => p.x0 + ((w - wlo) / (whi - wlo)) * (p.x1 - p.x0);
      const Y = (v: number) => y1 - ((Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * (y1 - y0);

      ctx.strokeStyle = c.grid;
      ctx.lineWidth = 1;
      ctx.fillStyle = c.textSecondary;
      ctx.font = mono;
      ctx.textAlign = 'right';
      for (let d = Math.log10(lo); d <= Math.log10(hi) + 1e-9; d++) {
        const y = Y(Math.pow(10, d));
        ctx.beginPath();
        ctx.moveTo(p.x0, y);
        ctx.lineTo(p.x1, y);
        ctx.stroke();
        ctx.fillText(Math.pow(10, d).toString(), p.x0 - 6, y + 4);
      }
      ctx.textAlign = 'center';
      const span = whi - wlo;
      const step = span > 2 ? 1 : span > 0.8 ? 0.5 : 0.1;
      for (let w = Math.ceil(wlo / step) * step; w <= whi + 1e-9; w += step) {
        const x = X(w);
        ctx.beginPath();
        ctx.moveTo(x, y0);
        ctx.lineTo(x, y1);
        ctx.stroke();
        ctx.fillText(`${w.toFixed(step < 1 ? 1 : 0)}${narrow ? '' : ' µm'}`, x, y1 + 15);
      }
      ctx.strokeStyle = c.textSecondary;
      ctx.strokeRect(p.x0, y0, p.x1 - p.x0, y1 - y0);

      if (p.etc && pandeia) {
        ctx.strokeStyle = c.textSecondary;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        let pen = false;
        pandeia.curve.forEach((v, i) => {
          if (!v || !rows[i]) {
            pen = false;
            return;
          }
          const x = X(disp.wave[i]);
          const y = Y(v * Math.sqrt(pandeia.totalS / totalS));
          if (pen) ctx.lineTo(x, y);
          else ctx.moveTo(x, y);
          pen = true;
        });
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (p.key === 'snrPix') {
        ctx.strokeStyle = c.textSecondary;
        ctx.setLineDash([2, 3]);
        for (const s of [3, 5]) {
          if (s > lo && s < hi) {
            ctx.beginPath();
            ctx.moveTo(p.x0, Y(s));
            ctx.lineTo(p.x1, Y(s));
            ctx.stroke();
          }
        }
        ctx.setLineDash([]);
      }

      ctx.strokeStyle = p.color;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      let first = true;
      for (const r of valid) {
        const v = r[p.key] * scale;
        if (!(v > 0)) continue;
        if (first) ctx.moveTo(X(r.wave), Y(v));
        else ctx.lineTo(X(r.wave), Y(v));
        first = false;
      }
      ctx.stroke();

      if (at) {
        const vm = at[p.key] * scale;
        if (vm > 0) {
          ctx.fillStyle = p.color;
          ctx.beginPath();
          ctx.arc(X(at.wave), Y(vm), 5, 0, 2 * Math.PI);
          ctx.fill();
          ctx.strokeStyle = c.bg;
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      }
      ctx.fillStyle = c.text;
      ctx.textAlign = 'left';
      ctx.font = monoBold;
      ctx.fillText(p.label, p.x0, y0 - 10);
    }
  }, [disp, rows, at, totalS, pandeia, hasSource, resolvedTheme, width]);

  return (
    <div ref={wrapRef} className="w-full rounded-lg border border-border overflow-hidden" style={{ aspectRatio: `1 / ${ASPECT}` }}>
      <canvas ref={ref} className="block w-full" role="img" aria-label="Noise and S/N versus wavelength" />
    </div>
  );
}
