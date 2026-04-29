'use client';

import React, { useState, useEffect, useRef, useImperativeHandle, forwardRef } from 'react';
import { RotateCcw } from 'lucide-react';
import type { InspectionState } from '@/lib/hooks/useInspectionState';

interface RedshiftSectionProps {
  state: InspectionState;
  canEdit: boolean;
  redshiftAuto: number | null;
  /** Stored redshift_inspected (may have been pinned at sign-off). Used as
   *  the "Current" display fallback when the override input is empty so the
   *  panel shows the inspector's pinned value rather than a freshly
   *  reprocessed auto-fit. */
  redshiftInspected: number | null;
  redshiftInputRef?: React.RefObject<HTMLInputElement | null>;
}

export interface RedshiftSectionHandle {
  flushPendingChanges: () => void;
}

export const RedshiftSection = forwardRef<RedshiftSectionHandle, RedshiftSectionProps>(({
  state,
  canEdit,
  redshiftAuto,
  redshiftInspected,
  redshiftInputRef,
}, ref) => {
  const [localRedshift, setLocalRedshift] = useState(state.redshiftInspected);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Track whether the last change was from local input (typing) vs external (slider)
  const isLocalChangeRef = useRef(false);
  const [isSliderSync, setIsSliderSync] = useState(false);
  const sliderSyncTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const prevResetKeyRef = useRef(state.resetKey);

  // Sync inspection state back to local state on navigation (resetKey change)
  // or external updates (slider). Detects source to apply highlight.
  useEffect(() => {
    clearTimeout(debounceTimerRef.current);
    const isNavigation = state.resetKey !== prevResetKeyRef.current;
    prevResetKeyRef.current = state.resetKey;

    if (!isNavigation && !isLocalChangeRef.current && state.redshiftInspected !== '' && state.redshiftInspected !== localRedshift) {
      // External change (slider sync) — highlight the override input
      setIsSliderSync(true);
      clearTimeout(sliderSyncTimerRef.current);
      sliderSyncTimerRef.current = setTimeout(() => setIsSliderSync(false), 1500);
    } else if (isNavigation) {
      setIsSliderSync(false);
    }

    isLocalChangeRef.current = false;
    setLocalRedshift(state.redshiftInspected);
  // localRedshift is intentionally excluded — we only want to react to external state changes
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.redshiftInspected, state.resetKey]);

  const handleChange = (value: string) => {
    isLocalChangeRef.current = true;
    setIsSliderSync(false);
    setLocalRedshift(value);
    clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = setTimeout(() => {
      state.setRedshiftInspected(value);
    }, 300);
  };

  const handleReset = () => {
    isLocalChangeRef.current = true;
    setIsSliderSync(false);
    setLocalRedshift('');
    clearTimeout(debounceTimerRef.current);
    state.setRedshiftInspected('');
  };

  // Clean up timers on unmount
  useEffect(() => {
    return () => {
      clearTimeout(debounceTimerRef.current);
      clearTimeout(sliderSyncTimerRef.current);
    };
  }, []);

  // Expose method to immediately flush pending debounced changes
  useImperativeHandle(ref, () => ({
    flushPendingChanges: () => {
      clearTimeout(debounceTimerRef.current);
      if (localRedshift !== state.redshiftInspected) {
        state.setRedshiftInspected(localRedshift);
      }
    },
  }), [localRedshift, state]);

  // Calculate current redshift. When the override input is empty, prefer the
  // stored redshift_inspected (which may be a pinned auto value from sign-off)
  // so the "Current" display matches what's persisted. Mirrors the COALESCE in
  // the objects.redshift generated column.
  const currentRedshift = localRedshift
    ? parseFloat(localRedshift)
    : (redshiftInspected ?? redshiftAuto);

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
          <div className="flex items-center justify-between mb-1">
            <label className="text-xs text-text-secondary dark:text-slate-400">
              Override:
            </label>
            {localRedshift && canEdit && (
              <button
                onClick={handleReset}
                className="flex items-center gap-0.5 text-xs text-primary hover:text-primary-hover transition-colors"
                title="Reset to auto-fit redshift"
              >
                <RotateCcw className="w-3 h-3" />
                Reset
              </button>
            )}
          </div>
          <input
            ref={redshiftInputRef}
            type="number"
            step="0.0001"
            value={localRedshift}
            onChange={(e) => handleChange(e.target.value)}
            placeholder="auto"
            disabled={!canEdit}
            className={`w-full px-2 py-1.5 text-sm font-mono border rounded bg-background dark:bg-slate-700
                       text-text-primary dark:text-slate-100 focus:outline-none
                       focus:ring-1 focus:ring-primary disabled:opacity-60 transition-all duration-300
                       ${isSliderSync
                         ? 'border-amber-400 dark:border-amber-500 ring-1 ring-amber-400/50 dark:ring-amber-500/50'
                         : 'border-border dark:border-slate-600'
                       }`}
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
