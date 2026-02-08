'use client';

import React, { forwardRef, useState, useEffect } from 'react';
import { Save, ArrowRight, Loader2, AlertCircle, CheckCircle } from 'lucide-react';
import {
  REDSHIFT_QUALITY,
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
  getContrastColor,
  type FlagDef,
} from '@/lib/flags';
import type { InspectionState } from '@/lib/hooks/useInspectionState';
import { useDebounce } from '@/lib/hooks/useDebounce';

interface InspectionControlsProps {
  state: InspectionState;
  canEdit: boolean;
  onSave: () => void;
  onSaveAndNext: () => void;
}

// Flag pill button
const FlagPill: React.FC<{
  flag: FlagDef;
  active: boolean;
  disabled: boolean;
  onClick: () => void;
}> = ({ flag, active, disabled, onClick }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium transition-colors border
      ${active
        ? 'border-transparent text-gray-900'
        : 'border-border dark:border-slate-600 text-text-secondary dark:text-slate-400 bg-transparent hover:bg-card dark:hover:bg-slate-700'
      }
      disabled:opacity-50 disabled:cursor-not-allowed`}
    style={active ? { backgroundColor: flag.color, color: getContrastColor(flag.color) } : undefined}
    title={flag.description}
  >
    <span>{flag.icon}</span>
    <span>{flag.short}</span>
  </button>
);

export const InspectionControls = forwardRef<HTMLInputElement, InspectionControlsProps>(
  ({ state, canEdit, onSave, onSaveAndNext }, redshiftInputRef) => {
    // Local state for immediate display updates
    const [localRedshift, setLocalRedshift] = useState(state.redshiftInspected);
    const debouncedRedshift = useDebounce(localRedshift, 300);

    // Update state with debounced value
    useEffect(() => {
      state.setRedshiftInspected(debouncedRedshift);
    }, [debouncedRedshift, state]);

    // Sync local state when inspection state changes externally (navigation)
    useEffect(() => {
      setLocalRedshift(state.redshiftInspected);
    }, [state.redshiftInspected]);

    return (
      <div className="border-t border-border dark:border-slate-700 px-4 py-3 flex-shrink-0 bg-background dark:bg-slate-900">
        {/* Status messages */}
        {state.saveError && (
          <div className="mb-2 p-2 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded flex items-start gap-2">
            <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-red-800 dark:text-red-400">{state.saveError}</p>
          </div>
        )}
        {state.saveSuccess && (
          <div className="mb-2 p-2 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded flex items-start gap-2">
            <CheckCircle className="w-4 h-4 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-green-800 dark:text-green-400">Saved</p>
          </div>
        )}

        {/* Row 1: Redshift + Quality */}
        <div className="flex items-center gap-3 mb-2 flex-wrap">
          {/* Redshift display */}
          <div className="flex items-center gap-2 text-sm">
            <span className="text-text-secondary dark:text-slate-400">z =</span>
            {state.redshiftQuality === 1 ? (
              <span className="font-mono text-text-secondary dark:text-slate-400 line-through">
                {state.currentRedshift?.toFixed(4) ?? '\u2014'}
              </span>
            ) : (
              <span className="font-mono font-semibold text-text-primary dark:text-slate-100">
                {state.currentRedshift?.toFixed(4) ?? '\u2014'}
              </span>
            )}
            <span className="text-xs text-text-secondary dark:text-slate-400">
              ({state.redshiftInspected ? 'overridden' : 'auto-fit'})
            </span>
          </div>

          <div className="h-4 w-px bg-border dark:bg-slate-600" />

          {/* Override input */}
          <div className="flex items-center gap-1.5">
            <label className="text-xs text-text-secondary dark:text-slate-400">Override:</label>
            <input
              ref={redshiftInputRef}
              type="number"
              step="0.0001"
              value={localRedshift}
              onChange={(e) => setLocalRedshift(e.target.value)}
              placeholder="auto"
              disabled={!canEdit}
              className="w-28 px-2 py-1 text-xs font-mono border border-border dark:border-slate-600 rounded bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary disabled:opacity-60"
            />
          </div>

          <div className="h-4 w-px bg-border dark:bg-slate-600" />

          {/* Quality buttons */}
          <div className="flex flex-col gap-0.5">
            <div className="flex items-center gap-1 mb-1">
              <span className="text-xs text-text-secondary dark:text-slate-400">
                Quality<span className="text-red-500">*</span>:
              </span>
              {state.redshiftQuality === 0 && (
                <span className="text-xs text-amber-600 dark:text-amber-400">
                  (required to save)
                </span>
              )}
            </div>
            <div className="flex items-center gap-1">
              {REDSHIFT_QUALITY.filter((q) => q.value > 0).map((q) => {
                const isSelected = state.redshiftQuality === q.value;
                return (
                  <button
                    key={q.value}
                    onClick={() => canEdit && state.setRedshiftQuality(q.value)}
                    disabled={!canEdit}
                    className={`relative px-2.5 py-1 text-xs font-medium rounded transition-all
                      ${isSelected
                        ? 'ring-2 ring-offset-1 dark:ring-offset-slate-900 ring-text-primary text-gray-900'
                        : 'border border-border dark:border-slate-600 text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700'
                      }
                      disabled:opacity-50 disabled:cursor-not-allowed`}
                    style={isSelected ? { backgroundColor: q.color } : undefined}
                    title={`${q.label} - ${q.description}`}
                  >
                    <kbd className="font-mono text-[10px] opacity-60 mr-1">{q.value}</kbd>
                    {q.short}
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Row 2: Flag pills */}
        <div className="flex items-start gap-4 mb-2 flex-wrap">
          {/* Spectral Features */}
          <div className="flex items-center gap-1 flex-wrap">
            <span className="text-[10px] uppercase text-text-secondary dark:text-slate-500 mr-0.5">Features:</span>
            {SPECTRAL_FEATURES.map((f) => (
              <FlagPill
                key={f.key}
                flag={f}
                active={state.spectralFeatures.includes(f.value)}
                disabled={!canEdit}
                onClick={() => state.toggleFlag('spectralFeatures', f.value)}
              />
            ))}
          </div>

          {/* Object Type */}
          <div className="flex items-center gap-1 flex-wrap">
            <span className="text-[10px] uppercase text-text-secondary dark:text-slate-500 mr-0.5">Type:</span>
            {OBJECT_FLAGS.map((f) => (
              <FlagPill
                key={f.key}
                flag={f}
                active={state.objectFlags.includes(f.value)}
                disabled={!canEdit}
                onClick={() => state.toggleFlag('objectFlags', f.value)}
              />
            ))}
          </div>

          {/* DQ Flags */}
          <div className="flex items-center gap-1 flex-wrap">
            <span className="text-[10px] uppercase text-text-secondary dark:text-slate-500 mr-0.5">DQ:</span>
            {DQ_FLAGS.map((f) => (
              <FlagPill
                key={f.key}
                flag={f}
                active={state.dqFlags.includes(f.value)}
                disabled={!canEdit}
                onClick={() => state.toggleFlag('dqFlags', f.value)}
              />
            ))}
          </div>
        </div>

        {/* Row 3: Save buttons */}
        {canEdit && (
          <div className="flex items-center justify-end gap-2">
            {state.hasChanges && (
              <span className="text-xs text-amber-600 dark:text-amber-400">Unsaved changes</span>
            )}
            {state.redshiftQuality === 0 && (
              <span className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
                <AlertCircle className="w-3 h-3" />
                Set quality to save
              </span>
            )}
            <button
              onClick={onSave}
              disabled={!state.hasChanges || state.saving || state.redshiftQuality === 0}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-border dark:border-slate-600 text-text-primary dark:text-slate-100 hover:bg-card dark:hover:bg-slate-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title="Save (S)"
            >
              {state.saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              <span>Save</span>
              <kbd className="font-mono text-[10px] opacity-50 ml-1">S</kbd>
            </button>
            <button
              onClick={onSaveAndNext}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-primary hover:bg-primary-hover text-white transition-colors"
              title="Save & Next (→)"
            >
              <span>Save & Next</span>
              <ArrowRight className="w-3.5 h-3.5" />
              <kbd className="font-mono text-[10px] opacity-70 ml-1">\u2192</kbd>
            </button>
          </div>
        )}
      </div>
    );
  }
);

InspectionControls.displayName = 'InspectionControls';
