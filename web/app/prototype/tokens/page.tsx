'use client';

/**
 * Token Lab — interactive design-token playground.
 *
 * THROWAWAY: this page exists only to tune the Ember & Dusk palette during the
 * design/direction-2-restyle PR. Delete this whole `tokens/` folder (and its
 * nav entry in ../layout.tsx) before merging.
 *
 * Color pickers drive the real CSS variables on a scoped preview wrapper, so the
 * mock components below render exactly as the app would. Use "Copy CSS" to lift a
 * palette you like straight into web/app/globals.css.
 */

import React, { useMemo, useState } from 'react';
import { Sun, Moon, RotateCcw, Copy, Check } from 'lucide-react';

// ---- Token model -----------------------------------------------------------

type Vals = Record<string, string>;

const CSS_VAR: Record<string, string> = {
  background: '--background',
  card: '--card',
  cardHover: '--card-hover',
  surface2: '--surface-2',
  tableHeader: '--table-header',
  border: '--border',
  borderStrong: '--border-strong',
  textPrimary: '--text-primary',
  textSecondary: '--text-secondary',
  textTertiary: '--text-tertiary',
  primary: '--primary',
  primaryHover: '--primary-hover',
  primaryText: '--primary-text',
  onPrimary: '--on-primary',
  plotBg: '--plot-bg',
  plotPaper: '--plot-paper',
  plotGrid: '--plot-grid',
  plotText: '--plot-text',
  plotTextSecondary: '--plot-text-secondary',
};

const GROUPS: { title: string; keys: string[] }[] = [
  { title: 'Surfaces', keys: ['background', 'card', 'cardHover', 'surface2', 'tableHeader'] },
  { title: 'Borders', keys: ['border', 'borderStrong'] },
  { title: 'Text', keys: ['textPrimary', 'textSecondary', 'textTertiary'] },
  { title: 'Accent', keys: ['primary', 'primaryHover', 'primaryText', 'onPrimary'] },
  { title: 'Plot', keys: ['plotBg', 'plotPaper', 'plotGrid', 'plotText', 'plotTextSecondary'] },
];

// Seeded from globals.css on this branch.
const LIGHT: Vals = {
  background: '#fffefb', card: '#fefcfa', cardHover: '#f6f0ea', surface2: '#f3ede5',
  tableHeader: '#f6f0ea', border: '#e6ddce', borderStrong: '#d7cbb9',
  textPrimary: '#211c17', textSecondary: '#5c5346', textTertiary: '#6c6150',
  primary: '#c63f0c', primaryHover: '#ad3408', primaryText: '#b8390a', onPrimary: '#ffffff',
  plotBg: '#fbf9f6', plotPaper: '#fefcfa', plotGrid: '#eee7e2',
  plotText: '#211c17', plotTextSecondary: '#5c5346',
};
const DARK: Vals = {
  background: '#16131c', card: '#1f1b27', cardHover: '#2a2435', surface2: '#241f2e',
  tableHeader: '#221d2a', border: '#332c40', borderStrong: '#423a52',
  textPrimary: '#f1ecf6', textSecondary: '#b8aec6', textTertiary: '#8b8398',
  primary: '#fb923c', primaryHover: '#fdba74', primaryText: '#fb923c', onPrimary: '#1a1206',
  plotBg: '#16131c', plotPaper: '#1f1b27', plotGrid: '#221e2b',
  plotText: '#f1ecf6', plotTextSecondary: '#b8aec6',
};

// ---- Color math ------------------------------------------------------------

function parseHex(hex: string): [number, number, number] {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function lin(c: number) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}
function lum(hex: string) {
  const [r, g, b] = parseHex(hex);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}
function contrast(a: string, b: string) {
  const la = lum(a), lb = lum(b), hi = Math.max(la, lb), lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}
function lightnessPct(hex: string) {
  const [r, g, b] = parseHex(hex).map((c) => c / 255);
  return ((Math.max(r, g, b) + Math.min(r, g, b)) / 2) * 100;
}
function rgba(hex: string, a: number) {
  const [r, g, b] = parseHex(hex);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

// ---- Spectrum trace (deterministic mock) -----------------------------------

function spectrumPath(w: number, h: number, seed: number, emission = false) {
  const pts: string[] = [];
  const n = 120;
  for (let i = 0; i <= n; i++) {
    const x = (i / n) * w;
    let y = h * 0.62;
    // pseudo-noise
    y += Math.sin(i * 0.7 + seed) * h * 0.04 + Math.sin(i * 2.3 + seed * 2) * h * 0.02;
    if (emission) {
      const peaks = [0.32, 0.55, 0.78];
      for (const p of peaks) {
        const d = i / n - p;
        y -= Math.exp(-(d * d) / 0.0006) * h * 0.4;
      }
    }
    pts.push(`${x.toFixed(1)},${Math.max(2, Math.min(h - 2, y)).toFixed(1)}`);
  }
  return 'M' + pts.join(' L');
}

// ---- Small UI bits ---------------------------------------------------------

function Swatch({ label, cssKey, value, onChange }: { label: string; cssKey: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex items-center gap-2 text-xs">
      <input
        type="color"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-7 w-7 shrink-0 cursor-pointer rounded border border-border bg-transparent p-0"
      />
      <span className="w-28 shrink-0 font-mono text-text-secondary">{CSS_VAR[cssKey]}</span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        className="w-20 rounded border border-border bg-background px-1.5 py-0.5 font-mono text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
      />
      <span className="ml-auto tabular-nums text-text-tertiary">{lightnessPct(value).toFixed(0)}%</span>
      <span className="sr-only">{label}</span>
    </label>
  );
}

function ContrastChip({ label, fg, bg }: { label: string; fg: string; bg: string }) {
  const r = contrast(fg, bg);
  const ok = r >= 4.5;
  return (
    <div className="flex items-center justify-between gap-2 rounded border border-border px-2 py-1 text-xs">
      <span className="text-text-secondary">{label}</span>
      <span className={`font-mono tabular-nums ${ok ? 'text-success' : 'text-danger'}`}>
        {r.toFixed(2)} {ok ? 'AA' : '✕'}
      </span>
    </div>
  );
}

// ---- Page ------------------------------------------------------------------

export default function TokenLabPage() {
  const [isDark, setIsDark] = useState(false);
  const [light, setLight] = useState<Vals>({ ...LIGHT });
  const [dark, setDark] = useState<Vals>({ ...DARK });
  const [copied, setCopied] = useState(false);

  const vals = isDark ? dark : light;
  const setVals = isDark ? setDark : setLight;
  const update = (k: string, v: string) => setVals((prev) => ({ ...prev, [k]: v }));
  const reset = () => (isDark ? setDark({ ...DARK }) : setLight({ ...LIGHT }));

  // Inline CSS variables for the scoped preview wrapper.
  const styleVars = useMemo(() => {
    const s: Record<string, string> = {};
    for (const [k, cssVar] of Object.entries(CSS_VAR)) s[cssVar] = vals[k];
    s['--primary-soft'] = rgba(vals.primary, isDark ? 0.14 : 0.1);
    return s as React.CSSProperties;
  }, [vals, isDark]);

  const exportCss = useMemo(() => {
    const emit = (v: Vals, soft: number) =>
      Object.entries(CSS_VAR)
        .map(([k, cssVar]) => `  ${cssVar}: ${v[k]};`)
        .join('\n') + `\n  --primary-soft: ${rgba(v.primary, soft)};`;
    return `:root {\n${emit(light, 0.1)}\n}\n\n.dark {\n${emit(dark, 0.14)}\n}`;
  }, [light, dark]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(exportCss);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard blocked — textarea is selectable as fallback */
    }
  };

  const rows = [
    { id: 'COS-184540', z: 5.6671, q: 'Secure', snr: 31.4 },
    { id: 'COS-201337', z: 3.2204, q: 'Probable', snr: 12.7 },
    { id: 'COS-118822', z: 7.1049, q: 'Tentative', snr: 6.2 },
    { id: 'COS-093471', z: 4.4188, q: 'Secure', snr: 22.9 },
    { id: 'COS-210556', z: 2.8853, q: 'Probable', snr: 9.1 },
  ];

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-2 text-sm text-text-secondary">
        <strong className="text-text-primary">Token Lab</strong> — throwaway playground for tuning the
        palette. Delete <code className="font-mono">app/prototype/tokens/</code> before merging.
      </div>

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex overflow-hidden rounded-lg border border-border">
          <button
            onClick={() => setIsDark(false)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-sm ${!isDark ? 'bg-primary text-on-primary' : 'bg-card text-text-secondary hover:bg-card-hover'}`}
          >
            <Sun className="h-4 w-4" /> Light
          </button>
          <button
            onClick={() => setIsDark(true)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-sm ${isDark ? 'bg-primary text-on-primary' : 'bg-card text-text-secondary hover:bg-card-hover'}`}
          >
            <Moon className="h-4 w-4" /> Dusk
          </button>
        </div>
        <button
          onClick={reset}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-text-secondary hover:bg-card-hover"
        >
          <RotateCcw className="h-4 w-4" /> Reset {isDark ? 'dusk' : 'light'}
        </button>
        <button
          onClick={copy}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm text-on-primary hover:bg-primary-hover"
        >
          {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
          {copied ? 'Copied' : 'Copy CSS'}
        </button>
        <span className="text-xs text-text-tertiary">Editing the <strong>{isDark ? 'dusk' : 'light'}</strong> palette</span>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_1fr]">
        {/* Controls */}
        <div className="space-y-4">
          {GROUPS.map((g) => (
            <div key={g.title} className="rounded-lg border border-border bg-card p-3">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-tertiary">{g.title}</h3>
              <div className="space-y-1.5">
                {g.keys.map((k) => (
                  <Swatch key={k} label={k} cssKey={k} value={vals[k]} onChange={(v) => update(k, v)} />
                ))}
              </div>
            </div>
          ))}
          <div className="rounded-lg border border-border bg-card p-3">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-tertiary">Contrast (AA ≥ 4.50)</h3>
            <div className="grid grid-cols-1 gap-1.5">
              <ContrastChip label="primary text / card" fg={vals.textPrimary} bg={vals.card} />
              <ContrastChip label="secondary / card" fg={vals.textSecondary} bg={vals.card} />
              <ContrastChip label="tertiary / card" fg={vals.textTertiary} bg={vals.cardHover} />
              <ContrastChip label="tertiary / surface-2" fg={vals.textTertiary} bg={vals.surface2} />
              <ContrastChip label="link / card" fg={vals.primaryText} bg={vals.card} />
              <ContrastChip label="on-primary / primary" fg={vals.onPrimary} bg={vals.primary} />
            </div>
          </div>
        </div>

        {/* Preview (scoped tokens) */}
        <div style={styleVars} className="space-y-6 rounded-xl border border-border bg-background p-5">
          {/* Filters row */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <input
                placeholder="Search objects…"
                className="w-56 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
            <select className="appearance-none rounded-md border border-border bg-card-hover px-3 py-1.5 text-sm text-text-primary">
              <option>COSMOS</option>
              <option>UDS</option>
            </select>
            {['z > 5', 'Secure', 'PRISM'].map((c, i) => (
              <span
                key={c}
                className={`rounded-full border px-3 py-1 text-xs ${i === 1 ? 'border-transparent bg-primary-soft text-primary-text' : 'border-border text-text-secondary'}`}
              >
                {c}
              </span>
            ))}
            <button className="ml-auto inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-on-primary hover:bg-primary-hover">
              Apply
            </button>
            <button className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-card-hover">
              Reset
            </button>
          </div>

          {/* Card + table */}
          <div className="overflow-hidden rounded-card border border-border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-3">
              <div>
                <h2 className="text-sm font-semibold text-text-primary">Spectra</h2>
                <p className="text-xs text-text-tertiary">5 of 1,284 targets</p>
              </div>
              <label className="flex items-center gap-2 text-sm text-text-secondary">
                <input type="checkbox" defaultChecked className="rounded border-border-strong" /> Verified only
              </label>
            </div>
            <table className="w-full text-sm">
              <thead className="bg-table-header">
                <tr className="text-left text-text-secondary">
                  <th className="px-4 py-2 font-medium">Object ID</th>
                  <th className="px-4 py-2 font-medium">z</th>
                  <th className="px-4 py-2 font-medium">Quality</th>
                  <th className="px-4 py-2 text-right font-medium">Max S/N</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => (
                  <tr key={r.id} className="bg-card transition-colors hover:bg-card-hover">
                    <td className="px-4 py-2 font-mono text-primary-text">{r.id}</td>
                    <td className="px-4 py-2 font-mono tabular-nums text-text-primary">{r.z.toFixed(4)}</td>
                    <td className="px-4 py-2 text-text-secondary">{r.q}</td>
                    <td className="px-4 py-2 text-right font-mono tabular-nums text-text-primary">{r.snr.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Spectrum viewer */}
          <div className="overflow-hidden rounded-card border border-border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-3">
              <h2 className="text-sm font-semibold text-text-primary">Spectrum viewer</h2>
              <span className="font-mono text-xs text-text-tertiary">COS-184540 · z = 5.6671</span>
            </div>
            <div className="p-4">
            <svg viewBox="0 0 600 240" className="w-full rounded-md" style={{ background: 'var(--plot-paper)' }}>
              {/* plot data well */}
              <rect x={48} y={12} width={540} height={196} fill="var(--plot-bg)" />
              {/* grid */}
              {[0, 1, 2, 3, 4].map((i) => (
                <line key={`h${i}`} x1={48} x2={588} y1={12 + i * 49} y2={12 + i * 49} stroke="var(--plot-grid)" strokeWidth={1} />
              ))}
              {[0, 1, 2, 3, 4, 5, 6].map((i) => (
                <line key={`v${i}`} x1={48 + i * 77} x2={48 + i * 77} y1={12} y2={208} stroke="var(--plot-grid)" strokeWidth={1} />
              ))}
              {/* traces (fixed data colors — not theme tokens) */}
              <g transform="translate(48,12)">
                <path d={spectrumPath(540, 196, 1.0, true)} fill="none" stroke="#3b82f6" strokeWidth={1.4} opacity={0.9} />
                <path d={spectrumPath(540, 196, 1.0, false)} fill="none" stroke="#ef4444" strokeWidth={1.6} opacity={0.85} />
              </g>
              {/* axes labels */}
              <text x={318} y={232} textAnchor="middle" fontSize={11} fill="var(--plot-text-secondary)">
                Observed wavelength (µm)
              </text>
              <text x={14} y={110} textAnchor="middle" fontSize={11} fill="var(--plot-text-secondary)" transform="rotate(-90 14 110)">
                Flux
              </text>
            </svg>
            <p className="mt-2 text-xs text-text-tertiary">
              Data well uses <span className="font-mono">--plot-bg</span>; margins use{' '}
              <span className="font-mono">--plot-paper</span>. Trace colors are scientific encodings (not tokens).
            </p>
            </div>
          </div>

          {/* Text specimen */}
          <div className="rounded-card border border-border bg-card p-4 shadow-sm">
            <p className="text-text-primary">Primary text — headings and body copy.</p>
            <p className="text-text-secondary">Secondary text — labels and metadata.</p>
            <p className="text-text-tertiary">Tertiary text — muted captions and placeholders.</p>
            <p className="mt-1">
              <a className="text-primary-text underline">An inline accent link</a>{' '}
              <span className="text-text-secondary">sits inside body text.</span>
            </p>
          </div>
        </div>
      </div>

      {/* Export */}
      <div className="rounded-lg border border-border bg-card p-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-tertiary">
          globals.css export
        </h3>
        <textarea
          readOnly
          value={exportCss}
          onFocus={(e) => e.currentTarget.select()}
          className="h-56 w-full resize-y rounded-md border border-border bg-background p-3 font-mono text-xs text-text-primary"
        />
      </div>
    </div>
  );
}
