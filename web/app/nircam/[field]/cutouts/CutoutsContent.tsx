'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Download,
  ImageIcon,
  Loader2,
  Map as MapIcon,
  Scissors,
  X,
} from 'lucide-react';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { FieldSelectorDropdown } from '@/components/nircam/FieldSelectorDropdown';
import type { FitsglDataset } from '@/lib/actions/map';
import type { NircamFieldCard } from '@/lib/types';
import { MAX_PIXELS_PER_BAND, MAX_PIXELS_TOTAL } from '@/lib/cutout/limits';
import { SHUTTER_OVERLAY_MAX_FOV_ARCSEC } from '@/lib/cutout/shutters';
import { parseCoordinates } from '@/lib/utils/coordinate-parser';

const STRETCHES = ['linear', 'asinh', 'log', 'sqrt'] as const;
type Scaling = 'snr' | 'percentile';
const DEFAULT_SNR_LO = '-5';
const DEFAULT_SNR_HI = '8';
const COMPOSITE_MODES = ['auto', 'rainbow', 'custom'] as const;
const COMPOSITE_LABEL: Record<(typeof COMPOSITE_MODES)[number], string> = {
  auto: 'Map default',
  rainbow: 'Rainbow',
  custom: 'Custom RGB',
};
const COLORMAPS = ['gray', 'viridis', 'magma', 'inferno', 'plasma', 'cividis'] as const;
const RGB_STRETCHES = ['auto', 'trilogy', 'asinh', 'log', 'sqrt', 'linear'] as const;

type Stretch = (typeof STRETCHES)[number];
type Colormap = (typeof COLORMAPS)[number];
type RgbStretch = (typeof RGB_STRETCHES)[number];
type CompositeMode = (typeof COMPOSITE_MODES)[number];
type RgbRole = 'r' | 'g' | 'b';

const RGB_ROLES: readonly RgbRole[] = ['r', 'g', 'b'];
const ROLE_LABEL: Record<RgbRole, string> = { r: 'R', g: 'G', b: 'B' };
const ROLE_DOT: Record<RgbRole, string> = { r: '#ef4444', g: '#22c55e', b: '#3b82f6' };

function pixelScaleArcsec(tag: string): number | null {
  const mas = tag.match(/^([\d.]+)\s*mas$/i);
  if (mas) return parseFloat(mas[1]) / 1000;
  const as = tag.match(/^([\d.]+)\s*(as|arcsec)$/i);
  if (as) return parseFloat(as[1]);
  return null;
}

const cutoutsRoute = (field: string) => `/nircam/${encodeURIComponent(field)}/cutouts`;

function isOneOf<T extends string>(list: readonly T[], value: string | undefined): value is T {
  return value !== undefined && (list as readonly string[]).includes(value);
}

function boundedInitial(
  value: string | undefined,
  fallback: string,
  min: number,
  max: number,
  integer = false,
) {
  if (value === undefined) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= min && parsed <= max && (!integer || Number.isInteger(parsed))
    ? value
    : fallback;
}

function optionalBoundedInitial(
  value: string | undefined,
  min: number,
  max: number,
  integer = false,
) {
  if (value === undefined || value.trim() === '') return '';
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= min && parsed <= max && (!integer || Number.isInteger(parsed))
    ? value
    : '';
}

function representativeBands(bands: string[]) {
  if (bands.length <= 3) return bands;
  return Array.from(new Set([bands[0], bands[Math.floor((bands.length - 1) / 2)], bands[bands.length - 1]]));
}

interface InitialCutoutRequest {
  ra?: string;
  dec?: string;
  fov?: string;
  bands?: string;
  rgb?: string;
  rgb_stretch?: string;
  shutters?: string;
  size?: string;
  cols?: string;
  stretch?: string;
  scaling?: string;
  snr_lo?: string;
  snr_hi?: string;
  colormap?: string;
  noiselum?: string;
  satpercent?: string;
}

interface RenderedArtifact {
  url: string;
  query: string;
  filename: string;
  rgbLabel: string | null;
  bandCount: number;
  shutterRequested: boolean;
  shutterCount: number;
}

interface CutoutsContentProps {
  field: string;
  dataset: FitsglDataset | null;
  allFields: NircamFieldCard[];
  canOverlayShutters: boolean;
  initial: InitialCutoutRequest;
}

export const CutoutsContent: React.FC<CutoutsContentProps> = ({
  field,
  dataset,
  allFields,
  canOverlayShutters,
  initial,
}) => {
  const displayName =
    allFields.find((candidate) => candidate.field === field)?.display_name ?? field.toUpperCase();
  const bandList = useMemo(() => dataset?.bands ?? [], [dataset]);
  const canRgb = bandList.length >= 3;

  const [coordText, setCoordText] = useState(
    initial.ra && initial.dec ? `${initial.ra} ${initial.dec}` : '',
  );
  const [fov, setFov] = useState(() => boundedInitial(initial.fov, '10', 0.5, 600));

  const initialBands = useMemo(() => {
    if (initial.bands === undefined) return null;
    const wanted = new Set(
      initial.bands.split(',').map((band) => band.trim().toLowerCase()).filter(Boolean),
    );
    const matched = bandList.filter((band) => wanted.has(band.toLowerCase()));
    return matched.length > 0 ? matched : null;
  }, [initial.bands, bandList]);
  const initialRgb = useMemo<{
    mode: CompositeMode;
    channels: Record<RgbRole, string> | null;
    rainbow: string[] | null;
  } | null>(() => {
    if (initial.rgb === undefined || !canRgb) return null;
    const raw = initial.rgb.trim().toLowerCase();
    const byLower = new Map(bandList.map((band) => [band.toLowerCase(), band]));
    const resolve = (csv: string) =>
      csv.split(',').map((band) => band.trim()).filter(Boolean).map((name) => byLower.get(name));
    if (raw === 'rainbow') return { mode: 'rainbow', channels: null, rainbow: null };
    if (raw.startsWith('rainbow:')) {
      const members = resolve(raw.slice('rainbow:'.length)).filter((band): band is string => !!band);
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
  }, [initial.rgb, bandList, canRgb]);
  const freshVisit = initialBands === null && initialRgb === null;
  const initialPanelBands = initialBands ?? representativeBands(bandList);

  const [bandPanelsOn, setBandPanelsOn] = useState(() => initialBands !== null || !canRgb);
  const [selectedBands, setSelectedBands] = useState<string[]>(() => initialPanelBands);
  const [rgbOn, setRgbOn] = useState(() => (initialRgb ? canRgb : freshVisit && canRgb));
  const [compositeMode, setCompositeMode] = useState<CompositeMode>(initialRgb?.mode ?? 'auto');
  const [rgbChannels, setRgbChannels] = useState<Record<RgbRole, string>>(
    () =>
      initialRgb?.channels ?? {
        r: bandList[bandList.length - 1] ?? '',
        g: bandList[Math.floor((bandList.length - 1) / 2)] ?? '',
        b: bandList[0] ?? '',
      },
  );
  const [rainbowBands, setRainbowBands] = useState<string[]>(() => initialRgb?.rainbow ?? bandList);
  const [rgbStretch, setRgbStretch] = useState<RgbStretch>(() =>
    initialRgb?.mode === 'rainbow'
      ? 'trilogy'
      : isOneOf(RGB_STRETCHES, initial.rgb_stretch)
        ? initial.rgb_stretch
        : 'auto',
  );
  const [noiselum, setNoiselum] = useState(() => optionalBoundedInitial(initial.noiselum, 0.000001, 0.999999));
  const [satpercent, setSatpercent] = useState(() => optionalBoundedInitial(initial.satpercent, 0.001, 1));

  const initialFov = Number(boundedInitial(initial.fov, '10', 0.5, 600));
  const [shutters, setShutters] = useState(
    initial.shutters === '1' && canOverlayShutters && initialFov <= SHUTTER_OVERLAY_MAX_FOV_ARCSEC,
  );

  const [showDisplay, setShowDisplay] = useState(false);
  const [showValidationSummary, setShowValidationSummary] = useState(false);
  const [stretch, setStretch] = useState<Stretch>(
    isOneOf(STRETCHES, initial.stretch) ? initial.stretch : 'linear',
  );
  const [scaling, setScaling] = useState<Scaling>(
    initial.scaling === 'percentile' ? 'percentile' : 'snr',
  );
  const [snrLo, setSnrLo] = useState(() => {
    const value = Number(initial.snr_lo);
    return initial.snr_lo !== undefined && Number.isFinite(value) ? initial.snr_lo : DEFAULT_SNR_LO;
  });
  const [snrHi, setSnrHi] = useState(() => {
    const value = Number(initial.snr_hi);
    return initial.snr_hi !== undefined && Number.isFinite(value) ? initial.snr_hi : DEFAULT_SNR_HI;
  });
  const [colormap, setColormap] = useState<Colormap>(
    isOneOf(COLORMAPS, initial.colormap) ? initial.colormap : 'gray',
  );
  const [panelSize, setPanelSize] = useState(() => boundedInitial(initial.size, '300', 64, 1024, true));
  const [cols, setCols] = useState(() => optionalBoundedInitial(initial.cols, 1, 1000, true));

  const [artifact, setArtifact] = useState<RenderedArtifact | null>(null);
  const [loadingQuery, setLoadingQuery] = useState<string | null>(null);
  const [previewFailure, setPreviewFailure] = useState<{ query: string; message: string } | null>(null);
  const artifactUrlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (artifactUrlRef.current) URL.revokeObjectURL(artifactUrlRef.current);
    },
    [],
  );

  const toggleIn = (setter: React.Dispatch<React.SetStateAction<string[]>>) => (band: string) =>
    setter((previous) => {
      const next = previous.includes(band)
        ? previous.filter((candidate) => candidate !== band)
        : [...previous, band];
      return bandList.filter((candidate) => next.includes(candidate));
    });
  const toggleBand = toggleIn(setSelectedBands);
  const toggleRainbowBand = toggleIn(setRainbowBands);

  const parsed = useMemo(() => parseCoordinates(coordText.trim()), [coordText]);
  const fovNum = Number(fov);
  const fovValid = Number.isFinite(fovNum) && fovNum >= 0.5 && fovNum <= 600;
  const shutterEligible =
    canOverlayShutters && fovValid && fovNum <= SHUTTER_OVERLAY_MAX_FOV_ARCSEC;

  useEffect(() => {
    if (!shutterEligible) setShutters(false);
  }, [shutterEligible]);

  const activeBands = useMemo(
    () => (bandPanelsOn ? selectedBands : []),
    [bandPanelsOn, selectedBands],
  );
  const allBands = bandList.length > 0 && activeBands.length === bandList.length;
  const snrLoNum = Number(snrLo);
  const snrHiNum = Number(snrHi);
  const snrValid =
    !bandPanelsOn || scaling !== 'snr' ||
    (Number.isFinite(snrLoNum) && Number.isFinite(snrHiNum) && snrHiNum > snrLoNum);
  const rainbowValid = !rgbOn || compositeMode !== 'rainbow' || rainbowBands.length > 0;
  const panelSizeNum = Number(panelSize);
  const panelSizeValid =
    Number.isInteger(panelSizeNum) && panelSizeNum >= 64 && panelSizeNum <= 1024;
  const colsNum = cols.trim() === '' ? null : Number(cols);
  const colsValid = colsNum === null || (Number.isInteger(colsNum) && colsNum >= 1);
  const effectiveRgbStretch: RgbStretch = compositeMode === 'rainbow' ? 'trilogy' : rgbStretch;
  const trilogyControlsActive =
    rgbOn && (effectiveRgbStretch === 'auto' || effectiveRgbStretch === 'trilogy');
  const noiselumNum = Number(noiselum);
  const noiselumValid =
    !trilogyControlsActive || noiselum.trim() === '' ||
    (Number.isFinite(noiselumNum) && noiselumNum > 0 && noiselumNum < 1);
  const satpercentNum = Number(satpercent);
  const satpercentValid =
    !trilogyControlsActive || satpercent.trim() === '' ||
    (Number.isFinite(satpercentNum) && satpercentNum >= 0.001 && satpercentNum <= 1);
  const hasPanels = activeBands.length > 0 || rgbOn;
  const ready =
    dataset !== null && parsed !== null && fovValid && hasPanels && snrValid && rainbowValid &&
    panelSizeValid && colsValid && noiselumValid && satpercentValid;

  const rgbValue =
    compositeMode === 'auto'
      ? 'auto'
      : compositeMode === 'rainbow'
        ? rainbowBands.length === bandList.length
          ? 'rainbow'
          : `rainbow:${rainbowBands.join(',')}`
        : RGB_ROLES.map((role) => rgbChannels[role]).join(',');

  const baseParams = useMemo(() => {
    if (!parsed || !fovValid) return null;
    return new URLSearchParams({
      field,
      ra: parsed.ra.toFixed(6),
      dec: parsed.dec.toFixed(6),
      fov: String(fovNum),
    });
  }, [parsed, fovValid, field, fovNum]);

  const figureParams = useMemo(() => {
    if (!baseParams || !ready) return null;
    const params = new URLSearchParams(baseParams);
    if (rgbOn) {
      params.set('rgb', rgbValue);
      if (activeBands.length > 0) params.set('bands', activeBands.join(','));
      if (effectiveRgbStretch !== 'auto') params.set('rgb_stretch', effectiveRgbStretch);
      if (trilogyControlsActive) {
        if (noiselum.trim() !== '') params.set('noiselum', String(noiselumNum));
        if (satpercent.trim() !== '') params.set('satpercent', String(satpercentNum));
      }
    } else if (!allBands) {
      params.set('bands', activeBands.join(','));
    }
    if (shutters && shutterEligible) params.set('shutters', '1');
    params.set('size', String(panelSizeNum));
    if (bandPanelsOn && stretch !== 'linear') params.set('stretch', stretch);
    if (bandPanelsOn && scaling !== 'snr') params.set('scaling', scaling);
    if (bandPanelsOn && scaling === 'snr') {
      if (snrLo.trim() !== DEFAULT_SNR_LO) params.set('snr_lo', String(snrLoNum));
      if (snrHi.trim() !== DEFAULT_SNR_HI) params.set('snr_hi', String(snrHiNum));
    }
    if (bandPanelsOn && colormap !== 'gray') params.set('colormap', colormap);
    if (colsNum !== null) params.set('cols', String(colsNum));
    return params;
  }, [
    baseParams, ready, rgbOn, rgbValue, activeBands, effectiveRgbStretch, trilogyControlsActive,
    noiselum, noiselumNum, satpercent, satpercentNum, allBands, shutters, shutterEligible,
    panelSizeNum, bandPanelsOn, stretch, scaling, snrLo, snrHi, snrLoNum, snrHiNum, colormap,
    colsNum,
  ]);

  const fitsParams = useMemo(() => {
    if (!baseParams || activeBands.length === 0) return null;
    const params = new URLSearchParams(baseParams);
    if (!allBands) params.set('bands', activeBands.join(','));
    return params;
  }, [baseParams, activeBands, allBands]);

  const figureQuery = figureParams?.toString() ?? null;
  const previewLoading = loadingQuery !== null;
  const previewStale = artifact !== null && artifact.query !== figureQuery;
  const currentFailure = previewFailure?.query === figureQuery ? previewFailure.message : null;
  const pngEnabled = artifact !== null && artifact.query === figureQuery && currentFailure === null;

  const rgbLabel = useMemo(() => {
    if (!rgbOn) return null;
    if (compositeMode === 'auto') return 'RGB · Map default';
    if (compositeMode === 'rainbow') {
      return `RGB · Rainbow (${rainbowBands.length} band${rainbowBands.length === 1 ? '' : 's'})`;
    }
    return `RGB · ${RGB_ROLES.map((role) => rgbChannels[role].toUpperCase()).join(' / ')}`;
  }, [rgbOn, compositeMode, rainbowBands.length, rgbChannels]);

  const generatePreview = useCallback(async () => {
    if (!figureQuery || !parsed) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoadingQuery(figureQuery);
    setPreviewFailure((failure) => (failure?.query === figureQuery ? null : failure));

    const pageParams = new URLSearchParams(figureQuery);
    pageParams.delete('field');
    if (activeBands.length > 0) pageParams.set('bands', activeBands.join(','));
    window.history.replaceState(null, '', `${cutoutsRoute(field)}?${pageParams.toString()}`);

    const filename =
      `campfire_${field}_${parsed.ra.toFixed(5)}_${parsed.dec.toFixed(5)}_${fovNum}as.png`;
    const renderedRgbLabel = rgbLabel;
    const renderedBandCount = activeBands.length;
    const shutterRequested = shutters && shutterEligible;

    try {
      const response = await fetch(`/api/v1/cutout/figure?${figureQuery}`, {
        signal: controller.signal,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.error ?? `Request failed (${response.status})`);
      }
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      if (artifactUrlRef.current) URL.revokeObjectURL(artifactUrlRef.current);
      artifactUrlRef.current = url;
      const parsedCount = Number(response.headers.get('X-Campfire-Shutter-Count'));
      setArtifact({
        url,
        query: figureQuery,
        filename,
        rgbLabel: renderedRgbLabel,
        bandCount: renderedBandCount,
        shutterRequested,
        shutterCount: Number.isFinite(parsedCount) ? parsedCount : 0,
      });
      setPreviewFailure(null);
    } catch (error) {
      if (controller.signal.aborted) return;
      setPreviewFailure({
        query: figureQuery,
        message: error instanceof Error ? error.message : 'Preview failed',
      });
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setLoadingQuery(null);
      }
    }
  }, [
    figureQuery, parsed, activeBands, field, fovNum, rgbLabel, shutters, shutterEligible,
  ]);

  const cancelPreview = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoadingQuery(null);
  }, []);

  const autoRan = useRef(false);
  useEffect(() => {
    if (!autoRan.current && initial.ra && initial.dec && ready) {
      autoRan.current = true;
      void generatePreview();
    }
  }, [initial.ra, initial.dec, ready, generatePreview]);

  const scaleAs = dataset ? pixelScaleArcsec(dataset.pixel_scale) : null;
  const nativePx = scaleAs && fovValid ? Math.round(fovNum / scaleAs) : null;
  const nBands = activeBands.length;
  const estMb = nativePx !== null ? (nativePx * nativePx * 4 * nBands) / 1024 ** 2 : null;
  const overBandBudget = nativePx !== null && nativePx * nativePx > MAX_PIXELS_PER_BAND;
  const overTotalBudget = nativePx !== null && nativePx * nativePx * nBands > MAX_PIXELS_TOTAL;
  const overBudget = overBandBudget || overTotalBudget;
  const fitsEnabled = fitsParams !== null && !overBudget;

  const formIssue =
    parsed === null
      ? 'Enter valid ICRS coordinates.'
      : !fovValid
        ? 'Field of view must be between 0.5 and 600 arcsec.'
        : !hasPanels
          ? 'Turn on an RGB composite or select at least one band panel.'
          : !rainbowValid
            ? 'Choose at least one band for the rainbow composite.'
            : !snrValid
              ? 'The SNR high point must be greater than the low point.'
              : !panelSizeValid
                ? 'Panel size must be a whole number from 64 to 1024 px.'
                : !colsValid
                  ? 'Columns must be blank or a positive whole number.'
                  : !noiselumValid
                    ? 'Noise luminance must be greater than 0 and less than 1.'
                    : !satpercentValid
                      ? 'Saturation percent must be between 0.001 and 1.'
                      : null;

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (previewLoading && (loadingQuery === figureQuery || !ready)) {
      cancelPreview();
      return;
    }
    if (!ready) {
      setShowValidationSummary(true);
      setShowDisplay(true);
      return;
    }
    void generatePreview();
  };

  const inputCls =
    'w-full px-3 py-2 bg-surface-2 border border-border rounded-lg text-sm text-text-primary ' +
    'placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50';
  const labelCls = 'block text-xs font-medium uppercase tracking-wide text-text-tertiary mb-1.5';
  const chipCls = (on: boolean) =>
    `px-2.5 py-1.5 rounded-md text-xs font-medium border transition-colors focus:outline-none focus:ring-2 focus:ring-primary/50 ${
      on
        ? 'bg-primary text-on-primary border-primary'
        : 'bg-surface-2 text-text-secondary border-border hover:border-primary'
    }`;
  const secondaryBtnCls = (enabled: boolean) =>
    `inline-flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ${
      enabled
        ? 'bg-surface-2 text-text-primary border-border hover:border-primary focus:outline-none focus:ring-2 focus:ring-primary/50'
        : 'bg-surface-2 text-text-tertiary border-border pointer-events-none opacity-50'
    }`;
  const linkBtnCls = 'text-xs text-primary hover:underline disabled:text-text-tertiary disabled:no-underline';

  const previewStatus = previewLoading
    ? loadingQuery === figureQuery
      ? 'Generating'
      : 'Generating earlier settings'
    : currentFailure
      ? 'Generation failed'
      : artifact === null
        ? 'Not generated'
        : previewStale
          ? 'Changes not applied'
          : 'Up to date';

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

      <div className="mb-7 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-lg bg-primary/10 p-2">
            <Scissors className="h-5 w-5 text-primary" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-text-primary">Make a NIRCam cutout</h1>
            <p className="mt-1 max-w-2xl text-sm text-text-secondary">
              Compose a publication-ready preview, then download the figure or its science bands.
            </p>
          </div>
        </div>
        <div className="sm:min-w-48">
          <p className={labelCls}>Imaging field</p>
          <FieldSelectorDropdown fields={allFields} current={field} linkTo={cutoutsRoute} />
        </div>
      </div>

      {dataset === null ? (
        <div className="rounded-xl border border-border bg-card py-16 text-center">
          <Scissors className="mx-auto mb-4 h-12 w-12 text-text-secondary" />
          <p className="text-text-secondary">Cutouts aren&apos;t available for {displayName} yet.</p>
          <p className="mt-2 text-sm text-text-secondary">
            This field&apos;s imaging hasn&apos;t been prepared for the cutout service.
          </p>
          <Link
            href={`/nircam/${encodeURIComponent(field)}`}
            className="mt-4 inline-block text-sm text-primary hover:underline"
          >
            ← Back to {displayName} data
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[360px_minmax(0,1fr)]">
          <form onSubmit={handleSubmit} className="overflow-hidden rounded-xl border border-border bg-card">
            <section className="space-y-4 p-5" aria-labelledby="cutout-position-heading">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">Step 1</p>
                <h2 id="cutout-position-heading" className="mt-1 text-base font-semibold text-text-primary">
                  Position
                </h2>
              </div>
              <div>
                <label className={labelCls} htmlFor="cutout-coords">Coordinates (ICRS)</label>
                <input
                  id="cutout-coords"
                  type="text"
                  value={coordText}
                  onChange={(event) => setCoordText(event.target.value)}
                  placeholder="150.11916 2.20583"
                  aria-invalid={coordText.trim() !== '' && parsed === null}
                  aria-describedby={coordText.trim() !== '' && parsed === null ? 'cutout-coords-error' : undefined}
                  className={inputCls}
                />
                {coordText.trim() !== '' && parsed === null && (
                  <p id="cutout-coords-error" className="mt-1 text-xs text-red-500">
                    Use decimal degrees or sexagesimal coordinates.
                  </p>
                )}
                {parsed && (
                  <p className="mt-1 text-xs text-text-tertiary">
                    α {parsed.ra.toFixed(6)}° · δ {parsed.dec.toFixed(6)}°
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
                  onChange={(event) => setFov(event.target.value)}
                  aria-invalid={!fovValid}
                  aria-describedby={!fovValid ? 'cutout-fov-error' : undefined}
                  className={inputCls}
                />
                {!fovValid && (
                  <p id="cutout-fov-error" className="mt-1 text-xs text-red-500">
                    Enter a value from 0.5 to 600.
                  </p>
                )}
              </div>
            </section>

            <section className="space-y-4 border-t border-border p-5" aria-labelledby="cutout-contents-heading">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">Step 2</p>
                <h2 id="cutout-contents-heading" className="mt-1 text-base font-semibold text-text-primary">
                  Figure contents
                </h2>
              </div>

              <div className="rounded-lg border border-border bg-surface-2/50 p-3">
                <label className={`flex items-start gap-3 ${canRgb ? 'text-text-primary' : 'text-text-tertiary'}`}>
                  <input
                    type="checkbox"
                    checked={rgbOn}
                    disabled={!canRgb}
                    onChange={(event) => setRgbOn(event.target.checked)}
                    className="mt-0.5 h-4 w-4 accent-[var(--primary)]"
                  />
                  <span>
                    <span className="block text-sm font-medium">RGB composite</span>
                    <span className="mt-0.5 block text-xs text-text-tertiary">
                      {canRgb ? 'A color overview of the selected position.' : 'Requires at least three bands.'}
                    </span>
                  </span>
                </label>

                {rgbOn && canRgb && (
                  <div className="mt-3 border-t border-border pt-3">
                    <p className={labelCls}>Color recipe</p>
                    <div className="grid grid-cols-3 gap-1" role="radiogroup" aria-label="Color recipe">
                      {COMPOSITE_MODES.map((mode) => {
                        const on = compositeMode === mode;
                        return (
                          <button
                            key={mode}
                            type="button"
                            role="radio"
                            aria-checked={on}
                            onClick={() => {
                              setCompositeMode(mode);
                              if (mode === 'rainbow') setRgbStretch('trilogy');
                            }}
                            className={chipCls(on)}
                          >
                            {COMPOSITE_LABEL[mode]}
                          </button>
                        );
                      })}
                    </div>

                    {compositeMode === 'rainbow' && (
                      <div className="mt-3">
                        <div className="mb-1.5 flex items-center justify-between">
                          <span className={`${labelCls} mb-0`}>Rainbow bands</span>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              className={linkBtnCls}
                              onClick={() => setRainbowBands(bandList)}
                              disabled={rainbowBands.length === bandList.length}
                            >All</button>
                            <button
                              type="button"
                              className={linkBtnCls}
                              onClick={() => setRainbowBands([])}
                              disabled={rainbowBands.length === 0}
                            >None</button>
                          </div>
                        </div>
                        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Rainbow bands">
                          {bandList.map((band) => (
                            <button
                              key={band}
                              type="button"
                              onClick={() => toggleRainbowBand(band)}
                              aria-pressed={rainbowBands.includes(band)}
                              className={chipCls(rainbowBands.includes(band))}
                            >
                              {band.toUpperCase()}
                            </button>
                          ))}
                        </div>
                        <p className={`mt-2 text-xs ${rainbowValid ? 'text-text-tertiary' : 'text-red-500'}`}>
                          {rainbowValid
                            ? 'All chosen bands are combined blue → red using their calibrated Trilogy levels.'
                            : 'Choose at least one rainbow band.'}
                        </p>
                      </div>
                    )}

                    {compositeMode === 'custom' && (
                      <div className="mt-3 grid grid-cols-3 gap-2">
                        {RGB_ROLES.map((role) => (
                          <div key={role}>
                            <label className={labelCls} htmlFor={`cutout-rgb-${role}`}>
                              <span
                                className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle"
                                style={{ background: ROLE_DOT[role] }}
                              />
                              {ROLE_LABEL[role]}
                            </label>
                            <select
                              id={`cutout-rgb-${role}`}
                              value={rgbChannels[role]}
                              onChange={(event) => {
                                const value = event.target.value;
                                setRgbChannels((previous) => ({ ...previous, [role]: value }));
                              }}
                              className={inputCls}
                            >
                              {bandList.map((band) => (
                                <option key={band} value={band}>{band.toUpperCase()}</option>
                              ))}
                            </select>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="rounded-lg border border-border bg-surface-2/50 p-3">
                <label className="flex items-start gap-3 text-text-primary">
                  <input
                    type="checkbox"
                    checked={bandPanelsOn}
                    disabled={!canRgb}
                    onChange={(event) => setBandPanelsOn(event.target.checked)}
                    className="mt-0.5 h-4 w-4 accent-[var(--primary)]"
                  />
                  <span>
                    <span className="block text-sm font-medium">Individual band panels</span>
                    <span className="mt-0.5 block text-xs text-text-tertiary">
                      Add labeled monochrome panels and enable FITS export.
                    </span>
                  </span>
                </label>
                {bandPanelsOn && (
                  <div className="mt-3 border-t border-border pt-3">
                    <div className="mb-1.5 flex items-center justify-between">
                      <span className={`${labelCls} mb-0`}>Bands</span>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          className={linkBtnCls}
                          onClick={() => setSelectedBands(bandList)}
                          disabled={selectedBands.length === bandList.length}
                        >All</button>
                        <button
                          type="button"
                          className={linkBtnCls}
                          onClick={() => setSelectedBands([])}
                          disabled={selectedBands.length === 0}
                        >None</button>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1.5" role="group" aria-label="Individual band panels">
                      {bandList.map((band) => (
                        <button
                          key={band}
                          type="button"
                          onClick={() => toggleBand(band)}
                          aria-pressed={selectedBands.includes(band)}
                          className={chipCls(selectedBands.includes(band))}
                        >
                          {band.toUpperCase()}
                        </button>
                      ))}
                    </div>
                    <p className="mt-2 text-xs text-text-tertiary">
                      {selectedBands.length} of {bandList.length} selected
                    </p>
                  </div>
                )}
              </div>

              <div className="rounded-lg border border-border bg-surface-2/50 p-3">
                <label className={`flex items-start gap-3 ${shutterEligible ? 'text-text-primary' : 'text-text-tertiary'}`}>
                  <input
                    type="checkbox"
                    checked={shutters}
                    disabled={!shutterEligible}
                    onChange={(event) => setShutters(event.target.checked)}
                    className="mt-0.5 h-4 w-4 accent-[var(--primary)]"
                  />
                  <span>
                    <span className="block text-sm font-medium">NIRSpec shutter footprints</span>
                    <span className="mt-0.5 block text-xs text-text-tertiary">
                      {!canOverlayShutters
                        ? 'Unavailable for share-link accounts.'
                        : fovValid && fovNum > SHUTTER_OVERLAY_MAX_FOV_ARCSEC
                          ? `Available up to ${SHUTTER_OVERLAY_MAX_FOV_ARCSEC}″ FOV.`
                          : 'Overlay shutters in this area, colored by observation.'}
                    </span>
                  </span>
                </label>
              </div>
            </section>

            <section className="border-t border-border p-5">
              <button
                type="button"
                onClick={() => setShowDisplay((value) => !value)}
                aria-expanded={showDisplay}
                aria-controls="cutout-display-settings"
                className="flex w-full items-center justify-between text-left focus:outline-none focus:ring-2 focus:ring-primary/50"
              >
                <span>
                  <span className="block text-sm font-medium text-text-primary">Advanced appearance</span>
                  {!showDisplay && (
                    <span className="mt-0.5 block text-xs text-text-tertiary">
                      {bandPanelsOn && `${scaling === 'snr' ? 'SNR' : 'Percentile'} · ${stretch} · ${colormap} · `}
                      {rgbOn && `${compositeMode === 'rainbow' ? 'Trilogy' : `RGB ${rgbStretch}`} · `}
                      {panelSize} px
                    </span>
                  )}
                </span>
                {showDisplay ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
              </button>

              {showDisplay && (
                <div id="cutout-display-settings" className="mt-4 space-y-5">
                  {bandPanelsOn && (
                    <div>
                      <p className="mb-2 text-xs font-medium text-text-secondary">Individual band panels</p>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className={labelCls} htmlFor="cutout-scaling">Scaling</label>
                          <select
                            id="cutout-scaling"
                            value={scaling}
                            onChange={(event) => setScaling(event.target.value as Scaling)}
                            className={inputCls}
                          >
                            <option value="snr">SNR (cutout σ)</option>
                            <option value="percentile">Percentile</option>
                          </select>
                        </div>
                        <div>
                          <label className={labelCls} htmlFor="cutout-stretch">Stretch</label>
                          <select
                            id="cutout-stretch"
                            value={stretch}
                            onChange={(event) => setStretch(event.target.value as Stretch)}
                            className={inputCls}
                          >
                            {STRETCHES.map((value) => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className={labelCls} htmlFor="cutout-colormap">Colormap</label>
                          <select
                            id="cutout-colormap"
                            value={colormap}
                            onChange={(event) => setColormap(event.target.value as Colormap)}
                            className={inputCls}
                          >
                            {COLORMAPS.map((value) => <option key={value} value={value}>{value}</option>)}
                          </select>
                        </div>
                      </div>
                      {scaling === 'snr' && (
                        <div className="mt-3">
                          <span className={labelCls}>SNR range (σ)</span>
                          <div className="flex items-center gap-2">
                            <input
                              aria-label="Black point in sigma"
                              type="number"
                              step="any"
                              value={snrLo}
                              onChange={(event) => setSnrLo(event.target.value)}
                              aria-invalid={!snrValid}
                              className={inputCls}
                            />
                            <span className="text-xs text-text-tertiary">to</span>
                            <input
                              aria-label="White point in sigma"
                              type="number"
                              step="any"
                              value={snrHi}
                              onChange={(event) => setSnrHi(event.target.value)}
                              aria-invalid={!snrValid}
                              className={inputCls}
                            />
                          </div>
                          {!snrValid && <p className="mt-1 text-xs text-red-500">High must be above low.</p>}
                        </div>
                      )}
                    </div>
                  )}

                  {rgbOn && (
                    <div className={bandPanelsOn ? 'border-t border-border pt-4' : ''}>
                      <p className="mb-2 text-xs font-medium text-text-secondary">RGB composite</p>
                      {compositeMode !== 'rainbow' && (
                        <div>
                          <label className={labelCls} htmlFor="cutout-rgb-stretch">Rendering</label>
                          <select
                            id="cutout-rgb-stretch"
                            value={rgbStretch}
                            onChange={(event) => setRgbStretch(event.target.value as RgbStretch)}
                            className={inputCls}
                          >
                            <option value="auto">Dataset default</option>
                            <option value="trilogy">Trilogy</option>
                            <option value="asinh">Asinh</option>
                            <option value="log">Log</option>
                            <option value="sqrt">Square root</option>
                            <option value="linear">Linear</option>
                          </select>
                        </div>
                      )}
                      {compositeMode === 'rainbow' && (
                        <p className="text-xs text-text-tertiary">
                          Rainbow always uses calibrated Trilogy levels for every chosen band.
                        </p>
                      )}
                      {trilogyControlsActive && (
                        <div className="mt-3 grid grid-cols-2 gap-3">
                          <div>
                            <label className={labelCls} htmlFor="cutout-noiselum">Noise luminance</label>
                            <input
                              id="cutout-noiselum"
                              type="number"
                              min={0.000001}
                              max={0.999999}
                              step="any"
                              placeholder="dataset default"
                              value={noiselum}
                              onChange={(event) => setNoiselum(event.target.value)}
                              aria-invalid={!noiselumValid}
                              className={inputCls}
                            />
                            {!noiselumValid && <p className="mt-1 text-xs text-red-500">Use a value between 0 and 1.</p>}
                          </div>
                          <div>
                            <label className={labelCls} htmlFor="cutout-satpercent">Saturate %</label>
                            <input
                              id="cutout-satpercent"
                              type="number"
                              min={0.001}
                              max={1}
                              step="any"
                              placeholder="dataset default"
                              value={satpercent}
                              onChange={(event) => setSatpercent(event.target.value)}
                              aria-invalid={!satpercentValid}
                              className={inputCls}
                            />
                            {!satpercentValid && <p className="mt-1 text-xs text-red-500">Use 0.001–1.</p>}
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  <div className={(bandPanelsOn || rgbOn) ? 'border-t border-border pt-4' : ''}>
                    <p className="mb-2 text-xs font-medium text-text-secondary">Layout</p>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className={labelCls} htmlFor="cutout-size">Panel size (px)</label>
                        <input
                          id="cutout-size"
                          type="number"
                          min={64}
                          max={1024}
                          step={1}
                          value={panelSize}
                          onChange={(event) => setPanelSize(event.target.value)}
                          aria-invalid={!panelSizeValid}
                          className={inputCls}
                        />
                        {!panelSizeValid && <p className="mt-1 text-xs text-red-500">Use 64–1024.</p>}
                      </div>
                      <div>
                        <label className={labelCls} htmlFor="cutout-cols">Columns</label>
                        <input
                          id="cutout-cols"
                          type="number"
                          min={1}
                          step={1}
                          placeholder="one row"
                          value={cols}
                          onChange={(event) => setCols(event.target.value)}
                          aria-invalid={!colsValid}
                          className={inputCls}
                        />
                        {!colsValid && <p className="mt-1 text-xs text-red-500">Use a positive whole number.</p>}
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </section>

            <div className="space-y-2 border-t border-border p-5">
              {formIssue && (showValidationSummary || coordText.trim() !== '') && (
                <p className="text-xs text-red-500" role="alert">{formIssue}</p>
              )}
              <button
                type="submit"
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-on-primary transition-colors hover:bg-primary-hover focus:outline-none focus:ring-2 focus:ring-primary/50"
              >
                {previewLoading ? (
                  loadingQuery === figureQuery ? <X className="h-4 w-4" /> : <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ImageIcon className="h-4 w-4" />
                )}
                {previewLoading
                  ? loadingQuery === figureQuery
                    ? 'Cancel generation'
                    : ready
                      ? 'Generate updated preview'
                      : 'Cancel generation'
                  : artifact && artifact.query !== figureQuery
                    ? 'Generate updated preview'
                    : ready
                      ? 'Generate preview'
                      : coordText.trim() === ''
                        ? 'Enter a position to generate'
                        : 'Fix settings to generate'}
              </button>
              <p className="text-center text-xs text-text-tertiary">
                Press Enter from a field to generate.
              </p>
            </div>
          </form>

          <section className="min-h-[520px] overflow-hidden rounded-xl border border-border bg-card lg:sticky lg:top-4" aria-labelledby="cutout-preview-heading">
            <div className="flex flex-col gap-3 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 id="cutout-preview-heading" className="text-base font-semibold text-text-primary">Preview</h2>
                <p
                  className={`mt-0.5 flex items-center gap-1.5 text-xs ${
                    !previewLoading && !currentFailure && artifact && !previewStale
                      ? 'text-emerald-500'
                      : currentFailure
                        ? 'text-red-500'
                        : 'text-text-tertiary'
                  }`}
                  role="status"
                  aria-live="polite"
                >
                  {!previewLoading && !currentFailure && artifact && !previewStale && <CheckCircle2 className="h-3.5 w-3.5" />}
                  {previewLoading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {previewStatus}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                {parsed && (
                  <Link
                    href={`/map?field=${encodeURIComponent(field)}&ra=${parsed.ra}&dec=${parsed.dec}`}
                    className={secondaryBtnCls(true)}
                  >
                    <MapIcon className="h-4 w-4" />
                    View on map
                  </Link>
                )}
                <a
                  href={pngEnabled ? artifact.url : undefined}
                  download={pngEnabled ? artifact.filename : undefined}
                  aria-disabled={!pngEnabled}
                  title={previewStale ? 'Regenerate to download the current settings' : undefined}
                  className={secondaryBtnCls(pngEnabled)}
                >
                  <Download className="h-4 w-4" />
                  Download PNG
                </a>
              </div>
            </div>

            {currentFailure && (
              <div className="mx-4 mt-4 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500" role="alert">
                {currentFailure}
              </div>
            )}

            <div className="p-4 sm:p-6">
              <div className="relative flex min-h-[360px] items-center justify-center overflow-hidden rounded-lg border border-border bg-black/90">
                {artifact ? (
                  <>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={artifact.url}
                      alt="Generated NIRCam cutout figure"
                      className="h-auto w-full object-contain"
                    />
                    {(previewStale || previewLoading) && (
                      <div className="absolute inset-0 flex items-center justify-center bg-black/55 p-6 text-center text-white backdrop-blur-[1px]">
                        <div>
                          {previewLoading && <Loader2 className="mx-auto mb-3 h-7 w-7 animate-spin" />}
                          <p className="text-sm font-medium">
                            {previewLoading ? 'Generating updated preview…' : 'Preview out of date'}
                          </p>
                          {!previewLoading && (
                            <p className="mt-1 text-xs text-white/70">Generate again to apply your changes.</p>
                          )}
                        </div>
                      </div>
                    )}
                  </>
                ) : previewLoading ? (
                  <div className="text-center text-white/70">
                    <Loader2 className="mx-auto mb-3 h-8 w-8 animate-spin" />
                    <p className="text-sm">Rendering your cutout…</p>
                  </div>
                ) : (
                  <div className="max-w-sm px-6 text-center text-white/55">
                    <ImageIcon className="mx-auto mb-3 h-10 w-10 opacity-60" />
                    <p className="text-sm font-medium text-white/75">Your figure will appear here</p>
                    <p className="mt-1 text-xs">Choose a position and contents, then generate the preview.</p>
                  </div>
                )}
              </div>

              {artifact && (
                <div className="mt-4 flex flex-wrap gap-2" aria-label="Rendered figure contents">
                  {artifact.rgbLabel && (
                    <span className="rounded-full bg-primary/10 px-2.5 py-1 text-xs text-primary">{artifact.rgbLabel}</span>
                  )}
                  {artifact.bandCount > 0 && (
                    <span className="rounded-full bg-surface-2 px-2.5 py-1 text-xs text-text-secondary">
                      {artifact.bandCount} band panel{artifact.bandCount === 1 ? '' : 's'}
                    </span>
                  )}
                  {artifact.shutterCount > 0 && (
                    <span className="rounded-full bg-surface-2 px-2.5 py-1 text-xs text-text-secondary">
                      {artifact.shutterCount} shutter footprint{artifact.shutterCount === 1 ? '' : 's'}
                    </span>
                  )}
                </div>
              )}

              {artifact?.shutterRequested && (
                <p className="mt-3 text-xs text-text-tertiary">
                  {artifact.shutterCount > 0
                    ? 'Shutters are colored by observation; stuck-closed shutters are red dashed.'
                    : 'No NIRSpec shutter footprints were found in this cutout.'}
                </p>
              )}
            </div>

            <div className="border-t border-border bg-surface-2/30 p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-medium text-text-primary">Science bands</p>
                  {activeBands.length > 0 ? (
                    <p className={`mt-0.5 text-xs ${overBudget ? 'text-red-500' : 'text-text-tertiary'}`}>
                      Native FITS · {nativePx ?? '—'}×{nativePx ?? '—'} px · {nBands} band{nBands === 1 ? '' : 's'}
                      {estMb !== null && ` · ~${estMb < 1 ? estMb.toFixed(2) : estMb.toFixed(1)} MB`}
                      {overBandBudget && ' · Reduce FOV to fit the per-band limit'}
                      {!overBandBudget && overTotalBudget && ' · Reduce FOV or band count'}
                    </p>
                  ) : (
                    <p className="mt-0.5 text-xs text-text-tertiary">
                      Turn on individual band panels to select FITS bands.
                    </p>
                  )}
                </div>
                <a
                  href={fitsEnabled ? `/api/v1/cutout/fits?${fitsParams.toString()}` : undefined}
                  aria-disabled={!fitsEnabled}
                  className={secondaryBtnCls(fitsEnabled)}
                >
                  <Download className="h-4 w-4" />
                  Download {nBands || ''} FITS band{nBands === 1 ? '' : 's'}
                </a>
              </div>
              <p className="mt-3 text-xs text-text-tertiary">
                FITS cutouts retain each band&apos;s native WCS and are photometrically faithful to ~0.03%.
              </p>
            </div>
          </section>
        </div>
      )}
    </div>
  );
};
