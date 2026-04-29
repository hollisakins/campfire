'use client';

import React from 'react';
import { DQ_FLAGS } from '@/lib/flags';
import type { ObjectDetail } from '@/lib/types';

interface MemberSpectraTableProps {
  object: ObjectDetail;
}

/**
 * Compact summary table for the inspection dashboard right panel.
 * One row per member spectrum (across all member targets) showing
 * grating, observation, z_auto, SNR, DQ. Read-only — DQ editing happens
 * on the object detail page's spectrum cards.
 */
export const MemberSpectraTable: React.FC<MemberSpectraTableProps> = ({ object }) => {
  const rows = object.member_targets.flatMap(m =>
    m.spectra.map(s => ({
      key: `${m.target_id}::${s.grating}`,
      grating: s.grating,
      observation: m.observation,
      programSlug: m.program_slug,
      snr: s.signal_to_noise,
      zAuto: s.redshift_auto,
      dqMask: s.dq_flags ?? 0,
      isSelected: object.redshift_auto != null && s.redshift_auto != null
        && Math.abs(s.redshift_auto - object.redshift_auto) < 1e-6,
    }))
  );

  if (rows.length === 0) return null;

  return (
    <div className="px-4 py-3 border-b border-border dark:border-slate-700">
      <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase mb-2">
        Member spectra ({rows.length})
      </h3>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-text-secondary dark:text-slate-500">
              <th className="text-left font-medium pb-1 pr-2">Grating</th>
              <th className="text-left font-medium pb-1 pr-2">Obs</th>
              <th className="text-right font-medium pb-1 pr-2">z_auto</th>
              <th className="text-right font-medium pb-1 pr-2">S/N</th>
              <th className="text-left font-medium pb-1">DQ</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => {
              const dqDefs = DQ_FLAGS.filter(f => (r.dqMask & f.value) !== 0);
              return (
                <tr key={r.key} className="border-t border-border/30 dark:border-slate-700/40">
                  <td className="py-1 pr-2 font-mono text-text-primary dark:text-slate-200">
                    {r.grating}
                    {r.isSelected && (
                      <span className="ml-1 text-emerald-600 dark:text-emerald-400" title="Drives objects.redshift_auto">
                        ★
                      </span>
                    )}
                  </td>
                  <td className="py-1 pr-2 text-text-secondary dark:text-slate-400 truncate max-w-[80px]" title={`${r.programSlug} · ${r.observation}`}>
                    {r.observation}
                  </td>
                  <td className="py-1 pr-2 text-right font-mono text-text-primary dark:text-slate-200">
                    {r.zAuto != null ? r.zAuto.toFixed(4) : '—'}
                  </td>
                  <td className="py-1 pr-2 text-right font-mono text-text-primary dark:text-slate-200">
                    {r.snr != null ? r.snr.toFixed(1) : '—'}
                  </td>
                  <td className="py-1">
                    {dqDefs.length === 0 ? (
                      <span className="text-text-secondary dark:text-slate-500">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-0.5">
                        {dqDefs.map(f => (
                          <span
                            key={f.key}
                            className="inline-block px-1 rounded text-[9px] font-medium"
                            style={{ backgroundColor: f.color, color: '#1a1a1a' }}
                            title={f.label}
                          >
                            {f.short}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};
