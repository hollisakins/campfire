'use client';

import React, { useState, useEffect, useImperativeHandle, forwardRef } from 'react';
import type { InspectionState } from '@/lib/hooks/useInspectionState';
import { useDebounce } from '@/lib/hooks/useDebounce';

interface RedshiftSectionProps {
  state: InspectionState;
  canEdit: boolean;
  redshiftAuto: number | null;
  redshiftInputRef?: React.RefObject<HTMLInputElement | null>;
}

export interface RedshiftSectionHandle {
  flushPendingChanges: () => void;
}

export const RedshiftSection = forwardRef<RedshiftSectionHandle, RedshiftSectionProps>(({
  state,
  canEdit,
  redshiftAuto,
  redshiftInputRef,
}, ref) => {
  const [localRedshift, setLocalRedshift] = useState(state.redshiftInspected);
  const debouncedRedshift = useDebounce(localRedshift, 300);

  // Sync debounced value to inspection state.
  // Use stable setter ref to avoid re-firing on every render
  // (state object is a new reference each render).
  const { setRedshiftInspected } = state;
  useEffect(() => {
    setRedshiftInspected(debouncedRedshift);
  }, [debouncedRedshift, setRedshiftInspected]);

  // Sync inspection state back to local state on external changes (e.g. navigation).
  // resetKey ensures this fires even when the value is batched back to the same string.
  useEffect(() => {
    setLocalRedshift(state.redshiftInspected);
  }, [state.redshiftInspected, state.resetKey]);

  // Expose method to immediately flush pending debounced changes
  useImperativeHandle(ref, () => ({
    flushPendingChanges: () => {
      if (localRedshift !== state.redshiftInspected) {
        console.log('[RedshiftSection] Flushing pending redshift:', localRedshift);
        state.setRedshiftInspected(localRedshift);
      }
    },
  }), [localRedshift, state]);

  // Calculate current redshift
  const currentRedshift = localRedshift ? parseFloat(localRedshift) : redshiftAuto;

  return (
    <div className="p-4 border-b border-border dark:border-slate-700">
      <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase mb-2">
        Redshift
      </h3>

      {/* Current redshift - bold and prominent */}
      <div className="mb-3 text-center">
        <div className="text-xs text-text-secondary dark:text-slate-400 mb-1">Current</div>
        <div className="text-2xl font-bold font-mono text-text-primary dark:text-slate-100">
          {currentRedshift?.toFixed(4) || '—'}
        </div>
        {localRedshift && (
          <div className="text-xs text-text-secondary dark:text-slate-400 mt-0.5">
            (overridden)
          </div>
        )}
      </div>

      {/* Two column layout for auto/override */}
      <div className="grid grid-cols-2 gap-2 text-sm">
        {/* Auto column */}
        <div>
          <label className="text-xs text-text-secondary dark:text-slate-400 block mb-1">
            Auto:
          </label>
          <div className="px-2 py-1.5 font-mono text-sm bg-card dark:bg-slate-800 rounded border border-border dark:border-slate-600 text-text-primary dark:text-slate-100">
            {redshiftAuto?.toFixed(4) || '—'}
          </div>
        </div>

        {/* Override column */}
        <div>
          <label className="text-xs text-text-secondary dark:text-slate-400 block mb-1">
            Override:
          </label>
          <input
            ref={redshiftInputRef}
            type="number"
            step="0.0001"
            value={localRedshift}
            onChange={(e) => setLocalRedshift(e.target.value)}
            placeholder="auto"
            disabled={!canEdit}
            className="w-full px-2 py-1.5 text-sm font-mono border border-border
                       dark:border-slate-600 rounded bg-background dark:bg-slate-700
                       text-text-primary dark:text-slate-100 focus:outline-none
                       focus:ring-1 focus:ring-primary disabled:opacity-60"
            title="Press Z to focus"
          />
        </div>
      </div>

      <p className="text-xs text-text-secondary dark:text-slate-400 mt-2 text-center">
        Press Z to focus override
      </p>
    </div>
  );
});

RedshiftSection.displayName = 'RedshiftSection';
