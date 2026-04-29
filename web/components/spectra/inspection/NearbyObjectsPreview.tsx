'use client';

import React, { useEffect, useState, useRef } from 'react';
import { ExternalLink } from 'lucide-react';
import { getSpectra } from '@/lib/actions/spectra';
import { SpectrumTarget, QUALITY_LABELS } from '@/lib/types';
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

export const NearbyObjectsPreview: React.FC<NearbyObjectsPreviewProps> = ({
  ra,
  dec,
  currentObjectId,
  queueIds,
  onNavigate,
}) => {
  const [loading, setLoading] = useState(true);
  const [nearbyObjects, setNearbyObjects] = useState<SpectrumTarget[]>([]);
  const currentIdRef = useRef(currentObjectId);

  useEffect(() => {
    currentIdRef.current = currentObjectId;

    const fetchNearby = async () => {
      setLoading(true);
      try {
        const result = await getSpectra(
          {
            coordinate_search: { ra, dec, radius: 0.3, radius_unit: 'arcsec' },
          },
          1,
          6,
          'object_id',
          'asc',
          'objects',
        );

        if (currentIdRef.current !== currentObjectId) return;

        if (!result.error) {
          const filtered = result.spectra.filter(obj => obj.target_id !== currentObjectId);
          setNearbyObjects(filtered);
        } else {
          setNearbyObjects([]);
        }
      } catch {
        if (currentIdRef.current === currentObjectId) setNearbyObjects([]);
      } finally {
        if (currentIdRef.current === currentObjectId) setLoading(false);
      }
    };

    fetchNearby();
  }, [ra, dec, currentObjectId]);

  const getQualityIcon = (quality: number) =>
    QUALITY_LABELS.find(q => q.value === quality)?.icon || '';

  if (loading) {
    return (
      <div className="px-4 py-3 border-b border-border dark:border-slate-700">
        <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase tracking-wide">
          Nearby
        </h3>
        <p className="text-xs text-text-secondary dark:text-slate-500 mt-1">Searching...</p>
      </div>
    );
  }

  if (nearbyObjects.length === 0) return null;

  const queueIdSet = new Set(queueIds);

  return (
    <div className="px-4 py-3 border-b border-border dark:border-slate-700">
      <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase tracking-wide mb-2">
        Nearby{' '}
        <span className="font-normal normal-case">
          ({nearbyObjects.length} within 0.3&quot;)
        </span>
      </h3>
      <div className="space-y-0.5">
        {nearbyObjects.map(obj => {
          // In objects mode the row's `target_id` field carries the IAU object_id.
          const objId = obj.target_id;
          const inQueue = queueIdSet.has(objId);
          const gratings = obj.gratings ?? obj.spectra.map(s => s.grating);

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
              className="w-full text-left px-2 py-1.5 rounded hover:bg-card dark:hover:bg-slate-800 transition-colors group"
            >
              <div className="flex items-center gap-1.5 text-xs">
                <span className="flex-shrink-0">{getQualityIcon(obj.redshift_quality)}</span>
                <span className="font-mono text-text-primary dark:text-slate-200 truncate flex-1">
                  {objId}
                </span>
                <span className="font-mono text-text-secondary dark:text-slate-500 flex-shrink-0">
                  {obj.distance != null ? formatDistance(obj.distance) : ''}
                </span>
                {!inQueue && (
                  <ExternalLink className="w-3 h-3 text-text-secondary dark:text-slate-500 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
                )}
              </div>
              <div className="flex items-center gap-1 mt-0.5 ml-5">
                {gratings.map(g => (
                  <span
                    key={g}
                    className="px-1 rounded text-[10px] leading-tight bg-card dark:bg-slate-700 text-text-secondary dark:text-slate-400"
                  >
                    {g}
                  </span>
                ))}
                <span className="font-mono text-text-secondary dark:text-slate-500 text-[11px] ml-auto">
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
