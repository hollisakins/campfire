'use client';

import { useState, useCallback, useRef, useMemo } from 'react';
import {
  SPECTRAL_FEATURES,
  DQ_FLAGS,
  decodeBitmask,
  encodeBitmask,
} from '@/lib/flags';

export interface InspectionInitialData {
  redshift_auto: number | null;
  redshift_inspected: number | null;
  redshift_quality: number;
  spectral_features: number;
  dq_flags: number;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
}

export interface SaveIfDirtyResult {
  saved: boolean;
  reason?: 'no-changes' | 'quality-zero' | 'save-failed' | 'already-saving';
  propagated?: number;
}

export interface InspectionState {
  redshiftInspected: string;
  setRedshiftInspected: (value: string) => void;
  redshiftQuality: number;
  setRedshiftQuality: (value: number) => void;
  spectralFeatures: (string | number)[];
  setSpectralFeatures: (value: (string | number)[]) => void;
  dqFlags: (string | number)[];
  setDqFlags: (value: (string | number)[]) => void;
  hasChanges: boolean;
  saving: boolean;
  saveError: string | null;
  saveSuccess: boolean;
  propagatedCount: number;
  currentRedshift: number | null;
  save: () => Promise<{ success: boolean; propagated: number }>;
  isDirty: () => boolean;
  saveIfDirty: () => Promise<SaveIfDirtyResult>;
  toggleFlag: (category: 'spectralFeatures' | 'dqFlags', value: number) => void;
  resetState: (newData: InspectionInitialData) => void;
  /** Counter that increments on every resetState call, used to force effect re-runs */
  resetKey: number;
}

export function useInspectionState(
  objectDbId: number,
  initialData: InspectionInitialData,
): InspectionState {
  // === State (for React rendering) ===
  const [redshiftInspected, _setRedshiftInspected] = useState<string>(
    initialData.redshift_inspected?.toString() ?? ''
  );
  const [redshiftQuality, _setRedshiftQuality] = useState(initialData.redshift_quality);
  const [spectralFeatures, _setSpectralFeatures] = useState<(string | number)[]>(
    decodeBitmask(initialData.spectral_features, SPECTRAL_FEATURES)
  );
  const [dqFlags, _setDqFlags] = useState<(string | number)[]>(
    decodeBitmask(initialData.dq_flags, DQ_FLAGS)
  );

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [propagatedCount, setPropagatedCount] = useState(0);
  // Bumped after each save to force useMemo recomputation of hasChanges
  const [saveCount, setSaveCount] = useState(0);
  // Bumped on every resetState to force dependent effects to re-run
  const [resetKey, setResetKey] = useState(0);

  // === Refs (always-current values for synchronous access) ===
  const valuesRef = useRef({
    redshiftInspected: initialData.redshift_inspected?.toString() ?? '',
    redshiftQuality: initialData.redshift_quality,
    spectralFeatures: decodeBitmask(initialData.spectral_features, SPECTRAL_FEATURES) as (string | number)[],
    dqFlags: decodeBitmask(initialData.dq_flags, DQ_FLAGS) as (string | number)[],
  });
  const initialDataRef = useRef(initialData);
  const objectDbIdRef = useRef(objectDbId);
  const savingRef = useRef(false);

  // Keep objectDbId ref in sync
  objectDbIdRef.current = objectDbId;

  // === Setters (update both ref and state) ===
  const setRedshiftInspected = useCallback((value: string) => {
    valuesRef.current.redshiftInspected = value;
    _setRedshiftInspected(value);
  }, []);

  const setRedshiftQuality = useCallback((value: number) => {
    valuesRef.current.redshiftQuality = value;
    _setRedshiftQuality(value);
  }, []);

  const setSpectralFeatures = useCallback((value: (string | number)[]) => {
    valuesRef.current.spectralFeatures = value;
    _setSpectralFeatures(value);
  }, []);

  const setDqFlags = useCallback((value: (string | number)[]) => {
    valuesRef.current.dqFlags = value;
    _setDqFlags(value);
  }, []);

  // === Computed: hasChanges (for display, uses state so React re-renders) ===
  // saveCount is included to force recomputation after save updates initialDataRef
  const hasChanges = useMemo(() => {
    const currentRedshiftInspected = redshiftInspected === '' ? null : parseFloat(redshiftInspected);
    const init = initialDataRef.current;

    return (
      currentRedshiftInspected !== init.redshift_inspected ||
      redshiftQuality !== init.redshift_quality ||
      encodeBitmask(spectralFeatures) !== init.spectral_features ||
      encodeBitmask(dqFlags) !== init.dq_flags
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [redshiftInspected, redshiftQuality, spectralFeatures, dqFlags, saveCount]);

  // === isDirty: synchronous check using refs (always current, no render lag) ===
  const isDirty = useCallback((): boolean => {
    const v = valuesRef.current;
    const init = initialDataRef.current;
    const currentRI = v.redshiftInspected === '' ? null : parseFloat(v.redshiftInspected);

    return (
      currentRI !== init.redshift_inspected ||
      v.redshiftQuality !== init.redshift_quality ||
      encodeBitmask(v.spectralFeatures) !== init.spectral_features ||
      encodeBitmask(v.dqFlags) !== init.dq_flags
    );
  }, []);

  const currentRedshift = redshiftInspected
    ? parseFloat(redshiftInspected)
    : initialData.redshift_auto;

  // === Save: reads from refs, always sends current values ===
  const save = useCallback(async (): Promise<{ success: boolean; propagated: number }> => {
    if (savingRef.current) {
      console.log('[Inspection] Save already in progress, skipping duplicate');
      return { success: true, propagated: 0 };
    }

    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);

    const v = valuesRef.current;
    const id = objectDbIdRef.current;

    try {
      const response = await fetch(`/api/targets/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          redshift_inspected: v.redshiftInspected === '' ? null : v.redshiftInspected,
          redshift_quality: v.redshiftQuality,
          spectral_features: encodeBitmask(v.spectralFeatures),
          dq_flags: encodeBitmask(v.dqFlags),
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        const errorMsg = data.details
          ? `${data.error}: ${data.details}${data.code ? ` (${data.code})` : ''}`
          : data.error || 'Failed to save changes';
        throw new Error(errorMsg);
      }

      // Update initialDataRef to reflect the saved state
      initialDataRef.current = {
        ...initialDataRef.current,
        redshift_inspected: v.redshiftInspected === '' ? null : parseFloat(v.redshiftInspected),
        redshift_quality: v.redshiftQuality,
        spectral_features: encodeBitmask(v.spectralFeatures),
        dq_flags: encodeBitmask(v.dqFlags),
      };

      const propagated = data.propagated || 0;
      setPropagatedCount(propagated);
      setSaveSuccess(true);
      setSaveCount(c => c + 1); // Force hasChanges useMemo recomputation
      setTimeout(() => setSaveSuccess(false), 3000);
      return { success: true, propagated };
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save changes');
      return { success: false, propagated: 0 };
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, []);

  // === saveIfDirty: auto-save with all checks, reads from refs ===
  const saveIfDirty = useCallback(async (): Promise<SaveIfDirtyResult> => {
    if (savingRef.current) {
      console.log('[Inspection] saveIfDirty: already saving');
      return { saved: false, reason: 'already-saving' };
    }

    if (!isDirty()) {
      return { saved: false, reason: 'no-changes' };
    }

    if (valuesRef.current.redshiftQuality === 0) {
      console.log('[Inspection] saveIfDirty: quality is 0, skipping');
      return { saved: false, reason: 'quality-zero' };
    }

    console.log('[Inspection] saveIfDirty: saving changes...');
    const result = await save();
    return result.success
      ? { saved: true, propagated: result.propagated }
      : { saved: false, reason: 'save-failed' };
  }, [isDirty, save]);

  const toggleFlag = useCallback((category: 'spectralFeatures' | 'dqFlags', value: number) => {
    const currentFlags = valuesRef.current[category];
    const numFlags = currentFlags.map(v => typeof v === 'number' ? v : parseInt(String(v)));

    let newFlags: (string | number)[];
    if (numFlags.includes(value)) {
      newFlags = numFlags.filter(v => v !== value);
    } else {
      newFlags = [...numFlags, value];
    }

    // Update ref and state
    valuesRef.current[category] = newFlags;
    const setters = {
      spectralFeatures: _setSpectralFeatures,
      dqFlags: _setDqFlags,
    };
    setters[category](newFlags);
  }, []);

  const resetState = useCallback((newData: InspectionInitialData) => {
    // Update refs first (synchronous)
    initialDataRef.current = newData;
    valuesRef.current = {
      redshiftInspected: newData.redshift_inspected?.toString() ?? '',
      redshiftQuality: newData.redshift_quality,
      spectralFeatures: decodeBitmask(newData.spectral_features, SPECTRAL_FEATURES),
      dqFlags: decodeBitmask(newData.dq_flags, DQ_FLAGS),
    };

    // Update state (async, for rendering)
    _setRedshiftInspected(newData.redshift_inspected?.toString() ?? '');
    _setRedshiftQuality(newData.redshift_quality);
    _setSpectralFeatures(decodeBitmask(newData.spectral_features, SPECTRAL_FEATURES));
    _setDqFlags(decodeBitmask(newData.dq_flags, DQ_FLAGS));
    setSaveSuccess(false);
    setSaveError(null);
    setResetKey(k => k + 1);
  }, []);

  return {
    redshiftInspected,
    setRedshiftInspected,
    redshiftQuality,
    setRedshiftQuality,
    spectralFeatures,
    setSpectralFeatures,
    dqFlags,
    setDqFlags,
    hasChanges,
    saving,
    saveError,
    saveSuccess,
    propagatedCount,
    currentRedshift,
    save,
    isDirty,
    saveIfDirty,
    toggleFlag,
    resetState,
    resetKey,
  };
}
