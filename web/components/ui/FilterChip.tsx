'use client';

import React, { useState, useRef, useEffect } from 'react';
import { ChevronDown, X } from 'lucide-react';

export interface FilterOption {
  value: string | number;
  label: string;
  short?: string;
  icon?: string;
  color?: string;
}

// Utility function to darken and saturate a hex color
function darkenColor(hex: string, percent: number): string {
  // Remove # if present
  const color = hex.replace('#', '');

  // Parse RGB components (0-255)
  const num = parseInt(color, 16);
  let r = (num >> 16) & 0xff;
  let g = (num >> 8) & 0xff;
  let b = num & 0xff;

  // Convert RGB to HSL
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

  // Increase saturation by 20% and darken by the specified percent
  s = Math.min(1, s * 1.2);
  l = l * (1 - percent / 100);

  // Convert HSL back to RGB
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

  // Convert back to 0-255 range and hex
  const rHex = Math.round(rOut * 255);
  const gHex = Math.round(gOut * 255);
  const bHex = Math.round(bOut * 255);

  return '#' + ((rHex << 16) | (gHex << 8) | bHex).toString(16).padStart(6, '0');
}

interface FilterChipProps {
  label: string;
  options: FilterOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
  multiSelect?: boolean;
  className?: string;
}

export const FilterChip: React.FC<FilterChipProps> = ({
  label,
  options,
  selected,
  onChange,
  multiSelect = true,
  className = '',
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
    if (multiSelect) {
      if (selected.includes(value)) {
        onChange(selected.filter(v => v !== value));
      } else {
        onChange([...selected, value]);
      }
    } else {
      onChange(selected.includes(value) ? [] : [value]);
      setIsOpen(false);
    }
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    onChange([]);
  };

  const isActive = selected.length > 0;
  const displayText = isActive
    ? `${label} (${selected.length})`
    : label;

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
        <span>{displayText}</span>
        {isActive ? (
          <X
            className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer"
            onClick={handleClear}
          />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {/* Dropdown */}
      {isOpen && (
        <div className="absolute z-50 mt-1 min-w-[240px] max-w-[320px] max-h-[400px] overflow-y-auto bg-background border border-border rounded-lg shadow-lg">
          {/* Chips container */}
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
                      ? 'border-transparent text-gray-900'
                      : 'border-border bg-card/50 text-text-secondary hover:bg-card hover:text-text-primary'
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
                  {/* Icon */}
                  {option.icon && (
                    <span className="text-sm">{option.icon}</span>
                  )}

                  {/* Label */}
                  <span>{option.label}</span>
                </button>
              );
            })}
          </div>

          {/* Clear all button */}
          {multiSelect && selected.length > 0 && (
            <div className="border-t border-border p-2">
              <button
                onClick={() => {
                  onChange([]);
                  setIsOpen(false);
                }}
                className="w-full px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary hover:bg-card rounded-md text-left"
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
