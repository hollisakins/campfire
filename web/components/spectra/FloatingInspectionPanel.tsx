'use client';

import React from 'react';
import { Button } from '@/components/ui/Button';
import { ObjectListsSection } from '@/components/spectra/ObjectListsSection';
import { ConflictBanner } from '@/components/spectra/inspection/ConflictBanner';
import { useAuth } from '@/lib/contexts/AuthContext';
import { REDSHIFT_QUALITY, getQualityDef } from '@/lib/flags';
import type { InspectionState } from '@/lib/hooks/useInspectionState';
import { Save, Loader2, AlertCircle, CheckCircle } from 'lucide-react';

interface FloatingInspectionPanelProps {
  objectId: number;
  ra: number;
  dec: number;
  inspection: InspectionState;
}

export const FloatingInspectionPanel: React.FC<FloatingInspectionPanelProps> = ({
  objectId,
  ra,
  dec,
  inspection,
}) => {
  const { user, userProfile } = useAuth();
  const canEdit = user && userProfile?.can_comment;

  if (!canEdit) return null;

  const qualityDef = getQualityDef(inspection.redshiftQuality);

  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-40 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-xl shadow-xl px-5 py-3 max-w-5xl w-auto">
      {inspection.versionConflict && inspection.conflictInfo && (
        <div className="mb-2">
          <ConflictBanner
            conflict={inspection.conflictInfo}
            pendingRedshiftInspected={inspection.redshiftInspected}
            pendingRedshiftQuality={inspection.redshiftQuality}
            onDiscard={() => window.location.reload()}
          />
        </div>
      )}

      {!inspection.versionConflict && inspection.saveError && (
        <div className="mb-2 p-2 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded flex items-center gap-2 text-sm">
          <AlertCircle className="w-4 h-4 text-red-600 dark:text-red-400 flex-shrink-0" />
          <span className="text-red-800 dark:text-red-400">{inspection.saveError}</span>
        </div>
      )}

      {inspection.saveSuccess && (
        <div className="mb-2 p-2 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded flex items-center gap-2 text-sm">
          <CheckCircle className="w-4 h-4 text-green-600 dark:text-green-400 flex-shrink-0" />
          <span className="text-green-800 dark:text-green-400">Saved</span>
        </div>
      )}

      <div className="flex items-center gap-4 flex-wrap">
        <ObjectListsSection objectId={objectId} ra={ra} dec={dec} dropdownPlacement="top" />

        <div className="w-px h-7 bg-border dark:bg-slate-600 flex-shrink-0" />

        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2 text-base">
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

          <input
            type="number"
            step="0.0001"
            value={inspection.redshiftInspected}
            onChange={e => inspection.setRedshiftInspected(e.target.value)}
            placeholder="Override z"
            className="w-36 px-2.5 py-1.5 text-sm font-mono border border-border dark:border-slate-600 rounded bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
          />

          <select
            value={inspection.redshiftQuality}
            onChange={e => inspection.setRedshiftQuality(parseInt(e.target.value))}
            className="px-2.5 py-1.5 text-sm border border-border dark:border-slate-600 rounded text-gray-900 focus:outline-none focus:ring-1 focus:ring-primary"
            style={{ backgroundColor: qualityDef.color }}
          >
            {REDSHIFT_QUALITY.map(q => (
              <option key={q.value} value={q.value} className="bg-white text-gray-900">
                {q.icon} {q.label}
              </option>
            ))}
          </select>

          <div className="flex-1" />

          {inspection.hasChanges && (
            <span className="text-sm text-amber-600 dark:text-amber-400">Unsaved</span>
          )}
          {inspection.redshiftQuality === 0 && (
            <span className="text-sm text-amber-600 dark:text-amber-400 flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" /> Set quality
            </span>
          )}

          <Button
            variant="primary"
            size="sm"
            onClick={() => inspection.save()}
            disabled={!inspection.hasChanges || inspection.saving || inspection.redshiftQuality === 0}
          >
            {inspection.saving ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <>
                <Save className="w-4 h-4 mr-1" />
                Save
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
};
