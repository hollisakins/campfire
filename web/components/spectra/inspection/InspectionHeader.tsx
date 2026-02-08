'use client';

import React from 'react';
import { ChevronLeft, ChevronRight, HelpCircle, X, Loader2, MessageSquare } from 'lucide-react';

interface InspectionHeaderProps {
  objectId: string;
  field: string;
  programName: string | null;
  index: number;
  total: number;
  loading: boolean;
  hasPrev: boolean;
  hasNext: boolean;
  commentCount: number;
  onPrev: () => void;
  onNext: () => void;
  onToggleHelp: () => void;
  onClose: () => void;
}

export const InspectionHeader: React.FC<InspectionHeaderProps> = ({
  objectId,
  field,
  programName,
  index,
  total,
  loading,
  hasPrev,
  hasNext,
  commentCount,
  onPrev,
  onNext,
  onToggleHelp,
  onClose,
}) => {
  return (
    <div className="h-12 border-b border-border dark:border-slate-700 px-4 flex items-center justify-between bg-background dark:bg-slate-900 flex-shrink-0">
      {/* Left: Navigation */}
      <div className="flex items-center gap-2">
        <button
          onClick={onPrev}
          disabled={!hasPrev}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-slate-700 transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-text-primary dark:text-slate-100"
          aria-label="Previous object"
          title="Previous (← or P)"
        >
          <ChevronLeft className="w-5 h-5" />
        </button>
        <button
          onClick={onNext}
          disabled={!hasNext}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-slate-700 transition-colors disabled:opacity-30 disabled:cursor-not-allowed text-text-primary dark:text-slate-100"
          aria-label="Next object"
          title="Next (→ or N)"
        >
          <ChevronRight className="w-5 h-5" />
        </button>
      </div>

      {/* Center: Object info */}
      <div className="flex items-center gap-3">
        <span className="font-mono font-bold text-text-primary dark:text-slate-100 text-sm">
          {objectId}
        </span>
        {commentCount > 0 && (
          <span className="text-xs text-text-secondary dark:text-slate-400 flex items-center gap-1">
            <MessageSquare className="w-3 h-3" />
            {commentCount}
          </span>
        )}
        <span className="text-text-secondary dark:text-slate-400 text-xs uppercase">
          {programName && `${programName} / `}{field}
        </span>
        <span className="text-text-secondary dark:text-slate-400 text-sm">
          {loading ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin inline" />
          ) : index > 0 && total > 0 ? (
            `${index.toLocaleString()} of ${total.toLocaleString()}`
          ) : null}
        </span>
      </div>

      {/* Right: Help + Close */}
      <div className="flex items-center gap-1">
        <button
          onClick={onToggleHelp}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-slate-700 transition-colors text-text-secondary dark:text-slate-400"
          title="Keyboard shortcuts (?)"
        >
          <HelpCircle className="w-4 h-4" />
        </button>
        <button
          onClick={onClose}
          className="p-1.5 rounded hover:bg-card dark:hover:bg-slate-700 transition-colors text-text-secondary dark:text-slate-400"
          title="Exit inspection mode (Esc)"
        >
          <X className="w-5 h-5" />
        </button>
      </div>
    </div>
  );
};
