'use client';

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import dynamic from 'next/dynamic';
import { Loader2, AlertCircle } from 'lucide-react';
import type { SpectrumData } from '@/app/api/spectrum/route';
import type { RedshiftFitData } from '@/app/api/redshift-fit/route';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import { useTheme } from '@/lib/contexts/ThemeContext';
import type { Colorscale2D, FluxUnit } from '@/lib/types';
import { getPlotColors, getVisibleEmissionLines, computeYRange, computeNiceRestTicks } from './plotting-utils';

// Dynamic import of Plotly to avoid SSR issues
const Plot = dynamic(() => import('react-plotly.js'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-[700px] bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
      <Loader2 className="w-6 h-6 animate-spin text-primary" />
    </div>
  ),
});

// Available colorscale options (display names)
const COLORSCALE_OPTIONS: Colorscale2D[] = ['Viridis', 'Plasma', 'Inferno', 'Magma', 'Cividis', 'Greys'];

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

interface CachedSpectrumData {
  spectrum: SpectrumData;
  fitData: RedshiftFitData | null;
}

interface SpectrumPlotProps {
  fitsPath: string;
  grating: string;
  initialRedshift?: number | null;
  inspectionMode?: boolean;
  getCachedData?: (fitsPath: string) => CachedSpectrumData | undefined;
}

export const SpectrumPlot: React.FC<SpectrumPlotProps> = ({
  fitsPath,
  grating,
  initialRedshift,
  inspectionMode = false,
  getCachedData
}) => {
  const { spectrumPreferences, accentColorHex } = usePreferences();
  const { resolvedTheme } = useTheme();

  const [data, setData] = useState<SpectrumData | null>(null);
  const [fitData, setFitData] = useState<RedshiftFitData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fluxUnit, setFluxUnit] = useState<FluxUnit>(spectrumPreferences.fluxUnit);
  const [colorscale, setColorscale] = useState<Colorscale2D>(spectrumPreferences.colorscale2D);
  const [showEmissionLines, setShowEmissionLines] = useState(inspectionMode);
  const [redshift, setRedshift] = useState(initialRedshift ?? 0);
  const [redshiftInput, setRedshiftInput] = useState((initialRedshift ?? 0).toFixed(4));
  const [colorMin, setColorMin] = useState(spectrumPreferences.snrMin);
  const [colorMax, setColorMax] = useState(spectrumPreferences.snrMax);

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
      setRedshiftInput(initialRedshift.toFixed(4));
    }
  }, [initialRedshift]);

  // Get current plot colors based on theme
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const plotColors = useMemo(() => getPlotColors(), [resolvedTheme]);

  // Convert f_nu to f_lambda: f_λ = f_ν * c / λ²
  // f_nu is in μJy (1 μJy = 10^-29 erg/s/cm²/Hz), wavelength in μm
  // f_λ (erg/s/cm²/Å) = f_ν (μJy) * 10^-29 * c / λ²
  // With c = 2.998e10 cm/s and λ in μm (1 μm = 10^-4 cm):
  // f_λ = f_ν * 10^-29 * 2.998e10 / (λ_μm * 10^-4)² / 10^8 (to convert /cm to /Å)
  // f_λ = f_ν * 2.998e-19 / λ_μm²
  const convertToFlambda = (fnuVal: number, wavelength: number): number => {
    return fnuVal * 2.998e-19 / (wavelength * wavelength);
  };

  useEffect(() => {
    async function fetchData() {
      console.log(`[SpectrumPlot] Loading ${grating}, inspection=${inspectionMode}, hasCache=${!!getCachedData}`);
      setLoading(true);
      setError(null);

      try {
        // In inspection mode, check cache first
        if (inspectionMode && getCachedData) {
          const cached = getCachedData(fitsPath);
          if (cached) {
            console.log(`[SpectrumPlot] ✓ Using cached data for ${grating}`);
            setData(cached.spectrum);
            setFitData(cached.fitData);
            setLoading(false);
            return;
          }
          console.log(`[SpectrumPlot] Cache miss for ${grating}, fetching...`);
        }

        // Fallback to normal fetch (existing code)
        if (inspectionMode) {
          const [spectrumResponse, fitResponse] = await Promise.all([
            fetch(`/api/spectrum?path=${encodeURIComponent(fitsPath)}`),
            fetch(`/api/redshift-fit?path=${encodeURIComponent(fitsPath)}`),
          ]);

          if (!spectrumResponse.ok) {
            const errorData = await spectrumResponse.json();
            throw new Error(errorData.error || 'Failed to load spectrum');
          }

          const spectrumData: SpectrumData = await spectrumResponse.json();
          setData(spectrumData);

          if (fitResponse.ok) {
            const fit: RedshiftFitData = await fitResponse.json();
            setFitData(fit);
          } else if (fitResponse.status !== 404) {
            console.warn('Failed to fetch redshift fit data:', fitResponse.status);
          }
        } else {
          // Normal mode: just fetch spectrum
          const response = await fetch(`/api/spectrum?path=${encodeURIComponent(fitsPath)}`);

          if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'Failed to load spectrum');
          }

          const spectrumData: SpectrumData = await response.json();
          setData(spectrumData);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load spectrum');
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, [fitsPath, inspectionMode, getCachedData, grating]);

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
    let modelWave: number[] | null = null;
    let modelFnu: number[] | null = null;
    let modelFlambda: number[] | null = null;

    if (fitData) {
      modelWave = fitData.model_wave;
      modelFnu = fitData.model_fnu;
      modelFlambda = fitData.model_fnu.map((f, i) =>
        convertToFlambda(f, fitData.model_wave[i])
      );
    }

    return { wave, fnu: fnuValues, fnuErr, flambda, flambdaErr, modelWave, modelFnu, modelFlambda };
  }, [data, fitData]);

  // Memoize all plot data - must be before early returns
  const plotData = useMemo(() => {
    if (!data || !processedData) return null;

    const { wave, fnu, fnuErr, flambda, flambdaErr, modelWave, modelFnu, modelFlambda } = processedData;

    // Select flux values based on current unit
    const flux = fluxUnit === 'fnu' ? fnu : flambda;
    const fluxErr = fluxUnit === 'fnu' ? fnuErr : flambdaErr;
    const modelFlux = fluxUnit === 'fnu' ? modelFnu : modelFlambda;
    const fluxLabel = fluxUnit === 'fnu' ? 'fν (μJy)' : 'fλ (erg/s/cm²/Å)';
    const hoverLabel = fluxUnit === 'fnu' ? 'fν' : 'fλ';

    // Calculate upper and lower bounds for error band
    const upperBound = flux.map((f, i) => {
      const err = fluxErr[i];
      return err !== null ? f + err : f;
    });
    const lowerBound = flux.map((f, i) => {
      const err = fluxErr[i];
      return err !== null ? f - err : f;
    });

    // Get wavelength range for filtering emission lines
    const waveMin = Math.min(...wave);
    const waveMax = Math.max(...wave);

    // Rest-frame wavelength conversion factor: μm → Å in rest frame
    const restFrameFactor = 10000 / (1 + redshift);

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
      // 2D S/N heatmap (top-left subplot, shares xaxis with 1D spectrum)
      {
        z: data.snr_2d,
        x: data.wave,
        y: hasProfile ? data.profile_pix : undefined,
        type: 'heatmap' as const,
        colorscale: getPlotlyColorscale(colorscale),
        zmin: colorMin,
        zmax: colorMax,
        showscale: false, // Remove colorbar - using profile panel instead
        hovertemplate: 'λ: %{x:.3f} μm<br>y: %{y:.1f} pix<br>S/N: %{z:.1f}<extra></extra>',
        xaxis: 'x',
        yaxis: 'y2',
      },
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
      // Invisible trace on xaxis3 (Plotly requires a trace to render the axis)
      // Uses same μm wavelengths as primary axis — xaxis3 is just a relabeled overlay
      {
        x: [data.wave[0], data.wave[data.wave.length - 1]],
        y: [0, 0],
        type: 'scatter' as const,
        mode: 'markers' as const,
        marker: { size: 0.1, opacity: 0 },
        hoverinfo: 'skip' as const,
        showlegend: false,
        xaxis: 'x3',
        yaxis: 'y',
      },
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

    // Add best-fit model trace if available (inspection mode)
    if (inspectionMode && modelWave && modelFlux) {
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

    // Smart y-axis auto-scaling (works in both normal and inspection mode)
    const yAxisRange = computeYRange(flux, fluxErr, {
      modelFlux,
      modelWave: processedData.modelWave,
      dataWave: wave,
    });

    // Add emission line markers if enabled
    if (showEmissionLines) {
      const visibleLines = getVisibleEmissionLines(redshift, waveMin, waveMax, grating);

      visibleLines.forEach((line) => {
        traces.push({
          x: [line.observedWave, line.observedWave],
          y: yAxisRange ?? [Math.min(...flux), Math.max(...flux)],
          type: 'scatter' as const,
          mode: 'lines' as const,
          name: line.name,
          line: {
            color: line.color,
            width: 1.5,
            dash: 'dash',
          },
          hovertemplate: `${line.name}<br>λ_rest: ${line.wave.toFixed(4)} μm<br>λ_obs: ${line.observedWave.toFixed(4)} μm<extra></extra>`,
          showlegend: true,
          legendgroup: 'emission_lines',
          xaxis: 'x',
          yaxis: 'y',
        });
      });
    }

    // Compute rest-frame ticks for the current view (zoomed or full range)
    const effectiveMin = obsRange ? obsRange[0] : waveMin;
    const effectiveMax = obsRange ? obsRange[1] : waveMax;
    const restTicks = computeNiceRestTicks(effectiveMin, effectiveMax, restFrameFactor);

    // Layout configuration with profile panel
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const layout: any = {
      uirevision: 'constant', // Preserve zoom/pan state across updates
      font: { family: 'Roboto, sans-serif', color: plotColors.text },
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
        uirevision: 'constant', // Preserve user zoom across re-renders
      },
      // X-axis: Rest-frame wavelength (Å), overlays primary axis
      // Shares μm coordinate system with xaxis; tickvals/ticktext relabel to Å
      xaxis3: {
        overlaying: 'x' as const,
        side: 'top' as const,
        tickmode: 'array' as const,
        tickvals: restTicks.map(å => å / restFrameFactor),
        ticktext: restTicks.map(å => `${parseFloat(å.toFixed(1))} Å`),
        ticks: 'outside' as const,
        tickcolor: plotColors.textSecondary,
        tickfont: { size: 11, color: plotColors.textSecondary },
        showgrid: false,
        gridcolor: 'transparent',
        zerolinecolor: 'transparent',
        domain: [0, 0.90],
        anchor: 'y' as const,
        // Range must match primary axis exactly for tick alignment
        // When zoomed: explicit range from state; when full: autorange from invisible trace
        ...(obsRange
          ? { range: obsRange, autorange: false }
          : { autorange: true }
        ),
        // Change uirevision on each zoom so Plotly applies new ticks/range
        // (xaxis keeps 'constant' to preserve user zoom; xaxis3 resets to accept layout updates)
        uirevision: obsRange ? `${obsRange[0]}-${obsRange[1]}` : 'default',
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
      },
      // Y-axis for 1D spectrum (bottom)
      yaxis: {
        title: { text: fluxLabel },
        gridcolor: plotColors.grid,
        zerolinecolor: plotColors.grid,
        exponentformat: 'e' as const,
        domain: [0, 0.7],
        anchor: 'x' as const,
        ...(yAxisRange && { range: yAxisRange }), // Apply model-based range in inspection mode
      },
      // Y-axis for 2D heatmap (top-left)
      yaxis2: {
        title: { text: 'y [pix]' },
        gridcolor: plotColors.grid,
        domain: [0.78, 1],
        anchor: 'x' as const,
        range: [-10, 10],
      },
      // Y-axis for profile panel (top-right, matches yaxis2)
      yaxis3: {
        gridcolor: plotColors.grid,
        domain: [0.78, 1],
        anchor: 'x2' as const,
        matches: 'y2' as const, // Link range to yaxis2
        showticklabels: false,
      },
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
      },
    };

    return { traces, layout };
  }, [data, processedData, fluxUnit, colorscale, colorMin, colorMax, accentColorHex, plotColors, showEmissionLines, redshift, grating, inspectionMode, obsRange]);

  // Capture observed wavelength range from user zoom/pan/reset events.
  // Purely updates React state — no imperative Plotly calls. The next render
  // cycle recomputes xaxis3 ticks and range declaratively via the layout prop.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleRelayout = useCallback((event: any) => {
    // Extract observed range — Plotly uses different key formats
    let obsMin: number | undefined;
    let obsMax: number | undefined;

    if (event['xaxis.range[0]'] !== undefined && event['xaxis.range[1]'] !== undefined) {
      // Box zoom: separate keys
      obsMin = event['xaxis.range[0]'];
      obsMax = event['xaxis.range[1]'];
    } else if (Array.isArray(event['xaxis.range'])) {
      // Pan/drag: array
      obsMin = event['xaxis.range'][0];
      obsMax = event['xaxis.range'][1];
    }

    if (obsMin !== undefined && obsMax !== undefined) {
      setObsRange([obsMin, obsMax]);
    } else if (event['xaxis.autorange'] === true) {
      // Double-click reset
      setObsRange(null);
    }
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[700px] bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
        <Loader2 className="w-6 h-6 animate-spin text-primary mr-3" />
        <span className="text-text-secondary dark:text-slate-400">Loading spectrum...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-[700px] bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
        <AlertCircle className="w-8 h-8 text-red-500 mb-3" />
        <p className="text-text-secondary dark:text-slate-400">{error}</p>
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
    <div className="bg-white dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg overflow-hidden">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-4 px-4 py-2 border-b border-border dark:border-slate-700 bg-gray-50 dark:bg-slate-900">
        {/* Flux unit toggle */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-secondary dark:text-slate-400">Units:</span>
          <div className="flex rounded-md overflow-hidden border border-border dark:border-slate-600">
            <button
              onClick={() => setFluxUnit('fnu')}
              className={`px-3 py-1 text-sm transition-colors ${
                fluxUnit === 'fnu'
                  ? 'bg-primary text-white'
                  : 'bg-white dark:bg-slate-800 text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-700'
              }`}
            >
              fν
            </button>
            <button
              onClick={() => setFluxUnit('flambda')}
              className={`px-3 py-1 text-sm transition-colors ${
                fluxUnit === 'flambda'
                  ? 'bg-primary text-white'
                  : 'bg-white dark:bg-slate-800 text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-700'
              }`}
            >
              fλ
            </button>
          </div>
        </div>

        {/* Divider */}
        <div className="h-6 w-px bg-border dark:bg-slate-600" />

        {/* 2D color scale controls */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-secondary dark:text-slate-400">2D scale:</span>
          <input
            type="number"
            value={colorMin}
            onChange={(e) => setColorMin(parseFloat(e.target.value) || 0)}
            step={1}
            className="w-16 px-2 py-1 text-sm border border-border dark:border-slate-600 rounded bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
            title="Color minimum (S/N)"
          />
          <span className="text-sm text-text-secondary dark:text-slate-400">to</span>
          <input
            type="number"
            value={colorMax}
            onChange={(e) => setColorMax(parseFloat(e.target.value) || 0)}
            step={1}
            className="w-16 px-2 py-1 text-sm border border-border dark:border-slate-600 rounded bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
            title="Color maximum (S/N)"
          />
          <button
            onClick={() => { setColorMin(spectrumPreferences.snrMin); setColorMax(spectrumPreferences.snrMax); }}
            className="px-2 py-1 text-xs text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100 border border-border dark:border-slate-600 rounded hover:bg-gray-100 dark:hover:bg-slate-700"
            title="Reset to default"
          >
            Reset
          </button>
          <select
            value={colorscale}
            onChange={(e) => setColorscale(e.target.value as Colorscale2D)}
            className="px-2 py-1 text-sm border border-border dark:border-slate-600 rounded bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
            title="Colormap"
          >
            {COLORSCALE_OPTIONS.map((scale) => (
              <option key={scale} value={scale}>
                {scale}
              </option>
            ))}
          </select>
        </div>

        {/* Divider */}
        <div className="h-6 w-px bg-border dark:bg-slate-600" />

        {/* Emission lines toggle */}
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={showEmissionLines}
              onChange={(e) => setShowEmissionLines(e.target.checked)}
              className="w-4 h-4 rounded border-border dark:border-slate-600 text-primary focus:ring-primary"
            />
            <span className="text-sm text-text-secondary dark:text-slate-400">Emission lines</span>
          </label>
        </div>

        {/* Redshift slider (only shown when emission lines are enabled) */}
        {showEmissionLines && (
          <div className="flex items-center gap-2 flex-1 max-w-md">
            <span className="text-sm text-text-secondary dark:text-slate-400">z =</span>
            <input
              type="text"
              value={redshiftInput}
              onChange={(e) => setRedshiftInput(e.target.value)}
              onBlur={() => {
                const parsed = parseFloat(redshiftInput);
                if (!isNaN(parsed) && parsed >= 0 && parsed <= 15) {
                  setRedshift(parsed);
                  setRedshiftInput(parsed.toFixed(4));
                } else {
                  setRedshiftInput(redshift.toFixed(4));
                }
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.currentTarget.blur();
                }
              }}
              className="w-20 px-2 py-1 text-sm border border-border dark:border-slate-600 rounded bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <input
              type="range"
              value={redshift}
              onChange={(e) => {
                const newValue = parseFloat(e.target.value);
                setRedshift(newValue);
                setRedshiftInput(newValue.toFixed(4));
              }}
              min={0}
              max={15}
              step={0.01}
              className="flex-1 h-2 bg-gray-200 dark:bg-slate-600 rounded-lg appearance-none cursor-pointer accent-primary"
            />
          </div>
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
    </div>
  );
};
