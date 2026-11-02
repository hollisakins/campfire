import type { ProgramExposure } from '@/app/api/metadata/programs/[slug]/exposures/route';
import { groupExposures, mbkgLabel } from './exposures';

/** The exposures of one observation (metadata program page, expanded observation
 * row): one row per exposure, both detectors together. */
export function ExposuresSubtable({ exposures }: { exposures: ProgramExposure[] }) {
  const rows = groupExposures(exposures);
  return (
    <div>
      <div className="mb-2">
        <span className="text-xs font-medium text-text-secondary uppercase tracking-wide">
          Exposures ({rows.length})
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border/60 text-text-secondary">
              <th className="text-left px-2 py-2 font-medium">Exposure</th>
              <th className="text-left px-2 py-2 font-medium">Date</th>
              <th className="text-left px-2 py-2 font-medium">Grating / filter</th>
              <th className="text-left px-2 py-2 font-medium">Readout</th>
              <th className="text-right px-2 py-2 font-medium">Exposure time</th>
              <th className="text-right px-2 py-2 font-medium">Nod</th>
              <th className="text-left px-2 py-2 font-medium">Master background</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.exposureRoot} className="border-b border-border/30 last:border-b-0">
                <td className="px-2 py-1.5 text-text-primary font-mono">{e.exposureRoot}</td>
                <td className="px-2 py-1.5 text-text-secondary tabular-nums">{e.date ?? '—'}</td>
                <td className="px-2 py-1.5 text-text-primary">
                  {e.grating ?? '—'}{e.filter ? ` / ${e.filter}` : ''}
                </td>
                <td
                  className="px-2 py-1.5 text-text-secondary"
                  title={e.nGroups != null && e.nIntegrations != null
                    ? `${e.nGroups} groups × ${e.nIntegrations} integrations` : undefined}
                >
                  {e.readoutPattern ?? '—'}
                  {e.nGroups != null && e.nIntegrations != null && ` · ${e.nGroups} × ${e.nIntegrations}`}
                </td>
                <td className="px-2 py-1.5 text-right text-text-primary tabular-nums">
                  {e.exposureTime != null ? `${Math.round(e.exposureTime)} s` : '—'}
                </td>
                <td className="px-2 py-1.5 text-right text-text-primary tabular-nums">
                  {e.ditherPosition != null
                    ? `${e.ditherPosition}${e.nDitherPositions != null ? ` of ${e.nDitherPositions}` : ''}`
                    : '—'}
                </td>
                <td className="px-2 py-1.5 text-text-primary">{mbkgLabel(e.mbkg)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
