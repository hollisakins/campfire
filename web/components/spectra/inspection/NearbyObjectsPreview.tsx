'use client';

import React from 'react';
import { ExternalLink } from 'lucide-react';
import { useNearbyObjectsQuery } from '@/lib/hooks/useNearbyObjectsQuery';
import { QUALITY_LABELS } from '@/lib/types';
import { formatDistance } from '@/lib/utils/coordinate-parser';

interface NearbyObjectsPreviewProps {
  ra: number;
  dec: number;
  /** IAU object_id of the currently-active object — excluded from results. */
  currentObjectId: string;
  /** Object IDs (IAU names) currently in the inspection queue. */
  queueIds: string[];
  /** Called when a queue object is clicked — switches the overlay to it. */
  onNavigate: (objectId: string) => void;
}

const RADIUS_ARCSEC = 0.3;
const LIMIT = 6;

export const NearbyObjectsPreview: React.FC<NearbyObjectsPreviewProps> = ({
  ra,
  dec,
  currentObjectId,
  queueIds,
  onNavigate,
}) => {
  // GET route + cone RPC (#506); keyed on the coordinates, so switching
  // objects in the overlay never shows a stale neighbour list.
  const query = useNearbyObjectsQuery({
    ra, dec, radiusArcsec: RADIUS_ARCSEC, limit: LIMIT, exclude: currentObjectId,
  });
  const nearbyObjects = query.data?.objects ?? [];

  const getQualityIcon = (quality: number) =>
    QUALITY_LABELS.find(q => q.value === quality)?.icon || '';

  if (query.isPending) {
    return (
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
          Nearby
        </h3>
        <p className="text-xs text-text-secondary dark:text-text-tertiary mt-1">Searching...</p>
      </div>
    );
  }

  if (nearbyObjects.length === 0) return null;

  const queueIdSet = new Set(queueIds);

  return (
    <div className="px-4 py-3 border-b border-border">
      <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wide mb-2">
        Nearby{' '}
        <span className="font-normal normal-case">
          ({nearbyObjects.length} within 0.3&quot;)
        </span>
      </h3>
      <div className="space-y-0.5">
        {nearbyObjects.map(obj => {
          const objId = obj.object_id;
          const inQueue = queueIdSet.has(objId);

          return (
            <button
              key={obj.id}
              onClick={() => {
                if (inQueue) {
                  onNavigate(objId);
                } else {
                  window.open(`/nirspec/objects/${encodeURIComponent(objId)}`, '_blank');
                }
              }}
              className="w-full text-left px-2 py-1.5 rounded hover:bg-card-hover transition-colors group"
            >
              <div className="flex items-center gap-1.5 text-xs">
                <span className="flex-shrink-0">{getQualityIcon(obj.redshift_quality)}</span>
                <span className="font-mono text-text-primary truncate flex-1">
                  {objId}
                </span>
                <span className="font-mono text-text-secondary dark:text-text-tertiary flex-shrink-0">
                  {formatDistance(obj.distance)}
                </span>
                {!inQueue && (
                  <ExternalLink className="w-3 h-3 text-text-secondary dark:text-text-tertiary flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
                )}
              </div>
              <div className="flex items-center gap-1 mt-0.5 ml-5">
                {obj.gratings.map(g => (
                  <span
                    key={g}
                    className="px-1 rounded text-[10px] leading-tight bg-card dark:bg-card-hover text-text-secondary"
                  >
                    {g}
                  </span>
                ))}
                <span className="font-mono text-text-secondary dark:text-text-tertiary text-[11px] ml-auto">
                  {obj.redshift !== null ? `z=${obj.redshift.toFixed(4)}` : 'z=?'}
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
};
