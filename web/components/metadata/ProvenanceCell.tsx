'use client';

import React, { useState } from 'react';
import { Info, Layers } from 'lucide-react';
import type { ObservationProvenance } from '@/lib/actions/programs';

function formatRelative(ts: string | null): string | null {
  if (!ts) return null;
  const t = new Date(ts).getTime();
  if (isNaN(t)) return null;
  const diffMs = Date.now() - t;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '—';
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

interface ProvenanceCellProps {
  provenance: ObservationProvenance;
  /** When true, render as a compact label suitable for a table cell. */
  compact?: boolean;
}

export const ProvenanceCell: React.FC<ProvenanceCellProps> = ({ provenance, compact = true }) => {
  const [open, setOpen] = useState(false);

  const {
    reduction_version,
    crds_context,
    cfpipe_version,
    jwst_version,
    reduced_at,
    deployed_at,
    n_patches_since_full,
    last_patch_at,
  } = provenance;

  const hasFull = !!reduction_version || !!reduced_at;
  const reducedRel = formatRelative(reduced_at);

  return (
    <div className="relative inline-flex items-center gap-2">
      {hasFull ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          onBlur={() => setOpen(false)}
          className="inline-flex items-center gap-1.5 text-left hover:text-primary transition-colors"
          title="Show reduction details"
        >
          <span className={compact ? 'text-sm font-medium text-text-primary dark:text-slate-100' : 'font-semibold'}>
            {reduction_version ?? '—'}
          </span>
          {reducedRel && (
            <span className="text-xs text-text-secondary dark:text-slate-400">
              {reducedRel}
            </span>
          )}
          <Info className="w-3 h-3 text-text-secondary dark:text-slate-400 opacity-0 group-hover:opacity-100" />
        </button>
      ) : (
        <span className="text-xs text-text-secondary dark:text-slate-400 italic">no full reduction</span>
      )}

      {n_patches_since_full > 0 && (
        <span
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300"
          title={`${n_patches_since_full} patch deployment${n_patches_since_full === 1 ? '' : 's'} since last full reduction${last_patch_at ? ` (most recent ${formatRelative(last_patch_at)})` : ''}`}
        >
          <Layers className="w-3 h-3" />
          +{n_patches_since_full}
        </span>
      )}

      {open && hasFull && (
        <div
          className="absolute z-30 top-full left-0 mt-2 w-80 rounded-md border border-border dark:border-slate-700 bg-card dark:bg-slate-800 shadow-lg p-3 text-xs leading-relaxed"
          onMouseDown={(e) => e.preventDefault()}
        >
          <div className="font-semibold text-text-primary dark:text-slate-100 mb-2">
            Last full reduction
          </div>
          <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-text-secondary dark:text-slate-400">
            <dt>Version</dt>
            <dd className="font-mono text-text-primary dark:text-slate-100">{reduction_version ?? '—'}</dd>
            <dt>cfpipe</dt>
            <dd className="font-mono text-text-primary dark:text-slate-100">{cfpipe_version ?? '—'}</dd>
            <dt>jwst</dt>
            <dd className="font-mono text-text-primary dark:text-slate-100">{jwst_version ?? '—'}</dd>
            <dt>CRDS</dt>
            <dd className="font-mono text-text-primary dark:text-slate-100">{crds_context ?? '—'}</dd>
            <dt>Reduced</dt>
            <dd className="font-mono text-text-primary dark:text-slate-100">{formatTimestamp(reduced_at)}</dd>
            <dt>Deployed</dt>
            <dd className="font-mono text-text-primary dark:text-slate-100">{formatTimestamp(deployed_at)}</dd>
          </dl>
          {n_patches_since_full > 0 && (
            <div className="mt-3 pt-2 border-t border-border dark:border-slate-700">
              <div className="font-semibold text-amber-700 dark:text-amber-300 mb-1">
                {n_patches_since_full} patch{n_patches_since_full === 1 ? '' : 'es'} since
              </div>
              <p className="text-text-secondary dark:text-slate-400">
                Per-source re-reductions exist after this full reduction. Affected spectra
                may have a newer reduction version than shown above.
              </p>
              {last_patch_at && (
                <div className="mt-1 text-text-secondary dark:text-slate-400">
                  Most recent: <span className="font-mono">{formatTimestamp(last_patch_at)}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
