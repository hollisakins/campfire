'use client';

import React from 'react';
import { Settings, Sun, Moon, Monitor, Palette, BarChart3 } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { usePreferences } from '@/lib/contexts/PreferencesContext';
import { useTheme } from '@/lib/contexts/ThemeContext';
import type { ThemeSetting, Colorscale2D, FluxUnit } from '@/lib/types';
import { SPECTRUM_COLOR_PRESETS } from '@/lib/types';

const COLORSCALE_OPTIONS: Colorscale2D[] = ['Viridis', 'Plasma', 'Inferno', 'Magma', 'Cividis', 'Greys'];

export const SettingsCard: React.FC = () => {
  const { theme, setTheme } = useTheme();
  const { spectrumPreferences, updateTheme, updateSpectrumPreferences } = usePreferences();

  const themeOptions: { value: ThemeSetting; icon: React.ElementType; label: string }[] = [
    { value: 'light', icon: Sun, label: 'Light' },
    { value: 'system', icon: Monitor, label: 'System' },
    { value: 'dark', icon: Moon, label: 'Dark' },
  ];

  const fluxUnitOptions: { value: FluxUnit; label: string }[] = [
    { value: 'fnu', label: 'fν (μJy)' },
    { value: 'flambda', label: 'fλ (erg/s/cm²/Å)' },
  ];

  return (
    <Card className="p-6">
      <div className="flex items-center gap-3 mb-6">
        <div className="w-10 h-10 bg-primary/10 rounded-full flex items-center justify-center">
          <Settings className="w-5 h-5 text-primary" />
        </div>
        <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">Settings</h2>
      </div>

      {/* Appearance Section */}
      <div className="mb-8">
        <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100 mb-3 flex items-center gap-2">
          <Sun className="w-4 h-4" />
          Appearance
        </h3>

        <div className="space-y-4">
          <div>
            <label className="text-sm text-text-secondary dark:text-slate-400 mb-2 block">Theme</label>
            <div className="flex rounded-lg border border-border dark:border-slate-600 overflow-hidden w-fit">
              {themeOptions.map((option) => {
                const Icon = option.icon;
                const isActive = theme === option.value;
                return (
                  <button
                    key={option.value}
                    onClick={() => {
                      setTheme(option.value);
                      updateTheme(option.value);
                    }}
                    className={`
                      flex items-center gap-2 px-4 py-2 text-sm transition-colors
                      ${isActive
                        ? 'bg-primary text-white'
                        : 'bg-white dark:bg-slate-800 text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-700'
                      }
                    `}
                  >
                    <Icon className="w-4 h-4" />
                    {option.label}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {/* Spectrum Viewer Defaults Section */}
      <div>
        <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100 mb-3 flex items-center gap-2">
          <BarChart3 className="w-4 h-4" />
          Spectrum Viewer Defaults
        </h3>

        <div className="space-y-6">
          {/* Flux Unit */}
          <div>
            <label className="text-sm text-text-secondary dark:text-slate-400 mb-2 block">
              Default Flux Units
            </label>
            <div className="flex rounded-lg border border-border dark:border-slate-600 overflow-hidden w-fit">
              {fluxUnitOptions.map((option) => (
                <button
                  key={option.value}
                  onClick={() => updateSpectrumPreferences({ fluxUnit: option.value })}
                  className={`
                    px-4 py-2 text-sm transition-colors
                    ${spectrumPreferences.fluxUnit === option.value
                      ? 'bg-primary text-white'
                      : 'bg-white dark:bg-slate-800 text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-700'
                    }
                  `}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          {/* 2D Colorscale */}
          <div>
            <label className="text-sm text-text-secondary dark:text-slate-400 mb-2 block">
              2D Spectrum Colormap
            </label>
            <select
              value={spectrumPreferences.colorscale2D}
              onChange={(e) => updateSpectrumPreferences({ colorscale2D: e.target.value as Colorscale2D })}
              className="px-4 py-2 text-sm border border-border dark:border-slate-600 rounded-lg bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary"
            >
              {COLORSCALE_OPTIONS.map((scale) => (
                <option key={scale} value={scale}>
                  {scale}
                </option>
              ))}
            </select>
          </div>

          {/* SNR Range */}
          <div>
            <label className="text-sm text-text-secondary dark:text-slate-400 mb-2 block">
              Default 2D Scale Range
            </label>
            <div className="flex items-center gap-3">
              <input
                type="number"
                value={spectrumPreferences.snrMin}
                onChange={(e) => updateSpectrumPreferences({ snrMin: parseFloat(e.target.value) || 0 })}
                className="w-20 px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-lg bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary"
              />
              <span className="text-text-secondary dark:text-slate-400">to</span>
              <input
                type="number"
                value={spectrumPreferences.snrMax}
                onChange={(e) => updateSpectrumPreferences({ snrMax: parseFloat(e.target.value) || 0 })}
                className="w-20 px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-lg bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
          </div>

          {/* Spectrum Color */}
          <div>
            <label className="text-sm text-text-secondary dark:text-slate-400 mb-2 block flex items-center gap-2">
              <Palette className="w-4 h-4" />
              Spectrum Line Color
            </label>
            <div className="flex flex-wrap gap-2">
              {SPECTRUM_COLOR_PRESETS.map((preset) => (
                <button
                  key={preset.color}
                  onClick={() => updateSpectrumPreferences({ spectrumColor: preset.color })}
                  className={`
                    w-10 h-10 rounded-lg border-2 transition-all
                    ${spectrumPreferences.spectrumColor === preset.color
                      ? 'border-text-primary dark:border-slate-100 scale-110'
                      : 'border-transparent hover:scale-105'
                    }
                  `}
                  style={{ backgroundColor: preset.color }}
                  title={preset.name}
                  aria-label={`Select ${preset.name} color`}
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    </Card>
  );
};
