'use client';

import React from 'react';

export interface ChipSelectOption {
  value: string | number;
  label: string;
  short?: string;
  icon?: string;
  color?: string;
  description?: string;
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

interface ChipSelectProps {
  options: ChipSelectOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
  disabled?: boolean;
  className?: string;
}

export const ChipSelect: React.FC<ChipSelectProps> = ({
  options,
  selected,
  onChange,
  disabled = false,
  className = '',
}) => {
  const handleToggle = (value: string | number) => {
    if (disabled) return;

    if (selected.includes(value)) {
      onChange(selected.filter((v) => v !== value));
    } else {
      onChange([...selected, value]);
    }
  };

  return (
    <div className={`flex flex-wrap gap-2 ${className}`}>
      {options.map((option) => {
        const isSelected = selected.includes(option.value);
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => handleToggle(option.value)}
            disabled={disabled}
            title={option.description}
            className={`
              inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm
              border transition-all duration-150
              ${disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}
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
  );
};
