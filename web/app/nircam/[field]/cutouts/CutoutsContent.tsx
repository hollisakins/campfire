'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import {
  ChevronDown,
  ChevronRight,
  Download,
  ImageIcon,
  Loader2,
  Map as MapIcon,
  Scissors,
} from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { FieldSelectorDropdown } from '@/components/nircam/FieldSelectorDropdown';
import type { FitsglDataset } from '@/lib/actions/map';
import type { NircamFieldCard } from '@/lib/types';
import { MAX_PIXELS_PER_BAND, MAX_PIXELS_TOTAL } from '@/lib/cutout/limits';
import { SHUTTER_OVERLAY_MAX_FOV_ARCSEC } from '@/lib/cutout/shutters';
import { parseCoordinates } from '@/lib/utils/coordinate-parser';

/** Single-band panel transfer curves the figure route accepts. */
const STRETCHES = ['linear', 'asinh', 'log', 'sqrt'] as const;
/** Single-band panel limits: SNR (σ about the cutout's own sky) or percentile cuts. */
type Scaling = 'snr' | 'percentile';
const DEFAULT_SNR_LO = '-5';
const DEFAULT_SNR_HI = '8';
/** Composite construction: the dataset default (the producer's weighted
 *  trilogy mix), the map's rainbow over chosen bands, or a custom R/G/B triple. */
const COMPOSITE_MODES = ['auto', 'rainbow', 'custom'] as const;
const COMPOSITE_LABEL: Record<(typeof COMPOSITE_MODES)[number], string> = {
  auto: 'Map default',
  rainbow: 'Rainbow (N bands)',
  custom: 'Custom R / G / B',
};
/** `@fitsgl/core` COLORMAP_NAMES, inlined so the page does not ship the core. */
const COLORMAPS = ['gray', 'viridis', 'magma', 'inferno', 'plasma', 'cividis'] as const;
/** Composite transfer: `auto` lets the server pick trilogy when the dataset
 *  carries the precomputed stats (the map's default), else asinh. */
const RGB_STRETCHES = ['auto', 'trilogy', 'asinh', 'log', 'sqrt', 'linear'] as const;

type Stretch = (typeof STRETCHES)[number];
type Colormap = (typeof COLORMAPS)[number];
type RgbStretch = (typeof RGB_STRETCHES)[number];
type CompositeMode = (typeof COMPOSITE_MODES)[number];
type RgbRole = 'r' | 'g' | 'b';

const RGB_ROLES: readonly RgbRole[] = ['r', 'g', 'b'];
const ROLE_LABEL: Record<RgbRole, string> = { r: 'R', g: 'G', b: 'B' };
const ROLE_DOT: Record<RgbRole, string> = { r: '#ef4444', g: '#22c55e', b: '#3b82f6' };

/** Parse a dataset `pixel_scale` tag ('30mas', '0.03as') to arcsec/px, or null. */
function pixelScaleArcsec(tag: string): number | null {
  const mas = tag.match(/^([\d.]+)\s*mas$/i);
  if (mas) return parseFloat(mas[1]) / 1000;
  const as = tag.match(/^([\d.]+)\s*(as|arcsec)$/i);
  if (as) return parseFloat(as[1]);
  return null;
}

const cutoutsRoute = (field: string) => `/nircam/${encodeURIComponent(field)}/cutouts`;

function isOneOf<T extends string>(list: readonly T[], v: string | undefined): v is T {
  return v !== undefined && (list as readonly string[]).includes(v);
}

interface CutoutsContentProps {
  field: string;
  /** The field's cutout source; null = cutouts not available for this field. */
  dataset: FitsglDataset | null;
  allFields: NircamFieldCard[];
  /** The shareable request mirrored into the page URL by a previous visit. */
  initial: {
    ra?: string;
    dec?: string;
    fov?: string;
    bands?: string;
    rgb?: string;
    rgb_stretch?: string;
    shutters?: string;
  };
}

export const CutoutsContent: React.FC<CutoutsContentProps> = ({
  field,
  dataset,
  allFields,
  initial,
}) => {
  const displayName =
    allFields.find((f) => f.field === field)?.display_name ?? field.toUpperCase();
  const bandList = useMemo(() => dataset?.bands ?? [], [dataset]);
  const canRgb = bandList.length >= 3;

  // ---- Request form -------------------------------------------------------
  const [coordText, setCoordText] = useState(
    initial.ra && initial.dec ? `${initial.ra} ${initial.dec}` : '',
  );
  const [fov, setFov] = useState(initial.fov ?? '10');

  // Panels: the single bands to show + whether to append the RGB composite.
  // A fresh visit opens on the composite alone (or the first band when the
  // field cannot composite) — never on every band, which is slow to render
  // and rarely what a quick look wants.
  const initialBands = useMemo(() => {
    if (initial.bands === undefined) return null;
    const wanted = new Set(initial.bands.split(',').map((b) => b.trim().toLowerCase()).filter(Boolean));
    return bandList.filter((b) => wanted.has(b.toLowerCase()));
  }, [initial.bands, bandList]);
  const initialRgb = useMemo<{
    mode: CompositeMode;
    channels: Record<RgbRole, string> | null;
    rainbow: string[] | null;
  } | null>(() => {
    if (initial.rgb === undefined) return null;
    const raw = initial.rgb.trim().toLowerCase();
    const byLower = new Map(bandList.map((b) => [b.toLowerCase(), b]));
    const resolve = (csv: string) =>
      csv.split(',').map((b) => b.trim()).filter(Boolean).map((n) => byLower.get(n));
    if (raw === 'rainbow') return { mode: 'rainbow', channels: null, rainbow: null };
    if (raw.startsWith('rainbow:')) {
      const members = resolve(raw.slice('rainbow:'.length)).filter((b): b is string => !!b);
      return { mode: 'rainbow', channels: null, rainbow: members.length > 0 ? members : null };
    }
    const resolved = resolve(raw);
    if (resolved.length === 3 && resolved.every(Boolean)) {
      return {
        mode: 'custom',
        channels: { r: resolved[0]!, g: resolved[1]!, b: resolved[2]! },
        rainbow: null,
      };
    }
    return { mode: 'auto', channels: null, rainbow: null };
  }, [initial.rgb, bandList]);
  const freshVisit = initialBands === null && initialRgb === null;

  const [selectedBands, setSelectedBands] = useState<string[]>(() => {
    if (initialBands) return initialBands;
    if (freshVisit && !canRgb && bandList.length > 0) return [bandList[0]];
    return [];
  });
  const [rgbOn, setRgbOn] = useState<boolean>(() => (initialRgb ? canRgb : freshVisit && canRgb));
  const [compositeMode, setCompositeMode] = useState<CompositeMode>(initialRgb?.mode ?? 'auto');
  /** Custom channel assignment (seeded reddest / middle / bluest by inventory order). */
  const [rgbChannels, setRgbChannels] = useState<Record<RgbRole, string>>(
    () =>
      initialRgb?.channels ?? {
        r: bandList[bandList.length - 1] ?? '',
        g: bandList[Math.floor((bandList.length - 1) / 2)] ?? '',
        b: bandList[0] ?? '',
      },
  );
  /** Rainbow members (inventory order); default every band. */
  const [rainbowBands, setRainbowBands] = useState<string[]>(() => initialRgb?.rainbow ?? bandList);
  const [rgbStretch, setRgbStretch] = useState<RgbStretch>(
    isOneOf(RGB_STRETCHES, initial.rgb_stretch) ? initial.rgb_stretch : 'auto',
  );
  // Trilogy knobs; blank = the producer's tuning.
  const [noiselum, setNoiselum] = useState('');
  const [satpercent, setSatpercent] = useState('');
  const [shutters, setShutters] = useState(initial.shutters === '1');

  // Display settings live behind a disclosure: the defaults are right for a
  // quick look, and the form stays short enough to see with the preview.
  const [showDisplay, setShowDisplay] = useState(false);
  const [stretch, setStretch] = useState<Stretch>('linear');
  const [scaling, setScaling] = useState<Scaling>('snr');
  const [snrLo, setSnrLo] = useState(DEFAULT_SNR_LO);
  const [snrHi, setSnrHi] = useState(DEFAULT_SNR_HI);
  const [colormap, setColormap] = useState<Colormap>('gray');
  const [panelSize, setPanelSize] = useState('300');
  const [cols, setCols] = useState('');

  // ---- Preview -------------------------------------------------------------
  // Fetched as a blob so API error JSON can be surfaced.
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  /** Query string of the render on screen, to flag a stale preview. */
  const [renderedQuery, setRenderedQuery] = useState<string | null>(null);
  const previewUrlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Release the last object URL and cancel an in-flight render on unmount.
  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    },
    [],
  );

  const toggleIn = (setter: React.Dispatch<React.SetStateAction<string[]>>) => (b: string) =>
    setter((prev) => {
      const next = prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b];
      return bandList.filter((x) => next.includes(x)); // keep inventory order
    });
  const toggleBand = toggleIn(setSelectedBands);
  const toggleRainbowBand = toggleIn(setRainbowBands);

  const snrLoNum = parseFloat(snrLo);
  const snrHiNum = parseFloat(snrHi);
  const snrValid = scaling !== 'snr' || (Number.isFinite(snrLoNum) && Number.isFinite(snrHiNum) && snrHiNum > snrLoNum);
  const rainbowValid = compositeMode !== 'rainbow' || rainbowBands.length > 0;
  /** The `rgb` query value for the current composite settings. */
  const rgbValue =
    compositeMode === 'auto'
      ? 'auto'
      : compositeMode === 'rainbow'
        ? rainbowBands.length === bandList.length ? 'rainbow' : `rainbow:${rainbowBands.join(',')}`
        : RGB_ROLES.map((r) => rgbChannels[r]).join(',');

  const parsed = useMemo(() => parseCoordinates(coordText.trim()), [coordText]);
  const fovNum = parseFloat(fov);
  const fovValid = Number.isFinite(fovNum) && fovNum >= 0.5 && fovNum <= 600;
  const allBands = bandList.length > 0 && selectedBands.length === bandList.length;
  const hasPanels = selectedBands.length > 0 || rgbOn;
  const ready =
    dataset !== null && parsed !== null && fovValid && hasPanels && snrValid && (!rgbOn || rainbowValid);

  /** The band list the figure/FITS endpoints share for the current form. */
  const baseParams = useMemo(() => {
    if (!parsed || !fovValid) return null;
    return new URLSearchParams({
      field,
      ra: parsed.ra.toFixed(6),
      dec: parsed.dec.toFixed(6),
      fov: String(fovNum),
    });
  }, [parsed, fovValid, field, fovNum]);

  /** Figure request: single-band panels + composite + overlays + display. */
  const figureParams = useMemo(() => {
    if (!baseParams || !hasPanels || !snrValid || (rgbOn && !rainbowValid)) return null;
    const p = new URLSearchParams(baseParams);
    // `rgb` alone yields just the composite, so the band list is explicit
    // whenever both are in play (see the route's default rule).
    if (rgbOn) {
      p.set('rgb', rgbValue);
      if (selectedBands.length > 0) p.set('bands', selectedBands.join(','));
      if (rgbStretch !== 'auto') p.set('rgb_stretch', rgbStretch);
      if (rgbStretch === 'auto' || rgbStretch === 'trilogy') {
        if (noiselum.trim() !== '') p.set('noiselum', noiselum.trim());
        if (satpercent.trim() !== '') p.set('satpercent', satpercent.trim());
      }
    } else if (!allBands) {
      p.set('bands', selectedBands.join(','));
    }
    if (shutters) p.set('shutters', '1');
    p.set('size', panelSize || '300');
    if (stretch !== 'linear') p.set('stretch', stretch);
    if (scaling !== 'snr') p.set('scaling', scaling);
    if (scaling === 'snr') {
      if (snrLo.trim() !== DEFAULT_SNR_LO) p.set('snr_lo', String(snrLoNum));
      if (snrHi.trim() !== DEFAULT_SNR_HI) p.set('snr_hi', String(snrHiNum));
    }
    if (colormap !== 'gray') p.set('colormap', colormap);
    if (cols) p.set('cols', cols);
    return p;
  }, [
    baseParams, hasPanels, snrValid, rgbOn, rainbowValid, rgbValue, selectedBands, rgbStretch,
    noiselum, satpercent, shutters, allBands, panelSize, stretch, scaling, snrLo, snrHi, snrLoNum,
    snrHiNum, colormap, cols,
  ]);

  /** FITS request: the selected single bands (the composite is display-only). */
  const fitsParams = useMemo(() => {
    if (!baseParams || selectedBands.length === 0) return null;
    const p = new URLSearchParams(baseParams);
    if (!allBands) p.set('bands', selectedBands.join(','));
    return p;
  }, [baseParams, selectedBands, allBands]);

  const figureQuery = figureParams?.toString() ?? null;
  const previewStale = renderedQuery !== null && figureQuery !== renderedQuery;

  const generatePreview = useCallback(async () => {
    if (!figureQuery) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setPreviewLoading(true);
    setPreviewError(null);
    // Make the request shareable: mirror it into the page URL without a
    // server round-trip (the field is the route segment, not a query key).
    // The band list is always explicit here: the figure request may omit
    // `bands` to mean "every band", but on this page an absent `bands` AND
    // `rgb` is a fresh visit (composite alone), so an all-bands strip would
    // otherwise reload as the composite.
    const pageParams = new URLSearchParams(figureQuery);
    for (const k of ['field', 'size', 'cols', 'stretch', 'scaling', 'snr_lo', 'snr_hi', 'colormap', 'noiselum', 'satpercent']) {
      pageParams.delete(k);
    }
    if (selectedBands.length > 0) pageParams.set('bands', selectedBands.join(','));
    window.history.replaceState(null, '', `${cutoutsRoute(field)}?${pageParams.toString()}`);
    try {
      const res = await fetch(`/api/v1/cutout/figure?${figureQuery}`, { signal: controller.signal });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.error ?? `Request failed (${res.status})`);
      }
      const blob = await res.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = url;
      setPreviewUrl(url);
      setRenderedQuery(figureQuery);
    } catch (err) {
      if (controller.signal.aborted) return;
      setPreviewError(err instanceof Error ? err.message : 'Preview failed');
    } finally {
      if (abortRef.current === controller) setPreviewLoading(false);
    }
  }, [figureQuery, field, selectedBands]);

  // Auto-generate when the page arrives with a shareable request in the URL.
  const autoRan = useRef(false);
  useEffect(() => {
    if (!autoRan.current && initial.ra && initial.dec && ready) {
      autoRan.current = true;
      void generatePreview();
    }
  }, [initial.ra, initial.dec, ready, generatePreview]);

  // Estimated native FITS extent for the current request, mirroring BOTH server
  // budgets (per-band and total across bands) so the FITS button never links to
  // a request the API would 400.
  const scaleAs = dataset ? pixelScaleArcsec(dataset.pixel_scale) : null;
  const nativePx = scaleAs && fovValid ? Math.round(fovNum / scaleAs) : null;
  const nBands = selectedBands.length;
  const estMb = nativePx !== null ? (nativePx * nativePx * 4 * nBands) / 1024 ** 2 : null;
  const overBandBudget = nativePx !== null && nativePx * nativePx > MAX_PIXELS_PER_BAND;
  const overTotalBudget = nativePx !== null && nativePx * nativePx * nBands > MAX_PIXELS_TOTAL;
  const overBudget = overBandBudget || overTotalBudget;
  const fitsEnabled = fitsParams !== null && !overBudget;

  const pngName = parsed
    ? `campfire_${field}_${parsed.ra.toFixed(5)}_${parsed.dec.toFixed(5)}_${fovNum}as.png`
    : `campfire_${field}_cutout.png`;

  const inputCls =
    'w-full px-3 py-2 bg-surface-2 border border-border rounded-lg text-sm text-text-primary ' +
    'placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50';
  const labelCls = 'block text-xs font-medium uppercase tracking-wide text-text-tertiary mb-1.5';
  const chipCls = (on: boolean) =>
    `px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
      on
        ? 'bg-primary text-on-primary border-primary'
        : 'bg-surface-2 text-text-secondary border-border hover:border-primary'
    }`;
  const secondaryBtnCls = (enabled: boolean) =>
    `inline-flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ${
      enabled
        ? 'bg-surface-2 text-text-primary border-border hover:border-primary'
        : 'bg-surface-2 text-text-tertiary border-border pointer-events-none opacity-50'
    }`;
  const linkBtnCls = 'text-xs text-primary hover:underline disabled:text-text-tertiary disabled:no-underline';

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRCam', href: '/nircam' },
          { label: displayName, href: `/nircam/${encodeURIComponent(field)}` },
          { label: 'Cutouts' },
        ]}
        className="mb-6"
      />

      {/* Field selector — switches fields without leaving the cutout tool */}
      <div className="mb-4">
        <FieldSelectorDropdown fields={allFields} current={field} linkTo={cutoutsRoute} />
      </div>

      <div className="flex items-center gap-3 mb-6">
        <Scissors className="w-6 h-6 text-primary" />
        <div>
          <h1 className="text-2xl font-semibold text-text-primary">{displayName} Cutouts</h1>
          <p className="text-sm text-text-secondary">
            Multi-band cutouts at any position in the field — preview a figure or download
            science-ready FITS.
          </p>
        </div>
      </div>

      {dataset === null ? (
        <div className="text-center py-16 bg-card border border-border rounded-xl">
          <Scissors className="w-12 h-12 text-text-secondary mx-auto mb-4" />
          <p className="text-text-secondary">
            Cutouts aren&apos;t available for {displayName} yet.
          </p>
          <p className="text-text-secondary text-sm mt-2">
            This field&apos;s imaging hasn&apos;t been prepared for the cutout service —
            check back after the next imaging release.
          </p>
          <Link
            href={`/nircam/${encodeURIComponent(field)}`}
            className="inline-block mt-4 text-sm text-primary hover:text-primary-hover hover:underline"
          >
            ← Back to {displayName} data
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-6 items-start">
          {/* ---- Controls ---- */}
          <div className="bg-card border border-border rounded-xl p-5 space-y-4">
            <div>
              <label className={labelCls} htmlFor="cutout-coords">Coordinates (ICRS)</label>
              <input
                id="cutout-coords"
                type="text"
                value={coordText}
                onChange={(e) => setCoordText(e.target.value)}
                placeholder="150.11916 2.20583  or  10h00m28.6s +02d12m21.0s"
                className={inputCls}
              />
              {coordText.trim() !== '' && parsed === null && (
                <p className="mt-1 text-xs text-red-500">
                  Could not parse — use decimal degrees or sexagesimal.
                </p>
              )}
              {parsed && (
                <p className="mt-1 text-xs text-text-tertiary">
                  α = {parsed.ra.toFixed(6)}°, δ = {parsed.dec.toFixed(6)}°
                </p>
              )}
            </div>

            <div>
              <label className={labelCls} htmlFor="cutout-fov">Field of view (arcsec)</label>
              <input
                id="cutout-fov"
                type="number"
                min={0.5}
                max={600}
                step="any"
                value={fov}
                onChange={(e) => setFov(e.target.value)}
                className={inputCls}
              />
              {!fovValid && fov.trim() !== '' && (
                <p className="mt-1 text-xs text-red-500">FOV must be 0.5–600 arcsec.</p>
              )}
            </div>

            {/* Panels: which single bands, plus the composite */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className={`${labelCls} mb-0`}>Bands</span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className={linkBtnCls}
                    onClick={() => setSelectedBands(bandList)}
                    disabled={allBands}
                  >
                    All
                  </button>
                  <span className="text-xs text-text-tertiary">·</span>
                  <button
                    type="button"
                    className={linkBtnCls}
                    onClick={() => setSelectedBands([])}
                    disabled={selectedBands.length === 0}
                  >
                    None
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap gap-1.5" role="group" aria-label="Bands">
                {bandList.map((b) => {
                  const on = selectedBands.includes(b);
                  return (
                    <button
                      key={b}
                      type="button"
                      onClick={() => toggleBand(b)}
                      aria-pressed={on}
                      className={chipCls(on)}
                    >
                      {b.toUpperCase()}
                    </button>
                  );
                })}
              </div>
              <p className="mt-1.5 text-xs text-text-tertiary">
                {selectedBands.length === 0
                  ? 'No single-band panels'
                  : `${selectedBands.length} of ${bandList.length} band${bandList.length === 1 ? '' : 's'}`}
                {' · '}one panel per band
              </p>
            </div>

            <div className="space-y-2">
              <label className={`flex items-center gap-2 text-sm ${canRgb ? 'text-text-primary' : 'text-text-tertiary'}`}>
                <input
                  type="checkbox"
                  checked={rgbOn}
                  disabled={!canRgb}
                  onChange={(e) => setRgbOn(e.target.checked)}
                  className="accent-[var(--primary)]"
                />
                <span>RGB composite panel</span>
                {canRgb && rgbOn && (
                  <span className="text-xs text-text-tertiary">
                    {compositeMode === 'custom'
                      ? RGB_ROLES.map((r) => rgbChannels[r].toUpperCase()).join(' / ')
                      : compositeMode === 'rainbow'
                        ? `rainbow · ${rainbowBands.length} band${rainbowBands.length === 1 ? '' : 's'}`
                        : 'map default'}
                  </span>
                )}
                {!canRgb && <span className="text-xs">(needs ≥ 3 bands)</span>}
              </label>
              <label className="flex items-center gap-2 text-sm text-text-primary">
                <input
                  type="checkbox"
                  checked={shutters}
                  onChange={(e) => setShutters(e.target.checked)}
                  className="accent-[var(--primary)]"
                />
                <span>Overlay NIRSpec shutters</span>
                {shutters && fovValid && fovNum > SHUTTER_OVERLAY_MAX_FOV_ARCSEC && (
                  <span className="text-xs text-text-tertiary">
                    (drawn for fields up to {SHUTTER_OVERLAY_MAX_FOV_ARCSEC}″)
                  </span>
                )}
              </label>
              {!hasPanels && (
                <p className="text-xs text-red-500">Select at least one band or the RGB composite.</p>
              )}
            </div>

            {/* Display settings — collapsed; the defaults suit a quick look */}
            <div className="border-t border-border pt-3">
              <button
                type="button"
                onClick={() => setShowDisplay((v) => !v)}
                aria-expanded={showDisplay}
                aria-controls="cutout-display-settings"
                className="w-full flex items-center justify-between text-xs font-medium uppercase
                           tracking-wide text-text-tertiary hover:text-text-primary transition-colors"
              >
                <span>Display settings</span>
                <span className="flex items-center gap-2 normal-case tracking-normal font-normal">
                  {!showDisplay && (
                    <span className="text-text-tertiary">
                      {stretch} · {scaling === 'snr' ? `${snrLo}σ to +${snrHi}σ` : 'percentile'} · {colormap}
                      {rgbOn && ` · rgb ${rgbStretch}`}
                      {' · '}{panelSize || '300'} px
                    </span>
                  )}
                  {showDisplay
                    ? <ChevronDown className="w-4 h-4" />
                    : <ChevronRight className="w-4 h-4" />}
                </span>
              </button>

              {showDisplay && (
                <div id="cutout-display-settings" className="mt-3 space-y-4">
                  <div>
                    <p className="text-xs text-text-secondary mb-2">Single-band panels</p>
                    <div className="grid grid-cols-2 gap-3 mb-3">
                      <div>
                        <label className={labelCls} htmlFor="cutout-scaling">Scaling</label>
                        <select
                          id="cutout-scaling"
                          value={scaling}
                          onChange={(e) => setScaling(e.target.value as Scaling)}
                          className={inputCls}
                        >
                          <option value="snr">SNR (σ of the cutout)</option>
                          <option value="percentile">percentile (0.5–99.5%)</option>
                        </select>
                      </div>
                      {scaling === 'snr' && (
                        <div>
                          <span className={labelCls}>Range (σ)</span>
                          <div className="flex items-center gap-1.5">
                            <input
                              aria-label="Black point (σ)"
                              type="number"
                              step="any"
                              value={snrLo}
                              onChange={(e) => setSnrLo(e.target.value)}
                              className={inputCls}
                            />
                            <span className="text-xs text-text-tertiary">to</span>
                            <input
                              aria-label="White point (σ)"
                              type="number"
                              step="any"
                              value={snrHi}
                              onChange={(e) => setSnrHi(e.target.value)}
                              className={inputCls}
                            />
                          </div>
                          {!snrValid && (
                            <p className="mt-1 text-xs text-red-500">Range needs finite σ with high above low.</p>
                          )}
                        </div>
                      )}
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className={labelCls} htmlFor="cutout-stretch">Stretch</label>
                        <select
                          id="cutout-stretch"
                          value={stretch}
                          onChange={(e) => setStretch(e.target.value as Stretch)}
                          className={inputCls}
                        >
                          {STRETCHES.map((s) => <option key={s} value={s}>{s}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className={labelCls} htmlFor="cutout-colormap">Colormap</label>
                        <select
                          id="cutout-colormap"
                          value={colormap}
                          onChange={(e) => setColormap(e.target.value as Colormap)}
                          className={inputCls}
                        >
                          {COLORMAPS.map((c) => <option key={c} value={c}>{c}</option>)}
                        </select>
                      </div>
                    </div>
                  </div>

                  {canRgb && (
                    <div className={rgbOn ? '' : 'opacity-50'}>
                      <p className="text-xs text-text-secondary mb-2">RGB composite</p>
                      <div className="mb-3">
                        <label className={labelCls} htmlFor="cutout-composite">Bands</label>
                        <select
                          id="cutout-composite"
                          disabled={!rgbOn}
                          value={compositeMode}
                          onChange={(e) => setCompositeMode(e.target.value as CompositeMode)}
                          className={inputCls}
                        >
                          {COMPOSITE_MODES.map((m) => (
                            <option key={m} value={m}>{COMPOSITE_LABEL[m]}</option>
                          ))}
                        </select>
                      </div>
                      {compositeMode === 'rainbow' && (
                        <div className="mb-3">
                          <div className="flex items-center justify-between mb-1.5">
                            <span className={`${labelCls} mb-0`}>Rainbow members</span>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                className={linkBtnCls}
                                onClick={() => setRainbowBands(bandList)}
                                disabled={!rgbOn || rainbowBands.length === bandList.length}
                              >
                                All
                              </button>
                              <span className="text-xs text-text-tertiary">·</span>
                              <button
                                type="button"
                                className={linkBtnCls}
                                onClick={() => setRainbowBands([])}
                                disabled={!rgbOn || rainbowBands.length === 0}
                              >
                                None
                              </button>
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Rainbow members">
                            {bandList.map((b) => {
                              const on = rainbowBands.includes(b);
                              return (
                                <button
                                  key={b}
                                  type="button"
                                  disabled={!rgbOn}
                                  onClick={() => toggleRainbowBand(b)}
                                  aria-pressed={on}
                                  className={chipCls(on)}
                                >
                                  {b.toUpperCase()}
                                </button>
                              );
                            })}
                          </div>
                          <p className="mt-1.5 text-xs text-text-tertiary">
                            {rainbowValid
                              ? 'Hues run blue → red in wavelength order; each band on its own trilogy levels.'
                              : 'Pick at least one band for the rainbow.'}
                          </p>
                        </div>
                      )}
                      {compositeMode === 'custom' && (
                        <div className="grid grid-cols-3 gap-2 mb-3">
                          {RGB_ROLES.map((role) => (
                            <div key={role}>
                              <label className={labelCls} htmlFor={`cutout-rgb-${role}`}>
                                <span
                                  className="inline-block w-2 h-2 rounded-full mr-1.5 align-middle"
                                  style={{ background: ROLE_DOT[role] }}
                                />
                                {ROLE_LABEL[role]}
                              </label>
                              <select
                                id={`cutout-rgb-${role}`}
                                disabled={!rgbOn}
                                value={rgbChannels[role]}
                                onChange={(e) => {
                                  const v = e.target.value;
                                  setRgbChannels((prev) => ({ ...prev, [role]: v }));
                                }}
                                className={inputCls}
                              >
                                {bandList.map((b) => <option key={b} value={b}>{b.toUpperCase()}</option>)}
                              </select>
                            </div>
                          ))}
                        </div>
                      )}
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelCls} htmlFor="cutout-rgb-stretch">Stretch</label>
                          <select
                            id="cutout-rgb-stretch"
                            disabled={!rgbOn}
                            value={rgbStretch}
                            onChange={(e) => setRgbStretch(e.target.value as RgbStretch)}
                            className={inputCls}
                          >
                            {RGB_STRETCHES.map((s) => <option key={s} value={s}>{s}</option>)}
                          </select>
                        </div>
                        {(rgbStretch === 'auto' || rgbStretch === 'trilogy') && (
                          <>
                            <div>
                              <label className={labelCls} htmlFor="cutout-noiselum">Noise lum.</label>
                              <input
                                id="cutout-noiselum"
                                type="number"
                                min={0.01}
                                max={0.99}
                                step={0.01}
                                placeholder="default"
                                disabled={!rgbOn}
                                value={noiselum}
                                onChange={(e) => setNoiselum(e.target.value)}
                                className={inputCls}
                              />
                            </div>
                            <div>
                              <label className={labelCls} htmlFor="cutout-satpercent">Saturate %</label>
                              <input
                                id="cutout-satpercent"
                                type="number"
                                min={0.001}
                                max={1}
                                step="any"
                                placeholder="default"
                                disabled={!rgbOn}
                                value={satpercent}
                                onChange={(e) => setSatpercent(e.target.value)}
                                className={inputCls}
                              />
                            </div>
                          </>
                        )}
                      </div>
                      <p className="mt-1.5 text-xs text-text-tertiary">
                        Trilogy stretches each band on its own precomputed levels, as the map does
                        (the map default is the producer&apos;s full weighted band mix); the other
                        curves are a plain three-channel composite over one shared range.
                      </p>
                    </div>
                  )}

                  <div>
                    <p className="text-xs text-text-secondary mb-2">Layout</p>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className={labelCls} htmlFor="cutout-size">Panel size (px)</label>
                        <input
                          id="cutout-size"
                          type="number"
                          min={64}
                          max={1024}
                          value={panelSize}
                          onChange={(e) => setPanelSize(e.target.value)}
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls} htmlFor="cutout-cols">Columns</label>
                        <input
                          id="cutout-cols"
                          type="number"
                          min={1}
                          placeholder="one row"
                          value={cols}
                          onChange={(e) => setCols(e.target.value)}
                          className={inputCls}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div className="pt-1 space-y-2">
              <button
                type="button"
                onClick={() => void generatePreview()}
                disabled={!ready || previewLoading}
                className="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-primary
                           text-on-primary rounded-lg text-sm font-medium hover:bg-primary-hover
                           disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {previewLoading
                  ? <Loader2 className="w-4 h-4 animate-spin" />
                  : <ImageIcon className="w-4 h-4" />}
                {previewStale ? 'Regenerate figure' : 'Generate figure'}
              </button>

              <div className="grid grid-cols-2 gap-2">
                <a
                  href={fitsEnabled ? `/api/v1/cutout/fits?${fitsParams.toString()}` : undefined}
                  aria-disabled={!fitsEnabled}
                  title={
                    selectedBands.length === 0
                      ? 'Select bands to download FITS (the composite is display-only)'
                      : undefined
                  }
                  className={secondaryBtnCls(fitsEnabled)}
                >
                  <Download className="w-4 h-4" />
                  FITS
                </a>
                <a
                  href={previewUrl ?? undefined}
                  download={previewUrl ? pngName : undefined}
                  aria-disabled={!previewUrl}
                  className={secondaryBtnCls(previewUrl !== null)}
                >
                  <Download className="w-4 h-4" />
                  PNG
                </a>
              </div>

              {nativePx !== null && selectedBands.length > 0 && (
                <p className={`text-xs ${overBudget ? 'text-red-500' : 'text-text-tertiary'}`}>
                  FITS at native scale: ~{nativePx}×{nativePx} px × {nBands} band
                  {nBands > 1 ? 's' : ''}
                  {estMb !== null && ` ≈ ${estMb < 1 ? estMb.toFixed(2) : estMb.toFixed(1)} MB`}
                  {overBandBudget && ' — over the 4096² per-band budget; reduce the FOV'}
                  {!overBandBudget && overTotalBudget &&
                    ' — over the total pixel budget; reduce the FOV or deselect bands'}
                </p>
              )}
              {selectedBands.length === 0 && (
                <p className="text-xs text-text-tertiary">
                  FITS downloads cover the selected bands; the RGB composite is display-only.
                </p>
              )}
              <p className="text-xs text-text-tertiary">
                FITS cutouts are cropped from the RICE-compressed display imagery
                (photometry faithful to ~0.03%) and carry each band&apos;s native WCS.
              </p>
            </div>
          </div>

          {/* ---- Preview ---- */}
          <div className="bg-card border border-border rounded-xl p-5 min-h-[420px] flex flex-col">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-medium text-text-primary">
                Preview
                {previewStale && !previewLoading && (
                  <span className="ml-2 text-xs font-normal text-text-tertiary">
                    settings changed — regenerate to update
                  </span>
                )}
              </h2>
              {parsed && (
                <Link
                  href={`/map?field=${encodeURIComponent(field)}&ra=${parsed.ra}&dec=${parsed.dec}`}
                  className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline"
                >
                  <MapIcon className="w-3.5 h-3.5" />
                  View on map
                </Link>
              )}
            </div>
            <div className="flex-1 flex items-center justify-center">
              {previewError ? (
                <p className="text-sm text-red-500 max-w-md text-center">{previewError}</p>
              ) : previewUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={previewUrl}
                  alt="Cutout figure preview"
                  className={`max-w-full h-auto rounded transition-opacity ${
                    previewLoading ? 'opacity-50' : ''
                  }`}
                />
              ) : (
                <div className="text-center text-text-tertiary">
                  {previewLoading ? (
                    <Loader2 className="w-8 h-8 animate-spin mx-auto" />
                  ) : (
                    <>
                      <ImageIcon className="w-10 h-10 mx-auto mb-3 opacity-50" />
                      <p className="text-sm">
                        Enter coordinates and generate a figure to preview the cutout.
                      </p>
                    </>
                  )}
                </div>
              )}
            </div>
            {previewUrl && shutters && !previewError && (
              <p className="mt-3 text-xs text-text-tertiary">
                Shutter footprints are coloured per observation; stuck-closed shutters are red dashed.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
