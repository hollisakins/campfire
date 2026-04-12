'use client';

import React from 'react';
import { Button } from '@/components/ui/Button';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  REDSHIFT_QUALITY,
  SPECTRAL_FEATURES,
  DQ_FLAGS,
  getQualityDef,
} from '@/lib/flags';
import type { InspectionState } from '@/lib/hooks/useInspectionState';
import {
  Save,
  Loader2,
  AlertCircle,
  CheckCircle,
  ChevronRight,
} from 'lucide-react';

interface StickyInspectionBarProps {
  inspection: InspectionState;
  onSaveAndNext?: () => void;
  hasNext?: boolean;
}

export const StickyInspectionBar: React.FC<StickyInspectionBarProps> = ({
  inspection,
  onSaveAndNext,
  hasNext = false,
}) => {
  const { user, userProfile } = useAuth();
  const canEdit = user && userProfile?.can_comment;

  const spectralFeatureOptions: FilterOption[] = SPECTRAL_FEATURES.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const dqFlagOptions: FilterOption[] = DQ_FLAGS.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const qualityDef = getQualityDef(inspection.redshiftQuality);

  if (!canEdit) return null;

  return (
    <div className="sticky bottom-0 z-10 bg-card dark:bg-slate-800 border-t border-border dark:border-slate-700 px-4 py-3 -mx-4 mt-4">
      {/* Status messages */}
      {inspection.saveError && (
        <div className="mb-2 p-2 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded flex items-center gap-2 text-sm">
          <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400 flex-shrink-0" />
          <span className="text-red-800 dark:text-red-400">{inspection.saveError}</span>
        </div>
      )}
      {inspection.saveSuccess && (
        <div className="mb-2 p-2 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded flex items-center gap-2 text-sm">
          <CheckCircle className="w-4 h-4 text-green-600 dark:text-green-400 flex-shrink-0" />
          <span className="text-green-800 dark:text-green-400">
            Saved{inspection.propagatedCount > 0 && ` · ${inspection.propagatedCount} cross-match${inspection.propagatedCount !== 1 ? 'es' : ''} auto-secured`}
          </span>
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap">
        {/* Redshift display */}
        <div className="flex items-center gap-2 text-sm">
          <span className="text-text-secondary dark:text-slate-400">z:</span>
          {inspection.redshiftQuality === 1 ? (
            <span className="font-mono text-text-secondary dark:text-slate-400 line-through">
              {inspection.currentRedshift?.toFixed(4) ?? '\u2014'}
            </span>
          ) : (
            <span className="font-mono font-semibold text-text-primary dark:text-slate-100">
              {inspection.currentRedshift?.toFixed(4) ?? '\u2014'}
            </span>
          )}
        </div>

        {/* Redshift override */}
        <input
          type="number"
          step="0.0001"
          value={inspection.redshiftInspected}
          onChange={e => inspection.setRedshiftInspected(e.target.value)}
          placeholder="Override z"
          className="w-32 px-2 py-1 text-xs font-mono border border-border dark:border-slate-600 rounded bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
        />

        {/* Quality selector */}
        <select
          value={inspection.redshiftQuality}
          onChange={e => inspection.setRedshiftQuality(parseInt(e.target.value))}
          className="px-2 py-1 text-xs border border-border dark:border-slate-600 rounded text-gray-900 focus:outline-none focus:ring-1 focus:ring-primary"
          style={{ backgroundColor: qualityDef.color }}
        >
          {REDSHIFT_QUALITY.map(q => (
            <option key={q.value} value={q.value} className="bg-white text-gray-900">
              {q.icon} {q.label}
            </option>
          ))}
        </select>

        {/* Flags */}
        <FilterChip
          label="Features"
          options={spectralFeatureOptions}
          selected={inspection.spectralFeatures}
          onChange={inspection.setSpectralFeatures}
        />
        <FilterChip
          label="DQ"
          options={dqFlagOptions}
          selected={inspection.dqFlags}
          onChange={inspection.setDqFlags}
        />

        {/* Spacer */}
        <div className="flex-1" />

        {/* Warnings */}
        {inspection.hasChanges && (
          <span className="text-xs text-amber-600 dark:text-amber-400">Unsaved</span>
        )}
        {inspection.redshiftQuality === 0 && (
          <span className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
            <AlertCircle className="w-3 h-3" /> Set quality
          </span>
        )}

        {/* Save button */}
        <Button
          variant="primary"
          size="sm"
          onClick={() => inspection.save()}
          disabled={!inspection.hasChanges || inspection.saving || inspection.redshiftQuality === 0}
        >
          {inspection.saving ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <>
              <Save className="w-3.5 h-3.5 mr-1" />
              Save
            </>
          )}
        </Button>

        {/* Save & Next */}
        {onSaveAndNext && hasNext && (
          <Button
            variant="secondary"
            size="sm"
            onClick={async () => {
              const result = await inspection.save();
              if (result.success) onSaveAndNext();
            }}
            disabled={inspection.saving || inspection.redshiftQuality === 0}
          >
            Save & Next
            <ChevronRight className="w-3.5 h-3.5 ml-1" />
          </Button>
        )}
      </div>
    </div>
  );
};
