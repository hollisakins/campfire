'use client';

import React, { useState, useMemo, useCallback } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import type { ObjectDetail, ObjectMemberTarget, Spectrum } from '@/lib/types';
import { MEMBER_COLORS } from '@/lib/types';
import { MetricCards } from '@/components/spectra/MetricCards';
import { DownloadButtons } from '@/components/spectra/DownloadButtons';
import { CopyLinkButton } from '@/components/spectra/CopyLinkButton';
import { CoordinateDisplay } from '@/components/spectra/CoordinateDisplay';
import { ShowOnMapLink } from '@/components/map/ShowOnMapLink';
import { ObjectListsSection } from '@/components/spectra/ObjectListsSection';
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

  // === Tab navigation ===
  const handleTabChange = useCallback((tab: string) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set('tab', tab);
    if (tab === 'overview') {
      params.delete('grating');
    }
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }, [router, pathname, searchParams]);

  const activeTarget = useMemo(() => {
    if (activeTab === 'overview') return null;
    return object.member_targets.find(m => m.target_id === activeTab) || null;
  }, [activeTab, object.member_targets]);

  const resolvedTab = activeTarget ? activeTab : 'overview';

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
      {/* Object Header + Cutout */}
      <div className="flex gap-6 items-start mb-6">
        {/* Left: metadata */}
        <div className="flex-1">
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

          <div className="mb-4">
            <ObjectListsSection objectId={object.id} ra={object.ra} dec={object.dec} />
          </div>

          <div className="mb-4">
            <MetricCards
              maxSnr={object.max_snr}
              redshift={object.best_redshift}
              redshiftQuality={object.best_redshift_quality}
              numGratings={object.gratings.length}
            />
          </div>

          <div className="flex gap-4">
            <DownloadButtons spectra={allSpectra} targetId={object.object_id} />
            <CopyLinkButton
              targetId={object.object_id}
              url={`/nirspec/objects/${encodeURIComponent(object.object_id)}`}
            />
          </div>
        </div>

        {/* Right: cutout (single instance, reactive) */}
        <div className="flex-shrink-0" style={{ width: '300px' }}>
          <TileThumbnailWithToggle
            targetId={object.object_id}
            size={600}
            displaySize={300}
            fov={3.2}
            ra={object.ra}
            dec={object.dec}
            field={object.field}
            linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
            memberColors={cutoutMemberColors}
          />
        </div>
      </div>

      {/* Sidebar + Main Panel */}
      <div className="flex gap-6 min-h-[600px]">
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

        <div className="flex-1 min-w-0">
          {resolvedTab === 'overview' ? (
            <OverviewTab
              object={object}
              colors={colors}
              orderedMembers={orderedMembers}
              visibility={visibility}
            />
          ) : activeTarget ? (
            <TargetTab
              key={activeTarget.target_id}
              target={activeTarget}
              initialGrating={grating}
              color={colors[activeTarget.target_id]}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
};
