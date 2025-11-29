/**
 * Reusable UI controls for spectrum plotting
 */

import React from 'react';
import type { FluxUnit } from './plotting-utils';

interface FluxUnitToggleProps {
  fluxUnit: FluxUnit;
  onChange: (unit: FluxUnit) => void;
}

export const FluxUnitToggle: React.FC<FluxUnitToggleProps> = ({ fluxUnit, onChange }) => {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-text-secondary">Units:</span>
      <div className="flex rounded-md overflow-hidden border border-border">
        <button
          onClick={() => onChange('fnu')}
          className={`px-3 py-1 text-sm transition-colors ${
            fluxUnit === 'fnu'
              ? 'bg-primary text-white'
              : 'bg-white text-text-secondary hover:bg-gray-100'
          }`}
        >
          fν
        </button>
        <button
          onClick={() => onChange('flambda')}
          className={`px-3 py-1 text-sm transition-colors ${
            fluxUnit === 'flambda'
              ? 'bg-primary text-white'
              : 'bg-white text-text-secondary hover:bg-gray-100'
          }`}
        >
          fλ
        </button>
      </div>
    </div>
  );
};

interface EmissionLinesControlProps {
  showEmissionLines: boolean;
  onChange: (show: boolean) => void;
}

export const EmissionLinesControl: React.FC<EmissionLinesControlProps> = ({
  showEmissionLines,
  onChange,
}) => {
  return (
    <div className="flex items-center gap-2">
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={showEmissionLines}
          onChange={(e) => onChange(e.target.checked)}
          className="w-4 h-4 rounded border-border text-primary focus:ring-primary"
        />
        <span className="text-sm text-text-secondary">Emission lines</span>
      </label>
    </div>
  );
};

interface RedshiftSliderControlProps {
  redshift: number;
  onChange: (z: number) => void;
  min?: number;
  max?: number;
  step?: number;
}

export const RedshiftSliderControl: React.FC<RedshiftSliderControlProps> = ({
  redshift,
  onChange,
  min = 0,
  max = 15,
  step = 0.01,
}) => {
  const [inputValue, setInputValue] = React.useState(redshift.toFixed(4));

  // Update input when redshift prop changes
  React.useEffect(() => {
    setInputValue(redshift.toFixed(4));
  }, [redshift]);

  const handleInputBlur = () => {
    const parsed = parseFloat(inputValue);
    if (!isNaN(parsed) && parsed >= min && parsed <= max) {
      onChange(parsed);
      setInputValue(parsed.toFixed(4));
    } else {
      // Reset to current value if invalid
      setInputValue(redshift.toFixed(4));
    }
  };

  const handleInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      (e.currentTarget as HTMLInputElement).blur();
    }
  };

  const handleSliderChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newValue = parseFloat(e.target.value);
    onChange(newValue);
    setInputValue(newValue.toFixed(4));
  };

  return (
    <div className="flex items-center gap-2 flex-1 max-w-md">
      <span className="text-sm text-text-secondary">z =</span>
      <input
        type="text"
        value={inputValue}
        onChange={(e) => setInputValue(e.target.value)}
        onBlur={handleInputBlur}
        onKeyDown={handleInputKeyDown}
        className="w-20 px-2 py-1 text-sm border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
      />
      <input
        type="range"
        value={redshift}
        onChange={handleSliderChange}
        min={min}
        max={max}
        step={step}
        className="flex-1 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-primary"
      />
    </div>
  );
};

export const ControlDivider: React.FC = () => {
  return <div className="h-6 w-px bg-border" />;
};
