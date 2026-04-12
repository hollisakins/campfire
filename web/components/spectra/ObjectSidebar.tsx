'use client';

import React from 'react';
import { LayoutGrid } from 'lucide-react';
import type { ObjectMemberTarget } from '@/lib/types';
import { QUALITY_LABELS, GRATINGS } from '@/lib/types';

interface ObjectSidebarProps {
  members: ObjectMemberTarget[];
  activeTab: string; // 'overview' | target_id
  onTabChange: (tab: string) => void;
  colors: Record<string, string>;
}

export const ObjectSidebar: React.FC<ObjectSidebarProps> = ({
  members,
  activeTab,
  onTabChange,
  colors,
}) => {
  return (
    <nav className="w-56 flex-shrink-0 border-r border-border dark:border-slate-700 pr-3 space-y-1">
      {/* Overview tab */}
      <button
        onClick={() => onTabChange('overview')}
        className={`w-full text-left px-3 py-2.5 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 ${
          activeTab === 'overview'
            ? 'bg-accent/10 text-accent dark:bg-accent/20'
            : 'text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-800 hover:text-text-primary dark:hover:text-slate-200'
        }`}
      >
        <LayoutGrid className="w-4 h-4 flex-shrink-0" />
        Overview
      </button>

      <div className="border-t border-border dark:border-slate-700 my-2" />

      {/* Member target tabs */}
      {members.map((member) => {
        const qualityDef = QUALITY_LABELS.find(q => q.value === member.redshift_quality);
        const memberGratings = [...new Set(member.spectra.map(s => s.grating))];
        const sortedGratings = GRATINGS.filter(g => memberGratings.includes(g));
        const isActive = activeTab === member.target_id;

        return (
          <button
            key={member.target_id}
            onClick={() => onTabChange(member.target_id)}
            className={`w-full text-left px-3 py-2.5 rounded-lg text-sm transition-colors ${
              isActive
                ? 'bg-accent/10 dark:bg-accent/20'
                : 'hover:bg-gray-100 dark:hover:bg-slate-800'
            }`}
          >
            <div className="flex items-center gap-2 mb-1">
              <div
                className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                style={{ backgroundColor: colors[member.target_id] }}
              />
              <span
                className={`font-mono text-xs truncate ${
                  isActive
                    ? 'text-accent font-medium'
                    : 'text-text-primary dark:text-slate-200'
                }`}
                title={member.target_id}
              >
                {member.target_id}
              </span>
              {qualityDef && (
                <span
                  className="w-2 h-2 rounded-full flex-shrink-0 ml-auto"
                  style={{ backgroundColor: qualityDef.color ?? undefined }}
                  title={qualityDef.label}
                />
              )}
            </div>
            <div className="pl-[18px] text-xs text-text-secondary dark:text-slate-500">
              <div className="truncate">{member.program_name}</div>
              <div className="flex items-center gap-1 mt-0.5">
                {member.redshift != null && (
                  <span className="font-mono">z={member.redshift.toFixed(3)}</span>
                )}
                {sortedGratings.length > 0 && (
                  <span className="text-text-secondary/60 dark:text-slate-600">
                    · {sortedGratings.length}g
                  </span>
                )}
              </div>
            </div>
          </button>
        );
      })}
    </nav>
  );
};
