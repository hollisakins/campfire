'use client';

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { Loader2, AlertCircle } from 'lucide-react';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import { useSpectrumJson, useSpectrum1d, useRedshiftFit, useSpectrumSidecarUrls, fullPayloadIsSeparate } from '@/lib/hooks/useSpectrumJson';
import type { SpectrumData } from '@/app/api/spectrum/route';
import { useTheme } from '@/lib/contexts/ThemeContext';
import type { Colorscale2D, FluxUnit } from '@/lib/types';
import { COLORSCALE_2D_OPTIONS } from '@/lib/types';
import {
  getPlotColors,
  convertToFlambda,
  getFluxLabel,
  getHoverLabel,
  computeYRange,
  parseXRangeFromRelayout,
  buildRestFrameAxis,
  buildRestFrameAxisActivationTrace,
  buildEmissionLineTraces,
  buildEmissionLineOverlayAxis,
} from './plotting-utils';
import {
  FluxUnitToggle,
  EmissionLinesControl,
  PlotCheckbox,
  RedshiftSliderControl,
  ControlDivider,
} from './PlottingControls';
import { LazyPlot as Plot } from '@/components/plot/LazyPlot';

// Custom colorscale definitions for scales not built into Plotly.js
// Plasma, Inferno, and Magma are matplotlib colormaps not available in Plotly.js
// Values sampled from https://bids.github.io/colormap/
type PlotlyColorscale = string | Array<[number, string]>;

const CUSTOM_COLORSCALES: Record<string, PlotlyColorscale> = {
  Viridis: 'Viridis',
  Cividis: 'Cividis',
  Greys: 'Greys',
  Plasma: [
    [0, '#0d0887'],
    [0.1, '#41049d'],
    [0.2, '#6a00a8'],
    [0.3, '#8f0da4'],
    [0.4, '#b12a90'],
    [0.5, '#cc4778'],
    [0.6, '#e16462'],
    [0.7, '#f2844b'],
    [0.8, '#fca636'],
    [0.9, '#fcce25'],
    [1, '#f0f921'],
  ],
  Inferno: [
    [0, '#000004'],
    [0.1, '#1b0c41'],
    [0.2, '#4a0c6b'],
    [0.3, '#781c6d'],
    [0.4, '#a52c60'],
    [0.5, '#cf4446'],
    [0.6, '#ed6925'],
    [0.7, '#fb9b06'],
    [0.8, '#f7d13d'],
    [0.9, '#fcffa4'],
    [1, '#fcffa4'],
  ],
  Magma: [
    [0, '#000004'],
    [0.1, '#180f3d'],
    [0.2, '#440f76'],
    [0.3, '#721f81'],
    [0.4, '#9e2f7f'],
    [0.5, '#cd4071'],
    [0.6, '#f1605d'],
    [0.7, '#fd9668'],
    [0.8, '#feca8d'],
    [0.9, '#fcfdbf'],
    [1, '#fcfdbf'],
  ],
};

// Get the Plotly-compatible colorscale value
const getPlotlyColorscale = (name: Colorscale2D): PlotlyColorscale => {
  return CUSTOM_COLORSCALES[name] || 'Viridis';
};

interface SpectrumPlotProps {
  fitsPath: string;
  grating: string;
  initialRedshift?: number | null;
  inspectionMode?: boolean;
  onRedshiftChange?: (value: number) => void;
  /** When true, drop the outer rounded-card wrapper so the plot can be embedded
   *  directly inside another container (e.g. SpectrumDetailCard). */
  bare?: boolean;
}

export const SpectrumPlot: React.FC<SpectrumPlotProps> = ({
  fitsPath,
  grating,
  initialRedshift,
  inspectionMode = false,
  onRedshiftChange,
  bare = false,
}) => {
  const { spectrumPreferences, accentColorHex } = usePreferences();
  const { resolvedTheme } = useTheme();

  // Sidecars come from the shared TanStack cache (lib/hooks/useSpectrumJson):
  // the inspection prefetch, RedshiftFitSummary and this plot all read the
  // same entries, so a path is fetched once per page (#500).
  //
  // 1-D before 2-D (perf T2-D2, #508): the 1-D sidecar paints the spectrum
  // and profile as soon as it lands; the heatmap joins when the full JSON
  // (the 2-D S/N array, 80–95 % of the bytes) arrives. A spectrum deployed
  // before the sidecar existed answers the 1-D query with its full payload,
  // which then serves the heatmap too.
  const oneDQuery = useSpectrum1d(fitsPath);
  // The full query waits for the url resolve (which the 1-D fetch awaits
  // anyway, so this costs no latency) and is skipped when the 1-D query
  // already delivers the full payload (pre-backfill spectra).
  // A failed resolve settles too: the payload fetchers then stream from the
  // app routes, and the full query must still run for the heatmap. Should
  // the resolve have been wrong (a 1-D answer without the 2-D array), the
  // landed 1-D payload re-enables it.
  const sidecarUrls = useSpectrumSidecarUrls(fitsPath);
  const data = oneDQuery.data ?? null;
  const fullQuery = useSpectrumJson(
    fitsPath,
    !sidecarUrls.isPending &&
      (fullPayloadIsSeparate(sidecarUrls.data) || (data !== null && !('snr_2d' in data))),
  );
  const heat: SpectrumData | null =
    fullQuery.data ?? (data && 'snr_2d' in data ? (data as SpectrumData) : null);
  const loading = oneDQuery.isPending;
  const error = oneDQuery.error ? oneDQuery.error.message : null;
  const heatError = fullQuery.error
    ? (fullQuery.error instanceof Error ? fullQuery.error.message : 'Failed to load 2-D spectrum')
    : null;
  const [fluxUnit, setFluxUnit] = useState<FluxUnit>(spectrumPreferences.fluxUnit);
  const [colorscale, setColorscale] = useState<Colorscale2D>(spectrumPreferences.colorscale2D);
  const [showEmissionLines, setShowEmissionLines] = useState(inspectionMode);
  // Show best-fit model overlay + χ²(z) panel. Defaults on in inspection mode.
  const [showModel, setShowModel] = useState(inspectionMode);
  // The zfit sidecar feeds only the model overlay and the χ²(z) panel, so it
  // is fetched once they are shown (the fit summary reads its scalars from
  // the spectra row, #508). An entry the inspection prefetch or the summary
  // already put in the shared cache serves immediately either way.
  const fitQuery = useRedshiftFit(fitsPath, showModel);
  const fitData = fitQuery.data ?? null;
  const [redshift, setRedshift] = useState(initialRedshift ?? 0);
  const [colorMin, setColorMin] = useState(spectrumPreferences.snrMin);
  const [colorMax, setColorMax] = useState(spectrumPreferences.snrMax);
  // Auto-scale the 1D y-axis to real spectral features (computeYRange). On by
  // default; toggling it off falls back to Plotly's full autorange so noise
  // spikes and the full flux range are visible. The 'y' key toggles it in
  // inspection mode (issue #245).
  const [autoStretch, setAutoStretch] = useState(true);

  // Track observed wavelength range for rest-frame axis tick computation
  // null = full range (autorange), [min, max] = user-zoomed range in μm
  const [obsRange, setObsRange] = useState<[number, number] | null>(null);

  // Reset zoom state when switching spectra
  useEffect(() => { setObsRange(null); }, [fitsPath]);

  // Update state when preferences change
  useEffect(() => {
    setFluxUnit(spectrumPreferences.fluxUnit);
    setColorscale(spectrumPreferences.colorscale2D);
    setColorMin(spectrumPreferences.snrMin);
    setColorMax(spectrumPreferences.snrMax);
  }, [spectrumPreferences]);

  // Update redshift when prop changes (inspection mode navigation)
  useEffect(() => {
    if (initialRedshift !== null && initialRedshift !== undefined) {
      setRedshift(initialRedshift);
    }
  }, [initialRedshift]);

  // Inspection-mode plot shortcuts (issue #245): 'f' toggles flux units
  // (fν ↔ fλ), 'y' toggles the 1D y-axis auto-stretch. Only wired in
  // inspection mode, where exactly one SpectrumPlot is mounted; the remaining
  // inspection shortcuts live in InspectionModeOverlay.
  useEffect(() => {
    if (!inspectionMode) return;
    const handler = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return;
      // Only plain F/Y are ours; let modifier combos (Ctrl/Cmd+F browser find,
      // Cmd+Y, etc.) through untouched.
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'f' || e.key === 'F') {
        e.preventDefault();
        setFluxUnit(prev => (prev === 'fnu' ? 'flambda' : 'fnu'));
      } else if (e.key === 'y' || e.key === 'Y') {
        e.preventDefault();
        setAutoStretch(prev => !prev);
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [inspectionMode]);

  // Get current plot colors based on theme
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const plotColors = useMemo(() => getPlotColors(), [resolvedTheme]);

  // Memoize processed spectrum data - must be before early returns
  const processedData = useMemo(() => {
    if (!data) return null;

    const validIndices = data.fnu
      .map((v, i) => (v !== null ? i : -1))
      .filter(i => i !== -1);

    const wave = validIndices.map(i => data.wave[i]);
    const fnuValues = validIndices.map(i => data.fnu[i] as number);
    const fnuErr = validIndices.map(i => data.fnu_err[i]);

    // Calculate f_lambda values
    const flambda = fnuValues.map((f, i) => convertToFlambda(f, wave[i]));
    const flambdaErr = fnuErr.map((err, i) =>
      err !== null ? convertToFlambda(err, wave[i]) : null
    );

    // Process model data if available (inspection mode)
    let modelWave: (number | null)[] | null = null;
    let modelFnu: (number | null)[] | null = null;
    let modelFlambda: (number | null)[] | null = null;

    if (fitData) {
      modelWave = fitData.model_wave;
      modelFnu = fitData.model_fnu;
      // Null samples (non-finite in the FITS) must survive as nulls — Plotly
      // renders them as gaps, while arithmetic would coerce null to 0 and
      // plot a false zero-flux model point.
      modelFlambda = fitData.model_fnu.map((f, i) => {
        const w = fitData.model_wave[i];
        return f !== null && w !== null ? convertToFlambda(f, w) : null;
      });
    }

    return { wave, fnu: fnuValues, fnuErr, flambda, flambdaErr, modelWave, modelFnu, modelFlambda };
  }, [data, fitData]);

  // Memoize the redshift-INDEPENDENT figure — every trace but the emission
  // lines, and every axis but the rest-frame overlay. Dragging the redshift
  // slider must not rebuild the heatmap / spectrum / profile / model traces:
  // they keep their identity across frames so Plotly.react can skip them and
  // only the emission-line traces + xaxis3 change (#500).
  const basePlotData = useMemo(() => {
    if (!data || !processedData) return null;

    const { wave, fnu, fnuErr, flambda, flambdaErr, modelWave, modelFnu, modelFlambda } = processedData;

    // Select flux values based on current unit
    const flux = fluxUnit === 'fnu' ? fnu : flambda;
    const fluxErr = fluxUnit === 'fnu' ? fnuErr : flambdaErr;
    const modelFlux = fluxUnit === 'fnu' ? modelFnu : modelFlambda;
    const fluxLabel = getFluxLabel(fluxUnit);
    const hoverLabel = getHoverLabel(fluxUnit);

    // Calculate upper and lower bounds for error band
    const upperBound = flux.map((f, i) => {
      const err = fluxErr[i];
      return err !== null ? f + err : f;
    });
    const lowerBound = flux.map((f, i) => {
      const err = fluxErr[i];
      return err !== null ? f - err : f;
    });

    // Get wavelength range from non-NaN values
    let waveMin = Infinity;
    let waveMax = -Infinity;
    for (const w of wave) {
      if (!isFinite(w)) continue;
      if (w < waveMin) waveMin = w;
      if (w > waveMax) waveMax = w;
    }

    // Build step-function coordinates for cross-dispersion profile
    // Using 'vh' (vertical-horizontal) pattern to match matplotlib's where='post'
    const buildStepCoords = (xVals: number[], yVals: number[]) => {
      const stepX: number[] = [];
      const stepY: number[] = [];
      for (let i = 0; i < xVals.length; i++) {
        // Start of step (vertical line up)
        stepX.push(xVals[i]);
        stepY.push(i === 0 ? yVals[0] - 0.5 : yVals[i - 1] + 0.5);
        // End of step (at current y)
        stepX.push(xVals[i]);
        stepY.push(yVals[i] + 0.5);
      }
      return { stepX, stepY };
    };

    // Check if profile data exists (for backwards compatibility)
    const hasProfile = data.profile && data.profile_fit && data.profile_pix;

    // Combined traces for stacked subplots
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const traces: any[] = [
      // 2D S/N heatmap (top-left subplot, shares xaxis with 1D spectrum) —
      // only once the full payload is in; the panel stays empty until then.
      ...(heat ? [{
        z: heat.snr_2d,
        x: heat.wave,
        y: hasProfile ? data.profile_pix : undefined,
        type: 'heatmap' as const,
        colorscale: getPlotlyColorscale(colorscale),
        zmin: colorMin,
        zmax: colorMax,
        showscale: false, // Remove colorbar - using profile panel instead
        hovertemplate: 'λ: %{x:.3f} μm<br>y: %{y:.1f} pix<br>S/N: %{z:.1f}<extra></extra>',
        xaxis: 'x',
        yaxis: 'y2',
      }] : []),
      // Error band (bottom subplot)
      {
        x: [...wave, ...wave.slice().reverse()],
        y: [...upperBound, ...lowerBound.slice().reverse()],
        fill: 'toself',
        fillcolor: accentColorHex + '26', // Add 15% opacity (hex 26 ≈ 15%)
        line: { color: 'transparent', shape: 'hvh' },
        name: '1σ error',
        hoverinfo: 'skip' as const,
        showlegend: true,
        xaxis: 'x',
        yaxis: 'y',
      },
      // Main spectrum line (bottom subplot)
      {
        x: wave,
        y: flux,
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'Flux',
        line: {
          color: accentColorHex,
          width: 1.5,
          shape: 'hvh',
        },
        hovertemplate: `λ: %{x:.3f} μm<br>${hoverLabel}: %{y:.3e}<extra></extra>`,
        xaxis: 'x',
        yaxis: 'y',
      },
      // Invisible trace keeping the rest-frame overlay axis rendered
      buildRestFrameAxisActivationTrace(waveMin, 'x3', 'y4'),
    ];

    // Add cross-dispersion profile traces if data exists
    if (hasProfile) {
      const { stepX: profStepX, stepY: profStepY } = buildStepCoords(data.profile, data.profile_pix);
      const { stepX: fitStepX, stepY: fitStepY } = buildStepCoords(data.profile_fit, data.profile_pix);

      // Optimal extraction weight fill (red, behind the profile line)
      traces.push({
        x: [...fitStepX, 0, 0],
        y: [...fitStepY, fitStepY[fitStepY.length - 1], fitStepY[0]],
        fill: 'toself',
        fillcolor: 'rgba(239, 68, 68, 0.3)',
        line: { color: 'transparent' },
        name: 'Extraction weight',
        hoverinfo: 'skip' as const,
        showlegend: false,
        xaxis: 'x2',
        yaxis: 'y3',
      });

      // Cross-dispersion profile line (adapts to theme)
      traces.push({
        x: profStepX,
        y: profStepY,
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'Spatial profile',
        line: {
          color: plotColors.text,
          width: 1.5,
        },
        hovertemplate: 'Profile: %{x:.2f}<br>y: %{y:.1f} pix<extra></extra>',
        showlegend: false,
        xaxis: 'x2',
        yaxis: 'y3',
      });

      // Zero line for profile panel
      traces.push({
        x: [0, 0],
        y: [-10, 10],
        type: 'scatter' as const,
        mode: 'lines' as const,
        line: { color: plotColors.grid, width: 1 },
        hoverinfo: 'skip' as const,
        showlegend: false,
        xaxis: 'x2',
        yaxis: 'y3',
      });
    }

    // Add best-fit model trace if user toggled "Model" on and zfit is available.
    if (showModel && modelWave && modelFlux) {
      traces.push({
        x: modelWave,
        y: modelFlux,
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'Model',
        line: {
          color: '#f97316',
          width: 2,
        },
        hovertemplate: `λ: %{x:.3f} μm<br>${hoverLabel}: %{y:.3e}<extra></extra>`,
        xaxis: 'x',
        yaxis: 'y',
      });
    }

    // Smart y-axis auto-scaling (works in both normal and inspection mode).
    // The model informs the range only while it is actually drawn — otherwise
    // Auto-y would scale to an invisible trace, and the same spectrum would
    // stretch differently depending on whether fit data happened to exist.
    const yAxisRange = computeYRange(flux, fluxErr, {
      modelFlux: showModel ? modelFlux : null,
      modelWave: showModel ? processedData.modelWave : null,
      dataWave: wave,
    });

    // Layout configuration with profile panel (xaxis3, the rest-frame
    // overlay, is added by the redshift-dependent memo below)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const layout: any = {
      // Per-axis uirevision instead of top-level: each axis's revision key
      // encodes exactly the state that should invalidate it (see the y-axis
      // and buildRestFrameAxis comments).
      font: { family: 'Inter, system-ui, sans-serif', color: plotColors.text },
      title: {
        text: `${grating} Spectrum`,
        font: { size: 16 },
      },
      // X-axis: Shared wavelength axis for both 2D and 1D spectra (linked zoom/pan)
      xaxis: {
        title: { text: 'Wavelength (μm)' },
        gridcolor: plotColors.grid,
        zerolinecolor: plotColors.grid,
        domain: [0, 0.90],
        range: [waveMin, waveMax],
        uirevision: 'constant', // Preserve user zoom across re-renders
      },
      // X-axis for profile panel (top-right, narrow)
      xaxis2: {
        gridcolor: plotColors.grid,
        zerolinecolor: plotColors.grid,
        domain: [0.92, 1.0],
        anchor: 'y3' as const,
        showticklabels: false,
        range: [-0.3, 1.2],
        fixedrange: true,
        uirevision: 'constant',
      },
      // Y-axis for 1D spectrum (bottom)
      yaxis: {
        title: { text: fluxLabel },
        gridcolor: plotColors.grid,
        zerolinecolor: plotColors.grid,
        exponentformat: 'e' as const,
        domain: [0, 0.7],
        anchor: 'x' as const,
        // The y-axis uirevision must change whenever the meaning of the y
        // coordinate changes, or Plotly preserves a user-zoomed range that no
        // longer makes sense: toggling autoStretch must re-apply the
        // range/autorange below, and switching flux units must drop a zoom
        // set in the other unit system (fν μJy vs fλ erg/s/cm²/Å differ by
        // ~19 orders of magnitude — a preserved range renders as a blank plot).
        uirevision: `${fluxUnit}-${autoStretch ? 'auto' : 'full'}`,
        ...(autoStretch && yAxisRange
          ? { range: yAxisRange, autorange: false as const }
          : { autorange: true as const }),
      },
      // Y-axis for 2D heatmap (top-left)
      yaxis2: {
        title: { text: 'y [pix]' },
        gridcolor: plotColors.grid,
        domain: [0.78, 1],
        anchor: 'x' as const,
        range: [-10, 10],
        uirevision: 'constant',
      },
      // Y-axis for profile panel (top-right, matches yaxis2)
      yaxis3: {
        gridcolor: plotColors.grid,
        domain: [0.78, 1],
        anchor: 'x2' as const,
        matches: 'y2' as const, // Link range to yaxis2
        showticklabels: false,
        uirevision: 'constant',
      },
      // Y-axis for emission lines — hidden overlay on yaxis, fixed [0,1] range
      // so emission line traces never affect auto-scaling or double-click reset
      yaxis4: buildEmissionLineOverlayAxis('y'),
      margin: { l: 80, r: 20, t: 50, b: 50 },
      paper_bgcolor: plotColors.paper,
      plot_bgcolor: plotColors.bg,
      hovermode: 'x unified' as const,
      showlegend: true,
      legend: {
        x: 0.96,
        xanchor: 'center' as const,
        y: 0.75,
        yanchor: 'top' as const,
        bgcolor: plotColors.paper,
        bordercolor: plotColors.grid,
        borderwidth: 1,
        font: { size: 10 },
        tracegroupgap: 2,
        uirevision: 'constant',
      },
    };

    return { traces, layout, waveMin, waveMax };
  }, [data, heat, processedData, fluxUnit, colorscale, colorMin, colorMax, accentColorHex, plotColors, grating, showModel, autoStretch]);

  // Emission line markers (drawn on the hidden overlay yaxis4 so they never
  // affect autoscaling or double-click reset) — the only traces that move
  // with the redshift slider.
  const emissionTraces = useMemo(() => {
    if (!basePlotData || !showEmissionLines) return [];
    return buildEmissionLineTraces(redshift, basePlotData.waveMin, basePlotData.waveMax, {
      yaxis: 'y4',
      grating,
      showlegend: true,
    });
  }, [basePlotData, showEmissionLines, redshift, grating]);

  // X-axis: Rest-frame wavelength (Å), overlays primary axis (shared builder —
  // see buildRestFrameAxis for the uirevision contract). Tracks the redshift
  // and the current observed view (user zoom, else the full range).
  const restFrameAxis = useMemo(() => {
    if (!basePlotData) return null;
    return buildRestFrameAxis({
      redshift,
      obsMin: obsRange ? obsRange[0] : basePlotData.waveMin,
      obsMax: obsRange ? obsRange[1] : basePlotData.waveMax,
      colors: plotColors,
      domain: [0, 0.90],
      anchor: 'y',
    });
  }, [basePlotData, redshift, obsRange, plotColors]);

  const plotData = useMemo(() => {
    if (!basePlotData) return null;
    return {
      traces: emissionTraces.length > 0 ? [...basePlotData.traces, ...emissionTraces] : basePlotData.traces,
      layout: { ...basePlotData.layout, xaxis3: restFrameAxis },
    };
  }, [basePlotData, emissionTraces, restFrameAxis]);

  // χ²(z) panel data — only built when the user toggles the Model overlay on.
  const chi2PlotData = useMemo(() => {
    if (!showModel || !fitData) return null;
    // Null best-fit scalars mean the chi2 grid had no finite minimum
    // (degenerate fit) — there is nothing meaningful to plot.
    if (fitData.redshift === null || fitData.chi2_min === null) return null;
    let chi2Min = Infinity;
    let chi2Max = -Infinity;
    for (const v of fitData.chi2_grid) {
      if (v === null) continue;
      if (v < chi2Min) chi2Min = v;
      if (v > chi2Max) chi2Max = v;
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const traces: any[] = [
      {
        x: fitData.z_grid,
        y: fitData.chi2_grid,
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'χ²(z)',
        line: { color: '#3b82f6', width: 2 },
        hovertemplate: 'z: %{x:.4f}<br>χ²: %{y:.2f}<extra></extra>',
        showlegend: false,
      },
      {
        x: [fitData.redshift, fitData.redshift],
        y: [chi2Min * 0.5, chi2Max * 2],
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'Best fit',
        line: { color: '#f97316', width: 2, dash: 'dash' },
        hovertemplate: `Best fit<br>z: ${fitData.redshift.toFixed(4)}<br>χ²_min: ${fitData.chi2_min.toFixed(2)}<extra></extra>`,
        showlegend: false,
      },
    ];

    return {
      traces,
      layout: {
        title: {
          text: `Redshift fit · z = ${fitData.redshift.toFixed(4)}, χ²_min = ${fitData.chi2_min.toFixed(2)}, conf = ${fitData.confidence !== null ? `${fitData.confidence.toFixed(1)}%` : '—'}`,
          font: { size: 12, color: plotColors.text },
        },
        font: { family: 'Inter, system-ui, sans-serif', color: plotColors.text },
        xaxis: {
          title: { text: 'Redshift', font: { color: plotColors.text } },
          tickfont: { color: plotColors.textSecondary },
          gridcolor: plotColors.grid,
          zerolinecolor: plotColors.grid,
        },
        yaxis: {
          title: { text: 'χ²', font: { color: plotColors.text } },
          tickfont: { color: plotColors.textSecondary },
          type: 'log' as const,
          gridcolor: plotColors.grid,
          zerolinecolor: plotColors.grid,
          // Explicit log range only when it's well-defined (χ² should always
          // be positive, but a degenerate grid must not produce -Infinity).
          ...(chi2Min > 0 && chi2Max > 0
            ? { range: [Math.log10(chi2Min * 0.9), Math.log10(chi2Max * 1.1)], autorange: false }
            : { autorange: true as const }),
        },
        margin: { l: 80, r: 20, t: 40, b: 40 },
        paper_bgcolor: plotColors.paper,
        plot_bgcolor: plotColors.bg,
        hovermode: 'closest' as const,
        showlegend: false,
      },
    };
  }, [showModel, fitData, plotColors]);

  // Capture observed wavelength range from user zoom/pan/reset events.
  // Purely updates React state — no imperative Plotly calls. The next render
  // cycle recomputes xaxis3 ticks and range declaratively via the layout prop.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleRelayout = useCallback((event: any) => {
    const parsed = parseXRangeFromRelayout(event);
    if (parsed === 'reset') setObsRange(null);
    else if (parsed) setObsRange(parsed);
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[700px] bg-card border border-border rounded-lg">
        <Loader2 className="w-6 h-6 animate-spin text-primary mr-3" />
        <span className="text-text-secondary">Loading spectrum...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-[700px] bg-card border border-border rounded-lg">
        <AlertCircle className="w-8 h-8 text-red-500 mb-3" />
        <p className="text-text-secondary">{error}</p>
        <button
          onClick={() => window.location.reload()}
          className="mt-4 px-4 py-2 text-sm text-primary hover:underline"
        >
          Try again
        </button>
      </div>
    );
  }

  if (!data || !processedData || !plotData) {
    return null;
  }

  return (
    <div className={bare ? '' : 'bg-card border border-border rounded-lg overflow-hidden'}>
      {heatError && (
        <div
          role="alert"
          className="flex items-center gap-2 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200"
        >
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>2-D spectrum unavailable: {heatError}</span>
          <button
            type="button"
            onClick={() => { void fullQuery.refetch(); }}
            className="ml-auto font-medium underline underline-offset-2"
          >
            Retry
          </button>
        </div>
      )}
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-4 px-4 py-2 border-b border-border bg-surface-2">
        <FluxUnitToggle fluxUnit={fluxUnit} onChange={setFluxUnit} />

        <ControlDivider />

        {/* 2D color scale controls */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-secondary">2D scale:</span>
          <input
            type="number"
            value={colorMin}
            onChange={(e) => setColorMin(parseFloat(e.target.value) || 0)}
            step={1}
            className="w-16 px-2 py-1 text-sm border border-border-strong rounded bg-card text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
            title="Color minimum (S/N)"
          />
          <span className="text-sm text-text-secondary">to</span>
          <input
            type="number"
            value={colorMax}
            onChange={(e) => setColorMax(parseFloat(e.target.value) || 0)}
            step={1}
            className="w-16 px-2 py-1 text-sm border border-border-strong rounded bg-card text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
            title="Color maximum (S/N)"
          />
          <button
            onClick={() => { setColorMin(spectrumPreferences.snrMin); setColorMax(spectrumPreferences.snrMax); }}
            className="px-2 py-1 text-xs text-text-secondary hover:text-text-primary border border-border dark:border-border-strong rounded hover:bg-card-hover"
            title="Reset to default"
          >
            Reset
          </button>
          <select
            value={colorscale}
            onChange={(e) => setColorscale(e.target.value as Colorscale2D)}
            className="px-2 py-1 text-sm border border-border-strong rounded bg-card text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
            title="Colormap"
          >
            {COLORSCALE_2D_OPTIONS.map((scale) => (
              <option key={scale} value={scale}>
                {scale}
              </option>
            ))}
          </select>
        </div>

        <ControlDivider />

        <EmissionLinesControl showEmissionLines={showEmissionLines} onChange={setShowEmissionLines} />

        {/* Model + chi²(z) toggle — disabled if no zfit data is available. */}
        <PlotCheckbox
          label="Model"
          checked={showModel && !!fitData}
          disabled={!fitData}
          onChange={setShowModel}
          title={fitData ? 'Show best-fit model + χ²(z)' : 'No redshift fit available for this spectrum'}
        />

        {/* y-axis auto-stretch toggle (inspection shortcut: y) */}
        <PlotCheckbox
          label="Auto-y"
          checked={autoStretch}
          onChange={setAutoStretch}
          title="Auto-scale the y-axis to real spectral features; off shows the full flux range (press y in inspection mode)"
        />

        {/* Redshift slider (only shown when emission lines are enabled) */}
        {showEmissionLines && (
          <RedshiftSliderControl
            redshift={redshift}
            onChange={(z) => {
              setRedshift(z);
              onRedshiftChange?.(z);
            }}
          />
        )}
      </div>

      {/* Plot */}
      <Plot
        data={plotData.traces}
        layout={plotData.layout}
        config={{
          responsive: true,
          displayModeBar: true,
          modeBarButtonsToRemove: ['lasso2d', 'select2d'],
          displaylogo: false,
          toImageButtonOptions: {
            format: 'png',
            width: 1920,
            height: 1080,
            scale: 2,
          },
        }}
        style={{ width: '100%', height: '700px' }}
        onRelayout={handleRelayout}
      />

      {/* χ²(z) panel — appears below the spectrum when Model is on. */}
      {chi2PlotData && (
        <div className="border-t border-border">
          <Plot
            data={chi2PlotData.traces}
            layout={chi2PlotData.layout}
            config={{
              responsive: true,
              displayModeBar: false,
              displaylogo: false,
            }}
            style={{ width: '100%', height: '220px' }}
          />
        </div>
      )}
    </div>
  );
};
