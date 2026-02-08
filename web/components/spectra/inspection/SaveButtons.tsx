'use client';

import React from 'react';
import { Save, ArrowRight, Loader2, AlertCircle } from 'lucide-react';
import type { InspectionState } from '@/lib/hooks/useInspectionState';

interface SaveButtonsProps {
  state: InspectionState;
  canEdit: boolean;
  onSave: () => void;
  onSaveAndNext: () => void;
}

export const SaveButtons: React.FC<SaveButtonsProps> = ({ state, canEdit, onSave, onSaveAndNext }) => {
  if (!canEdit) return null;

  return (
    <div className="space-y-2">
      {state.hasChanges && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          Unsaved changes
        </p>
      )}
      {state.redshiftQuality === 0 && state.hasChanges && (
        <p className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
          <AlertCircle className="w-3 h-3" />
          Set quality to save
        </p>
      )}

      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={onSave}
          disabled={!state.hasChanges || state.saving || state.redshiftQuality === 0}
          className="inline-flex items-center justify-center gap-1 px-3 py-2
                     text-sm font-medium rounded-lg border border-border dark:border-slate-600
                     text-text-primary dark:text-slate-100 hover:bg-card dark:hover:bg-slate-700
                     transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title="Save (S)"
        >
          {state.saving ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Save className="w-4 h-4" />
          )}
          <span>Save</span>
        </button>

        <button
          onClick={onSaveAndNext}
          className="inline-flex items-center justify-center gap-1 px-3 py-2
                     text-sm font-medium rounded-lg bg-primary hover:bg-primary-hover
                     text-white transition-colors"
          title="Save & Next (→)"
        >
          <span>Save & Next</span>
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>

      <p className="text-xs text-text-secondary dark:text-slate-400 text-center">
        S = Save • → = Save & Next
      </p>
    </div>
  );
};
