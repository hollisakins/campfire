'use client';

import React, { useEffect, useState, useRef, useCallback, useMemo } from 'react';
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
import { GRATINGS, type ObjectDetail, type Spectrum } from '@/lib/types';
import { useSpectrumDataCache } from '@/lib/hooks/useSpectrumDataCache';
import { useMultiObjectCache } from '@/lib/hooks/useMultiObjectCache';
import { useObjectNavigation } from '@/lib/hooks/useObjectNavigation';
import { useInspectionQueue } from '@/lib/hooks/useInspectionQueue';
import { createClient } from '@/lib/supabase/client';

interface InspectionModeOverlayProps {
  object: ObjectDetail;
  filterStr: string;
  filters: Partial<FilterOptions>;
}

interface TabSpec {
  spectrum: Spectrum;
  targetId: string;
  observation: string;
  programName: string;
  /** Compact label shown in the header tab. */
  label: string;
  /** Tooltip with full context (program / observation / target). */
  title: string;
}

/**
 * Build a flat list of (member spectrum × member target) tabs ordered by
 * GRATINGS canonical ordering, then by member target order. Multi-target
 * objects get observation suffixes so users can disambiguate.
 */
function buildSpectrumTabs(object: ObjectDetail): TabSpec[] {
  const isMultiTarget = object.member_targets.length > 1;
  const tabs: TabSpec[] = [];

  for (const member of object.member_targets) {
    for (const spec of member.spectra) {
      const programShort = (member.program_slug || member.program_name || '').toUpperCase().split('_')[0];
      const label = isMultiTarget ? `${spec.grating} (${programShort})` : spec.grating;
      tabs.push({
        spectrum: spec,
        targetId: member.target_id,
        observation: member.observation,
        programName: member.program_name,
        label,
        title: `${spec.grating} · ${member.program_name} · ${member.observation} · ${member.target_id}`,
      });
    }
  }

  const gratingOrder = (GRATINGS as readonly string[]);
  tabs.sort((a, b) => gratingOrder.indexOf(a.spectrum.grating) - gratingOrder.indexOf(b.spectrum.grating));
  return tabs;
}

function inspectionInitialFromObject(o: ObjectDetail): InspectionInitialData {
  return {
    redshift_auto: o.redshift_auto,
    redshift_inspected: o.redshift_inspected,
    redshift_quality: o.redshift_quality,
    last_inspected_at: o.last_inspected_at,
    last_inspected_by: o.last_inspected_by,
    version: o.version,
  };
}

export const InspectionModeOverlay: React.FC<InspectionModeOverlayProps> = ({
  object,
  filterStr,
  filters,
}) => {
  const router = useRouter();
  const { user, userProfile } = useAuth();
  const canEdit = !!(user && userProfile?.can_comment);
  const supabase = createClient();

  const redshiftInputRef = useRef<HTMLInputElement>(null);
  const redshiftSectionRef = useRef<RedshiftSectionHandle>(null);
  const [showHelp, setShowHelp] = useState(false);
  const [commentCount, setCommentCount] = useState(0);

  const [currentObject, setCurrentObject] = useState(object);

  const tabs = useMemo(() => buildSpectrumTabs(currentObject), [currentObject]);
  const [activeKey, setActiveKey] = useState<string>(() => {
    const first = buildSpectrumTabs(object)[0];
    return first ? `${first.targetId}::${first.spectrum.grating}` : '';
  });
  const activeTabIdx = Math.max(0, tabs.findIndex(t => `${t.targetId}::${t.spectrum.grating}` === activeKey));
  const activeTab = tabs[activeTabIdx];

  const initialData = useMemo(() => inspectionInitialFromObject(currentObject), [currentObject]);
  const inspectionState = useInspectionState(currentObject.id, initialData);

  const { prefetchGratings, getCached: getCachedGrating, clearCache: clearGratingCache } = useSpectrumDataCache();
  const { getCached: getCachedObject, setCached: setCachedObject, deleteCached: deleteCachedObject, clearCache: clearObjectCache } = useMultiObjectCache();

  const queue = useInspectionQueue({
    initialObjectId: object.object_id,
    filters,
  });

  const { navigateTo, fetchObject, prefetchObject, isNavigating, navigationError, setNavigationError } = useObjectNavigation();

  const allMemberSpectra = useMemo(
    () => currentObject.member_targets.flatMap(m => m.spectra),
    [currentObject]
  );

  // Prefetch all gratings on object change for instant tab switching.
  useEffect(() => {
    prefetchGratings(allMemberSpectra);
  }, [allMemberSpectra, prefetchGratings]);

  // Fetch comment count across all member targets + the object itself.
  useEffect(() => {
    async function fetchCommentCount() {
      if (!user) {
        setCommentCount(0);
        return;
      }
      try {
        const memberIds = currentObject.member_targets.map(m => m.id);
        const [objectRes, targetRes] = await Promise.all([
          supabase
            .from('comments')
            .select('*', { count: 'exact', head: true })
            .eq('object_id', currentObject.id)
            .eq('is_deleted', false),
          memberIds.length > 0
            ? supabase
                .from('comments')
                .select('*', { count: 'exact', head: true })
                .in('target_id', memberIds)
                .eq('is_deleted', false)
            : Promise.resolve({ count: 0, error: null }),
        ]);
        const total = (objectRes.count ?? 0) + (targetRes.count ?? 0);
        setCommentCount(total);
      } catch (err) {
        console.warn('[InspectionMode] Error fetching comment count:', err);
        setCommentCount(0);
      }
    }
    fetchCommentCount();
  }, [currentObject.id, currentObject.member_targets, user, supabase]);

  // Handle queue redirect: if initial object isn't in the queue, jump to first.
  const redirectHandledRef = useRef(false);
  const [redirectComplete, setRedirectComplete] = useState(false);
  useEffect(() => {
    if (redirectHandledRef.current) return;
    if (queue.loading) return;
    redirectHandledRef.current = true;

    if (!queue.redirected || !queue.firstId) {
      setRedirectComplete(true);
      return;
    }

    (async () => {
      const data = await fetchObject(queue.firstId!);
      if (data) {
        setCachedObject(queue.firstId!, data);
        setCurrentObject(data.object);
        inspectionState.resetState(inspectionInitialFromObject(data.object));
      }
      setRedirectComplete(true);
    })();
  }, [queue.loading, queue.redirected, queue.firstId, fetchObject, inspectionState, setCachedObject]);

  const queueReady = !queue.loading && redirectComplete;

  // Prefetch adjacent objects when queue position changes.
  useEffect(() => {
    if (!queueReady || queue.isEmpty) return;

    const prefetch = async (objectId: string | null) => {
      if (!objectId) return;
      if (getCachedObject(objectId)) return;
      try {
        const data = await prefetchObject(objectId);
        if (data) {
          setCachedObject(objectId, data);
          prefetchGratings(data.object.member_targets.flatMap(m => m.spectra));
        }
      } catch { /* ignore */ }
    };

    prefetch(queue.next);
    prefetch(queue.prev);
  }, [queue.next, queue.prev, queueReady, queue.isEmpty, prefetchObject, prefetchGratings, getCachedObject, setCachedObject]);

  // Block body scroll behind the overlay.
  useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = ''; };
  }, []);

  const handleNavigate = useCallback(async (objectId: string | null) => {
    if (!objectId) return;

    redshiftSectionRef.current?.flushPendingChanges();
    const saveResult = await inspectionState.saveIfDirty();
    if (saveResult.saved) {
      deleteCachedObject(currentObject.object_id);
    }

    queue.goTo(objectId);

    const cached = getCachedObject(objectId);
    let data;
    if (cached) {
      data = cached;
    } else {
      data = await navigateTo(objectId, async () => true);
      if (!data) return;
      setCachedObject(objectId, data);
    }

    setCurrentObject(data.object);
    inspectionState.resetState(inspectionInitialFromObject(data.object));
    // Reset active tab to the first available spectrum for the new object.
    const newTabs = buildSpectrumTabs(data.object);
    if (newTabs.length > 0) {
      setActiveKey(`${newTabs[0].targetId}::${newTabs[0].spectrum.grating}`);
    }
  }, [navigateTo, inspectionState, queue, getCachedObject, setCachedObject, deleteCachedObject, currentObject.object_id]);

  const handlePrev = useCallback(() => handleNavigate(queue.prev), [handleNavigate, queue.prev]);
  const handleNext = useCallback(() => handleNavigate(queue.next), [handleNavigate, queue.next]);

  const handleSave = useCallback(async () => {
    const result = await inspectionState.save();
    if (result.success) {
      deleteCachedObject(currentObject.object_id);
    }
  }, [inspectionState, deleteCachedObject, currentObject.object_id]);

  const handleSaveAndNext = useCallback(() => {
    handleNavigate(queue.next);
  }, [handleNavigate, queue.next]);

  const handleClose = useCallback(async () => {
    redshiftSectionRef.current?.flushPendingChanges();
    await inspectionState.saveIfDirty();
    clearGratingCache();
    clearObjectCache();
    const qs = filterStr;
    router.push(`/nirspec/objects/${encodeURIComponent(currentObject.object_id)}${qs ? `?${qs}` : ''}`);
  }, [router, currentObject.object_id, filterStr, clearGratingCache, clearObjectCache, inspectionState]);

  const handleCycleGrating = useCallback(() => {
    if (tabs.length <= 1) return;
    const nextIdx = (activeTabIdx + 1) % tabs.length;
    const t = tabs[nextIdx];
    setActiveKey(`${t.targetId}::${t.spectrum.grating}`);
  }, [tabs, activeTabIdx]);

  // Keyboard shortcuts.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const isInput = target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT';

      if (e.key === 'Escape') {
        if (isInput) target.blur();
        else if (showHelp) setShowHelp(false);
        else handleClose();
        return;
      }
      if (isInput) return;

      switch (e.key) {
        case '1':
        case '2':
        case '3':
        case '4':
          if (canEdit) inspectionState.setRedshiftQuality(parseInt(e.key));
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
          setShowHelp(prev => !prev);
          break;
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [canEdit, inspectionState, handleNext, handlePrev, handleSave, handleClose, handleCycleGrating, showHelp]);

  const sliderDebounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const handleRedshiftSliderChange = useCallback((value: number) => {
    clearTimeout(sliderDebounceRef.current);
    sliderDebounceRef.current = setTimeout(() => {
      inspectionState.setRedshiftInspected(value.toFixed(4));
    }, 100);
  }, [inspectionState]);

  useEffect(() => () => clearTimeout(sliderDebounceRef.current), []);

  const currentRedshift = inspectionState.currentRedshift ?? 0;
  const headerGratings = useMemo(
    () => tabs.map(t => ({ grating: t.label, title: t.title })),
    [tabs]
  );
  const programDisplay = currentObject.programs.length === 1
    ? (currentObject.member_targets[0]?.program_name ?? null)
    : `${currentObject.programs.length} programs`;

  return (
    <div
      className="fixed inset-0 z-[200] bg-background dark:bg-slate-900 flex flex-col overflow-hidden"
      style={{ isolation: 'isolate' }}
      data-inspection-mode
    >
      {!queueReady ? (
        <div className="h-12 border-b border-border dark:border-slate-700 px-4 flex items-center justify-between bg-background dark:bg-slate-900 flex-shrink-0">
          <div />
          <Loader2 className="w-4 h-4 animate-spin text-text-secondary dark:text-slate-400" />
          <div className="flex items-center gap-1">
            <button
              onClick={() => setShowHelp(prev => !prev)}
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
          targetId={currentObject.object_id}
          field={currentObject.field}
          programName={programDisplay}
          index={queue.index}
          total={queue.total}
          loading={isNavigating}
          hasPrev={!!queue.prev}
          hasNext={!!queue.next}
          commentCount={commentCount}
          gratings={headerGratings}
          activeGratingIdx={activeTabIdx}
          onGratingChange={(idx) => {
            const t = tabs[idx];
            if (t) setActiveKey(`${t.targetId}::${t.spectrum.grating}`);
          }}
          onPrev={handlePrev}
          onNext={handleNext}
          onToggleHelp={() => setShowHelp(prev => !prev)}
          onClose={handleClose}
        />
      )}

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

      {!queueReady && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center text-text-secondary dark:text-slate-400">
            Loading inspection queue...
          </div>
        </div>
      )}

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

      {queueReady && !queue.isEmpty && (
        <div className="flex-1 flex min-h-0">
          <div className="flex-1 flex flex-col min-w-0 px-4 py-2">
            {activeTab && (
              <div className="flex-1 min-h-0 overflow-auto">
                <SpectrumPlot
                  key={`${activeTab.targetId}-${activeTab.spectrum.grating}`}
                  fitsPath={activeTab.spectrum.fits_path}
                  grating={activeTab.spectrum.grating}
                  initialRedshift={currentRedshift}
                  inspectionMode
                  getCachedData={getCachedGrating}
                  onRedshiftChange={canEdit ? handleRedshiftSliderChange : undefined}
                />
              </div>
            )}
          </div>

          <DashboardPanel
            object={currentObject}
            inspectionState={inspectionState}
            canEdit={canEdit}
            commentCount={commentCount}
            queueIds={queue.ids}
            onNavigateToObject={handleNavigate}
            redshiftInputRef={redshiftInputRef}
            redshiftSectionRef={redshiftSectionRef}
            onSave={handleSave}
            onSaveAndNext={handleSaveAndNext}
          />
        </div>
      )}

      {showHelp && <KeyboardShortcutSheet onClose={() => setShowHelp(false)} />}
    </div>
  );
};
