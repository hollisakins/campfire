'use client';

import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle } from 'lucide-react';

// P3 (docs/design-needs-review.md): one rule, one reason. The server
// maintains objects.needs_review — true iff a quality 1-3 object's published
// member spectra differ from the set the inspector saw at sign-off — and
// "Mark as reviewed" is a sign-off with no value change, which re-stamps the
// reviewed set and clears the flag.
const REASON_COPY =
  'This object has new or removed spectra since its last inspection, and its redshift is not secure. Take another look.';

interface StalenessBadgeProps {
  /** objects.needs_review as served; nothing renders when false. */
  needsReview: boolean;
  compact?: boolean;
  /** When present alongside `version`, enables the click-to-open popover with a
   *  "Mark as reviewed" action. Omit (or pass `compact`) for a static badge. */
  objectId?: number;
  version?: number;
  /** Current user has can_inspect — controls whether the action button is shown. */
  canMarkReviewed?: boolean;
  /** Fired after a successful PATCH so the parent can update local state and
   *  hide the badge without a full refetch. */
  onReviewed?: (next: { last_inspected_at: string; version: number }) => void;
}

export const StalenessBadge: React.FC<StalenessBadgeProps> = ({
  needsReview,
  compact = false,
  objectId,
  version,
  canMarkReviewed = false,
  onReviewed,
}) => {
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [open]);

  if (!needsReview) return null;

  const tooltip = REASON_COPY;
  const interactive = !compact && typeof objectId === 'number' && typeof version === 'number';

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

  if (!interactive) {
    return (
      <span
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm font-medium bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300 border border-amber-200 dark:border-amber-800/50"
        title={tooltip}
      >
        <AlertTriangle className="w-4 h-4" />
        Needs Review
      </span>
    );
  }

  const handleMarkReviewed = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`/api/objects/${objectId}/inspect`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_version: version }),
      });
      const data = await res.json();
      if (res.status === 409) {
        setError('Another user changed this object — refresh to see the latest.');
        return;
      }
      if (!res.ok) {
        setError(data?.error || 'Failed to mark as reviewed.');
        return;
      }
      const updated = data?.object;
      const next = {
        last_inspected_at: updated?.last_inspected_at ?? new Date().toISOString(),
        version: updated?.version ?? version,
      };
      onReviewed?.(next);
      setOpen(false);
    } catch {
      setError('Network error. Try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div ref={containerRef} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm font-medium bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300 border border-amber-200 dark:border-amber-800/50 hover:bg-amber-200 dark:hover:bg-amber-900/60 transition-colors"
      >
        <AlertTriangle className="w-4 h-4" />
        Needs Review
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1.5 z-50 w-72 p-3 bg-background dark:bg-card border border-border rounded-lg shadow-lg">
          <p className="text-sm text-text-primary mb-3">{tooltip}</p>
          {error && (
            <p className="text-xs text-red-600 dark:text-red-400 mb-2">{error}</p>
          )}
          {canMarkReviewed ? (
            <button
              type="button"
              onClick={handleMarkReviewed}
              disabled={submitting}
              className="w-full px-3 py-1.5 text-sm font-medium rounded-md bg-primary text-on-primary hover:bg-primary-hover disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
            >
              {submitting ? 'Marking…' : 'Mark as reviewed'}
            </button>
          ) : (
            <p className="text-xs text-text-secondary">
              You do not have permission to mark objects as reviewed.
            </p>
          )}
        </div>
      )}
    </div>
  );
};
