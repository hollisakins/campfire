'use client';

import React, { useState, useEffect, useMemo } from 'react';
import dynamic from 'next/dynamic';
import { Loader2, AlertCircle } from 'lucide-react';
import type { SpectrumData } from '@/app/api/spectrum/route';
import type { RedshiftFitData } from '@/app/api/redshift-fit/route';
import {
  FluxUnitToggle,
  EmissionLinesControl,
  RedshiftSliderControl,
  ControlDivider,
} from './PlottingControls';
import {
  type FluxUnit,
  convertToFlambda,
  getFluxLabel,
  getHoverLabel,
  createEmissionLineTraces,
} from './plotting-utils';

// Dynamic import of Plotly to avoid SSR issues
const Plot = dynamic(() => import('react-plotly.js'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-[700px] bg-card border border-border rounded-lg">
      <Loader2 className="w-6 h-6 animate-spin text-primary" />
    </div>
  ),
});

interface RedshiftFitPlotProps {
  fitsPath: string;
  grating: string;
  initialRedshift?: number | null;
}

export const RedshiftFitPlot: React.FC<RedshiftFitPlotProps> = ({
  fitsPath,
  grating,
  initialRedshift,
}) => {
  const [spectrumData, setSpectrumData] = useState<SpectrumData | null>(null);
  const [fitData, setFitData] = useState<RedshiftFitData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fluxUnit, setFluxUnit] = useState<FluxUnit>('flambda');
  const [showEmissionLines, setShowEmissionLines] = useState(false);
  const [redshift, setRedshift] = useState(initialRedshift ?? 0);

  useEffect(() => {
    async function fetchData() {
      setLoading(true);
      setError(null);

      try {
        // Fetch spectrum and fit data in parallel
        const [spectrumResponse, fitResponse] = await Promise.all([
          fetch(`/api/spectrum?path=${encodeURIComponent(fitsPath)}`),
          fetch(`/api/redshift-fit?path=${encodeURIComponent(fitsPath)}`),
        ]);

        if (!spectrumResponse.ok) {
          const errorData = await spectrumResponse.json();
          throw new Error(errorData.error || 'Failed to load spectrum');
        }

        if (!fitResponse.ok) {
          if (fitResponse.status === 404) {
            throw new Error('Redshift fit data not available for this spectrum');
          }
          const errorData = await fitResponse.json();
          throw new Error(errorData.error || 'Failed to load redshift fit');
        }

        const spectrum: SpectrumData = await spectrumResponse.json();
        const fit: RedshiftFitData = await fitResponse.json();

        setSpectrumData(spectrum);
        setFitData(fit);

        // Set initial redshift to best-fit value if not provided
        if (initialRedshift === null || initialRedshift === undefined) {
          setRedshift(fit.redshift);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load data');
      } finally {
        setLoading(false);
      }
    }

    fetchData();
  }, [fitsPath, initialRedshift]);

  // Process spectrum and model data
  const processedData = useMemo(() => {
    if (!spectrumData || !fitData) return null;

    // Filter out null values from spectrum
    const validIndices = spectrumData.fnu
      .map((v, i) => (v !== null ? i : -1))
      .filter(i => i !== -1);

    const wave = validIndices.map(i => spectrumData.wave[i]);
    const fnuValues = validIndices.map(i => spectrumData.fnu[i] as number);
    const fnuErr = validIndices.map(i => spectrumData.fnu_err[i]);

    // Convert to f_lambda
    const flambda = fnuValues.map((f, i) => convertToFlambda(f, wave[i]));
    const flambdaErr = fnuErr.map((err, i) =>
      err !== null ? convertToFlambda(err, wave[i]) : null
    );

    // Process model spectrum
    const modelFlambda = fitData.model_fnu.map((f, i) =>
      convertToFlambda(f, fitData.model_wave[i])
    );

    return {
      wave,
      fnu: fnuValues,
      fnuErr,
      flambda,
      flambdaErr,
      modelWave: fitData.model_wave,
      modelFnu: fitData.model_fnu,
      modelFlambda,
    };
  }, [spectrumData, fitData]);

  // Create plot data
  const plotData = useMemo(() => {
    if (!spectrumData || !fitData || !processedData) return null;

    const { wave, fnu, fnuErr, flambda, flambdaErr, modelWave, modelFnu, modelFlambda } =
      processedData;

    // Select flux values based on current unit
    const flux = fluxUnit === 'fnu' ? fnu : flambda;
    const fluxErr = fluxUnit === 'fnu' ? fnuErr : flambdaErr;
    const modelFlux = fluxUnit === 'fnu' ? modelFnu : modelFlambda;
    const fluxLabel = getFluxLabel(fluxUnit);
    const hoverLabel = getHoverLabel(fluxUnit);

    // Calculate error bounds
    const upperBound = flux.map((f, i) => {
      const err = fluxErr[i];
      return err !== null ? f + err : f;
    });
    const lowerBound = flux.map((f, i) => {
      const err = fluxErr[i];
      return err !== null ? f - err : f;
    });

    // Get wavelength and flux ranges
    const waveMin = Math.min(...wave);
    const waveMax = Math.max(...wave);
    const fluxMin = Math.min(...flux);
    const fluxMax = Math.max(...flux);

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const traces: any[] = [
      // ===== Top subplot: Spectrum + Model =====
      // Error band
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
      // Observed spectrum
      {
        x: wave,
        y: flux,
        type: 'scatter' as const,
        mode: 'lines' as const,
        name: 'Observed',
        line: {
          color: '#c026d3',
          width: 1.5,
          shape: 'hvh',
        },
        hovertemplate: `λ: %{x:.3f} μm<br>${hoverLabel}: %{y:.3e}<extra></extra>`,
        xaxis: 'x',
        yaxis: 'y',
      },
      // Best-fit model
      {
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
      },
    ];

    // Add emission lines to top subplot
    if (showEmissionLines) {
      const emissionTraces = createEmissionLineTraces(
        redshift,
        waveMin,
        waveMax,
        fluxMin,
        fluxMax,
        'x',
        'y'
      );
      traces.push(...emissionTraces);
    }

    // ===== Bottom subplot: Chi² vs Redshift =====
    const chi2Min = Math.min(...fitData.chi2_grid);
    const chi2Max = Math.max(...fitData.chi2_grid);

    // Chi² curve
    traces.push({
      x: fitData.z_grid,
      y: fitData.chi2_grid,
      type: 'scatter' as const,
      mode: 'lines' as const,
      name: 'χ²(z)',
      line: {
        color: '#3b82f6',
        width: 2,
      },
      hovertemplate: 'z: %{x:.4f}<br>χ²: %{y:.2f}<extra></extra>',
      showlegend: false,
      xaxis: 'x2',
      yaxis: 'y2',
    });

    // Best-fit vertical line
    traces.push({
      x: [fitData.redshift, fitData.redshift],
      y: [chi2Min * 0.5, chi2Max * 2],
      type: 'scatter' as const,
      mode: 'lines' as const,
      name: 'Best fit',
      line: {
        color: '#ef4444',
        width: 2,
        dash: 'dash',
      },
      hovertemplate: `Best fit<br>z: ${fitData.redshift.toFixed(4)}<br>χ²_min: ${fitData.chi2_min.toFixed(2)}<extra></extra>`,
      showlegend: false,
      xaxis: 'x2',
      yaxis: 'y2',
    });

    // Layout configuration
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const layout: any = {
      uirevision: 'constant',
      title: {
        text: `${grating} Redshift Fit (z = ${fitData.redshift.toFixed(4)}, χ² = ${fitData.chi2_min.toFixed(2)}, conf = ${fitData.confidence.toFixed(1)}%)`,
        font: { size: 16, color: '#0f172a' },
      },
      // Top subplot (spectrum + model)
      xaxis: {
        title: { text: 'Wavelength (μm)' },
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        domain: [0, 1],
        anchor: 'y' as const,
      },
      yaxis: {
        title: { text: fluxLabel },
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        exponentformat: 'e' as const,
        domain: [0.4, 1],
      },
      // Bottom subplot (chi² curve)
      xaxis2: {
        title: { text: 'Redshift' },
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        domain: [0, 1],
        anchor: 'y2' as const,
      },
      yaxis2: {
        title: { text: 'χ²' },
        type: 'log' as const,
        gridcolor: '#e2e8f0',
        zerolinecolor: '#e2e8f0',
        domain: [0, 0.3],
      },
      margin: { l: 80, r: 120, t: 50, b: 50 },
      paper_bgcolor: 'white',
      plot_bgcolor: 'white',
      hovermode: 'closest' as const,
      showlegend: true,
      legend: {
        x: 1.02,
        xanchor: 'left' as const,
        y: 1,
        yanchor: 'top' as const,
        bgcolor: 'rgba(255,255,255,0.9)',
        bordercolor: '#e2e8f0',
        borderwidth: 1,
        font: { size: 10 },
      },
    };

    return { traces, layout };
  }, [spectrumData, fitData, processedData, fluxUnit, showEmissionLines, redshift, grating]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[700px] bg-card border border-border rounded-lg">
        <Loader2 className="w-6 h-6 animate-spin text-primary mr-3" />
        <span className="text-text-secondary">Loading redshift fit...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-[700px] bg-card border border-border rounded-lg">
        <AlertCircle className="w-8 h-8 text-red-500 mb-3" />
        <p className="text-text-secondary">{error}</p>
      </div>
    );
  }

  if (!spectrumData || !fitData || !processedData || !plotData) {
    return null;
  }

  return (
    <div className="bg-white border border-border rounded-lg overflow-hidden">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-4 px-4 py-2 border-b border-border bg-gray-50">
        <FluxUnitToggle fluxUnit={fluxUnit} onChange={setFluxUnit} />

        <ControlDivider />

        <EmissionLinesControl
          showEmissionLines={showEmissionLines}
          onChange={setShowEmissionLines}
        />

        {showEmissionLines && (
          <>
            <ControlDivider />
            <RedshiftSliderControl redshift={redshift} onChange={setRedshift} />
          </>
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
