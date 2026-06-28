'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  ShieldCheck,
  MessageSquare,
  Tag,
  Telescope,
  Check,
  Loader2,
  Clock,
  Info,
} from 'lucide-react';

interface InspectionRequest {
  id: number;
  status: 'pending' | 'approved' | 'rejected';
  message: string | null;
  created_at: string;
  reviewed_at?: string | null;
}

/**
 * Subtle role/permissions summary shown on the profile page. Surfaces what the
 * user can do today and — only when they lack inspection rights — offers a
 * low-key way to request them. Deliberately non-judgmental: it states
 * capabilities rather than highlighting what's missing.
 */
export const RolePermissionsCard: React.FC = () => {
  const { userProfile } = useAuth();
  const [request, setRequest] = useState<InspectionRequest | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canComment = !!userProfile?.can_comment;
  const canInspect = !!userProfile?.can_inspect;
  const isGroup = !!userProfile?.is_group_account;

  const fetchRequest = useCallback(async () => {
    try {
      const res = await fetch('/api/inspection-requests');
      if (res.ok) {
        const data = await res.json();
        setRequest(data.request);
      }
    } catch {
      // Non-fatal — the card still renders the static permission summary.
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    // Only inspectors-in-waiting need the request state.
    if (!canInspect && !isGroup) {
      fetchRequest();
    } else {
      setLoaded(true);
    }
  }, [canInspect, isGroup, fetchRequest]);

  const handleRequest = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch('/api/inspection-requests', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || 'Failed to submit request');
      }
      setRequest(data.request);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit request');
    } finally {
      setSubmitting(false);
    }
  };

  if (!userProfile) return null;

  const permissions = [
    {
      icon: MessageSquare,
      label: 'Comment on objects and targets',
      granted: canComment,
    },
    {
      icon: Tag,
      label: 'Create and edit tags',
      granted: canComment && !isGroup,
    },
    {
      icon: Telescope,
      label: 'Submit redshift inspections and quality flags',
      granted: canInspect,
    },
  ];

  const pending = request?.status === 'pending';

  return (
    <Card className="p-6">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 bg-primary/10 rounded-full flex items-center justify-center">
          <ShieldCheck className="w-5 h-5 text-primary" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-text-primary">Role &amp; permissions</h2>
          <p className="text-sm text-text-secondary">
            {canInspect
              ? 'You have full inspection access.'
              : 'What you can do on CAMPFIRE.'}
          </p>
        </div>
      </div>

      <ul className="space-y-2">
        {permissions.map((perm) => (
          <li key={perm.label} className="flex items-center gap-3 text-sm">
            <span
              className={`flex items-center justify-center w-6 h-6 rounded-full flex-shrink-0 ${
                perm.granted
                  ? 'bg-green-100 dark:bg-green-900/40 text-green-600 dark:text-green-400'
                  : 'bg-card text-text-tertiary'
              }`}
            >
              {perm.granted ? <Check className="w-3.5 h-3.5" /> : <perm.icon className="w-3.5 h-3.5" />}
            </span>
            <span className={perm.granted ? 'text-text-primary' : 'text-text-secondary'}>
              {perm.label}
            </span>
          </li>
        ))}
      </ul>

      {/* Inspection-access invitation — only for non-inspectors, kept low-key. */}
      {!canInspect && !isGroup && loaded && (
        <div className="mt-5 pt-5 border-t border-border">
          {pending ? (
            <div className="flex items-start gap-2 text-sm text-text-secondary">
              <Clock className="w-4 h-4 text-yellow-600 dark:text-yellow-400 flex-shrink-0 mt-0.5" />
              <p>
                Your request for inspection access is pending review. We&apos;ll email you
                once an administrator has had a chance to look.
              </p>
            </div>
          ) : (
            <div className="flex items-start gap-3">
              <Info className="w-4 h-4 text-text-tertiary flex-shrink-0 mt-1" />
              <div className="flex-1">
                <p className="text-sm text-text-secondary mb-3">
                  Inspecting spectra — recording redshifts and quality flags — is
                  granted on request. If you&apos;d like to contribute inspections, you
                  can ask an administrator for access.
                  {request?.status === 'rejected' && ' Your previous request wasn’t granted, but you’re welcome to ask again.'}
                </p>
                {error && (
                  <p className="text-sm text-red-600 dark:text-red-400 mb-3">{error}</p>
                )}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleRequest}
                  disabled={submitting}
                >
                  {submitting ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      Requesting...
                    </>
                  ) : (
                    'Request inspection access'
                  )}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
};
