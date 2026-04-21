'use client';

import React from 'react';
import { AlertTriangle } from 'lucide-react';
import type { ObjectDetail } from '@/lib/types';

type StalenessReason = ObjectDetail['staleness_reason'];

const REASON_COPY: Record<NonNullable<StalenessReason>, string> = {
  new_target: 'A new target was added to this object since the last inspection.',
  reprocessed: 'One or more member spectra have been reprocessed since the last inspection.',
  membership_changed: 'The set of member targets has changed since the last inspection.',
  migration_conflict: 'During the object-centric migration, multiple member targets carried conflicting secure redshifts. Please review.',
};

interface StalenessBadgeProps {
  reason: StalenessReason;
  lastInspectedAt: string | null;
  lastDataChangeAt: string | null;
  compact?: boolean;
}

export const StalenessBadge: React.FC<StalenessBadgeProps> = ({
  reason,
  lastInspectedAt,
  lastDataChangeAt,
  compact = false,
}) => {
  if (!reason || !lastInspectedAt) return null;

  if (lastDataChangeAt && new Date(lastDataChangeAt) <= new Date(lastInspectedAt)) {
    return null;
  }

  const tooltip = REASON_COPY[reason];

  if (compact) {
    return (
      <span
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300"
        title={tooltip}
      >
        <AlertTriangle className="w-3 h-3" />
        Review
      </span>
    );
  }

  return (
    <span
      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm font-medium bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300 border border-amber-200 dark:border-amber-800/50"
      title={tooltip}
    >
      <AlertTriangle className="w-4 h-4" />
      Needs Review
    </span>
  );
};
