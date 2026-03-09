'use client';

import React, { useEffect, useState, useRef, useCallback } from 'react';
import { Loader2, HelpCircle, X } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { InspectionHeader } from './InspectionHeader';
import { DashboardPanel } from './DashboardPanel';
import { KeyboardShortcutSheet } from './KeyboardShortcutSheet';
import type { RedshiftSectionHandle } from './RedshiftSection';
import { SpectrumPlot } from '../SpectrumPlot';
import { useInspectionState, type InspectionInitialData } from '@/lib/hooks/useInspectionState';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { FilterOptions } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import type { SpectrumObject } from '@/lib/types';
import type { MapLayer, Shutter } from '@/lib/actions/map';
import { getNearbyShutters } from '@/lib/actions/map';
import { useSpectrumDataCache } from '@/lib/hooks/useSpectrumDataCache';
import { useMultiObjectCache } from '@/lib/hooks/useMultiObjectCache';
import { useObjectNavigation } from '@/lib/hooks/useObjectNavigation';
import { useInspectionQueue } from '@/lib/hooks/useInspectionQueue';
import { createClient } from '@/lib/supabase/client';

interface InspectionModeOverlayProps {
  spectrum: SpectrumObject;
  mapLayer: MapLayer | null;
  nearbyShutters: Shutter[];
  filterStr: string;
  filters: Partial<FilterOptions>;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
}

export const InspectionModeOverlay: React.FC<InspectionModeOverlayProps> = ({
  spectrum,
  mapLayer,
  nearbyShutters,
  filterStr,
  filters,
  sortColumn,
  sortDirection,
}) => {
  const router = useRouter();
  const { user, userProfile } = useAuth();
  const canEdit = !!(user && userProfile?.can_comment);
  const supabase = createClient();

  const redshiftInputRef = useRef<HTMLInputElement>(null);
  const redshiftSectionRef = useRef<RedshiftSectionHandle>(null);
  const [showHelp, setShowHelp] = useState(false);
  const [autoSaveHint, setAutoSaveHint] = useState<string | null>(null);
  const [commentCount, setCommentCount] = useState(0);

  // Current object state (initialized from props, updated client-side)
  const [currentSpectrum, setCurrentSpectrum] = useState(spectrum);
  const [currentShutters, setCurrentShutters] = useState<Shutter[]>(nearbyShutters);

  // Grating state
  const sortedSpectra = [...currentSpectrum.spectra].sort((a, b) => {
    const order = ['PRISM', 'G140M', 'G235M', 'G395M'];
    return order.indexOf(a.grating) - order.indexOf(b.grating);
  });
  const [activeGratingIdx, setActiveGratingIdx] = useState(0);
  const activeSpec = sortedSpectra[activeGratingIdx];

  // Inspection state
  const initialData: InspectionInitialData = {
    redshift_auto: currentSpectrum.redshift_auto,
    redshift_inspected: currentSpectrum.redshift_inspected,
    redshift_quality: currentSpectrum.redshift_quality,
    spectral_features: currentSpectrum.spectral_features,
    object_flags: currentSpectrum.object_flags,
    dq_flags: currentSpectrum.dq_flags,
    last_inspected_at: currentSpectrum.last_inspected_at,
    last_inspected_by: currentSpectrum.last_inspected_by,
  };

  const inspectionState = useInspectionState(currentSpectrum.id, initialData);

  // Spectrum data cache (for gratings)
  const { prefetchGratings, getCached: getCachedGrating, clearCache: clearGratingCache } = useSpectrumDataCache();

  // Object data cache (for prev/next prefetch)
  const { getCached: getCachedObject, setCached: setCachedObject, deleteCached: deleteCachedObject, clearCache: clearObjectCache } = useMultiObjectCache();

  // Snapshot-based inspection queue (fetched once, stable navigation)
  const queue = useInspectionQueue({
    initialObjectId: spectrum.object_id,
    filters,
    sortColumn,
    sortDirection,
  });

  // Navigation hook (skipNavQuery: queue handles prev/next)
  const { navigateTo, fetchObject, isNavigating, navigationError, setNavigationError } = useObjectNavigation({
    filters,
    sortColumn,
    sortDirection,
    skipNavQuery: true,
  });

  // Prefetch all gratings on mount for instant switching
  useEffect(() => {
    console.log('[InspectionMode] Mounting, will prefetch gratings');
    prefetchGratings(currentSpectrum.spectra);
  }, [currentSpectrum.spectra, prefetchGratings]);

  // Fetch comment count for current object
  useEffect(() => {
    async function fetchCommentCount() {
      if (!user) {
        setCommentCount(0);
        return;
      }

      try {
        const { count, error } = await supabase
          .from('comments')
          .select('*', { count: 'exact', head: true })
          .eq('object_id', currentSpectrum.id)
          .eq('is_deleted', false);

        if (error) {
          console.warn('[InspectionMode] Failed to fetch comment count:', error);
          setCommentCount(0);
        } else {
          setCommentCount(count ?? 0);
        }
      } catch (error) {
        console.warn('[InspectionMode] Error fetching comment count:', error);
        setCommentCount(0);
      }
    }

    fetchCommentCount();
  }, [currentSpectrum.id, user, supabase]);

  // Build navigation URL
  const buildUrl = useCallback((objectId: string) => {
    const params = new URLSearchParams(filterStr);
    params.set('mode', 'inspect');
    return `/spectra/${encodeURIComponent(objectId)}?${params.toString()}`;
  }, [filterStr]);

  // Handle queue redirect: if initial object not in queue, navigate to first queue item
  const redirectHandledRef = useRef(false);
  const [redirectComplete, setRedirectComplete] = useState(false);
  useEffect(() => {
    if (redirectHandledRef.current) return;
    if (queue.loading) return;
    redirectHandledRef.current = true;

    if (!queue.redirected || !queue.firstId) {
      // No redirect needed — mark complete immediately
      setRedirectComplete(true);
      return;
    }

    console.log('[InspectionMode] Object not in queue, redirecting to first item:', queue.firstId);
    (async () => {
      const data = await fetchObject(queue.firstId!);
      if (data) {
        setCachedObject(queue.firstId!, data);
        setCurrentSpectrum(data.spectrum);
        // Fetch shutters for the new object
        const shuttersResult = await getNearbyShutters(data.spectrum.ra, data.spectrum.dec, data.spectrum.field);
        setCurrentShutters(shuttersResult.shutters);
        inspectionState.resetState(data.spectrum);
        window.history.replaceState(null, '', buildUrl(queue.firstId!));
        setRedirectComplete(true);
      } else {
        // Fetch was aborted or failed — retry once
        console.warn('[InspectionMode] Redirect fetch failed, retrying...');
        const retry = await fetchObject(queue.firstId!);
        if (retry) {
          setCachedObject(queue.firstId!, retry);
          setCurrentSpectrum(retry.spectrum);
          const shuttersResult = await getNearbyShutters(retry.spectrum.ra, retry.spectrum.dec, retry.spectrum.field);
          setCurrentShutters(shuttersResult.shutters);
          inspectionState.resetState(retry.spectrum);
          window.history.replaceState(null, '', buildUrl(queue.firstId!));
        }
        setRedirectComplete(true);
      }
    })();
  }, [queue.loading, queue.redirected, queue.firstId, fetchObject, inspectionState, setCachedObject]); // eslint-disable-line react-hooks/exhaustive-deps

  // Queue is ready when: loaded AND redirect resolved (either not needed or complete)
  const queueReady = !queue.loading && redirectComplete;

  // Prefetch adjacent objects' data when queue position changes.
  // IMPORTANT: Must gate on queueReady (not just queue.loading) because fetchObject
  // uses a shared AbortController — prefetching before redirect completes would abort
  // the redirect's in-flight fetch.
  useEffect(() => {
    if (!queueReady || queue.isEmpty) return;

    const prefetch = async (objectId: string | null) => {
      if (!objectId) return;
      if (getCachedObject(objectId)) return; // Already cached
      try {
        const data = await fetchObject(objectId);
        if (data) {
          setCachedObject(objectId, data);
          prefetchGratings(data.spectrum.spectra);
        }
      } catch { /* ignore prefetch errors */ }
    };

    // Sequential: fetchObject aborts the previous in-flight request,
    // so we must await next (priority) before starting prev
    (async () => {
      await prefetch(queue.next);
      await prefetch(queue.prev);
    })();
  }, [queue.next, queue.prev, queueReady, queue.isEmpty, fetchObject, prefetchGratings, getCachedObject, setCachedObject]);

  // Handle browser back/forward navigation
  useEffect(() => {
    const handlePopState = async () => {
      const pathMatch = window.location.pathname.match(/\/spectra\/([^/?]+)/);
      if (!pathMatch) return;

      const urlObjectId = decodeURIComponent(pathMatch[1]);

      if (urlObjectId !== currentSpectrum.object_id) {
        console.log('[InspectionMode] Popstate detected, navigating to:', urlObjectId);

        redshiftSectionRef.current?.flushPendingChanges();
        const popSaveResult = await inspectionState.saveIfDirty();
        if (popSaveResult.saved) {
          deleteCachedObject(currentSpectrum.object_id);
        }

        // Update queue position
        queue.goTo(urlObjectId);

        // Check cache first
        const cached = getCachedObject(urlObjectId);
        const data = cached || await fetchObject(urlObjectId);
        if (data) {
          if (!cached) setCachedObject(urlObjectId, data);
          setCurrentSpectrum(data.spectrum);
          const shuttersResult = await getNearbyShutters(data.spectrum.ra, data.spectrum.dec, data.spectrum.field);
          setCurrentShutters(shuttersResult.shutters);
          inspectionState.resetState(data.spectrum);
        }
      }
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [currentSpectrum.object_id, fetchObject, inspectionState, queue, getCachedObject, setCachedObject, deleteCachedObject]);

  // Body scroll prevention
  useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = '';
    };
  }, []);

  // Navigate with client-side data swapping (queue-based)
  const handleNavigate = useCallback(async (objectId: string | null) => {
    if (!objectId) return;

    // 1. Auto-save FIRST
    redshiftSectionRef.current?.flushPendingChanges();
    const saveResult = await inspectionState.saveIfDirty();
    if (saveResult.saved) {
      deleteCachedObject(currentSpectrum.object_id);
    }
    if (saveResult.reason === 'quality-zero') {
      setAutoSaveHint('Set quality to auto-save');
      setTimeout(() => setAutoSaveHint(null), 2000);
    }

    // 2. Update queue position
    queue.goTo(objectId);

    // 3. Check object cache first
    const cached = getCachedObject(objectId);
    let data;
    if (cached) {
      data = cached;
    } else {
      // Cache miss — fetch from server
      data = await navigateTo(objectId, async () => true);
      if (!data) return;
      setCachedObject(objectId, data);
    }

    // 4. Update state
    setCurrentSpectrum(data.spectrum);
    // Fetch shutters for the new object
    const shuttersResult = await getNearbyShutters(data.spectrum.ra, data.spectrum.dec, data.spectrum.field);
    setCurrentShutters(shuttersResult.shutters);
    inspectionState.resetState(data.spectrum);

    // 5. Update URL (use replaceState to avoid Next.js router remount)
    window.history.replaceState(null, '', buildUrl(objectId));
  }, [navigateTo, inspectionState, buildUrl, queue, getCachedObject, setCachedObject, deleteCachedObject, currentSpectrum.object_id]);

  const handlePrev = useCallback(() => handleNavigate(queue.prev), [handleNavigate, queue.prev]);
  const handleNext = useCallback(() => handleNavigate(queue.next), [handleNavigate, queue.next]);

  const handleSave = useCallback(async () => {
    const saved = await inspectionState.save();
    if (saved) {
      deleteCachedObject(currentSpectrum.object_id);
    }
  }, [inspectionState, deleteCachedObject, currentSpectrum.object_id]);

  const handleSaveAndNext = useCallback(() => {
    handleNavigate(queue.next);
  }, [handleNavigate, queue.next]);

  const handleClose = useCallback(async () => {
    // Auto-save before exiting
    redshiftSectionRef.current?.flushPendingChanges();
    await inspectionState.saveIfDirty();

    // Clear caches when explicitly exiting inspection mode
    clearGratingCache();
    clearObjectCache();

    const params = new URLSearchParams(filterStr);
    params.delete('mode');
    const qs = params.toString();
    router.push(`/spectra/${encodeURIComponent(currentSpectrum.object_id)}${qs ? `?${qs}` : ''}`);
  }, [router, currentSpectrum.object_id, filterStr, clearGratingCache, clearObjectCache, inspectionState]);

  const handleCycleGrating = useCallback(() => {
    if (sortedSpectra.length <= 1) return;
    setActiveGratingIdx((prev) => (prev + 1) % sortedSpectra.length);
  }, [sortedSpectra.length]);

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const isInput = target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT';

      // Escape always works
      if (e.key === 'Escape') {
        if (isInput) {
          target.blur();
        } else if (showHelp) {
          setShowHelp(false);
        } else {
          handleClose();
        }
        return;
      }

      // Suppress other shortcuts when typing
      if (isInput) return;

      switch (e.key) {
        case '1':
        case '2':
        case '3':
        case '4':
          if (canEdit) {
            inspectionState.setRedshiftQuality(parseInt(e.key));
          }
          break;
        case 'ArrowRight':
        case 'n':
        case 'N':
          e.preventDefault();
          handleNext();
          break;
        case 'ArrowLeft':
        case 'p':
        case 'P':
          e.preventDefault();
          handlePrev();
          break;
        case 's':
        case 'S':
          e.preventDefault();
          handleSave();
          break;
        case 'z':
        case 'Z':
          e.preventDefault();
          redshiftInputRef.current?.focus();
          redshiftInputRef.current?.select();
          break;
        case 'g':
        case 'G':
          e.preventDefault();
          handleCycleGrating();
          break;
        case '?':
          setShowHelp((prev) => !prev);
          break;
      }
    };

    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [canEdit, inspectionState, handleNext, handlePrev, handleSave, handleClose, handleCycleGrating, showHelp]);

  // Current redshift for emission lines
  const currentRedshift = inspectionState.currentRedshift ?? 0;

  return (
    <div
      className="fixed inset-0 z-[200] bg-background dark:bg-slate-900 flex flex-col overflow-hidden"
      style={{ isolation: 'isolate' }}
      data-inspection-mode
    >
      {/* Minimal header while queue loads — no object-specific details */}
      {!queueReady ? (
        <div className="h-12 border-b border-border dark:border-slate-700 px-4 flex items-center justify-between bg-background dark:bg-slate-900 flex-shrink-0">
          <div />
          <Loader2 className="w-4 h-4 animate-spin text-text-secondary dark:text-slate-400" />
          <div className="flex items-center gap-1">
            <button
              onClick={() => setShowHelp((prev) => !prev)}
              className="p-1.5 rounded hover:bg-card dark:hover:bg-slate-700 transition-colors text-text-secondary dark:text-slate-400"
              title="Keyboard shortcuts (?)"
            >
              <HelpCircle className="w-4 h-4" />
            </button>
            <button
              onClick={handleClose}
              className="p-1.5 rounded hover:bg-card dark:hover:bg-slate-700 transition-colors text-text-secondary dark:text-slate-400"
              title="Exit inspection mode (Esc)"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>
      ) : (
        <InspectionHeader
          objectId={currentSpectrum.object_id}
          field={currentSpectrum.field}
          programName={currentSpectrum.program_name || null}
          index={queue.index}
          total={queue.total}
          loading={isNavigating}
          hasPrev={!!queue.prev}
          hasNext={!!queue.next}
          commentCount={commentCount}
          gratings={sortedSpectra}
          activeGratingIdx={activeGratingIdx}
          onGratingChange={setActiveGratingIdx}
          onPrev={handlePrev}
          onNext={handleNext}
          onToggleHelp={() => setShowHelp((prev) => !prev)}
          onClose={handleClose}
        />
      )}

      {/* Navigation error banner */}
      {navigationError && (
        <div className="absolute top-14 left-1/2 -translate-x-1/2 px-4 py-2 bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200 text-sm rounded-lg shadow-lg z-10">
          {navigationError}
          <button
            onClick={() => setNavigationError(null)}
            className="ml-2 underline hover:no-underline"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Loading state while queue loads or redirect is in progress */}
      {!queueReady && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center text-text-secondary dark:text-slate-400">
            Loading inspection queue...
          </div>
        </div>
      )}

      {/* Empty queue message */}
      {queueReady && queue.isEmpty && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <p className="text-lg text-text-secondary dark:text-slate-400 mb-4">
              No uninspected objects match your filters.
            </p>
            <button
              onClick={handleClose}
              className="px-4 py-2 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
            >
              Exit Inspection Mode
            </button>
          </div>
        </div>
      )}

      {/* Main content - spectrum on left, dashboard on right */}
      {queueReady && !queue.isEmpty && <div className="flex-1 flex min-h-0">
        {/* Left: Spectrum (expanded) */}
        <div className="flex-1 flex flex-col min-w-0 px-4 py-2">
          {/* Spectrum Plot */}
          {activeSpec && (
            <div className="flex-1 min-h-0 overflow-auto">
              <SpectrumPlot
                key={activeSpec.grating}
                fitsPath={activeSpec.fits_path}
                grating={activeSpec.grating}
                initialRedshift={currentRedshift}
                inspectionMode={true}
                getCachedData={getCachedGrating}
              />
            </div>
          )}
        </div>

        {/* Right: Dashboard Panel */}
        <DashboardPanel
          spectrum={currentSpectrum}
          mapLayer={mapLayer}
          shutters={currentShutters}
          inspectionState={inspectionState}
          canEdit={canEdit}
          commentCount={commentCount}
          redshiftInputRef={redshiftInputRef}
          redshiftSectionRef={redshiftSectionRef}
          onSave={handleSave}
          onSaveAndNext={handleSaveAndNext}
        />
      </div>}

      {/* Auto-save hint */}
      {autoSaveHint && (
        <div className="absolute top-14 left-1/2 -translate-x-1/2 px-3 py-1.5 bg-amber-100 dark:bg-amber-900 text-amber-800 dark:text-amber-200 text-xs rounded-lg shadow">
          {autoSaveHint}
        </div>
      )}

      {showHelp && <KeyboardShortcutSheet onClose={() => setShowHelp(false)} />}
    </div>
  );
};
