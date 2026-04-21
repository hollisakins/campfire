'use client';

import React from 'react';
import { TileThumbnail } from '../TileThumbnail';
import { RedshiftSection, type RedshiftSectionHandle } from './RedshiftSection';
import { QualitySection } from './QualitySection';
import { CommentsPreview } from './CommentsPreview';
import { NearbyObjectsPreview } from './NearbyObjectsPreview';
import { MemberSpectraTable } from './MemberSpectraTable';
import { SaveButtons } from './SaveButtons';
import type { ObjectDetail } from '@/lib/types';
import type { InspectionState } from '@/lib/hooks/useInspectionState';

interface DashboardPanelProps {
  object: ObjectDetail;
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
  object,
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
  const memberTargetIds = object.member_targets.map(m => m.id);

  return (
    <div className="flex-shrink-0 w-[320px] border-l border-border dark:border-slate-700
                    flex flex-col overflow-hidden bg-background dark:bg-slate-900">
      <div className="p-4 flex justify-center border-b border-border dark:border-slate-700 flex-shrink-0">
        <TileThumbnail
          targetId={object.object_id}
          size={560}
          displaySize={280}
          fov={3.2}
          shutters
          ra={object.ra}
          dec={object.dec}
          field={object.field}
          linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        <MemberSpectraTable object={object} />
        <RedshiftSection
          ref={redshiftSectionRef}
          state={inspectionState}
          canEdit={canEdit}
          redshiftAuto={object.redshift_auto}
          redshiftInputRef={redshiftInputRef}
        />
        <QualitySection state={inspectionState} canEdit={canEdit} />
        <NearbyObjectsPreview
          ra={object.ra}
          dec={object.dec}
          currentObjectId={object.object_id}
          queueIds={queueIds}
          onNavigate={onNavigateToObject}
        />
        <CommentsPreview
          objectDbId={object.id}
          memberTargetIds={memberTargetIds}
          commentCount={commentCount}
        />
      </div>

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
