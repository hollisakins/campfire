'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import { CommentWithUser } from '@/lib/types';
import {
  REDSHIFT_QUALITY,
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
  decodeBitmask,
  encodeBitmask,
  getQualityDef,
} from '@/lib/flags';
import {
  MessageSquare,
  Send,
  Save,
  Loader2,
  AlertCircle,
  CheckCircle,
} from 'lucide-react';

interface InspectionPanelProps {
  objectDbId: number;
  objectId: string;
  initialData: {
    redshift_auto: number | null;
    redshift_inspected: number | null;
    redshift_quality: number;
    spectral_features: number;
    object_flags: number;
    dq_flags: number;
    last_inspected_at: string | null;
    last_inspected_by: string | null;
  };
}

export const InspectionPanel: React.FC<InspectionPanelProps> = ({
  objectDbId,
  initialData,
}) => {
  const { user, userProfile } = useAuth();
  const supabase = createClient();
  const canEdit = user && userProfile?.can_comment;

  // Inspection form state
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

  // Track if form has unsaved changes
  const [hasChanges, setHasChanges] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Last inspector info
  const [lastInspectorName, setLastInspectorName] = useState<string | null>(null);

  // Comments state
  const [comments, setComments] = useState<CommentWithUser[]>([]);
  const [newComment, setNewComment] = useState('');
  const [commentsLoading, setCommentsLoading] = useState(true);
  const [commentSubmitting, setCommentSubmitting] = useState(false);
  const [commentError, setCommentError] = useState<string | null>(null);

  // Fetch last inspector name
  useEffect(() => {
    async function fetchInspectorName() {
      if (initialData.last_inspected_by) {
        const { data } = await supabase
          .from('user_profiles')
          .select('full_name')
          .eq('user_id', initialData.last_inspected_by)
          .single();
        setLastInspectorName(data?.full_name || null);
      }
    }
    fetchInspectorName();
  }, [initialData.last_inspected_by, supabase]);

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

  // Fetch comments
  const fetchComments = useCallback(async () => {
    if (!user) {
      setComments([]);
      setCommentsLoading(false);
      return;
    }

    try {
      setCommentsLoading(true);
      const { data: commentsData, error } = await supabase
        .from('comments')
        .select('*')
        .eq('object_id', objectDbId)
        .eq('is_deleted', false)
        .order('created_at', { ascending: true });

      if (error) throw error;

      if (!commentsData || commentsData.length === 0) {
        setComments([]);
        return;
      }

      const userIds = [...new Set(commentsData.map(c => c.user_id))];
      const { data: profilesData } = await supabase
        .from('user_profiles')
        .select('*')
        .in('user_id', userIds);

      const commentsWithProfiles = commentsData.map(comment => ({
        ...comment,
        user_profile: profilesData?.find(p => p.user_id === comment.user_id) || null,
      }));

      setComments(commentsWithProfiles);
    } catch (err) {
      console.error('Error fetching comments:', err);
      setCommentError('Failed to load comments');
    } finally {
      setCommentsLoading(false);
    }
  }, [objectDbId, user, supabase]);

  useEffect(() => {
    fetchComments();
  }, [fetchComments]);

  // Save inspection changes
  const handleSave = async () => {
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
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save changes');
    } finally {
      setSaving(false);
    }
  };

  // Submit comment
  const handleCommentSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!user || !newComment.trim()) return;

    setCommentSubmitting(true);
    setCommentError(null);

    try {
      const { error } = await supabase
        .from('comments')
        .insert({
          object_id: objectDbId,
          user_id: user.id,
          content: newComment.trim(),
        });

      if (error) throw error;

      setNewComment('');
      await fetchComments();
    } catch (err) {
      console.error('Error posting comment:', err);
      setCommentError('Failed to post comment');
    } finally {
      setCommentSubmitting(false);
    }
  };

  // Convert flag definitions to FilterChip options
  const spectralFeatureOptions: FilterOption[] = SPECTRAL_FEATURES.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const objectFlagOptions: FilterOption[] = OBJECT_FLAGS.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const dqFlagOptions: FilterOption[] = DQ_FLAGS.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const currentRedshift = redshiftInspected
    ? parseFloat(redshiftInspected)
    : initialData.redshift_auto;

  const qualityDef = getQualityDef(redshiftQuality);

  return (
    <>
      {/* Inspection Card */}
      <Card className="p-6 mb-4">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
            Inspection
          </h3>
          {initialData.last_inspected_at && (
            <span className="text-xs text-text-secondary dark:text-slate-400">
              Last edited: {formatDate(initialData.last_inspected_at)}
              {lastInspectorName && ` by ${lastInspectorName}`}
            </span>
          )}
        </div>

        {/* Save status messages */}
        {saveError && (
          <div className="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg flex items-start gap-2">
            <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-red-800 dark:text-red-400">{saveError}</p>
          </div>
        )}

        {saveSuccess && (
          <div className="mb-4 p-3 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 rounded-lg flex items-start gap-2">
            <CheckCircle className="w-5 h-5 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-green-800 dark:text-green-400">Changes saved successfully</p>
          </div>
        )}

        {/* Compact Redshift & Classification Section (Single Line) */}
        <div className="mb-4 flex justify-between items-center flex-wrap gap-3 text-sm">
          {/* Left side: Redshift info */}
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <span className="text-text-secondary dark:text-slate-400">Redshift:</span>
              <span className="font-mono font-semibold text-text-primary dark:text-slate-100">
                {currentRedshift?.toFixed(4) ?? '—'}
              </span>
              <span className="text-xs text-text-secondary dark:text-slate-400">
                ({redshiftInspected ? 'overridden' : 'auto-fit'})
              </span>
            </div>

            <span className="text-border dark:text-slate-600">|</span>

            <div className="flex items-center gap-2">
              <label className="text-text-secondary dark:text-slate-400">Override?</label>
              <input
                type="number"
                step="0.0001"
                value={redshiftInspected}
                onChange={e => setRedshiftInspected(e.target.value)}
                placeholder="Leave blank to use auto"
                disabled={!canEdit}
                className="w-64 px-2 py-1 text-sm font-mono border border-border dark:border-slate-600 rounded bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-60"
              />
            </div>

            <span className="text-border dark:text-slate-600">|</span>

            <div className="flex items-center gap-2">
              <label className="text-text-secondary dark:text-slate-400">
                Quality<span className="text-red-500 dark:text-red-400">*</span>:
              </label>
              <select
                value={redshiftQuality}
                onChange={e => setRedshiftQuality(parseInt(e.target.value))}
                disabled={!canEdit}
                className="px-2 py-1 text-sm border border-border dark:border-slate-600 rounded focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-60"
                style={{ backgroundColor: qualityDef.color }}
              >
                {REDSHIFT_QUALITY.map(q => (
                  <option key={q.value} value={q.value}>
                    {q.icon} {q.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Right side: Classification flags */}
          <div className="flex flex-wrap items-center gap-2">
            <FilterChip
              label="Features"
              options={spectralFeatureOptions}
              selected={spectralFeatures}
              onChange={setSpectralFeatures}
              disabled={!canEdit}
            />
            <FilterChip
              label="Object Type"
              options={objectFlagOptions}
              selected={objectFlags}
              onChange={setObjectFlags}
              disabled={!canEdit}
            />
            <FilterChip
              label="Data Quality"
              options={dqFlagOptions}
              selected={dqFlags}
              onChange={setDqFlags}
              disabled={!canEdit}
            />
          </div>
        </div>

        {/* Save Button (Right Aligned, Small) */}
        {canEdit && (
          <div className="flex justify-end items-center gap-3">
            {hasChanges && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                You have unsaved changes
              </p>
            )}
            {redshiftQuality === 0 && (
              <p className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
                <AlertCircle className="w-3 h-3" />
                Please set redshift quality before saving
              </p>
            )}
            <Button
              variant="primary"
              size="sm"
              onClick={handleSave}
              disabled={!hasChanges || saving || redshiftQuality === 0}
            >
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Saving...
                </>
              ) : (
                <>
                  <Save className="w-4 h-4 mr-2" />
                  Save
                </>
              )}
            </Button>
          </div>
        )}
      </Card>

      {/* Discussion Card */}
      <Card className="p-6">
        <div className="flex items-center gap-2 mb-4">
          <MessageSquare className="w-5 h-5 text-primary" />
          <h4 className="font-medium text-text-primary dark:text-slate-100">
            Discussion ({comments.length})
          </h4>
        </div>

        {commentError && (
          <div className="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg">
            <p className="text-sm text-red-800 dark:text-red-400">{commentError}</p>
          </div>
        )}

        {/* Comment Form */}
        {canEdit && (
          <form onSubmit={handleCommentSubmit} className="mb-4">
            <textarea
              value={newComment}
              onChange={e => setNewComment(e.target.value)}
              placeholder="Add a comment..."
              className="w-full px-4 py-2 border border-border dark:border-slate-600 rounded-lg bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary resize-none text-sm"
              rows={2}
              disabled={commentSubmitting}
            />
            <div className="mt-2 flex justify-end">
              <Button
                type="submit"
                variant="secondary"
                size="sm"
                disabled={commentSubmitting || !newComment.trim()}
              >
                <Send className="w-4 h-4 mr-1" />
                {commentSubmitting ? 'Posting...' : 'Post'}
              </Button>
            </div>
          </form>
        )}

        {!user && (
          <div className="mb-4 p-4 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg text-center">
            <p className="text-sm text-text-secondary dark:text-slate-400">Sign in to add comments</p>
          </div>
        )}

        {/* Comments List */}
        {commentsLoading ? (
          <div className="text-center py-4 text-text-secondary dark:text-slate-400 text-sm">
            Loading comments...
          </div>
        ) : comments.length === 0 ? (
          <div className="text-center py-4 text-text-secondary dark:text-slate-400 text-sm">
            No comments yet
          </div>
        ) : (
          <div className="space-y-3">
            {comments.map(comment => (
              <div
                key={comment.id}
                className="p-3 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg"
              >
                <div className="flex items-start justify-between mb-1">
                  <span className="font-medium text-text-primary dark:text-slate-100 text-sm">
                    {comment.user_profile?.full_name || 'Unknown User'}
                  </span>
                  <span className="text-xs text-text-secondary dark:text-slate-500">
                    {formatDate(comment.created_at)}
                  </span>
                </div>
                <p className="text-sm text-text-primary dark:text-slate-100 whitespace-pre-wrap">
                  {comment.content}
                </p>
              </div>
            ))}
          </div>
        )}
      </Card>
    </>
  );
};
