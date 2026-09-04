'use client';

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useQueries, type UseQueryResult } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import type { SpectrumData } from '@/app/api/spectrum/route';
import { spectrumJsonQueryOptions, useSpectrumCacheTrim } from '@/lib/hooks/useSpectrumJson';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import { useTheme } from '@/lib/contexts/ThemeContext';
import {
  getPlotColors,
  convertToFlambda,
  computeYRange,
  getFluxLabel,
  parseXRangeFromRelayout,
  buildRestFrameAxis,
  buildRestFrameAxisActivationTrace,
  buildEmissionLineTraces,
  buildEmissionLineOverlayAxis,
} from './plotting-utils';
import type { FluxUnit } from './plotting-utils';
import { FluxUnitToggle, EmissionLinesControl, RedshiftSliderControl, ControlDivider } from './PlottingControls';
import { LazyPlot as Plot } from '@/components/plot/LazyPlot';

export interface SpectrumSource {
  fitsPath: string;
  label: string;
  color: string;
  /** Hidden sources stay loaded and keep contributing to the y-range, so
   *  toggling visibility doesn't rescale the plot. */
  visible: boolean;
}

interface MultiSpectrumViewerProps {
  sources: SpectrumSource[];
  grating: string | null;
  redshift: number | null;
}

export const MultiSpectrumViewer: React.FC<MultiSpectrumViewerProps> = ({
  sources,
  grating,
  redshift: initialRedshift,
}) => {
  const { spectrumPreferences } = usePreferences();
  const { resolvedTheme } = useTheme();

  // Spectrum JSON via the shared TanStack cache (lib/hooks/useSpectrumJson,
  // #500): one query per source, enabled while the source is visible. Data
  // for a hidden source stays in the cache and keeps contributing to the
  // y-range, so toggling visibility never rescales the plot. Traces appear
  // progressively as each query resolves.
  const combineSpectrumQueries = useCallback(
    (results: UseQueryResult<SpectrumData>[]) => {
      const map = new Map<string, SpectrumData>();
      let total = 0;
      let pending = 0;
      sources.forEach((s, i) => {
        const r = results[i];
        if (r?.data) map.set(s.fitsPath, r.data);
        if (s.visible) {
          total++;
          // A failed fetch is dropped from the plot, not waited on (it used
          // to be skipped silently; TanStack retries once first).
          if (!r?.data && r?.status === 'pending') pending++;
        }
      });
      return {
        loadedData: map,
        loading: pending > 0,
        loadingProgress: pending > 0 ? { loaded: total - pending, total } : null,
      };
    },
    [sources],
  );
  const { loadedData, loading, loadingProgress } = useQueries({
    queries: sources.map(s => ({ ...spectrumJsonQueryOptions(s.fitsPath), enabled: s.visible })),
    combine: combineSpectrumQueries,
  });
  // Bound the shared cache as spectra land (idle entries beyond the cap go).
  useSpectrumCacheTrim(loadedData);

  const [fluxUnit, setFluxUnit] = useState<FluxUnit>(spectrumPreferences.fluxUnit);
  const [showEmissionLines, setShowEmissionLines] = useState(true);
  const [redshift, setRedshift] = useState(initialRedshift ?? 0);
  const [observedRange, setObservedRange] = useState<[number, number] | null>(null);

  // Sync redshift when initialRedshift changes
  useEffect(() => {
    if (initialRedshift != null) setRedshift(initialRedshift);
  }, [initialRedshift]);


  // Redshift-INDEPENDENT traces — the error bands and flux lines of every
  // visible source, plus the merged x/y ranges. Dragging the redshift slider
  // (or zooming, or switching theme) must not rebuild these: they keep their
  // identity so Plotly.react can skip them and only the emission-line traces
  // and the rest-frame axis change (#500).
  const base = useMemo(() => {
    const visibleSources = sources.filter(s => s.visible && loadedData.has(s.fitsPath));

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const allTraces: any[] = [];

    // Y-range: per-source smart ranges, merged. Computed over ALL loaded
    // sources (hidden included) so toggling a spectrum doesn't rescale the
    // plot. Per-source rather than on the concatenation: edge-trimming and
    // the MAD statistics inside computeYRange are only meaningful within a
    // single spectrum.
    let yRange: [number, number] | undefined;
    for (const source of sources) {
      const data = loadedData.get(source.fitsPath);
      if (!data) continue;
      const srcFlux: number[] = [];
      const srcFluxErr: (number | null)[] = [];
      for (let i = 0; i < data.wave.length; i++) {
        const v = data.fnu[i];
        if (v == null || !isFinite(v)) continue;
        srcFlux.push(fluxUnit === 'flambda' ? convertToFlambda(v, data.wave[i]) : v);
        const e = data.fnu_err[i];
        srcFluxErr.push(e == null ? null : (fluxUnit === 'flambda' ? convertToFlambda(e, data.wave[i]) : e));
      }
      const r = computeYRange(srcFlux, srcFluxErr);
      if (!r) continue;
      yRange = yRange ? [Math.min(yRange[0], r[0]), Math.max(yRange[1], r[1])] : r;
    }

    for (const source of visibleSources) {
      const data = loadedData.get(source.fitsPath);
      if (!data) continue;

      const wave = data.wave;
      const flux = wave.map((w, i) => {
        const v = data.fnu[i];
        if (v == null) return null;
        return fluxUnit === 'flambda' ? convertToFlambda(v, w) : v;
      });
      const fluxErr = wave.map((w, i) => {
        const e = data.fnu_err[i];
        if (e == null) return null;
        return fluxUnit === 'flambda' ? convertToFlambda(e, w) : e;
      });

      // Error band: split into contiguous non-null segments, each a toself polygon.
      // This avoids cross-grating fill (tonexty) and null-gap artifacts (toself with nulls).
      type Segment = { wave: number[]; upper: number[]; lower: number[] };
      const segments: Segment[] = [];
      let seg: Segment | null = null;
      for (let i = 0; i < wave.length; i++) {
        if (flux[i] != null && fluxErr[i] != null) {
          if (!seg) seg = { wave: [], upper: [], lower: [] };
          seg.wave.push(wave[i]);
          seg.upper.push(flux[i]! + fluxErr[i]!);
          seg.lower.push(flux[i]! - fluxErr[i]!);
        } else if (seg) {
          segments.push(seg);
          seg = null;
        }
      }
      if (seg) segments.push(seg);

      for (const s of segments) {
        allTraces.push({
          x: [...s.wave, ...s.wave.slice().reverse()],
          y: [...s.upper, ...s.lower.slice().reverse()],
          type: 'scatter',
          mode: 'lines',
          line: { color: 'transparent', width: 0, shape: 'hvh' },
          fill: 'toself',
          fillcolor: source.color + '26', // 15% opacity
          showlegend: false,
          hoverinfo: 'skip',
        });
      }

      // Main flux trace
      allTraces.push({
        x: wave,
        y: flux,
        type: 'scatter',
        mode: 'lines',
        line: { color: source.color, width: 1.5, shape: 'hvh' },
        name: source.label,
        showlegend: false,
        hovertemplate: `${source.label}<br>λ: %{x:.4f} μm<br>${fluxUnit === 'fnu' ? 'fν' : 'fλ'}: %{y:.4g}<extra></extra>`,
      });
    }

    // Compute x-axis range from all loaded sources (non-NaN wave values).
    // Loop, not Math.min(...spread): the concatenation of every spectrum can
    // reach argument-count limits and throw.
    let xWaveMin = Infinity;
    let xWaveMax = -Infinity;
    for (const s of sources) {
      const d = loadedData.get(s.fitsPath);
      if (!d) continue;
      for (const w of d.wave) {
        if (!isFinite(w)) continue;
        if (w < xWaveMin) xWaveMin = w;
        if (w > xWaveMax) xWaveMax = w;
      }
    }
    const xRange: [number, number] | undefined =
      xWaveMin < xWaveMax ? [xWaveMin, xWaveMax] : undefined;

    return { traces: allTraces, xRange, yRange };
  }, [sources, loadedData, fluxUnit]);

  // Everything that moves with the slider, the zoom or the theme: emission
  // lines, the rest-frame overlay axis, and the layout.
  const { traces, layout } = useMemo(() => {
    const plotColors = getPlotColors();
    const { xRange, yRange } = base;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const overlayTraces: any[] = [];

    // Emission lines — z = 0 is a valid rest frame; no redshift gate, or the
    // toggle silently does nothing for objects without a catalog redshift.
    // Drawn on the hidden overlay yaxis2 so they never affect autoscaling.
    if (showEmissionLines && xRange) {
      overlayTraces.push(...buildEmissionLineTraces(redshift, xRange[0], xRange[1], {
        yaxis: 'y2',
        grating: grating ?? undefined,
      }));
    }

    // Current observed view for rest-frame axis ticks: user zoom if set,
    // otherwise the full data range (null = full-range convention).
    const effRange = observedRange ?? xRange;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const plotLayout: any = {
      autosize: true,
      height: 500,
      margin: { l: 70, r: 20, t: 40, b: 50 },
      paper_bgcolor: plotColors.paper,
      plot_bgcolor: plotColors.bg,
      font: { color: plotColors.text, size: 12 },
      hovermode: 'x unified',
      legend: { uirevision: 'constant' },
      xaxis: {
        title: { text: 'Observed Wavelength (μm)', standoff: 10 },
        gridcolor: plotColors.grid,
        zerolinecolor: plotColors.grid,
        tickcolor: plotColors.text,
        tickfont: { color: plotColors.text },
        ...(xRange && { range: xRange }),
        uirevision: 'constant',
      },
      yaxis: {
        title: { text: getFluxLabel(fluxUnit), standoff: 5 },
        gridcolor: plotColors.grid,
        zerolinecolor: plotColors.grid,
        tickcolor: plotColors.text,
        tickfont: { color: plotColors.text },
        range: yRange,
        // Keyed on the flux unit: a y-zoom set in fν must not be preserved
        // when switching to fλ (the units differ by ~19 orders of magnitude,
        // so a preserved range renders as a blank plot).
        uirevision: fluxUnit,
      },
      // Emission line overlay axis (hidden, fixed 0-1)
      yaxis2: buildEmissionLineOverlayAxis('y'),
    };

    // Rest-frame axis overlay — always present once data is loaded (labels
    // observed-frame Å at z = 0). Shared builder, see buildRestFrameAxis.
    if (effRange) {
      plotLayout.xaxis2 = buildRestFrameAxis({
        redshift,
        obsMin: effRange[0],
        obsMax: effRange[1],
        colors: plotColors,
      });
      overlayTraces.push(buildRestFrameAxisActivationTrace(effRange[0], 'x2', 'y2'));
    }

    return {
      traces: overlayTraces.length > 0 ? [...base.traces, ...overlayTraces] : base.traces,
      layout: plotLayout,
    };
    // resolvedTheme is a real dependency: getPlotColors() reads CSS variables
    // that change with the theme class on <html>.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, fluxUnit, showEmissionLines, redshift, grating, observedRange, resolvedTheme]);

  // Track zoom range for rest-frame axis (null = full data range)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleRelayout = useCallback((event: any) => {
    const parsed = parseXRangeFromRelayout(event);
    if (parsed === 'reset') setObservedRange(null);
    else if (parsed) setObservedRange(parsed);
  }, []);

  const visibleCount = sources.filter(s => s.visible).length;

  if (visibleCount === 0) {
    return (
      <div className="flex items-center justify-center h-[200px] bg-card border border-border rounded-lg text-text-secondary">
        No spectra selected. Check targets in the table above to compare.
      </div>
    );
  }

  return (
    <div>
      {/* Controls bar */}
      <div className="flex items-center gap-4 flex-wrap px-4 py-2 border-b border-border bg-surface-2">
        <FluxUnitToggle fluxUnit={fluxUnit} onChange={setFluxUnit} />
        <ControlDivider />
        <EmissionLinesControl showEmissionLines={showEmissionLines} onChange={setShowEmissionLines} />
        {showEmissionLines && (
          <>
            <ControlDivider />
            <RedshiftSliderControl redshift={redshift} onChange={setRedshift} />
          </>
        )}
      </div>

      {/* Plot */}
      <div className="relative">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-card/80 z-10 rounded-lg">
            <div className="flex items-center gap-2">
              <Loader2 className="w-5 h-5 animate-spin text-primary" />
              {loadingProgress && loadingProgress.total > 1 && (
                <span className="text-sm text-text-secondary">
                  Loading spectra ({loadingProgress.loaded}/{loadingProgress.total})
                </span>
              )}
            </div>
          </div>
        )}
        <Plot
          data={traces}
          layout={layout}
          config={{
            responsive: true,
            displayModeBar: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
          }}
          onRelayout={handleRelayout}
          style={{ width: '100%' }}
          useResizeHandler
        />
      </div>
    </div>
  );
};
