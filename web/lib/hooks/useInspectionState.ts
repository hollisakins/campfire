'use client';

import { useState, useCallback, useRef, useMemo } from 'react';

/**
 * Object-level inspection state.
 *
 * - Saves go to PATCH /api/objects/[id]/inspect with `expected_version`.
 * - Quality + redshift override are the only writable fields; per-spectrum
 *   DQ flags get their own PATCH (/api/spectra/[id]/dq).
 * - 409 surfaces via `saveError` as a refresh prompt (no merge UI).
 */

export interface InspectionInitialData {
  redshift_auto: number | null;
  redshift_inspected: number | null;
  redshift_quality: number;
  /** True when redshift_inspected was auto-pinned from redshift_auto at
   * sign-off. When true, the override input renders empty (the inspector
   * didn't type anything) even though redshift_inspected has a value. */
  inspected_used_auto: boolean;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  /** Required for the optimistic-locking save path. Defaults to 1. */
  version?: number;
}

/** Reduce InspectionInitialData to the string the override input should
 *  display: empty for auto-pinned sign-offs (the user didn't type anything),
 *  the formatted number for explicit overrides. */
function initialOverrideString(data: InspectionInitialData): string {
  if (data.inspected_used_auto) return '';
  return data.redshift_inspected?.toString() ?? '';
}

export interface SaveIfDirtyResult {
  saved: boolean;
  reason?: 'no-changes' | 'quality-zero' | 'save-failed' | 'already-saving' | 'version-conflict';
  version?: number;
}

/** Details about who/when/what caused a 409 — populated from the API body. */
export interface ConflictInfo {
  /** Display name (username or email) of the user who beat us to the save. */
  conflictingUser: string | null;
  /** Server's current last_inspected_at (ISO string). */
  lastInspectedAt: string | null;
  /** Server's current redshift_inspected value. */
  theirRedshiftInspected: number | null;
  /** Server's current redshift_quality value. */
  theirRedshiftQuality: number | null;
  /** Server's current version (for the user to re-try with). */
  theirVersion: number | null;
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
  /** Set when versionConflict is true; carries the server's current values. */
  conflictInfo: ConflictInfo | null;
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
    initialOverrideString(initialData)
  );
  const [redshiftQuality, _setRedshiftQuality] = useState(initialData.redshift_quality);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [versionConflict, setVersionConflict] = useState(false);
  const [conflictInfo, setConflictInfo] = useState<ConflictInfo | null>(null);
  const [version, setVersion] = useState((initialData.version ?? 1));
  const [saveCount, setSaveCount] = useState(0);
  const [resetKey, setResetKey] = useState(0);

  // Refs hold the always-current values (state lags by a render).
  const valuesRef = useRef({
    redshiftInspected: initialOverrideString(initialData),
    redshiftQuality: initialData.redshift_quality,
  });
  const initialDataRef = useRef(initialData);
  const objectDbIdRef = useRef(objectDbId);
  const versionRef = useRef((initialData.version ?? 1));
  const savingRef = useRef(false);

  objectDbIdRef.current = objectDbId;

  const setRedshiftInspected = useCallback((value: string) => {
    if (valuesRef.current.redshiftInspected === value) return;
    valuesRef.current.redshiftInspected = value;
    _setRedshiftInspected(value);
  }, []);

  const setRedshiftQuality = useCallback((value: number) => {
    if (valuesRef.current.redshiftQuality === value) return;
    valuesRef.current.redshiftQuality = value;
    _setRedshiftQuality(value);
  }, []);

  // Compare the form string against what initial display SHOULD be, not the
  // raw init.redshift_inspected. For auto-pinned sign-offs the initial display
  // is empty even though redshift_inspected has a value; without this guard
  // the form would always look dirty after the pin trigger lands.
  const hasChanges = useMemo(() => {
    const init = initialDataRef.current;
    return (
      redshiftInspected !== initialOverrideString(init) ||
      redshiftQuality !== init.redshift_quality
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [redshiftInspected, redshiftQuality, saveCount]);

  const isDirty = useCallback((): boolean => {
    const v = valuesRef.current;
    const init = initialDataRef.current;
    return (
      v.redshiftInspected !== initialOverrideString(init) ||
      v.redshiftQuality !== init.redshift_quality
    );
  }, []);

  // Fall back to the stored redshift_inspected (which may have been pinned by
  // pin_redshift_on_signoff at sign-off time) before redshift_auto, so signed-
  // off objects display their pinned value rather than the latest auto-fit
  // when the form override is empty. Mirrors the COALESCE in objects.redshift.
  const currentRedshift = redshiftInspected
    ? parseFloat(redshiftInspected)
    : (initialData.redshift_inspected ?? initialData.redshift_auto);

  const save = useCallback(async (): Promise<{ success: boolean; conflict?: boolean; version?: number }> => {
    if (savingRef.current) {
      return { success: true };
    }

    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    setVersionConflict(false);
    setConflictInfo(null);

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
        setConflictInfo({
          conflictingUser: data.conflicting_user ?? null,
          lastInspectedAt: data.last_inspected_at ?? null,
          theirRedshiftInspected: data.current_redshift_inspected ?? null,
          theirRedshiftQuality: data.current_redshift_quality ?? null,
          theirVersion: data.current_version ?? null,
        });
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

      // Read post-trigger state from the server response: the
      // pin_redshift_on_signoff trigger may have promoted redshift_auto into
      // redshift_inspected and set inspected_used_auto=true if the user
      // signed off with an empty override. Falling back to optimistic
      // synthesis would mark the form dirty on the next render.
      const serverObj = data.object;
      const persistedInspected: number | null =
        serverObj?.redshift_inspected !== undefined
          ? (serverObj.redshift_inspected === null ? null : Number(serverObj.redshift_inspected))
          : (v.redshiftInspected === '' ? null : parseFloat(v.redshiftInspected));
      const persistedUsedAuto: boolean =
        typeof serverObj?.inspected_used_auto === 'boolean'
          ? serverObj.inspected_used_auto
          : initialDataRef.current.inspected_used_auto;

      initialDataRef.current = {
        ...initialDataRef.current,
        redshift_inspected: persistedInspected,
        redshift_quality: v.redshiftQuality,
        inspected_used_auto: persistedUsedAuto,
        version: newVersion,
      };

      // Re-sync the form string to the new initial display so an empty input
      // after a pin-promotion stays empty (not "dirty against 2.30").
      const newOverrideStr = initialOverrideString(initialDataRef.current);
      valuesRef.current.redshiftInspected = newOverrideStr;
      _setRedshiftInspected(newOverrideStr);

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
    const overrideStr = initialOverrideString(newData);
    valuesRef.current = {
      redshiftInspected: overrideStr,
      redshiftQuality: newData.redshift_quality,
    };
    versionRef.current = newData.version ?? 1;
    _setRedshiftInspected(overrideStr);
    _setRedshiftQuality(newData.redshift_quality);
    setVersion(newData.version ?? 1);
    setSaveSuccess(false);
    setSaveError(null);
    setVersionConflict(false);
    setConflictInfo(null);
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
    conflictInfo,
    currentRedshift,
    version,
    save,
    isDirty,
    saveIfDirty,
    resetState,
    resetKey,
  };
}
