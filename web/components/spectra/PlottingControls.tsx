/**
 * Reusable UI controls for spectrum plotting
 */

import React from 'react';
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import type { FluxUnit } from './plotting-utils';

interface FluxUnitToggleProps {
  fluxUnit: FluxUnit;
  onChange: (unit: FluxUnit) => void;
}

export const FluxUnitToggle: React.FC<FluxUnitToggleProps> = ({ fluxUnit, onChange }) => {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-text-secondary">Units:</span>
      <div className="flex rounded-md overflow-hidden border border-border dark:border-border-strong">
        <button
          onClick={() => onChange('fnu')}
          className={`px-3 py-1 text-sm transition-colors ${
            fluxUnit === 'fnu'
              ? 'bg-primary text-on-primary'
              : 'bg-background text-text-secondary hover:bg-card-hover'
          }`}
        >
          fν
        </button>
        <button
          onClick={() => onChange('flambda')}
          className={`px-3 py-1 text-sm transition-colors ${
            fluxUnit === 'flambda'
              ? 'bg-primary text-on-primary'
              : 'bg-background text-text-secondary hover:bg-card-hover'
          }`}
        >
          fλ
        </button>
      </div>
    </div>
  );
};

interface PlotCheckboxProps {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  title?: string;
}

/** Labeled checkbox in the plot-controls-bar style. */
export const PlotCheckbox: React.FC<PlotCheckboxProps> = ({
  label,
  checked,
  onChange,
  disabled = false,
  title,
}) => {
  return (
    <div className="flex items-center gap-2">
      <label
        className={`flex items-center gap-2 ${disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}`}
        title={title}
      >
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          className="w-4 h-4 rounded border-border dark:border-border-strong text-primary focus:ring-primary dark:bg-card-hover"
        />
        <span className="text-sm text-text-secondary">{label}</span>
      </label>
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
  return <PlotCheckbox label="Emission lines" checked={showEmissionLines} onChange={onChange} />;
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
  // The range input is bound to this local value, not the (throttled) prop:
  // React would otherwise snap the thumb back to the stale prop between
  // frames while dragging.
  const [sliderValue, setSliderValue] = React.useState(redshift);

  // Update input + slider when redshift prop changes
  React.useEffect(() => {
    setInputValue(redshift.toFixed(4));
    setSliderValue(redshift);
  }, [redshift]);

  // The range input fires `input` events far faster than a Plotly relayout
  // can complete, and each onChange re-laid-out the whole figure (#500).
  // Coalesce to one onChange per animation frame; the text box still tracks
  // every event so the number never lags the thumb.
  const pendingRef = React.useRef<number | null>(null);
  const rafRef = React.useRef<number | null>(null);
  React.useEffect(() => () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
  }, []);
  const scheduleChange = (value: number) => {
    pendingRef.current = value;
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      if (pendingRef.current !== null) onChange(pendingRef.current);
      pendingRef.current = null;
    });
  };

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
    setSliderValue(newValue);
    setInputValue(newValue.toFixed(4));
    scheduleChange(newValue);
  };

  const nudge = (delta: number) => {
    const newValue = Math.min(max, Math.max(min, parseFloat((redshift + delta).toFixed(4))));
    onChange(newValue);
    setInputValue(newValue.toFixed(4));
  };

  const stepBtnClass =
    'p-0.5 rounded text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors disabled:opacity-30 disabled:pointer-events-none';

  return (
    <div className="flex items-center gap-2 flex-1 max-w-md">
      <span className="text-sm text-text-secondary">z =</span>
      <input
        type="text"
        value={inputValue}
        onChange={(e) => setInputValue(e.target.value)}
        onBlur={handleInputBlur}
        onKeyDown={handleInputKeyDown}
        className="w-20 px-2 py-1 text-sm border border-border dark:border-border-strong rounded bg-background dark:bg-card-hover text-text-primary focus:outline-none focus:ring-1 focus:ring-primary"
      />
      <div className="flex items-center gap-0.5">
        <button
          onClick={() => nudge(-0.01)}
          disabled={redshift <= min}
          className={stepBtnClass}
          title="-0.01"
        >
          <ChevronsLeft size={14} />
        </button>
        <button
          onClick={() => nudge(-0.001)}
          disabled={redshift <= min}
          className={stepBtnClass}
          title="-0.001"
        >
          <ChevronLeft size={14} />
        </button>
      </div>
      <input
        type="range"
        value={sliderValue}
        onChange={handleSliderChange}
        min={min}
        max={max}
        step={step}
        className="flex-1 h-2 bg-surface-2 rounded-lg appearance-none cursor-pointer accent-primary"
      />
      <div className="flex items-center gap-0.5">
        <button
          onClick={() => nudge(0.001)}
          disabled={redshift >= max}
          className={stepBtnClass}
          title="+0.001"
        >
          <ChevronRight size={14} />
        </button>
        <button
          onClick={() => nudge(0.01)}
          disabled={redshift >= max}
          className={stepBtnClass}
          title="+0.01"
        >
          <ChevronsRight size={14} />
        </button>
      </div>
    </div>
  );
};

export const ControlDivider: React.FC = () => {
  return <div className="h-6 w-px bg-border dark:bg-border-strong" />;
};
