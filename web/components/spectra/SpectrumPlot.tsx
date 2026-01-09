'use client';

import React, { useState, useEffect, useMemo } from 'react';
import dynamic from 'next/dynamic';
import { Loader2, AlertCircle } from 'lucide-react';
import type { SpectrumData } from '@/app/api/spectrum/route';

// Dynamic import of Plotly to avoid SSR issues
const Plot = dynamic(() => import('react-plotly.js'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-[700px] bg-card border border-border rounded-lg">
      <Loader2 className="w-6 h-6 animate-spin text-primary" />
    </div>
  ),
});

// Common emission lines with rest wavelengths in microns
// Colors assigned as rainbow from blue (short λ) to red (long λ)
const EMISSION_LINES = [
  { name: 'Lyα', wave: 0.12157, color: '#6366f1' },      // indigo (shortest)
  { name: 'CIV', wave: 0.1549, color: '#4f46e5' },       // indigo-600
  { name: 'CIII]', wave: 0.1909, color: '#4338ca' },     // indigo-700
  { name: 'MgII', wave: 0.2798, color: '#2563eb' },      // blue-600
  { name: '[OII]', wave: 0.3727, color: '#0ea5e9' },     // sky-500
  { name: 'Hδ', wave: 0.4102, color: '#06b6d4' },        // cyan-500
  { name: 'Hγ', wave: 0.4341, color: '#14b8a6' },        // teal-500
  { name: 'Hβ', wave: 0.4861, color: '#10b981' },        // emerald-500
  { name: '[OIII]₁', wave: 0.4959, color: '#22c55e' },   // green-500
  { name: '[OIII]₂', wave: 0.5007, color: '#84cc16' },   // lime-500
  { name: 'Hα', wave: 0.6563, color: '#eab308' },        // yellow-500
  { name: '[NII]', wave: 0.6584, color: '#f59e0b' },     // amber-500
  { name: '[SII]₁', wave: 0.6717, color: '#f97316' },    // orange-500
  { name: '[SII]₂', wave: 0.6731, color: '#ef4444' },    // red-500
  { name: 'Paβ', wave: 1.2822, color: '#dc2626' },       // red-600
  { name: 'Paα', wave: 1.8751, color: '#b91c1c' },       // red-700 (longest)
];

type FluxUnit = 'fnu' | 'flambda';

interface SpectrumPlotProps {
  fitsPath: string;
  grating: string;
  initialRedshift?: number | null;
}

export const SpectrumPlot: React.FC<SpectrumPlotProps> = ({ fitsPath, grating, initialRedshift }) => {
  const [data, setData] = useState<SpectrumData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fluxUnit, setFluxUnit] = useState<FluxUnit>('flambda');
  const [showEmissionLines, setShowEmissionLines] = useState(false);
  const [redshift, setRedshift] = useState(initialRedshift ?? 0);
  const [redshiftInput, setRedshiftInput] = useState((initialRedshift ?? 0).toFixed(4));
  const [colorMin, setColorMin] = useState(-5);
  const [colorMax, setColorMax] = useState(10);

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
      setLoading(true);
      setError(null);

      try {
        const response = await fetch(`/api/spectrum?path=${encodeURIComponent(fitsPath)}`);

        if (!response.ok) {
          const errorData = await response.json();
          throw new Error(errorData.error || 'Failed to load spectrum');
        }

        const spectrumData: SpectrumData = await response.json();
        setData(spectrumData);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load spectrum');
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, [fitsPath]);

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

    return { wave, fnu: fnuValues, fnuErr, flambda, flambdaErr };
  }, [data]);

  // Memoize all plot data - must be before early returns
  const plotData = useMemo(() => {
    if (!data || !processedData) return null;

    const { wave, fnu, fnuErr, flambda, flambdaErr } = processedData;

    // Select flux values based on current unit
    const flux = fluxUnit === 'fnu' ? fnu : flambda;
    const fluxErr = fluxUnit === 'fnu' ? fnuErr : flambdaErr;
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
        colorscale: 'Viridis',
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
        fillcolor: 'rgba(192, 38, 211, 0.15)',
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
          color: '#c026d3',
          width: 1.5,
          shape: 'hvh',
        },
        hovertemplate: `λ: %{x:.3f} μm<br>${hoverLabel}: %{y:.3e}<extra></extra>`,
        xaxis: 'x',
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

      // Cross-dispersion profile line (black step function)
      traces.push({
        x: profStepX,
        y: profStepY,
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'Spatial profile',
        line: {
          color: '#000000',
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
        line: { color: '#e2e8f0', width: 1 },
        hoverinfo: 'skip' as const,
        showlegend: false,
        xaxis: 'x2',
        yaxis: 'y3',
      });
    }

    // Add emission line markers if enabled
    if (showEmissionLines) {
      const visibleLines = EMISSION_LINES
        .map(line => ({
          ...line,
          observedWave: line.wave * (1 + redshift),
        }))
        .filter(line => line.observedWave >= waveMin && line.observedWave <= waveMax);

      visibleLines.forEach((line) => {
        traces.push({
          x: [line.observedWave, line.observedWave],
          y: [Math.min(...flux) * 0.9, Math.max(...flux) * 1.1],
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

    // Layout configuration with profile panel
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const layout: any = {
      uirevision: 'constant', // Preserve zoom/pan state across updates
      title: {
        text: `${grating} Spectrum`,
        font: { size: 16, color: '#0f172a' },
      },
      // X-axis: Shared wavelength axis for both 2D and 1D spectra (linked zoom/pan)
      xaxis: {
        title: { text: 'Wavelength (μm)' },
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        domain: [0, 0.88],
      },
      // X-axis for profile panel (top-right, narrow)
      xaxis2: {
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        domain: [0.90, 0.98],
        anchor: 'y3' as const,
        showticklabels: false,
        range: [-0.3, 1.2],
        fixedrange: true,
      },
      // Y-axis for 1D spectrum (bottom)
      yaxis: {
        title: { text: fluxLabel },
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        exponentformat: 'e' as const,
        domain: [0, 0.7],
        anchor: 'x' as const,
      },
      // Y-axis for 2D heatmap (top-left)
      yaxis2: {
        title: { text: 'y [pix]' },
        gridcolor: '#e2e8f0',
        domain: [0.78, 1],
        anchor: 'x' as const,
        range: [-10, 10],
      },
      // Y-axis for profile panel (top-right, matches yaxis2)
      yaxis3: {
        gridcolor: '#e2e8f0',
        domain: [0.78, 1],
        anchor: 'x2' as const,
        matches: 'y2' as const, // Link range to yaxis2
        showticklabels: false,
      },
      margin: { l: 80, r: 80, t: 50, b: 50 },
      paper_bgcolor: 'white',
      plot_bgcolor: 'white',
      hovermode: 'x unified' as const,
      showlegend: true,
      legend: {
        x: 1.02,
        xanchor: 'left' as const,
        y: 0.7,
        yanchor: 'top' as const,
        bgcolor: 'rgba(255,255,255,0.9)',
        bordercolor: '#e2e8f0',
        borderwidth: 1,
        font: { size: 10 },
        tracegroupgap: 2,
      },
    };

    return { traces, layout };
  }, [data, processedData, fluxUnit, colorMin, colorMax, showEmissionLines, redshift, grating]);

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
    <div className="bg-white border border-border rounded-lg overflow-hidden">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-4 px-4 py-2 border-b border-border bg-gray-50">
        {/* Flux unit toggle */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-secondary">Units:</span>
          <div className="flex rounded-md overflow-hidden border border-border">
            <button
              onClick={() => setFluxUnit('fnu')}
              className={`px-3 py-1 text-sm transition-colors ${
                fluxUnit === 'fnu'
                  ? 'bg-primary text-white'
                  : 'bg-white text-text-secondary hover:bg-gray-100'
              }`}
            >
              fν
            </button>
            <button
              onClick={() => setFluxUnit('flambda')}
              className={`px-3 py-1 text-sm transition-colors ${
                fluxUnit === 'flambda'
                  ? 'bg-primary text-white'
                  : 'bg-white text-text-secondary hover:bg-gray-100'
              }`}
            >
              fλ
            </button>
          </div>
        </div>

        {/* Divider */}
        <div className="h-6 w-px bg-border" />

        {/* 2D color scale controls */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-secondary">2D scale:</span>
          <input
            type="number"
            value={colorMin}
            onChange={(e) => setColorMin(parseFloat(e.target.value) || 0)}
            step={1}
            className="w-16 px-2 py-1 text-sm border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
            title="Color minimum (S/N)"
          />
          <span className="text-sm text-text-secondary">to</span>
          <input
            type="number"
            value={colorMax}
            onChange={(e) => setColorMax(parseFloat(e.target.value) || 0)}
            step={1}
            className="w-16 px-2 py-1 text-sm border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
            title="Color maximum (S/N)"
          />
          <button
            onClick={() => { setColorMin(-5); setColorMax(10); }}
            className="px-2 py-1 text-xs text-text-secondary hover:text-text-primary border border-border rounded hover:bg-gray-100"
            title="Reset to default"
          >
            Reset
          </button>
        </div>

        {/* Divider */}
        <div className="h-6 w-px bg-border" />

        {/* Emission lines toggle */}
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={showEmissionLines}
              onChange={(e) => setShowEmissionLines(e.target.checked)}
              className="w-4 h-4 rounded border-border text-primary focus:ring-primary"
            />
            <span className="text-sm text-text-secondary">Emission lines</span>
          </label>
        </div>

        {/* Redshift slider (only shown when emission lines are enabled) */}
        {showEmissionLines && (
          <div className="flex items-center gap-2 flex-1 max-w-md">
            <span className="text-sm text-text-secondary">z =</span>
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
              className="w-20 px-2 py-1 text-sm border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
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
              className="flex-1 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-primary"
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
      />
    </div>
  );
};
