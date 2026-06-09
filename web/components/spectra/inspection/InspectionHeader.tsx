'use client';

import React from 'react';
import { ChevronLeft, ChevronRight, HelpCircle, X, Loader2, MessageSquare } from 'lucide-react';

interface GratingInfo {
  grating: string;
  /** Optional tooltip shown on the tab button. */
  title?: string;
}

interface InspectionHeaderProps {
  targetId: string;
  field: string;
  programName: string | null;
  index: number;
  total: number;
  loading: boolean;
  hasPrev: boolean;
  hasNext: boolean;
  commentCount: number;
  gratings: GratingInfo[];
  activeGratingIdx: number;
  onGratingChange: (idx: number) => void;
  onPrev: () => void;
  onNext: () => void;
  onToggleHelp: () => void;
  onClose: () => void;
}

export const InspectionHeader: React.FC<InspectionHeaderProps> = ({
  targetId,
  field,
  programName,
  index,
  total,
  loading,
  hasPrev,
  hasNext,
  commentCount,
  gratings,
  activeGratingIdx,
  onGratingChange,
  onPrev,
  onNext,
  onToggleHelp,
  onClose,
}) => {
  return (
    <div className="h-12 border-b border-border px-4 flex items-center justify-between bg-background flex-shrink-0">
      {/* Left: Navigation */}
      <div className="flex items-center gap-2">
        <button
          onClick={onPrev}
          disabled={!hasPrev}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-card-hover transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-text-primary"
          aria-label="Previous object"
          title="Previous (← or P)"
        >
          <ChevronLeft className="w-5 h-5" />
        </button>
        <button
          onClick={onNext}
          disabled={!hasNext}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-card-hover transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-text-primary"
          aria-label="Next object"
          title="Next (→ or N)"
        >
          <ChevronRight className="w-5 h-5" />
        </button>
      </div>

      {/* Center: Object info */}
      <div className="flex items-center gap-3">
        <span className="font-mono font-bold text-text-primary text-sm">
          {targetId}
        </span>
        {commentCount > 0 && (
          <span className="text-xs text-text-secondary flex items-center gap-1">
            <MessageSquare className="w-3 h-3" />
            {commentCount}
          </span>
        )}
        <span className="text-text-secondary text-xs uppercase">
          {programName && `${programName} / `}{field}
        </span>
        <span className="text-text-secondary text-sm">
          {loading ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin inline" />
          ) : index > 0 && total > 0 ? (
            `${index.toLocaleString()} of ${total.toLocaleString()}`
          ) : null}
        </span>
      </div>

      {/* Grating toggle */}
      {gratings.length > 1 && (
        <div className="flex items-center gap-1">
          {gratings.map((spec, idx) => (
            <button
              key={`${spec.grating}-${idx}`}
              onClick={() => onGratingChange(idx)}
              title={spec.title}
              className={`px-3 py-1 text-xs font-medium rounded transition-colors
                ${idx === activeGratingIdx
                  ? 'bg-primary text-on-primary'
                  : 'bg-card text-text-secondary hover:bg-card-hover border border-border dark:border-border-strong'
                }`}
            >
              <span className="mr-1">{spec.grating}</span>
              {idx === activeGratingIdx && (
                <kbd className="text-[10px] opacity-70">G</kbd>
              )}
            </button>
          ))}
        </div>
      )}

      {/* Right: Help + Close */}
      <div className="flex items-center gap-1">
        <button
          onClick={onToggleHelp}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-card-hover transition-colors text-text-secondary"
          title="Keyboard shortcuts (?)"
        >
          <HelpCircle className="w-4 h-4" />
        </button>
        <button
          onClick={onClose}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-card-hover transition-colors text-text-secondary"
          title="Exit inspection mode (Esc)"
        >
          <X className="w-5 h-5" />
        </button>
      </div>
    </div>
  );
};
