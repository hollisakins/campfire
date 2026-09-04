'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Download, ImageIcon, Loader2, Map as MapIcon, Scissors } from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { FieldSelectorDropdown } from '@/components/nircam/FieldSelectorDropdown';
import type { FitsglDataset } from '@/lib/actions/map';
import type { NircamFieldCard } from '@/lib/types';
import { MAX_PIXELS_PER_BAND, MAX_PIXELS_TOTAL } from '@/lib/cutout/limits';
import { parseCoordinates } from '@/lib/utils/coordinate-parser';

const STRETCHES = ['linear', 'log', 'sqrt', 'asinh'] as const;
const COLORMAPS = ['gray', 'viridis', 'magma', 'inferno', 'plasma', 'cividis'] as const;

/** Parse a dataset `pixel_scale` tag ('30mas', '0.03as') to arcsec/px, or null. */
function pixelScaleArcsec(tag: string): number | null {
  const mas = tag.match(/^([\d.]+)\s*mas$/i);
  if (mas) return parseFloat(mas[1]) / 1000;
  const as = tag.match(/^([\d.]+)\s*(as|arcsec)$/i);
  if (as) return parseFloat(as[1]);
  return null;
}

const cutoutsRoute = (field: string) => `/nircam/${encodeURIComponent(field)}/cutouts`;

interface CutoutsContentProps {
  field: string;
  /** The field's cutout source; null = cutouts not available for this field. */
  dataset: FitsglDataset | null;
  allFields: NircamFieldCard[];
  initial: {
    ra?: string;
    dec?: string;
    fov?: string;
    bands?: string;
  };
}

export const CutoutsContent: React.FC<CutoutsContentProps> = ({
  field,
  dataset,
  allFields,
  initial,
}) => {
  const router = useRouter();
  const displayName =
    allFields.find((f) => f.field === field)?.display_name ?? field.toUpperCase();

  const [coordText, setCoordText] = useState(
    initial.ra && initial.dec ? `${initial.ra} ${initial.dec}` : '',
  );
  const [fov, setFov] = useState(initial.fov ?? '10');
  const [selectedBands, setSelectedBands] = useState<string[]>(
    dataset
      ? initial.bands
        ? initial.bands.split(',').filter((b) => dataset.bands.includes(b))
        : dataset.bands
      : [],
  );
  const [stretch, setStretch] = useState<(typeof STRETCHES)[number]>('asinh');
  const [colormap, setColormap] = useState<(typeof COLORMAPS)[number]>('gray');
  const [panelSize, setPanelSize] = useState('300');
  const [cols, setCols] = useState('');

  // Preview state: fetched as a blob so API error JSON can be surfaced.
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewUrlRef = useRef<string | null>(null);

  const toggleBand = (b: string) => {
    if (!dataset) return;
    setSelectedBands((prev) => {
      const next = prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b];
      // Keep inventory order and never allow an empty selection.
      const ordered = dataset.bands.filter((x) => next.includes(x));
      return ordered.length > 0 ? ordered : prev;
    });
  };

  const parsed = useMemo(() => parseCoordinates(coordText.trim()), [coordText]);
  const fovNum = parseFloat(fov);
  const fovValid = Number.isFinite(fovNum) && fovNum >= 0.5 && fovNum <= 600;
  const allBands = dataset !== null && selectedBands.length === dataset.bands.length;
  const ready = dataset !== null && parsed !== null && fovValid && selectedBands.length > 0;

  /** Query string shared by the figure/FITS endpoints for the current form. */
  const buildParams = useCallback(
    (extra: Record<string, string> = {}) => {
      if (!parsed) return null;
      const p = new URLSearchParams({
        field,
        ra: parsed.ra.toFixed(6),
        dec: parsed.dec.toFixed(6),
        fov: String(fovNum),
        ...extra,
      });
      if (!allBands) p.set('bands', selectedBands.join(','));
      return p;
    },
    [parsed, field, fovNum, allBands, selectedBands],
  );

  const figureParams = ready
    ? buildParams({ size: panelSize || '300', stretch, colormap, ...(cols ? { cols } : {}) })
    : null;
  const fitsParams = ready ? buildParams() : null;

  const generatePreview = useCallback(async () => {
    if (!figureParams) return;
    setPreviewLoading(true);
    setPreviewError(null);
    // Make the request shareable: mirror it into the page URL. The field is
    // the route segment, so strip it from the mirrored query.
    const pageParams = new URLSearchParams(fitsParams!);
    pageParams.delete('field');
    router.replace(`${cutoutsRoute(field)}?${pageParams.toString()}`, { scroll: false });
    try {
      const res = await fetch(`/api/v1/cutout/figure?${figureParams.toString()}`);
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.error ?? `Request failed (${res.status})`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = url;
      setPreviewUrl(url);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : 'Preview failed');
    } finally {
      setPreviewLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [figureParams?.toString()]);

  // Auto-generate when the page arrives with a shareable request in the URL.
  const autoRan = useRef(false);
  useEffect(() => {
    if (!autoRan.current && initial.ra && initial.dec && ready) {
      autoRan.current = true;
      generatePreview();
    }
  }, [initial.ra, initial.dec, ready, generatePreview]);

  // Estimated native FITS extent for the current request, mirroring BOTH server
  // budgets (per-band and total across bands) so the FITS button never links to
  // a request the API would 400.
  const scaleAs = dataset ? pixelScaleArcsec(dataset.pixel_scale) : null;
  const nativePx = scaleAs && fovValid ? Math.round(fovNum / scaleAs) : null;
  const estMb =
    nativePx !== null ? (nativePx * nativePx * 4 * selectedBands.length) / 1024 ** 2 : null;
  const overBandBudget = nativePx !== null && nativePx * nativePx > MAX_PIXELS_PER_BAND;
  const overTotalBudget =
    nativePx !== null && nativePx * nativePx * selectedBands.length > MAX_PIXELS_TOTAL;
  const overBudget = overBandBudget || overTotalBudget;

  const inputCls =
    'w-full px-3 py-2 bg-surface-2 border border-border rounded-lg text-sm text-text-primary ' +
    'placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50';
  const labelCls = 'block text-xs font-medium uppercase tracking-wide text-text-tertiary mb-1.5';

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
                placeholder='150.11916 2.20583  or  10h00m28.6s +02d12m21.0s'
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

            <div>
              <span className={labelCls}>Bands</span>
              <div className="flex flex-wrap gap-1.5">
                {dataset.bands.map((b) => {
                  const on = selectedBands.includes(b);
                  return (
                    <button
                      key={b}
                      type="button"
                      onClick={() => toggleBand(b)}
                      aria-pressed={on}
                      className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
                        on
                          ? 'bg-primary text-on-primary border-primary'
                          : 'bg-surface-2 text-text-secondary border-border hover:border-primary'
                      }`}
                    >
                      {b.toUpperCase()}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelCls} htmlFor="cutout-stretch">Stretch</label>
                <select
                  id="cutout-stretch"
                  value={stretch}
                  onChange={(e) => setStretch(e.target.value as (typeof STRETCHES)[number])}
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
                  onChange={(e) => setColormap(e.target.value as (typeof COLORMAPS)[number])}
                  className={inputCls}
                >
                  {COLORMAPS.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
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

            <div className="pt-1 space-y-2">
              <button
                type="button"
                onClick={generatePreview}
                disabled={!ready || previewLoading}
                className="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-primary
                           text-on-primary rounded-lg text-sm font-medium hover:bg-primary-hover
                           disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {previewLoading
                  ? <Loader2 className="w-4 h-4 animate-spin" />
                  : <ImageIcon className="w-4 h-4" />}
                Generate figure
              </button>

              <div className="grid grid-cols-2 gap-2">
                <a
                  href={fitsParams ? `/api/v1/cutout/fits?${fitsParams.toString()}` : undefined}
                  aria-disabled={!fitsParams || overBudget}
                  className={`inline-flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm
                              font-medium border transition-colors ${
                                fitsParams && !overBudget
                                  ? 'bg-surface-2 text-text-primary border-border hover:border-primary'
                                  : 'bg-surface-2 text-text-tertiary border-border pointer-events-none opacity-50'
                              }`}
                >
                  <Download className="w-4 h-4" />
                  FITS
                </a>
                <a
                  href={figureParams && previewUrl ? previewUrl : undefined}
                  download={figureParams ? `campfire_${field}_cutout.png` : undefined}
                  aria-disabled={!previewUrl}
                  className={`inline-flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm
                              font-medium border transition-colors ${
                                previewUrl
                                  ? 'bg-surface-2 text-text-primary border-border hover:border-primary'
                                  : 'bg-surface-2 text-text-tertiary border-border pointer-events-none opacity-50'
                              }`}
                >
                  <Download className="w-4 h-4" />
                  PNG
                </a>
              </div>

              {nativePx !== null && (
                <p className={`text-xs ${overBudget ? 'text-red-500' : 'text-text-tertiary'}`}>
                  FITS at native scale: ~{nativePx}×{nativePx} px × {selectedBands.length} band
                  {selectedBands.length > 1 ? 's' : ''}
                  {estMb !== null && ` ≈ ${estMb < 1 ? estMb.toFixed(2) : estMb.toFixed(1)} MB`}
                  {overBandBudget && ' — over the 4096² per-band budget; reduce the FOV'}
                  {!overBandBudget && overTotalBudget &&
                    ' — over the total pixel budget; reduce the FOV or deselect bands'}
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
              <h2 className="text-sm font-medium text-text-primary">Preview</h2>
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
                  className={`max-w-full h-auto rounded ${previewLoading ? 'opacity-50' : ''}`}
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
          </div>
        </div>
      )}
    </div>
  );
};
