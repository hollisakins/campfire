'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X } from 'lucide-react';

interface QuickRange {
  label: string;
  min: number | null;
  max: number | null;
}

interface RangeFilterChipProps {
  label: string;
  min?: number | null;
  max?: number | null;
  onChange: (min: number | null, max: number | null) => void;
  minBound?: number;
  maxBound?: number;
  step?: number;
  precision?: number;
  quickRanges?: QuickRange[];
  className?: string;
}

export const RangeFilterChip: React.FC<RangeFilterChipProps> = ({
  label,
  min,
  max,
  onChange,
  minBound = 0,
  maxBound = 15,
  step = 0.1,
  precision = 2,
  quickRanges,
  className = '',
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const [localMin, setLocalMin] = useState<string>(min?.toString() ?? '');
  const [localMax, setLocalMax] = useState<string>(max?.toString() ?? '');
  const containerRef = useRef<HTMLDivElement>(null);

  // Sync local state with props
  useEffect(() => {
    setLocalMin(min?.toString() ?? '');
    setLocalMax(max?.toString() ?? '');
  }, [min, max]);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleApply = () => {
    const parsedMin = localMin ? parseFloat(localMin) : null;
    const parsedMax = localMax ? parseFloat(localMax) : null;
    onChange(parsedMin, parsedMax);
    setIsOpen(false);
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    setLocalMin('');
    setLocalMax('');
    onChange(null, null);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleApply();
    }
  };

  const isActive = min != null || max != null;

  const getDisplayText = () => {
    if (!isActive) return label;
    if (min != null && max != null) {
      return `${label}: ${min.toFixed(precision)} - ${max.toFixed(precision)}`;
    }
    if (min != null) {
      return `${label}: ≥ ${min.toFixed(precision)}`;
    }
    if (max != null) {
      return `${label}: ≤ ${max.toFixed(precision)}`;
    }
    return label;
  };

  return (
    <div ref={containerRef} className={`relative inline-block ${className}`}>
      {/* Chip Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium
          border transition-all duration-150
          ${isActive
            ? 'bg-primary/10 border-primary text-primary'
            : 'bg-card border-border text-text-secondary hover:border-text-secondary hover:text-text-primary'
          }
        `}
      >
        <span className="truncate max-w-[200px]">{getDisplayText()}</span>
        {isActive ? (
          <X
            className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer flex-shrink-0"
            onClick={handleClear}
          />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform flex-shrink-0 ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {/* Dropdown */}
      {isOpen && (
        <div className="absolute z-50 mt-1 w-[280px] bg-background border border-border rounded-lg shadow-lg p-4">
          <div className="space-y-4">
            {/* Range inputs */}
            <div className="flex items-center gap-2">
              <div className="flex-1">
                <label className="block text-xs text-text-secondary mb-1">Min</label>
                <input
                  type="number"
                  value={localMin}
                  onChange={(e) => setLocalMin(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={minBound.toString()}
                  min={minBound}
                  max={maxBound}
                  step={step}
                  className="w-full px-3 py-2 text-sm border border-border rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                />
              </div>
              <span className="text-text-secondary mt-5">—</span>
              <div className="flex-1">
                <label className="block text-xs text-text-secondary mb-1">Max</label>
                <input
                  type="number"
                  value={localMax}
                  onChange={(e) => setLocalMax(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={maxBound.toString()}
                  min={minBound}
                  max={maxBound}
                  step={step}
                  className="w-full px-3 py-2 text-sm border border-border rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                />
              </div>
            </div>

            {/* Quick presets */}
            <div>
              <label className="block text-xs text-text-secondary mb-2">Quick ranges</label>
              <div className="flex flex-wrap gap-1.5">
                {(quickRanges ?? [
                  { label: '0-1', min: 0, max: 1 },
                  { label: '1-3', min: 1, max: 3 },
                  { label: '3-6', min: 3, max: 6 },
                  { label: '6-10', min: 6, max: 10 },
                  { label: '>10', min: 10, max: null },
                ]).map((preset) => (
                  <button
                    key={preset.label}
                    onClick={() => {
                      setLocalMin(preset.min?.toString() ?? '');
                      setLocalMax(preset.max?.toString() ?? '');
                    }}
                    className="px-2 py-1 text-xs rounded border border-border hover:border-primary hover:text-primary transition-colors"
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Action buttons */}
            <div className="flex gap-2 pt-2 border-t border-border">
              <button
                onClick={() => {
                  setLocalMin('');
                  setLocalMax('');
                }}
                className="flex-1 px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
              >
                Clear
              </button>
              <button
                onClick={handleApply}
                className="flex-1 px-3 py-1.5 text-sm bg-primary text-white rounded-md hover:bg-primary-hover transition-colors"
              >
                Apply
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
