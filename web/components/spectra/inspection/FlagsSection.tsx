'use client';

import React from 'react';
import { SPECTRAL_FEATURES, OBJECT_FLAGS, DQ_FLAGS, getContrastColor, type FlagDef } from '@/lib/flags';
import type { InspectionState } from '@/lib/hooks/useInspectionState';

interface FlagsSectionProps {
  state: InspectionState;
  canEdit: boolean;
}

// Flag pill button
const FlagPill: React.FC<{
  flag: FlagDef;
  active: boolean;
  disabled: boolean;
  onClick: () => void;
}> = ({ flag, active, disabled, onClick }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium transition-colors border
      ${active
        ? 'border-transparent text-gray-900'
        : 'border-border dark:border-slate-600 text-text-secondary dark:text-slate-400 bg-transparent hover:bg-card dark:hover:bg-slate-700'
      }
      disabled:opacity-50 disabled:cursor-not-allowed`}
    style={active ? { backgroundColor: flag.color, color: getContrastColor(flag.color) } : undefined}
    title={flag.description}
  >
    <span>{flag.icon}</span>
    <span>{flag.short}</span>
  </button>
);

export const FlagsSection: React.FC<FlagsSectionProps> = ({ state, canEdit }) => {
  return (
    <div className="p-4 border-b border-border dark:border-slate-700">
      <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase mb-3">
        Flags
      </h3>

      <div className="space-y-3">
        {/* Spectral Features */}
        <div>
          <p className="text-xs text-text-secondary dark:text-slate-400 mb-1.5">
            Features:
          </p>
          <div className="flex flex-wrap gap-1">
            {SPECTRAL_FEATURES.map((f) => (
              <FlagPill
                key={f.key}
                flag={f}
                active={state.spectralFeatures.includes(f.value)}
                disabled={!canEdit}
                onClick={() => state.toggleFlag('spectralFeatures', f.value)}
              />
            ))}
          </div>
        </div>

        {/* Object Type */}
        <div>
          <p className="text-xs text-text-secondary dark:text-slate-400 mb-1.5">
            Type:
          </p>
          <div className="flex flex-wrap gap-1">
            {OBJECT_FLAGS.map((f) => (
              <FlagPill
                key={f.key}
                flag={f}
                active={state.objectFlags.includes(f.value)}
                disabled={!canEdit}
                onClick={() => state.toggleFlag('objectFlags', f.value)}
              />
            ))}
          </div>
        </div>

        {/* DQ Flags */}
        <div>
          <p className="text-xs text-text-secondary dark:text-slate-400 mb-1.5">
            Data Quality:
          </p>
          <div className="flex flex-wrap gap-1">
            {DQ_FLAGS.map((f) => (
              <FlagPill
                key={f.key}
                flag={f}
                active={state.dqFlags.includes(f.value)}
                disabled={!canEdit}
                onClick={() => state.toggleFlag('dqFlags', f.value)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
