'use client';

import React, { useState, useEffect } from 'react';

interface InlineRangeProps {
  label: string;
  description?: string;
  min: number | null;
  max: number | null;
  onChange: (min: number | null, max: number | null) => void;
  minBound: number;
  maxBound: number;
  step: number;
  precision?: number;
}

export function InlineRange({
  label,
  description,
  min,
  max,
  onChange,
  minBound,
  maxBound,
  step,
}: InlineRangeProps) {
  const [minValue, setMinValue] = useState(min?.toString() ?? '');
  const [maxValue, setMaxValue] = useState(max?.toString() ?? '');

  useEffect(() => {
    setMinValue(min?.toString() ?? '');
    setMaxValue(max?.toString() ?? '');
  }, [min, max]);

  const handleMinBlur = () => {
    const val = minValue === '' ? null : parseFloat(minValue);
    if (val !== null && (isNaN(val) || val < minBound || val > maxBound)) return;
    onChange(val, max);
  };

  const handleMaxBlur = () => {
    const val = maxValue === '' ? null : parseFloat(maxValue);
    if (val !== null && (isNaN(val) || val < minBound || val > maxBound)) return;
    onChange(min, val);
  };

  const isActive = min !== null || max !== null;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <label className="text-sm font-medium text-text-primary">
          {label}
        </label>
        {isActive && (
          <button
            onClick={() => onChange(null, null)}
            className="text-xs text-text-secondary hover:text-primary transition-colors"
          >
            Clear
          </button>
        )}
      </div>
      {description && (
        <p className="text-xs text-text-tertiary mb-2">{description}</p>
      )}
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <input
            type="number"
            value={minValue}
            onChange={(e) => setMinValue(e.target.value)}
            onBlur={handleMinBlur}
            onKeyDown={(e) => e.key === 'Enter' && handleMinBlur()}
            placeholder={`Min (${minBound})`}
            step={step}
            className={`
              w-full px-3 py-2 text-sm border rounded-lg bg-background
              text-text-primary placeholder:text-text-tertiary transition-all duration-200
              focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
              ${isActive ? 'border-primary/50' : 'border-border'}
            `}
          />
        </div>
        <span className="text-sm text-text-secondary">to</span>
        <div className="flex-1">
          <input
            type="number"
            value={maxValue}
            onChange={(e) => setMaxValue(e.target.value)}
            onBlur={handleMaxBlur}
            onKeyDown={(e) => e.key === 'Enter' && handleMaxBlur()}
            placeholder={`Max (${maxBound})`}
            step={step}
            className={`
              w-full px-3 py-2 text-sm border rounded-lg bg-background
              text-text-primary placeholder:text-text-tertiary transition-all duration-200
              focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
              ${isActive ? 'border-primary/50' : 'border-border'}
            `}
          />
        </div>
      </div>
    </div>
  );
}
