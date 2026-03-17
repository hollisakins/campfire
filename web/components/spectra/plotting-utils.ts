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
// Rest wavelengths in vacuum (microns), matching pipeline templates.py.
// JWST measures in vacuum — optical/NIR lines converted from air via SDSS formula.
// gratingOnly: if true, only shown for non-PRISM gratings (blended with neighbor at PRISM resolution)
export const EMISSION_LINES = [
  { name: 'Lyα',      wave: 0.1215670, color: '#6366f1' },                     // indigo-500 (shortest)
  { name: 'NV₁',      wave: 0.1238821, color: '#818cf8', gratingOnly: true },  // indigo-400
  { name: 'NV₂',      wave: 0.1242804, color: '#818cf8', gratingOnly: true },  // indigo-400
  { name: 'SiII₁',    wave: 0.1260422, color: '#a78bfa', gratingOnly: true },  // violet-400
  { name: 'SiII₂',    wave: 0.1264730, color: '#a78bfa', gratingOnly: true },  // violet-400
  { name: 'SiIV₁',    wave: 0.1393755, color: '#7c3aed', gratingOnly: true },  // violet-700
  { name: 'OIV]₁',    wave: 0.1397232, color: '#6d28d9', gratingOnly: true },  // violet-800
  { name: 'OIV]₂',    wave: 0.1399780, color: '#6d28d9', gratingOnly: true },  // violet-800
  { name: 'SiIV₂',    wave: 0.1402770, color: '#7c3aed', gratingOnly: true },  // violet-700
  { name: 'NIV]',     wave: 0.1486496, color: '#5b21b6', gratingOnly: true },  // violet-900
  { name: 'CIV₁',     wave: 0.1548187, color: '#4f46e5', gratingOnly: true },  // indigo-600
  { name: 'CIV',      wave: 0.1549480, color: '#4f46e5' },                     // indigo-600 (centroid)
  { name: 'CIV₂',     wave: 0.1550772, color: '#4f46e5', gratingOnly: true },  // indigo-600
  { name: 'OIII]₁',   wave: 0.1660809, color: '#3730a3', gratingOnly: true },  // indigo-800
  { name: 'OIII]₂',   wave: 0.1666150, color: '#3730a3', gratingOnly: true },  // indigo-800
  { name: 'NIII]₁',   wave: 0.1746823, color: '#1e40af', gratingOnly: true },  // blue-800
  { name: 'NIII]₂',   wave: 0.1748656, color: '#1e40af', gratingOnly: true },  // blue-800
  { name: 'AlIII₁',   wave: 0.1854716, color: '#1e3a8a', gratingOnly: true },  // blue-900
  { name: 'AlIII₂',   wave: 0.1862790, color: '#1e3a8a', gratingOnly: true },  // blue-900
  { name: 'SiIII]',   wave: 0.1892030, color: '#1d4ed8', gratingOnly: true },  // blue-700
  { name: 'CIII]',    wave: 0.1908734, color: '#4338ca' },                     // indigo-700
  { name: 'MgII',     wave: 0.2799942, color: '#2563eb' },                     // blue-600
  { name: '[NeV]',    wave: 0.3426440, color: '#1d4ed8', gratingOnly: true },  // blue-700
  { name: '[OII]',    wave: 0.3728484, color: '#0ea5e9' },                     // sky-500
  { name: '[NeIII]₁', wave: 0.3869860, color: '#0284c7', gratingOnly: true },  // sky-600
  { name: '[NeIII]₂', wave: 0.3968593, color: '#0284c7', gratingOnly: true },  // sky-600 (λ3967)
  { name: 'Hε',       wave: 0.3971200, color: '#0891b2', gratingOnly: true },  // cyan-600
  { name: 'Hδ',       wave: 0.4102892, color: '#06b6d4' },                     // cyan-500
  { name: 'Hγ',       wave: 0.4341692, color: '#14b8a6' },                     // teal-500
  { name: '[OIII]',   wave: 0.4364437, color: '#0d9488' },                     // teal-600 (auroral; 22Å from Hγ)
  { name: 'HeII',     wave: 0.4686000, color: '#059669', gratingOnly: true },  // emerald-600
  { name: 'Hβ',       wave: 0.4862692, color: '#10b981' },                     // emerald-500
  { name: '[OIII]₁',  wave: 0.4960296, color: '#22c55e' },                     // green-500
  { name: '[OIII]₂',  wave: 0.5008241, color: '#84cc16' },                     // lime-500
  { name: 'HeI',      wave: 0.5877255, color: '#a3e635' },                     // lime-400
  { name: '[OI]',     wave: 0.6302050, color: '#ca8a04', gratingOnly: true },  // yellow-600
  { name: '[NII]',    wave: 0.6549860, color: '#facc15', gratingOnly: true },  // yellow-400 (λ6549)
  { name: 'Hα',       wave: 0.6564635, color: '#eab308' },                     // yellow-500
  { name: '[NII]',    wave: 0.6585282, color: '#f59e0b', gratingOnly: true },  // amber-500 (λ6585; 20Å from Hα)
  { name: '[SII]₁',   wave: 0.6718298, color: '#f97316' },                     // orange-500
  { name: '[SII]₂',   wave: 0.6732671, color: '#ef4444', gratingOnly: true },  // red-500 (15Å from [SII]₁)
  { name: '[ArIII]',  wave: 0.7137770, color: '#c2410c', gratingOnly: true },  // orange-700
  { name: '[SIII]₁',  wave: 0.9071095, color: '#e11d48' },                     // rose-600
  { name: '[SIII]₂',  wave: 0.9533721, color: '#be123c' },                     // rose-700
  { name: 'Paδ',      wave: 1.0049700, color: '#9d174d', gratingOnly: true },  // rose-800
  { name: 'HeI',      wave: 1.0833315, color: '#a21caf' },                     // fuchsia-700
  { name: 'Paγ',      wave: 1.0941090, color: '#c026d3' },                     // fuchsia-600 (108Å from HeI)
  { name: 'Paβ',      wave: 1.2821600, color: '#dc2626' },                     // red-600
  { name: 'Paα',      wave: 1.8756100, color: '#b91c1c' },                     // red-700 (longest)
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
 * and filter to visible range. For PRISM, grating-only lines are excluded.
 */
export function getVisibleEmissionLines(
  redshift: number,
  waveMin: number,
  waveMax: number,
  grating?: string
) {
  const isPrism = !grating || grating === 'PRISM';
  return EMISSION_LINES
    .filter(line => !isPrism || !line.gratingOnly)
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
 * Compute nicely-spaced rest-frame wavelength tick values (in Å)
 * for a given observed wavelength range (in μm).
 *
 * @param obsMin - Minimum observed wavelength in μm
 * @param obsMax - Maximum observed wavelength in μm
 * @param factor - Conversion factor: 10000 / (1 + z)
 * @param targetCount - Desired number of ticks (default 6)
 * @returns Array of nice round rest-frame wavelength values in Å
 */
export function computeNiceRestTicks(
  obsMin: number,
  obsMax: number,
  factor: number,
  targetCount = 6
): number[] {
  const restMin = obsMin * factor;
  const restMax = obsMax * factor;
  const range = restMax - restMin;
  if (range <= 0) return [];

  const roughStep = range / targetCount;
  const mag = Math.pow(10, Math.floor(Math.log10(roughStep)));
  const residual = roughStep / mag;
  let niceStep: number;
  if (residual <= 1.5) niceStep = mag;
  else if (residual <= 3.5) niceStep = 2 * mag;
  else if (residual <= 7.5) niceStep = 5 * mag;
  else niceStep = 10 * mag;

  const ticks: number[] = [];
  const start = Math.ceil(restMin / niceStep) * niceStep;
  for (let t = start; t <= restMax; t += niceStep) {
    ticks.push(parseFloat(t.toFixed(6)));
  }
  return ticks;
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
  yaxis: string = 'y',
  grating?: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): any[] {
  const visibleLines = getVisibleEmissionLines(redshift, waveMin, waveMax, grating);

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
