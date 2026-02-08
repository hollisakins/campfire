'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
  decodeBitmask,
  encodeBitmask,
} from '@/lib/flags';

export interface InspectionInitialData {
  redshift_auto: number | null;
  redshift_inspected: number | null;
  redshift_quality: number;
  spectral_features: number;
  object_flags: number;
  dq_flags: number;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
}

export interface InspectionState {
  redshiftInspected: string;
  setRedshiftInspected: (value: string) => void;
  redshiftQuality: number;
  setRedshiftQuality: (value: number) => void;
  spectralFeatures: (string | number)[];
  setSpectralFeatures: (value: (string | number)[]) => void;
  objectFlags: (string | number)[];
  setObjectFlags: (value: (string | number)[]) => void;
  dqFlags: (string | number)[];
  setDqFlags: (value: (string | number)[]) => void;
  hasChanges: boolean;
  saving: boolean;
  saveError: string | null;
  saveSuccess: boolean;
  currentRedshift: number | null;
  save: () => Promise<boolean>;
  toggleFlag: (category: 'spectralFeatures' | 'objectFlags' | 'dqFlags', value: number) => void;
  resetState: (newData: InspectionInitialData) => void;
}

export function useInspectionState(
  objectDbId: number,
  initialData: InspectionInitialData,
): InspectionState {
  const [redshiftInspected, setRedshiftInspected] = useState<string>(
    initialData.redshift_inspected?.toString() ?? ''
  );
  const [redshiftQuality, setRedshiftQuality] = useState(initialData.redshift_quality);
  const [spectralFeatures, setSpectralFeatures] = useState<(string | number)[]>(
    decodeBitmask(initialData.spectral_features, SPECTRAL_FEATURES)
  );
  const [objectFlags, setObjectFlags] = useState<(string | number)[]>(
    decodeBitmask(initialData.object_flags, OBJECT_FLAGS)
  );
  const [dqFlags, setDqFlags] = useState<(string | number)[]>(
    decodeBitmask(initialData.dq_flags, DQ_FLAGS)
  );

  const [hasChanges, setHasChanges] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Track changes
  useEffect(() => {
    const currentRedshiftInspected = redshiftInspected === '' ? null : parseFloat(redshiftInspected);
    const originalRedshiftInspected = initialData.redshift_inspected;

    const changed =
      currentRedshiftInspected !== originalRedshiftInspected ||
      redshiftQuality !== initialData.redshift_quality ||
      encodeBitmask(spectralFeatures, SPECTRAL_FEATURES) !== initialData.spectral_features ||
      encodeBitmask(objectFlags, OBJECT_FLAGS) !== initialData.object_flags ||
      encodeBitmask(dqFlags, DQ_FLAGS) !== initialData.dq_flags;

    setHasChanges(changed);
  }, [redshiftInspected, redshiftQuality, spectralFeatures, objectFlags, dqFlags, initialData]);

  const currentRedshift = redshiftInspected
    ? parseFloat(redshiftInspected)
    : initialData.redshift_auto;

  const save = useCallback(async (): Promise<boolean> => {
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);

    try {
      const response = await fetch(`/api/objects/${objectDbId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          redshift_inspected: redshiftInspected === '' ? null : redshiftInspected,
          redshift_quality: redshiftQuality,
          spectral_features: encodeBitmask(spectralFeatures, SPECTRAL_FEATURES),
          object_flags: encodeBitmask(objectFlags, OBJECT_FLAGS),
          dq_flags: encodeBitmask(dqFlags, DQ_FLAGS),
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        const errorMsg = data.details
          ? `${data.error}: ${data.details}${data.code ? ` (${data.code})` : ''}`
          : data.error || 'Failed to save changes';
        throw new Error(errorMsg);
      }

      setSaveSuccess(true);
      setHasChanges(false);
      setTimeout(() => setSaveSuccess(false), 3000);
      return true;
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save changes');
      return false;
    } finally {
      setSaving(false);
    }
  }, [objectDbId, redshiftInspected, redshiftQuality, spectralFeatures, objectFlags, dqFlags]);

  const toggleFlag = useCallback((category: 'spectralFeatures' | 'objectFlags' | 'dqFlags', value: number) => {
    const setters = {
      spectralFeatures: setSpectralFeatures,
      objectFlags: setObjectFlags,
      dqFlags: setDqFlags,
    };
    const setter = setters[category];
    setter(prev => {
      const numPrev = prev.map(v => typeof v === 'number' ? v : parseInt(String(v)));
      if (numPrev.includes(value)) {
        return numPrev.filter(v => v !== value);
      } else {
        return [...numPrev, value];
      }
    });
  }, []);

  const resetState = useCallback((newData: InspectionInitialData) => {
    setRedshiftInspected(newData.redshift_inspected?.toString() ?? '');
    setRedshiftQuality(newData.redshift_quality);
    setSpectralFeatures(decodeBitmask(newData.spectral_features, SPECTRAL_FEATURES));
    setObjectFlags(decodeBitmask(newData.object_flags, OBJECT_FLAGS));
    setDqFlags(decodeBitmask(newData.dq_flags, DQ_FLAGS));
    setHasChanges(false);
    setSaveSuccess(false);
    setSaveError(null);
  }, []);

  return {
    redshiftInspected,
    setRedshiftInspected,
    redshiftQuality,
    setRedshiftQuality,
    spectralFeatures,
    setSpectralFeatures,
    objectFlags,
    setObjectFlags,
    dqFlags,
    setDqFlags,
    hasChanges,
    saving,
    saveError,
    saveSuccess,
    currentRedshift,
    save,
    toggleFlag,
    resetState,
  };
}
