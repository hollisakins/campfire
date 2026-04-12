'use client';

import React, { useMemo, useCallback } from 'react';
import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import type { ObjectDetail } from '@/lib/types';
import { MEMBER_COLORS } from '@/lib/types';
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

  const handleTabChange = useCallback((tab: string) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set('tab', tab);
    // Clear grating when switching to overview
    if (tab === 'overview') {
      params.delete('grating');
    }
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }, [router, pathname, searchParams]);

  // Resolve active target for target tabs
  const activeTarget = useMemo(() => {
    if (activeTab === 'overview') return null;
    return object.member_targets.find(m => m.target_id === activeTab) || null;
  }, [activeTab, object.member_targets]);

  // If tab param doesn't match any target and isn't 'overview', fall back
  const resolvedTab = activeTarget ? activeTab : 'overview';

  return (
    <div className="flex gap-6 min-h-[600px]">
      {/* Sidebar */}
      <ObjectSidebar
        members={object.member_targets}
        activeTab={resolvedTab}
        onTabChange={handleTabChange}
        colors={colors}
      />

      {/* Main panel */}
      <div className="flex-1 min-w-0">
        {resolvedTab === 'overview' ? (
          <OverviewTab
            object={object}
            colors={colors}
            onTargetClick={handleTabChange}
          />
        ) : activeTarget ? (
          <TargetTab
            key={activeTarget.target_id}
            target={activeTarget}
            field={object.field}
            initialGrating={grating}
            color={colors[activeTarget.target_id]}
          />
        ) : null}
      </div>
    </div>
  );
};
