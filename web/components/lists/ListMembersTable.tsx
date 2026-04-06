'use client';

import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { REDSHIFT_QUALITY } from '@/lib/flags';
import type { ObjectListMemberWithObject } from '@/lib/types';

interface ListMembersTableProps {
  members: ObjectListMemberWithObject[];
  totalMembers: number;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
}

function formatCoord(val: number, decimals: number = 6): string {
  return val.toFixed(decimals);
}

function QualityBadge({ quality }: { quality: number }) {
  const def = REDSHIFT_QUALITY.find(q => q.value === quality);
  if (!def || quality === 0) return <span className="text-text-secondary dark:text-slate-500">—</span>;
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium"
      style={{ backgroundColor: def.color + '20', color: def.color }}
    >
      {def.icon && <span>{def.icon}</span>}
      {def.label}
    </span>
  );
}

export function ListMembersTable({ members, totalMembers, page, pageSize, onPageChange }: ListMembersTableProps) {
  const totalPages = Math.ceil(totalMembers / pageSize);

  if (members.length === 0 && page === 1) {
    return (
      <Card className="p-8 text-center">
        <p className="text-text-secondary dark:text-slate-400">
          No objects in this list yet.
        </p>
      </Card>
    );
  }

  return (
    <div>
      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border dark:border-slate-700 bg-card dark:bg-slate-800/50">
                <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Object ID</th>
                <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Field</th>
                <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">RA</th>
                <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Dec</th>
                <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Redshift</th>
                <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Quality</th>
                <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Spectra</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member) => {
                const obj = member.object;
                const hasObject = obj !== null;

                return (
                  <tr
                    key={member.id}
                    className="border-b border-border dark:border-slate-700/50 last:border-0 hover:bg-card-hover dark:hover:bg-slate-800/30 transition-colors"
                  >
                    <td className="px-4 py-3">
                      {hasObject ? (
                        <Link
                          href={`/nirspec/objects/${obj.id}`}
                          className="text-primary hover:underline font-mono text-xs"
                        >
                          {obj.object_id}
                        </Link>
                      ) : (
                        <span className="text-xs text-text-secondary dark:text-slate-500 italic">
                          Not yet matched
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-text-primary dark:text-slate-200">
                      {hasObject ? obj.field : '—'}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs text-text-primary dark:text-slate-200">
                      {formatCoord(hasObject ? obj.ra : member.ra)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs text-text-primary dark:text-slate-200">
                      {formatCoord(hasObject ? obj.dec : member.dec)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs text-text-primary dark:text-slate-200">
                      {hasObject && obj.best_redshift != null ? obj.best_redshift.toFixed(4) : '—'}
                    </td>
                    <td className="px-4 py-3">
                      {hasObject ? <QualityBadge quality={obj.best_redshift_quality} /> : '—'}
                    </td>
                    <td className="px-4 py-3 text-right text-text-primary dark:text-slate-200">
                      {hasObject ? obj.n_spectra : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-4 px-1">
          <p className="text-sm text-text-secondary dark:text-slate-400">
            Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, totalMembers)} of {totalMembers}
          </p>
          <div className="flex items-center gap-1">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              className="px-3 py-1.5 text-sm border border-border dark:border-slate-700 rounded-md hover:bg-card-hover dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Previous
            </button>
            <span className="px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              className="px-3 py-1.5 text-sm border border-border dark:border-slate-700 rounded-md hover:bg-card-hover dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
