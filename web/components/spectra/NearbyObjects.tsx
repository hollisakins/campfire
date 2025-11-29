'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { getSpectra } from '@/lib/actions/spectra';
import { SpectrumObject, QUALITY_LABELS } from '@/lib/types';
import { formatDistance } from '@/lib/utils/coordinate-parser';
import { Card } from '@/components/ui/Card';

interface NearbyObjectsProps {
  ra: number;
  dec: number;
  currentObjectId: string;
}

export const NearbyObjects: React.FC<NearbyObjectsProps> = ({
  ra,
  dec,
  currentObjectId,
}) => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nearbyObjects, setNearbyObjects] = useState<SpectrumObject[]>([]);

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
          'object_id', // sortColumn (will be overridden by distance sorting in RPC)
          'asc' // sortDirection
        );

        if (result.error) {
          setError(result.error);
        } else {
          // Filter out the current object
          const filtered = result.spectra.filter(
            (obj) => obj.object_id !== currentObjectId
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
  }, [ra, dec, currentObjectId]);

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
          <p className="text-text-secondary mt-4">Finding nearby objects...</p>
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <div className="p-8 text-center">
          <p className="text-red-500">{error}</p>
        </div>
      </Card>
    );
  }

  if (nearbyObjects.length === 0) {
    return (
      <Card>
        <div className="p-8 text-center">
          <p className="text-text-secondary">
            No other objects found within 1 arcminute
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="p-6">
        <h3 className="text-lg font-semibold text-text-primary mb-4">
          Nearby Objects
          <span className="text-sm font-normal text-text-secondary ml-2">
            ({nearbyObjects.length} found within 1 arcmin)
          </span>
        </h3>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4 text-sm font-medium text-text-secondary">
                  Object ID
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
                    className="border-b border-border hover:bg-background-hover transition-colors"
                  >
                    <td className="py-3 px-4">
                      <Link
                        href={`/spectra/${encodeURIComponent(obj.object_id)}`}
                        className="text-sm font-mono text-primary hover:underline"
                      >
                        {obj.object_id}
                      </Link>
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-sm font-mono text-text-primary">
                        {obj.distance != null
                          ? formatDistance(obj.distance)
                          : 'N/A'}
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
