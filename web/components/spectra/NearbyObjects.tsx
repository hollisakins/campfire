'use client';

import React, { useMemo, useRef } from 'react';
import Link from 'next/link';
import { useNearbyObjectsQuery } from '@/lib/hooks/useNearbyObjectsQuery';
import { useInView } from '@/lib/hooks/useInView';
import { QUALITY_LABELS } from '@/lib/types';
import { formatDistance } from '@/lib/utils/coordinate-parser';
import { Card } from '@/components/ui/Card';

interface NearbyObjectsProps {
  ra: number;
  dec: number;
  currentTargetId: string;
  /** Additional target IDs to exclude (e.g., member targets of an object) */
  excludeTargetIds?: string[];
}

const RADIUS_ARCSEC = 60; // 1 arcmin
const LIMIT = 10;

export const NearbyObjects: React.FC<NearbyObjectsProps> = ({
  ra,
  dec,
  currentTargetId,
  excludeTargetIds,
}) => {
  // This card renders last on the page; fetch only once it is about to
  // scroll into view (#499). The read is a GET route backed by a purpose-built
  // cone RPC (#506) — it used to be the 33-parameter list RPC through a
  // server action, which queued ahead of the reads above the fold.
  const containerRef = useRef<HTMLDivElement>(null);
  const inView = useInView(containerRef);

  const query = useNearbyObjectsQuery({
    ra, dec, radiusArcsec: RADIUS_ARCSEC, limit: LIMIT, exclude: currentTargetId, enabled: inView,
  });

  const nearbyObjects = useMemo(() => {
    const rows = query.data?.objects ?? [];
    if (!excludeTargetIds || excludeTargetIds.length === 0) return rows;
    const excludeSet = new Set(excludeTargetIds);
    return rows.filter((obj) => !excludeSet.has(obj.object_id));
  }, [query.data, excludeTargetIds]);

  const loading = !inView || query.isPending;
  const error = query.isError ? query.error.message : null;

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
        <div ref={containerRef} className="p-8 text-center">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
          <p className="text-text-secondary mt-4">Finding nearby objects...</p>
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <div ref={containerRef} className="p-8 text-center">
          <p className="text-red-500 dark:text-red-400">{error}</p>
        </div>
      </Card>
    );
  }

  if (nearbyObjects.length === 0) {
    return (
      <Card>
        <div ref={containerRef} className="p-8 text-center">
          <p className="text-text-secondary">
            No other objects found within 1 arcminute
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div ref={containerRef} className="p-6">
        <h3 className="text-lg font-semibold text-text-primary mb-4">
          Nearby Objects
          <span className="text-sm font-normal text-text-secondary ml-2">
            ({nearbyObjects.length} found within 1 arcmin)
          </span>
        </h3>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="bg-table-header">
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
                  Target ID
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
                  Distance
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
                  RA
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
                  Dec
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
                  Redshift
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
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
                    className="border-b border-border hover:bg-card-hover transition-colors"
                  >
                    <td className="py-3 px-4">
                      <Link
                        href={`/nirspec/objects/${encodeURIComponent(obj.object_id)}`}
                        className="text-sm font-mono text-primary hover:underline"
                      >
                        {obj.object_id}
                      </Link>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary">
                        {formatDistance(obj.distance)}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary">
                        {obj.ra.toFixed(6)}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary">
                        {obj.dec.toFixed(6)}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary">
                        {obj.redshift !== null ? obj.redshift.toFixed(4) : 'N/A'}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <span className="text-sm">{quality.icon}</span>
                        <span className="text-xs text-text-secondary">
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
