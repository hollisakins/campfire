'use client';

import React from 'react';
import Link from 'next/link';
import type { ObjectMemberTarget } from '@/lib/types';
import { QUALITY_LABELS, GRATINGS } from '@/lib/types';

interface MemberTargetsTableProps {
  members: ObjectMemberTarget[];
  selectedGrating: string | null;
  visibility: Record<string, boolean>;
  colors: Record<string, string>;
  onVisibilityChange: (targetId: string, visible: boolean) => void;
  onToggleAll: (visible: boolean) => void;
}

export const MemberTargetsTable: React.FC<MemberTargetsTableProps> = ({
  members,
  selectedGrating,
  visibility,
  colors,
  onVisibilityChange,
  onToggleAll,
}) => {
  const allChecked = members.every(m => visibility[m.target_id]);
  const noneChecked = members.every(m => !visibility[m.target_id]);

  return (
    <div className="border border-border dark:border-slate-700 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-gray-50 dark:bg-slate-800/50 border-b border-border dark:border-slate-700">
            <th className="w-10 px-3 py-2">
              <input
                type="checkbox"
                checked={allChecked}
                ref={(el) => { if (el) el.indeterminate = !allChecked && !noneChecked; }}
                onChange={() => onToggleAll(!allChecked)}
                className="rounded border-gray-300 dark:border-slate-600 text-accent focus:ring-accent"
              />
            </th>
            <th className="w-6 px-1 py-2" />
            <th className="px-3 py-2 text-left font-medium text-text-secondary dark:text-slate-400">Target ID</th>
            <th className="px-3 py-2 text-left font-medium text-text-secondary dark:text-slate-400">Program</th>
            <th className="px-3 py-2 text-right font-medium text-text-secondary dark:text-slate-400">Redshift</th>
            <th className="px-3 py-2 text-center font-medium text-text-secondary dark:text-slate-400">Quality</th>
            <th className="px-3 py-2 text-right font-medium text-text-secondary dark:text-slate-400">Max S/N</th>
            <th className="px-3 py-2 text-left font-medium text-text-secondary dark:text-slate-400">Gratings</th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => {
            const hasGrating = !selectedGrating || member.spectra.some(s => s.grating === selectedGrating);
            const qualityDef = QUALITY_LABELS.find(q => q.value === member.redshift_quality);
            const memberGratings = [...new Set(member.spectra.map(s => s.grating))];
            const sortedGratings = GRATINGS.filter(g => memberGratings.includes(g));

            return (
              <tr
                key={member.target_id}
                className={`border-b border-border/50 dark:border-slate-700/50 transition-opacity ${
                  !hasGrating ? 'opacity-40' : ''
                }`}
              >
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={visibility[member.target_id] ?? true}
                    disabled={!hasGrating}
                    onChange={(e) => onVisibilityChange(member.target_id, e.target.checked)}
                    className="rounded border-gray-300 dark:border-slate-600 text-accent focus:ring-accent disabled:opacity-50"
                  />
                </td>
                <td className="px-1 py-2">
                  <div
                    className="w-3 h-3 rounded-full"
                    style={{ backgroundColor: colors[member.target_id] }}
                  />
                </td>
                <td className="px-3 py-2">
                  <Link
                    href={`/spectra/${encodeURIComponent(member.target_id)}`}
                    className="font-mono text-accent hover:text-accent-hover transition-colors"
                  >
                    {member.target_id}
                  </Link>
                </td>
                <td className="px-3 py-2 text-text-primary dark:text-slate-200">
                  {member.program_name}
                </td>
                <td className="px-3 py-2 text-right font-mono text-text-primary dark:text-slate-200">
                  {member.redshift != null ? member.redshift.toFixed(4) : '—'}
                </td>
                <td className="px-3 py-2 text-center">
                  {qualityDef && (
                    <span title={qualityDef.label}>
                      {qualityDef.icon} <span className="text-xs text-text-secondary dark:text-slate-400">{qualityDef.short_label}</span>
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-right font-mono text-text-primary dark:text-slate-200">
                  {member.max_snr != null ? member.max_snr.toFixed(1) : '—'}
                </td>
                <td className="px-3 py-2">
                  <div className="flex gap-1 flex-wrap">
                    {sortedGratings.map(g => (
                      <span
                        key={g}
                        className={`text-xs px-1.5 py-0.5 rounded font-mono ${
                          selectedGrating && g === selectedGrating
                            ? 'bg-accent/20 text-accent dark:bg-accent/30'
                            : 'bg-gray-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400'
                        }`}
                      >
                        {g}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};
