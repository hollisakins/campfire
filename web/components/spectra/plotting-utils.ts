/**
 * Shared utilities for spectrum plotting components
 */

// Flux unit types
export type FluxUnit = 'fnu' | 'flambda';

// Plot color type
export interface PlotColors {
  bg: string;
  paper: string;
  grid: string;
  text: string;
  textSecondary: string;
}

/**
 * Get Plotly colors from CSS variables for theme-aware charts
 */
export function getPlotColors(): PlotColors {
  if (typeof window === 'undefined') {
    return { bg: '#ffffff', paper: '#f8fafc', grid: '#e2e8f0', text: '#0f172a', textSecondary: '#64748b' };
  }
  const style = getComputedStyle(document.documentElement);
  return {
    bg: style.getPropertyValue('--plot-bg').trim() || '#ffffff',
    paper: style.getPropertyValue('--plot-paper').trim() || '#f8fafc',
    grid: style.getPropertyValue('--plot-grid').trim() || '#e2e8f0',
    text: style.getPropertyValue('--plot-text').trim() || '#0f172a',
    textSecondary: style.getPropertyValue('--plot-text-secondary').trim() || '#64748b',
  };
}

// Common emission lines with rest wavelengths in microns
// Colors assigned as rainbow from blue (short λ) to red (long λ)
export const EMISSION_LINES = [
  { name: 'Lyα', wave: 0.12157, color: '#6366f1' },      // indigo (shortest)
  { name: 'CIV', wave: 0.1549, color: '#4f46e5' },       // indigo-600
  { name: 'CIII]', wave: 0.1909, color: '#4338ca' },     // indigo-700
  { name: 'MgII', wave: 0.2798, color: '#2563eb' },      // blue-600
  { name: '[OII]', wave: 0.3727, color: '#0ea5e9' },     // sky-500
  { name: 'Hδ', wave: 0.4102, color: '#06b6d4' },        // cyan-500
  { name: 'Hγ', wave: 0.4341, color: '#14b8a6' },        // teal-500
  { name: '[OIII]', wave: 0.436321, color: '#0d9488' },  // teal-600 (auroral)
  { name: 'Hβ', wave: 0.4861, color: '#10b981' },        // emerald-500
  { name: '[OIII]₁', wave: 0.4959, color: '#22c55e' },   // green-500
  { name: '[OIII]₂', wave: 0.5007, color: '#84cc16' },   // lime-500
  { name: 'HeI', wave: 0.5875624, color: '#a3e635' },    // lime-400
  { name: 'Hα', wave: 0.6563, color: '#eab308' },        // yellow-500
  { name: '[NII]', wave: 0.6584, color: '#f59e0b' },     // amber-500
  { name: '[SII]₁', wave: 0.6717, color: '#f97316' },    // orange-500
  { name: '[SII]₂', wave: 0.6731, color: '#ef4444' },    // red-500
  { name: '[SIII]₁', wave: 0.90686, color: '#e11d48' },  // rose-600
  { name: '[SIII]₂', wave: 0.95311, color: '#be123c' },  // rose-700
  { name: 'HeI', wave: 1.0833315, color: '#a21caf' },    // fuchsia-700
  { name: 'Paγ', wave: 1.0941090, color: '#c026d3' },    // fuchsia-600
  { name: 'Paβ', wave: 1.2822, color: '#dc2626' },       // red-600
  { name: 'Paα', wave: 1.8751, color: '#b91c1c' },       // red-700 (longest)
];

/**
 * Convert f_nu to f_lambda
 *
 * f_λ = f_ν * c / λ²
 *
 * Where:
 * - f_nu is in μJy (1 μJy = 10^-29 erg/s/cm²/Hz)
 * - wavelength is in μm
 * - f_λ is in erg/s/cm²/Å
 *
 * With c = 2.998e10 cm/s and λ in μm (1 μm = 10^-4 cm):
 * f_λ = f_ν * 10^-29 * 2.998e10 / (λ_μm * 10^-4)² / 10^8 (to convert /cm to /Å)
 * f_λ = f_ν * 2.998e-19 / λ_μm²
 *
 * @param fnuValue - Flux in μJy
 * @param wavelength - Wavelength in μm
 * @returns Flux in erg/s/cm²/Å
 */
export function convertToFlambda(fnuValue: number, wavelength: number): number {
  return fnuValue * 2.998e-19 / (wavelength * wavelength);
}

/**
 * Get flux label for plot axis based on unit
 */
export function getFluxLabel(unit: FluxUnit): string {
  return unit === 'fnu' ? 'fν (μJy)' : 'fλ (erg/s/cm²/Å)';
}

/**
 * Get hover label for flux based on unit
 */
export function getHoverLabel(unit: FluxUnit): string {
  return unit === 'fnu' ? 'fν' : 'fλ';
}

/**
 * Calculate observed wavelengths for emission lines at given redshift
 * and filter to visible range
 */
export function getVisibleEmissionLines(
  redshift: number,
  waveMin: number,
  waveMax: number
) {
  return EMISSION_LINES
    .map(line => ({
      ...line,
      observedWave: line.wave * (1 + redshift),
    }))
    .filter(line => line.observedWave >= waveMin && line.observedWave <= waveMax);
}

/**
 * Compute a smart y-axis range for spectrum plots.
 *
 * Uses model (if available) or high-SNR data points to determine
 * a range that highlights real spectral features rather than noisy outliers.
 *
 * @returns A [yMin, yMax] tuple, or undefined to let Plotly auto-scale.
 */
export function computeYRange(
  flux: number[],
  fluxErr: (number | null)[],
  options?: {
    modelFlux?: number[] | null;
    modelWave?: number[] | null;
    dataWave?: number[];
    edgeTrim?: number;
  }
): [number, number] | undefined {
  const edgeTrim = options?.edgeTrim ?? 20;

  // Edge trimming
  const start = edgeTrim;
  const end = flux.length - edgeTrim;
  if (end - start < 10) return undefined;

  let rangeMin: number;
  let rangeMax: number;

  const modelFlux = options?.modelFlux;
  const modelWave = options?.modelWave;
  const dataWave = options?.dataWave;

  if (modelFlux && modelWave && modelFlux.length > 0 && dataWave) {
    // Path A: Model available — use model points within trimmed data wavelength range
    const trimmedWaveMin = dataWave[start];
    const trimmedWaveMax = dataWave[end - 1];

    const filteredModelFlux: number[] = [];
    for (let i = 0; i < modelWave.length; i++) {
      if (modelWave[i] >= trimmedWaveMin && modelWave[i] <= trimmedWaveMax) {
        filteredModelFlux.push(modelFlux[i]);
      }
    }

    if (filteredModelFlux.length === 0) return undefined;

    rangeMin = filteredModelFlux[0];
    rangeMax = filteredModelFlux[0];
    for (let i = 1; i < filteredModelFlux.length; i++) {
      if (filteredModelFlux[i] < rangeMin) rangeMin = filteredModelFlux[i];
      if (filteredModelFlux[i] > rangeMax) rangeMax = filteredModelFlux[i];
    }
  } else {
    // Path B: Data only — use high-SNR pixels or percentile fallback
    const hasErrors = fluxErr.slice(start, end).some(e => e !== null && e > 0);

    if (hasErrors) {
      // Collect valid errors to compute median
      const validErrors: number[] = [];
      for (let i = start; i < end; i++) {
        const err = fluxErr[i];
        if (err !== null && err > 0) validErrors.push(err);
      }

      if (validErrors.length < 5) return undefined;

      validErrors.sort((a, b) => a - b);
      const medianErr = validErrors[Math.floor(validErrors.length / 2)];

      // Keep pixels with reasonable errors (reject bad detector regions),
      // but don't filter by SNR — low-flux regions are legitimate
      const reliableFlux: number[] = [];
      for (let i = start; i < end; i++) {
        const err = fluxErr[i];
        if (err !== null && err > 0 && err < 3 * medianErr && isFinite(flux[i])) {
          reliableFlux.push(flux[i]);
        }
      }

      if (reliableFlux.length < 5) return undefined;

      reliableFlux.sort((a, b) => a - b);

      // MAD-based bounds: tight around the median, robust to noise
      const median = reliableFlux[Math.floor(reliableFlux.length / 2)];
      const deviations = reliableFlux.map(v => Math.abs(v - median));
      deviations.sort((a, b) => a - b);
      const mad = deviations[Math.floor(deviations.length / 2)];
      const madMin = median - 5 * mad;
      const madMax = median + 5 * mad;

      // Percentile bounds: safety net for bright features
      const pLow = reliableFlux[Math.floor(reliableFlux.length * 0.005)];
      const pHigh = reliableFlux[Math.floor(reliableFlux.length * 0.995)];

      // Hybrid: take the wider bound at each end
      rangeMin = Math.min(madMin, pLow);
      rangeMax = Math.max(madMax, pHigh);
    } else {
      // Fallback: 5th–95th percentile of trimmed flux
      const trimmedFlux = flux.slice(start, end).filter(v => isFinite(v));
      if (trimmedFlux.length < 5) return undefined;

      trimmedFlux.sort((a, b) => a - b);
      const p5 = trimmedFlux[Math.floor(trimmedFlux.length * 0.05)];
      const p95 = trimmedFlux[Math.floor(trimmedFlux.length * 0.95)];
      rangeMin = p5;
      rangeMax = p95;
    }
  }

  // Safety: degenerate or invalid range
  if (!isFinite(rangeMin) || !isFinite(rangeMax)) return undefined;
  const totalRange = rangeMax - rangeMin;
  if (totalRange <= 0) return undefined;

  // Additive padding (20% each side)
  return [rangeMin - 0.2 * totalRange, rangeMax + 0.2 * totalRange];
}

/**
 * Create Plotly traces for emission line markers
 */
export function createEmissionLineTraces(
  redshift: number,
  waveMin: number,
  waveMax: number,
  fluxMin: number,
  fluxMax: number,
  xaxis: string = 'x',
  yaxis: string = 'y'
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): any[] {
  const visibleLines = getVisibleEmissionLines(redshift, waveMin, waveMax);

  return visibleLines.map(line => ({
    x: [line.observedWave, line.observedWave],
    y: [fluxMin * 0.9, fluxMax * 1.1],
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
    xaxis,
    yaxis,
  }));
}
