'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X } from 'lucide-react';
import { Popover, isInsidePopover } from './Popover';
import { RangeEditor, rangeText, type QuickRange } from './RangeEditor';

interface RangeFilterChipProps {
  label: string;
  min?: number | null;
  max?: number | null;
  onChange: (min: number | null, max: number | null) => void;
  minBound?: number;
  maxBound?: number;
  /** Kept for callers; the fields take any decimal. */
  step?: number;
  precision?: number;
  quickRanges?: QuickRange[];
  unit?: string;
  side?: 'min' | 'max';
  className?: string;
  /** Mount with the dropdown open (a chip the user just asked for). */
  defaultOpen?: boolean;
}

const DEFAULT_QUICK: QuickRange[] = [
  { label: '0 – 1', min: 0, max: 1 },
  { label: '1 – 3', min: 1, max: 3 },
  { label: '3 – 6', min: 3, max: 6 },
  { label: '6 – 10', min: 6, max: 10 },
  { label: '> 10', min: 10, max: null },
];

/** A numeric range as a chip: its bounds in the label, the shared range editor in its dropdown. */
export const RangeFilterChip: React.FC<RangeFilterChipProps> = ({
  label,
  min = null,
  max = null,
  onChange,
  minBound,
  maxBound,
  precision = 2,
  quickRanges,
  unit,
  side,
  className = '',
  defaultOpen = false,
}) => {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (!isInsidePopover(event.target, containerRef.current)) setIsOpen(false);
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const isActive = min != null || max != null;
  const text = isActive ? `${label}: ${rangeText(min, max, precision)}` : label;

  return (
    <div ref={containerRef} className={`relative inline-block ${className}`}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium
          border transition-all duration-150
          ${isActive
            ? 'bg-primary/10 border-primary text-primary'
            : 'bg-card border-border text-text-secondary hover:border-text-secondary dark:hover:border-border-strong hover:text-text-primary'
          }
        `}
      >
        <span className="truncate max-w-[200px]">{text}</span>
        {isActive ? (
          <X className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer flex-shrink-0" onClick={(e) => { e.stopPropagation(); onChange(null, null); }} />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform flex-shrink-0 ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {isOpen && (
        <Popover anchorRef={containerRef} open={isOpen} className="w-[300px] bg-card border border-border rounded-lg shadow-lg p-4">
          <RangeEditor min={min} max={max} minBound={minBound} maxBound={maxBound} precision={precision} unit={unit} side={side}
            quickRanges={quickRanges ?? DEFAULT_QUICK}
            onApply={(lo, hi) => { onChange(lo, hi); setIsOpen(false); }} />
        </Popover>
      )}
    </div>
  );
};
