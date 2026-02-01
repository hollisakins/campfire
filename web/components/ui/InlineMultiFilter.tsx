'use client';

import React from 'react';
import type { FilterMode } from '@/components/spectra/SpectraFilterBar';

interface FilterOption {
  value: string | number;
  label: string;
  icon?: string;
  color?: string;
}

interface InlineMultiFilterProps {
  label: string;
  options: FilterOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
  mode: FilterMode;
  onModeChange: (mode: FilterMode) => void;
}

function darkenColor(hex: string, percent: number): string {
  const color = hex.replace('#', '');
  const num = parseInt(color, 16);
  let r = (num >> 16) & 0xff;
  let g = (num >> 8) & 0xff;
  let b = num & 0xff;

  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  let h = 0, s = 0, l = (max + min) / 2;

  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
      case g: h = ((b - r) / d + 2) / 6; break;
      case b: h = ((r - g) / d + 4) / 6; break;
    }
  }

  s = Math.min(1, s * 1.2);
  l = l * (1 - percent / 100);

  const hue2rgb = (p: number, q: number, t: number) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1/6) return p + (q - p) * 6 * t;
    if (t < 1/2) return q;
    if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
    return p;
  };

  let rOut, gOut, bOut;
  if (s === 0) {
    rOut = gOut = bOut = l;
  } else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    rOut = hue2rgb(p, q, h + 1/3);
    gOut = hue2rgb(p, q, h);
    bOut = hue2rgb(p, q, h - 1/3);
  }

  const rHex = Math.round(rOut * 255);
  const gHex = Math.round(gOut * 255);
  const bHex = Math.round(bOut * 255);

  return '#' + ((rHex << 16) | (gHex << 8) | bHex).toString(16).padStart(6, '0');
}

export function InlineMultiFilter({
  label,
  options,
  selected,
  onChange,
  mode,
  onModeChange,
}: InlineMultiFilterProps) {
  const toggle = (value: string | number) => {
    if (selected.includes(value)) {
      onChange(selected.filter(v => v !== value));
    } else {
      onChange([...selected, value]);
    }
  };

  const hasSelection = selected.length > 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <label className="text-sm font-medium text-text-primary dark:text-slate-200">
          {label}
        </label>
        {/* Always show mode selector to prevent layout shift */}
        <div className={`flex gap-0.5 bg-slate-100 dark:bg-slate-800 rounded-md p-0.5 transition-opacity duration-200 ${hasSelection ? 'opacity-100' : 'opacity-40 pointer-events-none'}`}>
          {(['any', 'all', 'none'] as FilterMode[]).map((m) => (
            <button
              key={m}
              onClick={() => onModeChange(m)}
              disabled={!hasSelection}
              className={`
                px-2.5 py-1 text-xs font-medium rounded transition-all duration-200
                ${mode === m
                  ? m === 'any'
                    ? 'bg-primary text-white shadow-sm'
                    : m === 'all'
                      ? 'bg-green-500 text-white shadow-sm'
                      : 'bg-red-500 text-white shadow-sm'
                  : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                }
              `}
            >
              {m === 'any' ? 'Any' : m === 'all' ? 'All' : 'None'}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => {
          const isSelected = selected.includes(option.value);
          return (
            <button
              key={option.value}
              onClick={() => toggle(option.value)}
              className={`
                inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm
                border transition-all duration-200
                ${isSelected
                  ? 'border-transparent text-gray-900 dark:text-slate-100 shadow-sm'
                  : 'border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700 hover:border-slate-300 dark:hover:border-slate-600'
                }
              `}
              style={
                isSelected && option.color
                  ? { backgroundColor: `${option.color}60`, borderColor: darkenColor(option.color, 30) }
                  : isSelected
                    ? { backgroundColor: 'rgb(192 38 211 / 0.4)', borderColor: darkenColor('#c026d3', 30) }
                    : undefined
              }
            >
              {option.icon && <span>{option.icon}</span>}
              <span>{option.label}</span>
            </button>
          );
        })}
      </div>
      {/* Always reserve space for description to prevent layout shift */}
      <p className={`mt-2 text-xs text-text-secondary dark:text-slate-500 h-4 transition-opacity duration-200 ${hasSelection ? 'opacity-100' : 'opacity-0'}`}>
        {mode === 'any' && 'Show objects with any of the selected'}
        {mode === 'all' && 'Show objects with all of the selected'}
        {mode === 'none' && 'Exclude objects with any of the selected'}
        {!hasSelection && '\u00A0'}
      </p>
    </div>
  );
}
