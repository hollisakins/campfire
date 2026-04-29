'use client';

import React from 'react';
import { AlertCircle, RotateCcw } from 'lucide-react';
import { getQualityDef } from '@/lib/flags';
import type { ConflictInfo } from '@/lib/hooks/useInspectionState';

interface ConflictBannerProps {
  conflict: ConflictInfo;
  /** Your currently-typed inspected-z (string, '' if none). */
  pendingRedshiftInspected: string;
  /** Your currently-selected quality value. */
  pendingRedshiftQuality: number;
  /** Called when the user chooses to discard their edits and accept the
   *  server state. Caller decides how to refresh — full page reload on
   *  the detail page, refetch-and-resetState in the inspection overlay. */
  onDiscard: () => void;
}

function formatWhen(iso: string | null): string {
  if (!iso) return 'just now';
  try {
    const d = new Date(iso);
    const secs = (Date.now() - d.getTime()) / 1000;
    if (secs < 60) return 'seconds ago';
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return 'recently';
  }
}

function formatRedshift(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return v.toFixed(4);
}

/** Inline banner shown when a PATCH returned 409. Names the conflicting
 *  user, shows their values next to the user's pending edits, and offers
 *  a single explicit "discard" action. No auto-merge (per design doc). */
export const ConflictBanner: React.FC<ConflictBannerProps> = ({
  conflict,
  pendingRedshiftInspected,
  pendingRedshiftQuality,
  onDiscard,
}) => {
  const who = conflict.conflictingUser ?? 'another user';
  const when = formatWhen(conflict.lastInspectedAt);

  const theirQ = getQualityDef(conflict.theirRedshiftQuality ?? 0);
  const yourQ = getQualityDef(pendingRedshiftQuality);

  const yourZ = pendingRedshiftInspected === '' ? null : parseFloat(pendingRedshiftInspected);

  return (
    <div className="p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded text-sm">
      <div className="flex items-start gap-2 mb-2">
        <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400 mt-0.5 flex-shrink-0" />
        <div className="flex-1">
          <div className="text-red-800 dark:text-red-200 font-medium">
            Saved by {who} {when}.
          </div>
          <div className="text-red-700 dark:text-red-300 text-xs mt-0.5">
            Your edits didn&apos;t apply. Review, then discard to load their values.
          </div>
        </div>
        <button
          onClick={onDiscard}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium
                     text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-900/40
                     rounded flex-shrink-0"
        >
          <RotateCcw className="w-3 h-3" />
          Discard &amp; refresh
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2 mt-2 pl-6">
        <div>
          <div className="text-xs uppercase tracking-wide text-red-700/70 dark:text-red-300/70 mb-0.5">
            Theirs (on server)
          </div>
          <div className="font-mono text-sm text-red-900 dark:text-red-100">
            z = {formatRedshift(conflict.theirRedshiftInspected)}
            <span className="mx-1 opacity-50">·</span>
            {theirQ.icon} {theirQ.label}
          </div>
        </div>
        <div>
          <div className="text-xs uppercase tracking-wide text-red-700/70 dark:text-red-300/70 mb-0.5">
            Yours (unsaved)
          </div>
          <div className="font-mono text-sm text-red-900 dark:text-red-100">
            z = {formatRedshift(yourZ)}
            <span className="mx-1 opacity-50">·</span>
            {yourQ.icon} {yourQ.label}
          </div>
        </div>
      </div>
    </div>
  );
};
