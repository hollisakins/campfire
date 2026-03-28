'use client';

import React, { useEffect, useState, useRef } from 'react';
import { ExternalLink } from 'lucide-react';
import { getSpectra } from '@/lib/actions/spectra';
import { SpectrumTarget, QUALITY_LABELS } from '@/lib/types';
import { formatDistance } from '@/lib/utils/coordinate-parser';

interface NearbyObjectsPreviewProps {
  ra: number;
  dec: number;
  currentTargetId: string;
  queueIds: string[];
  onNavigate: (targetId: string) => void;
}

export const NearbyObjectsPreview: React.FC<NearbyObjectsPreviewProps> = ({
  ra,
  dec,
  currentTargetId,
  queueIds,
  onNavigate,
}) => {
  const [loading, setLoading] = useState(true);
  const [nearbyObjects, setNearbyObjects] = useState<SpectrumTarget[]>([]);
  const currentIdRef = useRef(currentTargetId);

  useEffect(() => {
    currentIdRef.current = currentTargetId;

    const fetchNearby = async () => {
      setLoading(true);

      try {
        const result = await getSpectra(
          {
            coordinate_search: {
              ra,
              dec,
              radius: 0.3,
              radius_unit: 'arcsec',
            },
          },
          1,
          6,
          'target_id',
          'asc'
        );

        // Discard stale results
        if (currentIdRef.current !== currentTargetId) return;

        if (!result.error) {
          const filtered = result.spectra.filter(
            (obj) => obj.target_id !== currentTargetId
          );
          setNearbyObjects(filtered);
        } else {
          setNearbyObjects([]);
        }
      } catch {
        if (currentIdRef.current === currentTargetId) {
          setNearbyObjects([]);
        }
      } finally {
        if (currentIdRef.current === currentTargetId) {
          setLoading(false);
        }
      }
    };

    fetchNearby();
  }, [ra, dec, currentTargetId]);

  const getQualityIcon = (quality: number) => {
    return QUALITY_LABELS.find((q) => q.value === quality)?.icon || '';
  };

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
        {nearbyObjects.map((obj) => {
          const inQueue = queueIdSet.has(obj.target_id);
          const gratings = obj.spectra.map((s) => s.grating);

          return (
            <button
              key={obj.id}
              onClick={() => {
                if (inQueue) {
                  onNavigate(obj.target_id);
                } else {
                  window.open(
                    `/spectra/${encodeURIComponent(obj.target_id)}`,
                    '_blank'
                  );
                }
              }}
              className="w-full text-left px-2 py-1.5 rounded hover:bg-card dark:hover:bg-slate-800 transition-colors group"
            >
              <div className="flex items-center gap-1.5 text-xs">
                <span className="flex-shrink-0">{getQualityIcon(obj.redshift_quality)}</span>
                <span className="font-mono text-text-primary dark:text-slate-200 truncate flex-1">
                  {obj.target_id}
                </span>
                <span className="font-mono text-text-secondary dark:text-slate-500 flex-shrink-0">
                  {obj.distance != null ? formatDistance(obj.distance) : ''}
                </span>
                {!inQueue && (
                  <ExternalLink className="w-3 h-3 text-text-secondary dark:text-slate-500 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
                )}
              </div>
              <div className="flex items-center gap-1 mt-0.5 ml-5">
                {gratings.map((g) => (
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
