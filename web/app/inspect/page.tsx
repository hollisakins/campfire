'use client';

import React, { useEffect, useState, useRef, Suspense } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { Loader2 } from 'lucide-react';
import { InspectionModeOverlay } from '@/components/spectra/inspection/InspectionModeOverlay';
import { useAuth } from '@/lib/contexts/AuthContext';
import { getSpectrumById } from '@/lib/actions/spectra';
import { getMapLayers, getNearbyShutters } from '@/lib/actions/map';
import { parseFiltersFromURL, parseSortingFromURL } from '@/lib/utils/url-params';
import type { SpectrumObject } from '@/lib/types';
import type { MapLayer, Shutter } from '@/lib/actions/map';

function InspectPageInner() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  const startId = searchParams.get('start');

  // Parse filter/sort params
  const urlParams = new URLSearchParams(searchParams.toString());
  urlParams.delete('start');
  const filterStr = urlParams.toString();
  const filters = parseFiltersFromURL(urlParams);
  const { sortColumn, sortDirection } = parseSortingFromURL(urlParams);

  // Data state
  const [spectrum, setSpectrum] = useState<SpectrumObject | null>(null);
  const [mapLayer, setMapLayer] = useState<MapLayer | null>(null);
  const [nearbyShutters, setNearbyShutters] = useState<Shutter[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchedRef = useRef(false);

  // Redirect if no start param
  useEffect(() => {
    if (!startId) {
      router.replace('/spectra');
    }
  }, [startId, router]);

  // Redirect if not authenticated (after auth loads)
  useEffect(() => {
    if (!authLoading && !user) {
      router.replace('/login');
    }
  }, [authLoading, user, router]);

  // Fetch initial data
  useEffect(() => {
    if (!startId || authLoading || !user || fetchedRef.current) return;
    fetchedRef.current = true;

    (async () => {
      try {
        const result = await getSpectrumById(startId);
        if (!result.spectrum) {
          setError('Object not found');
          setLoading(false);
          return;
        }

        const spec = result.spectrum;
        setSpectrum(spec);

        // Fetch map layer and shutters in parallel
        const [mapResult, shutterResult] = await Promise.all([
          getMapLayers(spec.field),
          getNearbyShutters(spec.ra, spec.dec, spec.field),
        ]);

        const rgbLayer = mapResult.layers.find(l => l.filter === 'rgb')
          || mapResult.layers.find(l => l.is_default)
          || mapResult.layers[0]
          || null;

        setMapLayer(rgbLayer);
        setNearbyShutters(shutterResult.shutters);
        setLoading(false);
      } catch (err) {
        console.error('[InspectPage] Failed to load data:', err);
        setError('Failed to load spectrum data');
        setLoading(false);
      }
    })();
  }, [startId, authLoading, user]);

  // Loading / error / redirect states
  if (!startId || authLoading || !user) {
    return (
      <div className="fixed inset-0 z-[200] bg-background dark:bg-slate-900 flex items-center justify-center">
        <Loader2 className="w-8 h-8 animate-spin text-text-secondary dark:text-slate-400" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="fixed inset-0 z-[200] bg-background dark:bg-slate-900 flex items-center justify-center">
        <div className="text-center">
          <p className="text-lg text-red-500 mb-4">{error}</p>
          <button
            onClick={() => router.push('/spectra')}
            className="px-4 py-2 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            Back to Spectra
          </button>
        </div>
      </div>
    );
  }

  if (loading || !spectrum) {
    return (
      <div className="fixed inset-0 z-[200] bg-background dark:bg-slate-900 flex items-center justify-center">
        <div className="text-center text-text-secondary dark:text-slate-400">
          <Loader2 className="w-8 h-8 animate-spin mx-auto mb-3" />
          <p className="text-sm">Loading inspection mode...</p>
        </div>
      </div>
    );
  }

  return (
    <InspectionModeOverlay
      spectrum={spectrum}
      mapLayer={mapLayer}
      nearbyShutters={nearbyShutters}
      filterStr={filterStr}
      filters={filters}
      sortColumn={sortColumn}
      sortDirection={sortDirection}
    />
  );
}

export default function InspectPage() {
  return (
    <Suspense
      fallback={
        <div className="fixed inset-0 z-[200] bg-background dark:bg-slate-900 flex items-center justify-center">
          <Loader2 className="w-8 h-8 animate-spin text-text-secondary dark:text-slate-400" />
        </div>
      }
    >
      <InspectPageInner />
    </Suspense>
  );
}
