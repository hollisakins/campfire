'use client';

import React, { useEffect, useState, useRef, useCallback } from 'react';
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
import { useSpectrumDataCache } from '@/lib/hooks/useSpectrumDataCache';
import { useObjectNavigation } from '@/lib/hooks/useObjectNavigation';
import { useInspectionQueue } from '@/lib/hooks/useInspectionQueue';
import { createClient } from '@/lib/supabase/client';

interface InspectionModeOverlayProps {
  spectrum: SpectrumObject;
  rgbImageUrl: string | null;
  filterStr: string;
  filters: Partial<FilterOptions>;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
}

export const InspectionModeOverlay: React.FC<InspectionModeOverlayProps> = ({
  spectrum,
  rgbImageUrl,
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
  const [currentRgbUrl, setCurrentRgbUrl] = useState(rgbImageUrl);

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
  useEffect(() => {
    if (redirectHandledRef.current) return;
    if (queue.loading || !queue.redirected || !queue.firstId) return;
    redirectHandledRef.current = true;

    console.log('[InspectionMode] Object not in queue, redirecting to first item:', queue.firstId);
    // Fetch the first queue item
    (async () => {
      const data = await fetchObject(queue.firstId!);
      if (data) {
        setCurrentSpectrum(data.spectrum);
        if (data.rgbImageUrl !== null) {
          setCurrentRgbUrl(data.rgbImageUrl);
        }
        inspectionState.resetState(data.spectrum);
        window.history.replaceState(null, '', buildUrl(queue.firstId!));
      }
    })();
  }, [queue.loading, queue.redirected, queue.firstId, fetchObject, inspectionState]); // eslint-disable-line react-hooks/exhaustive-deps

  // Prefetch next object's data when queue position changes
  useEffect(() => {
    if (queue.loading || queue.isEmpty) return;
    if (queue.next) {
      // Prefetch next object (spectrum + FITS)
      fetchObject(queue.next).then(data => {
        if (data) {
          prefetchGratings(data.spectrum.spectra);
        }
      }).catch(() => {});
    }
  }, [queue.next, queue.loading, queue.isEmpty, fetchObject, prefetchGratings]);

  // Handle browser back/forward navigation
  useEffect(() => {
    const handlePopState = async () => {
      const pathMatch = window.location.pathname.match(/\/spectra\/([^/?]+)/);
      if (!pathMatch) return;

      const urlObjectId = decodeURIComponent(pathMatch[1]);

      if (urlObjectId !== currentSpectrum.object_id) {
        console.log('[InspectionMode] Popstate detected, navigating to:', urlObjectId);

        redshiftSectionRef.current?.flushPendingChanges();
        await inspectionState.saveIfDirty();

        // Update queue position
        queue.goTo(urlObjectId);

        const data = await fetchObject(urlObjectId);
        if (data) {
          setCurrentSpectrum(data.spectrum);
          if (data.rgbImageUrl !== null) {
            setCurrentRgbUrl(data.rgbImageUrl);
          }
          inspectionState.resetState(data.spectrum);
        }
      }
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [currentSpectrum.object_id, fetchObject, inspectionState, queue]);

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
    if (saveResult.reason === 'quality-zero') {
      setAutoSaveHint('Set quality to auto-save');
      setTimeout(() => setAutoSaveHint(null), 2000);
    }

    // 2. Update queue position
    queue.goTo(objectId);

    // 3. Fetch object data (no nav query needed — queue provides prev/next)
    const data = await navigateTo(objectId, async () => true);
    if (!data) return;

    // 4. Update state
    setCurrentSpectrum(data.spectrum);
    if (data.rgbImageUrl !== null) {
      setCurrentRgbUrl(data.rgbImageUrl);
    }
    inspectionState.resetState(data.spectrum);

    // 5. Update URL (use replaceState to avoid Next.js router remount)
    window.history.replaceState(null, '', buildUrl(objectId));
  }, [navigateTo, inspectionState, buildUrl, queue]);

  const handlePrev = useCallback(() => handleNavigate(queue.prev), [handleNavigate, queue.prev]);
  const handleNext = useCallback(() => handleNavigate(queue.next), [handleNavigate, queue.next]);

  const handleSave = useCallback(() => {
    inspectionState.save();
  }, [inspectionState]);

  const handleSaveAndNext = useCallback(() => {
    handleNavigate(queue.next);
  }, [handleNavigate, queue.next]);

  const handleClose = useCallback(async () => {
    // Auto-save before exiting
    redshiftSectionRef.current?.flushPendingChanges();
    await inspectionState.saveIfDirty();

    // Clear grating cache when explicitly exiting inspection mode
    clearGratingCache();

    const params = new URLSearchParams(filterStr);
    params.delete('mode');
    const qs = params.toString();
    router.push(`/spectra/${encodeURIComponent(currentSpectrum.object_id)}${qs ? `?${qs}` : ''}`);
  }, [router, currentSpectrum.object_id, filterStr, clearGratingCache, inspectionState]);

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
      <InspectionHeader
        objectId={currentSpectrum.object_id}
        field={currentSpectrum.field}
        programName={currentSpectrum.program_name || null}
        index={queue.index}
        total={queue.total}
        loading={queue.loading || isNavigating}
        hasPrev={!!queue.prev}
        hasNext={!!queue.next}
        commentCount={commentCount}
        onPrev={handlePrev}
        onNext={handleNext}
        onToggleHelp={() => setShowHelp((prev) => !prev)}
        onClose={handleClose}
      />

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

      {/* Empty queue message */}
      {queue.isEmpty && (
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
      {!queue.isEmpty && <div className="flex-1 flex min-h-0">
        {/* Left: Spectrum (expanded) */}
        <div className="flex-1 flex flex-col min-w-0 px-4 py-2">
          {/* Grating tabs */}
          {sortedSpectra.length > 1 && (
            <div className="flex gap-2 mb-2 flex-shrink-0">
              {sortedSpectra.map((spec, idx) => (
                <button
                  key={spec.grating}
                  onClick={() => setActiveGratingIdx(idx)}
                  className={`px-4 py-2 text-sm font-medium rounded transition-colors
                    ${idx === activeGratingIdx
                      ? 'bg-primary text-white'
                      : 'bg-card dark:bg-slate-800 text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-700 border border-border dark:border-slate-600'
                    }`}
                >
                  <span className="mr-1">{spec.grating}</span>
                  {idx === activeGratingIdx && (
                    <kbd className="text-xs opacity-70">G</kbd>
                  )}
                </button>
              ))}
            </div>
          )}

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
          rgbImageUrl={currentRgbUrl}
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
