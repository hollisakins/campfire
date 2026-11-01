import type { ExposureMbkg } from '@/app/api/metadata/programs/[slug]/mbkg/route';

/** The master background of each exposure x detector of one observation
 * (metadata program page, expanded observation row). */
export function MasterBackgroundSubtable({ exposures }: { exposures: ExposureMbkg[] }) {
  return (
    <div>
      <div className="mb-2">
        <span className="text-xs font-medium text-text-secondary uppercase tracking-wide">
          Master background ({exposures.length})
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border/60 text-text-secondary">
              <th className="text-left px-2 py-2 font-medium">Exposure</th>
              <th className="text-left px-2 py-2 font-medium">Detector</th>
              <th className="text-left px-2 py-2 font-medium">Grating</th>
              <th className="text-left px-2 py-2 font-medium">Source</th>
              <th className="text-left px-2 py-2 font-medium">vs. other exposures</th>
              <th className="text-right px-2 py-2 font-medium">Empty shutters</th>
              <th className="text-right px-2 py-2 font-medium">Exposures combined</th>
              <th className="text-left px-2 py-2 font-medium">Version</th>
            </tr>
          </thead>
          <tbody>
            {exposures.map((e) => {
              const combined = e.mbkg_source === 'combined' ? e.mbkg_exposures ?? [] : [];
              return (
                <tr key={`${e.exposure_root}_${e.detector}`} className="border-b border-border/30 last:border-b-0">
                  <td className="px-2 py-1.5 text-text-primary font-mono">{e.exposure_root}</td>
                  <td className="px-2 py-1.5 text-text-primary">{e.detector}</td>
                  <td className="px-2 py-1.5 text-text-secondary">{e.grating ?? '—'}</td>
                  <td className="px-2 py-1.5 text-text-primary">{e.mbkg_source ?? '—'}</td>
                  <td className="px-2 py-1.5 text-text-secondary">{e.mbkg_test ?? '—'}</td>
                  <td className="px-2 py-1.5 text-right text-text-primary tabular-nums">
                    {e.mbkg_n_empty_shutters ?? '—'}
                  </td>
                  <td
                    className="px-2 py-1.5 text-right text-text-primary tabular-nums"
                    title={combined.length > 0 ? `nod/detector: ${combined.join(', ')}` : undefined}
                  >
                    {combined.length > 0 ? combined.length : '—'}
                  </td>
                  <td className="px-2 py-1.5 text-text-secondary font-mono">{e.mbkg_version ?? '—'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
