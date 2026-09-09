'use client';

import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import {
  continuum,
  disperserCaveats,
  fmtTime,
  makeExposure,
  median,
  midBin,
  type DisperserModel,
  type NoiseModel,
} from '@/lib/etc/model';

/**
 * The report that accompanies the calculator: every number in the summary
 * and ETC-comparison tables is read or computed from the model JSON, so a
 * rebuilt model updates the page without editing prose. Only the method and
 * caveats text is static.
 */
export function EtcReport({ model }: { model: NoiseModel }) {
  return (
    <Tabs defaultValue="summary" className="mt-10">
      <TabsList className="overflow-x-auto">
        <TabsTrigger value="summary">Summary</TabsTrigger>
        <TabsTrigger value="method">Method &amp; model</TabsTrigger>
        <TabsTrigger value="etc">Versus the ETC</TabsTrigger>
        <TabsTrigger value="caveats">Caveats</TabsTrigger>
      </TabsList>
      <TabsContent value="summary" className="pt-6">
        <Summary model={model} />
      </TabsContent>
      <TabsContent value="method" className="pt-6">
        <Method model={model} />
      </TabsContent>
      <TabsContent value="etc" className="pt-6">
        <VersusEtc model={model} />
      </TabsContent>
      <TabsContent value="caveats" className="pt-6">
        <Caveats model={model} />
      </TabsContent>
    </Tabs>
  );
}

// ----------------------------------------------------------------- shared bits

const h2 = 'text-xl font-semibold text-text-primary mt-8 mb-3';
const p = 'text-sm text-text-secondary leading-relaxed max-w-prose mb-3';
const th = 'px-2 py-1.5 font-mono font-semibold text-[11px] text-text-secondary whitespace-nowrap';
const td = 'px-2 py-1 text-right text-text-primary whitespace-nowrap tabular-nums';

function Table({ caption, children }: { caption: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-card border border-border my-3">
      <table className="w-full text-xs">
        <caption className="text-left text-xs text-text-tertiary px-3 py-2 caption-top">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

const fmt = (v: number | null | undefined, digits = 2) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—');
const n = (v: number) => v.toLocaleString('en-US');

// ----------------------------------------------------------------- summary

function summaryRow(d: DisperserModel) {
  const e = makeExposure({ totalS: 10000, perExposureS: 1000 });
  const rows = continuum(d, e, { extraction: 'optimal' });
  const i = midBin(d);
  const at = i >= 0 ? rows[i] : null;
  const s = d.sample;
  const rn = median(d.rn_frac);
  const per = d.pipeline_error_ratio;
  const run0 = d.pandeia?.runs[0];
  const rec = d.recovery;
  return {
    key: d.name,
    spectra: n(s.n_all),
    faint: n(s.n_std),
    obs: s.n_obs,
    progs: s.n_programs,
    T: `${(s.T_range[0] / 1000).toFixed(1)}–${Math.round(s.T_range[1] / 1000)}`,
    te: `${Math.round(s.texp_range[0])}–${Math.round(s.texp_range[1])}`,
    slope: fmt(d.slope, 2),
    rn: rn === null ? '—' : `${rn.toFixed(2)} @ ${Math.round(d.te_ref)} s${d.B_constrained ? ' †' : ''}`,
    scatter: fmt(median(Object.values(d.obs_scatter)), 3),
    placement: `${d.mult_median.toFixed(2)}${d.pos_borrowed ? ' *' : ''}`,
    recovery:
      rec.from === 'measured'
        ? `${fmt(rec.r3_median)} / ${fmt(rec.ro_median)} (${rec.band.toUpperCase()}, n=${n(rec.n_match)})`
        : `— (${rec.band.toUpperCase()}, n=${rec.n_match}; PRISM table used)`,
    pipe: `${fmt(median(per.pixel_2d))} · ${fmt(median(per.oned_3px))}/${fmt(median(per.oned_optimal))}`,
    etc: fmt(run0?.ratio_median),
    lamMid: at ? at.wave.toFixed(2) : '—',
    sig: at ? (at.sig1d * 1e3).toFixed(1) : '—',
    ab5: at ? at.ab5Pix.toFixed(2) : '—',
    line5: at ? (at.line5 / 1e-19).toFixed(1) : '—',
  };
}

function Summary({ model }: { model: NoiseModel }) {
  const ds = Object.values(model.dispersers);
  const rows = ds.map(summaryRow);
  const a = model.archive;
  const slopes = ds.map((d) => d.slope).filter((v): v is number => typeof v === 'number');
  const grat = ds.filter((d) => d.grating !== 'prism').map((d) => d.slope).filter((v): v is number => typeof v === 'number');
  const rnFracs = ds.filter((d) => d.grating !== 'prism').map((d) => median(d.rn_frac)).filter((v): v is number => v !== null);
  const mults = ds.map((d) => d.mult_median);
  const ratios = ds.map((d) => d.pandeia?.runs[0]?.ratio_median).filter((v): v is number => typeof v === 'number');
  const prismRn = median(model.dispersers.prism_clear?.rn_frac ?? []);
  const prismRec = model.dispersers.prism_clear?.recovery;
  const tiles: [string, string][] = [
    [`${ds.length}`, `disperser/filter combinations; ${n(a.n_faint_standard_fitted)} faint standard-configuration spectra fitted in ${n(a.n_observations_fitted)} observations`],
    [`T^${fmt(median(slopes), 2)}`, `median free T-slope across dispersers (PRISM ${fmt(model.dispersers.prism_clear?.slope, 2)}; gratings ${fmt(Math.min(...grat), 2)} to ${fmt(Math.max(...grat), 2)})`],
    [`${Math.round(Math.min(...rnFracs) * 100)}–${Math.round(Math.max(...rnFracs) * 100)}%`, `read-noise share of the per-pixel variance for the gratings at their typical t_exp (PRISM: ${prismRn === null ? '—' : Math.round(prismRn * 100)}%)`],
    [`×${fmt(Math.min(...mults), 2)}–${fmt(Math.max(...mults), 2)}`, 'median noise penalty from where the source sits in its shutter, by disperser'],
    [`${prismRec?.r3_median ? Math.round(prismRec.r3_median * 100) : '—'}%`, `of a typical target's total flux in the 3-px extracted PRISM spectrum (${prismRec?.ro_median ? Math.round(prismRec.ro_median * 100) : '—'}% with optimal extraction)`],
    [`${fmt(Math.min(...ratios), 1)}–${fmt(Math.max(...ratios), 1)}×`, `pandeia ${model.dispersers.prism_clear?.pandeia?.version ?? ''} full-shutter noise ÷ empirical optimal-extraction noise for a centred point source`],
  ];
  return (
    <div>
      <p className={p}>
        How the per-pixel noise of a NIRSpec MSA spectrum scales with exposure time for every disperser/filter combination, what else moves it, and the
        continuum, line and magnitude depths that follow. Built from {n(a.n_spectra)} archival CAMPFIRE spectra (model {model.version}, built {model.built}) as an
        independent check on the official ETC.
      </p>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-px bg-border border border-border rounded-card overflow-hidden my-4">
        {tiles.map(([v, l]) => (
          <div key={l} className="bg-card p-3">
            <div className="text-2xl font-bold text-text-primary tabular-nums leading-tight">{v}</div>
            <div className="text-xs text-text-secondary mt-1">{l}</div>
          </div>
        ))}
      </div>
      <div className="rounded-lg border-l-4 border-primary bg-primary-soft px-4 py-3 text-sm text-text-primary max-w-prose my-4">
        <b>The one-line takeaway.</b> For every disperser the archive noise follows σ²<sub>pix</sub>(λ) = A(λ)/T + B(λ)/(T·t<sub>exp</sub>²) with no detectable
        floor. The gratings are far more read-noise-limited than PRISM, so for them the per-exposure time matters as much as the total. The two things the
        ETC will not tell you still dominate realistic depth: only a fifth to a half of a galaxy&apos;s total flux reaches the extracted spectrum, and
        off-centre shutter placement costs another 10–40% in noise. The calculator above applies all of it.
      </div>
      <Table caption="Every disperser/filter combination in the archive: sample, fitted scaling, systematics, and 10-ks depths at the middle of the band (centred point source, optimal extraction, t_exp = 1000 s)">
        <thead className="bg-table-header">
          <tr>
            {['disperser', 'spectra', 'faint std.', 'obs', 'progs', 'T range [ks]', 't_exp range [s]', 'T slope', 'RN var. frac.', 'obs scatter [dex]', 'placement ×', 'flux in spec (3px / opt)', 'emp/pipe 2D · 1D', 'ETC / emp', 'λ_mid', 'σ_1D/px 10 ks [nJy]', '5σ/px 10 ks [AB]', '5σ line 10 ks [10⁻¹⁹]'].map((h, i) => (
              <th key={h} className={`${th} ${i === 0 ? 'text-left' : 'text-right'}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((r) => (
            <tr key={r.key} className="hover:bg-card-hover">
              <td className={`${td} text-left font-semibold`}>{r.key}</td>
              <td className={td}>{r.spectra}</td>
              <td className={td}>{r.faint}</td>
              <td className={td}>{r.obs}</td>
              <td className={td}>{r.progs}</td>
              <td className={td}>{r.T}</td>
              <td className={td}>{r.te}</td>
              <td className={td}>{r.slope}</td>
              <td className={td}>{r.rn}</td>
              <td className={td}>{r.scatter}</td>
              <td className={td}>{r.placement}</td>
              <td className={td}>{r.recovery}</td>
              <td className={td}>{r.pipe}</td>
              <td className={td}>{r.etc}</td>
              <td className={td}>{r.lamMid}</td>
              <td className={td}>{r.sig}</td>
              <td className={td}>{r.ab5}</td>
              <td className={td}>{r.line5}</td>
            </tr>
          ))}
        </tbody>
      </Table>
      <p className="text-xs text-text-tertiary max-w-[90ch]">
        * placement term borrowed from PRISM (too few faint spectra to fit). † read-noise/sky ratio B/A borrowed from the sibling disperser (rescaled by pixel
        bandwidth) because the (T, t<sub>exp</sub>) coverage in the archive cannot separate the two terms; only A(λ) is fitted. &quot;emp/pipe&quot; = empirical
        ÷ pipeline error: 2-D per pixel at the source rows · 1-D 3-px / optimal. &quot;ETC / emp&quot; = pandeia full-shutter noise ÷ empirical
        optimal-extraction noise (13 groups NRSIRS2 × 6). RN var. frac. = read-noise share of the per-pixel variance at the archive-median t<sub>exp</sub>.
      </p>
    </div>
  );
}

// ----------------------------------------------------------------- method

function Method({ model }: { model: NoiseModel }) {
  const a = model.archive;
  const ds = Object.values(model.dispersers);
  const fitted = ds.filter((d) => !d.pos_borrowed && d.grating !== 'prism');
  return (
    <div>
      <h2 className={`${h2} mt-0`}>Data and method</h2>
      <p className={p}>
        The CAMPFIRE archive holds {n(a.n_spectra)} NIRSpec/MSA <code className="font-mono">*_spec.fits</code> products ({a.reduction}). Every product carries
        the drizzled 2-D spectrum (SCI/ERR/WHT/WAVELENGTH, converted to µJy per pixel), the 1-D optimal and boxcar extractions, the spatial profile, and an{' '}
        <code className="font-mono">EXPOSURES</code> table with per-exposure nod position, exposure time, shutter state and the source position inside its
        shutter. The {ds.length} disperser/filter combinations are treated independently and identically:
      </p>
      <ul className="list-disc pl-5 text-sm text-text-secondary leading-relaxed max-w-prose space-y-1.5 mb-3">
        <li>
          <b className="text-text-primary">On-source per-pixel noise</b> in 0.1 µm bins from the three rows centred on the source, for faint sources only
          (median S/N per pixel &lt; 1.5), as a clipped variance about the bin median; the estimator&apos;s small-sample bias is calibrated by Monte Carlo and
          removed, and per-observation curves use the median of per-spectrum variances so that the minority of spectra with an emission line in a bin do not
          bias it.
        </li>
        <li>
          <b className="text-text-primary">Pipeline-reported errors</b> (2-D ERR at the source rows, 1-D <code className="font-mono">fnu_err</code> /{' '}
          <code className="font-mono">fnu_3px_err</code>) and the normalised residual SCI/ERR elsewhere.
        </li>
        <li>
          <b className="text-text-primary">Empirical 1-D noise</b> from the scatter of the extracted spectra, and adjacent-pixel correlations along dispersion
          and slit.
        </li>
        <li>
          <b className="text-text-primary">Shutter-position term</b> fitted as a quartic in the |x| (dispersion) and |y| (slit) offsets; where a disperser
          has too few faint standard spectra the PRISM term is borrowed and only its normalisation refit.
        </li>
        <li>
          <b className="text-text-primary">Source Poisson term</b> from sources with S/N 10–100 where the disperser has enough of them; otherwise scaled from
          PRISM by the pixel bandwidth.
        </li>
        <li>
          <b className="text-text-primary">Flux recovery and achieved S/N</b> from matches to total photometry in the NIRCam band nearest the middle of each
          disperser&apos;s range.
        </li>
      </ul>
      <p className={p}>
        The model is fit to per-observation noise curves for the standard configuration (3-shutter slitlet, 3 nod positions where available, no dither, no
        stuck-shutter flag, MSA only). Observations more than 0.5 dex off the model (broken reductions) are rejected. Because each program is essentially
        one (T, t<sub>exp</sub>) point, scalings are inferred <em>across</em> programs; the number of observations, programs and the T and t<sub>exp</sub>{' '}
        ranges say how well each disperser constrains them — see the caveats tab for the dispersers that borrow terms.
      </p>
      <h2 className={h2}>The noise model</h2>
      <pre className="font-mono text-sm bg-surface-2 border-l-4 border-primary rounded-r-lg px-4 py-3 overflow-x-auto my-3">
        σ²_pix(λ) = A(λ) / T + B(λ) / (T · t_exp²){'\n'}[µJy² per drizzled pixel; T = total on-source time, t_exp = per-exposure time, both in s]
      </pre>
      <p className={p}>
        The first term is background (zodiacal + thermal) photon noise; the second is read noise, which for IRS2 up-the-ramp fitting falls as t
        <sub>exp</sub>
        <sup>−3</sup> per exposure and is the same for NRSIRS2 and NRSIRS2RAPID at fixed t<sub>exp</sub>. For PRISM read noise is a 5–15% correction; for
        the medium gratings it is a third to a half of the variance at typical exposure times, and for the high-resolution gratings it dominates, so the same
        total time split into fewer, longer exposures is measurably deeper. None of the dispersers requires a noise floor within the archive&apos;s reach.
      </p>
      <h2 className={h2}>From per-pixel noise to a proposal number</h2>
      <p className={p}>
        The calculator chains: σ<sub>pix</sub>(λ; T, t<sub>exp</sub>) × placement multiplier × (1-D/2-D ratio for the extraction) → σ<sub>1D</sub> per
        spectral pixel; source flux = total flux × size-dependent recovery fraction; σ<sub>1D</sub>² += g(λ)·F<sub>spec</sub>/T (source photon noise); S/N
        per pixel = F<sub>spec</sub>/σ<sub>1D</sub>; binned S/N over n pixels uses the measured adjacent-pixel correlation, n<sub>eff</sub> = n(1 +
        2ρ(n−1)/n); line S/N integrates over ±1 FWHM (resolution and velocity width added in quadrature) with the same correlation and the line&apos;s own
        photon noise.
      </p>
      <h2 className={h2}>Where the source sits in its shutter</h2>
      <p className={p}>
        The pipeline&apos;s point-source pathloss correction rescales flux and noise together, so a source near the edge of its 0.20″ shutter in the
        dispersion direction pays for its lower throughput with a larger flux-calibrated noise. For PRISM the effect is fitted directly (median ×
        {fmt(model.dispersers.prism_clear?.mult_median)} for the archive&apos;s placements, ×2–3 at |x| ≥ 0.4); the gratings with enough faint spectra give
        consistent terms ({fitted.map((d) => `${d.name} ×${d.mult_median.toFixed(2)}`).join(', ')}), and the rest borrow PRISM&apos;s. Budget ×1.1–1.25 in
        noise unless the MSA design forces central placements.
      </p>
      <h2 className={h2}>Are the pipeline errors realistic?</h2>
      <p className={p}>
        Empirical ÷ pipeline error ratios sit between 0.8 and 1.1 for every disperser: the 2-D ERR arrays and the 1-D error columns are right to ~10–20% and,
        where they miss, err on the conservative side. A S/N quoted from a CAMPFIRE error column can be used as is.
      </p>
    </div>
  );
}

// ----------------------------------------------------------------- versus the ETC

function VersusEtc({ model }: { model: NoiseModel }) {
  const ds = Object.values(model.dispersers).filter((d) => d.pandeia?.runs.length);
  const version = ds[0]?.pandeia?.version ?? '';
  const ratios = ds.map((d) => d.pandeia!.runs[0].ratio_median).filter((v): v is number => typeof v === 'number');
  return (
    <div>
      <p className={p}>
        pandeia {version} (the engine behind the JWST ETC) was run for every disperser/filter with the same three configurations (13 groups NRSIRS2 × 6; 65
        groups NRSIRS2RAPID × 3; 19 groups NRSIRS2 × 36), MSA 1×3 slitlet, full-shutter extraction with background from the flanking shutters, a flat 10 nJy
        point source and the medium &quot;minzodi&quot; background. Its 1σ noise, taken as F/SN(λ), exceeds the empirical optimal-extraction noise for a
        centred point source by a factor {fmt(Math.min(...ratios), 1)}–{fmt(Math.max(...ratios), 1)} depending on the disperser.
      </p>
      <p className={p}>
        Much of this is strategy rather than physics — the ETC extracts the whole 0.46″ shutter (≈4.6 px along the slit, against an effective 3 for the
        optimal profile) and estimates the background from two flanking shutters, where nod-subtracted CAMPFIRE data keep all three nods on source — but the
        ETC is also too pessimistic about read noise for long IRS2 ramps where the gratings are read-noise-limited. In the other direction the ETC takes
        whatever flux you give it: quoting a galaxy by its total magnitude overstates the flux in the spectrum by 2–5×. Net, for a typical target the
        realistic S/N is 0.6–0.9× the ETC point-source value; for a compact source 1.5–2× better than the ETC; for an extended source or an edge placement
        0.3–0.4×.
      </p>
      {ds.map((d) => {
        const runs = d.pandeia!.runs;
        const bands = Object.keys(runs[0].ratio_by_band ?? {});
        return (
          <div key={d.name}>
            <h2 className={h2}>{d.name}</h2>
            <Table caption={`pandeia ${d.pandeia!.version} extracted 1σ noise ÷ empirical optimal-extraction noise (centred point source, same T and t_exp), by band`}>
              <thead className="bg-table-header">
                <tr>
                  <th className={`${th} text-left`}>ETC configuration</th>
                  <th className={`${th} text-right`}>T [s]</th>
                  <th className={`${th} text-right`}>t_exp [s]</th>
                  <th className={`${th} text-right`}>zodi</th>
                  {bands.map((b) => (
                    <th key={b} className={`${th} text-right`}>
                      {b} µm
                    </th>
                  ))}
                  <th className={`${th} text-right`}>median</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {runs.map((r) => (
                  <tr key={r.label} className="hover:bg-card-hover">
                    <td className={`${td} text-left`}>{r.label}</td>
                    <td className={td}>{Math.round(r.total_s)}</td>
                    <td className={td}>{Math.round(r.per_exposure_s)}</td>
                    <td className={td}>{r.background}</td>
                    {bands.map((b) => (
                      <td key={b} className={td}>
                        {fmt(r.ratio_by_band?.[b])}
                      </td>
                    ))}
                    <td className={`${td} font-semibold`}>{fmt(r.ratio_median)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------------------- caveats

function Caveats({ model }: { model: NoiseModel }) {
  const ds = Object.values(model.dispersers);
  const measured = ds.filter((d) => d.recovery.from === 'measured').map((d) => d.name);
  return (
    <div>
      <ul className="list-disc pl-5 text-sm text-text-secondary leading-relaxed max-w-prose space-y-2">
        <li>
          The model describes CAMPFIRE reductions ({model.archive.reduction}). Other reductions differ at the 10–20% level in per-pixel noise; the T and t
          <sub>exp</sub> scalings and the flux-recovery fractions are properties of the instrument and the sources.
        </li>
        <li>
          Exposure-time scalings are inferred across programs. Dispersers with few observations or a narrow t<sub>exp</sub> range constrain the read-noise
          term weakly; treat their B(λ) as indicative (see the per-disperser list below).
        </li>
        <li>
          Flux recovery is measured directly only where the local photometry catalogs overlap the spectroscopy ({measured.join(', ')}); the other dispersers
          use the PRISM table. It uses catalog total fluxes (aperture conventions add ~10%) and includes the pipeline&apos;s point-source pathloss
          correction, so it is the right factor to apply to a point-source ETC run.
        </li>
        <li>
          All data are 3-shutter nod-subtracted (GTO-Wide&apos;s H-grating spectra are two visits at two nod positions); master-background and fixed-slit
          strategies are not covered. Only IRS2 readout patterns appear in the archive; the calculator&apos;s NRS/NRSRAPID options assume the same read-noise
          term and are marked as extrapolations.
        </li>
        <li>
          G140M and G235M coverage extends beyond the nominal cut-offs because the archive carries CAMPFIRE&apos;s extended-wavelength reductions; those
          bins are included with their (lower) throughput.
        </li>
        <li>
          The same model, arithmetic and caveats are served by the <code className="font-mono">campfire-etc</code> MCP server and Python package in the
          repository&apos;s <code className="font-mono">etc/</code> directory; this page reads the bundled model file (version {model.version}) directly.
        </li>
      </ul>
      <h2 className={h2}>Per-disperser caveats</h2>
      <Table caption="Where a disperser borrows a term instead of fitting it">
        <thead className="bg-table-header">
          <tr>
            <th className={`${th} text-left`}>disperser</th>
            <th className={`${th} text-left`}>standard sample</th>
            <th className={`${th} text-left`}>caveats</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {ds.map((d) => {
            const c = disperserCaveats(d);
            return (
              <tr key={d.name} className="hover:bg-card-hover align-top">
                <td className={`${td} text-left font-semibold`}>{d.name}</td>
                <td className={`${td} text-left whitespace-normal`}>
                  {n(d.sample.n_std)} faint spectra ({d.std_def}), {d.sample.n_obs} obs, T {fmtTime(d.sample.T_range[0])}–{fmtTime(d.sample.T_range[1])}
                  {d.dropped_obs.length ? `; dropped: ${d.dropped_obs.join(', ')}` : ''}
                </td>
                <td className={`${td} text-left whitespace-normal`}>{c.length ? c.join('; ') : 'all terms fitted directly'}</td>
              </tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}
