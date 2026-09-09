'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Check, Copy, Link2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { EtcChart } from './EtcChart';
import {
  EXTRAPOLATED_READOUTS,
  READOUT_GROUP_S,
  abToUjy,
  continuum,
  fmtHours,
  fmtTime,
  fmtTimeH,
  line,
  makeExposure,
  nearestBin,
  resolveMorphology,
  resolvePlacement,
  sciParts,
  timeForSnr,
  type BinMode,
  type ContinuumRow,
  type Extraction,
  type Morphology,
  type NoiseModel,
} from '@/lib/etc/model';
import { H0_KM_S_MPC, OMEGA_M, apparentFromAbsolute } from '@/lib/etc/cosmology';

// ----------------------------------------------------------------- state

type PosMode = 'typ' | 'cen' | 'mean' | 'custom';
type BinKey = 'res' | 'pix' | 'R' | 'dl';

export interface CalcState {
  disp: string;
  ro: string;
  ng: number;
  ni: number;
  ne: number;
  direct: boolean;
  /** total time [ks] when `direct` */
  T: number;
  /** per-exposure time [s] when `direct` */
  te: number;
  /** 'app': apparent AB magnitude; 'abs': absolute magnitude + redshift */
  mm: 'app' | 'abs';
  mag: number;
  /** absolute AB magnitude (flat f_nu) when mm = 'abs' */
  Mabs: number;
  /** redshift when mm = 'abs' */
  z: number;
  size: Morphology | 'custom';
  fwhm: number;
  /** line flux [1e-18 cgs]; blank = no line */
  fl: string;
  /** observed line wavelength [um] */
  ll: string;
  /** line FWHM [km/s] */
  lv: number;
  /** wavelength of interest [um]; null = disperser default */
  lam: number | null;
  ext: Extraction;
  pos: PosMode;
  bin: BinKey;
  px: number;
  py: number;
  binv: number;
  /** recovery override, blank = from morphology */
  rec: string;
  snt: number;
  marg: number;
  showetc: boolean;
}

const DEFAULTS: CalcState = {
  disp: 'prism_clear',
  ro: 'nrsirs2',
  ng: 13,
  ni: 1,
  ne: 6,
  direct: false,
  T: 10,
  te: 1000,
  mm: 'app',
  mag: 27,
  Mabs: -20,
  z: 6,
  size: 'typical',
  fwhm: 2.3,
  fl: '',
  ll: '',
  lv: 0,
  lam: null,
  ext: 'optimal',
  pos: 'typ',
  bin: 'res',
  px: 0.18,
  py: 0.23,
  binv: 100,
  rec: '',
  snt: 5,
  marg: 1.1,
  showetc: true,
};

const NUMERIC: (keyof CalcState)[] = ['ng', 'ni', 'ne', 'T', 'te', 'mag', 'Mabs', 'z', 'fwhm', 'lv', 'lam', 'px', 'py', 'binv', 'snt', 'marg'];
const BOOLEAN: (keyof CalcState)[] = ['direct', 'showetc'];

function stateFromParams(params: URLSearchParams, model: NoiseModel): CalcState {
  const raw: Record<string, unknown> = { ...DEFAULTS };
  for (const key of Object.keys(DEFAULTS) as (keyof CalcState)[]) {
    const v = params.get(key);
    if (v === null) continue;
    if (NUMERIC.includes(key)) {
      const n = parseFloat(v);
      if (Number.isFinite(n)) raw[key] = n;
    } else if (BOOLEAN.includes(key)) {
      raw[key] = v === '1';
    } else {
      raw[key] = v;
    }
  }
  const s = raw as unknown as CalcState;
  if (!model.dispersers[s.disp]) s.disp = DEFAULTS.disp;
  if (!READOUT_GROUP_S[s.ro]) s.ro = DEFAULTS.ro;
  if (!['point', 'compact', 'typical', 'extended', 'custom'].includes(s.size)) s.size = DEFAULTS.size;
  if (s.mm !== 'app' && s.mm !== 'abs') s.mm = DEFAULTS.mm;
  if (!(s.z >= 0)) s.z = DEFAULTS.z;
  if (s.ext !== 'optimal' && s.ext !== '3px') s.ext = DEFAULTS.ext;
  if (!['typ', 'cen', 'mean', 'custom'].includes(s.pos)) s.pos = DEFAULTS.pos;
  if (!['res', 'pix', 'R', 'dl'].includes(s.bin)) s.bin = DEFAULTS.bin;
  return s;
}

function paramsFromState(s: CalcState): URLSearchParams {
  const u = new URLSearchParams();
  for (const key of Object.keys(DEFAULTS) as (keyof CalcState)[]) {
    const v = s[key];
    const d = DEFAULTS[key];
    if (v === d || v === '' || v === null) continue;
    u.set(key, typeof v === 'boolean' ? (v ? '1' : '0') : String(v));
  }
  return u;
}

const BIN_MODES: Record<BinKey, BinMode> = { res: 'resolution', pix: 'pixel', R: 'R', dl: 'dlambda' };

// ----------------------------------------------------------------- compute

function compute(model: NoiseModel, s: CalcState) {
  const disp = model.dispersers[s.disp];
  const exp = s.direct
    ? makeExposure({ totalS: Math.max(0.3, s.T) * 1000, perExposureS: Math.max(100, s.te) })
    : makeExposure({ readout: s.ro, ngroups: s.ng, nint: s.ni, nexp: s.ne });
  const recIn = parseFloat(s.rec);
  const morph = resolveMorphology(disp, s.size, s.size === 'custom' ? s.fwhm : null, s.ext, Number.isFinite(recIn) ? recIn : null);
  const placement = resolvePlacement(disp, s.pos === 'typ' ? 'typical' : s.pos === 'cen' ? 'centred' : s.pos === 'mean' ? 'mean' : { x: s.px, y: s.py });
  const mag = s.mm === 'abs' ? apparentFromAbsolute(s.Mabs, Math.max(0, s.z)) : s.mag;
  const Ftot = abToUjy(mag);
  const Fspec = Ftot * morph.rec;
  const rows = continuum(disp, exp, {
    fluxSpecUjy: Fspec,
    extraction: s.ext,
    placementMult: placement.mult,
    margin: s.marg,
    binMode: BIN_MODES[s.bin],
    binValue: s.binv,
  });
  const valid = rows.filter((r): r is ContinuumRow => r !== null);
  // wavelength of interest: requested, else the middle of the disperser's output grid
  let lam = s.lam;
  if (lam === null || lam < disp.coverage[0] || lam > disp.coverage[1]) {
    lam = disp.lam_out[Math.floor(disp.lam_out.length / 2)];
  }
  const iAt = nearestBin(disp, lam);
  const at = iAt >= 0 ? rows[iAt] : valid[0] ?? null;

  const fl = parseFloat(s.fl);
  const ll = parseFloat(s.ll);
  let lineRes: ReturnType<typeof line> | null = null;
  let lineOutside = false;
  if (fl > 0 && ll > 0) {
    const i = nearestBin(disp, ll);
    if (i >= 0 && rows[i]) lineRes = line(disp, exp, rows[i] as ContinuumRow, fl * 1e-18, s.lv || 0);
    else lineOutside = true;
  }
  const pandeiaRun = disp.pandeia?.runs.find((r) => r.noise_curve_njy) ?? null;
  return { disp, exp, morph, placement, mag, Ftot, Fspec, rows, valid, lam, at, lineRes, lineOutside, pandeiaRun, lineFlux: fl };
}

// ----------------------------------------------------------------- UI bits

const inputCls =
  'w-full px-3 py-1.5 text-sm font-mono bg-background text-text-primary placeholder:text-text-tertiary border border-border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary disabled:opacity-50';
const selectCls =
  'w-full px-3 py-1.5 text-sm border border-border-strong rounded-lg bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary disabled:opacity-50';
const labelCls = 'block text-xs font-medium text-text-secondary mb-1';

/**
 * Controlled number input that tolerates transient text ("", "2.", "-")
 * while typing: the draft is local, the parsed value is pushed up when valid,
 * and the draft is re-synced only when the prop changes to something else
 * (slider, disperser switch, URL).
 */
function NumberField({
  value,
  onChange,
  digits,
  ...rest
}: { value: number; onChange: (v: number) => void; digits?: number } & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'type'>) {
  const fmtV = (v: number) => (digits === undefined ? String(v) : v.toFixed(digits));
  const [text, setText] = useState(fmtV(value));
  useEffect(() => {
    const parsed = parseFloat(text);
    if (!(Number.isFinite(parsed) && Math.abs(parsed - value) < 1e-9)) setText(fmtV(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <input
      {...rest}
      type="number"
      className={inputCls}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v)) onChange(v);
      }}
    />
  );
}

function Field({ label, children, className = '' }: { label: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <label className={`block min-w-0 ${className}`}>
      <span className={labelCls}>{label}</span>
      {children}
    </label>
  );
}

function Fieldset({ step, title, children }: { step: number; title: string; children: React.ReactNode }) {
  return (
    <fieldset className="rounded-card border border-border bg-card p-4 min-w-0">
      <legend className="flex items-center gap-2 px-1 text-sm font-semibold text-text-primary">
        <span className="inline-grid place-items-center w-5 h-5 rounded-full bg-primary text-on-primary text-[11px] font-mono font-semibold">{step}</span>
        {title}
      </legend>
      {children}
    </fieldset>
  );
}

function Sci({ x }: { x: number }) {
  const p = sciParts(x);
  if (!p) return <>—</>;
  return (
    <>
      {p.m}×10<sup>{p.e}</sup>
    </>
  );
}

type Tone = 'good' | 'ok' | 'warn' | 'accent' | 'plain';
function snTone(v: number): Tone {
  return v >= 5 ? 'good' : v >= 3 ? 'ok' : 'warn';
}
const TONE_CLS: Record<Tone, string> = {
  good: 'text-success',
  ok: 'text-info',
  warn: 'text-warning',
  accent: 'text-primary-text',
  plain: 'text-text-primary',
};

function StatCard({ k, v, s, tone = 'plain' }: { k: React.ReactNode; v: React.ReactNode; s: React.ReactNode; tone?: Tone }) {
  return (
    <div className="bg-card p-3 min-w-0">
      <div className="text-[10px] font-mono uppercase tracking-wider text-text-tertiary">{k}</div>
      <div className={`text-2xl font-bold leading-tight tabular-nums my-0.5 break-words ${TONE_CLS[tone]}`}>{v}</div>
      <div className="text-xs text-text-secondary">{s}</div>
    </div>
  );
}

function useCopy() {
  const [msg, setMsg] = useState('');
  const copy = useCallback(async (text: string, done: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setMsg(done);
    } catch {
      setMsg('Copy blocked by the browser — select the text manually.');
    }
    window.setTimeout(() => setMsg(''), 2500);
  }, []);
  return { msg, copy };
}

// ----------------------------------------------------------------- component

export function EtcCalculator({ model }: { model: NoiseModel }) {
  const searchParams = useSearchParams();
  const [state, setState] = useState<CalcState>(() => stateFromParams(searchParams, model));
  const set = useCallback(<K extends keyof CalcState>(key: K, value: CalcState[K]) => {
    setState((prev) => ({ ...prev, [key]: value }));
  }, []);
  const num = (key: keyof CalcState) => (v: number) => set(key, v as never);

  // keep the URL in sync so a link reproduces the calculation
  useEffect(() => {
    const q = paramsFromState(state).toString();
    const url = `${window.location.pathname}${q ? `?${q}` : ''}${window.location.hash}`;
    window.history.replaceState(null, '', url);
  }, [state]);

  const r = useMemo(() => compute(model, state), [model, state]);
  const { disp, exp, at } = r;
  const { msg, copy } = useCopy();

  const binLabel =
    state.bin === 'pix' ? 'per pixel' : state.bin === 'res' ? 'per resolution element' : state.bin === 'R' ? `per R = ${state.binv} bin` : `per ${state.binv} µm bin`;
  const extLabel = state.ext === 'optimal' ? 'optimal' : '3-px';
  const Tneed = at ? timeForSnr(exp, at.snrBin, state.snt) : NaN;

  const setup = exp.readout
    ? `${exp.readout.toUpperCase()} with ${exp.ngroups} groups × ${exp.nint} integration${exp.nint! > 1 ? 's' : ''} × ${exp.nexp} exposures (t_exp = ${Math.round(exp.perExposureS)} s, ${fmtTimeH(exp.totalS)} on source)`
    : `t_exp = ${Math.round(exp.perExposureS)} s, ${fmtTimeH(exp.totalS)} on source`;
  const sourceLabel =
    state.mm === 'abs'
      ? `M_AB = ${state.Mabs.toFixed(1)} at z = ${state.z.toFixed(2)} (m_AB = ${r.mag.toFixed(1)} for a flat f_ν continuum)`
      : `m_AB = ${state.mag.toFixed(1)}`;
  const sentence = at
    ? `NIRSpec/MSA ${disp.name}, ${setup}: a source with ${sourceLabel} (${r.morph.label}, ${(r.morph.rec * 100).toFixed(0)}% of the flux in the extraction) reaches S/N = ${at.snrPix.toFixed(1)} per pixel and ${at.snrRes.toFixed(1)} per resolution element at ${at.wave.toFixed(2)} µm; 5σ continuum limit ${at.ab5Res.toFixed(1)} AB per resolution element and 5σ unresolved-line limit ${sciText(at.line5)} erg s⁻¹ cm⁻² at the same wavelength.` +
      (r.lineRes ? ` The ${r.lineFlux.toFixed(1)}×10⁻¹⁸ erg s⁻¹ cm⁻² line at ${r.lineRes.wave.toFixed(2)} µm is detected at S/N = ${r.lineRes.snr.toFixed(1)}.` : '') +
      ` Empirical estimate from the CAMPFIRE archive (model ${model.version}; ${r.placement.label}, ×${(r.placement.mult * state.marg).toFixed(2)} noise; no noise floor assumed).`
    : '';

  const permalink = () => `${window.location.origin}${window.location.pathname}?${paramsFromState(state).toString()}`;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)] gap-6 items-start">
      {/* ------------------------------------------------------------ inputs */}
      <form className="space-y-4 min-w-0" autoComplete="off" onSubmit={(e) => e.preventDefault()}>
        <Fieldset step={1} title="Disperser / filter">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5" role="radiogroup" aria-label="Disperser">
            {Object.entries(model.dispersers).map(([key, d]) => {
              const on = key === state.disp;
              return (
                <button
                  key={key}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  onClick={() => setState((p) => ({ ...p, disp: key, lam: null }))}
                  className={`text-left rounded-lg border px-2.5 py-1.5 transition-colors ${key === 'prism_clear' ? 'col-span-2 sm:col-span-4' : ''} ${
                    on ? 'border-primary bg-primary-soft' : 'border-border bg-background hover:bg-card-hover'
                  }`}
                >
                  <div className={`text-xs font-mono font-semibold ${on ? 'text-primary-text' : 'text-text-primary'}`}>{d.name}</div>
                  <div className="text-[10px] font-mono text-text-tertiary">
                    {d.coverage[0].toFixed(2)}–{d.coverage[1].toFixed(2)} µm
                  </div>
                </button>
              );
            })}
          </div>
        </Fieldset>

        <Fieldset step={2} title="Exposure setup">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <Field label="Readout pattern">
              <select className={selectCls} value={state.ro} onChange={(e) => set('ro', e.target.value)} disabled={state.direct}>
                {Object.entries(READOUT_GROUP_S).map(([k, tg]) => (
                  <option key={k} value={k}>
                    {k.toUpperCase()} ({tg.toFixed(1)} s/group){EXTRAPOLATED_READOUTS.has(k) ? ' — extrapolated' : ''}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Groups / integration">
              <NumberField min={2} max={200} step={1} value={state.ng} onChange={num('ng')} disabled={state.direct} />
            </Field>
            <Field label="Integrations / exposure">
              <NumberField min={1} max={50} step={1} value={state.ni} onChange={num('ni')} disabled={state.direct} />
            </Field>
            <Field label="Exposures (all nods × visits)">
              <NumberField min={1} max={500} step={1} value={state.ne} onChange={num('ne')} disabled={state.direct} />
            </Field>
            <Field label="Per-exposure time">
              <output className={`${inputCls} block border-dashed bg-surface-2 text-primary-text font-semibold`}>{Math.round(exp.perExposureS)} s</output>
            </Field>
            <Field label="Total on-source time">
              <output className={`${inputCls} block border-dashed bg-surface-2 text-primary-text font-semibold`}>
                {fmtTime(exp.totalS)} <span className="font-normal text-text-secondary">= {fmtHours(exp.totalS)}</span>
              </output>
            </Field>
          </div>
          <details className="mt-3" open={state.direct}>
            <summary className="text-xs text-primary-text cursor-pointer">…or enter the times directly</summary>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-2">
              <label className="flex items-center gap-2 text-xs text-text-secondary">
                <input type="checkbox" className="accent-primary" checked={state.direct} onChange={(e) => set('direct', e.target.checked)} />
                use these instead
              </label>
              <Field label="Total time T [ks]">
                <NumberField step={0.5} min={0.3} value={state.T} onChange={num('T')} disabled={!state.direct} />
              </Field>
              <Field label={<>Per-exposure time t<sub>exp</sub> [s]</>}>
                <NumberField step={50} min={100} value={state.te} onChange={num('te')} disabled={!state.direct} />
              </Field>
            </div>
          </details>
        </Fieldset>

        <Fieldset step={3} title="Source">
          <div className="flex gap-1 mb-3" role="radiogroup" aria-label="How the brightness is given">
            {(
              [
                ['app', 'apparent magnitude'],
                ['abs', 'absolute magnitude + redshift'],
              ] as const
            ).map(([v, l]) => (
              <button
                key={v}
                type="button"
                role="radio"
                aria-checked={state.mm === v}
                onClick={() => set('mm', v)}
                className={`px-2.5 py-1 rounded-lg border text-xs font-medium transition-colors ${
                  state.mm === v ? 'border-primary bg-primary-soft text-primary-text' : 'border-border bg-background text-text-secondary hover:bg-card-hover'
                }`}
              >
                {l}
              </button>
            ))}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {state.mm === 'app' ? (
              <Field label="Total continuum magnitude [AB]">
                <NumberField step={0.1} min={15} max={35} value={state.mag} onChange={num('mag')} />
              </Field>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <Field label={<>Absolute magnitude M<sub>AB</sub></>}>
                  <NumberField step={0.1} min={-30} max={-5} value={state.Mabs} onChange={num('Mabs')} />
                </Field>
                <Field label="Redshift z">
                  <NumberField step={0.1} min={0} max={20} value={state.z} onChange={num('z')} />
                </Field>
              </div>
            )}
            <Field label="Morphology">
              <select className={selectCls} value={state.size} onChange={(e) => set('size', e.target.value as CalcState['size'])}>
                <option value="point">true point source (QSO, star)</option>
                <option value="compact">compact galaxy (FWHM ≲ 2 px)</option>
                <option value="typical">typical spectroscopic target</option>
                <option value="extended">extended (FWHM ≳ 2.8 px)</option>
                <option value="custom">custom spatial FWHM…</option>
              </select>
            </Field>
            {state.size === 'custom' && (
              <Field label="Spatial FWHM [px, 0.1″]">
                <NumberField step={0.1} min={1.4} max={3.5} value={state.fwhm} onChange={num('fwhm')} />
              </Field>
            )}
          </div>
          {state.mm === 'abs' && (
            <p className="text-xs text-text-secondary mt-2">
              m<sub>AB</sub> = <b className="text-text-primary">{Number.isFinite(r.mag) ? r.mag.toFixed(2) : '—'}</b> for a flat f<sub>ν</sub> continuum (M
              <sub>AB</sub> + DM(z) − 2.5 log(1+z); flat ΛCDM, H<sub>0</sub> = {H0_KM_S_MPC}, Ω<sub>m</sub> = {OMEGA_M}).
            </p>
          )}
          <p className="text-xs text-text-secondary mt-2">
            {r.morph.label}: <b className="text-text-primary">{(r.morph.rec * 100).toFixed(0)}%</b> of the total flux lands in the {extLabel} extraction (
            {state.size === 'point' ? 'point-source pathloss correction assumed exact' : `archive median for this size, ${disp.recovery.band.toUpperCase()}-calibrated`}) →{' '}
            {(r.Fspec * 1e3).toFixed(1)} nJy in the spectrum.
          </p>
          <details className="mt-3" open={state.fl !== ''}>
            <summary className="text-xs text-primary-text cursor-pointer">Emission line (optional)</summary>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-2">
              <Field label="Line flux [10⁻¹⁸ erg s⁻¹ cm⁻²]">
                <input className={inputCls} type="number" step={0.1} min={0} value={state.fl} onChange={(e) => set('fl', e.target.value)} />
              </Field>
              <Field label="Observed wavelength [µm]">
                <input className={inputCls} type="number" step={0.01} value={state.ll} onChange={(e) => set('ll', e.target.value)} />
              </Field>
              <Field label="FWHM [km s⁻¹] (0 = unresolved)">
                <NumberField step={50} min={0} value={state.lv} onChange={num('lv')} />
              </Field>
            </div>
          </details>
        </Fieldset>

        <Fieldset step={4} title="Wavelength of interest">
          <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-3 items-end">
            <Field label="λ [µm] for the headline numbers">
              <NumberField step={0.05} min={disp.coverage[0]} max={disp.coverage[1]} value={r.lam} digits={2} onChange={num('lam')} />
            </Field>
            <Field label="slide within the disperser's coverage">
              <input
                type="range"
                className="w-full accent-primary"
                min={disp.coverage[0].toFixed(2)}
                max={disp.coverage[1].toFixed(2)}
                step={0.05}
                value={r.lam}
                onChange={(e) => set('lam', parseFloat(e.target.value))}
              />
            </Field>
          </div>
        </Fieldset>

        <details className="rounded-card border border-dashed border-border px-4 py-3">
          <summary className="text-sm font-semibold text-text-secondary cursor-pointer">
            Assumptions &amp; advanced settings <span className="font-normal text-xs text-text-tertiary">(defaults are archive medians — usually fine)</span>
          </summary>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
            <Field label="Extraction">
              <select className={selectCls} value={state.ext} onChange={(e) => set('ext', e.target.value as Extraction)}>
                <option value="optimal">optimal (default)</option>
                <option value="3px">3-pixel boxcar</option>
              </select>
            </Field>
            <Field label="Shutter placement">
              <select className={selectCls} value={state.pos} onChange={(e) => set('pos', e.target.value as PosMode)}>
                <option value="typ">typical MSA design (archive median)</option>
                <option value="cen">centred in the shutter (best case)</option>
                <option value="mean">random placement, mean</option>
                <option value="custom">custom offset…</option>
              </select>
            </Field>
            {state.pos === 'custom' && (
              <>
                <Field label="|x| offset (dispersion, shutter units)">
                  <NumberField step={0.05} min={0} max={0.5} value={state.px} onChange={num('px')} />
                </Field>
                <Field label="|y| offset (along slit, shutter units)">
                  <NumberField step={0.05} min={0} max={0.5} value={state.py} onChange={num('py')} />
                </Field>
              </>
            )}
            <Field label="Binned S/N reported per">
              <select className={selectCls} value={state.bin} onChange={(e) => set('bin', e.target.value as BinKey)}>
                <option value="res">resolution element (default)</option>
                <option value="pix">spectral pixel</option>
                <option value="R">custom resolving power R…</option>
                <option value="dl">custom Δλ [µm]…</option>
              </select>
            </Field>
            {(state.bin === 'R' || state.bin === 'dl') && (
              <Field label={state.bin === 'R' ? 'Resolving power R' : 'Bin width Δλ [µm]'}>
                <NumberField step={state.bin === 'R' ? 1 : 0.01} min={0} value={state.binv} onChange={num('binv')} />
              </Field>
            )}
            <Field label="Flux-recovery override (0–1)">
              <input className={inputCls} type="number" step={0.05} min={0.05} max={1} placeholder="from morphology" value={state.rec} onChange={(e) => set('rec', e.target.value)} />
            </Field>
            <Field label="Target S/N (for the time estimate)">
              <NumberField step={0.5} min={0.5} value={state.snt} onChange={num('snt')} />
            </Field>
            <Field label="Field-to-field margin">
              <select className={selectCls} value={String(state.marg)} onChange={(e) => set('marg', parseFloat(e.target.value))}>
                <option value="1">none (archive median)</option>
                <option value="1.1">+10% noise (conservative)</option>
                <option value="1.2">+20% noise</option>
              </select>
            </Field>
            <label className="flex items-center gap-2 text-xs text-text-secondary self-end pb-2">
              <input type="checkbox" className="accent-primary" checked={state.showetc} onChange={(e) => set('showetc', e.target.checked)} />
              show pandeia {disp.pandeia?.version ?? ''} for comparison
            </label>
          </div>
        </details>
      </form>

      {/* ------------------------------------------------------------ results */}
      <section className="min-w-0 lg:sticky lg:top-20 space-y-3" aria-live="polite">
        {at ? (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-px bg-border border border-border rounded-card overflow-hidden">
            <StatCard k={`S/N per pixel @ ${at.wave.toFixed(2)} µm`} v={at.snrPix.toFixed(at.snrPix < 10 ? 2 : 1)} s={`continuum, ${extLabel} extraction`} tone={snTone(at.snrPix)} />
            <StatCard
              k={`S/N ${binLabel}`}
              v={at.snrBin.toFixed(at.snrBin < 10 ? 2 : 1)}
              s={state.bin === 'pix' ? '1 pixel' : `${at.nBin.toFixed(1)} pixels, correlation included`}
              tone={snTone(at.snrBin)}
            />
            <StatCard
              k={`time for S/N = ${state.snt} ${binLabel}`}
              v={fmtTime(Tneed)}
              s={`${fmtHours(Tneed)}; ${Tneed > exp.totalS ? `×${(Tneed / exp.totalS).toFixed(1)} the current ${fmtTimeH(exp.totalS)}` : `reached at ${fmtTime(Tneed)} of ${fmtTimeH(exp.totalS)}`}`}
              tone="accent"
            />
            <StatCard
              k="5σ continuum limit"
              v={
                <>
                  {at.ab5Res.toFixed(2)} <small className="text-sm font-normal text-text-tertiary">AB</small>
                </>
              }
              s={`per resolution element @ ${at.wave.toFixed(2)} µm (${at.ab5Pix.toFixed(2)} per pixel)`}
            />
            <StatCard k={`5σ line limit @ ${at.wave.toFixed(2)} µm`} v={<Sci x={at.line5} />} s="erg s⁻¹ cm⁻², unresolved line" />
            {r.lineRes ? (
              <StatCard
                k={`your line @ ${r.lineRes.wave.toFixed(2)} µm`}
                v={`S/N ${r.lineRes.snr.toFixed(1)}`}
                s={
                  <>
                    FWHM {(r.lineRes.fwhmUm * 1e4).toFixed(0)} Å = {r.lineRes.windowPx.toFixed(1)} px window; 5σ limit <Sci x={r.lineRes.limit5} />
                  </>
                }
                tone={snTone(r.lineRes.snr)}
              />
            ) : r.lineOutside ? (
              <StatCard k="your line" v="—" s="outside this disperser's coverage" tone="warn" />
            ) : (
              <StatCard
                k="1σ noise per pixel"
                v={
                  <>
                    {(at.sig1d * 1e3).toFixed(1)} <small className="text-sm font-normal text-text-tertiary">nJy</small>
                  </>
                }
                s={`1-D, @ ${at.wave.toFixed(2)} µm; ${(at.sigPix * 1e3).toFixed(1)} nJy per 2-D pixel`}
              />
            )}
          </div>
        ) : (
          <div className="rounded-card border border-border bg-card p-4 text-sm text-text-secondary">The model is not defined at this wavelength.</div>
        )}

        <EtcChart
          disp={disp}
          rows={r.rows}
          at={at}
          totalS={exp.totalS}
          hasSource
          pandeia={
            state.showetc && r.pandeiaRun?.noise_curve_njy
              ? { curve: r.pandeiaRun.noise_curve_njy, totalS: r.pandeiaRun.total_s, label: r.pandeiaRun.label }
              : null
          }
        />
        <p className="text-xs text-text-tertiary">
          {state.showetc && r.pandeiaRun
            ? `Dashed: pandeia ${disp.pandeia?.version} full-shutter extraction for a centred point source (${r.pandeiaRun.label.split(' (')[0]}), rescaled to your T as T^-1/2. `
            : ''}
          Dotted: S/N = 3 and 5.
        </p>

        <div className="overflow-x-auto rounded-card border border-border">
          <table className="w-full text-xs tabular-nums">
            <caption className="text-left text-xs text-text-tertiary px-3 py-2 caption-top">
              {disp.name}, t_exp = {Math.round(exp.perExposureS)} s, T = {fmtTimeH(exp.totalS)}, {state.ext === 'optimal' ? 'optimal' : '3-px boxcar'} extraction, placement ×
              {(r.placement.mult * state.marg).toFixed(2)}
            </caption>
            <thead className="bg-table-header text-text-secondary">
              <tr>
                <th className="px-2 py-1.5 text-left font-mono font-semibold">λ [µm]</th>
                {disp.lam_out.map((l) => (
                  <th key={l} className="px-2 py-1.5 text-right font-mono font-semibold">
                    {l.toFixed(2)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(
                [
                  ['σ per 2-D pixel [nJy]', (x) => (x.sigPix * 1e3).toFixed(1)],
                  ['σ 1-D per pixel [nJy]', (x) => (x.sig1d * 1e3).toFixed(1)],
                  ['5σ continuum per pixel [AB]', (x) => x.ab5Pix.toFixed(2)],
                  ['5σ continuum per res. element [AB]', (x) => x.ab5Res.toFixed(2)],
                  ['5σ line flux [10⁻¹⁹ cgs]', (x) => (x.line5 / 1e-19).toFixed(1)],
                  ['S/N per pixel, this source', (x) => x.snrPix.toFixed(2)],
                  [`S/N ${binLabel}`, (x) => x.snrBin.toFixed(2)],
                ] as [string, (x: ContinuumRow) => string][]
              ).map(([label, f]) => (
                <tr key={label} className="hover:bg-card-hover">
                  <th className="px-2 py-1 text-left font-mono font-medium text-text-secondary whitespace-nowrap">{label}</th>
                  {disp.lam_out.map((l) => {
                    const i = nearestBin(disp, l);
                    const x = i >= 0 ? r.rows[i] : null;
                    return (
                      <td key={l} className="px-2 py-1 text-right text-text-primary">
                        {x ? f(x) : '—'}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="rounded-card border border-border bg-card p-4">
          <div className="text-[10px] font-mono uppercase tracking-wider text-text-tertiary">for the proposal text</div>
          <p className="text-sm text-text-primary my-2">{sentence}</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" size="sm" onClick={() => copy(sentence, 'Sentence copied.')}>
              <Copy className="w-3.5 h-3.5 mr-1.5" /> Copy sentence
            </Button>
            <Button type="button" size="sm" variant="secondary" onClick={() => copy(permalink(), 'Link copied.')}>
              <Link2 className="w-3.5 h-3.5 mr-1.5" /> Copy link with these settings
            </Button>
            {msg && (
              <span className="text-xs text-text-secondary inline-flex items-center gap-1">
                <Check className="w-3.5 h-3.5" /> {msg}
              </span>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function sciText(x: number): string {
  const p = sciParts(x);
  return p ? `${p.m}×10^${p.e}` : '—';
}
