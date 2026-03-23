'use client';

import React from 'react';
import { TileThumbnail } from '../TileThumbnail';
import { RedshiftSection, type RedshiftSectionHandle } from './RedshiftSection';
import { QualitySection } from './QualitySection';
import { FlagsSection } from './FlagsSection';
import { CommentsPreview } from './CommentsPreview';
import { NearbyObjectsPreview } from './NearbyObjectsPreview';
import { SaveButtons } from './SaveButtons';
import type { SpectrumObject } from '@/lib/types';
import type { InspectionState } from '@/lib/hooks/useInspectionState';

interface DashboardPanelProps {
  spectrum: SpectrumObject;
  inspectionState: InspectionState;
  canEdit: boolean;
  commentCount: number;
  queueIds: string[];
  onNavigateToObject: (objectId: string) => void;
  redshiftInputRef?: React.RefObject<HTMLInputElement | null>;
  redshiftSectionRef?: React.RefObject<RedshiftSectionHandle | null>;
  onSave: () => void;
  onSaveAndNext: () => void;
}

export const DashboardPanel: React.FC<DashboardPanelProps> = ({
  spectrum,
  inspectionState,
  canEdit,
  commentCount,
  queueIds,
  onNavigateToObject,
  redshiftInputRef,
  redshiftSectionRef,
  onSave,
  onSaveAndNext,
}) => {
  return (
    <div className="flex-shrink-0 w-[320px] border-l border-border dark:border-slate-700
                    flex flex-col overflow-hidden bg-background dark:bg-slate-900">
      {/* Tile Cutout with Shutters - fixed at top */}
      <div className="p-4 flex justify-center border-b border-border dark:border-slate-700 flex-shrink-0">
        <TileThumbnail
          objectId={spectrum.object_id}
          size={560}
          displaySize={280}
          fov={3.2}
          shutters
          ra={spectrum.ra}
          dec={spectrum.dec}
          field={spectrum.field}
          linkToMap={{ field: spectrum.field, ra: spectrum.ra, dec: spectrum.dec }}
        />
      </div>

      {/* Scrollable sections below */}
      <div className="flex-1 overflow-y-auto">
        <RedshiftSection
          ref={redshiftSectionRef}
          state={inspectionState}
          canEdit={canEdit}
          redshiftAuto={spectrum.redshift_auto}
          redshiftInputRef={redshiftInputRef}
        />
        <QualitySection state={inspectionState} canEdit={canEdit} />
        <NearbyObjectsPreview
          ra={spectrum.ra}
          dec={spectrum.dec}
          currentObjectId={spectrum.object_id}
          queueIds={queueIds}
          onNavigate={onNavigateToObject}
        />
        <FlagsSection state={inspectionState} canEdit={canEdit} />
        <CommentsPreview objectDbId={spectrum.id} commentCount={commentCount} />
      </div>

      {/* Save buttons - sticky at bottom */}
      <div className="p-4 border-t border-border dark:border-slate-700 bg-background dark:bg-slate-900 flex-shrink-0">
        <SaveButtons
          state={inspectionState}
          canEdit={canEdit}
          onSave={onSave}
          onSaveAndNext={onSaveAndNext}
        />
      </div>
    </div>
  );
};
