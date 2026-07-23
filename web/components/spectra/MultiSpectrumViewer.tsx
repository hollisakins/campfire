'use client';

import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { Loader2 } from 'lucide-react';
import type { SpectrumData } from '@/app/api/spectrum/route';
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
  visible: boolean;
}

interface MultiSpectrumViewerProps {
  sources: SpectrumSource[];
  grating: string | null;
  redshift: number | null;
}

const FETCH_BATCH_SIZE = 4;

export const MultiSpectrumViewer: React.FC<MultiSpectrumViewerProps> = ({
  sources,
  grating,
  redshift: initialRedshift,
}) => {
  const { spectrumPreferences } = usePreferences();
  const { resolvedTheme } = useTheme();

  // Internal cache of fetched spectrum data
  const dataCache = useRef<Map<string, SpectrumData>>(new Map());
  const [loadedData, setLoadedData] = useState<Map<string, SpectrumData>>(new Map());
  const [loading, setLoading] = useState(false);
  const [loadingProgress, setLoadingProgress] = useState<{ loaded: number; total: number } | null>(null);

  const [fluxUnit, setFluxUnit] = useState<FluxUnit>(spectrumPreferences.fluxUnit);
  const [showEmissionLines, setShowEmissionLines] = useState(true);
  const [redshift, setRedshift] = useState(initialRedshift ?? 0);
  const [observedRange, setObservedRange] = useState<[number, number] | null>(null);

  // Sync redshift when initialRedshift changes
  useEffect(() => {
    if (initialRedshift != null) setRedshift(initialRedshift);
  }, [initialRedshift]);

  // Fetch spectrum data for visible sources (batched, progressive)
  useEffect(() => {
    const visibleSources = sources.filter(s => s.visible);
    const toFetch = visibleSources.filter(s => !dataCache.current.has(s.fitsPath));

    if (toFetch.length === 0) {
      // All visible data is cached
      const map = new Map<string, SpectrumData>();
      for (const s of visibleSources) {
        const d = dataCache.current.get(s.fitsPath);
        if (d) map.set(s.fitsPath, d);
      }
      setLoadedData(map);
      setLoadingProgress(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setLoadingProgress({ loaded: 0, total: toFetch.length });

    const fetchOne = async (s: SpectrumSource) => {
      try {
        const res = await fetch(`/api/spectrum?path=${encodeURIComponent(s.fitsPath)}`);
        if (!res.ok) return null;
        const data: SpectrumData = await res.json();
        return { path: s.fitsPath, data };
      } catch {
        return null;
      }
    };

    (async () => {
      let loaded = 0;
      for (let i = 0; i < toFetch.length; i += FETCH_BATCH_SIZE) {
        if (cancelled) return;
        const batch = toFetch.slice(i, i + FETCH_BATCH_SIZE);
        const results = await Promise.all(batch.map(fetchOne));

        for (const r of results) {
          if (r) {
            dataCache.current.set(r.path, r.data);
            loaded++;
          }
        }

        if (cancelled) return;

        // Update state after each batch → traces appear progressively
        const map = new Map<string, SpectrumData>();
        for (const s of visibleSources) {
          const d = dataCache.current.get(s.fitsPath);
          if (d) map.set(s.fitsPath, d);
        }
        setLoadedData(new Map(map));
        setLoadingProgress({ loaded, total: toFetch.length });
      }
      if (!cancelled) {
        setLoading(false);
        setLoadingProgress(null);
      }
    })();

    return () => { cancelled = true; };
  }, [sources]);

  // Build Plotly traces
  const { traces, layout } = useMemo(() => {
    const plotColors = getPlotColors();
    const visibleSources = sources.filter(s => s.visible && loadedData.has(s.fitsPath));

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const allTraces: any[] = [];

    // Collect flux values from ALL loaded sources (not just visible) for stable y-range
    const allFlux: number[] = [];
    const allFluxErr: (number | null)[] = [];
    for (const source of sources) {
      const data = loadedData.get(source.fitsPath);
      if (!data) continue;
      for (let i = 0; i < data.wave.length; i++) {
        const v = data.fnu[i];
        if (v == null || !isFinite(v)) continue;
        allFlux.push(fluxUnit === 'flambda' ? convertToFlambda(v, data.wave[i]) : v);
        const e = data.fnu_err[i];
        allFluxErr.push(e == null ? null : (fluxUnit === 'flambda' ? convertToFlambda(e, data.wave[i]) : e));
      }
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

    // Emission lines — z = 0 is a valid rest frame; no redshift gate, or the
    // toggle silently does nothing for objects without a catalog redshift.
    // Drawn on the hidden overlay yaxis2 so they never affect autoscaling.
    if (showEmissionLines && xRange) {
      allTraces.push(...buildEmissionLineTraces(redshift, xRange[0], xRange[1], {
        yaxis: 'y2',
        grating: grating ?? undefined,
      }));
    }

    // Y-range
    const yRange = allFlux.length > 0
      ? computeYRange(allFlux, allFluxErr, { edgeTrim: Math.min(20, Math.floor(allFlux.length * 0.02)) })
      : undefined;

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
      allTraces.push(buildRestFrameAxisActivationTrace(effRange[0], 'x2', 'y2'));
    }

    return { traces: allTraces, layout: plotLayout };
    // resolvedTheme is a real dependency: getPlotColors() reads CSS variables
    // that change with the theme class on <html>.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources, loadedData, fluxUnit, showEmissionLines, redshift, grating, observedRange, resolvedTheme]);

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
