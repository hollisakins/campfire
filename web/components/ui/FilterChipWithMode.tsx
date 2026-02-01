'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X } from 'lucide-react';

export type FilterMode = 'any' | 'all' | 'none';

export interface FilterOption {
  value: string | number;
  label: string;
  short?: string;
  icon?: string;
  color?: string;
}

interface FilterChipWithModeProps {
  label: string;
  options: FilterOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
  mode: FilterMode;
  onModeChange: (mode: FilterMode) => void;
  showModeToggle?: boolean;  // Whether to show the mode selector
  modeLabels?: {
    any: string;
    all: string;
    none: string;
  };
  className?: string;
  disabled?: boolean;
}

const DEFAULT_MODE_LABELS = {
  any: 'Match any',
  all: 'Match all',
  none: 'Exclude all',
};

// Utility function to darken and saturate a hex color
function darkenColor(hex: string, percent: number): string {
  const color = hex.replace('#', '');
  const num = parseInt(color, 16);
  let r = (num >> 16) & 0xff;
  let g = (num >> 8) & 0xff;
  let b = num & 0xff;

  r /= 255;
  g /= 255;
  b /= 255;
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

export const FilterChipWithMode: React.FC<FilterChipWithModeProps> = ({
  label,
  options,
  selected,
  onChange,
  mode,
  onModeChange,
  showModeToggle = true,
  modeLabels = DEFAULT_MODE_LABELS,
  className = '',
  disabled = false,
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

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

  const handleToggle = (value: string | number) => {
    if (selected.includes(value)) {
      onChange(selected.filter(v => v !== value));
    } else {
      onChange([...selected, value]);
    }
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    onChange([]);
  };

  const isActive = selected.length > 0;

  // Mode indicator for the chip button
  const getModeIndicator = () => {
    if (!isActive) return '';
    switch (mode) {
      case 'all': return ' +';
      case 'none': return ' -';
      default: return '';
    }
  };

  // Mode color for the chip
  const getModeColor = () => {
    if (!isActive) return '';
    switch (mode) {
      case 'all': return 'bg-green-500/10 border-green-500 text-green-700 dark:text-green-400';
      case 'none': return 'bg-red-500/10 border-red-500 text-red-700 dark:text-red-400';
      default: return 'bg-primary/10 border-primary text-primary';
    }
  };

  const displayText = isActive
    ? `${label} (${selected.length})${getModeIndicator()}`
    : label;

  return (
    <div ref={containerRef} className={`relative inline-block ${className}`}>
      {/* Chip Button */}
      <button
        onClick={() => !disabled && setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium
          border transition-all duration-150
          ${disabled ? 'opacity-60 cursor-not-allowed' : ''}
          ${isActive
            ? getModeColor()
            : 'bg-card dark:bg-slate-800 border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:border-text-secondary dark:hover:border-slate-600 hover:text-text-primary dark:hover:text-slate-200'
          }
        `}
      >
        <span>{displayText}</span>
        {isActive ? (
          <X
            className="w-3.5 h-3.5 hover:opacity-70 cursor-pointer"
            onClick={handleClear}
          />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {/* Dropdown */}
      {isOpen && !disabled && (
        <div className="absolute z-50 mt-1 min-w-[280px] max-w-[360px] max-h-[450px] overflow-y-auto bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg">
          {/* Options */}
          <div className="p-3 flex flex-wrap gap-2">
            {options.map((option) => {
              const isSelected = selected.includes(option.value);
              return (
                <button
                  key={option.value}
                  onClick={() => handleToggle(option.value)}
                  className={`
                    inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm
                    border transition-all duration-150
                    ${isSelected
                      ? 'border-transparent text-gray-900 dark:text-slate-100'
                      : 'border-border dark:border-slate-700 bg-card/50 dark:bg-slate-800/50 text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-200'
                    }
                  `}
                  style={
                    isSelected && option.color
                      ? {
                          backgroundColor: `${option.color}60`,
                          borderColor: darkenColor(option.color, 30),
                        }
                      : isSelected
                        ? {
                            backgroundColor: 'rgb(192 38 211 / 0.4)',
                            borderColor: darkenColor('#c026d3', 30),
                          }
                        : undefined
                  }
                >
                  {option.icon && <span className="text-sm">{option.icon}</span>}
                  <span>{option.label}</span>
                </button>
              );
            })}
          </div>

          {/* Mode selector - only show when there are selections and mode toggle is enabled */}
          {showModeToggle && selected.length > 0 && (
            <div className="border-t border-border dark:border-slate-700 p-3">
              <div className="text-xs font-medium text-text-secondary dark:text-slate-400 mb-2">
                Filter mode
              </div>
              <div className="flex gap-1">
                {(['any', 'all', 'none'] as FilterMode[]).map((m) => (
                  <button
                    key={m}
                    onClick={() => onModeChange(m)}
                    className={`
                      flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors
                      ${mode === m
                        ? m === 'any'
                          ? 'bg-primary text-white'
                          : m === 'all'
                            ? 'bg-green-500 text-white'
                            : 'bg-red-500 text-white'
                        : 'bg-slate-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-600'
                      }
                    `}
                  >
                    {modeLabels[m]}
                  </button>
                ))}
              </div>
              <p className="mt-2 text-xs text-text-secondary dark:text-slate-500">
                {mode === 'any' && 'Show objects matching ANY selected option'}
                {mode === 'all' && 'Show objects matching ALL selected options'}
                {mode === 'none' && 'Exclude objects matching ANY selected option'}
              </p>
            </div>
          )}

          {/* Clear button */}
          {selected.length > 0 && (
            <div className="border-t border-border dark:border-slate-700 p-2">
              <button
                onClick={() => {
                  onChange([]);
                  setIsOpen(false);
                }}
                className="w-full px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 hover:bg-card dark:hover:bg-slate-700 rounded-md text-left"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
