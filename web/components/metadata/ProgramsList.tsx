'use client';

import React from 'react';
import { Telescope } from 'lucide-react';
import { ProgramRow } from './ProgramRow';
import { EmptyState } from '@/components/ui/EmptyState';
import type { ProgramOverview } from '@/lib/actions/programs';

interface ProgramsListProps {
  programs: ProgramOverview[];
}

export const ProgramsList: React.FC<ProgramsListProps> = ({ programs }) => {
  if (programs.length === 0) {
    return (
      <EmptyState
        icon={Telescope}
        title="No programs match these filters"
        description="Try clearing some filters or broadening your search."
      />
    );
  }

  return (
    <div className="bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg overflow-hidden">
      <div className="grid grid-cols-[auto,1fr,auto,auto] gap-4 px-4 py-2 bg-card-hover dark:bg-slate-700/40 text-[11px] uppercase tracking-wider font-semibold text-text-secondary dark:text-slate-400 border-b border-border dark:border-slate-700">
        <div className="w-8" />
        <div>Program · stats</div>
        <div className="text-right">Status</div>
        <div />
      </div>
      <div>
        {programs.map((p) => (
          <ProgramRow key={p.slug} program={p} />
        ))}
      </div>
    </div>
  );
};
