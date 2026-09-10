'use client';

import React, { useMemo, useRef, useState } from 'react';
import { AlertTriangle, Info, Loader2 } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { useInView } from '@/lib/hooks/useInView';
import { useObjectLinesQuery, type ObjectLineFit, type ObjectLineRecord } from '@/lib/hooks/useObjectLinesQuery';
import { LINE_FLAGS, decodeBitmask, getQualityDef } from '@/lib/flags';
import { catalogLine, lineLabel, lineWave } from '@/lib/linelist';
import type { Spectrum } from '@/lib/types';

interface EmissionLinesSectionProps {
  spectra: (Spectrum & { observation?: string })[];
}

const FLUX_UNIT = 1e-18; // erg s^-1 cm^-2

const BLENDED = 2;
const BLEND = 4;
const BROAD = 64;
const RESOLVED = 2048;
const WARN = 8 | 256 | 512; // edge | fit_failed | masked

function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

/** Row classification: a total or a stand-alone line is primary; doublet
 *  components and broad components sit behind the disclosure. */
function isPrimary(name: string): boolean {
  if (name.endsWith('_broad')) return false;
  const entry = catalogLine(name);
  return !entry || entry.kind === 'doublet' || !entry.doublet;
}

const LineCell: React.FC<{ rec: ObjectLineRecord | undefined }> = ({ rec }) => {
  if (!rec) return <td className="px-2 py-1.5 text-center text-text-tertiary">·</td>;
  const flags = rec.flags ?? 0;
  const flagDefs = decodeBitmask(flags, LINE_FLAGS).map((v) => LINE_FLAGS.find((f) => f.value === v)!).filter(Boolean);
  const title = flagDefs.map((f) => `${f.label}: ${f.description}`).join('\n');
  if (rec.flux == null) {
    return (
      <td className="px-2 py-1.5 text-center" title={title}>
        <span className="text-xs text-text-secondary">
          {flags & BLENDED ? `in ${rec.blend_into ? lineLabel(rec.blend_into) : 'blend'}` : '—'}
        </span>
      </td>
    );
  }
  const snr = rec.snr;
  const detected = snr != null && snr >= 3;
  return (
    <td className="px-2 py-1.5 text-right whitespace-nowrap" title={title}>
      <span className={`font-mono text-sm ${detected ? 'text-text-primary' : 'text-text-secondary'}`}>
        {fmt(rec.flux / FLUX_UNIT)}
        <span className="text-text-tertiary"> ± {fmt(rec.flux_err != null ? rec.flux_err / FLUX_UNIT : null)}</span>
      </span>
      <span className={`ml-1.5 font-mono text-xs ${detected ? 'text-green-700 dark:text-green-400' : 'text-text-tertiary'}`}>
        {snr != null && Number.isFinite(snr) ? `${snr.toFixed(1)}σ` : ''}
      </span>
      {(flags & (RESOLVED | BLEND | BROAD | WARN)) !== 0 && (
        <span className="ml-1 inline-flex gap-0.5 align-middle">
          {flags & RESOLVED ? <span className="text-[10px] px-1 rounded bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-300" title="Doublet resolved: components carry their own fluxes">res</span> : null}
          {flags & BLEND ? <span className="text-[10px] px-1 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300" title="Includes blended companions">blend</span> : null}
          {flags & BROAD ? <span className="text-[10px] px-1 rounded bg-purple-100 dark:bg-purple-900/40 text-purple-800 dark:text-purple-300" title="A broad component was accepted (see the broad row)">broad</span> : null}
          {flags & WARN ? <span className="text-[10px] px-1 rounded bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300" title={flagDefs.filter((f) => f.value & WARN).map((f) => f.label).join(', ')}>!</span> : null}
        </span>
      )}
    </td>
  );
};

const FitHeader: React.FC<{ fit: ObjectLineFit; observation?: string; showObservation: boolean }> = ({ fit, observation, showObservation }) => {
  const q = getQualityDef(fit.z_quality);
  const stale = fit.stale_redshift || fit.stale_spectrum;
  return (
    <th className="px-2 py-1.5 text-right font-normal align-bottom">
      <div className="font-semibold text-text-primary">{fit.grating.toUpperCase()}</div>
      {showObservation && observation && <div className="text-[11px] text-text-tertiary">{observation}</div>}
      <div className="text-[11px] text-text-secondary font-mono">
        z={fit.z_used.toFixed(4)}
        <span title={q.description} className="ml-1">{q.icon}</span>
        {fit.z_source === 'auto' && <span className="ml-1 text-amber-600 dark:text-amber-400" title="Fit at the pipeline's auto redshift, not an inspected one">auto</span>}
      </div>
      {stale && (
        <div
          className="text-[11px] text-amber-700 dark:text-amber-400 inline-flex items-center gap-0.5"
          title={fit.stale_redshift
            ? 'The inspected redshift changed after this fit; re-run linefit + deploy lines'
            : 'The spectrum was re-deployed after this fit; re-run linefit + deploy lines'}
        >
          <AlertTriangle className="w-3 h-3" /> stale
        </div>
      )}
    </th>
  );
};

/**
 * Object-page section: the emission-line fluxes of every member spectrum
 * (docs/design-emission-line-fitting.md), one column per fit and one row per
 * catalog line, ordered by rest wavelength. Doublet totals and stand-alone
 * lines are the default rows; the doublet components and broad components
 * open behind a disclosure, because the totals are the quantities that mean
 * the same thing on every grating. Fetched when the section scrolls into
 * view (#499), like the nearby-objects section.
 */
export const EmissionLinesSection: React.FC<EmissionLinesSectionProps> = ({ spectra }) => {
  const ref = useRef<HTMLDivElement | null>(null);
  const inView = useInView(ref);
  const ids = useMemo(() => spectra.map((s) => s.id), [spectra]);
  const { data, isPending, error } = useObjectLinesQuery(ids, inView);
  const [showComponents, setShowComponents] = useState(false);

  const observationOf = useMemo(() => {
    const m = new Map<number, string | undefined>();
    for (const s of spectra) m.set(s.id, s.observation);
    return m;
  }, [spectra]);
  const observations = new Set(spectra.map((s) => s.observation).filter(Boolean));

  const fits = useMemo(() => {
    const list = data?.fits ?? [];
    // spectrum order as the page shows them (by grating list order, then observation)
    const order = new Map(ids.map((id, i) => [id, i]));
    return [...list].sort((a, b) => (order.get(a.spectrum_id) ?? 0) - (order.get(b.spectrum_id) ?? 0));
  }, [data, ids]);

  const { primaryRows, secondaryRows } = useMemo(() => {
    const names = new Set<string>();
    for (const f of fits) for (const n of Object.keys(f.lines)) names.add(n);
    const sorted = [...names].sort((a, b) => lineWave(a) - lineWave(b) || a.localeCompare(b));
    return {
      primaryRows: sorted.filter(isPrimary),
      secondaryRows: sorted.filter((n) => !isPrimary(n)),
    };
  }, [fits]);

  return (
    <Card className="p-4 sm:p-6" id="emission-lines">
      <div ref={ref} className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold text-text-primary">Emission Lines</h2>
          <p className="text-xs text-text-secondary mt-0.5">
            Line fluxes measured at the inspected redshift, in 10⁻¹⁸ erg s⁻¹ cm⁻², with S/N.
            Close doublets are reported as totals so they compare across gratings.
          </p>
        </div>
        {secondaryRows.length > 0 && (
          <label className="text-xs text-text-secondary inline-flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" className="rounded border-border" checked={showComponents} onChange={(e) => setShowComponents(e.target.checked)} />
            Show doublet components &amp; broad ({secondaryRows.length})
          </label>
        )}
      </div>

      {!inView || (isPending && !data) ? (
        <div className="flex items-center gap-2 text-sm text-text-secondary py-6">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading line fits…
        </div>
      ) : error ? (
        <div className="flex items-center gap-2 text-sm text-red-600 dark:text-red-400 py-6">
          <AlertTriangle className="w-4 h-4" /> {error.message}
        </div>
      ) : fits.length === 0 ? (
        <div className="flex items-start gap-2 text-sm text-text-secondary py-6">
          <Info className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>
            No line fits yet. Lines are measured after inspection: a reducer runs
            <code className="mx-1 text-xs">campfire pull</code>→<code className="mx-1 text-xs">cfpipe nirspec linefit</code>→<code className="mx-1 text-xs">campfire deploy lines</code>
            for the observation.
          </span>
        </div>
      ) : (
        <div className="overflow-x-auto mt-3">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="px-2 py-1.5 text-left font-normal text-text-secondary align-bottom">Line</th>
                <th className="px-2 py-1.5 text-right font-normal text-text-secondary align-bottom">λ<sub>rest</sub> [Å]</th>
                {fits.map((f) => (
                  <FitHeader key={f.spectrum_id} fit={f} observation={observationOf.get(f.spectrum_id)} showObservation={observations.size > 1} />
                ))}
              </tr>
            </thead>
            <tbody>
              {[...primaryRows, ...(showComponents ? secondaryRows : [])]
                .sort((a, b) => lineWave(a) - lineWave(b) || a.localeCompare(b))
                .map((name) => {
                  const secondary = !isPrimary(name);
                  const entry = catalogLine(name);
                  return (
                    <tr key={name} className={`border-b border-border/60 ${secondary ? 'text-text-secondary' : ''}`}>
                      <td className={`px-2 py-1.5 whitespace-nowrap ${secondary ? 'pl-5' : ''}`}>
                        <span className={secondary ? '' : 'text-text-primary'}>{lineLabel(name)}</span>
                        {entry?.kind === 'doublet' && <span className="ml-1 text-[10px] text-text-tertiary">total</span>}
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono text-xs text-text-tertiary">
                        {entry ? entry.wave.toFixed(entry.wave >= 10000 ? 0 : 1) : ''}
                      </td>
                      {fits.map((f) => <LineCell key={f.spectrum_id} rec={f.lines[name]} />)}
                    </tr>
                  );
                })}
            </tbody>
          </table>
          <p className="text-[11px] text-text-tertiary mt-2">
            Undetected lines carry their measured flux ± error (no upper limits are applied); σ is the flux S/N.
            Hover a cell for its fit flags. A total marked <em>res</em> was resolved into its components on that grating.
          </p>
        </div>
      )}
    </Card>
  );
};
