'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { getSpectra } from '@/lib/actions/spectra';
import { SpectrumTarget, QUALITY_LABELS } from '@/lib/types';
import { formatDistance } from '@/lib/utils/coordinate-parser';
import { Card } from '@/components/ui/Card';

interface NearbyObjectsProps {
  ra: number;
  dec: number;
  currentTargetId: string;
  /** Additional target IDs to exclude (e.g., member targets of an object) */
  excludeTargetIds?: string[];
}

export const NearbyObjects: React.FC<NearbyObjectsProps> = ({
  ra,
  dec,
  currentTargetId,
  excludeTargetIds,
}) => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nearbyObjects, setNearbyObjects] = useState<SpectrumTarget[]>([]);

  useEffect(() => {
    const fetchNearbyObjects = async () => {
      setLoading(true);
      setError(null);

      try {
        const result = await getSpectra(
          {
            coordinate_search: {
              ra,
              dec,
              radius: 1,
              radius_unit: 'arcmin',
            },
          },
          1, // page
          10, // pageSize (limit to 10 results)
          'target_id', // sortColumn (will be overridden by distance sorting in RPC)
          'asc' // sortDirection
        );

        if (result.error) {
          setError(result.error);
        } else {
          // Filter out the current object and any excluded targets
          const excludeSet = new Set([currentTargetId, ...(excludeTargetIds || [])]);
          const filtered = result.spectra.filter(
            (obj) => !excludeSet.has(obj.target_id)
          );
          setNearbyObjects(filtered);
        }
      } catch (err) {
        setError('Failed to fetch nearby objects');
        console.error(err);
      } finally {
        setLoading(false);
      }
    };

    fetchNearbyObjects();
  }, [ra, dec, currentTargetId]);

  // Helper to get quality info
  const getQualityInfo = (quality: number) => {
    const def = QUALITY_LABELS.find((q) => q.value === quality);
    return {
      icon: def?.icon || '',
      label: def?.label || 'Unknown',
    };
  };

  if (loading) {
    return (
      <Card>
        <div className="p-8 text-center">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
          <p className="text-text-secondary dark:text-slate-400 mt-4">Finding nearby objects...</p>
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <div className="p-8 text-center">
          <p className="text-red-500 dark:text-red-400">{error}</p>
        </div>
      </Card>
    );
  }

  if (nearbyObjects.length === 0) {
    return (
      <Card>
        <div className="p-8 text-center">
          <p className="text-text-secondary dark:text-slate-400">
            No other objects found within 1 arcminute
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="p-6">
        <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100 mb-4">
          Nearby Objects
          <span className="text-sm font-normal text-text-secondary dark:text-slate-400 ml-2">
            ({nearbyObjects.length} found within 1 arcmin)
          </span>
        </h3>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border dark:border-slate-700">
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary dark:text-slate-400">
                  Target ID
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary dark:text-slate-400">
                  Distance
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary dark:text-slate-400">
                  RA
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary dark:text-slate-400">
                  Dec
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary dark:text-slate-400">
                  Redshift
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary dark:text-slate-400">
                  Quality
                </th>
              </tr>
            </thead>
            <tbody>
              {nearbyObjects.map((obj) => {
                const quality = getQualityInfo(obj.redshift_quality);
                return (
                  <tr
                    key={obj.id}
                    className="border-b border-border dark:border-slate-700 hover:bg-background-hover dark:hover:bg-slate-700 transition-colors"
                  >
                    <td className="py-3 px-4">
                      <Link
                        href={`/nirspec/targets/${encodeURIComponent(obj.target_id)}`}
                        className="text-sm font-mono text-primary hover:underline"
                      >
                        {obj.target_id}
                      </Link>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary dark:text-slate-100">
                        {obj.distance != null
                          ? formatDistance(obj.distance)
                          : 'N/A'}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary dark:text-slate-100">
                        {obj.ra.toFixed(6)}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary dark:text-slate-100">
                        {obj.dec.toFixed(6)}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary dark:text-slate-100">
                        {obj.redshift !== null ? obj.redshift.toFixed(4) : 'N/A'}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <span className="text-sm">{quality.icon}</span>
                        <span className="text-xs text-text-secondary dark:text-slate-400">
                          {quality.label}
                        </span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </Card>
  );
};
