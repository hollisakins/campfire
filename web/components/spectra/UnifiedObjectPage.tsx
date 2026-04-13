'use client';

import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import type { ObjectDetail, ObjectMemberTarget, Spectrum } from '@/lib/types';
import { MEMBER_COLORS } from '@/lib/types';
import { useInspectionState, type InspectionInitialData } from '@/lib/hooks/useInspectionState';
import { MetricCards } from '@/components/spectra/MetricCards';
import { DownloadButtons } from '@/components/spectra/DownloadButtons';
import { CopyLinkButton } from '@/components/spectra/CopyLinkButton';
import { CoordinateDisplay } from '@/components/spectra/CoordinateDisplay';
import { ShowOnMapLink } from '@/components/map/ShowOnMapLink';
import { FloatingInspectionPanel } from './FloatingInspectionPanel';
import { TileThumbnailWithToggle } from './TileThumbnailWithToggle';
import { ObjectSidebar } from './ObjectSidebar';
import { OverviewTab } from './OverviewTab';
import { TargetTab } from './TargetTab';

interface UnifiedObjectPageProps {
  object: ObjectDetail;
}

export const UnifiedObjectPage: React.FC<UnifiedObjectPageProps> = ({
  object,
}) => {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const activeTab = searchParams.get('tab') || 'overview';
  const grating = searchParams.get('grating') || undefined;

  // Stable color assignment
  const colors = useMemo(() => {
    const c: Record<string, string> = {};
    object.member_targets.forEach((m, i) => {
      c[m.target_id] = MEMBER_COLORS[i % MEMBER_COLORS.length];
    });
    return c;
  }, [object.member_targets]);

  // Program slug → human-readable name
  const programNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const m of object.member_targets) {
      if (!names[m.program_slug]) {
        names[m.program_slug] = m.program_name;
      }
    }
    return names;
  }, [object.member_targets]);

  // === Shared visibility + order state (used by sidebar + overview spectrum viewer) ===
  const [memberOrder, setMemberOrder] = useState<string[]>(
    () => object.member_targets.map(m => m.target_id)
  );

  const [visibility, setVisibility] = useState<Record<string, boolean>>(() => {
    const vis: Record<string, boolean> = {};
    object.member_targets.forEach((m) => {
      vis[m.target_id] = true;
    });
    return vis;
  });

  const handleVisibilityChange = useCallback((targetId: string, visible: boolean) => {
    setVisibility(prev => ({ ...prev, [targetId]: visible }));
  }, []);

  const handleToggleAll = useCallback((visible: boolean) => {
    setVisibility(prev => {
      const next = { ...prev };
      for (const m of object.member_targets) {
        next[m.target_id] = visible;
      }
      return next;
    });
  }, [object.member_targets]);

  const handleReorder = useCallback((newOrder: string[]) => {
    setMemberOrder(newOrder);
  }, []);

  // Ordered + filtered members
  const orderedMembers = useMemo(() => {
    const memberMap = new Map<string, ObjectMemberTarget>(
      object.member_targets.map(m => [m.target_id, m])
    );
    return memberOrder
      .map(id => memberMap.get(id))
      .filter((m): m is ObjectMemberTarget => m != null);
  }, [object.member_targets, memberOrder]);

  // === Tab + target resolution ===
  const activeTarget = useMemo(() => {
    if (activeTab === 'overview') return null;
    return object.member_targets.find(m => m.target_id === activeTab) || null;
  }, [activeTab, object.member_targets]);

  const resolvedTab = activeTarget ? activeTab : 'overview';

  const isSingleton = object.member_targets.length === 1;

  // The target whose inspection state is active:
  // - on a target tab: that target
  // - on overview with a singleton: the only target
  // - on overview with multiple targets: null (inspection disabled)
  const resolvedTarget = useMemo(() => {
    if (activeTarget) return activeTarget;
    if (isSingleton) return object.member_targets[0];
    return null;
  }, [activeTarget, isSingleton, object.member_targets]);

  // === Lifted inspection state ===
  const emptyInitial: InspectionInitialData = {
    redshift_auto: null, redshift_inspected: null, redshift_quality: 0,
    spectral_features: 0, dq_flags: 0, last_inspected_at: null, last_inspected_by: null,
  };

  const initialDataForTarget = useMemo((): InspectionInitialData => {
    if (!resolvedTarget) return emptyInitial;
    return {
      redshift_auto: resolvedTarget.redshift_auto,
      redshift_inspected: resolvedTarget.redshift_inspected,
      redshift_quality: resolvedTarget.redshift_quality,
      spectral_features: resolvedTarget.spectral_features,
      dq_flags: resolvedTarget.dq_flags,
      last_inspected_at: resolvedTarget.last_inspected_at,
      last_inspected_by: resolvedTarget.last_inspected_by,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedTarget?.id]);

  const inspection = useInspectionState(
    resolvedTarget?.id ?? -1,
    initialDataForTarget,
  );

  // Reset inspection state when the resolved target changes
  const prevTargetIdRef = useRef(resolvedTarget?.id ?? -1);
  useEffect(() => {
    const newId = resolvedTarget?.id ?? -1;
    if (newId !== prevTargetIdRef.current) {
      prevTargetIdRef.current = newId;
      if (resolvedTarget) {
        inspection.resetState({
          redshift_auto: resolvedTarget.redshift_auto,
          redshift_inspected: resolvedTarget.redshift_inspected,
          redshift_quality: resolvedTarget.redshift_quality,
          spectral_features: resolvedTarget.spectral_features,
          dq_flags: resolvedTarget.dq_flags,
          last_inspected_at: resolvedTarget.last_inspected_at,
          last_inspected_by: resolvedTarget.last_inspected_by,
        });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedTarget?.id]);

  // Protect against browser back/forward/close when dirty
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (resolvedTarget && inspection.isDirty()) {
        e.preventDefault();
      }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [resolvedTarget, inspection.isDirty]);

  // === Tab navigation ===
  const handleTabChange = useCallback((tab: string) => {
    // Check for unsaved inspection changes before switching away
    if (resolvedTarget && inspection.isDirty()) {
      const confirmed = window.confirm(
        'You have unsaved inspection changes. Discard and switch targets?'
      );
      if (!confirmed) return;
    }

    const params = new URLSearchParams(searchParams.toString());
    params.set('tab', tab);
    // Grating is only meaningful on initial mount of a specific target tab;
    // clear it on any tab switch to avoid stale/invalid values in the URL
    params.delete('grating');
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }, [router, pathname, searchParams, resolvedTarget, inspection]);

  // === Cutout shutter colors: reactive to current state ===
  const cutoutMemberColors = useMemo(() => {
    if (activeTarget) {
      // On a target tab: highlight just that target
      return { [activeTarget.target_id]: colors[activeTarget.target_id] };
    }
    // On overview: show all visible members
    const mc: Record<string, string> = {};
    for (const m of orderedMembers) {
      if (visibility[m.target_id]) {
        mc[m.target_id] = colors[m.target_id];
      }
    }
    return mc;
  }, [activeTarget, orderedMembers, visibility, colors]);

  // Flatten all member spectra for download button
  const allSpectra: Spectrum[] = useMemo(() =>
    object.member_targets.flatMap(m => m.spectra),
    [object.member_targets]
  );

  return (
    <div>
      {/* Sidebar (with cutout) + Header + Main Panel */}
      <div className="flex gap-6 pb-24">
        {/* Desktop sidebar — spans full height from header to bottom */}
        <div className="hidden lg:block">
          <div className="w-[260px] flex-shrink-0 sticky top-4 max-h-[calc(100vh-6rem)] overflow-y-auto border-r border-border dark:border-slate-700 pr-3">
            {/* Cutout — reactive to sidebar/tab state */}
            <div className="mb-3">
              <TileThumbnailWithToggle
                targetId={object.object_id}
                size={600}
                displaySize={240}
                fov={3.2}
                ra={object.ra}
                dec={object.dec}
                field={object.field}
                linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
                memberColors={cutoutMemberColors}
              />
            </div>

            <ObjectSidebar
              members={orderedMembers}
              activeTab={resolvedTab}
              onTabChange={handleTabChange}
              colors={colors}
              visibility={visibility}
              onVisibilityChange={handleVisibilityChange}
              onToggleAll={handleToggleAll}
              onReorder={handleReorder}
            />
          </div>
        </div>

        <div className="flex-1 min-w-0">
          {/* Object Header */}
          <div className="mb-4">
            <h1 className="text-3xl font-bold font-mono text-text-primary dark:text-slate-100 mb-2">
              {object.object_id}
            </h1>
            <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400 mb-3">
              <span>Field:</span>
              <Link
                href={`/nirspec?view=objects&fields=${object.field}`}
                className="inline-flex items-center hover:bg-gray-100 dark:hover:bg-slate-700 px-2 py-1 rounded transition-colors text-text-primary dark:text-slate-100"
              >
                {object.field}
              </Link>
              <span>&middot;</span>
              <span>{object.n_targets} targets</span>
              <span>&middot;</span>
              <span>{object.n_spectra} spectra</span>
            </div>
            <div className="flex items-center gap-4 mb-3">
              <CoordinateDisplay ra={object.ra} dec={object.dec} />
              <ShowOnMapLink ra={object.ra} dec={object.dec} field={object.field} objectId={object.object_id} />
            </div>

            <div className="flex items-end justify-between gap-4 flex-wrap mb-4">
              <MetricCards
                maxSnr={object.max_snr}
                redshift={object.best_redshift}
                redshiftQuality={object.best_redshift_quality}
                numGratings={object.gratings.length}
              />
              <div className="flex gap-4">
                <DownloadButtons spectra={allSpectra} targetId={object.object_id} />
                <CopyLinkButton
                  targetId={object.object_id}
                  url={`/nirspec/objects/${encodeURIComponent(object.object_id)}`}
                />
              </div>
            </div>
          </div>

          {/* Mobile: cutout + target selector (replaces sidebar on small screens) */}
          <div className="lg:hidden mb-4">
            <div className="mb-3">
              <TileThumbnailWithToggle
                targetId={object.object_id}
                size={600}
                displaySize={200}
                fov={3.2}
                ra={object.ra}
                dec={object.dec}
                field={object.field}
                linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
                memberColors={cutoutMemberColors}
              />
            </div>
            <select
              value={resolvedTab}
              onChange={(e) => handleTabChange(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-lg bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary"
            >
              <option value="overview">Overview</option>
              {orderedMembers.map(m => (
                <option key={m.target_id} value={m.target_id}>
                  {m.target_id} — {m.program_name}
                </option>
              ))}
            </select>
          </div>

          {/* Main content */}
          {resolvedTab === 'overview' ? (
            <OverviewTab
              object={object}
              colors={colors}
              orderedMembers={orderedMembers}
              visibility={visibility}
              programNames={programNames}
            />
          ) : activeTarget ? (
            <TargetTab
              key={activeTarget.target_id}
              target={activeTarget}
              initialGrating={grating}
              color={colors[activeTarget.target_id]}
              inspection={inspection}
            />
          ) : null}
        </div>
      </div>

      {/* Floating inspection panel — always visible */}
      <FloatingInspectionPanel
        objectId={object.id}
        ra={object.ra}
        dec={object.dec}
        inspection={resolvedTarget ? inspection : null}
      />
    </div>
  );
};
