'use client';

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { useAuth } from './AuthContext';
import type {
  UserPreferences,
  SpectrumPreferences,
  ThemeSetting,
} from '@/lib/types';
import {
  DEFAULT_USER_PREFERENCES,
  DEFAULT_SPECTRUM_PREFERENCES,
} from '@/lib/types';
import { useTheme } from './ThemeContext';

interface PreferencesContextValue {
  preferences: UserPreferences;
  spectrumPreferences: SpectrumPreferences;
  isLoading: boolean;
  updateTheme: (theme: ThemeSetting) => void;
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

export function PreferencesProvider({ children }: { children: React.ReactNode }) {
  const { user, userProfile } = useAuth();
  const { setTheme } = useTheme();
  const [preferences, setPreferences] = useState<UserPreferences>(DEFAULT_USER_PREFERENCES);
  const [isLoading, setIsLoading] = useState(true);

  // Load preferences from user profile
  useEffect(() => {
    if (userProfile?.preferences) {
      // Merge saved preferences with defaults
      const savedPrefs = userProfile.preferences as Partial<UserPreferences>;
      setPreferences({
        theme: savedPrefs.theme || DEFAULT_USER_PREFERENCES.theme,
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

  const updateSpectrumPreferences = useCallback((prefs: Partial<SpectrumPreferences>) => {
    setPreferences(prev => ({
      ...prev,
      spectrum: { ...prev.spectrum, ...prefs },
    }));

    if (user) {
      debouncedSave({ spectrum: prefs as SpectrumPreferences });
    }
  }, [user, debouncedSave]);

  return (
    <PreferencesContext.Provider
      value={{
        preferences,
        spectrumPreferences: preferences.spectrum,
        isLoading,
        updateTheme,
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
