'use client';

import React, { useState, useMemo, useEffect } from 'react';
import dynamic from 'next/dynamic';
import { Loader2, BarChart3 } from 'lucide-react';
import { useTheme } from '@/lib/contexts/ThemeContext';
import { getPlotColors } from './plotting-utils';
import { PhotometryTable } from './PhotometryTable';
import type { ObjectPhotometry } from '@/lib/types';

const Plot = dynamic(() => import('react-plotly.js'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-[400px] bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
      <Loader2 className="w-6 h-6 animate-spin text-primary" />
    </div>
  ),
});

// Classify bands by wavelength for color coding
function getBandColor(wav: number): string {
  if (wav < 0.4) return '#8b5cf6';       // UV — violet
  if (wav < 1.0) return '#3b82f6';       // Optical — blue
  if (wav < 2.5) return '#10b981';       // NIR (short) — green
  if (wav < 5.5) return '#f59e0b';       // NIR (long) — amber
  return '#ef4444';                       // MIR — red
}

function getBandCategory(wav: number): string {
  if (wav < 0.4) return 'UV/u-band';
  if (wav < 1.0) return 'Optical/HST';
  if (wav < 2.5) return 'NIRCam SW';
  if (wav < 5.5) return 'NIRCam LW';
  return 'MIRI';
}

interface PzSidecarData {
  label: string;
  color: string;
  z_best: number;
  chi2: number;
  z_grid?: number[];
  pz?: number[];
  template_wav?: number[];
  template_flux_ujy?: number[];
}

interface PhotometrySEDProps {
  photometry: ObjectPhotometry;
  objectId: string;
  bestRedshift: number | null;
}

export const PhotometrySED: React.FC<PhotometrySEDProps> = ({
  photometry,
  objectId,
  bestRedshift,
}) => {
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === 'dark';
  const [pzData, setPzData] = useState<PzSidecarData | null>(null);
  const [pzLoading, setPzLoading] = useState(false);

  // Load P(z) sidecar if available
  useEffect(() => {
    if (!photometry.has_pz) return;

    const fetchPz = async () => {
      setPzLoading(true);
      try {
        const resp = await fetch(
          `/api/photometry-pz?object_id=${encodeURIComponent(objectId)}`
        );
        if (resp.ok) {
          setPzData(await resp.json());
        }
      } catch {
        // Silently fail — P(z) is optional
      } finally {
        setPzLoading(false);
      }
    };

    fetchPz();
  }, [photometry.has_pz, objectId]);

  const { bands } = photometry.photometry;

  // Prepare band data sorted by wavelength
  const bandData = useMemo(() => {
    const entries = Object.entries(bands)
      .filter(([, b]) => b.wav != null && isFinite(b.flux) && isFinite(b.flux_err))
      .map(([name, b]) => ({
        name,
        wav: b.wav!,
        wav_min: b.wav_min ?? b.wav!,
        wav_max: b.wav_max ?? b.wav!,
        flux: b.flux,
        flux_err: b.flux_err,
        snr: b.flux_err > 0 ? b.flux / b.flux_err : 0,
        color: getBandColor(b.wav!),
        category: getBandCategory(b.wav!),
      }))
      .sort((a, b) => a.wav - b.wav);
    return entries;
  }, [bands]);

  // Always AB magnitudes
  const plotData = useMemo(() => {
    return bandData.map(b => {
      if (b.snr < 2) {
        const upper_limit = -2.5 * Math.log10(2 * b.flux_err) + 23.9;
        return { ...b, y: upper_limit, y_err: 0.3, y_upper_limit: upper_limit };
      }
      const mag = -2.5 * Math.log10(b.flux) + 23.9;
      const mag_err_hi = Math.abs(-2.5 * Math.log10(b.flux - b.flux_err) + 23.9 - mag);
      const mag_err_lo = Math.abs(-2.5 * Math.log10(b.flux + b.flux_err) + 23.9 - mag);
      return { ...b, y: mag, y_err: Math.max(mag_err_lo, mag_err_hi), y_upper_limit: undefined };
    });
  }, [bandData]);

  // Auto-scale y-axis: 10% padding on faint side, 50% on bright side
  // (matches generate_sed.py convention)
  const yRange: [number, number] = useMemo(() => {
    let brightest = 40;
    let faintest = 0;
    for (const b of plotData) {
      if (b.y_upper_limit !== undefined) {
        // Upper limit
        if (isFinite(b.y)) {
          brightest = Math.min(brightest, b.y - 0.3);
          faintest = Math.max(faintest, b.y);
        }
      } else {
        // Detection
        if (isFinite(b.y) && isFinite(b.y_err)) {
          brightest = Math.min(brightest, b.y - b.y_err);
          faintest = Math.max(faintest, b.y + b.y_err);
        }
      }
    }
    const range = faintest - brightest;
    // [faint end, bright end] — Plotly renders range[0] > range[1] as reversed axis
    return [
      Math.ceil(faintest + 0.1 * range),
      Math.floor(brightest - 0.5 * range),
    ];
  }, [plotData]);

  // Group by category for legend
  const categories = useMemo(() => {
    const cats = new Map<string, string>();
    bandData.forEach(b => cats.set(b.category, b.color));
    return cats;
  }, [bandData]);

  // Build Plotly traces
  const traces: Plotly.Data[] = useMemo(() => {
    const result: Plotly.Data[] = [];

    for (const [category, color] of categories) {
      const catBands = plotData.filter(b => b.category === category);
      const detections = catBands.filter(b => !b.y_upper_limit);
      const limits = catBands.filter(b => b.y_upper_limit !== undefined);

      if (detections.length > 0) {
        result.push({
          type: 'scatter',
          mode: 'markers',
          name: category,
          x: detections.map(b => b.wav),
          y: detections.map(b => b.y),
          error_y: {
            type: 'data',
            array: detections.map(b => b.y_err),
            visible: true,
            color: color,
            thickness: 1.5,
          },
          error_x: {
            type: 'data',
            array: detections.map(b => b.wav_max - b.wav),
            arrayminus: detections.map(b => b.wav - b.wav_min),
            visible: true,
            color: color,
            thickness: 1,
          },
          marker: { color, size: 8, symbol: 'circle' },
          text: detections.map(b => {
            const mag = -2.5 * Math.log10(b.flux) + 23.9;
            return `${b.name}<br>mag: ${mag.toFixed(2)}<br>flux: ${b.flux.toFixed(3)} µJy<br>err: ${b.flux_err.toFixed(3)} µJy<br>SNR: ${b.snr.toFixed(1)}`;
          }),
          hoverinfo: 'text',
        } as Plotly.Data);
      }

      if (limits.length > 0) {
        result.push({
          type: 'scatter',
          mode: 'markers',
          name: `${category} (upper limits)`,
          showlegend: false,
          x: limits.map(b => b.wav),
          y: limits.map(b => b.y),
          error_x: {
            type: 'data',
            array: limits.map(b => b.wav_max - b.wav),
            arrayminus: limits.map(b => b.wav - b.wav_min),
            visible: true,
            color: color,
            thickness: 1,
          },
          marker: {
            color: color,
            size: 8,
            symbol: 'triangle-down',
            opacity: 0.6,
          },
          text: limits.map(b => `${b.name} (2σ upper limit)<br>mag: ${b.y.toFixed(2)}<br>flux: ${(2 * b.flux_err).toFixed(3)} µJy`),
          hoverinfo: 'text',
        } as Plotly.Data);
      }
    }

    // Template SED overlay from P(z) data
    if (pzData?.template_wav && pzData?.template_flux_ujy) {
      const templateY = pzData.template_flux_ujy.map(f =>
        f > 0 ? -2.5 * Math.log10(f) + 23.9 : NaN
      );

      result.push({
        type: 'scatter',
        mode: 'lines',
        name: `${pzData.label} (z=${pzData.z_best.toFixed(3)})`,
        x: pzData.template_wav,
        y: templateY,
        line: { color: pzData.color, width: 1.5 },
        opacity: 0.8,
        hoverinfo: 'name',
      } as Plotly.Data);
    }

    return result;
  }, [plotData, categories, pzData]);

  // Layout — use shared plot colors
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const plotColors = useMemo(() => getPlotColors(), [resolvedTheme]);

  const hasPzPanel = !!(pzData || pzLoading);

  const layout: Partial<Plotly.Layout> = useMemo(() => ({
    autosize: true,
    height: 400,
    margin: { l: 70, r: 20, t: 10, b: 50 },
    paper_bgcolor: plotColors.paper,
    plot_bgcolor: plotColors.bg,
    font: { color: plotColors.text, family: 'Inter, sans-serif', size: 12 },
    xaxis: {
      title: { text: 'Wavelength (µm)' },
      type: 'log',
      range: [Math.log10(0.3), Math.log10(20)],
      gridcolor: plotColors.grid,
      zerolinecolor: plotColors.grid,
      tickformat: '.2f',
    },
    yaxis: {
      title: { text: 'AB Magnitude' },
      type: 'linear',
      range: yRange,
      gridcolor: plotColors.grid,
      zerolinecolor: plotColors.grid,
    },
    legend: {
      x: 0.02,
      y: 0.02,
      bgcolor: 'rgba(0,0,0,0)',
      font: { size: 10, color: plotColors.text },
    },
    hovermode: 'closest',
  }), [plotColors, yRange]);

  // P(z) traces for the inset panel
  const pzTraces: Plotly.Data[] = useMemo(() => {
    if (!pzData?.z_grid || !pzData?.pz) return [];
    const result: Plotly.Data[] = [];

    result.push({
      type: 'scatter',
      mode: 'lines',
      name: pzData.label,
      x: pzData.z_grid,
      y: pzData.pz,
      line: { color: pzData.color, width: 1.5 },
      fill: 'tozeroy',
      fillcolor: `${pzData.color}20`,
      hoverinfo: 'name',
    } as Plotly.Data);

    // Spectroscopic redshift line
    if (bestRedshift !== null) {
      result.push({
        type: 'scatter',
        mode: 'lines',
        name: `z_spec = ${bestRedshift.toFixed(4)}`,
        x: [bestRedshift, bestRedshift],
        y: [0, 1.05],
        line: { color: isDark ? '#f8fafc' : '#1e293b', width: 1.5, dash: 'dash' },
        hoverinfo: 'name',
      } as Plotly.Data);
    }

    return result;
  }, [pzData, bestRedshift, isDark]);

  const pzLayout: Partial<Plotly.Layout> = useMemo(() => ({
    autosize: true,
    height: 400,
    margin: { l: 50, r: 20, t: 10, b: 50 },
    paper_bgcolor: plotColors.paper,
    plot_bgcolor: plotColors.bg,
    font: { color: plotColors.text, family: 'Inter, sans-serif', size: 11 },
    xaxis: {
      title: { text: 'Redshift' },
      gridcolor: plotColors.grid,
      zerolinecolor: plotColors.grid,
    },
    yaxis: {
      title: { text: 'P(z)' },
      gridcolor: plotColors.grid,
      zerolinecolor: plotColors.grid,
      range: [0, 1.1],
    },
    legend: {
      x: 0.5,
      y: 0.98,
      bgcolor: 'rgba(0,0,0,0)',
      font: { size: 10, color: plotColors.text },
    },
    showlegend: true,
    hovermode: 'closest',
  }), [plotColors]);

  if (bandData.length === 0) {
    return null;
  }

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-5 h-5 text-text-secondary" />
          <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
            Photometry
          </h3>
          <span className="text-sm text-text-secondary">
            {photometry.catalog_name} ID {photometry.catalog_id}
            {photometry.match_distance_arcsec != null && (
              <> &middot; match: {photometry.match_distance_arcsec.toFixed(2)}&quot;</>
            )}
          </span>
        </div>
      </div>

      {/* Photo-z summary */}
      {photometry.photo_z != null && (
        <div className="text-sm text-text-secondary dark:text-slate-400 px-1">
          Photo-z: <span className="font-mono text-text-primary dark:text-slate-200">
            {photometry.photo_z.toFixed(4)}
          </span>
          {photometry.photo_z_err_lo != null && photometry.photo_z_err_hi != null && (
            <span className="font-mono">
              {' '}({photometry.photo_z_err_lo.toFixed(4)} &ndash; {photometry.photo_z_err_hi.toFixed(4)})
            </span>
          )}
          {bestRedshift != null && (
            <span>
              {' '}&middot; Spec-z: <span className="font-mono text-text-primary dark:text-slate-200">
                {bestRedshift.toFixed(4)}
              </span>
            </span>
          )}
        </div>
      )}

      {/* SED + P(z) side by side */}
      <div className="flex gap-3">
        {/* SED Plot */}
        <div className={`bg-white dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg overflow-hidden ${hasPzPanel ? 'flex-[2]' : 'flex-1'} min-w-0`}>
          <Plot
            data={traces}
            layout={layout}
            config={{ responsive: true, displayModeBar: false }}
            style={{ width: '100%' }}
          />
        </div>

        {/* P(z) Panel */}
        {hasPzPanel && (
          <div className="flex-1 min-w-0 bg-white dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg overflow-hidden">
            {pzLoading ? (
              <div className="flex items-center justify-center h-[400px]">
                <Loader2 className="w-5 h-5 animate-spin text-primary" />
                <span className="ml-2 text-sm text-text-secondary">Loading P(z)...</span>
              </div>
            ) : pzTraces.length > 0 ? (
              <Plot
                data={pzTraces}
                layout={pzLayout}
                config={{ responsive: true, displayModeBar: false }}
                style={{ width: '100%' }}
              />
            ) : null}
          </div>
        )}
      </div>

      {/* Photometry data table */}
      <PhotometryTable bands={bands} />
    </div>
  );
};
