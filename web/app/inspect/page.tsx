'use client';

import React, { useEffect, useState, useRef, Suspense } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { Loader2 } from 'lucide-react';
import { InspectionModeOverlay } from '@/components/spectra/inspection/InspectionModeOverlay';
import { useAuth } from '@/lib/contexts/AuthContext';
import { getObjectById } from '@/lib/actions/spectra';
import { parseFiltersFromURL } from '@/lib/utils/url-params';
import type { ObjectDetail } from '@/lib/types';

function InspectPageInner() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();

  const startId = searchParams.get('start');

  // Filter params (all URL params except `start`).
  const urlParams = new URLSearchParams(searchParams.toString());
  urlParams.delete('start');
  const filterStr = urlParams.toString();
  const filters = parseFiltersFromURL(urlParams);

  const [object, setObject] = useState<ObjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetchedRef = useRef(false);

  useEffect(() => {
    if (!startId) router.replace('/nirspec');
  }, [startId, router]);

  useEffect(() => {
    if (!authLoading && !user) router.replace('/login');
  }, [authLoading, user, router]);

  useEffect(() => {
    if (!startId || authLoading || !user || fetchedRef.current) return;
    fetchedRef.current = true;

    (async () => {
      try {
        const result = await getObjectById(startId);
        if (!result.object) {
          setError('Object not found');
          setLoading(false);
          return;
        }
        setObject(result.object);
        setLoading(false);
      } catch (err) {
        console.error('[InspectPage] Failed to load data:', err);
        setError('Failed to load object data');
        setLoading(false);
      }
    })();
  }, [startId, authLoading, user]);

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
            onClick={() => router.push('/nirspec')}
            className="px-4 py-2 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            Back to NIRSpec
          </button>
        </div>
      </div>
    );
  }

  if (loading || !object) {
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
      object={object}
      filterStr={filterStr}
      filters={filters}
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
