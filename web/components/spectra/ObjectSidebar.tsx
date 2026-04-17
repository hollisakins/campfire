'use client';

import React from 'react';
import type { ObjectMemberTarget } from '@/lib/types';
import { getSpectrumShade } from './plotting-utils';

interface ObjectSidebarProps {
  members: ObjectMemberTarget[];
  /** Color per target_id (sidebar palette). */
  colors: Record<string, string>;
  /** Spectrum.id → visible? */
  spectrumVisibility: Record<number, boolean>;
  /** Per-spectrum checkbox toggle. */
  onSpectrumVisibility: (spectrumId: number, visible: boolean) => void;
  /** Target row checkbox: applies `visible` to ALL of the target's spectra (and shutters). */
  onTargetVisibility: (targetId: string, visible: boolean) => void;
  /** Click on a child spectrum row jumps to its detail card (auto-expands + scrolls). */
  onJumpToSpectrum: (spectrumId: number) => void;
}

function formatExposureTime(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}hr`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}min`;
  return `${Math.round(seconds)}s`;
}

type TargetState = 'on' | 'off' | 'partial';

function targetState(member: ObjectMemberTarget, visibility: Record<number, boolean>): TargetState {
  if (member.spectra.length === 0) return 'off';
  let onCount = 0;
  for (const s of member.spectra) {
    if (visibility[s.id]) onCount++;
  }
  if (onCount === 0) return 'off';
  if (onCount === member.spectra.length) return 'on';
  return 'partial';
}

export const ObjectSidebar: React.FC<ObjectSidebarProps> = ({
  members,
  colors,
  spectrumVisibility,
  onSpectrumVisibility,
  onTargetVisibility,
  onJumpToSpectrum,
}) => {
  return (
    <nav>
      <div className="space-y-2">
        {members.map((member) => {
          const tState = targetState(member, spectrumVisibility);
          const targetColor = colors[member.target_id] ?? '#94a3b8';

          return (
            <div key={member.target_id}>
              {/* Target row */}
              <div className="flex items-start gap-2 px-1 py-1 rounded-md hover:bg-gray-100 dark:hover:bg-slate-800">
                <input
                  type="checkbox"
                  checked={tState === 'on'}
                  ref={(el) => {
                    if (el) el.indeterminate = tState === 'partial';
                  }}
                  onChange={() => onTargetVisibility(member.target_id, tState !== 'on')}
                  style={{ accentColor: targetColor }}
                  className="mt-1 rounded border-gray-300 dark:border-slate-600 focus:ring-accent w-3.5 h-3.5"
                  title={`Toggle ${member.target_id} + shutters`}
                />
                <div className="flex-1 min-w-0">
                  <span
                    className="text-xs font-mono truncate text-text-primary dark:text-slate-200 block"
                    title={member.target_id}
                  >
                    {member.target_id}
                  </span>
                  <span className="text-[11px] text-text-secondary dark:text-slate-500 truncate block">
                    {member.program_name}
                  </span>
                </div>
              </div>

              {/* Child spectrum rows */}
              <ul className="pl-5 mt-0.5 space-y-0.5">
                {member.spectra.map((spectrum, i) => {
                  const visible = spectrumVisibility[spectrum.id] ?? true;
                  const childColor = getSpectrumShade(targetColor, i);

                  return (
                    <li key={spectrum.id} className="flex items-center gap-1.5">
                      <span className="text-text-secondary/40 dark:text-slate-600 text-xs leading-none select-none">
                        ↳
                      </span>
                      <input
                        type="checkbox"
                        checked={visible}
                        onChange={(e) => onSpectrumVisibility(spectrum.id, e.target.checked)}
                        style={{ accentColor: childColor }}
                        className="rounded border-gray-300 dark:border-slate-600 focus:ring-accent w-3.5 h-3.5"
                        title={`Toggle ${spectrum.grating} on the comparison plot`}
                      />
                      <button
                        type="button"
                        onClick={() => onJumpToSpectrum(spectrum.id)}
                        className="flex-1 min-w-0 text-left text-xs px-1 py-0.5 rounded hover:bg-gray-100 dark:hover:bg-slate-800 hover:text-primary dark:hover:text-primary text-text-primary dark:text-slate-200"
                        title={`Jump to ${spectrum.grating} card`}
                      >
                        <span className="font-mono">{spectrum.grating}</span>
                        <span className="ml-1.5 text-text-secondary dark:text-slate-500">
                          ({formatExposureTime(spectrum.exposure_time)})
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
    </nav>
  );
};
