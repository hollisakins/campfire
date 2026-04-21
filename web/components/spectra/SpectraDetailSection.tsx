'use client';

import React, { useEffect, useMemo } from 'react';
import { ChevronsDown, ChevronsUp } from 'lucide-react';
import { SpectrumDetailCard } from './SpectrumDetailCard';
import { getSpectrumShade } from './plotting-utils';
import type { ObjectMemberTarget, Spectrum } from '@/lib/types';

interface SpectraDetailSectionProps {
  members: ObjectMemberTarget[];
  /** Spectrum.id → visible? (sidebar checkboxes). */
  spectrumVisibility: Record<number, boolean>;
  /** target_id → hex color (member palette) */
  colors: Record<string, string>;
  /** Object-level redshift used to overlay emission lines on each plot. */
  objectRedshift: number | null;
  /** Controlled expansion state, owned by UnifiedObjectPage. */
  expanded: Set<number>;
  onToggleExpand: (spectrumId: number) => void;
  onExpandAll: () => void;
  onCollapseAll: () => void;
}

interface FlatCard {
  member: ObjectMemberTarget;
  spectrum: Spectrum;
  /** Spectrum's index within member.spectra — drives the shade. */
  memberIndex: number;
}

export const SpectraDetailSection: React.FC<SpectraDetailSectionProps> = ({
  members,
  spectrumVisibility,
  colors,
  objectRedshift,
  expanded,
  onToggleExpand,
  onExpandAll,
  onCollapseAll,
}) => {
  const cards: FlatCard[] = useMemo(
    () =>
      members.flatMap(m =>
        m.spectra
          .map((spectrum, memberIndex) => ({ member: m, spectrum, memberIndex }))
          .filter(({ spectrum }) => spectrumVisibility[spectrum.id] ?? true)
      ),
    [members, spectrumVisibility]
  );

  // J/K stepping: scroll to next/previous card by viewport position.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const isInput =
        target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' ||
        target.isContentEditable;
      if (isInput) return;
      if (e.key !== 'j' && e.key !== 'J' && e.key !== 'k' && e.key !== 'K') return;

      const els = cards
        .map(c => document.getElementById(`spec-card-${c.spectrum.id}`))
        .filter((el): el is HTMLElement => el != null);
      if (els.length === 0) return;

      // Find the card closest to the top of the viewport (offset for header).
      let currentIdx = 0;
      let bestDist = Infinity;
      els.forEach((el, i) => {
        const top = el.getBoundingClientRect().top;
        const dist = Math.abs(top - 80);
        if (dist < bestDist) {
          bestDist = dist;
          currentIdx = i;
        }
      });

      const delta = (e.key === 'j' || e.key === 'J') ? 1 : -1;
      const nextIdx = Math.max(0, Math.min(els.length - 1, currentIdx + delta));
      if (nextIdx === currentIdx) return;
      e.preventDefault();
      els[nextIdx].scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [cards]);

  if (cards.length === 0) {
    return (
      <div className="text-center py-8 text-sm text-text-secondary dark:text-slate-400 border border-dashed border-border dark:border-slate-700 rounded-lg">
        No member spectra are visible. Toggle visibility in the sidebar.
      </div>
    );
  }

  const allExpanded = cards.every(c => expanded.has(c.spectrum.id));

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
          Spectra
        </h2>
        <button
          onClick={allExpanded ? onCollapseAll : onExpandAll}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 border border-border dark:border-slate-700 rounded-md hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
          title="Use J / K to step through cards"
        >
          {allExpanded ? (
            <>
              <ChevronsUp className="w-3.5 h-3.5" />
              Collapse all
            </>
          ) : (
            <>
              <ChevronsDown className="w-3.5 h-3.5" />
              Expand all
            </>
          )}
        </button>
      </div>

      <div className="space-y-3">
        {cards.map(({ member, spectrum, memberIndex }) => (
          <SpectrumDetailCard
            key={spectrum.id}
            cardId={`spec-card-${spectrum.id}`}
            spectrum={spectrum}
            targetId={member.target_id}
            color={getSpectrumShade(colors[member.target_id], memberIndex)}
            expanded={expanded.has(spectrum.id)}
            onToggle={() => onToggleExpand(spectrum.id)}
            objectRedshift={objectRedshift}
          />
        ))}
      </div>
    </div>
  );
};
