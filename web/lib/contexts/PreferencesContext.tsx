'use client';

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { useAuth } from './AuthContext';
import type {
  UserPreferences,
  SpectrumPreferences,
  ThemeSetting,
  AccentColorName,
} from '@/lib/types';
import {
  DEFAULT_USER_PREFERENCES,
  DEFAULT_SPECTRUM_PREFERENCES,
  DEFAULT_ACCENT_COLOR,
  getAccentColor,
} from '@/lib/types';
import { useTheme } from './ThemeContext';

interface PreferencesContextValue {
  preferences: UserPreferences;
  spectrumPreferences: SpectrumPreferences;
  accentColor: AccentColorName;
  accentColorHex: string; // Current hex value (respects light/dark mode)
  isLoading: boolean;
  updateTheme: (theme: ThemeSetting) => void;
  updateAccentColor: (color: AccentColorName) => void;
  updateSpectrumPreferences: (prefs: Partial<SpectrumPreferences>) => void;
}

const PreferencesContext = createContext<PreferencesContextValue | undefined>(undefined);

// Debounce helper
function useDebouncedCallback<T extends (...args: Parameters<T>) => void>(
  callback: T,
  delay: number
): T {
  const timeoutRef = useRef<NodeJS.Timeout | null>(null);
  const callbackRef = useRef(callback);

  // Keep callback ref updated
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  return useCallback(
    ((...args: Parameters<T>) => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      timeoutRef.current = setTimeout(() => {
        callbackRef.current(...args);
      }, delay);
    }) as T,
    [delay]
  );
}

// Apply accent color CSS variables to document root
function applyAccentColorCSS(colorName: AccentColorName, isDark: boolean) {
  if (typeof document === 'undefined') return;

  const color = getAccentColor(colorName);
  const root = document.documentElement;

  root.style.setProperty('--primary', isDark ? color.dark : color.light);
  root.style.setProperty('--primary-hover', isDark ? color.hover.dark : color.hover.light);
}

export function PreferencesProvider({ children }: { children: React.ReactNode }) {
  const { user, userProfile } = useAuth();
  const { setTheme, resolvedTheme } = useTheme();
  const [preferences, setPreferences] = useState<UserPreferences>(DEFAULT_USER_PREFERENCES);
  const [isLoading, setIsLoading] = useState(true);

  // Load preferences from user profile
  useEffect(() => {
    if (userProfile?.preferences) {
      // Merge saved preferences with defaults
      const savedPrefs = userProfile.preferences as Partial<UserPreferences>;
      setPreferences({
        theme: savedPrefs.theme || DEFAULT_USER_PREFERENCES.theme,
        accentColor: savedPrefs.accentColor || DEFAULT_ACCENT_COLOR,
        spectrum: {
          ...DEFAULT_SPECTRUM_PREFERENCES,
          ...(savedPrefs.spectrum || {}),
        },
      });

      // Sync theme setting with ThemeContext
      if (savedPrefs.theme) {
        setTheme(savedPrefs.theme);
      }
    }
    setIsLoading(false);
  }, [userProfile, setTheme]);

  // Apply accent color CSS whenever accent color or theme changes
  useEffect(() => {
    applyAccentColorCSS(preferences.accentColor, resolvedTheme === 'dark');
  }, [preferences.accentColor, resolvedTheme]);

  // Save preferences to API
  const saveToApi = useCallback(async (prefs: Partial<UserPreferences>) => {
    if (!user) return;

    try {
      await fetch('/api/profile', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: prefs }),
      });
    } catch (error) {
      console.error('Failed to save preferences:', error);
    }
  }, [user]);

  // Debounced save
  const debouncedSave = useDebouncedCallback(saveToApi, 500);

  const updateTheme = useCallback((theme: ThemeSetting) => {
    setPreferences(prev => ({ ...prev, theme }));
    setTheme(theme);

    if (user) {
      debouncedSave({ theme });
    }
  }, [user, setTheme, debouncedSave]);

  const updateAccentColor = useCallback((accentColor: AccentColorName) => {
    setPreferences(prev => ({ ...prev, accentColor }));

    if (user) {
      debouncedSave({ accentColor });
    }
  }, [user, debouncedSave]);

  const updateSpectrumPreferences = useCallback((prefs: Partial<SpectrumPreferences>) => {
    setPreferences(prev => ({
      ...prev,
      spectrum: { ...prev.spectrum, ...prefs },
    }));

    if (user) {
      debouncedSave({ spectrum: prefs as SpectrumPreferences });
    }
  }, [user, debouncedSave]);

  // Get current accent color hex based on theme
  const accentColorHex = getAccentColor(preferences.accentColor)[resolvedTheme === 'dark' ? 'dark' : 'light'];

  return (
    <PreferencesContext.Provider
      value={{
        preferences,
        spectrumPreferences: preferences.spectrum,
        accentColor: preferences.accentColor,
        accentColorHex,
        isLoading,
        updateTheme,
        updateAccentColor,
        updateSpectrumPreferences,
      }}
    >
      {children}
    </PreferencesContext.Provider>
  );
}

export function usePreferences() {
  const context = useContext(PreferencesContext);
  if (context === undefined) {
    throw new Error('usePreferences must be used within a PreferencesProvider');
  }
  return context;
}
