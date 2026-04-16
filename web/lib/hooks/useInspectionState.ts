'use client';

import { useState, useCallback, useRef, useMemo } from 'react';

/**
 * Phase D: object-level inspection state.
 *
 * - `objectDbId` is the parent object's DB id, not a target id.
 * - Saves go to PATCH /api/objects/[id]/inspect with `expected_version`.
 * - Quality + override are the only writable fields here; per-spectrum DQ
 *   flags get their own dedicated PATCH (/api/spectra/[id]/dq) and aren't
 *   tracked by this hook.
 * - 409 from the server is surfaced via `saveError` as a refresh prompt;
 *   the design rejects merge UI in favour of a hard reload.
 */

export interface InspectionInitialData {
  redshift_auto: number | null;
  redshift_inspected: number | null;
  redshift_quality: number;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  /** Required for the optimistic-locking save path. Defaults to 1. */
  version?: number;
}

export interface SaveIfDirtyResult {
  saved: boolean;
  reason?: 'no-changes' | 'quality-zero' | 'save-failed' | 'already-saving' | 'version-conflict';
  version?: number;
}

export interface InspectionState {
  redshiftInspected: string;
  setRedshiftInspected: (value: string) => void;
  redshiftQuality: number;
  setRedshiftQuality: (value: number) => void;
  hasChanges: boolean;
  saving: boolean;
  saveError: string | null;
  saveSuccess: boolean;
  /** True if the last save returned 409 — UI should prompt full refresh. */
  versionConflict: boolean;
  currentRedshift: number | null;
  /** Latest known object version (server-of-record). */
  version: number;
  save: () => Promise<{ success: boolean; conflict?: boolean; version?: number }>;
  isDirty: () => boolean;
  saveIfDirty: () => Promise<SaveIfDirtyResult>;
  resetState: (newData: InspectionInitialData) => void;
  /** Counter that increments on every resetState call, used to force effect re-runs */
  resetKey: number;
}

export function useInspectionState(
  objectDbId: number,
  initialData: InspectionInitialData,
): InspectionState {
  const [redshiftInspected, _setRedshiftInspected] = useState<string>(
    initialData.redshift_inspected?.toString() ?? ''
  );
  const [redshiftQuality, _setRedshiftQuality] = useState(initialData.redshift_quality);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [versionConflict, setVersionConflict] = useState(false);
  const [version, setVersion] = useState((initialData.version ?? 1));
  const [saveCount, setSaveCount] = useState(0);
  const [resetKey, setResetKey] = useState(0);

  // Refs hold the always-current values (state lags by a render).
  const valuesRef = useRef({
    redshiftInspected: initialData.redshift_inspected?.toString() ?? '',
    redshiftQuality: initialData.redshift_quality,
  });
  const initialDataRef = useRef(initialData);
  const objectDbIdRef = useRef(objectDbId);
  const versionRef = useRef((initialData.version ?? 1));
  const savingRef = useRef(false);

  objectDbIdRef.current = objectDbId;

  const setRedshiftInspected = useCallback((value: string) => {
    valuesRef.current.redshiftInspected = value;
    _setRedshiftInspected(value);
  }, []);

  const setRedshiftQuality = useCallback((value: number) => {
    valuesRef.current.redshiftQuality = value;
    _setRedshiftQuality(value);
  }, []);

  const hasChanges = useMemo(() => {
    const currentRedshiftInspected = redshiftInspected === '' ? null : parseFloat(redshiftInspected);
    const init = initialDataRef.current;
    return (
      currentRedshiftInspected !== init.redshift_inspected ||
      redshiftQuality !== init.redshift_quality
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [redshiftInspected, redshiftQuality, saveCount]);

  const isDirty = useCallback((): boolean => {
    const v = valuesRef.current;
    const init = initialDataRef.current;
    const currentRI = v.redshiftInspected === '' ? null : parseFloat(v.redshiftInspected);
    return (
      currentRI !== init.redshift_inspected ||
      v.redshiftQuality !== init.redshift_quality
    );
  }, []);

  const currentRedshift = redshiftInspected
    ? parseFloat(redshiftInspected)
    : initialData.redshift_auto;

  const save = useCallback(async (): Promise<{ success: boolean; conflict?: boolean; version?: number }> => {
    if (savingRef.current) {
      return { success: true };
    }

    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    setVersionConflict(false);

    const v = valuesRef.current;
    const id = objectDbIdRef.current;
    const expected = versionRef.current;

    try {
      const response = await fetch(`/api/objects/${id}/inspect`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          redshift_inspected: v.redshiftInspected === '' ? null : v.redshiftInspected,
          redshift_quality: v.redshiftQuality,
          expected_version: expected,
        }),
      });

      const data = await response.json();

      if (response.status === 409) {
        setVersionConflict(true);
        setSaveError(data.message || 'Inspection state has been changed, please refresh.');
        return { success: false, conflict: true, version: data.current_version };
      }

      if (!response.ok) {
        const errorMsg = data.details
          ? `${data.error}: ${data.details}`
          : data.error || 'Failed to save changes';
        throw new Error(errorMsg);
      }

      const newVersion = data.object?.version ?? expected + 1;
      versionRef.current = newVersion;
      setVersion(newVersion);

      initialDataRef.current = {
        ...initialDataRef.current,
        redshift_inspected: v.redshiftInspected === '' ? null : parseFloat(v.redshiftInspected),
        redshift_quality: v.redshiftQuality,
        version: newVersion,
      };

      setSaveSuccess(true);
      setSaveCount(c => c + 1);
      setTimeout(() => setSaveSuccess(false), 3000);
      return { success: true, version: newVersion };
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save changes');
      return { success: false };
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, []);

  const saveIfDirty = useCallback(async (): Promise<SaveIfDirtyResult> => {
    if (savingRef.current) {
      return { saved: false, reason: 'already-saving' };
    }

    if (!isDirty()) {
      return { saved: false, reason: 'no-changes' };
    }

    if (valuesRef.current.redshiftQuality === 0) {
      return { saved: false, reason: 'quality-zero' };
    }

    const result = await save();
    if (result.conflict) {
      return { saved: false, reason: 'version-conflict', version: result.version };
    }
    return result.success
      ? { saved: true, version: result.version }
      : { saved: false, reason: 'save-failed' };
  }, [isDirty, save]);

  const resetState = useCallback((newData: InspectionInitialData) => {
    initialDataRef.current = newData;
    valuesRef.current = {
      redshiftInspected: newData.redshift_inspected?.toString() ?? '',
      redshiftQuality: newData.redshift_quality,
    };
    versionRef.current = newData.version ?? 1;
    _setRedshiftInspected(newData.redshift_inspected?.toString() ?? '');
    _setRedshiftQuality(newData.redshift_quality);
    setVersion(newData.version ?? 1);
    setSaveSuccess(false);
    setSaveError(null);
    setVersionConflict(false);
    setResetKey(k => k + 1);
  }, []);

  return {
    redshiftInspected,
    setRedshiftInspected,
    redshiftQuality,
    setRedshiftQuality,
    hasChanges,
    saving,
    saveError,
    saveSuccess,
    versionConflict,
    currentRedshift,
    version,
    save,
    isDirty,
    saveIfDirty,
    resetState,
    resetKey,
  };
}
