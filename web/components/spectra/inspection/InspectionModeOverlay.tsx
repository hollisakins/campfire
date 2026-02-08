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
import { getAdjacentObjectIds, type FilterOptions } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import type { SpectrumObject } from '@/lib/types';
import { useSpectrumDataCache } from '@/lib/hooks/useSpectrumDataCache';
import { useObjectNavigation } from '@/lib/hooks/useObjectNavigation';
import { useMultiObjectCache } from '@/lib/hooks/useMultiObjectCache';
import { createClient } from '@/lib/supabase/client';

interface InspectionModeOverlayProps {
  spectrum: SpectrumObject;
  rgbImageUrl: string | null;
  filterStr: string;
  filters: Partial<FilterOptions>;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
}

interface NavState {
  prev: string | null;
  next: string | null;
  index: number;
  total: number;
  loading: boolean;
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

  // Object cache (for navigation)
  const { getCached, setCached, prefetchAdjacent, clearCache } = useMultiObjectCache();

  // Navigation hook
  const { navigateTo, fetchObject, isNavigating, navigationError, setNavigationError } = useObjectNavigation({
    filters,
    sortColumn,
    sortDirection,
  });

  // Navigation state
  const [nav, setNav] = useState<NavState>({
    prev: null,
    next: null,
    index: 0,
    total: 0,
    loading: true,
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

  // Check URL on mount - if it doesn't match current state, load from cache
  // This handles remounts with stale props (URL changed but props are old)
  useEffect(() => {
    const urlMatch = window.location.pathname.match(/\/spectra\/([^/?]+)/);
    if (urlMatch) {
      const urlObjectId = decodeURIComponent(urlMatch[1]);
      if (urlObjectId !== currentSpectrum.object_id) {
        console.log('[InspectionMode] URL mismatch on mount, checking cache:', urlObjectId);
        const cached = getCached(urlObjectId);
        if (cached) {
          console.log('[InspectionMode] Loading from cache to fix URL mismatch');
          console.log('[InspectionMode] Cached RGB URL:', cached.rgbImageUrl);
          setCurrentSpectrum(cached.spectrum);
          // Only update RGB URL if we have a valid one (don't overwrite with null)
          if (cached.rgbImageUrl !== null) {
            setCurrentRgbUrl(cached.rgbImageUrl);
          }
          setNav({ ...cached.nav, loading: false });
          inspectionState.resetState(cached.spectrum);
        }
      }
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  // Empty deps - only run once on mount to fix URL/state mismatch

  // NOTE: We DON'T clear cache on unmount because:
  // 1. Cache is module-level and should persist across remounts
  // 2. router.replace() causes remount, we want cache to survive
  // 3. Cache will be cleared on handleClose (explicit exit) instead

  // Eager prefetch: Start immediately on mount (parallel with nav state load)
  useEffect(() => {
    let cancelled = false;

    async function eagerPrefetch() {
      try {
        const adjacentIds = await getAdjacentObjectIds(
          currentSpectrum.object_id,
          filters,
          sortColumn,
          sortDirection
        );

        if (!cancelled) {
          setNav({
            prev: adjacentIds.prev,
            next: adjacentIds.next,
            index: adjacentIds.currentIndex,
            total: adjacentIds.total,
            loading: false,
          });

          // Start prefetching adjacent objects IMMEDIATELY (including FITS data)
          if (adjacentIds.next || adjacentIds.prev) {
            prefetchAdjacent(adjacentIds.prev, adjacentIds.next, fetchObject, prefetchGratings);
          }
        }
      } catch (error) {
        console.error('[InspectionMode] Eager prefetch failed:', error);
        if (!cancelled) {
          setNav((prev) => ({ ...prev, loading: false }));
        }
      }
    }

    eagerPrefetch();
    return () => { cancelled = true; };
  }, [currentSpectrum.object_id, filters, sortColumn, sortDirection, prefetchAdjacent, fetchObject, prefetchGratings]);

  // Build navigation URL
  const buildUrl = useCallback((objectId: string) => {
    const params = new URLSearchParams(filterStr);
    params.set('mode', 'inspect');
    return `/spectra/${encodeURIComponent(objectId)}?${params.toString()}`;
  }, [filterStr]);

  // Handle browser back/forward navigation
  useEffect(() => {
    const handlePopState = () => {
      // Don't handle popstate while saving
      if (inspectionState.saving) {
        console.log('[InspectionMode] Blocked popstate during save');
        return;
      }

      // Extract objectId from current URL
      const pathMatch = window.location.pathname.match(/\/spectra\/([^/?]+)/);
      if (!pathMatch) return;

      const urlObjectId = decodeURIComponent(pathMatch[1]);

      // If URL changed to different object, fetch it
      if (urlObjectId !== currentSpectrum.object_id) {
        console.log('[InspectionMode] Popstate detected, navigating to:', urlObjectId);
        const cached = getCached(urlObjectId);
        if (cached) {
          // Use cache
          console.log('[InspectionMode] Using cached data for popstate');
          setCurrentSpectrum(cached.spectrum);
          // Only update RGB URL if we have a valid one
          if (cached.rgbImageUrl !== null) {
            setCurrentRgbUrl(cached.rgbImageUrl);
          }
          setNav({ ...cached.nav, loading: false });
          inspectionState.resetState(cached.spectrum);
        } else {
          // Fetch from server
          console.log('[InspectionMode] Fetching fresh data for popstate');
          fetchObject(urlObjectId).then(data => {
            if (data) {
              setCurrentSpectrum(data.spectrum);
              // Only update RGB URL if we have a valid one
              if (data.rgbImageUrl !== null) {
                setCurrentRgbUrl(data.rgbImageUrl);
              }
              setNav({ ...data.nav, loading: false });
              inspectionState.resetState(data.spectrum);
              setCached(urlObjectId, data);
            }
          });
        }
      }
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [currentSpectrum.object_id, getCached, fetchObject, setCached, inspectionState]);

  // Body scroll prevention
  useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = '';
    };
  }, []);

  // Navigate with client-side data swapping
  const handleNavigate = useCallback(async (objectId: string | null) => {
    if (!objectId) return;

    // 1. Check cache first
    const cached = getCached(objectId);
    if (cached) {
      console.log('[InspectionMode] Using cached object for navigation');
      console.log('[InspectionMode] Cached RGB URL:', cached.rgbImageUrl?.substring(0, 80) + '...');
      console.log('[InspectionMode] Setting currentSpectrum.object_id to:', cached.spectrum.object_id);
      setCurrentSpectrum(cached.spectrum);
      // Only update RGB URL if we have a valid one (don't overwrite with null)
      if (cached.rgbImageUrl !== null) {
        console.log('[InspectionMode] Setting currentRgbUrl to:', cached.rgbImageUrl?.substring(0, 80) + '...');
        setCurrentRgbUrl(cached.rgbImageUrl);
      } else {
        console.warn('[InspectionMode] Cached RGB URL is null, keeping current');
      }
      setNav({ ...cached.nav, loading: false });
      inspectionState.resetState(cached.spectrum);
      // Use replaceState to avoid Next.js router remount
      window.history.replaceState(null, '', buildUrl(objectId));
      prefetchAdjacent(cached.nav.prev, cached.nav.next, fetchObject, prefetchGratings);
      return;
    }

    // 2. Prepare auto-save callback
    const onBeforeNavigate = async () => {
      // Flush any pending debounced redshift changes first
      redshiftSectionRef.current?.flushPendingChanges();

      if (inspectionState.hasChanges && inspectionState.redshiftQuality > 0) {
        return await inspectionState.save();
      }
      if (inspectionState.hasChanges && inspectionState.redshiftQuality === 0) {
        setAutoSaveHint('Set quality to auto-save');
        setTimeout(() => setAutoSaveHint(null), 2000);
        return false;
      }
      return true;
    };

    // 3. Fetch new data
    const data = await navigateTo(objectId, onBeforeNavigate);
    if (!data) return; // Navigation blocked or failed

    // 4. Update state
    console.log('[InspectionMode] Fetched RGB URL:', data.rgbImageUrl);
    console.log('[InspectionMode] Setting currentSpectrum.object_id to:', data.spectrum.object_id);
    setCurrentSpectrum(data.spectrum);
    // Only update RGB URL if we have a valid one
    if (data.rgbImageUrl !== null) {
      console.log('[InspectionMode] Setting currentRgbUrl to:', data.rgbImageUrl?.substring(0, 80) + '...');
      setCurrentRgbUrl(data.rgbImageUrl);
    } else {
      console.warn('[InspectionMode] Fetched RGB URL is null, keeping current');
    }
    setNav({ ...data.nav, loading: false });
    inspectionState.resetState(data.spectrum);

    // 5. Update URL (use replaceState to avoid Next.js router remount)
    window.history.replaceState(null, '', buildUrl(objectId));

    // 6. Cache and prefetch (including FITS data)
    setCached(objectId, data);
    prefetchAdjacent(data.nav.prev, data.nav.next, fetchObject, prefetchGratings);
  }, [getCached, navigateTo, setCached, prefetchAdjacent, fetchObject, inspectionState, buildUrl, prefetchGratings]);

  const handlePrev = useCallback(() => handleNavigate(nav.prev), [handleNavigate, nav.prev]);
  const handleNext = useCallback(() => handleNavigate(nav.next), [handleNavigate, nav.next]);

  const handleSave = useCallback(() => {
    inspectionState.save();
  }, [inspectionState]);

  const handleSaveAndNext = useCallback(() => {
    handleNavigate(nav.next);
  }, [handleNavigate, nav.next]);

  const handleClose = useCallback(() => {
    // Clear caches when explicitly exiting inspection mode
    clearGratingCache();
    clearCache();

    const params = new URLSearchParams(filterStr);
    params.delete('mode');
    const qs = params.toString();
    router.push(`/spectra/${encodeURIComponent(currentSpectrum.object_id)}${qs ? `?${qs}` : ''}`);
  }, [router, currentSpectrum.object_id, filterStr, clearGratingCache, clearCache]);

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
        index={nav.index}
        total={nav.total}
        loading={nav.loading || isNavigating}
        hasPrev={!!nav.prev}
        hasNext={!!nav.next}
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

      {/* Main content - spectrum on left, dashboard on right */}
      <div className="flex-1 flex min-h-0">
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
      </div>

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
