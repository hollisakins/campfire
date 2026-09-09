/**
 * Empirical NIRSpec/MSA noise model — the TypeScript twin of
 * `etc/campfire_etc/model.py` for the hidden /nirspec/etc calculator.
 *
 * Per disperser/filter the model is the per-pixel variance of a centred faint
 * point source in the drizzled 2-D spectrum,
 *
 *     sigma_pix^2(lambda) = A(lambda) / T + B(lambda) / (T * t_exp^2)   [uJy^2]
 *
 * with T the total on-source time and t_exp the per-exposure time (seconds),
 * fitted in 0.1 um bins to the CAMPFIRE archive, plus the measured multipliers
 * that turn it into a proposal number (1-D/2-D noise ratio per extraction,
 * adjacent-pixel correlation, shutter-placement penalty, source-Poisson
 * coefficient, flux-recovery fractions).
 *
 * The model JSON is the one bundled with `campfire-etc`; `web/public/etc/models/`
 * holds verbatim copies (`npm run etc-models`). This module evaluates the model
 * on its native 0.1 um bin grid (the nearest bin to a requested wavelength),
 * which is what the published depth tables and the original calculator did;
 * the Python package additionally interpolates between bins for arbitrary
 * wavelengths — at bin centres the two agree to machine precision (see
 * `model.test.ts`, pinned to the same numbers as `etc/tests/test_model.py`).
 */

// ----------------------------------------------------------------- model JSON

export interface RecoveryRow {
  fwhm_lo: number;
  fwhm_hi: number;
  n: number;
  r3: number | null;
  ro: number | null;
  r3_16?: number | null;
  r3_84?: number | null;
}

export interface PandeiaRun {
  label: string;
  total_s: number;
  per_exposure_s: number;
  background: string;
  ratio_median: number | null;
  ratio_by_band: Record<string, number | null>;
  noise_curve_njy?: (number | null)[];
}

export interface DisperserModel {
  name: string;
  grating: string;
  filter: string;
  resolution_class: string;
  coverage: [number, number];
  wave: number[];
  band_ok: boolean[];
  A: (number | null)[];
  B: (number | null)[];
  f3: (number | null)[];
  fo: (number | null)[];
  rho1: (number | null)[];
  g: (number | null)[];
  dlds: (number | null)[];
  R: (number | null)[];
  n_res: (number | null)[];
  lam_out: number[];
  g_source: string;
  pos_coef: number[];
  pos_borrowed: boolean;
  mult_median: number;
  mult_mean: number;
  mult_84?: number;
  recovery: {
    band: string;
    from: string;
    n_match: number;
    r3_median: number | null;
    ro_median: number | null;
    rows: RecoveryRow[];
  };
  B_constrained: boolean;
  B_from: string | null;
  rn_frac: (number | null)[];
  te_ref: number;
  slope: number | null;
  scatter: { p2: number | null; p1: number | null; p0: number | null };
  obs_scatter: Record<string, number | null>;
  within: number | null;
  pipeline_error_ratio: {
    pixel_2d: (number | null)[];
    oned_3px: (number | null)[];
    oned_optimal: (number | null)[];
  };
  sample: {
    n_all: number;
    n_faint: number;
    n_std: number;
    n_obs: number;
    n_programs: number;
    T_range: [number, number];
    texp_range: [number, number];
    readpatt: Record<string, number>;
  };
  std_def: string;
  dropped_obs: string[];
  pandeia?: { version: string; runs: PandeiaRun[] };
}

export interface NoiseModel {
  schema: number;
  version: string;
  built: string;
  notes: string;
  archive: {
    n_spectra: number;
    n_faint_standard_fitted: number;
    n_observations_fitted: number;
    reduction: string;
  };
  readout_seconds_per_group: Record<string, number>;
  dispersers: Record<string, DisperserModel>;
}

export interface ModelManifest {
  latest: string;
  versions: { version: string; file: string; built: string; notes: string }[];
}

/** Public URL prefix of the copied model files. */
export const ETC_MODELS_BASE = '/etc/models';

// ----------------------------------------------------------------- constants

export const C_KMS = 299792.458;
/** f_lambda [erg s^-1 cm^-2 A^-1] = f_nu [uJy] / lambda[um]^2 * FNU_UJY_TO_FLAM */
export const FNU_UJY_TO_FLAM = 2.99792458e-19;
/** Fraction of an emission line's flux inside a +-1 FWHM window. */
export const LINE_WINDOW_FRACTION = 0.98;
/** Nearest bin has to be at most this far away for the model to be defined. */
export const MAX_BIN_GAP_UM = 0.101;

export const READOUT_GROUP_S: Record<string, number> = {
  nrsirs2: 72.944,
  nrsirs2rapid: 14.589,
  nrs: 42.947,
  nrsrapid: 10.737,
};
export const EXTRAPOLATED_READOUTS = new Set(['nrs', 'nrsrapid']);

export type Morphology = 'point' | 'compact' | 'typical' | 'extended';
export const MORPHOLOGY_FWHM_PX: Record<Morphology, number | null> = {
  point: null,
  compact: 1.9,
  typical: 2.3,
  extended: 2.8,
};
export const MORPHOLOGY_LABELS: Record<Morphology, string> = {
  point: 'true point source',
  compact: 'compact galaxy',
  typical: 'typical target',
  extended: 'extended galaxy',
};

export type Extraction = 'optimal' | '3px';
export type BinMode = 'pixel' | 'resolution' | 'R' | 'dlambda';

// ----------------------------------------------------------------- exposure

export interface Exposure {
  totalS: number;
  perExposureS: number;
  /** undefined when the times were given directly */
  readout?: string;
  ngroups?: number;
  nint?: number;
  nexp?: number;
}

export function makeExposure(
  args:
    | { readout: string; ngroups: number; nint?: number; nexp?: number }
    | { totalS: number; perExposureS: number }
): Exposure {
  if ('readout' in args) {
    const key = args.readout.trim().toLowerCase().replace(/[-_]/g, '');
    const tg = READOUT_GROUP_S[key];
    if (tg === undefined) throw new Error(`unknown readout pattern ${args.readout}`);
    const ngroups = Math.max(2, Math.round(args.ngroups));
    const nint = Math.max(1, Math.round(args.nint ?? 1));
    const nexp = Math.max(1, Math.round(args.nexp ?? 1));
    const te = ngroups * tg;
    return { totalS: te * nint * nexp, perExposureS: te, readout: key, ngroups, nint, nexp };
  }
  const perExposureS = Math.max(50, args.perExposureS);
  const totalS = Math.max(perExposureS, args.totalS);
  return { totalS, perExposureS };
}

export function fmtTime(seconds: number): string {
  if (!Number.isFinite(seconds)) return '—';
  if (seconds >= 1000) return `${(seconds / 1000).toFixed(seconds >= 10000 ? 1 : 2)} ks`;
  return `${Math.round(seconds)} s`;
}

/** Hours, e.g. "1.58 h" / "12.3 h". */
export function fmtHours(seconds: number): string {
  if (!Number.isFinite(seconds)) return '—';
  const h = seconds / 3600;
  return `${h.toFixed(h >= 10 ? 1 : 2)} h`;
}

/** "5.69 ks (1.58 h)" — the ks form with hours alongside for anything over ten minutes. */
export function fmtTimeH(seconds: number): string {
  if (!Number.isFinite(seconds)) return '—';
  return seconds >= 600 ? `${fmtTime(seconds)} (${fmtHours(seconds)})` : fmtTime(seconds);
}

/** Mantissa and exponent of x for rendering as m×10^e. */
export function sciParts(x: number): { m: string; e: number } | null {
  if (!Number.isFinite(x) || x <= 0) return null;
  const e = Math.floor(Math.log10(x));
  return { m: (x / Math.pow(10, e)).toFixed(1), e };
}

export function fmtSciText(x: number): string {
  const p = sciParts(x);
  return p ? `${p.m}×10^${p.e}` : '—';
}

// ----------------------------------------------------------------- helpers

function finite(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

export function median(values: (number | null | undefined)[]): number | null {
  const v = values.filter(finite).sort((a, b) => a - b);
  if (!v.length) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 ? v[mid] : 0.5 * (v[mid - 1] + v[mid]);
}

function interp(xs: number[], ys: number[], x: number): number {
  if (x <= xs[0]) return ys[0];
  if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
  let i = 1;
  while (xs[i] < x) i++;
  const t = (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
  return ys[i - 1] + t * (ys[i] - ys[i - 1]);
}

/** Effective number of independent pixels when summing n adjacent pixels with lag-1 correlation rho. */
function neff(n: number, rho: number): number {
  return n * (1 + (2 * rho * (n - 1)) / n);
}

/** Index of the nearest usable bin to `wave`, or -1 when none is within MAX_BIN_GAP_UM. */
export function nearestBin(disp: DisperserModel, wave: number): number {
  let best = -1;
  let bestD = Infinity;
  for (let i = 0; i < disp.wave.length; i++) {
    if (!binUsable(disp, i)) continue;
    const d = Math.abs(disp.wave[i] - wave);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  }
  return bestD <= MAX_BIN_GAP_UM ? best : -1;
}

export function binUsable(disp: DisperserModel, i: number): boolean {
  return (
    disp.band_ok[i] &&
    finite(disp.A[i]) &&
    finite(disp.B[i]) &&
    finite(disp.fo[i]) &&
    finite(disp.f3[i]) &&
    finite(disp.dlds[i]) &&
    finite(disp.R[i])
  );
}

export function inCoverage(disp: DisperserModel, wave: number): boolean {
  return wave >= disp.coverage[0] && wave <= disp.coverage[1];
}

// ----------------------------------------------------------------- placement / recovery

/** Noise multiplier for a source offset |x| (dispersion) and |y| (slit) in shutter units. */
export function placementMultiplier(disp: DisperserModel, x: number, y: number): number {
  const c = disp.pos_coef;
  x = Math.abs(x);
  y = Math.abs(y);
  return Math.pow(10, c[1] * x * x + c[2] * y * y + c[3] * x ** 4 + c[4] * y ** 4);
}

export type Placement = 'typical' | 'centred' | 'mean' | { x: number; y: number };

export function resolvePlacement(disp: DisperserModel, placement: Placement): { mult: number; label: string } {
  if (placement === 'typical') return { mult: disp.mult_median, label: 'typical MSA placement (archive median)' };
  if (placement === 'centred') return { mult: 1, label: 'centred in the shutter' };
  if (placement === 'mean') return { mult: disp.mult_mean, label: 'random placement (archive mean)' };
  const m = placementMultiplier(disp, placement.x, placement.y);
  return {
    mult: m,
    label: `offset |x|=${Math.abs(placement.x).toFixed(2)}, |y|=${Math.abs(placement.y).toFixed(2)} shutter units`,
  };
}

/** Fraction of the total photometric flux landing in the extracted spectrum for a source of the given spatial FWHM (null = point source). */
export function recovery(disp: DisperserModel, fwhmPx: number | null, extraction: Extraction): number {
  if (fwhmPx === null) return 1;
  const rows = disp.recovery.rows.filter((r) => finite(r.r3));
  if (!rows.length) return 0.42;
  const xs = rows.map((r) => 0.5 * (r.fwhm_lo + r.fwhm_hi));
  const ys = rows.map((r) => (extraction === 'optimal' && finite(r.ro) ? r.ro : (r.r3 as number)));
  return interp(xs, ys, fwhmPx);
}

export function resolveMorphology(
  disp: DisperserModel,
  morphology: Morphology | 'custom',
  fwhmPx: number | null,
  extraction: Extraction,
  recoveryOverride: number | null
): { rec: number; label: string; fwhm: number | null } {
  const fw =
    morphology === 'custom'
      ? Math.min(3.5, Math.max(1.4, fwhmPx ?? 2.3))
      : MORPHOLOGY_FWHM_PX[morphology];
  const label = morphology === 'custom' ? `FWHM ${fw!.toFixed(1)} px` : MORPHOLOGY_LABELS[morphology];
  if (recoveryOverride !== null && Number.isFinite(recoveryOverride)) {
    return { rec: Math.min(1, Math.max(0.05, recoveryOverride)), label, fwhm: fw };
  }
  return { rec: recovery(disp, fw, extraction), label, fwhm: fw };
}

// ----------------------------------------------------------------- the arithmetic

export interface ContinuumOptions {
  /** Flux *in the extracted spectrum* [uJy] (total flux times recovery); null for depths only. */
  fluxSpecUjy?: number | null;
  extraction?: Extraction;
  placementMult?: number;
  margin?: number;
  binMode?: BinMode;
  binValue?: number | null;
}

export interface ContinuumRow {
  i: number;
  wave: number;
  /** 1σ per 2-D pixel [uJy] */
  sigPix: number;
  /** 1σ per 1-D pixel from background alone [uJy] */
  sigBg: number;
  /** 1σ per 1-D pixel, source Poisson included [uJy] */
  sig1d: number;
  sigRes: number;
  sigBin: number;
  nRes: number;
  nBin: number;
  rho: number;
  dlds: number;
  R: number;
  ab5Pix: number;
  ab5Res: number;
  ab5Bin: number;
  /** 5σ unresolved-line limit [erg s^-1 cm^-2] */
  line5: number;
  snrPix: number;
  snrRes: number;
  snrBin: number;
}

const ab5 = (sigUjy: number) => -2.5 * Math.log10(5 * sigUjy * 1e-6) + 8.9;

/** Continuum noise, depth and S/N on the model's bin grid; null entries where the model is undefined. */
export function continuum(disp: DisperserModel, exp: Exposure, opts: ContinuumOptions = {}): (ContinuumRow | null)[] {
  const extraction = opts.extraction ?? 'optimal';
  const F = opts.fluxSpecUjy ?? null;
  const mult = (opts.placementMult ?? 1) * (opts.margin ?? 1);
  const binMode = opts.binMode ?? 'resolution';
  const binValue = opts.binValue ?? null;
  const T = exp.totalS;
  const te = exp.perExposureS;
  const out: (ContinuumRow | null)[] = [];
  for (let i = 0; i < disp.wave.length; i++) {
    if (!binUsable(disp, i)) {
      out.push(null);
      continue;
    }
    const w = disp.wave[i];
    const A = disp.A[i] as number;
    const B = disp.B[i] as number;
    const sigPix = Math.sqrt(A / T + B / (T * te * te)) * mult;
    const f = (extraction === 'optimal' ? disp.fo[i] : disp.f3[i]) as number;
    const sigBg = sigPix * f;
    const g = finite(disp.g[i]) ? (disp.g[i] as number) : 0;
    const sig1d = F === null ? sigBg : Math.sqrt(sigBg * sigBg + (g * Math.max(F, 0)) / T);
    const rho = Math.min(0.5, Math.max(-0.2, finite(disp.rho1[i]) ? (disp.rho1[i] as number) : 0));
    const dlds = disp.dlds[i] as number;
    const R = disp.R[i] as number;
    const nRes = w / R / dlds;
    let nBin: number;
    if (binMode === 'pixel') nBin = 1;
    else if (binMode === 'resolution') nBin = nRes;
    else if (binMode === 'R') nBin = Math.max(1, w / (binValue || 100) / dlds);
    else nBin = Math.max(1, (binValue || 0.01) / dlds);
    const sigBin = (sig1d * Math.sqrt(neff(nBin, rho))) / nBin;
    const sigRes = (sig1d * Math.sqrt(neff(nRes, rho))) / nRes;
    const flam = (sig1d / (w * w)) * FNU_UJY_TO_FLAM;
    const nl = Math.max(2 * nRes, 2);
    const line5 = (5 * flam * (dlds * 1e4) * Math.sqrt(neff(nl, rho))) / LINE_WINDOW_FRACTION;
    out.push({
      i,
      wave: w,
      sigPix,
      sigBg,
      sig1d,
      sigRes,
      sigBin,
      nRes,
      nBin,
      rho,
      dlds,
      R,
      ab5Pix: ab5(sig1d),
      ab5Res: ab5(sigRes),
      ab5Bin: ab5(sigBin),
      line5,
      snrPix: F === null ? NaN : F / sig1d,
      snrRes: F === null ? NaN : F / sigRes,
      snrBin: F === null ? NaN : F / sigBin,
    });
  }
  return out;
}

export interface LineResult {
  wave: number;
  fwhmUm: number;
  windowPx: number;
  nEff: number;
  snr: number;
  /** 5σ limit on the line's total flux [erg s^-1 cm^-2] */
  limit5: number;
}

/**
 * S/N of an emission line of total flux `fluxCgs` [erg/s/cm2] at `waveUm`
 * with intrinsic FWHM `fwhmKms` (0 = unresolved), integrated over ±1 FWHM
 * including the instrumental resolution. `rowAt` is the continuum row at
 * the nearest bin (its `sig1d` carries the continuum's photon noise).
 */
export function line(
  disp: DisperserModel,
  exp: Exposure,
  rowAt: ContinuumRow,
  fluxCgs: number,
  fwhmKms = 0
): LineResult {
  const w = rowAt.wave;
  const { R, dlds, rho, sig1d } = rowAt;
  const g = finite(disp.g[rowAt.i]) ? (disp.g[rowAt.i] as number) : 0;
  const fw = Math.sqrt(((w * fwhmKms) / C_KMS) ** 2 + (w / R) ** 2);
  const n = Math.max((2 * fw) / dlds, 2);
  const ne = neff(n, rho);
  const S = (LINE_WINDOW_FRACTION * (fluxCgs / (dlds * 1e4)) * w * w) / FNU_UJY_TO_FLAM;
  const sigS = Math.sqrt(ne * sig1d * sig1d + (g * S) / exp.totalS);
  const limit5 = (5 * sig1d * Math.sqrt(ne) * (dlds * 1e4) * FNU_UJY_TO_FLAM) / (w * w) / LINE_WINDOW_FRACTION;
  return { wave: w, fwhmUm: fw, windowPx: n, nEff: ne, snr: sigS > 0 ? S / sigS : NaN, limit5 };
}

/** Total time (s) reaching `snrTarget` at the same per-exposure time (S/N ∝ √T at fixed t_exp). */
export function timeForSnr(exp: Exposure, snrNow: number, snrTarget: number): number {
  if (!Number.isFinite(snrNow) || snrNow <= 0) return NaN;
  return exp.totalS * (snrTarget / snrNow) ** 2;
}

/** AB magnitude → total flux [uJy]. */
export function abToUjy(mag: number): number {
  return Math.pow(10, -0.4 * (mag - 23.9));
}

// ----------------------------------------------------------------- descriptive helpers (report tables)

export function disperserCaveats(d: DisperserModel): string[] {
  const out: string[] = [];
  if (d.B_constrained) out.push(`read-noise/sky ratio borrowed from ${d.B_from} (archive cannot separate the two terms)`);
  if (d.pos_borrowed) out.push('shutter-placement term borrowed from PRISM (too few faint spectra to fit)');
  if (d.recovery.from !== 'measured') out.push(`flux-recovery table ${d.recovery.from}`);
  if (d.g_source !== 'measured') out.push(`source-Poisson coefficient ${d.g_source}`);
  if (d.sample.n_obs < 6) out.push(`only ${d.sample.n_obs} observations constrain the exposure-time scaling`);
  return out;
}

/** Bin index nearest the middle of the disperser's coverage. */
export function midBin(d: DisperserModel): number {
  return nearestBin(d, 0.5 * (d.coverage[0] + d.coverage[1]));
}
