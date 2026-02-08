'use client';

import React from 'react';
import { REDSHIFT_QUALITY, getContrastColor } from '@/lib/flags';
import type { InspectionState } from '@/lib/hooks/useInspectionState';

interface QualitySectionProps {
  state: InspectionState;
  canEdit: boolean;
}

export const QualitySection: React.FC<QualitySectionProps> = ({ state, canEdit }) => {
  return (
    <div className="p-4 border-b border-border dark:border-slate-700">
      <div className="flex items-center gap-1 mb-2">
        <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase">
          Quality<span className="text-red-500">*</span>
        </h3>
        {state.redshiftQuality === 0 && (
          <span className="text-xs text-amber-600 dark:text-amber-400">
            (required)
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2">
        {REDSHIFT_QUALITY.filter((q) => q.value > 0).map((q) => {
          const isSelected = state.redshiftQuality === q.value;
          return (
            <button
              key={q.value}
              onClick={() => canEdit && state.setRedshiftQuality(q.value)}
              disabled={!canEdit}
              className={`px-3 py-2 text-sm font-medium rounded transition-all
                ${isSelected
                  ? 'ring-2 ring-offset-1 dark:ring-offset-slate-900 ring-text-primary'
                  : 'border border-border dark:border-slate-600 hover:bg-card dark:hover:bg-slate-700'
                }
                disabled:opacity-50 disabled:cursor-not-allowed`}
              style={isSelected ? { backgroundColor: q.color, color: getContrastColor(q.color) } : undefined}
              title={`${q.label} - ${q.description}`}
            >
              <kbd className="font-mono text-xs opacity-60 mr-1">{q.value}</kbd>
              {q.short}
            </button>
          );
        })}
      </div>
    </div>
  );
};
